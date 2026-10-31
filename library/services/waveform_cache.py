"""Trusted waveform caches: format, validation and the only writer (roadmap 1.21, r0123).

A waveform cache (``<WAVEFORMS_DIR>/<track id>.json``, recorded as ``Track.waveform_path``) may be
*displayed* when it is readable; it may feed a *marker calculation* (Repick) only when it is
**trusted**: the version-2 format below, computed from audio whose decoded identity
(library.services.audio_identity) was established for the very file the Track names now.

Version 2::

    {"schema": "isadoraair.waveform", "schema_version": 2, "track_id": <int>,
     "audio_identity": {algorithm, sha256, sample_rate, channels, sample_format, frames, decoder} | null,
     "source": {path, dev, ino, size, mtime_ns, ctime_ns},     # the file the identity was taken from
     "analysis": {"sample_rate", "window_seconds", "waveform_points"},
     "generation": {"writer", "started_ns"},                  # decode start; a later decode wins
     "analysis_duration": <float > 0>,
     "envelope_times": [...], "envelope_db": [...],         # same length >= 1, finite, ascending
     "samples_left": [...], "samples_right": [...],         # same length >= 1, integers 0..255
     "marker_revision": <int>, "markers": {<the eight markers>},   # the Track's markers at promotion
     "cue_in_seconds": ..., "next_start": ...}              # legacy reader fields (same snapshot)

Trust re-verification is bounded: the cache's identity is current while the Track's file still
has the recorded source witness (path, device, inode, size, mtime and the kernel-maintained ctime,
which no ordinary write, rename or metadata change leaves untouched). Any difference -- a tag
edit, a replacement, a move -- makes the cache *unverified* until a decode (Refresh Waveform Only
or analysis) re-establishes the identity; no request ever trusts a cache on path or time alone,
and none decodes audio merely to check trust.

Waveform trust and marker freshness are separate: a trusted cache whose marker snapshot predates
the Track's current marker revision is ``trusted_stale_markers`` -- its samples and envelope
remain good, and readers take the markers from the database. A snapshot that claims a newer
revision than the database, or different values at the same revision, is ``inconsistent``.

Every replacement of a cache file goes through this module (``promote``, ``install_file``,
``restore_file``, ``remove``): under a per-Track lock (``flock`` on
``<WAVEFORMS_DIR>/.locks/<id>.lock`` -- one location per station whatever directory is written,
so a publisher's scratch analysis and the real cache serialize too; bounded wait), staged in
the destination directory, validated,
renamed atomically over the old file and made durable (file and directory ``fsync``). A failure
at any point leaves the previous cache in place. Lock order: a holder of a cache lock never waits
for a database row lock (it only reads), so a writer may take the cache lock while it holds a
Track row lock and nothing can deadlock; nobody decodes audio while holding the cache lock.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from library.services import audio_identity

SCHEMA = "isadoraair.waveform"
SCHEMA_VERSION = 2
MAX_BYTES = 64 * 1024 * 1024
#: The cache's analysed duration may differ from the stored duration by this much (absolute or relative).
DURATION_TOLERANCE_SECONDS = 1.0
DURATION_TOLERANCE_RATIO = 0.02
ENVELOPE_DB_FLOOR, ENVELOPE_DB_CEILING = -120.0, 0.5
LOCK_DIRNAME = ".locks"
LOCK_TIMEOUT_SECONDS = 15.0
PREVIOUS_SUFFIX = ".prev"

# Verdicts
MISSING, UNREADABLE, MALFORMED, WRONG_TRACK, INCONSISTENT = "missing", "unreadable", "malformed", "wrong_track", \
    "inconsistent"
UNSUPPORTED, LEGACY, UNVERIFIED_AUDIO, INCOMPLETE = "unsupported", "legacy", "unverified_audio", "incomplete"
TRUSTED, TRUSTED_STALE_MARKERS = "trusted", "trusted_stale_markers"


@dataclass
class Verdict:
    status: str
    reason: str
    payload: dict | None = field(default=None, repr=False)

    @property
    def trusted(self) -> bool:
        """Derived from the Track's current audio: may feed a marker calculation."""
        return self.status in (TRUSTED, TRUSTED_STALE_MARKERS)

    @property
    def markers_current(self) -> bool:
        return self.status == TRUSTED

    @property
    def displayable(self) -> bool:
        return self.status in (TRUSTED, TRUSTED_STALE_MARKERS, UNVERIFIED_AUDIO, LEGACY)


