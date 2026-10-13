"""Fixtures for the active-message domain tests.

Media rows are created directly (no bytes) with production's own test helper,
inside production's IsolatedMediaRootMixin: no test touches the real media
root or the production database.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import models
from django.utils import timezone

from active_messages import services
from active_messages.models import (
    ActiveBroadcastExecution, ActiveBroadcastMessage, ActiveBroadcastMessageEvent, ActiveBroadcastPolicy,
)
from authz.models import GroupRole, Role
from production.models import ProductionMedia
from production.tests.support import VALID_FACTS, IsolatedMediaRootMixin, make_row

User = get_user_model()
DOMAIN_MODELS = (ActiveBroadcastPolicy, ActiveBroadcastMessage, ActiveBroadcastExecution, ActiveBroadcastMessageEvent)


def user_with_role(username, role_name=None, **flags):
    user = User.objects.create_user(username, **flags)
    if role_name:
        group, _ = Group.objects.get_or_create(name=f"test-{role_name}")
        GroupRole.objects.update_or_create(group=group, defaults={"role": Role.objects.get(name=role_name)})
        user.groups.add(group)
    return user


def media(owner=None, *, kind=ProductionMedia.KIND_RECORDING, seconds="30.000000", **overrides):
    values = {**VALID_FACTS, "decoded_duration_seconds": seconds, "validated_at": timezone.now(),
              "owner": owner, "owner_username": owner.get_username() if owner else "", **overrides}
    if values.get("validation_state") == "unvalidated":
        values["validated_at"] = None
        values["validation_code"] = ""
    return make_row(kind=kind, **values)


def raw(model) -> models.QuerySet:
    """Django's base QuerySet: a deliberate guard bypass (= raw SQL) for
    proving database-level invariants and preparing D2B-only states."""
    return models.QuerySet(model=model)


class Domain(IsolatedMediaRootMixin):
    """Users: ``admin`` (Station Administrator role), ``producer`` and
    ``producer2`` (produce only, through a custom role), ``host`` (Remote Host),
    ``nobody`` (no role)."""

    def setUp(self):
        super().setUp()
        produce_only, _ = Role.objects.get_or_create(name="Test Producer")
        from authz.models import Capability, RoleCapability
        RoleCapability.objects.get_or_create(role=produce_only,
                                             capability=Capability.objects.get(slug=services.PRODUCE))
        self.admin = user_with_role("admin", "Station Administrator")
        self.producer = user_with_role("producer", "Test Producer")
        self.producer2 = user_with_role("producer2", "Test Producer")
        self.host = user_with_role("host", "Remote Host")
        self.nobody = user_with_role("nobody")

    # -- builders ------------------------------------------------------------------------------
    def prepared(self, actor=None, *, kind="public_address", seconds="30.000000", **kwargs):
        actor = actor or self.admin
        message = services.prepare_message(actor=actor, kind=kind, **kwargs)
        return services.bind_message_media(message, media(actor, seconds=seconds), actor=actor)

    def active(self, actor=None, *, kind="public_address", now=None, **kwargs):
        message = self.prepared(actor, kind=kind, **kwargs)
        return services.activate_message(message, actor=self.admin, now=now)

    def policy(self):
        return services.current_policy()

    def events(self, message=None, execution=None):
        rows = ActiveBroadcastMessageEvent.objects.all()
        if message is not None:
            rows = rows.filter(message=message)
        if execution is not None:
            rows = rows.filter(execution=execution)
        return list(rows.values_list("event", flat=True))

    def put_on_air(self, execution, *, started_at=None, attempt=None):
        """Move an execution to STARTED as the D2B engine path will (raw: no D1 service can)."""
        import uuid as _uuid
        mix = media(None, kind=ProductionMedia.KIND_RENDITION, derived_from=execution_source(execution))
        started_at = started_at or timezone.now()
        token = attempt or _uuid.uuid4()
        raw(ActiveBroadcastExecution).filter(pk=execution.pk).update(
            state="started", mix_media=mix, ready_at=started_at - timedelta(seconds=3),
            accepted_at=started_at - timedelta(seconds=2), claimed_at=started_at - timedelta(seconds=1),
            started_at=started_at, attempt_token=token, attempt_count=1)
        return ActiveBroadcastExecution.objects.get(pk=execution.pk), token


def execution_source(execution):
    first = execution.composition_snapshot["components"][0]["media"]
    return ProductionMedia.objects.get(pk=first)


DURATION = Decimal("30.000")
