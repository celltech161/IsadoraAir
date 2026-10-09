"""The spoken/produced-content domain (iPortal Phase C).

News, underwriting/ministry reads, calendar and community announcements,
PSAs, 3-minute-center material and similar ordinary produced spoken audio.
This domain owns its own identity, lifecycle, review, recall, expiry and
library-projection policy. It is NOT a generic workflow: Voice Tracking and
active messages are separate domains. Audio bytes live in the shared
``production`` substrate; every reference points domain -> ProductionMedia
(PROTECT), never the reverse, and every such reference is written under
``production.services.retention.lock_for_binding`` (the binding rule).

    SpokenContentType 1--* SpokenItem 1--* SpokenItemEvent   (authoritative history)
            |                  |
            |                  +-- submission_media / rendition_media --> ProductionMedia
            +--* SpokenAsset --1:1-- library.Track (SET_NULL)     [projection: C2]
                     +--* SpokenAssetEdition --> ProductionMedia   [edition history]

Lifecycle state changes happen ONLY in ``spoken.transitions`` (row-locked,
fixed transition table, state + event in one transaction); ordinary ORM paths
refuse to change ``SpokenItem.state``, and events are append-only.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models, router, transaction
from django.db.models import Q

from production.models import ProductionMedia

from . import guard

CODE_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,47}$"
SLOT_KEY_PATTERN = r"^([a-z0-9][a-z0-9_.-]{0,63})?$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
LIBRARY_RELPATH_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,19}/spoken-[0-9a-f]{32}\.flac$"


# -- SpokenContentType ----------------------------------------------------------------

class DeliveryMode(models.TextChoices):
    CURRENT_EDITION = "current_edition", "Current edition (one stable Track per slot)"
    ACCUMULATING = "accumulating", "Accumulating (one Track per delivered item)"
    RETAIN_ONLY = "retain_only", "Retain only (never projected to the library)"


PROJECTED_MODES = (DeliveryMode.CURRENT_EDITION, DeliveryMode.ACCUMULATING)


class TitlePolicy(models.TextChoices):
    SUBMITTED_DATE = "submitted_date", "Submission date (MM/DD/YYYY)"
    ITEM_TITLE = "item_title", "Item title"
    ITEM_TITLE_AND_DATE = "item_title_and_date", "Item title - date"
    SUBMITTER_AND_DATE = "submitter_and_date", "Submitter - date"


class CurrentRecallPolicy(models.TextChoices):
    RETIRE_ASSET = "retire_asset", "Retire the asset (nothing airs for the slot)"
    RESTORE_PREVIOUS = "restore_previous", "Restore the previous edition"


class RecallPolicy(models.TextChoices):
    OWNER_UNTIL_DELIVERED = "owner_until_delivered", "Owner until delivered; managers anytime"
    OWNER_ANYTIME = "owner_anytime", "Owner or managers anytime"
    MANAGERS_ONLY = "managers_only", "Managers only"


class SpokenContentTypeQuerySet(models.QuerySet):
    """Configuration is written ONLY through ``SpokenContentType.save()``
    (row-locked, validated). Generic bulk writes would skip both."""

    def update(self, **kwargs):
        raise guard.SpokenIntegrityError(
            "content-type configuration changes only through SpokenContentType.save() (validated, row-locked)")

    def bulk_update(self, objs, fields, batch_size=None):
        raise guard.SpokenIntegrityError("content-type bulk_update is refused; save each type")

    def bulk_create(self, objs, *args, **kwargs):
        raise guard.SpokenIntegrityError("content-type bulk_create is refused; save each type")


class SpokenContentType(models.Model):
    """A station-configured kind of spoken content. Created disabled; the
    configuration (recipe, limits, projection policy) is validated by
    ``spoken.configuration`` on every save. Once an item uses it, its ``code``
    is permanent.

    ``save()`` is the configuration authority: in ONE transaction it locks the
    persisted row (``SELECT ... FOR UPDATE``), checks code permanence against
    the items that exist while the lock is held, validates, writes and commits.
    ``create_item`` and render completion lock the same row, so a rename and a
    first item -- or a reconfiguration and a completion -- are serialized."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=48, unique=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    instructions = models.TextField(blank=True)
    enabled = models.BooleanField(default=False)

    delivery_mode = models.CharField(max_length=16, choices=DeliveryMode.choices)
    recipe_key = models.CharField(max_length=64)
    recipe_params = models.JSONField(default=dict, blank=True)

    min_duration_seconds = models.DecimalField(max_digits=9, decimal_places=3, null=True, blank=True)
    max_duration_seconds = models.DecimalField(max_digits=9, decimal_places=3, null=True, blank=True)
    max_bytes = models.PositiveBigIntegerField(null=True, blank=True)

    target_category = models.ForeignKey(
        "library.Category", null=True, blank=True, on_delete=models.PROTECT, related_name="spoken_content_types",
    )
    artist_label = models.CharField(max_length=200, blank=True)
    title_policy = models.CharField(max_length=24, choices=TitlePolicy.choices, default=TitlePolicy.SUBMITTED_DATE)

    requires_review = models.BooleanField(default=True)
    deliver_ready_to_air = models.BooleanField(default=False)
    on_current_recall = models.CharField(max_length=20, choices=CurrentRecallPolicy.choices,
                                         default=CurrentRecallPolicy.RETIRE_ASSET)
    recall_policy = models.CharField(max_length=24, choices=RecallPolicy.choices,
                                     default=RecallPolicy.OWNER_UNTIL_DELIVERED)
    default_lifetime = models.DurationField(null=True, blank=True)
    media_retention_days = models.PositiveIntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SpokenContentTypeQuerySet.as_manager()

    class Meta:
        ordering = ["name"]
        base_manager_name = "objects"
        constraints = [
            models.CheckConstraint(condition=Q(code__regex=CODE_PATTERN), name="spoken_type_code_shape"),
            models.CheckConstraint(condition=~Q(recipe_key=""), name="spoken_type_recipe_required"),
            models.CheckConstraint(
                condition=Q(min_duration_seconds__isnull=True) | Q(min_duration_seconds__gt=0),
                name="spoken_type_min_duration_positive"),
            models.CheckConstraint(
                condition=Q(max_duration_seconds__isnull=True) | Q(max_duration_seconds__gt=0),
                name="spoken_type_max_duration_positive"),
            models.CheckConstraint(
                condition=(Q(min_duration_seconds__isnull=True) | Q(max_duration_seconds__isnull=True)
                           | Q(min_duration_seconds__lte=models.F("max_duration_seconds"))),
                name="spoken_type_duration_order"),
            models.CheckConstraint(condition=Q(max_bytes__isnull=True) | Q(max_bytes__gt=0),
                                   name="spoken_type_max_bytes_positive"),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"

    @property
    def projects_to_library(self) -> bool:
        return self.delivery_mode in PROJECTED_MODES

    def save(self, *args, **kwargs):
        from . import configuration
        using = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            configuration.check_before_save(self, using=using)       # locks the persisted row first
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        using = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            # The same row lock create_item takes (the PROTECT foreign key also
            # refuses at the database).
            type(self)._base_manager.db_manager(using).select_for_update().filter(pk=self.pk).values_list("pk").first()
            if SpokenItem._base_manager.db_manager(using).filter(content_type_id=self.pk).exists():
                raise guard.SpokenIntegrityError("a content type with items is never deleted; disable it instead")
            return super().delete(*args, **kwargs)


# -- SpokenItem --------------------------------------------------------------------------

class ItemOrigin(models.TextChoices):
    SUBMITTED = "submitted", "Submitted (unsolicited contribution)"
    ASSIGNED = "assigned", "Assigned (Phase E)"
    IMPORTED = "imported", "Imported (legacy migration)"


class ItemState(models.TextChoices):
    DRAFT = "draft", "Draft"
    SUBMITTED = "submitted", "Submitted"
    VALIDATED = "validated", "Validated"
    PROCESSING = "processing", "Processing"
    PROCESSED = "processed", "Processed"
    AWAITING_REVIEW = "awaiting_review", "Awaiting review"
    DELIVERED = "delivered", "Delivered"
    SUPERSEDED = "superseded", "Superseded"
    RECALLED = "recalled", "Recalled"
    EXPIRED = "expired", "Expired"
    INVALID = "invalid", "Invalid"
    FAILED = "failed", "Failed"
    REJECTED = "rejected", "Rejected"


TERMINAL_STATES = (ItemState.RECALLED, ItemState.EXPIRED, ItemState.INVALID, ItemState.REJECTED)


class SpokenItemQuerySet(models.QuerySet):
    """Default AND base manager; reverse managers derive from it (see guard)."""

    def update(self, **kwargs):
        guard.refuse_queryset_update("SpokenItem", kwargs, allowed=guard.ITEM_FREE_COLUMNS)
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        guard.refuse_item_bulk_update(fields)
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def bulk_create(self, objs, *args, **kwargs):
        guard.refuse("spoken items are created only by spoken.transitions.create_item; bulk_create is refused")

    def delete(self):
        guard.refuse("spoken items are never deleted; recall or retire them")

    delete.queryset_only = True


class SpokenItem(models.Model):
    """One piece of spoken content -- the authoritative workflow object."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    content_type = models.ForeignKey(SpokenContentType, on_delete=models.PROTECT, related_name="items")
    origin = models.CharField(max_length=16, choices=ItemOrigin.choices, default=ItemOrigin.SUBMITTED)

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="spoken_items",
    )
    submitted_by_username = models.CharField(max_length=150, blank=True)

    title = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)
    submission_media = models.ForeignKey(
        ProductionMedia, null=True, blank=True, on_delete=models.PROTECT, related_name="spoken_submissions",
    )
    rendition_media = models.ForeignKey(
        ProductionMedia, null=True, blank=True, on_delete=models.PROTECT, related_name="spoken_renditions",
    )

    state = models.CharField(max_length=16, choices=ItemState.choices, default=ItemState.DRAFT)
    state_changed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    validated_at = models.DateTimeField(null=True, blank=True)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    terminal_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    failure_code = models.CharField(max_length=64, blank=True)
    failure_detail = models.CharField(max_length=500, blank=True)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="reviewed_spoken_items",
    )
    reviewed_by_username = models.CharField(max_length=150, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=1000, blank=True)

    recalled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="recalled_spoken_items",
    )
    recalled_by_username = models.CharField(max_length=150, blank=True)
    recalled_at = models.DateTimeField(null=True, blank=True)
    recall_reason = models.CharField(max_length=500, blank=True)

    processing_attempts = models.PositiveSmallIntegerField(default=0)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)

    # Dormant legacy-import provenance (Phase F). Native items leave it empty.
    import_source = models.CharField(max_length=64, blank=True)
    import_external_id = models.CharField(max_length=128, blank=True)
    imported_at = models.DateTimeField(null=True, blank=True)
    import_snapshot = models.JSONField(null=True, blank=True)

    objects = SpokenItemQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        base_manager_name = "objects"
        constraints = [
            models.CheckConstraint(
                condition=(Q(lease_token__isnull=True, lease_expires_at__isnull=True)
                           | Q(lease_token__isnull=False, lease_expires_at__isnull=False)),
                name="spoken_item_lease_pair"),
            models.UniqueConstraint(fields=["import_source", "import_external_id"], condition=~Q(import_source=""),
                                    name="spoken_item_import_identity"),
            models.CheckConstraint(
                condition=(Q(import_source="", import_external_id="", imported_at__isnull=True)
                           & ~Q(origin="imported"))
                | (~Q(import_source="") & ~Q(import_external_id="") & Q(imported_at__isnull=False)
                   & Q(origin="imported")),
                name="spoken_item_import_provenance_consistent"),
            models.CheckConstraint(condition=Q(processing_attempts__lte=1000), name="spoken_item_attempts_bounded"),
        ]
        indexes = [
            models.Index(fields=["state", "lease_expires_at"], name="spoken_item_state_lease"),
            models.Index(fields=["content_type", "state"], name="spoken_item_type_state"),
            models.Index(fields=["submitted_by", "submitted_at"], name="spoken_item_submitter"),
        ]

    def __str__(self):
        return f"{self.content_type_id} item {self.id} [{self.state}]"

    def save(self, *args, **kwargs):
        guard.check_item_save(self, kwargs.get("update_fields"))
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise guard.SpokenIntegrityError("spoken items are never deleted; recall or retire them")


# -- SpokenItemEvent ------------------------------------------------------------------

class ActorKind(models.TextChoices):
    USER = "user", "User"
    SYSTEM = "system", "System"
    WORKER = "worker", "Worker"
    IMPORT = "import", "Import"


class SpokenItemEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        # The one Django-owned write: SET_NULL of a deleted user. The
        # ``actor_username`` snapshot keeps the history readable.
        if set(kwargs) <= {"actor", "actor_id"} and all(value is None for value in kwargs.values()):
            return super().update(**kwargs)
        raise guard.SpokenIntegrityError("spoken item events are append-only")

    def bulk_update(self, objs, fields, batch_size=None):
        raise guard.SpokenIntegrityError("spoken item events are append-only")

    def bulk_create(self, objs, *args, **kwargs):
        raise guard.SpokenIntegrityError("spoken item events are written only by spoken.transitions")

    def delete(self):
        raise guard.SpokenIntegrityError("spoken item events are append-only")

    delete.queryset_only = True


class SpokenItemEvent(models.Model):
    """THE authoritative spoken-content history. Append-only: written only by
    ``spoken.transitions`` in the same transaction as the change it records.
    (``monitoring.SystemEvent`` is only for operator attention.)"""

    id = models.BigAutoField(primary_key=True)
    item = models.ForeignKey(SpokenItem, on_delete=models.PROTECT, related_name="events")
    event = models.SlugField(max_length=64)
    from_state = models.CharField(max_length=16, blank=True)
    to_state = models.CharField(max_length=16, blank=True)
    actor_kind = models.CharField(max_length=8, choices=ActorKind.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="spoken_item_events",
    )
    actor_username = models.CharField(max_length=150, blank=True)
    media = models.ForeignKey(
        ProductionMedia, null=True, blank=True, on_delete=models.PROTECT, related_name="spoken_item_events",
    )
    asset = models.ForeignKey("SpokenAsset", null=True, blank=True, on_delete=models.PROTECT, related_name="events")
    edition = models.ForeignKey("SpokenAssetEdition", null=True, blank=True, on_delete=models.PROTECT,
                                related_name="events")
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SpokenItemEventQuerySet.as_manager()

    class Meta:
        ordering = ["item", "id"]
        base_manager_name = "objects"
        indexes = [models.Index(fields=["item", "created_at"], name="spoken_event_item_time")]

    def __str__(self):
        return f"{self.event} {self.from_state}->{self.to_state}"

    def save(self, *args, **kwargs):
        guard.check_event_write(self)                  # exact, single-use authorization (or refused)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise guard.SpokenIntegrityError("spoken item events are append-only")


# -- SpokenAsset / SpokenAssetEdition (projection identity; delivery is C2) -----------------

class AssetMode(models.TextChoices):
    CURRENT_EDITION = "current_edition", "Current edition"
    ACCUMULATING = "accumulating", "Accumulating"


class AssetState(models.TextChoices):
    PENDING = "pending", "Pending"
    LIVE = "live", "Live"
    RETIRING = "retiring", "Retiring"
    RETIRED = "retired", "Retired"
    DETACHED = "detached", "Detached (Track deleted by an operator)"


class SpokenAsset(models.Model):
    """The stable airable identity a spoken item projects into a library Track.

    current_edition: ONE asset per (content type, slot_key) -- a stable Track
    whose content is replaced edition by edition. accumulating: one asset per
    delivered item. Identity never depends on a submitted filename: the
    library path is ``<category code>/spoken-<asset uuid>.flac``."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    content_type = models.ForeignKey(SpokenContentType, on_delete=models.PROTECT, related_name="assets")
    mode = models.CharField(max_length=16, choices=AssetMode.choices)
    slot_key = models.CharField(max_length=64, blank=True, default="")
    item = models.ForeignKey(SpokenItem, null=True, blank=True, on_delete=models.PROTECT,
                             related_name="accumulating_assets")
    track = models.OneToOneField("library.Track", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="spoken_asset")
    library_relpath = models.CharField(max_length=255, unique=True)
    current_item = models.ForeignKey(SpokenItem, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="current_assets")
    current_rendition = models.ForeignKey(ProductionMedia, null=True, blank=True, on_delete=models.PROTECT,
                                          related_name="spoken_current_assets")
    state = models.CharField(max_length=10, choices=AssetState.choices, default=AssetState.PENDING)
    generation = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    retired_at = models.DateTimeField(null=True, blank=True)
    retired_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["content_type", "slot_key"],
                condition=Q(mode="current_edition") & ~Q(state="retired"),
                name="spoken_asset_one_current_edition_per_slot"),
            models.UniqueConstraint(
                fields=["item"], condition=Q(mode="accumulating") & ~Q(state="retired"),
                name="spoken_asset_one_accumulating_per_item"),
            models.CheckConstraint(
                condition=(Q(mode="accumulating", item__isnull=False)
                           | Q(mode="current_edition", item__isnull=True)),
                name="spoken_asset_item_matches_mode"),
            models.CheckConstraint(
                condition=Q(state="retired", retired_at__isnull=False) | (~Q(state="retired") & Q(retired_at__isnull=True)),
                name="spoken_asset_retired_consistent"),
            models.CheckConstraint(condition=Q(slot_key__regex=SLOT_KEY_PATTERN), name="spoken_asset_slot_key_shape"),
            models.CheckConstraint(condition=Q(library_relpath__regex=LIBRARY_RELPATH_PATTERN),
                                   name="spoken_asset_library_relpath_shape"),
        ]
        indexes = [models.Index(fields=["content_type", "state"], name="spoken_asset_type_state")]

    def __str__(self):
        return f"{self.mode} asset {self.id} [{self.state}]"