class WaveformBusy(Exception):
    """Another writer holds this Track's cache lock (or operation lock)."""


class PromotionRefused(Exception):
    """A staged cache was not promoted; the previous cache is untouched. ``code`` says why."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ reading and validation

def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _int(value, minimum=0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _structure_problem(payload: dict, *, versioned: bool) -> str | None:
    duration = payload.get("analysis_duration")
    if not _number(duration) or duration <= 0:
        return "analysis_duration is not a positive finite number"
    times, levels = payload.get("envelope_times"), payload.get("envelope_db")
    if not isinstance(times, list) or not isinstance(levels, list):
        return "envelope_times/envelope_db missing"
    if not times or len(times) != len(levels):
        return "envelope_times and envelope_db are empty or of different lengths"
    if not all(_number(t) for t in times) or not all(_number(v) for v in levels):
        return "envelope contains non-numeric or non-finite values"
    if times[0] < 0 or any(b <= a for a, b in zip(times, times[1:])):
        return "envelope_times are negative or not strictly increasing"
    window = (payload.get("analysis") or {}).get("window_seconds") if versioned else None
    slack = window if _number(window) and window > 0 else max(times[-1] - times[-2], 0.0) if len(times) > 1 else 0.5
    if times[-1] > duration + slack + 1e-6:
        return "envelope extends past analysis_duration"
    if any(v < ENVELOPE_DB_FLOOR - 1e-6 or v > ENVELOPE_DB_CEILING for v in levels):
        return "envelope_db outside the analyzer's range"
    left, right = payload.get("samples_left"), payload.get("samples_right")
    if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
        return "samples_left/samples_right missing or of different lengths"
    if not all(isinstance(s, int) and not isinstance(s, bool) and 0 <= s <= 255 for s in left + right):
        return "stereo samples are not integers 0..255"
    return None


def _v2_metadata_problem(payload: dict) -> str | None:
    analysis = payload.get("analysis")
    if not isinstance(analysis, dict) or not _int(analysis.get("sample_rate"), 1) \
            or not _number(analysis.get("window_seconds")) or analysis["window_seconds"] <= 0 \
            or not _int(analysis.get("waveform_points"), 1):
        return "invalid analysis metadata"
    generation = payload.get("generation")
    if not isinstance(generation, dict) or not _int(generation.get("started_ns"), 1) \
            or not isinstance(generation.get("writer"), str):
        return "invalid generation metadata"
    source = payload.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("path"), str) or not all(
            _int(source.get(name)) for name in audio_identity.WITNESS_FIELDS if name != "path"):
        return "invalid source metadata"
    identity = payload.get("audio_identity")
    if identity is not None and not audio_identity.well_formed(identity):
        return "malformed audio identity"
    from library.services import cue_markers
    revision, markers = payload.get("marker_revision"), payload.get("markers")
    if not _int(revision) or not isinstance(markers, dict) or set(markers) != set(cue_markers.MARKERS) \
            or not all(value is None or _number(value) for value in markers.values()):
        return "incomplete marker snapshot"
    return None


def _duration_mismatch(track_duration, analysis_duration) -> bool:
    if not _number(track_duration) or track_duration <= 0:
        return False
    tolerance = max(DURATION_TOLERANCE_SECONDS, DURATION_TOLERANCE_RATIO * track_duration)
    return abs(analysis_duration - track_duration) > tolerance


def _reject_constant(name):
    raise ValueError(f"non-finite JSON constant {name}")


def read(path) -> tuple[dict | None, Verdict | None]:
    """Parse a cache file defensively: (payload, None) or (None, failing verdict)."""
    if not path:
        return None, Verdict(MISSING, "no waveform cache recorded for this Track")
    path = Path(path)
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return None, Verdict(MISSING, f"waveform cache {path.name} does not exist")
    except OSError as exc:
        return None, Verdict(UNREADABLE, f"waveform cache {path.name} cannot be read: {exc.strerror}")
    if not os.path.isfile(path):
        return None, Verdict(UNREADABLE, f"waveform cache {path.name} is not a regular file")
    if info.st_size > MAX_BYTES:
        return None, Verdict(UNREADABLE, f"waveform cache {path.name} is implausibly large")
    try:
        payload = json.loads(path.read_bytes().decode("utf-8"), parse_constant=_reject_constant)
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        return None, Verdict(MALFORMED, f"waveform cache {path.name} is not valid JSON ({type(exc).__name__})")
    if not isinstance(payload, dict):
        return None, Verdict(MALFORMED, f"waveform cache {path.name} is not a JSON object")
    return payload, None


def identity_consistent(identity, analysis_duration, window_seconds) -> bool:
    """The identified audio lasts as long as the envelope says (within two analysis windows)."""
    return abs(identity["frames"] / identity["sample_rate"] - analysis_duration) <= 2 * window_seconds + 0.05


def check_payload(payload: dict, track_id: int) -> Verdict | None:
    """Track-independent checks of a parsed payload: a failing verdict, or None (well formed)."""
    found = payload.get("track_id")
    if not _int(found) or found != track_id:
        return Verdict(WRONG_TRACK, f"the cache names Track {found!r}, not {track_id}")
    if "schema" in payload or "schema_version" in payload:
        if payload.get("schema") != SCHEMA or payload.get("schema_version") != SCHEMA_VERSION:
            return Verdict(UNSUPPORTED, f"unsupported cache schema {payload.get('schema')!r} "
                                        f"version {payload.get('schema_version')!r}")
        versioned = True
    else:
        versioned = False
    problem = _structure_problem(payload, versioned=versioned) or (_v2_metadata_problem(payload) if versioned else None)
    if problem:
        return Verdict(MALFORMED, problem)
    if versioned:
        identity, window = payload["audio_identity"], payload["analysis"]["window_seconds"]
        if identity is not None and not identity_consistent(identity, payload["analysis_duration"], window):
            return Verdict(INCONSISTENT, f"the identified audio lasts "
                                         f"{identity['frames'] / identity['sample_rate']:.3f}s but the envelope "
                                         f"{payload['analysis_duration']:.3f}s")
    return None


def validate(track, path=None) -> Verdict:
    """The cache's verdict for ``track`` (default path: ``track.waveform_path``). Reads the cache and
    stats the Track's file; never decodes audio."""
    from library.services import cue_markers
    payload, failure = read(path if path is not None else track.waveform_path)
    if failure:
        return failure
    failure = check_payload(payload, track.pk)
    if failure:
        return failure
    if _duration_mismatch(track.duration_seconds, payload["analysis_duration"]):
        return Verdict(INCONSISTENT, f"the cache's analysed duration {payload['analysis_duration']:.3f}s does not "
                                     f"match the Track's {track.duration_seconds:.3f}s")
    if "schema" not in payload:
        return Verdict(LEGACY, "legacy waveform cache (no version-2 identity): display only", payload)
    if not payload["samples_left"]:
        return Verdict(INCOMPLETE, "the cache has no stereo waveform samples (the stereo decode failed)", payload)
    revision, markers = payload["marker_revision"], payload["markers"]
    if revision > track.cue_marker_revision or (
            revision == track.cue_marker_revision
            and any(not cue_markers._same(markers[name], getattr(track, name)) for name in cue_markers.MARKERS)):
        return Verdict(INCONSISTENT, f"the cache's marker snapshot (revision {revision}) contradicts the "
                                     f"Track's markers (revision {track.cue_marker_revision})")
    if payload["audio_identity"] is None:
        return Verdict(UNVERIFIED_AUDIO, "the audio could not be identified when this cache was made", payload)
    if payload["audio_identity"]["algorithm"] != audio_identity.ALGORITHM:
        return Verdict(UNVERIFIED_AUDIO, "the cache's audio identity uses an unsupported algorithm", payload)
    source = payload["source"]
    if source["path"] != str(track.filepath):
        return Verdict(UNVERIFIED_AUDIO, "the cache was made from a different file path", payload)
    if not audio_identity.same_witness(source, audio_identity.stat_source(track.filepath)):
        return Verdict(UNVERIFIED_AUDIO, "the Track's file changed since the cache was made (refresh the waveform)",
                       payload)
    if revision < track.cue_marker_revision:
        return Verdict(TRUSTED_STALE_MARKERS, f"trusted waveform; its marker snapshot (revision {revision}) "
                                              f"predates the Track's (revision {track.cue_marker_revision})", payload)
    return Verdict(TRUSTED, "trusted", payload)


