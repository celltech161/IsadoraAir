"""Native produced-content authority (iPortal Phase D3A).

A station defines its producer content TYPES (legacy OGRemote's per-station
categories: ``usd239min``, ``news_item``, ``urgent_pa``, ``public_address``,
``test_audio`` ...) and which users may produce each one:

    ProducedContentType 1--* ProducedContentTypeAssignment *--1 User

Each type has exactly one DELIVERY MODE:

* ``regular`` -- ordinary library content: a target library Category, an
  output mode (``single`` replaces, ``accumulating`` adds) and a LOGICAL
  recipe key. Processed and delivered (D3C) by the Phase-C ``spoken`` domain
  through an EXPLICIT, managed backing ``spoken.SpokenContentType``
  (``spoken_content_type``; produced_content.backend keeps it synchronized --
  this type is the configuration authority). Never an active message.
* ``public_address`` / ``urgent`` -- an active message (active_messages):
  ``recallable`` (may the producer withdraw their own ACTIVE message?),
  ``producer_self_publish`` (may an assigned producer activate their own
  message, or does activation need CONTROL?) -- both default to yes for a
  new type -- and an optional replay interval override (NULL: the
  ActiveBroadcastPolicy default). (The two compatibility types the D3A
  migration creates set both to no explicitly, preserving D1 authority.)
* ``test`` -- explicitly non-airing (no delivery of any kind): a producer's
  current saved take is a ``ProducedContentTestTake`` (validated, temporary
  ProductionMedia only) -- never spoken, never library, never air.

An assignment is SCOPE, not a role: the broad capability still comes from
``authz`` (e.g. ``active_message.produce``); the assignment narrows it to
exact types. CONTROL authority works across enabled types without one.

No field here is executable: no paths, scripts, dotted imports or commands.
Incompatible field combinations are refused by ``clean()`` (forms, admin)
and by database CHECK constraints. ``key`` is the durable identity (history
and execution snapshots record it) and never changes; neither does the
delivery mode of a type that active messages already reference (their
frozen ``kind`` was derived from it), that has a spoken backend, or that has
saved test takes.
"""
from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models, router, transaction
from django.db.models import Q

from production.models import ProductionMedia

KEY_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
# A logical recipe identifier only (D3C maps it to a local, built-in recipe):
# no dots (no dotted imports), no slashes (no paths), no spaces (no commands).
# It may start with a digit: the accepted Phase-C recipe ``3min_ctr`` does
# (D3A's first pattern refused it; produced_content.0003 corrects that).
RECIPE_KEY_PATTERN = r"^[a-z0-9][a-z0-9_]{0,63}$"
REPLAY_INTERVAL_MIN_SECONDS = 60          # the active-message bounds (active_messages.models)
REPLAY_INTERVAL_MAX_SECONDS = 86400


class DeliveryMode(models.TextChoices):
    REGULAR = "regular", "Regular library content"
    PUBLIC_ADDRESS = "public_address", "Public address"
    URGENT = "urgent", "Urgent"
    TEST = "test", "Test / no air"


ACTIVE_MESSAGE_MODES = (DeliveryMode.PUBLIC_ADDRESS, DeliveryMode.URGENT)


class OutputMode(models.TextChoices):
    SINGLE = "single", "Single (replaces the previous item)"
    ACCUMULATING = "accumulating", "Accumulating (adds items)"


def _blank(*names) -> Q:
    return Q(**{name: "" for name in names})


def _null(*names, isnull=True) -> Q:
    return Q(**{f"{name}__isnull": isnull for name in names})


_ACTIVE_ONLY = ("recallable", "producer_self_publish", "replay_interval_seconds")
_REGULAR_TEXT = ("output_mode", "recipe_key", "artist_tag")


# -- the spoken backend link is bridge-only identity ----------------------------------------
# ``ProducedContentType.spoken_content_type`` is domain identity, not an ordinary
# editable foreign key: it is FIRST-linked by produced_content.backend (under the
# type's row lock) and never attached, replaced or cleared by anything else. An
# ordinary save never writes it (a stale instance cannot erase it) and refuses an
# instance whose link was changed in memory; the queryset refuses link writes
# except the bridge's one exact granted first link; the database refuses a backend
# on a non-regular type (produced_type_backend_regular_only).
LINK_FIELDS = frozenset({"spoken_content_type", "spoken_content_type_id"})
BRIDGE_ONLY = "The spoken backend is linked only by produced_content.backend."


class BackendLinkRefused(ValidationError):
    """A write of the spoken backend link outside the bridge's exact grant."""


