"""Streaming, crash-safe intake of production media.

Ordering is the whole crash-safety argument -- bytes become permanent BEFORE
the row exists, so a committed row can never point at missing data:

    1. stream the source into incoming/<32hex>.part (0600), hashing and
       counting as it goes; a hard byte cap aborts the stream; fsync;
    2. (optional) validate the .part file -- the verdict, or a refusal, is known
       before anything becomes permanent;
    3. promote with link(2) then unlink: atomic, same filesystem, and it can
       NEVER overwrite an existing permanent file (EEXIST is an error);
       permanent bytes are mode 0440;
    4. commit the ProductionMedia row (its storage key is system generated).

A crash after 1 leaves a stale .part and no row; after 3 an orphan permanent
file and no row; after 4 a complete, consistent row. Orphans and stale parts
are reclaimed by production.services.reconcile. Whole files are never buffered
in memory (one CHUNK_SIZE at a time) and nothing is staged in /tmp.

Callers should commit promptly: rows created inside a caller's still-open
transaction are, to the orphan sweeper, indistinguishable from "no row yet" for
the (24 h minimum) grace period.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from .. import policy as policy_mod
from ..errors import IntakeError, MediaRejected
from ..models import ProductionMedia
from . import layout, validation

CHUNK_SIZE = 1024 * 1024
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class IngestResult:
    media: ProductionMedia
    # The validation outcome when validation ran at intake (None when deferred).
    # An infrastructure outcome means: stored, still unvalidated, retry later.
    outcome: validation.ValidationOutcome | None


def digest_recipe_params(params) -> str:
    """Canonical SHA-256 of a recipe's parameters (provenance only)."""
    try:
        canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise IntakeError("recipe parameters must be plain JSON", code="invalid_recipe") from exc
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _display_name(name) -> str:
    """Display-only: basename of whatever the client sent, control characters
    removed, bounded. Never used as, or inside, a filesystem path."""
    text = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    return _CONTROL_RE.sub("", text).strip()[:255]


def _display_type(value) -> str:
    return _CONTROL_RE.sub("", str(value or "")).strip()[:127]


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass            # best effort: the reconciler owns whatever remains


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _stream_to_part(source, part: Path, max_bytes: int):
    read = getattr(source, "read", None)
    if not callable(read):
        raise IntakeError("source must be a readable file-like object", code="invalid_source")
    digest = hashlib.sha256()
    total = 0
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, layout.PART_MODE)
    try:
        while True:
            try:
                chunk = read(CHUNK_SIZE)
            except Exception as exc:                       # client went away, etc.
                raise IntakeError("reading the upload failed", code="source_read_failed") from exc
            if not chunk:
                break
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise IntakeError("source returned non-bytes data", code="invalid_source")
            total += len(chunk)
            if total > max_bytes:
                raise MediaRejected("too_large", "upload exceeds the byte limit")
            digest.update(chunk)
            try:
                _write_all(fd, chunk)
            except OSError as exc:
                raise IntakeError("could not write the upload", code="storage_write_failed") from exc
        if total == 0:
            raise MediaRejected("empty", "upload contained no data")
        os.fsync(fd)
        os.fchmod(fd, layout.MEDIA_FILE_MODE)
    except Exception:
        os.close(fd)
        _unlink_quiet(part)
        raise
    os.close(fd)
    return digest.hexdigest(), total


def _promote(part: Path, destination: Path) -> None:
    layout.ensure_dir(destination.parent)
    try:
        os.link(part, destination)            # atomic; never overwrites
    except FileExistsError as exc:
        raise IntakeError("permanent storage identity already exists", code="storage_collision") from exc
    except OSError as exc:
        raise IntakeError("could not promote the upload", code="promotion_failed") from exc
    try:
        os.unlink(part)
        layout.fsync_directory(destination.parent)
    except OSError as exc:
        _unlink_quiet(destination)
        raise IntakeError("could not finalize the upload", code="promotion_failed") from exc


