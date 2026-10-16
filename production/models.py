"""iPortal shared production-media substrate: ``ProductionMedia``.

What is common across iPortal workflows is the media-production substrate,
NOT a workflow lifecycle. A ProductionMedia row is one immutable audio object
-- its bytes, its authoritative technical facts, where it came from and
whether its bytes still exist. It deliberately says nothing about *why* a
recording exists or what happens to it on air: submission, approval, delivery,
recall, as-run evidence, active-message playback and the like belong to the
domains that reference it (Voice Tracking, spoken/produced content,
urgent/public-address), each through its own PROTECT foreign key. There is no
generic foreign key, no job, no assignment and no workflow state here.

Immutability contract -- the application trust boundary. Every ordinary
Django write path fails closed; privileged raw SQL is outside the boundary.
There is deliberately no database trigger (that would need a manual migration).

* A row is created once (``objects.create()`` / an instance ``save()`` while
  adding). After that NO generic ORM path may change it:
  - instance ``save()`` compares every loaded field against the AUTHORITATIVE
    row re-read from the database (so a deferred/``only()`` instance cannot
    smuggle a change past it) and refuses any difference;
  - ``QuerySet.update()`` -- default manager, ``_base_manager`` (pointed at the
    same guarded queryset by ``Meta.base_manager_name``) and every reverse
    related manager -- refuses everything except the one Django-owned
    operation ``update(owner=None)``: the ``SET_NULL`` a User deletion performs
    (also ``user.production_media.clear()``). It only unlinks custody; the
    ``owner_username`` snapshot keeps it understandable, and reassigning custody
    to another user is refused;
  - ``bulk_update()`` and ``bulk_create(update_conflicts=True)`` refuse;
  - ``delete()`` on instances and on every queryset refuses: purge removes
    bytes, never rows.
* The only legitimate state changes are the explicit, state-qualified
  transitions in ``production.transitions`` (a validation verdict recorded once
  on an ``unvalidated`` row; an infrastructure-attempt note on an
  ``unvalidated`` row; ``present -> purged``). There is no ``purged -> present``.
* Editing audio means a NEW ProductionMedia with ``derived_from`` pointing at
  the original -- never an in-place overwrite.

NO-AIR provenance (r0113 / D3C). ``airing_prohibited`` is fixed at creation
like every other column. Intake sets it for media made under a TEST / NO AIR
recorder subject (the adapter decides from the server-resolved subject), and a
derivative can only keep or strengthen its parent's restriction: every insert
path refuses a child of a prohibited parent that is not itself prohibited
(``save()``, the guarded ``bulk_create``, and the rendition authority). The
canonical binding (``retention.lock_for_binding``) refuses prohibited media,
so no air-capable domain can reference it -- whether or not a TEST binding
still exists.
"""
from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from django.conf import settings
from django.db import models, transaction
from django.db.models import Q

from .errors import ImmutableMediaError, ReferenceAudioIntegrityError

SHA256_PATTERN = r"^[0-9a-f]{64}$"
# <2 hex shard>/<32 hex media id> -- relative to <root>/media/. Extensionless on
# purpose: no extension is chosen from untrusted metadata, and the permanent
# name never has to change when validation later establishes the container.
STORAGE_KEY_PATTERN = r"^[0-9a-f]{2}/[0-9a-f]{32}$"


# -- the sole rendition-creation authority (2.22C2A) ----------------------------------
#
# A ``rendition`` row is created ONLY by production.services.render.rendition,
# from a sealed output the isadoraair-render executor returned over its
# authenticated socket, and only as ONE COMPLETE, EXACT row:
#
#   begin the insertion transaction
#   -> derive the complete expected row (every concrete column)
#   -> open rendition_authority(row)       (refused outside a transaction)
#   -> insert_rendition()                  (in that same transaction block)
#   -> verify the complete persisted row   (any difference: refused, rolled back)
#   -> close the authority -> commit
#
# The insert is an exact write that emits no model signals (no pre_save
# receiver can widen the row), and the persisted row is still re-read and
# compared column by column. A rendition always starts UNVALIDATED with every
# validation fact unset: the authority is not a validation authority -- the
# verdict is recorded afterwards, once, only by production.transitions
# (record_verdict / record_infrastructure_attempt). ProductionMedia.save() and
# every bulk_create refuse renditions outright, and generic intake refuses
# kind=rendition before a byte is staged. Raw SQL is outside the boundary, as
# everywhere in this module.

