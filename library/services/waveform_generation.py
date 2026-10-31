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

Resource limits (r0123 B3, waveform-only refresh; ordinary analysis is unchanged), enforced while
decoding, not just before:

* duration: up to ``MAX_REFRESH_SECONDS`` (6 hours -- above any syndicated or specialty
  programme the stations carry); a longer Track is refused (``too_long``) without decoding;
* memory: each decode is read through a byte cap -- the PCM the Track's recorded duration can
  produce (+5 % and 60 s of slack) at the configured analysis rate, and never more than
  ``PCM_BUDGET_BYTES`` (512 MiB, the largest single buffer). The mono buffer is released before the
  stereo decode, so peak PCM is one stereo buffer: about 0.06 GiB per hour at the default 4410 Hz
  (6 hours: about 0.36 GiB). A Track whose duration would need more than the budget at the current
  rate is refused before decoding, and media that decodes to more than its recorded duration (or
  without one, more than the 6-hour bound) is stopped at the cap (``resource_limit``);
* time: each ffmpeg decode is killed after ``decode_timeout`` (120 s + 0.1 s per second of audio:
  4 minutes -> 144 s, 6 hours -> 38 minutes), as is the identity decode (``resource_limit``).

Any limit failure kills and reaps the decoder, writes nothing, keeps the previous cache and every
Track column, and releases the locks (they are held by ``refresh``'s own ``finally``). The
remaining exposure is the Python-side envelope computation over an already-capped buffer.
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
TOO_LONG, RESOURCE_LIMIT = "too_long", "resource_limit"
CONFLICTS = {BUSY, SOURCE_CHANGED, SUPERSEDED}


def analysis_settings(cfg_values) -> dict:
    sample_rate, window_seconds, target_points = cfg_values[:3]
    return {"sample_rate": int(sample_rate), "window_seconds": float(window_seconds),
            "waveform_points": int(target_points)}


MAX_REFRESH_SECONDS = 6 * 3600
PCM_BUDGET_BYTES = 512 * 1024 * 1024
DURATION_SLACK_RATIO, DURATION_SLACK_SECONDS = 1.05, 60.0
_READ_CHUNK = 1 << 20


class ResourceLimit(Exception):
    """A waveform-refresh decode exceeded its time or memory bound (the message says which)."""


@dataclass
class Limits:
    max_mono_bytes: int
    max_stereo_bytes: int
    timeout: float


def decode_timeout(seconds) -> float:
    return 120.0 + 0.1 * max(0.0, float(seconds or 0.0))


def refresh_limits(declared_seconds, settings) -> tuple[Limits | None, str, str]:
    """``(limits, "", "")``, or ``(None, status, reason)`` when the Track cannot be refreshed within them."""
    rate = int(settings["sample_rate"])
    declared = float(declared_seconds) if declared_seconds and float(declared_seconds) > 0 else None
    if declared is not None and declared > MAX_REFRESH_SECONDS:
        return None, TOO_LONG, (f"the Track is {declared / 3600:.1f} h long; Refresh Waveform Only supports up to "
                                f"{MAX_REFRESH_SECONDS // 3600} h")
    bound = (declared * DURATION_SLACK_RATIO + DURATION_SLACK_SECONDS) if declared else MAX_REFRESH_SECONDS
    bound = min(bound, MAX_REFRESH_SECONDS * DURATION_SLACK_RATIO + DURATION_SLACK_SECONDS)
    stereo = int(bound * rate * 2 * 2)
    if declared is not None and int(declared * rate * 2 * 2) > PCM_BUDGET_BYTES:
        return None, RESOURCE_LIMIT, (f"decoding {declared / 3600:.1f} h at {rate} Hz needs more than the "
                                      f"{PCM_BUDGET_BYTES // (1024 * 1024)} MiB waveform-refresh memory budget")
    return Limits(max_mono_bytes=min(stereo // 2, PCM_BUDGET_BYTES), max_stereo_bytes=min(stereo, PCM_BUDGET_BYTES),
                  timeout=decode_timeout(bound)), "", ""


def decode_bounded(cmd, max_bytes, timeout) -> bytes:
    """Run a PCM decoder, reading at most ``max_bytes`` within ``timeout`` seconds; on either limit
    the process is killed and reaped and ResourceLimit raised. A decoder failure returns b"" (as the
    analyzer's own decoders do)."""
    import threading
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return b""
    timed_out = threading.Event()
    watchdog = threading.Timer(timeout, lambda: (timed_out.set(), proc.kill()))
    watchdog.start()
    data = bytearray()
    exceeded = False
    try:
        for chunk in iter(lambda: proc.stdout.read(_READ_CHUNK), b""):
            if len(data) + len(chunk) > max_bytes:
                exceeded = True
                proc.kill()
                break
            data.extend(chunk)
        proc.wait()
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
    if timed_out.is_set():
        raise ResourceLimit(f"decoding took longer than {timeout:.0f} s")
    if exceeded:
        raise ResourceLimit(f"the audio decodes to more than {max_bytes // (1024 * 1024)} MiB "
                            "(longer than its recorded duration, or beyond the memory budget)")
    return bytes(data) if proc.returncode == 0 else b""


def measure(path, settings, limits=None) -> tuple[list, list, list, list]:
    """``(envelope_times, envelope_db, samples_left, samples_right)`` of the file (empty lists when it
    does not decode), computed by the analyzer's primitives. With ``limits`` the decodes are bounded
    (``decode_bounded``; ResourceLimit) and produce the same PCM as the analyzer's decoders."""
    from library.management.commands import analyze_tracks as at
    rate, window, points = settings["sample_rate"], settings["window_seconds"], settings["waveform_points"]
    if limits is None:
        raw = at.decode_audio_to_pcm(path, rate)
    else:
        raw = decode_bounded(at.pcm_command(path, rate, 1), limits.max_mono_bytes, limits.timeout)
    times, envelope_db = at.compute_envelope(raw, rate, window) if raw else ([], [])
    del raw                                   # peak memory: one buffer at a time
    if not envelope_db:
        raw_stereo = b""
    elif limits is None:
        raw_stereo = at.decode_audio_to_pcm_stereo(path, rate)
    else:
        raw_stereo = decode_bounded(at.pcm_command(path, rate, 2), limits.max_stereo_bytes, limits.timeout)
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


def refresh(track_id, *, wave_dir=None, operation_lock_fd=None, request_token=None) -> RefreshResult:
    """Regenerate one Track's waveform cache from its current file (see the module docstring).
    ``request_token``: a background request's exact token, recorded in the promoted cache's
    generation metadata so the status service can attribute that cache to this request only."""
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
        return _refresh_locked(track, wave_dir, request_token)
    finally:
        os.close(fd)


def _refresh_locked(track, wave_dir, request_token=None) -> RefreshResult:
    result = RefreshResult(track.pk, FAILED, old=_describe(waveform_cache.validate(track)))
    started_ns = time.time_ns()
    before = audio_identity.stat_source(track.filepath)
    if before is None:
        result.status, result.detail = SOURCE_MISSING, "the Track's audio file is missing or not a regular file"
        return result
    settings = _settings()
    limits, refused, why = refresh_limits(track.duration_seconds, settings)
    if limits is None:
        result.status, result.detail = refused, why
        return result
    try:
        times, envelope_db, left, right = measure(track.filepath, settings, limits)
    except ResourceLimit as exc:
        result.status, result.detail = RESOURCE_LIMIT, f"waveform not rebuilt: {exc}; the previous waveform is kept"
        return result
    if not envelope_db or not left:
        result.status, result.detail = DECODE_FAILED, "the audio did not decode into a waveform"
        return result
    identity, why = audio_identity.compute_or_none(track.filepath, timeout=limits.timeout)
    if identity is None and why.startswith("decoding took longer"):
        result.status, result.detail = RESOURCE_LIMIT, f"waveform not rebuilt: {why}; the previous waveform is kept"
        return result
    if identity is None:
        result.status, result.detail = UNIDENTIFIABLE, f"the decoded audio cannot be identified: {why}"
        return result
    after = audio_identity.stat_source(track.filepath)
    if not audio_identity.same_witness(before, after):
        result.status, result.detail = SOURCE_CHANGED, "the audio file changed while it was decoded"
        return result
    payload = waveform_cache.build_payload(
        track.pk, identity=identity, source=after, analysis=settings, writer="refresh", started_ns=started_ns,
        analysis_duration=times[-1], times=times, envelope_db=envelope_db, samples_left=left, samples_right=right,
        request=request_token)
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
    limits, status, why = refresh_limits(track.duration_seconds, _settings())
    if limits is None:
        return RefreshResult(track_id, status, why)
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
            # This request's own lock: held by the child until it has recorded its outcome, so
            # "running" is a fact about this request, not about whichever refresh holds the Track.
            request_lock = waveform_cache.create_request_lock(request_lock_name(track_id, requested_ns))
        except OSError as exc:
            os.close(operation)
            return RefreshResult(track_id, FAILED, f"the waveform lock cannot be opened: {exc.strerror or exc}")
        try:
            subprocess.Popen(
                [sys.executable, str(Path(django_settings.BASE_DIR) / "manage.py"), "refresh_waveform",
                 str(int(track_id)), "--background", "--operation-lock-fd", str(operation),
                 "--slot-lock-fd", str(slot), "--requested-ns", str(requested_ns),
                 "--request-lock-fd", str(request_lock)],
                pass_fds=(operation, slot, request_lock), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        except OSError as exc:
            (waveform_cache.lock_dir() / request_lock_name(track_id, requested_ns)).unlink(missing_ok=True)
            return RefreshResult(track_id, FAILED, f"the background refresh could not start: {exc.strerror or exc}")
        finally:
            os.close(operation)
            os.close(request_lock)
    finally:
        os.close(slot)
    return RefreshResult(track_id, STARTED, "the waveform is being regenerated in the background",
                         old=_describe(waveform_cache.validate(track)), requested_ns=requested_ns)


def outcome_key(track_id, requested_ns) -> str:
    """The SystemEvent dedupe key a background refresh records its outcome under."""
    return f"library|waveform-refresh|{int(track_id)}|{int(requested_ns)}"


def request_lock_name(track_id, requested_ns) -> str:
    return f"{int(track_id)}.refresh.{int(requested_ns)}.lock"


#: How far in the future a request token may claim to be (clock skew allowance).
TOKEN_FUTURE_SLACK_NS = 300 * 10 ** 9


def valid_request_token(value) -> int | None:
    """A background refresh request token as issued by ``start_background`` (a positive integer
    nanosecond timestamp, not from the future), or None for anything else."""
    if isinstance(value, bool):
        return None
    try:
        token = int(value)
    except (TypeError, ValueError):
        return None
    if str(value) != str(token) or token <= 0 or token > time.time_ns() + TOKEN_FUTURE_SLACK_NS:
        return None
    return token


# Background status (Package C): what a browser polling a 202 "started" may be told.
RUNNING, COMPLETED, FAILED_STATE, UNKNOWN = "running", "completed", "failed", "unknown"


def background_status(track_id, requested_ns, *, wave_dir=None) -> dict:
    """The state of exactly the background refresh started with ``requested_ns`` (r0123 B2) -- never
    inferred from the launch, from another request, or from timing:

    * ``completed``/``failed``: the outcome this request's process recorded (its own SystemEvent
      key), or -- if the process died after promoting but before recording -- a cache whose
      generation metadata carries exactly this request's token;
    * ``running``: this request's own lock is still held by a live process;
    * ``unknown``: none of that -- the process ended without a result (or never ran), and the
      previous cache is still in place. A later refresh can never complete an earlier request."""
    from monitoring.models import SystemEvent
    token = valid_request_token(requested_ns)
    if token is None:
        return {"state": UNKNOWN, "status": None, "new": {}, "detail": "not a refresh request token"}
    event = (SystemEvent.objects.filter(dedupe_key=outcome_key(track_id, token))
             .order_by("-created_at").values("detail").first())
    if event is not None:
        detail = event["detail"] or {}
        status = detail.get("status")
        return {"state": COMPLETED if status == REPLACED else FAILED_STATE, "status": status,
                "detail": detail.get("detail", ""), "new": detail.get("new") or {}}
    try:
        if waveform_cache.request_lock_held(request_lock_name(track_id, token)):
            return {"state": RUNNING, "status": STARTED, "detail": "the waveform is being regenerated", "new": {}}
    except OSError as exc:
        return {"state": UNKNOWN, "status": None, "detail": f"the refresh lock cannot be read: {exc}", "new": {}}
    track = _track(track_id)
    if track is not None:
        verdict = waveform_cache.validate(track)
        generation = (verdict.payload or {}).get("generation") or {}
        if generation.get("writer") == "refresh" and generation.get("request") == token:
            return {"state": COMPLETED, "status": REPLACED, "detail": "waveform regenerated",
                    "new": _describe(verdict)}
    return {"state": UNKNOWN, "status": None, "new": {},
            "detail": "the background refresh ended without recording a result; the previous waveform is kept"}