def _check_request(kind, owner, derived_from, recipe_key, recipe_version, recipe_params):
    if kind not in dict(ProductionMedia.KIND_CHOICES):
        raise IntakeError("unknown media kind", code="invalid_kind")
    if owner is not None and getattr(owner, "pk", None) is None:
        raise IntakeError("owner must be a saved user or None", code="invalid_owner")
    derived = kind in ProductionMedia.DERIVED_KINDS
    if derived != (derived_from is not None):
        raise IntakeError(
            "edit/rendition media must name the media it derives from, and nothing else may",
            code="invalid_derivation",
        )
    if not recipe_key and (recipe_version is not None or recipe_params is not None):
        raise IntakeError("recipe version/parameters require a recipe key", code="invalid_recipe")
    if recipe_version is not None and (not isinstance(recipe_version, int) or isinstance(recipe_version, bool)
                                       or recipe_version < 0):
        raise IntakeError("recipe version must be a non-negative integer", code="invalid_recipe")
    parent = None
    if derived:
        parent = ProductionMedia.objects.filter(pk=getattr(derived_from, "pk", None)).first()
        if parent is None or not parent.is_present or not parent.is_valid:
            raise IntakeError("the parent media is missing, purged or not valid", code="parent_not_usable")
    return parent


def ingest_stream(
    source, *, kind, owner=None, original_filename="", declared_content_type="",
    policy=None, validate=True, retain_invalid=False, derived_from=None,
    recipe_key="", recipe_version=None, recipe_params=None, toolchain="",
) -> IngestResult:
    """Store ``source`` (anything with ``read(n)``) as a new immutable
    ProductionMedia.

    * ``policy`` (production.policy.MediaPolicy) is the consuming domain's
      bounds. A byte-cap or duration violation, and -- unless ``retain_invalid``
      -- an invalid verdict, raise MediaRejected and create NOTHING.
    * ``validate=False`` defers validation: the row is created ``unvalidated``
      and validate_media() can be called later.
    * An infrastructure failure during validation never rejects the media: it
      is stored ``unvalidated`` and the returned outcome says why.
    """
    media_policy = policy if policy is not None else policy_mod.DEFAULT_POLICY
    recipe_digest = ""
    if recipe_params is not None:
        recipe_digest = digest_recipe_params(recipe_params)
    parent = _check_request(kind, owner, derived_from, recipe_key, recipe_version, recipe_params)

    layout.ensure_layout()
    part = layout.new_part_path()
    sha256, size = _stream_to_part(source, part, media_policy.max_bytes)

    promoted = None
    try:
        outcome = None
        if validate:
            outcome = validation._analyze_path(part, require_engine_decode=media_policy.require_engine_decode)
            if outcome.status == validation.STATUS_INVALID and not retain_invalid:
                raise MediaRejected(outcome.code, "the media failed validation", outcome=outcome)
            if outcome.is_valid:
                code = policy_mod.policy_duration_code(outcome.facts["decoded_duration_seconds"], media_policy)
                if code is not None:
                    raise MediaRejected(code, "the media does not fit the requested policy", outcome=outcome)

        media_id = layout.new_media_id()
        storage_key = layout.storage_key_for(media_id)
        destination = layout.resolve_storage_path(storage_key)
        _promote(part, destination)
        promoted = destination

        columns = dict(
            id=media_id, kind=kind, owner=owner, storage_key=storage_key, sha256=sha256, byte_size=size,
            original_filename=_display_name(original_filename),
            declared_content_type=_display_type(declared_content_type),
            derived_from=parent, recipe_key=recipe_key[:64], recipe_version=recipe_version,
            recipe_params_digest=recipe_digest, toolchain=toolchain[:200],
        )
        if outcome is not None:
            if outcome.status in (validation.STATUS_VALID, validation.STATUS_INVALID):
                columns.update(validation.verdict_columns(outcome, timezone.now()))
            else:
                columns["validation_code"] = outcome.code
        with transaction.atomic():
            media = ProductionMedia.objects.create(**columns)
    except Exception:
        # Not a hard kill: tidy up. (A hard kill is covered by the reconciler.)
        _unlink_quiet(part)
        if promoted is not None:
            _unlink_quiet(promoted)
        raise
    return IngestResult(media=media, outcome=outcome)


def ingest_derivative(parent, source, *, kind=ProductionMedia.KIND_EDIT, recipe_key="", recipe_version=None,
                      recipe_params=None, toolchain="", **options) -> IngestResult:
    """Store ``source`` as a NEW media derived from ``parent``. The parent's
    bytes and row are never touched; the derivative gets its own UUID, storage
    key and hash."""
    return ingest_stream(
        source, kind=kind, derived_from=parent, recipe_key=recipe_key, recipe_version=recipe_version,
        recipe_params=recipe_params, toolchain=toolchain, **options,
    )
