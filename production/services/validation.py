"""Authoritative, bounded validation of production media.

Nothing a client says is trusted: not the filename, not the browser MIME type,
not a reported duration. A file is judged by its bytes through four steps, each
run without a shell, with a hard timeout and bounded output:

1. content sniff -> ONE allowlisted ffmpeg demuxer is forced for every tool
   call (and only the ``file`` protocol is allowed), so an uploaded playlist or
   descriptor can never make ffmpeg fetch URLs or read other local files;
2. ffprobe (restricted to that demuxer) -> stream topology, container, codec,
   channels, sample rate, header duration;
3. a FULL ffmpeg decode with ``-xerror`` -> the decoded duration, the only
   duration worth trusting (browser WebM has no header duration at all);
4. the engine's own GStreamer decode in an isolated child process, proving the
   playout path can really decode what we accepted.

Two kinds of failure are kept strictly apart:

* a MEDIA verdict (``invalid`` + stable code) is a property of the bytes and
  is persisted on the ProductionMedia row;
* an INFRASTRUCTURE error (tool missing, timeout, killed, decoder not
  installed ...) says nothing about the bytes. It is never persisted as
  ``invalid``: the row stays ``unvalidated`` and validation can simply be
  retried.

Exit codes decide, never stderr text: a valid browser WebM prints an
error-level line yet decodes fine. Domain fitness (does it fit this intro?
this slot?) is the consumer's job; see production.policy.

Known, deliberate limit: truncation is detectable only where the container
carries an expected length (WAV, FLAC, AIFF, MP4 fail to decode; MP3 with a
Xing header and WebM with a duration are caught by decoded-vs-header
mismatch). A byte-truncated Ogg is a shorter, self-consistent file and is
indistinguishable from one. Silence/peak measurement is deliberately NOT done
here (see docs/PRODUCTION_MEDIA.md): a silent file is not an invalid file and
reliable measurement needs unbounded tool output.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.utils import timezone

from .. import formats, policy, transitions
from ..errors import MediaInconsistent, MediaPurged
from ..models import ProductionMedia
from . import layout, validator_commands

STATUS_VALID = "valid"
STATUS_INVALID = "invalid"
STATUS_INFRASTRUCTURE = "infrastructure_error"

# Media verdicts: persisted on the row as ``invalid``.
MEDIA_VERDICT_CODES = frozenset({
    "empty", "unreadable_container", "unsupported_container", "unsupported_codec",
    "no_audio_stream", "multiple_audio_streams", "video_stream_present",
    "invalid_stream_topology", "channels_out_of_range", "sample_rate_out_of_range",
    "too_long", "decode_error", "truncated_or_inconsistent", "empty_audio",
    "engine_decode_failed",
})
# Never persisted as ``invalid``; the media is not at fault.
INFRASTRUCTURE_CODES = frozenset({
    "storage_unreadable", "probe_unavailable", "probe_timeout", "probe_killed",
    "probe_output_invalid", "decoder_unavailable", "decode_unavailable",
    "decode_timeout", "decode_killed", "decode_output_invalid",
    "engine_probe_unavailable", "engine_probe_timeout", "engine_probe_failed",
    "engine_probe_killed",
    "engine_capability_unavailable",
    "validation_interrupted",
    # 2.22B resource confinement (production.services.confinement): a tool
    # stopped by a station resource limit, or validation that could not be
    # confined at all. Station limits, never a verdict on the bytes.
    "validation_resource_limit", "confinement_unavailable",
    # The validation service is at its admission limit: retry later.
    "validation_busy",
})

FFPROBE = "ffprobe"
FFMPEG = "ffmpeg"
FFPROBE_TIMEOUT_SECONDS = 20.0
CAPABILITY_TIMEOUT_SECONDS = 10.0
FFMPEG_TIMEOUT_SECONDS = 300.0
GSTREAMER_CHILD_TIMEOUT_SECONDS = 120.0
GSTREAMER_HARD_TIMEOUT_SECONDS = 125.0


def _canonical_interpreter() -> str:
    """This installation's Python, spelled the SAME way in every process.

    The validator commands are matched by exact argv equality between the
    client (Gunicorn) and the isadoraair-validation service, which rebuilds
    them from its own constants. The two processes are launched differently:
    on a station Gunicorn runs from its script's shebang (the virtualenv's
    real path, e.g. .../isadoraair-django/venv/bin/python3.14) and the service
    from ExecStart (@@ISA_ROOT@@/venv/bin/python through the /opt/isadoraair
    symlink), so sys.executable differs between them and every engine probe
    would be refused. A virtualenv's canonical ``bin/python`` (real prefix)
    is the same in both and runs the same environment."""
    if sys.prefix != sys.base_prefix:
        candidate = os.path.join(os.path.realpath(sys.prefix), "bin", "python")
        if os.path.isfile(candidate):
            return candidate
    return os.path.realpath(sys.executable)


# Production's OWN isolated probe (production/services/gst_probe.py), with a
# structured media-vs-capability taxonomy. The interpreter is a tuple so the
# tests can run it the way a host without GI bindings would (python -S).
GSTREAMER_PROBE_SCRIPT = os.path.realpath(Path(__file__).with_name("gst_probe.py"))
GSTREAMER_PROBE_INTERPRETER = (_canonical_interpreter(),)
VALIDATOR_VERSION = 1

_PROBE_ENTRIES = validator_commands.PROBE_ENTRIES
_OUT_TIME_RE = re.compile(r"^out_time_us=(\d+)$", re.MULTILINE)
_VERSION_RE = re.compile(r"^(?:ffprobe|ffmpeg) version (\S+)")
_MICRO = Decimal("0.000001")
# Decoded audio may be shorter than the container's declared duration by this
# much (the larger of the two) before the file is called truncated. One-sided:
# a decoded duration LONGER than an estimated header is not truncation.
_TRUNCATION_TOLERANCE_SECONDS = Decimal("1.0")
_TRUNCATION_TOLERANCE_FRACTION = Decimal("0.05")


@dataclass(frozen=True)
class ValidationOutcome:
    status: str
    code: str
    facts: dict = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return self.status == STATUS_VALID

    @property
    def is_infrastructure_error(self) -> bool:
        return self.status == STATUS_INFRASTRUCTURE


def _invalid(code, facts=None):
    assert code in MEDIA_VERDICT_CODES, code
    return ValidationOutcome(STATUS_INVALID, code, facts or {})


def _infrastructure(code):
    assert code in INFRASTRUCTURE_CODES, code
    return ValidationOutcome(STATUS_INFRASTRUCTURE, code, {})


# -- process boundary -------------------------------------------------------

def _run(args, *, timeout_seconds, stop_event=None, media=None):
    """Bounded, shell-free, OS-confined execution (2.22B): every tool run is
    handed to the isadoraair-validation service, which owns its kernel-limited
    cgroup (aggregate memory, tasks, CPU), its Landlock/seccomp sandbox and its
    hard deadline, and destroys the whole tree when the run ends -- even if
    this process dies. ``media`` (the path inside ``args``) travels as an open
    descriptor. If the service or the boundary is unavailable the tool is not
    run at all (fail closed) -- see production.services.confinement."""
    from . import confinement
    return confinement.run_confined(args, timeout_seconds=timeout_seconds, stop_event=stop_event, media=media)


def _run_failure(result, prefix):
    """Map a non-ok bounded-run result to an outcome. Exit codes > 0 are the
    TOOL's verdict on the input (caller decides); everything else is infra."""
    status = result.get("status")
    if status in _SERVICE_REFUSALS:
        return _infrastructure(_SERVICE_REFUSALS[status])
    if status == "resource_limit":
        return _infrastructure("validation_resource_limit")
    if status == "timeout":
        return _infrastructure(f"{prefix}_timeout")
    if status == "stopped":
        return _infrastructure("validation_interrupted")
    if status in ("unavailable", "infrastructure_error"):
        return _infrastructure(f"{prefix}_unavailable")
    code = result.get("returncode")
    if status == "failed" and isinstance(code, int) and code > 0:
        return None                       # the tool rejected the input
    return _infrastructure(f"{prefix}_killed")


