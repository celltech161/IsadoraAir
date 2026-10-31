"""Waveform generation for trusted caches, and Refresh Waveform Only (roadmap 1.21, r0123 Package B).

``measure`` produces the waveform data (stereo display samples and the mono RMS envelope) with the
analyzer's own decode and compute primitives, so a refreshed cache is exactly what analysis would
have produced for the same audio and configuration. ``identify`` adds the decoded-audio identity.

``refresh(track_id)`` regenerates one Track's cache from its current file and changes nothing
else: no Track column (markers, their state and revision, ``waveform_path``, duration,
Ready-to-Air, relations), no source byte, tag or format -- it never calls ``analyze_one_track`` or
``transcode_lossless_to_flac``, and it performs no database write at all. The source witness is
taken before decoding and must be unchanged afterwards and again at promotion; the marker
snapshot embedded is the Track's current one at promotion (``waveform_cache.promote``), so a
marker edit during the refresh is never overwritten or contradicted. Failures keep the previous
cache.

Bounded execution: a Track up to ``SYNC_MAX_SECONDS`` long is refreshed within the request
(about 3 s per 4-minute song on the development host). A longer one (long-form programmes) runs
in one detached, niced ``manage.py refresh_waveform`` process: at most one such process station-
wide (a slot lock handed to the child), and at most one refresh per Track (an operation lock, also
handed over). No queue, no bulk refresh, nothing scheduled.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from library.services import audio_identity, cue_markers, waveform_cache

SYNC_MAX_SECONDS = 900
BACKGROUND_SLOT_LOCK = "background-refresh.lock"

# Refresh outcomes
REPLACED, NOT_FOUND, NOT_ANALYZED, NONCANONICAL, BUSY, SOURCE_MISSING, DECODE_FAILED, UNIDENTIFIABLE, \
    SOURCE_CHANGED, SUPERSEDED, FAILED, STARTED = (
        "replaced", "not_found", "not_analyzed", "noncanonical_cache_path", "busy", "source_missing",
        "decode_failed", "audio_unidentifiable", "source_changed", "superseded", "failed", "started")
CONFLICTS = {BUSY, SOURCE_CHANGED, SUPERSEDED}


def analysis_settings(cfg_values) -> dict:
    sample_rate, window_seconds, target_points = cfg_values[:3]
    return {"sample_rate": int(sample_rate), "window_seconds": float(window_seconds),
            "waveform_points": int(target_points)}


def measure(path, settings) -> tuple[list, list, list, list]:
    """``(envelope_times, envelope_db, samples_left, samples_right)`` of the file (empty lists when it
    does not decode), computed by the analyzer's primitives."""
    from library.management.commands import analyze_tracks as at
    rate, window, points = settings["sample_rate"], settings["window_seconds"], settings["waveform_points"]
    raw = at.decode_audio_to_pcm(path, rate)
    times, envelope_db = at.compute_envelope(raw, rate, window) if raw else ([], [])
    raw_stereo = at.decode_audio_to_pcm_stereo(path, rate) if envelope_db else b""
    left, right = at.compute_stereo_waveform(raw_stereo, rate, window, points) if raw_stereo else ([], [])
    return times, envelope_db, left, right


@dataclass
class RefreshResult:
    track_id: int
    status: str
    detail: str = ""
    replaced: bool = False
    previous_cache_retained: bool = True
    old: dict = field(default_factory=dict)       # {"status", "audio_revision", "started_ns"}
    requested_ns: int | None = None               # a background refresh's request token (status polling)
    new: dict = field(default_factory=dict)       # {"status", "audio_revision", "started_ns", "trusted"}

    @property
    def ok(self) -> bool:
        return self.status in (REPLACED, STARTED)

    @property
    def conflict(self) -> bool:
        return self.status in CONFLICTS

    def to_dict(self) -> dict:
        return {**asdict(self), "ok": self.ok, "conflict": self.conflict}


def _describe(verdict) -> dict:
    payload = verdict.payload or {}
    return {"status": verdict.status,
            "audio_revision": audio_identity.revision(payload.get("audio_identity")),
            "started_ns": (payload.get("generation") or {}).get("started_ns"), "trusted": verdict.trusted}


def _track(track_id):
    from library.models import Track
    return Track.objects.filter(pk=track_id).only(
        "id", "filepath", "waveform_path", "duration_seconds", "cue_marker_revision", *cue_markers.MARKERS).first()


