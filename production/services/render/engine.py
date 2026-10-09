"""Render a ProductionMedia through a registered recipe into a NEW rendition.

    result = render(source_media, "news_item", {"bed_gain_db": 6})

1. the recipe is looked up in the code registry and its parameters validated
   against the recipe's declared schema (defaults filled in);
2. the source must be present and valid (it passed the sandboxed validation
   service -- ffprobe, full decode, GStreamer); reference audio (beds) is
   resolved by KEY through the ReferenceAudio registry and its bytes are
   proven to hash to the pinned SHA-256;
3. in a private scratch directory under ``work/``, the fixed steps run as
   bounded tools (production.services.render.runner): prepare the voice
   (48 kHz stereo), mix the bed, measure loudness (structured JSON), then apply
   two-pass loudness normalization and encode FLAC;
4. the FLAC is ingested through the ordinary rendition path
   (``intake.ingest_derivative`` with ``kind=rendition``): it is validated by
   the validation service -- including the engine's own GStreamer decode --
   and only a ``valid`` result becomes a usable rendition, with
   ``derived_from`` = the source, the recipe key/version, the canonical
   provenance digest (production.services.render.provenance -- the SAME
   helper a consumer uses to verify a rendition at completion) and the
   toolchain identity.

Any failure raises RenderError and leaves no bindable rendition: nothing is
ingested until every step succeeded, an invalid rendition is refused by
intake (no row), and a validation-infrastructure failure leaves an
``unvalidated`` row that nothing can bind (``lock_for_binding`` requires
``valid``). The scratch directory is always removed. The source is never
touched; a retry produces another rendition and earlier ones remain.
"""
from __future__ import annotations

import json
import math
import shutil
import wave
from dataclasses import dataclass
from functools import lru_cache

from ...errors import IntakeError, MediaInconsistent, MediaNotValidated, MediaPurged, MediaRejected
from ...models import ProductionMedia
from ...policy import MediaPolicy
from .. import intake, layout, media_io, reference_audio, validation
from . import params as params_mod
from . import provenance as provenance_mod
from . import registry, runner
from .provenance import ENGINE_VERSION, RenderEvidence
_DETAIL_LIMIT = 500
_LOUDNORM_KEYS = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")


class RenderError(Exception):
    """The render did not produce a usable rendition. ``code`` is stable;
    ``retryable`` means the inputs are fine and a later attempt may succeed."""

    def __init__(self, code: str, message: str, *, retryable: bool = False, stage: str = "", media=None):
        super().__init__(message)
        self.code, self.retryable, self.stage, self.media = code, retryable, stage, media


@dataclass(frozen=True)
class RenderResult:
    media: ProductionMedia            # the new, VALID rendition
    recipe_key: str
    recipe_version: int
    params: dict                      # resolved (defaults included)
    provenance: dict                  # what the digest covers: recipe, params, exact reference bytes
    params_digest: str
    toolchain: str
    measured_input: dict              # first-pass loudness of the pre-normalization mix

    @property
    def evidence(self) -> RenderEvidence:
        """The primitive claim a consumer checks (never trusts) at completion."""
        return RenderEvidence(rendition_id=str(self.media.pk), source_id=str(self.media.derived_from_id),
                              recipe_key=self.recipe_key, recipe_version=self.recipe_version,
                              provenance_digest=self.params_digest)


@lru_cache(maxsize=4)
def _toolchain(ffmpeg: str) -> str:
    result = runner.run([ffmpeg, "-hide_banner", "-version"], timeout_seconds=20)
    first = result.stdout.decode("utf-8", "replace").splitlines()[:1] if result.ok else []
    version = first[0].strip() if first else "ffmpeg (version unknown)"
    return f"{version}; render-engine {ENGINE_VERSION}"[:200]


def _fail(code, result, stage):
    if result.status == "timeout":
        raise RenderError("render_timeout", f"the {stage} step exceeded its time limit", retryable=True, stage=stage)
    if result.status != "ok":
        raise RenderError("render_unavailable", f"the {stage} step could not run ({result.status})",
                          retryable=True, stage=stage)
    tail = result.stderr.decode("utf-8", "replace")[-_DETAIL_LIMIT:]
    raise RenderError(code, f"the {stage} step failed (exit {result.returncode}): {tail}", stage=stage)


def _run(argv, *, timeout, stage, pass_fds=(), cwd=None):
    result = runner.run(argv, timeout_seconds=timeout, pass_fds=pass_fds, cwd=cwd)
    if not result.ok:
        _fail("render_failed", result, stage)
    return result


def _wav_seconds(path) -> float:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        return handle.getnframes() / rate if rate else 0.0


def parse_loudnorm(stderr: bytes) -> dict:
    """The LAST JSON object ffmpeg printed, parsed structurally; every
    required measurement must be a finite number."""
    text = stderr.decode("utf-8", "replace")
    end = text.rfind("}")
    start = text.rfind("{", 0, end + 1) if end >= 0 else -1
    if start < 0 or end < start:
        raise RenderError("loudness_analysis_failed", "no loudness measurement in the analysis output",
                          stage="loudnorm_analysis")
    try:
        data = json.loads(text[start:end + 1])
    except ValueError as exc:
        raise RenderError("loudness_analysis_failed", "the loudness measurement is not JSON",
                          stage="loudnorm_analysis") from exc
    if not isinstance(data, dict):
        raise RenderError("loudness_analysis_failed", "the loudness measurement is not an object",
                          stage="loudnorm_analysis")
    measured = {}
    for key in _LOUDNORM_KEYS:
        raw = data.get(key)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise RenderError("loudness_analysis_failed", f"loudness field {key} is missing or not a number",
                              stage="loudnorm_analysis") from None
        if not math.isfinite(value):
            raise RenderError("silent_render", "the audio is silent: loudness cannot be normalized",
                              stage="loudnorm_analysis")
        measured[key] = value
    return measured