class _Interrupted(Exception):
    """The caller's stop_event fired while a tool was running."""


class _Unconfined(Exception):
    """The validation service / kernel boundary is unavailable, or the service
    is at capacity: no tool ran. ``code`` is the infrastructure code."""

    def __init__(self, code="confinement_unavailable"):
        super().__init__(code)
        self.code = code


# Run statuses meaning the service did not (or could not safely) run the tool.
_SERVICE_REFUSALS = {"confinement_unavailable": "confinement_unavailable", "busy": "validation_busy"}


_TOOL_VERSIONS: dict[str, str] = {}
_DECODER_CACHE: dict[str, bool] = {}


def clear_capability_cache():
    _TOOL_VERSIONS.clear()
    _DECODER_CACHE.clear()


def _tool_version(binary, stop_event=None):
    if binary in _TOOL_VERSIONS:
        return _TOOL_VERSIONS[binary]
    result = _run(validator_commands.tool_version(binary), timeout_seconds=CAPABILITY_TIMEOUT_SECONDS,
                  stop_event=stop_event)
    if result.get("status") == "stopped":
        raise _Interrupted
    if result.get("status") in _SERVICE_REFUSALS:
        raise _Unconfined(_SERVICE_REFUSALS[result["status"]])
    if result.get("status") != "ok":
        return None
    match = _VERSION_RE.match(result.get("stdout", ""))
    version = match.group(1)[:64] if match else "unknown"
    _TOOL_VERSIONS[binary] = version
    return version