class EditionState(models.TextChoices):
    PENDING = "pending", "Pending"
    ACTIVE = "active", "Active"
    SUPERSEDED = "superseded", "Superseded"
    ABORTED = "aborted", "Aborted"


class SpokenAssetEdition(models.Model):
    """Edition/promotion history of an asset: which item's rendition the
    asset carried, at which generation. Not a copy of media."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    asset = models.ForeignKey(SpokenAsset, on_delete=models.PROTECT, related_name="editions")
    item = models.ForeignKey(SpokenItem, on_delete=models.PROTECT, related_name="editions")
    rendition = models.ForeignKey(ProductionMedia, on_delete=models.PROTECT, related_name="spoken_editions")
    generation = models.PositiveIntegerField()
    state = models.CharField(max_length=10, choices=EditionState.choices, default=EditionState.PENDING)
    library_sha256 = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    promoted_at = models.DateTimeField(null=True, blank=True)
    superseded_at = models.DateTimeField(null=True, blank=True)
    superseded_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT,
                                      related_name="supersedes")

    class Meta:
        ordering = ["asset", "generation"]
        constraints = [
            models.UniqueConstraint(fields=["asset"], condition=Q(state="active"),
                                    name="spoken_edition_one_active_per_asset"),
            models.UniqueConstraint(fields=["asset", "generation"], name="spoken_edition_unique_generation"),
            models.CheckConstraint(condition=Q(library_sha256="") | Q(library_sha256__regex=SHA256_PATTERN),
                                   name="spoken_edition_library_sha256_hex"),
            models.CheckConstraint(condition=~Q(state="active") | Q(promoted_at__isnull=False),
                                   name="spoken_edition_active_promoted"),
            models.CheckConstraint(condition=~Q(state="superseded") | Q(superseded_at__isnull=False),
                                   name="spoken_edition_superseded_dated"),
        ]

    def __str__(self):
        return f"edition g{self.generation} of {self.asset_id} [{self.state}]"