def _preflight(track_id, wave_dir) -> tuple[object, RefreshResult | None]:
    track = _track(track_id)
    if track is None:
        return None, RefreshResult(track_id, NOT_FOUND, "no such Track")
    if not track.waveform_path:
        return track, RefreshResult(track_id, NOT_ANALYZED, "the Track has never been analyzed; run analysis")
    if Path(track.waveform_path) != waveform_cache.canonical_path(wave_dir, track_id):
        return track, RefreshResult(track_id, NONCANONICAL,
                                    f"the Track's waveform_path {track.waveform_path} is not its canonical cache")
    return track, None


def _settings():
    from library.models import AnalysisConfig
    cfg = AnalysisConfig.load()
    return analysis_settings((cfg.analysis_sample_rate, cfg.analysis_window_seconds, cfg.waveform_points))


def refresh(track_id, *, wave_dir=None, operation_lock_fd=None) -> RefreshResult:
    """Regenerate one Track's waveform cache from its current file (see the module docstring)."""
    from library.management.commands.analyze_tracks import get_waveforms_dir
    wave_dir = Path(wave_dir) if wave_dir is not None else get_waveforms_dir()
    track, refused = _preflight(track_id, wave_dir)
    if refused:
        if operation_lock_fd is not None:
            os.close(operation_lock_fd)
        return refused
    try:
        fd = operation_lock_fd if operation_lock_fd is not None else waveform_cache.try_operation_lock(
            wave_dir, f"{int(track_id)}.refresh.lock")
    except waveform_cache.WaveformBusy:
        return RefreshResult(track_id, BUSY, "a refresh of this Track is already running")
    except OSError as exc:
        return RefreshResult(track_id, FAILED, f"the waveform lock cannot be opened: {exc.strerror or exc}")
    try:
        return _refresh_locked(track, wave_dir)
    finally:
        os.close(fd)


def _refresh_locked(track, wave_dir) -> RefreshResult:
    result = RefreshResult(track.pk, FAILED, old=_describe(waveform_cache.validate(track)))
    started_ns = time.time_ns()
    before = audio_identity.stat_source(track.filepath)
    if before is None:
        result.status, result.detail = SOURCE_MISSING, "the Track's audio file is missing or not a regular file"
        return result
    settings = _settings()
    times, envelope_db, left, right = measure(track.filepath, settings)
    if not envelope_db or not left:
        result.status, result.detail = DECODE_FAILED, "the audio did not decode into a waveform"
        return result
    identity, why = audio_identity.compute_or_none(track.filepath)
    if identity is None:
        result.status, result.detail = UNIDENTIFIABLE, f"the decoded audio cannot be identified: {why}"
        return result
    after = audio_identity.stat_source(track.filepath)
    if not audio_identity.same_witness(before, after):
        result.status, result.detail = SOURCE_CHANGED, "the audio file changed while it was decoded"
        return result
    payload = waveform_cache.build_payload(
        track.pk, identity=identity, source=after, analysis=settings, writer="refresh", started_ns=started_ns,
        analysis_duration=times[-1], times=times, envelope_db=envelope_db, samples_left=left, samples_right=right)
    try:
        waveform_cache.promote(track.pk, payload, wave_dir, expected_source=after)
    except waveform_cache.PromotionRefused as exc:
        result.status = {"source_changed": SOURCE_CHANGED, "superseded": SUPERSEDED,
                         "track_missing": NOT_FOUND}.get(exc.code, FAILED)
        result.detail = str(exc)
        return result
    except waveform_cache.WaveformBusy as exc:
        result.status, result.detail = BUSY, str(exc)
        return result
    except OSError as exc:
        result.status, result.detail = FAILED, f"the cache could not be written: {exc.strerror or exc}"
        return result
    from library.models import Track
    current = Track.objects.get(pk=track.pk)
    result.status, result.replaced, result.previous_cache_retained = REPLACED, True, False
    result.new = _describe(waveform_cache.validate(current))
    result.detail = f"waveform regenerated ({result.new['status']})"
    return result