@dataclass(frozen=True)
class _LinkGrant:
    type_pk: int
    backing_pk: object


_LINK_GRANT: contextvars.ContextVar = contextvars.ContextVar("produced_backend_link_grant", default=None)


@contextlib.contextmanager
def _grant_backend_link(type_pk, backing_pk):
    """produced_content.backend only: authorize ONE first link of one exact type."""
    token = _LINK_GRANT.set(_LinkGrant(type_pk, backing_pk))
    try:
        yield
    finally:
        _LINK_GRANT.reset(token)


def _refuse_link(message=BRIDGE_ONLY):
    raise BackendLinkRefused({"spoken_content_type": message})


class ProducedContentTypeQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if LINK_FIELDS & set(kwargs):
            grant = _LINK_GRANT.get()
            value = kwargs.get("spoken_content_type", kwargs.get("spoken_content_type_id"))
            backing_pk = getattr(value, "pk", value)
            if grant is None or len(kwargs) != 1 or backing_pk is None or str(backing_pk) != str(grant.backing_pk):
                _refuse_link()
            # exactly the granted, regular, still unlinked row
            if list(self.values_list("pk", "delivery_mode", "spoken_content_type_id")) != [
                    (grant.type_pk, DeliveryMode.REGULAR, None)]:
                _refuse_link("The bridge first-links one exact unlinked regular type.")
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        if LINK_FIELDS & set(fields):
            _refuse_link()
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def bulk_create(self, objs, *args, **kwargs):
        objs = list(objs)
        if any(getattr(obj, "spoken_content_type_id", None) is not None for obj in objs):
            _refuse_link()
        return super().bulk_create(objs, *args, **kwargs)


