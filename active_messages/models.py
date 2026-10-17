"""The active / urgent / public-address message domain (iPortal Phase D).

Producers' urgent and public-address messages: which are active, the one
aggregate the station airs for the whole active set, when that aggregate is
next due, and what actually happened each time it was meant to air. This
domain owns that policy. It is NOT the spoken-content domain (no SpokenItem,
no library Track, no LogItem) and NOT music accounting (no PlayEvent). Audio
bytes live in the shared ``production`` substrate; every reference points
domain -> ProductionMedia (PROTECT) and is written under
``production.services.retention.lock_for_binding`` (the binding rule).

    ActiveBroadcastPolicy (singleton: generation, gate, cadence, stinger key)
    ActiveBroadcastMessage 1--* ActiveBroadcastMessageEvent  (authoritative history)
            +-- message_media --> ProductionMedia
    ActiveBroadcastExecution 1--* ActiveBroadcastMessageEvent
            +-- mix_media --> ProductionMedia   (bound in D2A)

Every change goes through ``active_messages.services`` (lock order: policy ->
messages in UUID order -> media -> execution). Ordinary ORM writes refuse
(``active_messages.guard``); events are append-only. See docs/ACTIVE_MESSAGES.md.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

from production.models import REFERENCE_KEY_PATTERN, ProductionMedia

from . import guard

SHA256_PATTERN = r"^[0-9a-f]{64}$"
# A durable producer identity, independent of native accounts: "user:<pk>" for
# a native IsadoraAir account; other namespaces are reserved for the Phase G
# import of legacy producers.
PRODUCER_KEY_PATTERN = r"^(user|ogremote):[A-Za-z0-9][A-Za-z0-9_.@+-]{0,127}$"

POLICY_ID = 1
REPLAY_INTERVAL_MIN_SECONDS = 60
REPLAY_INTERVAL_MAX_SECONDS = 86400
DEFAULT_REPLAY_INTERVAL_SECONDS = 1200          # legacy OGRemote default: 20 minutes

# Operator policy limits (ActiveBroadcastPolicy, editable in admin) and the
# ABSOLUTE ceilings no configuration may exceed (docs/ACTIVE_MESSAGES.md,
# "Bounds"). The ceilings are resource and playout safety, enforced by the
# services, the admin form and the database.
DEFAULT_MAX_ACTIVE_MESSAGES = 16
DEFAULT_MAX_MESSAGE_SECONDS = 300
DEFAULT_MAX_AGGREGATE_SECONDS = 600
ABSOLUTE_MAX_ACTIVE_MESSAGES = 24       # one sealed render input each (+ stinger); avg 50 s within 1200 s
ABSOLUTE_MAX_MESSAGE_SECONDS = 600      # a 10-minute "message" is programming, not an announcement
ABSOLUTE_MAX_AGGREGATE_SECONDS = 1200   # exceptional absolute resource/playout ceiling; independent of cadence


class ActorKind(models.TextChoices):
    USER = "user", "User"
    SYSTEM = "system", "System"
    WORKER = "worker", "Worker"
    ENGINE = "engine", "Engine"


# -- ActiveBroadcastPolicy -----------------------------------------------------------------

class PolicyQuerySet(guard.GuardedQuerySet):
    model_label = "the active-message policy"
    user_foreign_keys = guard.user_fk_names("updated_by")


class ActiveBroadcastPolicy(models.Model):
    """The singleton domain policy, and THE serialization root: every change
    to the active composition, every due-execution materialization and every
    evidence application locks this row first.

    ``generation`` increases by exactly one for every change of what the
    aggregate would contain (activation, replacement, clear, expiry) and for
    output-affecting policy changes (stinger, the native-airing gate, the
    three composition limits). Replay alone never changes it.

    The limits are operator policy within code-level absolute ceilings
    (ABSOLUTE_MAX_*), enforced by the services, the admin form validators and
    the database. Media is never truncated to fit a limit. An execution carries the generation it was built
    for; one whose generation is no longer current is stale and never airs.

    ``native_airing_enabled`` is the dormant gate: False by default and on
    every installation; D1 refuses to set it (nothing can air yet). The row is
    created on first use by the services (no data migration)."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=POLICY_ID, editable=False)
    native_airing_enabled = models.BooleanField(default=False)
    generation = models.PositiveBigIntegerField(default=0)
    default_replay_interval_seconds = models.PositiveIntegerField(default=DEFAULT_REPLAY_INTERVAL_SECONDS)
    stinger_key = models.CharField(max_length=64, blank=True, default="")
    max_active_messages = models.PositiveSmallIntegerField(
        default=DEFAULT_MAX_ACTIVE_MESSAGES,
        validators=[MinValueValidator(1), MaxValueValidator(ABSOLUTE_MAX_ACTIVE_MESSAGES)],
        help_text=(f"How many messages may be active together (all producers, both kinds). "
                   f"1..{ABSOLUTE_MAX_ACTIVE_MESSAGES}; default {DEFAULT_MAX_ACTIVE_MESSAGES}. Activation beyond it is "
                   f"refused; an active set above a lowered limit is not aired until messages are cleared."))
    max_message_seconds = models.PositiveIntegerField(
        default=DEFAULT_MAX_MESSAGE_SECONDS,
        validators=[MinValueValidator(1), MaxValueValidator(ABSOLUTE_MAX_MESSAGE_SECONDS)],
        help_text=(f"Longest single message, in seconds of validated decoded audio. "
                   f"1..{ABSOLUTE_MAX_MESSAGE_SECONDS}; default {DEFAULT_MAX_MESSAGE_SECONDS}; at most the aggregate "
                   f"limit. Longer media is refused at binding and activation -- never truncated."))
    max_aggregate_seconds = models.PositiveIntegerField(
        default=DEFAULT_MAX_AGGREGATE_SECONDS,
        validators=[MinValueValidator(1), MaxValueValidator(ABSOLUTE_MAX_AGGREGATE_SECONDS)],
        help_text=(f"Longest aggregate airing, in seconds: every active message plus the stinger when an urgent "
                   f"message uses one. 1..{ABSOLUTE_MAX_AGGREGATE_SECONDS}; default {DEFAULT_MAX_AGGREGATE_SECONDS}. "
                   f"An active set over the limit is not aired (fail closed, operator alert)."))
    updated_at = models.DateTimeField(null=True, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    updated_by_username = models.CharField(max_length=150, blank=True)

    objects = PolicyQuerySet.as_manager()

    class Meta:
        base_manager_name = "objects"
        verbose_name = "active broadcast policy"
        verbose_name_plural = "active broadcast policy"
        constraints = [
            models.CheckConstraint(condition=Q(id=POLICY_ID), name="active_policy_singleton"),
            models.CheckConstraint(
                condition=Q(default_replay_interval_seconds__gte=REPLAY_INTERVAL_MIN_SECONDS)
                & Q(default_replay_interval_seconds__lte=REPLAY_INTERVAL_MAX_SECONDS),
                name="active_policy_interval_bounded"),
            models.CheckConstraint(condition=Q(stinger_key="") | Q(stinger_key__regex=REFERENCE_KEY_PATTERN),
                                   name="active_policy_stinger_key_shape"),
            models.CheckConstraint(
                condition=Q(max_active_messages__gte=1) & Q(max_active_messages__lte=ABSOLUTE_MAX_ACTIVE_MESSAGES),
                name="active_policy_active_limit_bounded"),
            models.CheckConstraint(
                condition=Q(max_message_seconds__gte=1) & Q(max_message_seconds__lte=ABSOLUTE_MAX_MESSAGE_SECONDS),
                name="active_policy_message_limit_bounded"),
            models.CheckConstraint(
                condition=Q(max_aggregate_seconds__gte=1) & Q(max_aggregate_seconds__lte=ABSOLUTE_MAX_AGGREGATE_SECONDS),
                name="active_policy_aggregate_limit_bounded"),
            models.CheckConstraint(condition=Q(max_message_seconds__lte=models.F("max_aggregate_seconds")),
                                   name="active_policy_message_within_aggregate",
                                   violation_error_message="The per-message limit cannot exceed the aggregate limit."),
        ]

    def __str__(self):
        return f"active-message policy (generation {self.generation})"

    def save(self, *args, **kwargs):
        guard.refuse("the active-message policy changes only through active_messages.services")

    def delete(self, *args, **kwargs):
        guard.refuse("the active-message policy is never deleted")


# -- ActiveBroadcastMessage ------------------------------------------------------------------

class MessageKind(models.TextChoices):
    URGENT = "urgent", "Urgent"
    PUBLIC_ADDRESS = "public_address", "Public address"


class MessageState(models.TextChoices):
    PREPARED = "prepared", "Prepared"
    ACTIVE = "active", "Active"
    CLEARED = "cleared", "Cleared"
    EXPIRED = "expired", "Expired"


TERMINAL_MESSAGE_STATES = (MessageState.CLEARED, MessageState.EXPIRED)


class MessageQuerySet(guard.GuardedQuerySet):
    model_label = "active messages"
    user_foreign_keys = guard.user_fk_names("producer", "activated_by", "terminal_by")


class ActiveBroadcastMessage(models.Model):
    """One producer's message lifecycle: PREPARED -> ACTIVE -> CLEARED/EXPIRED.

    A replay is NOT a state: an active message stays ACTIVE while it airs
    again and again; each airing is an ActiveBroadcastExecution. Once ACTIVE,
    ``message_media`` is immutable -- editing an active message means
    preparing a new one and activating it, which supersedes this one.

    Its producer SLOT is the exact ``content_type`` (a ProducedContentType of
    delivery mode urgent or public address, D3A): at most one ACTIVE message
    per (producer_key, content_type), enforced by the database -- so one
    producer may have several public-address types, several urgent types, or
    both, active together, and a replacement supersedes only the same exact
    type. ``kind`` is the FROZEN broad composition class derived from the
    type when the message was prepared; rendering and playout read only
    ``kind`` and the frozen execution snapshot, never the mutable type
    configuration. ``producer_key`` is the durable producer identity (native
    accounts are optional: ``producer`` is SET_NULL, the key and username
    snapshot stay)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=16, choices=MessageKind.choices)
    content_type = models.ForeignKey(
        "produced_content.ProducedContentType", on_delete=models.PROTECT, related_name="active_broadcast_messages",
    )
    state = models.CharField(max_length=10, choices=MessageState.choices, default=MessageState.PREPARED)
    title = models.CharField(max_length=200, blank=True)

    producer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="active_broadcast_messages",
    )
    producer_key = models.CharField(max_length=140)
    producer_username = models.CharField(max_length=150)

    message_media = models.ForeignKey(
        ProductionMedia, null=True, blank=True, on_delete=models.PROTECT, related_name="active_messages",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    state_changed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    activation_generation = models.PositiveBigIntegerField(null=True, blank=True)
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    activated_by_username = models.CharField(max_length=150, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    replay_interval_seconds = models.PositiveIntegerField(null=True, blank=True)
    last_completed_at = models.DateTimeField(null=True, blank=True)
    next_due_at = models.DateTimeField(null=True, blank=True)

    terminal_at = models.DateTimeField(null=True, blank=True)
    terminal_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    terminal_by_username = models.CharField(max_length=150, blank=True)
    terminal_reason = models.CharField(max_length=500, blank=True)
    superseded_by = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="supersedes",
    )

    objects = MessageQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        base_manager_name = "objects"
        constraints = [
            models.CheckConstraint(condition=Q(kind__in=MessageKind.values), name="active_message_kind_valid"),
            models.CheckConstraint(condition=Q(state__in=MessageState.values), name="active_message_state_valid"),
            models.CheckConstraint(condition=Q(producer_key__regex=PRODUCER_KEY_PATTERN),
                                   name="active_message_producer_key_shape"),
            models.CheckConstraint(condition=~Q(producer_username=""), name="active_message_producer_named"),
            models.UniqueConstraint(fields=["producer_key", "content_type"], condition=Q(state="active"),
                                    name="active_message_one_active_per_producer_type"),
            models.CheckConstraint(
                condition=~Q(state="prepared") | Q(
                    activation_generation__isnull=True, activated_at__isnull=True, next_due_at__isnull=True,
                    last_completed_at__isnull=True, replay_interval_seconds__isnull=True, terminal_at__isnull=True),
                name="active_message_prepared_consistent"),
            models.CheckConstraint(
                condition=~Q(state="active") | Q(
                    message_media__isnull=False, activation_generation__isnull=False, activated_at__isnull=False,
                    replay_interval_seconds__isnull=False, next_due_at__isnull=False, terminal_at__isnull=True),
                name="active_message_active_consistent"),
            models.CheckConstraint(condition=~Q(state__in=TERMINAL_MESSAGE_STATES) | Q(terminal_at__isnull=False),
                                   name="active_message_terminal_dated"),
            models.CheckConstraint(
                condition=~Q(state="expired") | Q(activated_at__isnull=False, expires_at__isnull=False),
                name="active_message_expired_was_active"),
            models.CheckConstraint(condition=Q(superseded_by__isnull=True) | Q(state="cleared"),
                                   name="active_message_superseded_is_cleared"),
            models.CheckConstraint(
                condition=Q(replay_interval_seconds__isnull=True)
                | (Q(replay_interval_seconds__gte=REPLAY_INTERVAL_MIN_SECONDS)
                   & Q(replay_interval_seconds__lte=REPLAY_INTERVAL_MAX_SECONDS)),
                name="active_message_interval_bounded"),
            models.CheckConstraint(condition=Q(activation_generation__isnull=True) | Q(activation_generation__gte=1),
                                   name="active_message_generation_positive"),
        ]
        indexes = [
            models.Index(fields=["state", "next_due_at"], name="active_message_due"),
            models.Index(fields=["state", "expires_at"], name="active_message_expiry"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} ({self.content_type_id}) from {self.producer_username} [{self.state}]"

    def save(self, *args, **kwargs):
        guard.refuse("active messages change only through active_messages.services")

    def delete(self, *args, **kwargs):
        guard.refuse("active messages are never deleted; clear them")


# -- ActiveBroadcastExecution ----------------------------------------------------------------

class ExecutionReason(models.TextChoices):
    SCHEDULED = "scheduled", "Due (activation or cadence)"
    MANUAL = "manual", "Replay Now"
    RECONCILE = "reconcile", "Reconcile"


class ExecutionState(models.TextChoices):
    PENDING_RENDER = "pending_render", "Waiting for its aggregate"
    READY = "ready", "Ready"
    ACCEPTED = "accepted", "Accepted by the engine"
    CLAIMED = "claimed", "Claimed (not yet on air)"
    STARTED = "started", "On air"
    COMPLETED = "completed", "Completed"
    SUPPRESSED = "suppressed", "Suppressed"
    FAILED = "failed", "Failed"
    INTERRUPTED = "interrupted", "Interrupted"


UNFINISHED_EXECUTION_STATES = (ExecutionState.PENDING_RENDER, ExecutionState.READY, ExecutionState.ACCEPTED,
                               ExecutionState.CLAIMED, ExecutionState.STARTED)
# Unfinished states the domain owns outright (before the engine has it).
PRESTART_DOMAIN_STATES = (ExecutionState.PENDING_RENDER, ExecutionState.READY)
TERMINAL_EXECUTION_STATES = (ExecutionState.COMPLETED, ExecutionState.SUPPRESSED, ExecutionState.FAILED,
                             ExecutionState.INTERRUPTED)
_HAS_MIX = (ExecutionState.READY, ExecutionState.ACCEPTED, ExecutionState.CLAIMED, ExecutionState.STARTED,
            ExecutionState.COMPLETED, ExecutionState.INTERRUPTED)
_WAS_ACCEPTED = (ExecutionState.ACCEPTED, ExecutionState.CLAIMED, ExecutionState.STARTED,
                 ExecutionState.COMPLETED, ExecutionState.INTERRUPTED)
_WAS_CLAIMED = (ExecutionState.CLAIMED, ExecutionState.STARTED, ExecutionState.COMPLETED,
                ExecutionState.INTERRUPTED)
_WAS_STARTED = (ExecutionState.STARTED, ExecutionState.COMPLETED, ExecutionState.INTERRUPTED)


class ExecutionQuerySet(guard.GuardedQuerySet):
    model_label = "active-message executions"
    user_foreign_keys = guard.user_fk_names("requested_by")


class ActiveBroadcastExecution(models.Model):
    """One logical airing of the aggregate of the active set.

    ``composition_snapshot`` is the canonical, bounded, path-free identity of
    exactly what this airing contains (active_messages.composition), frozen at
    creation; ``composition_digest`` is its SHA-256. ``mix_media`` is the one
    rendered aggregate authorized for this airing (bound in D2A, before
    READY).

    CLAIMED is not air evidence. Only STARTED means a real post-primer
    program buffer crossed the output boundary (D2B).

    At most one execution is unfinished at any time (``open_slot`` is 1 for
    every unfinished execution and NULL otherwise, under a unique constraint):
    only one aggregate can be pending or on air."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reason = models.CharField(max_length=12, choices=ExecutionReason.choices)
    state = models.CharField(max_length=16, choices=ExecutionState.choices, default=ExecutionState.PENDING_RENDER)
    open_slot = models.PositiveSmallIntegerField(null=True, blank=True, default=1, editable=False)

    policy_generation = models.PositiveBigIntegerField()
    composition_digest = models.CharField(max_length=64)
    composition_snapshot = models.JSONField()
    component_count = models.PositiveSmallIntegerField()
    mix_media = models.ForeignKey(
        ProductionMedia, null=True, blank=True, on_delete=models.PROTECT, related_name="active_message_executions",
    )

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    requested_by_username = models.CharField(max_length=150, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    due_at = models.DateTimeField()
    ready_at = models.DateTimeField(null=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    terminal_at = models.DateTimeField(null=True, blank=True)

    attempt_token = models.UUIDField(null=True, blank=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    engine_instance = models.CharField(max_length=64, blank=True)
    confirmed_duration_seconds = models.DecimalField(max_digits=9, decimal_places=3, null=True, blank=True)

    failure_code = models.CharField(max_length=64, blank=True)
    failure_detail = models.CharField(max_length=500, blank=True)

    objects = ExecutionQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        base_manager_name = "objects"
        constraints = [
            models.CheckConstraint(condition=Q(reason__in=ExecutionReason.values), name="active_execution_reason_valid"),
            models.CheckConstraint(condition=Q(state__in=ExecutionState.values), name="active_execution_state_valid"),
            models.UniqueConstraint(fields=["open_slot"], name="active_execution_one_unfinished"),
            models.CheckConstraint(
                # open_slot__isnull=False first: a bare "open_slot = 1" is NULL (and a
                # CHECK passes on NULL) for an unfinished row whose slot is NULL.
                condition=(Q(open_slot__isnull=False) & Q(open_slot=1) & Q(state__in=UNFINISHED_EXECUTION_STATES)
                           & Q(terminal_at__isnull=True))
                | (Q(open_slot__isnull=True) & Q(state__in=TERMINAL_EXECUTION_STATES) & Q(terminal_at__isnull=False)),
                name="active_execution_open_slot_matches_state"),
            models.CheckConstraint(condition=Q(composition_digest__regex=SHA256_PATTERN),
                                   name="active_execution_digest_sha256"),
            models.CheckConstraint(condition=Q(component_count__gte=1)
                                   & Q(component_count__lte=ABSOLUTE_MAX_ACTIVE_MESSAGES),
                                   name="active_execution_components_bounded"),
            models.CheckConstraint(condition=Q(policy_generation__gte=1), name="active_execution_generation_positive"),
            models.CheckConstraint(condition=~Q(state__in=_HAS_MIX) | Q(mix_media__isnull=False, ready_at__isnull=False),
                                   name="active_execution_mix_bound_from_ready"),
            models.CheckConstraint(condition=~Q(state__in=_WAS_ACCEPTED) | Q(accepted_at__isnull=False),
                                   name="active_execution_accepted_dated"),
            models.CheckConstraint(condition=~Q(state__in=_WAS_CLAIMED) | Q(claimed_at__isnull=False),
                                   name="active_execution_claimed_dated"),
            models.CheckConstraint(condition=~Q(state__in=_WAS_STARTED) | Q(started_at__isnull=False),
                                   name="active_execution_started_dated"),
            models.CheckConstraint(condition=Q(started_at__isnull=False) | Q(ended_at__isnull=True,
                                                                            confirmed_duration_seconds__isnull=True),
                                   name="active_execution_air_evidence_needs_start"),
            models.CheckConstraint(
                condition=~Q(state="completed") | Q(ended_at__isnull=False, confirmed_duration_seconds__isnull=False),
                name="active_execution_completed_evidence"),
            models.CheckConstraint(
                condition=Q(ended_at__isnull=True) | Q(started_at__isnull=True) | Q(ended_at__gte=models.F("started_at")),
                name="active_execution_air_ordered"),
            models.CheckConstraint(
                condition=Q(confirmed_duration_seconds__isnull=True) | Q(confirmed_duration_seconds__gte=0),
                name="active_execution_duration_nonnegative"),
            models.CheckConstraint(condition=~Q(state__in=("failed", "suppressed")) | ~Q(failure_code=""),
                                   name="active_execution_failure_coded"),
            models.CheckConstraint(condition=Q(attempt_count__lte=1000), name="active_execution_attempts_bounded"),
        ]
        indexes = [
            models.Index(fields=["state", "created_at"], name="active_execution_state"),
            # D3D: the operator board's newest-first airing history across all
            # states, deterministic on equal timestamps (created_at, id).
            models.Index(fields=["created_at", "id"], name="active_execution_recent"),
            # D3D: "airings this message was part of" -- containment
            # (composition_snapshot @> {"components": [{"message": ...}]}) on
            # the frozen snapshot.
            GinIndex(fields=["composition_snapshot"], opclasses=["jsonb_path_ops"], name="active_execution_members"),
        ]

    def __str__(self):
        return f"{self.get_reason_display()} airing {self.id} [{self.state}]"

    def save(self, *args, **kwargs):
        guard.refuse("active-message executions change only through active_messages.services")

    def delete(self, *args, **kwargs):
        guard.refuse("active-message executions are never deleted")


# -- ActiveBroadcastMessageEvent ---------------------------------------------------------------

class EventQuerySet(guard.GuardedQuerySet):
    model_label = "active-message events (append-only)"
    user_foreign_keys = guard.user_fk_names("actor")


class ActiveBroadcastMessageEvent(models.Model):
    """THE authoritative active-message history. Append-only: written only by
    ``active_messages.services`` in the same transaction as the change it
    records. ``generation`` is the policy generation once the change applied.
    (``monitoring.SystemEvent`` is only for operator attention.)

    Media appear here as identities in ``detail`` (UUID + SHA-256), not as
    foreign keys: a media a PREPARED message stopped using stays purgeable.

    ``request_token`` is the idempotency key of a Replay Now request: one
    event per token, naming the execution that request resolved to."""

    id = models.BigAutoField(primary_key=True)
    message = models.ForeignKey(ActiveBroadcastMessage, null=True, blank=True, on_delete=models.PROTECT,
                                related_name="events")
    execution = models.ForeignKey(ActiveBroadcastExecution, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="events")
    event = models.SlugField(max_length=64)
    from_state = models.CharField(max_length=16, blank=True)
    to_state = models.CharField(max_length=16, blank=True)
    actor_kind = models.CharField(max_length=8, choices=ActorKind.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="+")
    actor_username = models.CharField(max_length=150, blank=True)
    generation = models.PositiveBigIntegerField()
    request_token = models.UUIDField(null=True, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = EventQuerySet.as_manager()

    class Meta:
        ordering = ["id"]
        base_manager_name = "objects"
        constraints = [
            models.CheckConstraint(condition=Q(actor_kind__in=ActorKind.values), name="active_event_actor_kind_valid"),
            models.CheckConstraint(
                condition=Q(message__isnull=False) | Q(execution__isnull=False) | Q(event__startswith="policy_"),
                name="active_event_has_subject"),
            models.UniqueConstraint(fields=["request_token"], condition=Q(request_token__isnull=False),
                                    name="active_event_one_per_request_token"),
            models.CheckConstraint(condition=Q(request_token__isnull=True) | Q(execution__isnull=False),
                                   name="active_event_request_names_execution"),
        ]
        indexes = [
            models.Index(fields=["message", "id"], name="active_event_message"),
            models.Index(fields=["execution", "id"], name="active_event_execution"),
        ]

    def __str__(self):
        return f"{self.event} {self.from_state}->{self.to_state}"

    def save(self, *args, **kwargs):
        guard.refuse("active-message events are written only by active_messages.services")

    def delete(self, *args, **kwargs):
        guard.refuse("active-message events are append-only")


# -- playout representation (iPortal Phase D2B) ------------------------------------------------
#
# A READY aggregate airs the way every other station element does: as an
# ordinary library Track in the UrgentPA category, inserted by the existing
# urgent splice and played by the existing A/B decks. These two rows are the
# DURABLE correlation between the domain and that ordinary playout -- never
# authority over playback itself (the deck's own LogItem/PlayEvent evidence
# is):
#
#     ActiveBroadcastExecution 1--1 ActiveBroadcastPublication --> library.Track
#                              1--* ActiveBroadcastInsertion   --> library.LogItem
#
# Both reference library rows SET_NULL with an immutable id snapshot: a Track
# can be deleted by an operator and a LogItem is pruned with its PlaylistLog,
# while the correlation history must survive.

PUBLICATION_RELPATH_PATTERN = r"^UrgentPA/am-[0-9a-f]{32}\.flac$"


class PublicationQuerySet(guard.GuardedQuerySet):
    model_label = "active-message publications"
    # Django's SET_NULL when the library Track is deleted (the snapshot stays).
    user_foreign_keys = guard.user_fk_names("track")


class ActiveBroadcastPublication(models.Model):
    """The exact library representation of one execution's aggregate: the
    execution-specific, immutable file ``UrgentPA/am-<execution hex>.flac``
    holding exactly ``media``'s bytes, and the ordinary Track for it."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    execution = models.OneToOneField(ActiveBroadcastExecution, on_delete=models.PROTECT, related_name="publication")
    media = models.ForeignKey(ProductionMedia, on_delete=models.PROTECT, related_name="active_message_publications")
    library_relpath = models.CharField(max_length=255, unique=True)
    sha256 = models.CharField(max_length=64)
    track = models.ForeignKey("library.Track", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    track_id_snapshot = models.BigIntegerField(unique=True)
    published_at = models.DateTimeField()

    objects = PublicationQuerySet.as_manager()

    class Meta:
        ordering = ["-published_at"]
        base_manager_name = "objects"
        constraints = [
            models.CheckConstraint(condition=Q(library_relpath__regex=PUBLICATION_RELPATH_PATTERN),
                                   name="active_publication_relpath_shape"),
            models.CheckConstraint(condition=Q(sha256__regex=SHA256_PATTERN), name="active_publication_sha256"),
        ]

    def __str__(self):
        return f"publication of {self.execution_id} as {self.library_relpath}"

    def save(self, *args, **kwargs):
        guard.refuse("active-message publications are written only by active_messages.playout")

    def delete(self, *args, **kwargs):
        guard.refuse("active-message publications are never deleted")


class InsertionQuerySet(guard.GuardedQuerySet):
    model_label = "active-message insertions"
    # Django's SET_NULL when the LogItem is pruned with its PlaylistLog.
    user_foreign_keys = guard.user_fk_names("log_item")


class ActiveBroadcastInsertion(models.Model):
    """One LogItem the engine spliced for one dispatch ATTEMPT of an
    execution (written in the engine's own splice transaction). Only the
    insertion of the execution's CURRENT attempt may open a deck."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    execution = models.ForeignKey(ActiveBroadcastExecution, on_delete=models.PROTECT, related_name="insertions")
    publication = models.ForeignKey(ActiveBroadcastPublication, on_delete=models.PROTECT, related_name="insertions")
    attempt_token = models.UUIDField(unique=True)
    log_item = models.ForeignKey("library.LogItem", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="+")
    log_item_id_snapshot = models.BigIntegerField(unique=True)
    engine_instance = models.CharField(max_length=64)
    inserted_at = models.DateTimeField()

    objects = InsertionQuerySet.as_manager()

    class Meta:
        ordering = ["-inserted_at"]
        base_manager_name = "objects"
        constraints = [
            models.CheckConstraint(condition=~Q(engine_instance=""), name="active_insertion_engine_named"),
        ]

    def __str__(self):
        return f"insertion {self.attempt_token} of {self.execution_id} as log item {self.log_item_id_snapshot}"

    def save(self, *args, **kwargs):
        guard.refuse("active-message insertions are written only by active_messages.playout")

    def delete(self, *args, **kwargs):
        guard.refuse("active-message insertions are never deleted")
