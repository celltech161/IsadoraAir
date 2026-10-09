"""The render executor core: ONE recipe render over descriptors only.

Runs inside isadoraair-render (production.services.render_service), never in
the web process or a worker. It receives already-open, READ-ONLY descriptors
for the source and each pinned reference (beds) and:

1. proves every descriptor is a regular file, opened read-only, of the claimed
   size, whose bytes hash to the claimed SHA-256 (a substituted descriptor is
   refused before anything runs);
2. resolves the recipe from the code registry (the version must be the
   registered one), validates the parameters against its schema, checks the
   pinned references are exactly the ones the plan uses, and refuses unless
   its own canonical provenance digest equals the client's expectation;
3. runs the FIXED steps -- prepare, mix, loudness analysis, encode -- each a
   ffmpeg invocation built here from the step library, through ``run_step``
   (in the service: confinement.execute in the render subtree with the
   socket-denying seccomp profile). Every input is a fresh read-only reopen of
   a descriptor (``-fd N -i fd:``), every output a memfd created here
   (``-fd N fd:``), and ``-protocol_whitelist fd`` refuses every other ffmpeg
   protocol (file:, http:, ...). The tool tree therefore needs, and has, no
   filesystem write authority at all;
4. seals the FLAC memfd (no further write, grow or shrink, ever) and hashes it.

The caller owns the returned descriptor. Nothing here touches the database,
the media store or any path a client chose.
"""
from __future__ import annotations

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

SEALS = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
_HASH_CHUNK = 1024 * 1024
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


def verify_input(fd: int, entry: dict) -> None:
    """A received descriptor must be exactly the claimed bytes, read-only."""
    try:
        info = os.fstat(fd)
        mode = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
    except OSError:
        raise RenderError("descriptor_invalid", "a descriptor cannot be inspected") from None
    if not stat.S_ISREG(info.st_mode) or mode != os.O_RDONLY:
        raise RenderError("descriptor_invalid", "inputs are regular files opened read-only")
    if info.st_size != entry["bytes"] or fd_sha256(fd) != entry["sha256"]:
        raise RenderError("descriptor_mismatch", "a descriptor does not hold the claimed bytes")


def reopen_read_only(fd: int) -> int:
    """A fresh, read-only description of ``fd``'s file at offset 0."""
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
            max_seconds: Decimal) -> Rendered:
    """Render one shape-checked request. ``reference_fds``: {param: fd}.
    ``run_step(argv, pass_fds, timeout_seconds) -> confinement result``.
    ``deadline``: monotonic time by which everything must be done."""
    recipe, resolved, plan, pins, digest, document = plan_request(request)
    if request["source"]["duration_seconds"] > max_seconds:
        raise RenderError("source_too_long", "the source is longer than this service renders")
    verify_input(source_fd, request["source"])
    for name, fd in reference_fds.items():
        verify_input(fd, request["references"][name])

    opened = []

    def fresh_ro(fd):
        handle = reopen_read_only(fd)
        opened.append(handle)
        return handle

    def step(stage, argv, pass_fds):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RenderError("render_timeout", "the render exceeded its time limit", retryable=True, stage=stage)
        result = run_step(argv, tuple(pass_fds), remaining)
        _status(result, stage)
        return result

    common = [ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-y", "-protocol_whitelist", "fd"]
    voice = new_memfd("voice")
    mixed = new_memfd("mix") if plan.bed is not None else None
    output = new_memfd("rendition")
    try:
        # 1. the voice, 48 kHz stereo 16-bit
        source = fresh_ro(source_fd)
        argv = [*common, "-loglevel", "error", "-fd", str(source), "-i", "fd:", "-map", "0:a:0", "-vn", "-sn",
                "-dn", "-ac", "2", "-ar", "48000"]
        if plan.voice:
            argv += ["-af", plan.voice_filter()]
        step("prepare", [*argv, "-c:a", "pcm_s16le", "-f", "wav", "-fd", str(voice), "fd:"], (source, voice))
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
            bed, voice_in = fresh_ro(reference_fds[plan.bed.reference_param]), fresh_ro(voice)
            argv = [*common, "-loglevel", "error"]
            if plan.bed.loops:
                argv += ["-stream_loop", "-1"]
            argv += ["-fd", str(bed), "-i", "fd:", "-fd", str(voice_in), "-i", "fd:", "-filter_complex", graph,
                     "-map", "[mix]", "-c:a", "pcm_s16le", "-f", "wav", "-fd", str(mixed), "fd:"]
            step("mix", argv, (bed, voice_in, mixed))
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
    finally:
        for fd in (*opened, voice, mixed, output):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