def _timeout_for(source: ProductionMedia, override) -> float:
    if override is not None:
        return float(override)
    seconds = float(source.decoded_duration_seconds or 0)
    return min(1800.0, 60.0 + 4.0 * seconds)


def render(source, recipe_key, recipe_params=None, *, owner=None, timeout_seconds=None,
           policy: MediaPolicy | None = None) -> RenderResult:
    try:
        recipe, resolved, plan = provenance_mod.prepare(recipe_key, recipe_params)
    except registry.UnknownRecipe as exc:
        raise RenderError("unknown_recipe", str(exc)) from exc
    except params_mod.RecipeParameterError as exc:
        raise RenderError(exc.code, str(exc)) from exc

    ffmpeg = runner.resolve_tool(validation.FFMPEG)
    if ffmpeg is None or runner.resolve_tool("prlimit") is None:
        raise RenderError("render_unavailable", "ffmpeg/prlimit are not available", retryable=True)

    try:
        opened = media_io.open_media(source, require_valid=True)
    except (MediaPurged, MediaNotValidated, MediaInconsistent) as exc:
        raise RenderError("source_not_usable", "the source media is missing, purged or not valid") from exc
    source_row = opened.media
    bed = None
    work = None
    try:
        if plan.bed is not None:
            try:
                key = provenance_mod.reference_keys(plan, resolved)[plan.bed.reference_param]
                bed = reference_audio.resolve(key)
            except reference_audio.ReferenceAudioError as exc:
                raise RenderError(exc.code, str(exc)) from exc
        timeout = _timeout_for(source_row, timeout_seconds)
        toolchain = _toolchain(ffmpeg)
        work = layout.create_work_dir()
        common = [ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-y"]

        # 1. the voice, 48 kHz stereo 16-bit
        src_fd = opened.file.fileno()
        voice = work / "voice.wav"
        argv = [*common, "-loglevel", "error", "-i", f"/proc/self/fd/{src_fd}", "-map", "0:a:0", "-vn", "-sn",
                "-dn", "-ac", "2", "-ar", "48000"]
        if plan.voice:
            argv += ["-af", plan.voice_filter()]
        _run([*argv, "-c:a", "pcm_s16le", str(voice)], timeout=timeout, stage="prepare", pass_fds=(src_fd,),
             cwd=work)
        voice_seconds = _wav_seconds(voice)
        if voice_seconds <= 0:
            raise RenderError("empty_voice", "no voice audio remained after preparation", stage="prepare")

        # 2. the bed underlay
        master = voice
        if plan.bed is not None:
            bed_fd = bed.file.fileno()
            delay = plan.delay.ms / 1000.0 if plan.delay is not None else 0.0
            graph = plan.bed.filter_complex(
                voice_end=voice_seconds + delay, bed_duration=float(bed.media.decoded_duration_seconds),
                delay=plan.delay,
            )
            master = work / "mixed.wav"
            argv = [*common, "-loglevel", "error"]
            if plan.bed.loops:
                argv += ["-stream_loop", "-1"]
            argv += ["-i", f"/proc/self/fd/{bed_fd}", "-i", str(voice), "-filter_complex", graph,
                     "-map", "[mix]", "-c:a", "pcm_s16le", str(master)]
            _run(argv, timeout=timeout, stage="mix", pass_fds=(bed_fd,), cwd=work)

        # 3. two-pass loudness normalization + FLAC
        result = _run([*common, "-loglevel", "info", "-i", str(master), "-af", plan.loudnorm.analysis_filter(),
                       "-f", "null", "-"], timeout=timeout, stage="loudnorm_analysis", cwd=work)
        measured = parse_loudnorm(result.stderr)
        rendition = work / "rendition.flac"
        _run([*common, "-loglevel", "error", "-i", str(master), "-af", plan.loudnorm.apply_filter(measured),
              *plan.encode.output_args(), str(rendition)], timeout=timeout, stage="encode", cwd=work)

        # 4. provenance + the ordinary validated rendition intake
        provenance = provenance_mod.document(
            recipe, resolved, {plan.bed.reference_param: bed.provenance()} if bed is not None else {})
        digest = provenance_mod.digest(provenance)
        try:
            with open(rendition, "rb") as handle:
                ingested = intake.ingest_derivative(
                    source_row, handle, kind=ProductionMedia.KIND_RENDITION, recipe_key=recipe.key,
                    recipe_version=recipe.version, recipe_params=provenance, toolchain=toolchain, owner=owner,
                    validate=True, policy=policy or MediaPolicy(require_engine_decode=True),
                )
        except MediaRejected as exc:
            raise RenderError("rendition_invalid", f"the rendition failed validation ({exc.code})",
                              stage="validate") from exc
        except IntakeError as exc:
            raise RenderError(exc.code or "rendition_intake_failed", str(exc), stage="validate") from exc
        if not ingested.media.is_valid:
            raise RenderError("rendition_unvalidated",
                              "the rendition could not be validated yet; it is stored but not usable",
                              retryable=True, stage="validate", media=ingested.media)
        return RenderResult(media=ingested.media, recipe_key=recipe.key, recipe_version=recipe.version,
                            params=resolved, provenance=provenance, params_digest=digest, toolchain=toolchain,
                            measured_input=measured)
    finally:
        opened.file.close()
        if bed is not None:
            bed.close()
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