# Every concrete ProductionMedia column, classified for rendition creation.
# (test_render_service proves this covers the model exactly: a new column
# must be classified before any rendition can be inserted.)
#
# BOUND -- exact values the rendition intake derives from the executor output
# and ordinary intake semantics (identity, custody, bytes, display metadata,
# derivation provenance, executor identity):
RENDITION_BOUND_FIELDS = frozenset({
    "id", "kind", "owner_id", "owner_username", "storage_key", "sha256", "byte_size", "original_filename",
    "declared_content_type", "derived_from_id", "recipe_key", "recipe_version", "recipe_params_digest",
    "toolchain", "airing_prohibited",
})
# INITIAL -- unknown until validation / retention: must hold exactly these
# canonical values at insert (validation alone establishes the facts):
RENDITION_INITIAL_VALUES = {
    "container": "", "codec": "", "sample_rate": None, "channels": None, "decoded_duration_seconds": None,
    "header_duration_seconds": None, "probe": {}, "validation_state": "unvalidated", "validation_code": "",
    "validated_at": None, "retention_state": "present", "purged_at": None,
}
# GENERATED -- set at insert; verified semantically (an aware timestamp taken
# while the authority was open).
RENDITION_GENERATED_FIELDS = frozenset({"created_at"})


def _rendition_refused(message):
    return ImmutableMediaError(message, code="rendition_authority")


class _RenditionGrant:
    __slots__ = ("expected", "connection", "block", "opened_at", "used", "closed")

    def __init__(self, expected, connection, block, opened_at):
        self.expected, self.connection, self.block, self.opened_at = expected, connection, block, opened_at
        self.used = self.closed = False


_rendition = contextvars.ContextVar("production_rendition_authority", default=None)


def _rendition_classification_complete() -> bool:
    concrete = {field.attname for field in ProductionMedia._meta.concrete_fields}
    classified = RENDITION_BOUND_FIELDS | set(RENDITION_INITIAL_VALUES) | RENDITION_GENERATED_FIELDS
    return concrete == classified and not (RENDITION_BOUND_FIELDS & set(RENDITION_INITIAL_VALUES))


@contextmanager
def rendition_authority(row: dict):
    """Authorize inserting exactly ONE rendition: the complete row ``row``
    ({attname: value} for every BOUND column) plus the canonical INITIAL
    values. Must be opened inside the transaction that consumes it, and is
    consumed (insert_rendition) in that same transaction block. Entered only
    by production.services.render.rendition."""
    from django.utils import timezone
    connection = transaction.get_connection()
    if not connection.in_atomic_block or not connection.atomic_blocks:
        raise _rendition_refused("a rendition authority is opened inside its insertion transaction")
    if _rendition.get() is not None:
        raise _rendition_refused("rendition authorities do not nest")
    if not _rendition_classification_complete():
        raise _rendition_refused("ProductionMedia has a column the rendition authority does not classify")
    if not isinstance(row, dict) or set(row) != RENDITION_BOUND_FIELDS:
        raise _rendition_refused("a rendition authority names every bound column, and only those")
    if row["kind"] != ProductionMedia.KIND_RENDITION or row["derived_from_id"] is None:
        raise _rendition_refused("a rendition authority names a rendition derived from its source")
    if not isinstance(row["airing_prohibited"], bool) or _launders_restriction(
            row["derived_from_id"], row["airing_prohibited"]):
        raise _rendition_refused("a rendition keeps its source's no-air restriction")
    expected = {**row, **RENDITION_INITIAL_VALUES}
    grant = _RenditionGrant(expected, connection, connection.atomic_blocks[-1], timezone.now())
    token = _rendition.set(grant)
    try:
        yield grant
    finally:
        grant.closed = True
        _rendition.reset(token)


