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

Immutability contract (enforced here in Python, backed by the database
constraints below; there is deliberately no database trigger because that
would need a manual migration):

* identity, bytes identity (storage_key / sha256 / byte_size), kind, owner
  custody, display metadata and derivation provenance never change after the
  row is created;
* the *technical facts* and validation verdict are written once, by
  production.services.validation, while the row is still ``unvalidated``, and
  are frozen as soon as the verdict is ``valid`` or ``invalid``;
* ``retention_state`` only ever moves present -> purged, together with
  ``purged_at``; the row (hashes, size, probe facts, provenance) outlives a
  byte purge;
* editing audio means a NEW ProductionMedia with ``derived_from`` pointing at
  the original -- never an in-place overwrite.

Rows are never deleted by application code; purge removes bytes only.
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
    """Refuses bulk operations that would bypass the immutability contract."""

    # Columns a service may change with a conditional UPDATE (the model's own
    # save() additionally enforces the one-way rules for instance saves).
    MUTABLE_COLUMNS = frozenset({
        "container", "codec", "sample_rate", "channels",
        "decoded_duration_seconds", "header_duration_seconds", "probe",
        "validation_state", "validation_code", "validated_at",
        "retention_state", "purged_at",
    })

    def delete(self):
        raise ImmutableMediaError(
            "ProductionMedia rows are never deleted; purge the bytes instead "
            "(production.services.retention.purge_media).",
        )

    def update(self, **kwargs):
        illegal = sorted(set(kwargs) - self.MUTABLE_COLUMNS)
        if illegal:
            raise ImmutableMediaError(
                f"ProductionMedia is immutable; refusing to bulk-update {illegal}.",
            )
        return super().update(**kwargs)


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

    # Frozen from the moment the row exists.
    FROZEN_ALWAYS = (
        "kind", "owner_id", "owner_username", "storage_key", "sha256", "byte_size",
        "original_filename", "declared_content_type", "derived_from_id",
        "recipe_key", "recipe_version", "recipe_params_digest", "toolchain", "created_at",
    )
    # Written once by validation; frozen once the verdict is valid/invalid.
    FROZEN_AFTER_VERDICT = (
        "container", "codec", "sample_rate", "channels", "decoded_duration_seconds",
        "header_duration_seconds", "probe", "validation_state", "validation_code",
        "validated_at",
    )

    class Meta:
        ordering = ["-created_at"]
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
    _TRACKED = FROZEN_ALWAYS + FROZEN_AFTER_VERDICT + ("retention_state", "purged_at")

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._capture_original()
        return instance

    def _capture_original(self):
        # Only attributes actually loaded (deferred fields are skipped).
        loaded = self.__dict__
        self._original = {name: loaded[name] for name in self._TRACKED if name in loaded}

    def refresh_from_db(self, using=None, fields=None, **kwargs):
        super().refresh_from_db(using=using, fields=fields, **kwargs)
        self._capture_original()

    def _check_mutation_allowed(self):
        original = getattr(self, "_original", None)
        if original is None:
            raise ImmutableMediaError(
                "refusing to save a ProductionMedia instance with no loaded baseline",
            )
        changed = {
            name for name, before in original.items()
            if name in self.__dict__ and self.__dict__[name] != before
        }
        frozen = set(self.FROZEN_ALWAYS) & changed
        if original.get("validation_state", self.VALIDATION_UNVALIDATED) != self.VALIDATION_UNVALIDATED:
            frozen |= set(self.FROZEN_AFTER_VERDICT) & changed
        if {"retention_state", "purged_at"} & changed:
            # The only legal retention move is present -> purged, with purged_at.
            legal = (
                original.get("retention_state") == self.RETENTION_PRESENT
                and self.retention_state == self.RETENTION_PURGED
                and self.purged_at is not None
            )
            if not legal:
                frozen |= {"retention_state", "purged_at"} & changed
        if frozen:
            raise ImmutableMediaError(
                f"ProductionMedia is immutable; refusing to change {sorted(frozen)}. "
                "Create a derived ProductionMedia instead of editing in place.",
            )

    def save(self, *args, **kwargs):
        if self._state.adding:
            if self.owner_id and not self.owner_username:
                self.owner_username = self.owner.get_username()[:150]
        else:
            self._check_mutation_allowed()
        super().save(*args, **kwargs)
        self._capture_original()

    def delete(self, *args, **kwargs):
        raise ImmutableMediaError(
            "ProductionMedia rows are never deleted; purge the bytes instead "
            "(production.services.retention.purge_media).",
        )