def _decoder_available(name, stop_event=None):
    """True/False whether this ffmpeg has decoder ``name``; None if unknown."""
    if name in _DECODER_CACHE:
        return _DECODER_CACHE[name]
    result = _run(validator_commands.decoder_help(FFMPEG, name),
                  timeout_seconds=CAPABILITY_TIMEOUT_SECONDS, stop_event=stop_event)
    if result.get("status") == "stopped":
        raise _Interrupted
    if result.get("status") in _SERVICE_REFUSALS:
        raise _Unconfined(_SERVICE_REFUSALS[result["status"]])
    if result.get("status") != "ok":
        return None
    available = result.get("stdout", "").lstrip().startswith(f"Decoder {name}")
    _DECODER_CACHE[name] = available
    return available


# -- parsing helpers ----------------------------------------------------------

def _decimal(value):
    if value in (None, "", "N/A"):
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed.quantize(_MICRO)


def _positive_int(value):
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _stream_evidence(stream):
    channels = stream.get("channels")
    return {
        "index": stream.get("index") if isinstance(stream.get("index"), int) else None,
        "codec_type": str(stream.get("codec_type", ""))[:16],
        "codec_name": str(stream.get("codec_name", ""))[:32],
        "sample_rate": str(stream.get("sample_rate", ""))[:12],
        "channels": channels if isinstance(channels, int) and not isinstance(channels, bool) else None,
        "channel_layout": str(stream.get("channel_layout", ""))[:32],
        "attached_pic": bool((stream.get("disposition") or {}).get("attached_pic")),
    }


# -- the analysis ---------------------------------------------------------------

def _analyze_path(path: Path, *, require_engine_decode=True, stop_event=None) -> ValidationOutcome:
    """Judge the bytes at ``path`` (a system-generated path: an incoming/ part
    file or a resolved media/ key). Pure with respect to the database."""
    try:
        return _analyze(path, require_engine_decode=require_engine_decode, stop_event=stop_event)
    except _Interrupted:
        return _infrastructure("validation_interrupted")
    except _Unconfined as exc:
        return _infrastructure(exc.code)