def _launders_restriction(parent_id, airing_prohibited) -> bool:
    """Would a child of ``parent_id`` with this flag drop a NO-AIR restriction?
    The parent's flag is immutable, so an unlocked read is authoritative."""
    if parent_id is None or airing_prohibited is True:
        return False
    return ProductionMedia._base_manager.filter(pk=parent_id, airing_prohibited=True).exists()


def _refuse_laundering(objs) -> None:
    for obj in objs:
        if _launders_restriction(getattr(obj, "derived_from_id", None), getattr(obj, "airing_prohibited", False)):
            raise ImmutableMediaError("a derivative of no-air media is itself no-air", code="airing_prohibited")


def _same(field, mine, theirs) -> bool:
    try:
        return field.to_python(mine) == field.to_python(theirs)
    except Exception:                   # noqa: BLE001 -- an unparseable value is a difference
        return False


def insert_rendition() -> "ProductionMedia":
    """THE rendition insert: the open authority's exact row, once, in the
    transaction block that opened it; then the complete persisted row is
    verified. Any difference raises (the caller's transaction rolls back)."""
    from django.utils import timezone
    grant = _rendition.get()
    connection = transaction.get_connection()
    if grant is None or grant.used or grant.closed:
        raise _rendition_refused("renditions are created only by the render executor's intake")
    if (grant.connection is not connection or not connection.in_atomic_block or not connection.atomic_blocks
            or connection.atomic_blocks[-1] is not grant.block):
        raise _rendition_refused("a rendition authority is consumed in the transaction that opened it")
    grant.used = True
    fields = {field.attname: field for field in ProductionMedia._meta.concrete_fields}
    instance = ProductionMedia(**{name: grant.expected[name] for name in grant.expected})
    # The exact write: the plain QuerySet insert -- no pre_save/post_save, so no
    # receiver can see or change the row; created_at comes from auto_now_add.
    models.QuerySet.bulk_create(ProductionMedia._base_manager.all(), [instance])
    stored = ProductionMedia._base_manager.filter(pk=grant.expected["id"]).values(*fields).first()
    checked_at = timezone.now()
    if stored is None:
        raise _rendition_refused("the authorized rendition was not inserted")
    differing = sorted(name for name, value in grant.expected.items() if not _same(fields[name], value, stored[name]))
    created = stored["created_at"]
    if created is None or created.tzinfo is None or not grant.opened_at <= created <= checked_at:
        differing.append("created_at")
    if differing:
        raise _rendition_refused(f"the inserted rendition differs from the authorized one: {differing}")
    return ProductionMedia._base_manager.get(pk=grant.expected["id"])


class ProductionMediaQuerySet(models.QuerySet):
    """Fails closed on every generic bulk mutation (see the module docstring).

    Used as the default AND the base manager (``Meta.base_manager_name``), so
    ``_base_manager``, the deletion collector and reverse related managers all
    get it. Legitimate transitions live in ``production.transitions``."""

    def update(self, **kwargs):
        # Exactly the SET_NULL Django performs when a User is deleted (and the
        # reverse manager's clear()). Nothing else, and never a reassignment.
        if len(kwargs) == 1 and next(iter(kwargs)) in ("owner", "owner_id") and next(iter(kwargs.values())) is None:
            return super().update(**kwargs)
        raise ImmutableMediaError(
            f"ProductionMedia is immutable; refusing a generic update of {sorted(kwargs)}. "
            "State changes go through production.transitions.",
        )

    def bulk_update(self, objs, fields, batch_size=None):
        raise ImmutableMediaError("ProductionMedia is immutable; bulk_update is refused.")

    def bulk_create(self, objs, *args, update_conflicts=False, **kwargs):
        if update_conflicts:
            raise ImmutableMediaError("ProductionMedia is immutable; bulk_create(update_conflicts=True) is refused.")
        objs = list(objs)
        if any(getattr(obj, "kind", None) == ProductionMedia.KIND_RENDITION for obj in objs):
            raise _rendition_refused("renditions are created only by the render executor's intake")
        _refuse_laundering(objs)
        return super().bulk_create(objs, *args, update_conflicts=update_conflicts, **kwargs)

    def delete(self):
        raise ImmutableMediaError(
            "ProductionMedia rows are never deleted; purge the bytes instead "
            "(production.services.retention.purge_media).",
        )

    delete.queryset_only = True