def start_background(track_id, *, wave_dir=None) -> RefreshResult:
    """Refresh a long Track in one detached, niced process, holding the station-wide slot and this
    Track's operation lock for it. BUSY when either is taken."""
    from django.conf import settings as django_settings

    from library.management.commands.analyze_tracks import get_waveforms_dir
    wave_dir = Path(wave_dir) if wave_dir is not None else get_waveforms_dir()
    track, refused = _preflight(track_id, wave_dir)
    if refused:
        return refused
    try:
        slot = waveform_cache.try_operation_lock(wave_dir, BACKGROUND_SLOT_LOCK)
    except waveform_cache.WaveformBusy:
        return RefreshResult(track_id, BUSY, "another long waveform refresh is running; try again when it ends")
    except OSError as exc:
        return RefreshResult(track_id, FAILED, f"the waveform lock cannot be opened: {exc.strerror or exc}")
    try:
        try:
            operation = waveform_cache.try_operation_lock(wave_dir, f"{int(track_id)}.refresh.lock")
        except waveform_cache.WaveformBusy:
            return RefreshResult(track_id, BUSY, "a refresh of this Track is already running")
        except OSError as exc:
            return RefreshResult(track_id, FAILED, f"the waveform lock cannot be opened: {exc.strerror or exc}")
        requested_ns = time.time_ns()
        try:
            subprocess.Popen(
                [sys.executable, str(Path(django_settings.BASE_DIR) / "manage.py"), "refresh_waveform",
                 str(int(track_id)), "--background", "--operation-lock-fd", str(operation),
                 "--slot-lock-fd", str(slot), "--requested-ns", str(requested_ns)],
                pass_fds=(operation, slot), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        except OSError as exc:
            return RefreshResult(track_id, FAILED, f"the background refresh could not start: {exc.strerror or exc}")
        finally:
            os.close(operation)
    finally:
        os.close(slot)
    return RefreshResult(track_id, STARTED, "the waveform is being regenerated in the background",
                         old=_describe(waveform_cache.validate(track)), requested_ns=requested_ns)


def outcome_key(track_id, requested_ns) -> str:
    """The SystemEvent dedupe key a background refresh records its outcome under."""
    return f"library|waveform-refresh|{int(track_id)}|{int(requested_ns)}"


# Background status (Package C): what a browser polling a 202 "started" may be told.
RUNNING, COMPLETED, FAILED_STATE, UNKNOWN = "running", "completed", "failed", "unknown"


def background_status(track_id, requested_ns, *, wave_dir=None) -> dict:
    """The state of the background refresh started with ``requested_ns`` -- never inferred from the
    launch alone. ``completed``/``failed`` come from the outcome the process recorded (or, if that
    record is missing, a cache this very refresh demonstrably promoted); ``running`` while the
    process holds the Track's refresh lock; otherwise ``unknown`` (it ended without a result --
    e.g. killed by a restart), and the previous cache is still in place."""
    from library.management.commands.analyze_tracks import get_waveforms_dir
    from monitoring.models import SystemEvent
    wave_dir = Path(wave_dir) if wave_dir is not None else get_waveforms_dir()
    event = (SystemEvent.objects.filter(dedupe_key=outcome_key(track_id, requested_ns))
             .order_by("-created_at").values("detail").first())
    if event is not None:
        detail = event["detail"] or {}
        status = detail.get("status")
        return {"state": COMPLETED if status == REPLACED else FAILED_STATE, "status": status,
                "detail": detail.get("detail", ""), "new": detail.get("new") or {}}
    try:
        os.close(waveform_cache.try_operation_lock(wave_dir, f"{int(track_id)}.refresh.lock"))
    except waveform_cache.WaveformBusy:
        return {"state": RUNNING, "status": STARTED, "detail": "the waveform is being regenerated", "new": {}}
    except OSError as exc:
        return {"state": UNKNOWN, "status": None, "detail": f"the refresh lock cannot be read: {exc}", "new": {}}
    track = _track(track_id)
    if track is not None:
        verdict = waveform_cache.validate(track)
        generation = (verdict.payload or {}).get("generation") or {}
        if generation.get("writer") == "refresh" and (generation.get("started_ns") or 0) >= int(requested_ns):
            return {"state": COMPLETED, "status": REPLACED, "detail": "waveform regenerated",
                    "new": _describe(verdict)}
    return {"state": UNKNOWN, "status": None, "new": {},
            "detail": "the background refresh ended without recording a result; the previous waveform is kept"}