def current_audio_revision(track) -> str | None:
    """The decoded-audio revision of the Track's file, when a trusted cache establishes it (no decode)."""
    verdict = validate(track)
    return audio_identity.revision(verdict.payload["audio_identity"]) if verdict.trusted else None


# ------------------------------------------------------------------ building

def build_payload(track_id, *, identity, source, analysis, writer, started_ns, analysis_duration, times,
                  envelope_db, samples_left, samples_right) -> dict:
    """A version-2 payload; the marker snapshot is added at promotion (``promote``)."""
    return {"schema": SCHEMA, "schema_version": SCHEMA_VERSION, "track_id": int(track_id),
            "audio_identity": identity, "source": dict(source), "analysis": dict(analysis),
            "generation": {"writer": writer, "started_ns": int(started_ns)},
            "analysis_duration": analysis_duration, "envelope_times": times, "envelope_db": envelope_db,
            "samples_left": samples_left, "samples_right": samples_right}


# ------------------------------------------------------------------ locking and promotion

def canonical_path(wave_dir, track_id) -> Path:
    return Path(wave_dir) / f"{int(track_id)}.json"


def lock_dir() -> Path:
    """The station's waveform lock directory: ``<WAVEFORMS_DIR>/.locks``."""
    from django.conf import settings
    path = Path(getattr(settings, "WAVEFORMS_DIR", "/srv/isadoraair/waveforms")) / LOCK_DIRNAME
    os.makedirs(path, 0o755, exist_ok=True)
    return path


