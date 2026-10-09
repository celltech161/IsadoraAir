"""THE rendition intake: the only way a ``rendition`` ProductionMedia is made.

It accepts only an ExecutorOutput the render client issued (identity-sealed,
single use: a hand-built one, or one already used, is refused) -- i.e. a sealed
memfd the isadoraair-render executor returned over its authenticated socket.
Under ``production.models.rendition_authority`` -- naming the exact row: the
source it derives from, the recipe key and version, the provenance digest, the
executor's toolchain identity and the output's SHA-256 -- it streams the SAME
open descriptor (never a path) through the ordinary derivative intake, which
hashes exactly the bytes it stores, validates them in the validation service
(ffprobe, full decode, GStreamer engine parity) and inserts the immutable row.
The model refuses any rendition insert that is not exactly this one.

A rendition whose provenance later turns out stale (configuration or a bed
changed during the render) stays an immutable, unbindable orphan: completion
refuses it (render_outdated) and ordinary retention may purge it later.
"""
from __future__ import annotations

import json
import os

from ...errors import IntakeError, MediaRejected
from ...models import ProductionMedia, rendition_authority
from ...policy import MediaPolicy
from .. import intake
from . import client
from .engine import RenderError, RenderResult


def ingest(output, source, *, owner=None, policy: MediaPolicy | None = None) -> RenderResult:
    """Turn one executor output into a VALID rendition of ``source``. Raises
    RenderError: rendition_authority, rendition_invalid, rendition_unvalidated
    (retryable; the stored row is unbindable), or an intake code."""
    client.consume(output)                        # refuses anything the client did not issue, and reuse
    try:
        if str(getattr(source, "pk", source)) != output.source_id:
            raise RenderError("rendition_authority", "the output was rendered from another source")
        handle = os.fdopen(os.dup(output.fd), "rb")
        try:
            handle.seek(0)
            provenance = json.loads(output.provenance_json)
            with rendition_authority(derived_from_id=output.source_id, recipe_key=output.recipe_key,
                                     recipe_version=output.recipe_version, recipe_params_digest=output.digest,
                                     toolchain=output.toolchain, sha256=output.sha256):
                result = intake.ingest_derivative(
                    source, handle, kind=ProductionMedia.KIND_RENDITION, recipe_key=output.recipe_key,
                    recipe_version=output.recipe_version, recipe_params=provenance, toolchain=output.toolchain,
                    owner=owner, validate=True, policy=policy or MediaPolicy(require_engine_decode=True))
        finally:
            handle.close()
    except MediaRejected as exc:
        raise RenderError("rendition_invalid", f"the rendition failed validation ({exc.code})",
                          stage="validate") from exc
    except IntakeError as exc:
        raise RenderError(exc.code or "rendition_intake_failed", str(exc), stage="validate") from exc
    finally:
        output.close()
    media = result.media
    if media.sha256 != output.sha256 or media.recipe_params_digest != output.digest:
        raise RenderError("rendition_authority", "the stored rendition is not the executor's output")
    if not media.is_valid:
        raise RenderError("rendition_unvalidated", "the rendition could not be validated yet; it is stored but "
                          "not usable", retryable=True, stage="validate", media=media)
    return RenderResult(media=media, recipe_key=output.recipe_key, recipe_version=output.recipe_version,
                        params=json.loads(output.provenance_json)["params"],
                        provenance=json.loads(output.provenance_json), params_digest=output.digest,
                        toolchain=output.toolchain, measured_input=output.measured_input)
