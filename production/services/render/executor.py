"""The render executor core: ONE recipe render over descriptors only.

Runs inside isadoraair-render (production.services.render_service), never in
the web process or a worker. It receives already-open, READ-ONLY descriptors
for the source and each pinned reference (beds) and:

1. SNAPSHOTS every input: proves the descriptor is a regular file opened
   read-only of the claimed size, then copies its bytes into a service-owned
   memfd IN THE SAME read pass that counts and hashes them, refuses unless
   exactly the claimed size and SHA-256 were copied (a substituted descriptor
   is refused before anything runs), and SEALS the memfd (no write, grow,
   shrink or further seal change, ever). From then on the caller's descriptor
   is no longer authoritative -- it is released (``release``) before the first
   step and nothing reads it again: a read-only descriptor does not make its
   inode immutable, the sealed snapshot does;
2. resolves the recipe from the code registry (the version must be the
   registered one), validates the parameters against its schema, checks the
   pinned references are exactly the ones the plan uses, and refuses unless
   its own canonical provenance digest equals the client's expectation;
3. runs the FIXED steps -- prepare, mix, loudness analysis, encode -- each a
   ffmpeg invocation built here from the step library, through ``run_step``
   (in the service: confinement.execute in the render subtree with the
   socket-denying seccomp profile). Every input is a fresh read-only reopen of
   a SEALED memfd -- an input snapshot or a finished, sealed intermediate --
   (``-fd N -i fd:``; reopen_read_only refuses anything unsealed), every
   output a memfd created here (``-fd N fd:``), and ``-protocol_whitelist fd`` refuses every other ffmpeg
   protocol (file:, http:, ...). The tool tree therefore needs, and has, no
   filesystem write authority at all;
4. seals the FLAC memfd (no further write, grow or shrink, ever) and hashes it.

Every snapshot and intermediate is closed as soon as its last step is done;
nothing persists beyond the request.

The caller owns the returned descriptor. Nothing here touches the database,
the media store or any path a client chose.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import io
import os
import stat
import time
import wave
from dataclasses import dataclass
from decimal import Decimal

from . import params as params_mod
from . import protocol, provenance, registry
from .engine import RenderError, parse_loudnorm
from .steps import EncodeFlac

SEALS = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
_HASH_CHUNK = 1024 * 1024
# The most input bytes (source + references) one render snapshots into memory.
MAX_INPUT_BYTES = 1024 * 1024 * 1024
_RESOURCE_ERRNOS = frozenset({errno.ENOMEM, errno.ENOSPC, errno.EFBIG, errno.EDQUOT})
_DETAIL_LIMIT = 500


@dataclass
class Rendered:
    fd: int                         # sealed memfd: the FLAC
    sha256: str
    size: int
    digest: str
    provenance: dict
    measured_input: dict
    recipe_key: str
    recipe_version: int


def fd_sha256(fd: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(fd, _HASH_CHUNK, offset)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)
        offset += len(chunk)


def is_sealed(fd: int) -> bool:
    try:
        return fcntl.fcntl(fd, fcntl.F_GET_SEALS) & SEALS == SEALS
    except OSError:
        return False


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def snapshot_input(fd: int, entry: dict, name: str) -> int:
    """An immutable, service-owned copy of EXACTLY the claimed bytes: a sealed
    memfd, or RenderError. The copy, the byte count and the SHA-256 come from
    ONE read pass over ``fd`` (never "hash, then copy later"), reading at most
    one byte past the claimed size; a mutation of the caller's inode after
    this returns cannot reach the snapshot."""
    try:
        info = os.fstat(fd)
        mode = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
    except OSError:
        raise RenderError("descriptor_invalid", "a descriptor cannot be inspected") from None
    if not stat.S_ISREG(info.st_mode) or mode != os.O_RDONLY:
        raise RenderError("descriptor_invalid", "inputs are regular files opened read-only")
    expected = entry["bytes"]
    if info.st_size != expected:
        raise RenderError("descriptor_mismatch", "a descriptor does not hold the claimed bytes")
    snapshot = new_memfd(f"input-{name}")
    try:
        digest, copied = hashlib.sha256(), 0
        while True:
            chunk = os.pread(fd, min(_HASH_CHUNK, expected + 1 - copied), copied)
            if not chunk:
                break
            digest.update(chunk)
            _write_all(snapshot, chunk)
            copied += len(chunk)
            if copied > expected:
                break
        if copied != expected or digest.hexdigest() != entry["sha256"]:
            raise RenderError("descriptor_mismatch", "a descriptor does not hold the claimed bytes")
        fcntl.fcntl(snapshot, fcntl.F_ADD_SEALS, SEALS)
        if os.fstat(snapshot).st_size != expected or not is_sealed(snapshot):
            raise RenderError("render_failed", "an input snapshot could not be sealed", stage="snapshot")
        sealed, snapshot = snapshot, None
        return sealed
    except OSError as exc:
        if exc.errno in _RESOURCE_ERRNOS:
            raise RenderError("render_resource_limit", "no room to snapshot an input", stage="snapshot") from None
        raise RenderError("descriptor_invalid", "a descriptor cannot be read") from None
    finally:
        if snapshot is not None:
            os.close(snapshot)


def snapshot_inputs(request: dict, source_fd: int, reference_fds: dict) -> tuple[int, dict]:
    """(source snapshot, {param: reference snapshot}) -- all sealed -- or
    RenderError with nothing left open."""
    total = request["source"]["bytes"] + sum(entry["bytes"] for entry in request["references"].values())
    if total > MAX_INPUT_BYTES:
        raise RenderError("input_too_large", "the inputs are larger than one render may hold", stage="snapshot")
    taken = []
    try:
        source = snapshot_input(source_fd, request["source"], "source")
        taken.append(source)
        references = {}
        for name in sorted(reference_fds):
            references[name] = snapshot_input(reference_fds[name], request["references"][name], name)
            taken.append(references[name])
        taken = []
        return source, references
    finally:
        for fd in taken:
            os.close(fd)


def reopen_read_only(fd: int) -> int:
    """A fresh, read-only description (its own offset 0) of a SEALED memfd --
    an input snapshot or a finished intermediate. Anything else (a caller's
    descriptor, an unsealed file) is refused: no step ever reads bytes that
    can still change."""
    if not is_sealed(fd):
        raise RenderError("render_failed", "a step input is not a sealed snapshot", stage="snapshot")
    return os.open(f"/proc/self/fd/{fd}", os.O_RDONLY | os.O_CLOEXEC)


def new_memfd(name: str) -> int:
    return os.memfd_create(f"isadoraair-render-{name}", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)


def _wav_seconds(fd: int) -> float:
    data = io.BytesIO(os.pread(fd, os.fstat(fd).st_size, 0))
    with wave.open(data, "rb") as handle:
        rate = handle.getframerate()
        return handle.getnframes() / rate if rate else 0.0


def plan_request(request: dict) -> tuple:
    """(recipe, resolved, plan, pins, digest, document) -- or RenderError.
    Pure: the recipe semantics of a shape-checked request."""
    try:
        recipe, resolved, plan = provenance.prepare(request["recipe_key"], request["params"])
    except registry.UnknownRecipe as exc:
        raise RenderError("unknown_recipe", str(exc)) from exc
    except params_mod.RecipeParameterError as exc:
        raise RenderError("invalid_parameters", str(exc)) from exc
    if request["recipe_version"] != recipe.version:
        raise RenderError("recipe_version_mismatch", "the recipe version is not the registered one")
    keys = provenance.reference_keys(plan, resolved)
    references = request["references"]
    if set(references) != set(keys) or any(references[name]["key"] != key for name, key in keys.items()):
        raise RenderError("reference_mismatch", "the pinned references are not the ones this plan uses")
    pins = {name: protocol.pin_of(entry) for name, entry in references.items()}
    document = provenance.document(recipe, resolved, pins)
    digest = provenance.digest(document)
    if digest != request["expected_digest"]:
        raise RenderError("provenance_mismatch", "the expected provenance is not what this render produces")
    return recipe, resolved, plan, pins, digest, document


def _status(result, stage):
    status = result.get("status")
    if status == "ok":
        return
    if status == "timeout":
        raise RenderError("render_timeout", f"the {stage} step exceeded its time limit", retryable=True, stage=stage)
    if status == "stopped":
        raise RenderError("cancelled", "the client went away", retryable=True, stage=stage)
    if status == "resource_limit":
        raise RenderError("render_resource_limit", f"the {stage} step hit a resource limit", stage=stage)
    if status == "confinement_unavailable":
        raise RenderError("sandbox_unavailable", "the render sandbox is unavailable", retryable=True, stage=stage)
    if status in ("unavailable", "busy", "infrastructure_error"):
        raise RenderError("render_unavailable", f"the {stage} step could not run", retryable=True, stage=stage)
    raise RenderError("render_failed", f"the {stage} step failed (exit {result.get('returncode')}): "
                      + str(result.get("stderr", ""))[-_DETAIL_LIMIT:], stage=stage)


def execute(request: dict, source_fd: int, reference_fds: dict, *, run_step, ffmpeg: str, deadline: float,
            max_seconds: Decimal, release=None) -> Rendered:
    """Render one shape-checked request. ``reference_fds``: {param: fd} (the
    caller's descriptors; read ONCE, into sealed snapshots).
    ``run_step(argv, pass_fds, timeout_seconds) -> confinement result``.
    ``deadline``: monotonic time by which everything must be done.
    ``release()``: called once the snapshots are sealed, before the first
    step -- the service closes the caller's descriptors there."""
    recipe, resolved, plan, pins, digest, document = plan_request(request)
    if request["source"]["duration_seconds"] > max_seconds:
        raise RenderError("source_too_long", "the source is longer than this service renders")
    source_snapshot, reference_snapshots = snapshot_inputs(request, source_fd, reference_fds)
    held = {"source": source_snapshot, **{f"reference:{name}": fd for name, fd in reference_snapshots.items()}}
    opened = []

    def fresh_ro(fd):
        handle = reopen_read_only(fd)
        opened.append(handle)
        return handle

    def drop(*names):
        """Close what no later step reads (snapshots, finished intermediates)."""
        for name in names:
            fd = held.pop(name, None)
            if fd is not None:
                os.close(fd)
        while opened:
            os.close(opened.pop())

    def seal(fd):
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, SEALS)

    def step(stage, argv, pass_fds):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RenderError("render_timeout", "the render exceeded its time limit", retryable=True, stage=stage)
        result = run_step(argv, tuple(pass_fds), remaining)
        _status(result, stage)
        return result

    common = [ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-y", "-protocol_whitelist", "fd"]
    output = None
    try:
        if release is not None:
            release()                               # the caller's descriptors are no longer authoritative
        held["voice"] = voice = new_memfd("voice")
        if plan.bed is not None:
            held["mix"] = mixed = new_memfd("mix")
        output = new_memfd("rendition")
        # 1. the voice, 48 kHz stereo 16-bit
        source = fresh_ro(source_snapshot)
        argv = [*common, "-loglevel", "error", "-fd", str(source), "-i", "fd:", "-map", "0:a:0", "-vn", "-sn",
                "-dn", "-ac", "2", "-ar", "48000"]
        if plan.voice:
            argv += ["-af", plan.voice_filter()]
        step("prepare", [*argv, "-c:a", "pcm_s16le", "-f", "wav", "-fd", str(voice), "fd:"], (source, voice))
        drop("source")
        seal(voice)
        voice_seconds = _wav_seconds(voice)
        if voice_seconds <= 0:
            raise RenderError("empty_voice", "no voice audio remained after preparation", stage="prepare")

        # 2. the bed underlay
        master = voice
        if plan.bed is not None:
            delay = plan.delay.ms / 1000.0 if plan.delay is not None else 0.0
            bed_seconds = float(request["references"][plan.bed.reference_param]["duration_seconds"])
            graph = plan.bed.filter_complex(voice_end=voice_seconds + delay, bed_duration=bed_seconds,
                                            delay=plan.delay)
            bed, voice_in = fresh_ro(held[f"reference:{plan.bed.reference_param}"]), fresh_ro(voice)
            argv = [*common, "-loglevel", "error"]
            if plan.bed.loops:
                argv += ["-stream_loop", "-1"]
            argv += ["-fd", str(bed), "-i", "fd:", "-fd", str(voice_in), "-i", "fd:", "-filter_complex", graph,
                     "-map", "[mix]", "-c:a", "pcm_s16le", "-f", "wav", "-fd", str(mixed), "fd:"]
            step("mix", argv, (bed, voice_in, mixed))
            drop(f"reference:{plan.bed.reference_param}", "voice")
            seal(mixed)
            master = mixed

        # 3. two-pass loudness normalization + FLAC
        master_in = fresh_ro(master)
        result = step("loudnorm_analysis", [*common, "-loglevel", "info", "-fd", str(master_in), "-i", "fd:",
                                            "-af", plan.loudnorm.analysis_filter(), "-f", "null", "-"],
                      (master_in,))
        measured = parse_loudnorm(str(result.get("stderr", "")).encode("utf-8", "replace"))
        master_in = fresh_ro(master)
        step("encode", [*common, "-loglevel", "error", "-fd", str(master_in), "-i", "fd:", "-af",
                        plan.loudnorm.apply_filter(measured), *plan.encode.output_args(), "-f", "flac",
                        "-fd", str(output), "fd:"], (master_in, output))
        if os.fstat(output).st_size == 0:
            raise RenderError("render_failed", "the encoder produced no output", stage="encode")
        fcntl.fcntl(output, fcntl.F_ADD_SEALS, SEALS)
        rendered = Rendered(fd=output, sha256=fd_sha256(output), size=os.fstat(output).st_size, digest=digest,
                            provenance=document, measured_input=measured, recipe_key=recipe.key,
                            recipe_version=recipe.version)
        output = None                                    # ownership passes to the caller
        return rendered
    except OSError as exc:
        if exc.errno in _RESOURCE_ERRNOS:
            raise RenderError("render_resource_limit", "no room for a render intermediate") from None
        raise
    finally:
        for fd in (*opened, *held.values(), output):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


# -- assemblies (iPortal Phase D2A) ------------------------------------------------------------

def plan_assembly(request: dict) -> tuple:
    """(assembly, digest, document) of a shape-checked ``assemble`` request --
    or RenderError. Pure."""
    try:
        recipe = provenance.assembly(request["recipe_key"])
    except registry.UnknownRecipe as exc:
        raise RenderError("unknown_recipe", str(exc)) from exc
    if request["recipe_version"] != recipe.version:
        raise RenderError("recipe_version_mismatch", "the assembly version is not the registered one")
    document = provenance.assembly_document(recipe, [protocol.assembly_input_of(entry) for entry in request["inputs"]])
    digest = provenance.digest(document)
    if digest != request["expected_digest"]:
        raise RenderError("provenance_mismatch", "the expected provenance is not what this assembly produces")
    return recipe, digest, document


_ASSEMBLY_PREPARE = "aresample=48000,aformat=sample_fmts=s16:channel_layouts=stereo"
# Raw PCM of the rendition format: 48 kHz, stereo, signed 16-bit LE (s16le).
_PCM_ARGS = ["-c:a", "pcm_s16le", "-f", "s16le"]
_PCM_FRAME_BYTES = 4
_PCM_RATE = 48000
# How far past the remaining allowance a decode may run before the step itself
# stops it: enough to PROVE an over-long input, never an unbounded write.
_DECODE_OVERRUN_SECONDS = Decimal(1)


def pcm_seconds(byte_count: int) -> Decimal:
    """The ACTUAL duration of raw 48 kHz stereo s16le PCM (4 bytes a frame)."""
    return Decimal(byte_count // _PCM_FRAME_BYTES) / _PCM_RATE


def _append(target: int, source: int) -> int:
    """Append a sealed memfd's bytes to ``target``; returns the count."""
    offset = 0
    while True:
        chunk = os.pread(source, _HASH_CHUNK, offset)
        if not chunk:
            return offset
        _write_all(target, chunk)
        offset += len(chunk)


def execute_assembly(request: dict, fds: list, *, run_step, ffmpeg: str, deadline: float, release=None) -> Rendered:
    """Assemble one shape-checked ``assemble`` request. ``fds``: the caller's
    descriptors in input order (each read ONCE, into a sealed snapshot whose
    size and SHA-256 must be the claimed ones).

    Each input is decoded ALONE, in its own confined step, to raw PCM of the
    rendition format (48 kHz stereo s16le); the executor appends those
    identically formatted samples in input order -- a sequential join with no
    gap, overlap or processing -- and one final single-input step encodes the
    FLAC. (One ffmpeg over 25 inputs would need a demuxer and decoder thread
    per input, beyond the render sandbox's task limit.)

    The service's absolute ceilings bind the ACTUAL decoded audio, not the
    durations the request claims: each decode is cut off by the step itself
    just past what may still be allowed (``-t``, from the ceilings only), and
    each input's real duration -- its PCM byte count -- must be within
    MAX_ASSEMBLE_INPUT_SECONDS and, cumulatively, MAX_ASSEMBLE_TOTAL_SECONDS,
    or the assembly is refused (``decoded_too_long``) before that input is
    appended. Declared and decoded durations may differ slightly (codec and
    container timing); only the ceilings are absolute."""
    recipe, digest, document = plan_assembly(request)
    inputs = request["inputs"]
    if len(fds) != len(inputs):
        raise RenderError("invalid_request", "one descriptor per input")
    if sum(entry["bytes"] for entry in inputs) > MAX_INPUT_BYTES:
        raise RenderError("input_too_large", "the inputs are larger than one render may hold", stage="snapshot")
    common = [ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-y", "-protocol_whitelist", "fd", "-loglevel", "error"]
    snapshots, opened, output, joined, piece = [], [], None, None, None

    def step(stage, argv, pass_fds):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RenderError("render_timeout", "the assembly exceeded its time limit", retryable=True, stage=stage)
        _status(run_step(argv, tuple(pass_fds), remaining), stage)

    def fresh_ro(fd):
        handle = reopen_read_only(fd)
        opened.append(handle)
        return handle

    def close_opened():
        while opened:
            os.close(opened.pop())

    try:
        for index, (fd, entry) in enumerate(zip(fds, inputs)):
            snapshots.append(snapshot_input(fd, entry, f"input{index}"))
        if release is not None:
            release()                               # the caller's descriptors are no longer authoritative
        joined = new_memfd("assembly-pcm")
        decoded_total = Decimal(0)
        for index in range(len(snapshots)):
            allowance = min(protocol.MAX_ASSEMBLE_INPUT_SECONDS, protocol.MAX_ASSEMBLE_TOTAL_SECONDS - decoded_total)
            piece = new_memfd(f"pcm{index}")
            source = fresh_ro(snapshots[index])
            step(f"prepare{index}", [*common, "-fd", str(source), "-i", "fd:", "-map", "0:a:0", "-vn", "-sn", "-dn",
                                     "-af", _ASSEMBLY_PREPARE, "-t", f"{allowance + _DECODE_OVERRUN_SECONDS:.6f}",
                                     *_PCM_ARGS, "-fd", str(piece), "fd:"],
                 (source, piece))
            close_opened()
            os.close(snapshots[index])
            snapshots[index] = None
            size = os.fstat(piece).st_size
            if size == 0 or size % _PCM_FRAME_BYTES:
                raise RenderError("render_failed", "an input decoded to no whole audio frames", stage=f"prepare{index}")
            seconds = pcm_seconds(size)
            if seconds > protocol.MAX_ASSEMBLE_INPUT_SECONDS:
                raise RenderError("decoded_too_long", "an input decodes longer than one input may be",
                                  stage=f"prepare{index}")
            if decoded_total + seconds > protocol.MAX_ASSEMBLE_TOTAL_SECONDS:
                raise RenderError("decoded_too_long", "the inputs decode longer than one assembly may be",
                                  stage=f"prepare{index}")
            decoded_total += seconds
            fcntl.fcntl(piece, fcntl.F_ADD_SEALS, SEALS)
            _append(joined, piece)
            os.close(piece)
            piece = None
        fcntl.fcntl(joined, fcntl.F_ADD_SEALS, SEALS)
        output = new_memfd("assembly")
        pcm = fresh_ro(joined)
        step("encode", [*common, "-f", "s16le", "-ar", "48000", "-ac", "2", "-fd", str(pcm), "-i", "fd:",
                        *EncodeFlac().output_args(), "-f", "flac", "-fd", str(output), "fd:"], (pcm, output))
        close_opened()
        if os.fstat(output).st_size == 0:
            raise RenderError("render_failed", "the encoder produced no output", stage="encode")
        fcntl.fcntl(output, fcntl.F_ADD_SEALS, SEALS)
        rendered = Rendered(fd=output, sha256=fd_sha256(output), size=os.fstat(output).st_size, digest=digest,
                            provenance=document, measured_input={}, recipe_key=recipe.key,
                            recipe_version=recipe.version)
        output = None                                    # ownership passes to the caller
        return rendered
    except OSError as exc:
        if exc.errno in _RESOURCE_ERRNOS:
            raise RenderError("render_resource_limit", "no room for the assembly") from None
        raise
    finally:
        for fd in (*opened, *snapshots, piece, joined, output):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
