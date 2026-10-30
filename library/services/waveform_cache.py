"""Fail-closed validation of a Track's waveform cache (roadmap 1.21, r0123 Package A).

A waveform cache (``Track.waveform_path``, ``<track id>.json``) may be *displayed* when it is
well formed; it may feed a *marker calculation* (Repick Cue Points) only when it is **trusted**:
the version-2 contract below, whose audio identity equals the authoritative identity of the
Track's current media. Anything else -- missing, unreadable, truncated, malformed, another
Track's, inconsistent with the Track, a legacy (unversioned) cache, or a cache whose audio cannot
be proven current -- is refused, and the caller leaves every marker unchanged.

Version-2 contract (written by Package B's atomic cache promotion)::

    {"schema": "isadoraair.waveform", "schema_version": 2,
     "track_id": <int>,
     "audio_identity": {...},            # equals current_audio_identity(track), never inferred
     "analysis": {"sample_rate": <int>, "window_seconds": <float>, "waveform_points": <int>},
     "analysis_duration": <float > 0>,
     "envelope_times": [...], "envelope_db": [...],     # same length >= 1, finite, times ascending
     "samples_left": [...], "samples_right": [...],     # same length >= 1, integers 0..255
     "marker_revision": <int>, "markers": {<the eight markers>}}

Temporary restriction (until Package B): nothing can establish the authoritative audio identity
of a Track's media (``AUDIO_IDENTITY_PROVIDER`` is unset; ``Track.file_hash`` is opportunistic and
never trusted), so no cache is trusted and Repick abstains for every Track. Validation is not
relaxed to keep Repick available.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = "isadoraair.waveform"
SCHEMA_VERSION = 2
MAX_BYTES = 64 * 1024 * 1024
#: The cache's analysed duration may differ from the stored duration by this much (absolute or relative).
DURATION_TOLERANCE_SECONDS = 1.0
DURATION_TOLERANCE_RATIO = 0.02
ENVELOPE_DB_FLOOR, ENVELOPE_DB_CEILING = -120.0, 0.5

#: Package B installs ``callable(track) -> dict | None`` returning the authoritative identity of the
#: Track's current decoded audio. Until then there is none, and no cache is trusted.
AUDIO_IDENTITY_PROVIDER = None

# Verdicts
MISSING, UNREADABLE, MALFORMED, WRONG_TRACK, INCONSISTENT = "missing", "unreadable", "malformed", "wrong_track", \
    "inconsistent"
LEGACY, UNVERIFIED_AUDIO, TRUSTED = "legacy", "unverified_audio", "trusted"


@dataclass
class Verdict:
    status: str
    reason: str
    payload: dict | None = field(default=None, repr=False)

    @property
    def trusted(self) -> bool:
        return self.status == TRUSTED

    @property
    def displayable(self) -> bool:
        return self.status in (TRUSTED, UNVERIFIED_AUDIO, LEGACY)


def current_audio_identity(track):
    provider = AUDIO_IDENTITY_PROVIDER
    return provider(track) if provider is not None else None


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


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
    if versioned and not left:
        return "stereo samples are empty"
    if not all(isinstance(s, int) and not isinstance(s, bool) and 0 <= s <= 255 for s in left + right):
        return "stereo samples are not integers 0..255"
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


def validate(track, path=None) -> Verdict:
    """The cache's verdict for ``track`` (default path: ``track.waveform_path``)."""
    from library.services import cue_markers
    payload, failure = read(path if path is not None else track.waveform_path)
    if failure:
        return failure
    track_id = payload.get("track_id")
    if not isinstance(track_id, int) or isinstance(track_id, bool) or track_id != track.pk:
        return Verdict(WRONG_TRACK, f"the cache names Track {track_id!r}, not {track.pk}")
    versioned = payload.get("schema") == SCHEMA and payload.get("schema_version") == SCHEMA_VERSION
    problem = _structure_problem(payload, versioned=versioned)
    if problem:
        return Verdict(MALFORMED, problem)
    if _duration_mismatch(track.duration_seconds, payload["analysis_duration"]):
        return Verdict(INCONSISTENT, f"the cache's analysed duration {payload['analysis_duration']:.3f}s does not "
                                     f"match the Track's {track.duration_seconds:.3f}s")
    if not versioned:
        return Verdict(LEGACY, "legacy waveform cache (no version-2 identity): display only", payload)
    revision, markers = payload.get("marker_revision"), payload.get("markers")
    if not isinstance(revision, int) or isinstance(revision, bool) or not isinstance(markers, dict) \
            or set(markers) != set(cue_markers.MARKERS):
        return Verdict(MALFORMED, "version-2 cache without a complete marker snapshot")
    if revision > track.cue_marker_revision or (
            revision == track.cue_marker_revision
            and any(not cue_markers._same(markers[name], getattr(track, name)) for name in cue_markers.MARKERS)):
        return Verdict(INCONSISTENT, f"the cache's marker snapshot (revision {revision}) does not match the "
                                     f"Track's markers (revision {track.cue_marker_revision})")
    identity = payload.get("audio_identity")
    current = current_audio_identity(track)
    if current is None:
        return Verdict(UNVERIFIED_AUDIO, "the current audio identity cannot be established yet (Package B)",
                       payload)
    if not isinstance(identity, dict) or identity != current:
        return Verdict(UNVERIFIED_AUDIO, "the cache was computed from different audio than the Track's current media",
                       payload)
    return Verdict(TRUSTED, "trusted", payload)
