"""Render a ProductionMedia through a registered recipe into a NEW rendition.

    result = render(source_media, "news_item", {"bed_gain_db": 6})

Rendering itself never happens in this process. ``render()`` is the client
side of a separate, sandboxed service:

1. render.client opens the source and every pinned reference (ReferenceAudio,
   hash-verified) as read-only descriptors, computes the canonical provenance
   it expects and asks isadoraair-render (production.services.render_service)
   over its authenticated Unix socket;
2. the service verifies every descriptor's bytes, the recipe, its version, the
   parameters and the provenance, runs the fixed steps confined (its own
   cgroup subtree, Landlock with no filesystem writes, seccomp, rlimits, no
   network) and returns the FLAC as a SEALED memfd;
3. render.rendition -- the sole rendition-creation authority -- streams that
   same descriptor through the ordinary derivative intake (validated by the
   validation service, GStreamer engine parity included) into an immutable
   ``rendition`` with ``derived_from``, recipe key/version, the canonical
   provenance digest and the executor's toolchain identity.

Any failure raises RenderError and leaves no bindable rendition: an invalid
rendition is refused by intake (no row); a validation-infrastructure failure
leaves an ``unvalidated`` row nothing can bind. The source is never touched; a
retry produces another rendition and earlier ones remain.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass

from ...models import ProductionMedia
from ...policy import MediaPolicy
from .provenance import ENGINE_VERSION, RenderEvidence

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


def render(source, recipe_key, recipe_params=None, *, owner=None, timeout_seconds=None,
           policy: MediaPolicy | None = None, item=None, lease=None) -> RenderResult:
    """Render ``source`` through the isadoraair-render service and ingest the
    result as a validated rendition (render.client + render.rendition)."""
    from . import client, rendition
    output = client.request_render(source, recipe_key, recipe_params, item=item, lease=lease,
                                   timeout_seconds=timeout_seconds)
    try:
        source_row = ProductionMedia.objects.get(pk=output.source_id)
    except ProductionMedia.DoesNotExist:
        output.close()
        raise RenderError("source_not_usable", "the source media no longer exists") from None
    return rendition.ingest(output, source_row, owner=owner, policy=policy)
