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
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from .errors import ImmutableMediaError

SHA256_PATTERN = r"^[0-9a-f]{64}$"
# <2 hex shard>/<32 hex media id> -- relative to <root>/media/. Extensionless on
# purpose: no extension is chosen from untrusted metadata, and the permanent
# name never has to change when validation later establishes the container.
STORAGE_KEY_PATTERN = r"^[0-9a-f]{2}/[0-9a-f]{32}$"


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