class ProducedContentType(models.Model):
    key = models.CharField(
        max_length=64, unique=True, validators=[RegexValidator(KEY_PATTERN)],
        help_text="Durable identifier (lowercase letters, digits, _ and -). Never changes once created.")
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    enabled = models.BooleanField(
        default=True, help_text="A disabled type refuses new production and activation (for everyone).")
    sort_order = models.PositiveSmallIntegerField(default=0)
    delivery_mode = models.CharField(max_length=16, choices=DeliveryMode.choices)

    # -- regular -------------------------------------------------------------------------------
    target_category = models.ForeignKey(
        "library.Category", null=True, blank=True, on_delete=models.PROTECT, related_name="produced_content_types",
        help_text="Regular only: the library category the produced audio is delivered into.")
    output_mode = models.CharField(max_length=16, choices=OutputMode.choices, blank=True,
                                   help_text="Regular only.")
    recipe_key = models.CharField(
        max_length=64, blank=True, validators=[RegexValidator(RECIPE_KEY_PATTERN)],
        help_text="Regular only, optional: a logical local recipe identifier (not a path, script or import).")
    artist_tag = models.CharField(max_length=200, blank=True, help_text="Regular only, optional.")
    # The explicit identity of the Phase-C execution backend (D3C). Written
    # only by produced_content.backend; never inferred from key/code names.
    spoken_content_type = models.OneToOneField(
        "spoken.SpokenContentType", null=True, blank=True, editable=False, on_delete=models.PROTECT,
        related_name="produced_content_type",
        help_text="Regular only: the managed spoken content type that processes and delivers this type.")

    # -- public address / urgent -----------------------------------------------------------------
    recallable = models.BooleanField(
        null=True, blank=True,
        help_text="Public address / urgent only: the producer may withdraw their own ACTIVE message. "
                  "Default: yes.")
    producer_self_publish = models.BooleanField(
        null=True, blank=True,
        help_text="Public address / urgent only: an assigned producer may activate their own message; "
                  "otherwise activation needs control authority. Default: yes.")
    replay_interval_seconds = models.PositiveIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(REPLAY_INTERVAL_MIN_SECONDS), MaxValueValidator(REPLAY_INTERVAL_MAX_SECONDS)],
        help_text=(f"Public address / urgent only, optional: replay interval for messages of this type "
                   f"({REPLAY_INTERVAL_MIN_SECONDS}..{REPLAY_INTERVAL_MAX_SECONDS} s). "
                   f"Empty: the active-message policy default."))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ProducedContentTypeQuerySet.as_manager()

    class Meta:
        ordering = ["sort_order", "name", "key"]
        verbose_name = "produced content type"
        constraints = [
            models.CheckConstraint(condition=Q(key__regex=KEY_PATTERN), name="produced_type_key_shape"),
            models.CheckConstraint(condition=Q(delivery_mode__in=DeliveryMode.values),
                                   name="produced_type_delivery_mode_valid"),
            models.CheckConstraint(condition=Q(output_mode__in=["", *OutputMode.values]),
                                   name="produced_type_output_mode_valid"),
            models.CheckConstraint(condition=Q(recipe_key="") | Q(recipe_key__regex=RECIPE_KEY_PATTERN),
                                   name="produced_type_recipe_key_shape"),
            models.CheckConstraint(
                condition=Q(replay_interval_seconds__isnull=True)
                | (Q(replay_interval_seconds__gte=REPLAY_INTERVAL_MIN_SECONDS)
                   & Q(replay_interval_seconds__lte=REPLAY_INTERVAL_MAX_SECONDS)),
                name="produced_type_replay_bounded"),
            # regular: a target category and an output mode; nothing active-message.
            models.CheckConstraint(
                condition=~Q(delivery_mode=DeliveryMode.REGULAR)
                | (_null("target_category", isnull=False) & ~Q(output_mode="") & _null(*_ACTIVE_ONLY)),
                name="produced_type_regular_consistent"),
            # public address / urgent: the active-message settings; nothing regular.
            models.CheckConstraint(
                condition=~Q(delivery_mode__in=ACTIVE_MESSAGE_MODES)
                | (_null("target_category") & _blank(*_REGULAR_TEXT)
                   & _null("recallable", "producer_self_publish", isnull=False)),
                name="produced_type_active_message_consistent"),
            # test: nothing that could deliver or air.
            models.CheckConstraint(
                condition=~Q(delivery_mode=DeliveryMode.TEST)
                | (_null("target_category") & _blank(*_REGULAR_TEXT) & _null(*_ACTIVE_ONLY)),
                name="produced_type_test_inert"),
            # D3C: only a regular type has a spoken processing backend.
            models.CheckConstraint(condition=Q(spoken_content_type__isnull=True) | Q(delivery_mode=DeliveryMode.REGULAR),
                                   name="produced_type_backend_regular_only"),
        ]

    def __str__(self):
        return f"{self.name} ({self.key})"

    @property
    def is_active_message_type(self) -> bool:
        return self.delivery_mode in ACTIVE_MESSAGE_MODES

    def _apply_mode_defaults(self):
        if self.delivery_mode in ACTIVE_MESSAGE_MODES:
            if self.recallable is None:
                self.recallable = True
            if self.producer_self_publish is None:
                self.producer_self_publish = True

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._loaded_backend_id = instance.__dict__.get("spoken_content_type_id")
        return instance

    def refresh_from_db(self, *args, **kwargs):
        super().refresh_from_db(*args, **kwargs)
        self._loaded_backend_id = self.__dict__.get("spoken_content_type_id")

    def _link_changed_in_memory(self) -> bool:
        """Did the caller attach, replace or clear the link on this instance?
        (The value it was loaded with is not a change: a stale instance whose
        row the bridge linked meanwhile is just stale -- see save().)"""
        if "spoken_content_type_id" not in self.__dict__:
            return False
        return self.__dict__["spoken_content_type_id"] != getattr(self, "_loaded_backend_id", None)

    def _stored(self):
        if self.pk is None:
            return None
        return type(self).objects.filter(pk=self.pk).values("key", "delivery_mode", "spoken_content_type_id").first()

    def _referenced_by_messages(self) -> bool:
        related = getattr(self, "active_broadcast_messages", None)
        return related is not None and self.pk is not None and related.exists()

    def _mode_frozen_reason(self) -> str:
        """Why this type's delivery mode can no longer change ("" if it can)."""
        if self._referenced_by_messages():
            return ("Active messages of this type exist; their kind was derived from this delivery mode, so it "
                    "can no longer change.")
        stored = self._stored()
        if stored is not None and stored["spoken_content_type_id"] is not None:
            return "This type has a spoken processing backend (it has produced content), so its mode is fixed."
        if self.pk is not None and ProducedContentTestTake.objects.filter(content_type_id=self.pk).exists():
            return "Producers have saved test takes of this type, so its mode is fixed."
        return ""

    def _identity_errors(self) -> dict:
        stored = self._stored()
        errors = {}
        if stored is not None:
            if stored["key"] != self.key:
                errors["key"] = "The key is a durable identifier and never changes."
            if stored["delivery_mode"] != self.delivery_mode:
                reason = self._mode_frozen_reason()
                if reason:
                    errors["delivery_mode"] = reason
            if stored["spoken_content_type_id"] is not None and self.delivery_mode != DeliveryMode.REGULAR:
                errors.setdefault("delivery_mode", "Only a regular type has a spoken processing backend.")
        elif self.spoken_content_type_id is not None:
            errors["spoken_content_type"] = BRIDGE_ONLY
        if self._link_changed_in_memory():
            errors["spoken_content_type"] = BRIDGE_ONLY
        return errors

    def clean(self):
        super().clean()
        self._apply_mode_defaults()
        errors = self._identity_errors()
        mode = self.delivery_mode
        regular_fields = {"target_category": self.target_category_id is not None, "output_mode": bool(self.output_mode),
                          "recipe_key": bool(self.recipe_key), "artist_tag": bool(self.artist_tag)}
        active_fields = {"recallable": self.recallable is not None,
                         "producer_self_publish": self.producer_self_publish is not None,
                         "replay_interval_seconds": self.replay_interval_seconds is not None}
        if mode == DeliveryMode.REGULAR:
            if self.target_category_id is None:
                errors.setdefault("target_category", "A regular type needs a target library category.")
            if not self.output_mode:
                errors.setdefault("output_mode", "A regular type needs an output mode.")
            for name, present in active_fields.items():
                if present:
                    errors.setdefault(name, "Only public-address and urgent types use this setting.")
        elif mode in ACTIVE_MESSAGE_MODES or mode == DeliveryMode.TEST:
            for name, present in regular_fields.items():
                if present:
                    errors.setdefault(name, "Only regular types use this setting.")
            if mode == DeliveryMode.TEST:
                for name, present in active_fields.items():
                    if present:
                        errors.setdefault(name, "A test type never airs; this setting does not apply.")
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and LINK_FIELDS & set(update_fields):
            _refuse_link()
        if self._link_changed_in_memory():
            _refuse_link()
        using = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            if self.pk is not None and not self._state.adding:
                # The row lock every producer operation (backend bridge, test
                # take) takes first: a mode change and a first backend link or
                # first test take are serialized, so the frozen-mode check
                # below sees whichever committed first.
                type(self).objects.db_manager(using).select_for_update().filter(pk=self.pk).values_list("pk").first()
            self._apply_mode_defaults()
            errors = self._identity_errors()
            if errors:
                raise ValidationError(errors)
            if not self._state.adding and update_fields is None:
                # The backend link is written ONLY by produced_content.backend
                # (under this row's lock); an ordinary save -- an admin form
                # loaded before a producer's first submission -- never
                # overwrites it.
                kwargs["update_fields"] = [field.name for field in self._meta.concrete_fields
                                           if not field.primary_key and field.name != "spoken_content_type"]
            super().save(*args, **kwargs)