class ProductionMedia(models.Model):
    # kind says what the bytes ARE, never what a workflow does with them.
    KIND_RECORDING = "recording"      # captured by a recorder
    KIND_UPLOAD = "upload"            # imported/uploaded as-is
    KIND_EDIT = "edit"                # an edit of another ProductionMedia
    KIND_RENDITION = "rendition"      # a rendered derivative of another one
    KIND_BED = "bed"                  # station-managed production asset
    KIND_CHOICES = [
        (KIND_RECORDING, "Recording"),
        (KIND_UPLOAD, "Upload / import"),
        (KIND_EDIT, "Edit"),
        (KIND_RENDITION, "Rendition"),
        (KIND_BED, "Bed / production asset"),
    ]
    DERIVED_KINDS = (KIND_EDIT, KIND_RENDITION)

    VALIDATION_UNVALIDATED = "unvalidated"
    VALIDATION_VALID = "valid"
    VALIDATION_INVALID = "invalid"
    VALIDATION_CHOICES = [
        (VALIDATION_UNVALIDATED, "Not yet validated"),
        (VALIDATION_VALID, "Valid"),
        (VALIDATION_INVALID, "Invalid"),
    ]

    RETENTION_PRESENT = "present"
    RETENTION_PURGED = "purged"
    RETENTION_CHOICES = [
        (RETENTION_PRESENT, "Present"),
        (RETENTION_PURGED, "Purged (bytes removed)"),
    ]

    # --- identity / custody ------------------------------------------------
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="production_media",
        help_text="Custody of the bytes, not authority over any workflow.",
    )
    owner_username = models.CharField(
        max_length=150, blank=True,
        help_text="Snapshot taken at creation so custody stays understandable "
                  "after the user account is deleted.",
    )

    # --- storage identity (system generated, never user supplied) ----------
    storage_key = models.CharField(max_length=64, unique=True)
    sha256 = models.CharField(max_length=64)
    byte_size = models.PositiveBigIntegerField()

    # --- authoritative content facts (written once, by validation) ---------
    container = models.CharField(max_length=32, blank=True, default="")
    codec = models.CharField(max_length=32, blank=True, default="")
    sample_rate = models.PositiveIntegerField(null=True, blank=True)
    channels = models.PositiveSmallIntegerField(null=True, blank=True)
    decoded_duration_seconds = models.DecimalField(
        max_digits=12, decimal_places=6, null=True, blank=True,
        help_text="Duration established by a full decode -- the only duration to trust.",
    )
    header_duration_seconds = models.DecimalField(
        max_digits=12, decimal_places=6, null=True, blank=True,
        help_text="Duration the container claims; absent for e.g. browser WebM. Evidence only.",
    )
    probe = models.JSONField(
        default=dict, blank=True,
        help_text="Bounded, whitelisted technical evidence (never raw tool output).",
    )

    # --- untrusted display / provenance metadata ----------------------------
    original_filename = models.CharField(max_length=255, blank=True)
    declared_content_type = models.CharField(max_length=127, blank=True)

    # --- validation ----------------------------------------------------------
    validation_state = models.CharField(
        max_length=12, choices=VALIDATION_CHOICES, default=VALIDATION_UNVALIDATED,
    )
    validation_code = models.CharField(
        max_length=48, blank=True,
        help_text="Stable result code. While unvalidated it may hold the code of the "
                  "last infrastructure failure (the media was NOT judged invalid).",
    )
    validated_at = models.DateTimeField(null=True, blank=True)

    # --- derivation provenance ------------------------------------------------
    derived_from = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="derivatives",
    )
    recipe_key = models.CharField(max_length=64, blank=True)
    recipe_version = models.PositiveIntegerField(null=True, blank=True)
    recipe_params_digest = models.CharField(max_length=64, blank=True)
    toolchain = models.CharField(max_length=200, blank=True)

    # --- retention --------------------------------------------------------------
    retention_state = models.CharField(
        max_length=10, choices=RETENTION_CHOICES, default=RETENTION_PRESENT,
    )
    purged_at = models.DateTimeField(null=True, blank=True)

    # --- no-air provenance (immutable; see the module docstring) -------------
    airing_prohibited = models.BooleanField(
        default=False, editable=False,
        help_text="Made under a TEST / NO AIR subject, or derived from such media: "
                  "no air-capable binding may ever reference it.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    objects = ProductionMediaQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        # _base_manager (deletion collector, reverse managers, Model.save
        # internals) must be the guarded queryset too, not a plain Manager.
        base_manager_name = "objects"
        verbose_name = "production media"
        verbose_name_plural = "production media"
        constraints = [
            models.CheckConstraint(
                condition=Q(sha256__regex=SHA256_PATTERN),
                name="production_media_sha256_hex",
            ),
            # Path safety is an integrity invariant, not just a service habit:
            # a stored key can never contain '..', a leading '/' or any other
            # character outside the system-generated shape.
            models.CheckConstraint(
                condition=Q(storage_key__regex=STORAGE_KEY_PATTERN),
                name="production_media_storage_key_shape",
            ),
            models.CheckConstraint(
                condition=Q(byte_size__gt=0),
                name="production_media_byte_size_positive",
            ),
            models.CheckConstraint(
                condition=~Q(kind__in=("edit", "rendition")) | Q(derived_from__isnull=False),
                name="production_media_derived_requires_parent",
            ),
            models.CheckConstraint(
                condition=(
                    Q(retention_state="present", purged_at__isnull=True)
                    | Q(retention_state="purged", purged_at__isnull=False)
                ),
                name="production_media_retention_consistent",
            ),
            models.CheckConstraint(
                condition=(
                    Q(validation_state="unvalidated", validated_at__isnull=True)
                    | Q(validation_state__in=("valid", "invalid"), validated_at__isnull=False)
                ),
                name="production_media_validation_consistent",
            ),
            # A "valid" verdict without its evidence is meaningless.
            models.CheckConstraint(
                condition=(
                    ~Q(validation_state="valid")
                    | (
                        ~Q(container="") & ~Q(codec="")
                        & Q(sample_rate__isnull=False) & Q(channels__isnull=False)
                        & Q(decoded_duration_seconds__isnull=False)
                    )
                ),
                name="production_media_valid_has_facts",
            ),
        ]

    def __str__(self):
        return f"{self.kind} {self.id}"

    # -- state ---------------------------------------------------------------
    @property
    def is_present(self) -> bool:
        return self.retention_state == self.RETENTION_PRESENT

    @property
    def is_valid(self) -> bool:
        return self.validation_state == self.VALIDATION_VALID

    # -- immutability enforcement ---------------------------------------------
    def _changed_fields(self) -> list[str]:
        """Loaded fields whose value differs from the AUTHORITATIVE database row.

        Re-reads the row rather than trusting anything captured when this
        instance was loaded, so a deferred field assigned after an ``only()``
        load, or a stale instance, is compared against the truth."""
        concrete = [field for field in self._meta.concrete_fields if field.attname in self.__dict__]
        current = type(self)._base_manager.filter(pk=self.pk).values(*[f.attname for f in concrete]).first()
        if current is None:
            raise ImmutableMediaError("refusing to save a ProductionMedia whose row does not exist")
        changed = []
        for field in concrete:
            try:
                mine = field.to_python(self.__dict__[field.attname])
                theirs = field.to_python(current[field.attname])
            except Exception:           # noqa: BLE001 -- an unparseable value is a change
                changed.append(field.name)
                continue
            if mine != theirs:
                changed.append(field.name)
        return changed

    def save(self, *args, **kwargs):
        if self._state.adding:
            if self.kind == self.KIND_RENDITION:
                # Never through save(): only insert_rendition's exact write.
                raise _rendition_refused("renditions are created only by the render executor's intake")
            _refuse_laundering([self])
            if self.owner_id and not self.owner_username:
                self.owner_username = self.owner.get_username()[:150]
            super().save(*args, **kwargs)
            return
        changed = self._changed_fields()
        if changed:
            raise ImmutableMediaError(
                f"ProductionMedia is immutable; refusing to change {changed}. "
                "Create a derived ProductionMedia instead, or use production.transitions.",
            )
        # Nothing differs: there is nothing to write (never issue an UPDATE).

    def delete(self, *args, **kwargs):
        raise ImmutableMediaError(
            "ProductionMedia rows are never deleted; purge the bytes instead "
            "(production.services.retention.purge_media).",
        )


REFERENCE_KEY_PATTERN = r"^[a-z0-9][a-z0-9_.-]{0,63}$"


# -- ReferenceAudio authority ------------------------------------------------------------
#
# A ReferenceAudio row is retained provenance. Ordinary ORM code can READ it and
# nothing else:
#
# * creation happens only inside ``production.services.reference_audio.register``,
#   which (under the per-key advisory lock and ``lock_for_binding``) opens a
#   registration scope naming the EXACT row it is about to insert -- key, media,
#   pinned SHA-256. ``save()`` refuses any insert outside that scope or not
#   matching it, so ``objects.create()``, ``get_or_create()``,
#   ``update_or_create()`` and instance saves cannot fabricate a registration;
# * an existing row is never re-saved: identity, media, pin and registration
#   evidence are immutable. Retirement -- the one mutation -- is a single
#   state-qualified UPDATE of exactly the retirement columns, issued by the
#   service (as production.transitions does for ProductionMedia);
# * ``QuerySet.update()`` refuses everything except the SET_NULL Django performs
#   when a User is deleted; ``bulk_create``/``bulk_update`` and every deletion
#   (instance, queryset, default/base/reverse manager) refuse.
#
# The guarded queryset is the default AND the base manager. ``base_manager_name``
# sits on an abstract parent so the accepted production.0002 migration state is
# unchanged (abstract-parent Meta is not part of the child's migration options;
# Options.base_manager falls back to the first parent's base manager name).
# Raw SQL is outside the boundary, as for ProductionMedia.

@dataclass(frozen=True)
class _Registration:
    key: str
    media_id: object
    sha256: str


_registration = contextvars.ContextVar("production_reference_registration", default=None)

_REFERENCE_USER_FKS = ("registered_by", "registered_by_id", "retired_by", "retired_by_id")


@contextmanager
def reference_registration(*, key, media_id, sha256):
    """Authorize inserting exactly ONE row with these identity columns.
    Entered only by production.services.reference_audio.register."""
    token = _registration.set(_Registration(key=key, media_id=media_id, sha256=sha256))
    try:
        yield
    finally:
        _registration.reset(token)


class ReferenceAudioQuerySet(models.QuerySet):
    def update(self, **kwargs):
        # Exactly Django's SET_NULL of a deleted user (actor unlinked; the
        # username snapshots keep the history readable). Nothing else.
        if kwargs and set(kwargs) <= set(_REFERENCE_USER_FKS) and all(v is None for v in kwargs.values()):
            return super().update(**kwargs)
        raise ReferenceAudioIntegrityError(
            f"reference audio registrations are immutable; refusing a generic update of {sorted(kwargs)} "
            "(retire through production.services.reference_audio)")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ReferenceAudioIntegrityError("reference audio registrations are immutable; bulk_update is refused")

    def bulk_create(self, objs, *args, **kwargs):
        raise ReferenceAudioIntegrityError(
            "reference audio is registered only by production.services.reference_audio.register")

    def delete(self):
        raise ReferenceAudioIntegrityError("reference audio registrations are retained provenance; never deleted")

    delete.queryset_only = True


class _GuardedReferenceAudio(models.Model):
    objects = ReferenceAudioQuerySet.as_manager()

    class Meta:
        abstract = True
        base_manager_name = "objects"


class ReferenceAudio(_GuardedReferenceAudio):
    """A station-managed, human-keyed pin of one retained ProductionMedia --
    the beds, intros and similar reference audio that render recipes use.

    Domain-neutral and deliberately tiny: it is NOT a workflow and NOT an
    asset hierarchy. A key (``bed.news_intro``) resolves to exactly one
    CURRENT row, which names the media AND pins the SHA-256 it had when it was
    registered. Rows are append-only: replacing the audio behind a key retires
    the current row and registers a new one (both recorded, with who and why),
    so a rendition's provenance can always name the exact bytes it used.

    Retained provenance: rows are never deleted, so every bed media a
    registration -- current OR retired -- names stays retained (PROTECT; purge
    refuses). That low-volume cost is intentional.

    Resolution (production.services.reference_audio) refuses a missing,
    retired, purged, not-valid or hash-mismatched reference; a recipe can never
    reach audio by path -- only by key.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=64)
    media = models.ForeignKey(ProductionMedia, on_delete=models.PROTECT, related_name="reference_audio")
    sha256 = models.CharField(max_length=64)
    label = models.CharField(max_length=200, blank=True)
    note = models.CharField(max_length=500, blank=True)
    registered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="registered_reference_audio",
    )
    registered_by_username = models.CharField(max_length=150, blank=True)
    registered_at = models.DateTimeField(auto_now_add=True)
    retired_at = models.DateTimeField(null=True, blank=True)
    retired_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="retired_reference_audio",
    )
    retired_by_username = models.CharField(max_length=150, blank=True)
    retire_reason = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["key", "-registered_at"]
        verbose_name = "reference audio"
        verbose_name_plural = "reference audio"
        constraints = [
            models.CheckConstraint(condition=Q(key__regex=REFERENCE_KEY_PATTERN),
                                   name="production_reference_key_shape"),
            models.CheckConstraint(condition=Q(sha256__regex=SHA256_PATTERN),
                                   name="production_reference_sha256_hex"),
            # One current (un-retired) row per key: resolution is unambiguous.
            # Defense in depth beneath the service's per-key advisory lock.
            models.UniqueConstraint(fields=["key"], condition=Q(retired_at__isnull=True),
                                    name="production_reference_one_current_per_key"),
        ]
        indexes = [models.Index(fields=["key", "registered_at"], name="production_reference_history")]

    def __str__(self):
        return f"{self.key} -> {self.media_id}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ReferenceAudioIntegrityError(
                "reference audio registrations are immutable (retire through production.services.reference_audio)")
        authorized = _registration.get()
        if (authorized is None or self.retired_at is not None or self.key != authorized.key
                or str(self.media_id) != str(authorized.media_id) or self.sha256 != authorized.sha256):
            raise ReferenceAudioIntegrityError(
                "reference audio is registered only by production.services.reference_audio.register")
        if kwargs.get("update_fields") is not None or kwargs.get("force_update"):
            raise ReferenceAudioIntegrityError("a registration is inserted once, never updated")
        if not transaction.get_connection(kwargs.get("using")).in_atomic_block:
            raise ReferenceAudioIntegrityError("a registration is inserted inside its registering transaction")
        kwargs["force_insert"] = True
        super().save(*args, **kwargs)
        # The authorization was checked on entry; a pre_save receiver runs after
        # that. Verify what actually reached the row (same transaction, before
        # commit) so a tampered insert is refused and rolled back.
        stored = type(self)._base_manager.filter(pk=self.pk).values("key", "media_id", "sha256", "retired_at").first()
        if stored is None or (stored["key"], str(stored["media_id"]), stored["sha256"], stored["retired_at"]) != (
                authorized.key, str(authorized.media_id), authorized.sha256, None):
            raise ReferenceAudioIntegrityError("the inserted registration differs from the authorized one")

    def delete(self, *args, **kwargs):
        raise ReferenceAudioIntegrityError("reference audio registrations are retained provenance; never deleted")