def _analyze(path: Path, *, require_engine_decode, stop_event) -> ValidationOutcome:
    # 1. content sniff -------------------------------------------------------
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return _infrastructure("storage_unreadable")
    try:
        size = os.fstat(fd).st_size
        if size == 0:
            return _invalid("empty")
        head = os.pread(fd, 16, 0)
        sniffed = formats.sniff_container(head, lambda offset, count: os.pread(fd, count, offset))
    except OSError:
        return _infrastructure("storage_unreadable")
    finally:
        os.close(fd)
    if sniffed is None:
        return _invalid("unsupported_container")
    demuxer = formats.CONTAINERS[sniffed][0]          # the probe and decode force it, file protocol only

    probe_version = _tool_version(FFPROBE, stop_event)
    ffmpeg_version = _tool_version(FFMPEG, stop_event)
    if probe_version is None or ffmpeg_version is None:
        return _infrastructure("probe_unavailable")

    # 2. ffprobe ---------------------------------------------------------------
    result = _run(validator_commands.probe(FFPROBE, demuxer, path),
                  timeout_seconds=FFPROBE_TIMEOUT_SECONDS, stop_event=stop_event, media=path)
    if result.get("status") != "ok":
        failure = _run_failure(result, "probe")
        return failure if failure is not None else _invalid("unreadable_container", {"container": sniffed})
    if result.get("stdout_truncated"):
        return _invalid("invalid_stream_topology", {"container": sniffed})
    try:
        payload = json.loads(result["stdout"])
        fmt = payload["format"]
        streams = payload.get("streams") or []
        format_name = str(fmt["format_name"])
        assert isinstance(streams, list) and all(isinstance(item, dict) for item in streams)
    except (ValueError, KeyError, TypeError, AssertionError):
        return _infrastructure("probe_output_invalid")

    evidence = {
        "validator_version": VALIDATOR_VERSION,
        "sniffed_container": sniffed,
        "format_name": format_name[:80],
        "nb_streams": len(streams),
        "streams": [_stream_evidence(stream) for stream in streams[: policy.PLATFORM_MAX_STREAMS]],
        "tools": {"ffprobe": probe_version, "ffmpeg": ffmpeg_version},
    }
    facts = {"container": sniffed, "probe": evidence}

    # 3. topology ----------------------------------------------------------------
    if len(streams) > policy.PLATFORM_MAX_STREAMS:
        return _invalid("invalid_stream_topology", facts)
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    pictures = [s for s in streams if s.get("codec_type") == "video"
                and (s.get("disposition") or {}).get("attached_pic")]
    other_video = [s for s in streams if s.get("codec_type") == "video" and s not in pictures]
    odd = [s for s in streams if s.get("codec_type") not in ("audio", "video", "data", "attachment")]
    if other_video:
        return _invalid("video_stream_present", facts)
    if odd or len(pictures) > policy.PLATFORM_MAX_ATTACHED_PICTURES:
        return _invalid("invalid_stream_topology", facts)
    if not audio:
        return _invalid("no_audio_stream", facts)
    if len(audio) > 1:
        return _invalid("multiple_audio_streams", facts)
    stream = audio[0]
    classification, code = formats.classify(sniffed, format_name, str(stream.get("codec_name", "")))
    if classification is None:
        return _invalid(code, facts)
    facts["codec"] = classification.codec
    channels = stream.get("channels")
    if not isinstance(channels, int) or isinstance(channels, bool) \
            or not 1 <= channels <= policy.PLATFORM_MAX_CHANNELS:
        return _invalid("channels_out_of_range", facts)
    facts["channels"] = channels
    sample_rate = _positive_int(stream.get("sample_rate"))
    if sample_rate is None or not policy.PLATFORM_MIN_SAMPLE_RATE <= sample_rate <= policy.PLATFORM_MAX_SAMPLE_RATE:
        return _invalid("sample_rate_out_of_range", facts)
    facts["sample_rate"] = sample_rate
    header_duration = _decimal(fmt.get("duration"))
    facts["header_duration_seconds"] = header_duration
    if header_duration is not None and header_duration > policy.PLATFORM_MAX_DURATION_SECONDS:
        return _invalid("too_long", facts)

    # 4. is a decoder for this codec installed? (infrastructure, not the media's fault)
    # A codec on our allowlist whose decoder this host lacks (or whose
    # availability cannot be established) is a host problem, never a bad file.
    names = formats.decoder_names(classification.codec)
    if not any(_decoder_available(name, stop_event) is True for name in names):
        return _infrastructure("decoder_unavailable")

    # 5. full decode -> authoritative duration ----------------------------------------
    result = _run(validator_commands.decode(FFMPEG, demuxer, path),
                  timeout_seconds=FFMPEG_TIMEOUT_SECONDS, stop_event=stop_event, media=path)
    if result.get("status") != "ok":
        failure = _run_failure(result, "decode")
        return failure if failure is not None else _invalid("decode_error", facts)
    times = _OUT_TIME_RE.findall(result.get("stdout", ""))
    if not times:
        return _infrastructure("decode_output_invalid")
    decoded = (Decimal(int(times[-1])) / Decimal(1_000_000)).quantize(_MICRO)
    facts["decoded_duration_seconds"] = decoded
    if decoded <= 0:
        return _invalid("empty_audio", facts)
    if decoded > policy.PLATFORM_MAX_DURATION_SECONDS:
        return _invalid("too_long", facts)
    if header_duration is not None:
        tolerance = max(_TRUNCATION_TOLERANCE_SECONDS, header_duration * _TRUNCATION_TOLERANCE_FRACTION)
        if decoded < header_duration - tolerance:
            return _invalid("truncated_or_inconsistent", facts)

    # 6. the engine's own decoder -------------------------------------------------------
    if require_engine_decode:
        failure = _engine_decode(path, evidence, facts, stop_event)
        if failure is not None:
            return failure
    else:
        evidence["engine_decode"] = {"status": "skipped"}

    return ValidationOutcome(STATUS_VALID, "ok", facts)


