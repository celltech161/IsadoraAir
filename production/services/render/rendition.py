"""THE rendition intake: the only way a ``rendition`` ProductionMedia is made.

It accepts only an ExecutorOutput the render client issued (identity-sealed,
single use: a hand-built one, or one already used, is refused) -- i.e. a sealed
memfd the isadoraair-render executor returned over its authenticated socket --
rendered from THIS source, carrying the executor's identity. It streams the
SAME open descriptor (never a path) through the ordinary intake, which hashes
exactly the bytes it stores and has them judged by the validation service
(ffprobe, full decode, GStreamer engine parity); an invalid rendition is
refused and stored nowhere. Then, inside intake's insertion transaction, after
the source is locked for binding:

1. the COMPLETE expected row is derived -- every bound column from the
   executor output (parent, recipe key/version, provenance digest, toolchain,
   SHA-256, size) and ordinary intake semantics (identity, storage key,
   custody, display metadata), every validation fact at its canonical unset
   value (production.models.RENDITION_*);
2. production.models.rendition_authority(row) is opened -- in that
   transaction -- and insert_rendition() writes exactly that row without
   model signals and verifies the complete persisted row (any difference:
   refused, the transaction rolls back, the stored bytes are removed);
3. the rendition now exists UNVALIDATED; the validation service's verdict is
   recorded by the ordinary one-time transition (production.transitions.
   record_verdict), or, if the validator could not judge it, the
   infrastructure code (record_infrastructure_attempt) -- the authority itself
   never writes a validation fact.

A rendition the validator could not judge stays an immutable, UNVALIDATED,
unbindable orphan (rendition_unvalidated, retryable) until
validation.validate_media() judges it. A rendition whose provenance later
turns out stale (configuration or a bed changed during the render) stays an
immutable, unbindable orphan: completion refuses it (render_outdated) and
ordinary retention may purge it later.
"""
from __future__ import annotations

import json
import os

from django.utils import timezone

from ... import transitions
from ...errors import ImmutableMediaError, IntakeError, MediaRejected
from ...models import RENDITION_BOUND_FIELDS, ProductionMedia, insert_rendition, rendition_authority
from ...policy import MediaPolicy
from .. import intake, validation
from . import client, protocol
from .engine import RenderError, RenderResult


def _expected_row(columns: dict, output) -> dict:
    """The complete bound row: intake's columns, each executor-bound one
    checked against the executor output itself."""
    parent = columns["derived_from"]
    owner = columns["owner"]
    row = {
        "id": columns["id"], "kind": ProductionMedia.KIND_RENDITION, "owner_id": getattr(owner, "pk", None),
        "owner_username": owner.get_username()[:150] if owner is not None else "",
        "storage_key": columns["storage_key"], "sha256": columns["sha256"], "byte_size": columns["byte_size"],
        "original_filename": columns["original_filename"], "declared_content_type": columns["declared_content_type"],
        "derived_from_id": getattr(parent, "pk", None), "recipe_key": columns["recipe_key"],
        "recipe_version": columns["recipe_version"], "recipe_params_digest": columns["recipe_params_digest"],
        "toolchain": columns["toolchain"],
    }
    assert set(row) == RENDITION_BOUND_FIELDS
    if ((str(row["derived_from_id"]), row["recipe_key"], row["recipe_version"], row["recipe_params_digest"],
         row["toolchain"], row["sha256"], row["byte_size"]) != (
            output.source_id, output.recipe_key, output.recipe_version, output.digest, output.toolchain,
            output.sha256, output.size)):
        raise RenderError("rendition_authority", "the stored rendition is not the executor's output")
    return row


def _inserter(output):
    def insert(columns, outcome):
        with rendition_authority(_expected_row(columns, output)):
            media = insert_rendition()
        if outcome is not None and outcome.status in (validation.STATUS_VALID, validation.STATUS_INVALID):
            recorded = transitions.record_verdict(media.pk, validation.verdict_columns(outcome, timezone.now()))
        else:
            recorded = transitions.record_infrastructure_attempt(
                media.pk, outcome.code if outcome is not None else "validation_unavailable")
        if recorded != 1:
            raise RenderError("rendition_authority", "the rendition's validation could not be recorded")
        return ProductionMedia.objects.get(pk=media.pk)
    return insert


def ingest(output, source, *, owner=None, policy: MediaPolicy | None = None) -> RenderResult:
    """Turn one executor output into a VALID rendition of ``source``. Raises
    RenderError: rendition_authority, rendition_invalid, rendition_unvalidated
    (retryable; the stored row is unbindable), or an intake code."""
    client.consume(output)                        # refuses anything the client did not issue, and reuse
    try:
        if str(getattr(source, "pk", source)) != output.source_id:
            raise RenderError("rendition_authority", "the output was rendered from another source")
        if not output.toolchain.endswith(protocol.EXECUTOR_MARKER):
            raise RenderError("rendition_authority", "the output does not carry the executor's identity")
        handle = os.fdopen(os.dup(output.fd), "rb")
        try:
            handle.seek(0)
            provenance = json.loads(output.provenance_json)
            result = intake._ingest(
                handle, kind=ProductionMedia.KIND_RENDITION, derived_from=source, recipe_key=output.recipe_key,
                recipe_version=output.recipe_version, recipe_params=provenance, toolchain=output.toolchain,
                owner=owner, validate=True, policy=policy or MediaPolicy(require_engine_decode=True),
                rendition_insert=_inserter(output))
        finally:
            handle.close()
    except MediaRejected as exc:
        raise RenderError("rendition_invalid", f"the rendition failed validation ({exc.code})",
                          stage="validate") from exc
    except IntakeError as exc:
        raise RenderError(exc.code or "rendition_intake_failed", str(exc), stage="validate") from exc
    except ImmutableMediaError as exc:                # the exact-row authority refused: rolled back
        raise RenderError("rendition_authority", str(exc), stage="validate") from exc
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