def _open_lock(name):
    # Read-only is enough for flock, so a lock file another service user created still opens.
    return os.open(lock_dir() / name, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)


def _acquire_track_lock(track_id, timeout):
    """Acquire and return the per-Track flock fd; the caller owns and closes it."""
    fd = _open_lock(f"{int(track_id)}.lock")
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise WaveformBusy(f"the waveform cache of Track {track_id} is locked by another writer") \
                        from None
                time.sleep(0.05)
    except BaseException:
        os.close(fd)
        raise


@contextlib.contextmanager
def track_lock(wave_dir, track_id, *, timeout=LOCK_TIMEOUT_SECONDS):
    """The per-Track cache lock (cross-process ``flock``); WaveformBusy after ``timeout`` seconds.
    ``wave_dir`` names the directory being written; the lock itself is per Track, station-wide."""
    fd = _acquire_track_lock(track_id, timeout)
    try:
        yield
    finally:
        os.close(fd)


def try_operation_lock(wave_dir, name) -> int:
    """A non-blocking exclusive operation lock (e.g. one refresh per Track): its fd (close to release,
    or pass to a child process), or WaveformBusy."""
    fd = _open_lock(name)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise WaveformBusy(f"{name} is held by another operation") from None
    return fd


def _fsync_dir(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _stage(directory, track_id, data: bytes) -> Path:
    staged = Path(directory) / f".{int(track_id)}.json.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o666)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        staged.unlink(missing_ok=True)
        raise
    os.close(fd)
    return staged


@dataclass
class Promotion:
    """A completed replacement. ``previous`` is the same-filesystem copy of the cache it replaced
    (None if there was none), kept only when the caller asked to be able to ``undo``."""
    track_id: int
    path: Path
    previous_existed: bool
    previous: Path | None = None
    lock_fd: int | None = None
    installed_witness: tuple | None = None

    def _verify_owner(self):
        if self.lock_fd is None:
            raise PromotionRefused("finalized", "this waveform promotion has already been finalized")
        try:
            current = _file_witness(self.path)
        except FileNotFoundError:
            current = None
        if current != self.installed_witness:
            raise PromotionRefused(
                "ownership_lost",
                "the installed waveform no longer belongs to this promotion; rollback state was retained",
            )

    def _release(self):
        fd, self.lock_fd = self.lock_fd, None
        if fd is not None:
            os.close(fd)

    def undo(self):
        """Put back exactly what was there before while retaining this promotion's lock lease."""
        try:
            self._verify_owner()
            if self.previous is not None and self.previous.exists():
                os.replace(self.previous, self.path)
            elif not self.previous_existed:
                self.path.unlink(missing_ok=True)
            else:
                raise PromotionRefused("rollback_missing", "this promotion's rollback cache is missing")
            _fsync_dir(self.path.parent)
            self.previous = None
        finally:
            self._release()

    def discard(self):
        """Keep the new cache and drop only this promotion's uniquely owned rollback copy."""
        try:
            self._verify_owner()
            if self.previous is not None:
                self.previous.unlink(missing_ok=True)
                self.previous = None
                _fsync_dir(self.path.parent)
        finally:
            self._release()


def _file_witness(path: Path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _replace(track_id, final: Path, data: bytes, *, keep_previous: bool, lock_fd=None) -> Promotion:
    staged = _stage(final.parent, track_id, data)
    previous = final.with_name(
        f".{final.name}{PREVIOUS_SUFFIX}.{os.getpid()}.{secrets.token_hex(8)}"
    )
    existed = final.exists()
    replaced = False
    try:
        payload, failure = read(staged)
        if failure is None:
            failure = check_payload(payload, int(track_id))
        if failure is not None:
            raise PromotionRefused("invalid", f"the staged cache failed validation: {failure.reason}")
        if existed:
            os.link(final, previous)             # the same-filesystem rollback copy
        os.replace(staged, final)
        replaced = True
        _fsync_dir(final.parent)
        installed_witness = _file_witness(final)
    except BaseException as exc:
        staged.unlink(missing_ok=True)
        if replaced:
            try:
                if existed and previous.exists():
                    os.replace(previous, final)
                elif not existed:
                    final.unlink(missing_ok=True)
                _fsync_dir(final.parent)
            except BaseException as restore_exc:
                raise OSError(
                    f"waveform replacement failed and its previous cache could not be restored: {restore_exc}"
                ) from exc
        elif existed:
            previous.unlink(missing_ok=True)
        raise
    promotion = Promotion(
        int(track_id), final, existed, previous if existed else None,
        lock_fd=lock_fd if keep_previous else None,
        installed_witness=installed_witness,
    )
    if not keep_previous:
        if existed:
            try:
                previous.unlink(missing_ok=True)
            except OSError:
                # The new canonical cache was already renamed and directory-fsynced. A failure to
                # remove this operation's unique recovery link must not falsely report that the
                # old cache is still canonical; leaving the uniquely named link is safe debris.
                pass
    return promotion


def _newer_existing(final: Path, track_id, started_ns) -> bool:
    payload, failure = read(final)
    if failure or check_payload(payload, int(track_id)) or "schema" not in payload:
        return False
    return payload["generation"]["started_ns"] > started_ns


def _marker_snapshot(track_id) -> dict | None:
    from library.services import cue_markers
    row = cue_markers.snapshot(track_id)
    if row is None:
        return None
    return {"marker_revision": row["cue_marker_revision"], "markers": {name: row[name] for name in cue_markers.MARKERS},
            "cue_in_seconds": row["cue_in_seconds"], "next_start": row["next_start_seconds"]}


def promote(track_id, payload: dict, wave_dir, *, expected_source=None, keep_previous=False,
            lock_timeout=LOCK_TIMEOUT_SECONDS) -> Promotion:
    """Make ``payload`` (``build_payload``) the Track's cache in ``wave_dir``, atomically.

    Under the Track's cache lock: the Track must still exist and, with ``expected_source``, the file
    that was decoded must still be exactly that file (else ``source_changed``; whether it is still
    the Track's file is the validator's question -- a cache of another path is never trusted); a
    cache whose decode started later is
    never replaced by this older one (``superseded``); the Track's *current* markers are embedded
    as the snapshot. PromotionRefused / WaveformBusy / OSError leave the previous cache untouched."""
    from library.models import Track
    final = canonical_path(wave_dir, track_id)
    lock_fd = _acquire_track_lock(track_id, lock_timeout)
    try:
        filepath = Track.objects.filter(pk=track_id).values_list("filepath", flat=True).first()
        snapshot = _marker_snapshot(track_id)
        if filepath is None or snapshot is None:
            raise PromotionRefused("track_missing", f"Track {track_id} no longer exists")
        if expected_source is not None and not audio_identity.same_witness(
                expected_source, audio_identity.stat_source(expected_source.get("path"))):
            raise PromotionRefused("source_changed", "the Track's file changed while the waveform was generated")
        if _newer_existing(final, track_id, payload["generation"]["started_ns"]):
            raise PromotionRefused("superseded", "a newer waveform was promoted meanwhile")
        data = json.dumps({**payload, **snapshot}, ensure_ascii=False, allow_nan=False).encode("utf-8")
        promotion = _replace(
            track_id, final, data, keep_previous=keep_previous,
            lock_fd=lock_fd if keep_previous else None,
        )
        if keep_previous:
            lock_fd = None                 # the Promotion owns the transaction-scoped lease
        return promotion
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


def install_file(track_id, staged_file, wave_dir, *, keep_previous=False,
                 lock_timeout=LOCK_TIMEOUT_SECONDS) -> Promotion:
    """Install a cache another directory holds (a publisher's scratch analysis), validated, atomically."""
    payload, failure = read(staged_file)
    if failure is None:
        failure = check_payload(payload, int(track_id))
    if failure is not None:
        raise PromotionRefused("invalid", f"the cache to install failed validation: {failure.reason}")
    final = canonical_path(wave_dir, track_id)
    lock_fd = _acquire_track_lock(track_id, lock_timeout)
    try:
        if "schema" in payload and _newer_existing(final, track_id, payload["generation"]["started_ns"]):
            raise PromotionRefused("superseded", "a newer waveform was promoted meanwhile")
        promotion = _replace(
            track_id, final, Path(staged_file).read_bytes(), keep_previous=keep_previous,
            lock_fd=lock_fd if keep_previous else None,
        )
        if keep_previous:
            lock_fd = None                 # the Promotion owns the transaction-scoped lease
        return promotion
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


def restore_file(track_id, backup_file, wave_dir, *, lock_timeout=LOCK_TIMEOUT_SECONDS) -> None:
    """Put a publisher's copy of the previous cache back, verbatim and atomically (its rollback)."""
    final = canonical_path(wave_dir, track_id)
    data = Path(backup_file).read_bytes()
    with track_lock(wave_dir, track_id, timeout=lock_timeout):
        staged = _stage(final.parent, track_id, data)
        try:
            os.replace(staged, final)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
        _fsync_dir(final.parent)


def remove(track_id, wave_dir=None, *, path=None, lock_timeout=LOCK_TIMEOUT_SECONDS) -> bool:
    """Remove a Track's cache (default: the canonical one in ``wave_dir``) under its lock."""
    target = Path(path) if path is not None else canonical_path(wave_dir, track_id)
    if not target.parent.is_dir():
        return False
    with track_lock(target.parent, track_id, timeout=lock_timeout):
        try:
            target.unlink()
        except FileNotFoundError:
            return False
        with contextlib.suppress(OSError):
            _fsync_dir(target.parent)
        return True