def _engine_decode(path, evidence, facts, stop_event):
    """Run the GStreamer parity probe. None on success; otherwise the outcome.

    Only a ``media_error`` (the runtime is capable, these bytes do not decode)
    becomes an ``invalid`` verdict. A ``capability_error`` (missing decoder,
    plugin, element or GI) and every other failure are retryable
    infrastructure -- valid media is never condemned for what the station
    runtime lacks."""
    result = _run(validator_commands.engine_decode(GSTREAMER_PROBE_INTERPRETER, GSTREAMER_PROBE_SCRIPT,
                                                   GSTREAMER_CHILD_TIMEOUT_SECONDS, path),
                  timeout_seconds=GSTREAMER_HARD_TIMEOUT_SECONDS, stop_event=stop_event, media=path)
    if result.get("status") != "ok":
        failure = _run_failure(result, "engine_probe")
        # A child that exits non-zero without a verdict is a broken probe
        # environment, not a bad file.
        return failure if failure is not None else _infrastructure("engine_probe_failed")
    try:
        verdict = json.loads(result["stdout"])
        status = verdict["status"]
    except (ValueError, KeyError, TypeError):
        return _infrastructure("engine_probe_failed")
    engine = {"status": str(status)[:24], "reason": str(verdict.get("reason", ""))[:40]}
    for key in ("buffers", "error_code"):
        if isinstance(verdict.get(key), int) and not isinstance(verdict.get(key), bool):
            engine[key] = verdict[key]
    if verdict.get("error_domain"):
        engine["error_domain"] = str(verdict["error_domain"])[:64]
    evidence["engine_decode"] = engine
    if status == "eos":
        return None
    if status == "media_error":
        return _invalid("engine_decode_failed", facts)
    if status == "capability_error":
        return _infrastructure("engine_capability_unavailable")
    if status == "timeout":
        return _infrastructure("engine_probe_timeout")
    return _infrastructure("engine_probe_failed")


# -- persistence ---------------------------------------------------------------------

_FACT_FIELDS = (
    "container", "codec", "sample_rate", "channels", "decoded_duration_seconds",
    "header_duration_seconds", "probe",
)


def verdict_columns(outcome: ValidationOutcome, now) -> dict:
    """The ProductionMedia column values that record a persisted verdict."""
    assert outcome.status in (STATUS_VALID, STATUS_INVALID)
    columns = {name: outcome.facts[name] for name in _FACT_FIELDS if name in outcome.facts}
    columns.update(validation_state=outcome.status, validation_code=outcome.code, validated_at=now)
    return columns


def outcome_from_row(media: ProductionMedia) -> ValidationOutcome:
    """The recorded verdict of an already-validated row."""
    assert media.validation_state != ProductionMedia.VALIDATION_UNVALIDATED
    facts = {name: getattr(media, name) for name in _FACT_FIELDS}
    return ValidationOutcome(media.validation_state, media.validation_code, facts)


def validate_media(media, *, require_engine_decode=True, now=None, stop_event=None) -> ValidationOutcome:
    """Validate a stored, still-unvalidated ProductionMedia and persist the
    verdict. Idempotent: an already-validated row returns its recorded verdict.
    An infrastructure error is returned but never recorded as ``invalid``; the
    row stays ``unvalidated`` (with the failure code noted) and the call can be
    repeated."""
    pk = getattr(media, "pk", media)
    row = ProductionMedia.objects.get(pk=pk)
    if row.retention_state != ProductionMedia.RETENTION_PRESENT:
        raise MediaPurged("media bytes have been purged")
    if row.validation_state != ProductionMedia.VALIDATION_UNVALIDATED:
        return outcome_from_row(row)
    path = layout.resolve_storage_path(row.storage_key)
    try:
        size = path.lstat().st_size
    except OSError as exc:
        raise MediaInconsistent("media bytes are missing", code="missing_bytes") from exc
    if size != row.byte_size:
        raise MediaInconsistent("media bytes do not match the recorded size", code="size_mismatch")
    outcome = _analyze_path(path, require_engine_decode=require_engine_decode, stop_event=stop_event)
    now = now or timezone.now()
    if outcome.status in (STATUS_VALID, STATUS_INVALID):
        if transitions.record_verdict(row.pk, verdict_columns(outcome, now)) == 1:
            return outcome
        # The one-time transition did not apply: someone else recorded a
        # verdict first, or the media was purged while we analysed it.
        # Report what is actually true -- never assume.
        current = ProductionMedia.objects.get(pk=row.pk)
        if current.retention_state != ProductionMedia.RETENTION_PRESENT:
            raise MediaPurged("media bytes were purged during validation")
        if current.validation_state != ProductionMedia.VALIDATION_UNVALIDATED:
            return outcome_from_row(current)
        raise MediaInconsistent("the verdict could not be recorded", code="verdict_not_recorded")
    if transitions.record_infrastructure_attempt(row.pk, outcome.code) == 0:
        current = ProductionMedia.objects.get(pk=row.pk)
        if current.retention_state != ProductionMedia.RETENTION_PRESENT:
            raise MediaPurged("media bytes were purged during validation")
        if current.validation_state != ProductionMedia.VALIDATION_UNVALIDATED:
            return outcome_from_row(current)
    return outcome