class ProducedContentTypeAssignment(models.Model):
    """User U may produce content of type T (scope; the capability is authz's)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="produced_content_assignments")
    content_type = models.ForeignKey(ProducedContentType, on_delete=models.CASCADE, related_name="assignments")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["content_type__sort_order", "content_type__name", "user__username"]
        verbose_name = "produced content assignment"
        constraints = [
            models.UniqueConstraint(fields=["user", "content_type"], name="produced_assignment_unique"),
        ]

    def __str__(self):
        return f"{self.user} → {self.content_type.key}"


TEST_TAKE_RETENTION_DAYS = 7


class ProducedContentTestTake(models.Model):
    """A producer's CURRENT saved take of one exact TEST / NO-AIR type:

        (user, test type) -> one validated ProductionMedia

    Temporary media state, never broadcast history: no spoken item, no
    rendition, no library Track, no log, no active message, no engine command
    -- nothing that could air. Written only by produced_content.no_air
    (binding under ``lock_for_binding``; an optimistic ``revision``; expiry
    ``TEST_TAKE_RETENTION_DAYS`` after the most recent save, then the binding is
    removed and the media goes to ordinary safe production retention)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="produced_content_test_takes")
    content_type = models.ForeignKey(ProducedContentType, on_delete=models.PROTECT, related_name="test_takes")
    media = models.ForeignKey(ProductionMedia, on_delete=models.PROTECT, related_name="produced_content_test_takes")
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["content_type__sort_order", "content_type__name", "user_id"]
        verbose_name = "produced content test take"
        constraints = [
            models.UniqueConstraint(fields=["user", "content_type"], name="produced_test_take_one_per_user_type"),
            models.CheckConstraint(condition=Q(revision__gte=1), name="produced_test_take_revision_positive"),
        ]
        indexes = [models.Index(fields=["expires_at"], name="produced_test_take_expiry")]

    def __str__(self):
        return f"test take of {self.content_type_id} by {self.user_id} (r{self.revision})"
