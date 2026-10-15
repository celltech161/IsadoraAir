"""Native produced-content authority (iPortal Phase D3A).

A station defines its producer content TYPES (legacy OGRemote's per-station
categories: ``usd239min``, ``news_item``, ``urgent_pa``, ``public_address``,
``test_audio`` ...) and which users may produce each one:

    ProducedContentType 1--* ProducedContentTypeAssignment *--1 User

Each type has exactly one DELIVERY MODE:

* ``regular`` -- ordinary library content: a target library Category, an
  output mode (``single`` replaces, ``accumulating`` adds) and a LOGICAL
  recipe key. Configuration only: processing is Phase D3C. Never an active
  message.
* ``public_address`` / ``urgent`` -- an active message (active_messages):
  ``recallable`` (may the producer withdraw their own ACTIVE message?),
  ``producer_self_publish`` (may an assigned producer activate their own
  message, or does activation need CONTROL?) -- both default to yes for a
  new type -- and an optional replay interval override (NULL: the
  ActiveBroadcastPolicy default). (The two compatibility types the D3A
  migration creates set both to no explicitly, preserving D1 authority.)
* ``test`` -- explicitly non-airing (no delivery of any kind).

An assignment is SCOPE, not a role: the broad capability still comes from
``authz`` (e.g. ``active_message.produce``); the assignment narrows it to
exact types. CONTROL authority works across enabled types without one.

No field here is executable: no paths, scripts, dotted imports or commands.
Incompatible field combinations are refused by ``clean()`` (forms, admin)
and by database CHECK constraints. ``key`` is the durable identity (history
and execution snapshots record it) and never changes; neither does the
delivery mode of a type that active messages already reference (their
frozen ``kind`` was derived from it).
"""
from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q

KEY_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
# A logical recipe identifier only (D3C maps it to a local, built-in recipe):
# no dots (no dotted imports), no slashes (no paths), no spaces (no commands).
RECIPE_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
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

    def _stored(self):
        if self.pk is None:
            return None
        return type(self).objects.filter(pk=self.pk).values("key", "delivery_mode").first()

    def _referenced_by_messages(self) -> bool:
        related = getattr(self, "active_broadcast_messages", None)
        return related is not None and self.pk is not None and related.exists()

    def _identity_errors(self) -> dict:
        stored = self._stored()
        errors = {}
        if stored is not None:
            if stored["key"] != self.key:
                errors["key"] = "The key is a durable identifier and never changes."
            if stored["delivery_mode"] != self.delivery_mode and self._referenced_by_messages():
                errors["delivery_mode"] = ("Active messages of this type exist; their kind was derived from "
                                           "this delivery mode, so it can no longer change.")
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
        self._apply_mode_defaults()
        errors = self._identity_errors()
        if errors:
            raise ValidationError(errors)
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
