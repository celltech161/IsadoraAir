from types import SimpleNamespace
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase

from hardware.models import AudioInput
from monitoring.models import SystemEvent
from monitoring.services.config_audit import emit_config_change_event


def _request(username="operator"):
    return SimpleNamespace(
        user=SimpleNamespace(get_username=lambda: username)
    )


def _emit(**overrides):
    facts = {
        "category": "hardware",
        "title": "Audio input configuration updated",
        "action": "update",
        "object_type": "hardware.AudioInput",
        "object_id": 7,
        "object_name": "Studio Mic",
        "changed_fields": ["gain_db"],
        "changes": {"gain_db": {"old": 0.0, "new": 3.0}},
        "redacted_fields": [],
        "request": _request(),
        "apply_modes": {"gain_db": "engine_restart_required"},
        "restart_required": True,
    }
    facts.update(overrides)
    return emit_config_change_event(**facts)


class ConfigurationAuditHelperTests(TestCase):
    def test_info_event_retains_actor_and_caller_approved_safe_values(self):
        with self.captureOnCommitCallbacks(execute=True):
            _emit()

        event = SystemEvent.objects.get()
        self.assertEqual(event.category, "hardware")
        self.assertEqual(event.level, "info")
        self.assertEqual(event.source, "django_admin")
        self.assertEqual(event.detail["event_type"], "configuration_change")
        self.assertEqual(event.detail["changed_by"], "operator")
        self.assertEqual(
            event.detail["changes"],
            {"gain_db": {"old": 0.0, "new": 3.0}},
        )
        self.assertTrue(event.detail["restart_required"])

    def test_unavailable_request_does_not_invent_an_operator(self):
        with self.captureOnCommitCallbacks(execute=True):
            _emit(request=None)
        self.assertNotIn("changed_by", SystemEvent.objects.get().detail)

    def test_explicitly_redacted_value_never_enters_event_or_key(self):
        secret = "never-store-this-token"
        with self.captureOnCommitCallbacks(execute=True):
            _emit(
                title="Safe configuration updated",
                changed_fields=["gain_db", "api_token"],
                changes={
                    "gain_db": {"old": 0.0, "new": 3.0},
                    "api_token": {"old": "old", "new": secret},
                },
                redacted_fields=["api_token"],
            )

        event = SystemEvent.objects.get()
        retained = f"{event.title!r} {event.detail!r} {event.dedupe_key!r}"
        self.assertNotIn(secret, retained)
        self.assertNotIn("old", repr(event.detail["changes"].get("api_token")))
        self.assertNotIn("api_token", event.detail["changes"])
        self.assertEqual(event.detail["redacted_fields"], ["api_token"])

    def test_rapid_distinct_transactions_do_not_coalesce(self):
        with self.captureOnCommitCallbacks(execute=True):
            _emit()
            _emit(changes={"gain_db": {"old": 3.0, "new": 4.0}})

        events = list(SystemEvent.objects.order_by("pk"))
        self.assertEqual(len(events), 2)
        self.assertNotEqual(events[0].dedupe_key, events[1].dedupe_key)
        self.assertEqual([event.repeat_count for event in events], [1, 1])

    def test_event_is_deferred_until_commit_callback(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            _emit()
            self.assertFalse(SystemEvent.objects.exists())
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(SystemEvent.objects.count(), 1)

    def test_rolled_back_transaction_creates_no_event(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    _emit()
                    raise RuntimeError("force rollback")
            except RuntimeError:
                pass
        self.assertFalse(SystemEvent.objects.exists())

    def test_event_detail_is_frozen_before_callback_registration(self):
        changes = {"gain_db": {"old": 0.0, "new": 3.0}}
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            _emit(changes=changes)
            changes["gain_db"]["new"] = 99.0
        callbacks[0]()
        self.assertEqual(
            SystemEvent.objects.get().detail["changes"]["gain_db"]["new"],
            3.0,
        )

    def test_emitter_failure_cannot_fail_configuration_transaction(self):
        with patch(
            "monitoring.services.config_audit.emit_event",
            side_effect=RuntimeError("audit unavailable"),
        ), self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                saved = AudioInput.objects.create(name="Saved despite audit failure")
                _emit(object_id=saved.pk, object_name=saved.name)

        self.assertTrue(AudioInput.objects.filter(pk=saved.pk).exists())
        self.assertFalse(SystemEvent.objects.exists())
