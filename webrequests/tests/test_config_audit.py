import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib import admin as django_admin
from django.contrib.auth.models import User
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from isadoraair.tts.models import StationTTSVoice
from monitoring.models import SystemEvent
from webrequests.admin import SongRequestAdmin, WebRequestConfigAdmin
from webrequests.config_audit import (
    WEB_REQUEST_CONFIG_APPLY_MODES,
    WEB_REQUEST_CONFIG_AUDIT_FIELDS,
    WEB_REQUEST_REDACTED_FIELDS,
    WEB_REQUEST_STAFF_API_FIELDS,
)
from webrequests.models import SongRequest, WebRequestConfig


PRIVATE_EMAIL = "private-webrequests-operator@example.invalid"
PRIVATE_TEMPLATE = (
    "Private campaign wording token-DO-NOT-RETAIN for {title} by {artist}."
)


def _request(username="webrequests-audit-operator"):
    return SimpleNamespace(
        user=SimpleNamespace(get_username=lambda: username)
    )


def _audit_events():
    return [
        event
        for event in SystemEvent.objects.order_by("pk")
        if event.detail.get("event_type") == "configuration_change"
        and event.detail.get("object_type")
        == "webrequests.WebRequestConfig"
    ]


class WebRequestConfigAdminAuditTests(TestCase):
    def setUp(self):
        self.admin = WebRequestConfigAdmin(
            WebRequestConfig, django_admin.AdminSite()
        )
        self.config = WebRequestConfig.load()

    def _save(self, updates=None, *, change=True, execute=True):
        updates = updates or {}
        for field, value in updates.items():
            setattr(self.config, field, value)
        with self.captureOnCommitCallbacks(execute=execute) as callbacks:
            self.admin.save_model(
                _request(), self.config,
                SimpleNamespace(changed_data=list(updates)), change=change,
            )
        return callbacks

    def test_allowlist_covers_exact_config_model_fields(self):
        model_fields = {
            field.name
            for field in WebRequestConfig._meta.concrete_fields
            if field.name != "id"
        }
        self.assertEqual(set(WEB_REQUEST_CONFIG_AUDIT_FIELDS), model_fields)

    def test_admission_and_timing_fields_retain_safe_values(self):
        cases = (
            ("enabled", True),
            ("max_fulfilled_per_hour", 7),
            ("lookahead_warning_minutes", 75),
            ("expire_after_hours", 9),
        )
        for field, value in cases:
            with self.subTest(field=field):
                old = getattr(self.config, field)
                self._save({field: value})
                event = _audit_events()[0]
                self.assertEqual(event.detail["changed_fields"], [field])
                self.assertEqual(
                    event.detail["changes"][field],
                    {"old": old, "new": value},
                )
                self.assertEqual(
                    event.detail["apply_modes"][field],
                    "next_web_request_evaluation",
                )
                SystemEvent.objects.all().delete()

    def test_dedication_numeric_fields_apply_to_next_tts_attempt(self):
        self._save({
            "dedication_tts_timeout_seconds": 45,
            "dedication_message_spoken_limit": 240,
        })
        event = _audit_events()[0]
        self.assertEqual(
            event.detail["changed_fields"],
            [
                "dedication_tts_timeout_seconds",
                "dedication_message_spoken_limit",
            ],
        )
        self.assertEqual(
            set(event.detail["apply_modes"].values()),
            {"next_dedication_tts_attempt"},
        )

    def test_tts_voice_uses_only_id_and_logical_name(self):
        voice = StationTTSVoice.objects.create(
            name="dedication_voice",
            enabled=True,
            engine=StationTTSVoice.Engine.KOKORO,
            provider_voice="af_jessica",
        )
        self._save({"dedication_tts_voice": voice})
        event = _audit_events()[0]
        self.assertEqual(
            event.detail["changes"]["dedication_tts_voice"]["new"],
            {"id": voice.pk, "name": "dedication_voice"},
        )
        retained = repr(event.detail)
        self.assertNotIn("af_jessica", retained)
        self.assertNotIn("provider_voice", retained)

    def test_templates_are_identified_but_bodies_are_omitted(self):
        updates = {
            field: PRIVATE_TEMPLATE.replace("token", field)
            for field in (
                "dedication_named_message_template",
                "dedication_named_request_template",
                "dedication_anonymous_message_template",
                "dedication_anonymous_request_template",
            )
        }
        callbacks = self._save(updates, execute=False)
        self.assertEqual(_audit_events(), [])
        self.assertEqual(len(callbacks), 1)
        for value in updates.values():
            self.assertNotIn(value, repr(callbacks[0]))
        callbacks[0]()
        event = _audit_events()[0]
        self.assertEqual(event.detail["changed_fields"], list(updates))
        self.assertEqual(event.detail["changes"], {})
        self.assertEqual(event.detail["redacted_fields"], list(updates))
        for value in updates.values():
            self.assertNotIn(value, repr((event.detail, event.dedupe_key)))

    def test_notify_email_is_fully_redacted_and_not_deferred(self):
        callbacks = self._save(
            {"notify_email": PRIVATE_EMAIL}, execute=False
        )
        self.assertEqual(_audit_events(), [])
        self.assertEqual(len(callbacks), 1)
        self.assertNotIn(PRIVATE_EMAIL, repr(callbacks[0]))
        callbacks[0]()
        event = _audit_events()[0]
        self.assertEqual(event.detail["changed_fields"], ["notify_email"])
        self.assertEqual(event.detail["redacted_fields"], ["notify_email"])
        self.assertNotIn("notify_email", event.detail["changes"])
        self.assertNotIn(PRIVATE_EMAIL, repr((event.detail, event.dedupe_key)))

    def test_schedule_uses_counts_not_the_full_grid(self):
        self._save({"open_slots": [0, 1, 24, 167]})
        change = _audit_events()[0].detail["changes"]["open_slots"]
        self.assertEqual(change, {
            "old_open_slot_count": 0,
            "new_open_slot_count": 4,
            "changed_slot_count": 4,
        })
        self.assertNotIn("[0, 1, 24, 167]", repr(_audit_events()[0].detail))

    def test_multiple_fields_emit_one_event_with_per_field_modes(self):
        self._save({
            "enabled": True,
            "notify_email": PRIVATE_EMAIL,
            "dedication_tts_timeout_seconds": 40,
        })
        events = _audit_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].detail["changed_fields"],
            ["enabled", "notify_email", "dedication_tts_timeout_seconds"],
        )
        self.assertEqual(
            events[0].detail["apply_modes"],
            {
                "enabled": "next_web_request_evaluation",
                "notify_email": "next_web_request_notification_attempt",
                "dedication_tts_timeout_seconds": "next_dedication_tts_attempt",
            },
        )

    def test_noop_is_silent_and_event_waits_for_commit(self):
        self._save()
        self.assertEqual(_audit_events(), [])
        callbacks = self._save({"enabled": True}, execute=False)
        self.assertEqual(_audit_events(), [])
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(len(_audit_events()), 1)

    def test_rollback_emits_nothing_and_restores_config(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    self.config.enabled = True
                    self.admin.save_model(
                        _request(), self.config,
                        SimpleNamespace(changed_data=["enabled"]), change=True,
                    )
                    raise RuntimeError("force rollback")
            except RuntimeError:
                pass
        self.config.refresh_from_db()
        self.assertFalse(self.config.enabled)
        self.assertEqual(_audit_events(), [])

    def test_rapid_saves_are_distinct_and_claim_no_runtime_success(self):
        self._save({"enabled": True})
        self._save({"enabled": False})
        events = _audit_events()
        self.assertEqual(len(events), 2)
        self.assertNotEqual(events[0].dedupe_key, events[1].dedupe_key)
        retained = repr(events).lower()
        for false_claim in (
            "request accepted", "track scheduled", "tts succeeded",
            "audio played", "notification sent",
        ):
            self.assertNotIn(false_claim, retained)

    def test_emitter_failure_does_not_break_admin_persistence(self):
        with patch(
            "monitoring.services.config_audit.emit_event",
            side_effect=RuntimeError("audit unavailable"),
        ), self.captureOnCommitCallbacks(execute=True):
            self.config.enabled = True
            self.admin.save_model(
                _request(), self.config,
                SimpleNamespace(changed_data=["enabled"]), change=True,
            )
        self.config.refresh_from_db()
        self.assertTrue(self.config.enabled)
        self.assertEqual(_audit_events(), [])

    def test_lazy_creation_is_silent_and_explicit_creation_is_audited(self):
        self.config.delete()
        WebRequestConfig.load()
        self.assertEqual(_audit_events(), [])

        WebRequestConfig.objects.all().delete()
        self.config = WebRequestConfig()
        self._save(change=False)
        event = _audit_events()[0]
        self.assertEqual(event.detail["action"], "create")
        self.assertEqual(
            event.detail["changed_fields"], list(WEB_REQUEST_CONFIG_AUDIT_FIELDS)
        )
        self.assertEqual(
            set(event.detail["redacted_fields"]), WEB_REQUEST_REDACTED_FIELDS
        )

    def test_song_request_admin_runtime_edit_is_not_config_audited(self):
        song_request = SongRequest.objects.create(
            external_request_id="runtime-request-1",
            submitted_at=timezone.now(),
        )
        song_request.status = "expired"
        SongRequestAdmin(SongRequest, django_admin.AdminSite()).save_model(
            _request(), song_request,
            SimpleNamespace(changed_data=["status"]), change=True,
        )
        self.assertEqual(_audit_events(), [])


@override_settings(SECURE_SSL_REDIRECT=False)
class StaffConfigAPIAuditTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser(
            "staff-config-operator", "staff@example.invalid", "pw"
        )
        self.client.force_login(self.staff)
        self.config = WebRequestConfig.load()
        self.url = reverse("webrequests:api-config")

    def _patch(self, payload, *, execute=True):
        with self.captureOnCommitCallbacks(execute=execute) as callbacks:
            response = self.client.generic(
                "PATCH", self.url, data=json.dumps(payload),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        return callbacks

    def test_single_and_multiple_changes_use_staff_source_and_actor(self):
        self._patch({"enabled": True})
        event = _audit_events()[0]
        self.assertEqual(event.detail["changed_fields"], ["enabled"])
        self.assertEqual(event.detail["changed_by"], "staff-config-operator")
        self.assertEqual(event.source, "webrequests_staff_config_api")
        self.assertEqual(
            event.detail["change_source"], "webrequests_staff_config_api"
        )
        SystemEvent.objects.all().delete()

        self._patch({
            "max_fulfilled_per_hour": 8,
            "lookahead_warning_minutes": 90,
            "expire_after_hours": 12,
        })
        events = _audit_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].detail["changed_fields"],
            [
                "max_fulfilled_per_hour",
                "lookahead_warning_minutes",
                "expire_after_hours",
            ],
        )
        self.assertEqual(
            set(events[0].detail["changed_fields"]),
            set(WEB_REQUEST_STAFF_API_FIELDS) - {"enabled"},
        )

    def test_effective_noop_and_invalid_json_emit_none(self):
        self._patch({"enabled": False, "max_fulfilled_per_hour": 4})
        self.assertEqual(_audit_events(), [])
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.generic(
                "PATCH", self.url, data="not-json",
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(_audit_events(), [])

    def test_patch_waits_for_commit_and_rollback_emits_none(self):
        callbacks = self._patch({"enabled": True}, execute=False)
        self.assertEqual(_audit_events(), [])
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(len(_audit_events()), 1)

        SystemEvent.objects.all().delete()
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    response = self.client.generic(
                        "PATCH", self.url,
                        data=json.dumps({"enabled": False}),
                        content_type="application/json",
                    )
                    self.assertEqual(response.status_code, 200)
                    raise RuntimeError("force rollback")
            except RuntimeError:
                pass
        self.config.refresh_from_db()
        self.assertTrue(self.config.enabled)
        self.assertEqual(_audit_events(), [])

    def test_emitter_failure_does_not_break_api_persistence(self):
        with patch(
            "monitoring.services.config_audit.emit_event",
            side_effect=RuntimeError("audit unavailable"),
        ):
            self._patch({"enabled": True})
        self.config.refresh_from_db()
        self.assertTrue(self.config.enabled)
        self.assertEqual(_audit_events(), [])

    def test_unauthorized_patch_does_not_change_or_audit(self):
        nonstaff = User.objects.create_user(
            "nonstaff-config-user", "nonstaff@example.invalid", "pw"
        )
        self.client.force_login(nonstaff)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.generic(
                "PATCH", self.url, data=json.dumps({"enabled": True}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 403)
        self.config.refresh_from_db()
        self.assertFalse(self.config.enabled)
        self.assertEqual(_audit_events(), [])


@override_settings(SECURE_SSL_REDIRECT=False)
class ScheduleToggleAuditTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser(
            "grid-config-operator", "grid@example.invalid", "pw"
        )
        self.client.force_login(self.staff)
        self.config = WebRequestConfig.load()

    def _post(self, url_name, payload, *, execute=True):
        with self.captureOnCommitCallbacks(execute=execute) as callbacks:
            response = self.client.post(
                reverse(f"webrequests:{url_name}"),
                data=json.dumps(payload), content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        return response, callbacks

    def test_single_slot_records_compact_coordinates_actor_and_source(self):
        self._post("api-open-slot-toggle", {"slot": 38})
        event = _audit_events()[0]
        self.assertEqual(event.detail["changed_fields"], ["open_slots"])
        self.assertEqual(event.detail["changed_by"], "grid-config-operator")
        self.assertEqual(event.source, "webrequests_schedule_slot_toggle")
        self.assertEqual(
            event.detail["changes"]["open_slots"],
            {
                "operation": "slot_toggle",
                "day_of_week": 1,
                "day": "tuesday",
                "hour": 14,
                "old": False,
                "new": True,
            },
        )
        self.assertEqual(
            event.detail["apply_modes"]["open_slots"],
            "next_request_window_evaluation",
        )

    def test_single_slot_rollback_and_rapid_inverse_actions(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    response = self.client.post(
                        reverse("webrequests:api-open-slot-toggle"),
                        data=json.dumps({"slot": 5}),
                        content_type="application/json",
                    )
                    self.assertEqual(response.status_code, 200)
                    raise RuntimeError("force rollback")
            except RuntimeError:
                pass
        self.config.refresh_from_db()
        self.assertEqual(self.config.open_slots, [])
        self.assertEqual(_audit_events(), [])

        self._post("api-open-slot-toggle", {"slot": 5})
        self._post("api-open-slot-toggle", {"slot": 5})
        events = _audit_events()
        self.assertEqual(len(events), 2)
        self.assertEqual(
            [event.detail["changes"]["open_slots"]["new"] for event in events],
            [True, False],
        )
        self.assertNotEqual(events[0].dedupe_key, events[1].dedupe_key)

    def test_day_toggle_is_one_aggregate_event_with_actual_count(self):
        self.config.open_slots = [24, 25, 100]
        self.config.save(update_fields=["open_slots"])
        self._post("api-open-slot-toggle-row", {"day_of_week": 1})
        events = _audit_events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.source, "webrequests_schedule_day_toggle")
        self.assertEqual(event.detail["changed_by"], "grid-config-operator")
        self.assertEqual(
            event.detail["changes"]["open_slots"],
            {
                "operation": "day_toggle",
                "day_of_week": 1,
                "day": "tuesday",
                "new_state": False,
                "affected_slots": 2,
            },
        )
        self.assertNotIn("slots", event.detail["changes"]["open_slots"])

    def test_day_toggle_rollback_emits_none(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    response = self.client.post(
                        reverse("webrequests:api-open-slot-toggle-row"),
                        data=json.dumps({"day_of_week": 0}),
                        content_type="application/json",
                    )
                    self.assertEqual(response.status_code, 200)
                    raise RuntimeError("force rollback")
            except RuntimeError:
                pass
        self.config.refresh_from_db()
        self.assertEqual(self.config.open_slots, [])
        self.assertEqual(_audit_events(), [])

    def test_hour_toggle_is_one_aggregate_event_with_actual_count(self):
        self.config.open_slots = [3, 27, 100]
        self.config.save(update_fields=["open_slots"])
        self._post("api-open-slot-toggle-column", {"hour": 3})
        events = _audit_events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.source, "webrequests_schedule_hour_toggle")
        self.assertEqual(event.detail["changed_by"], "grid-config-operator")
        self.assertEqual(
            event.detail["changes"]["open_slots"],
            {
                "operation": "hour_toggle",
                "hour": 3,
                "new_state": False,
                "affected_slots": 2,
            },
        )
        self.assertNotIn("slots", event.detail["changes"]["open_slots"])

    def test_hour_toggle_rollback_emits_none(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    response = self.client.post(
                        reverse("webrequests:api-open-slot-toggle-column"),
                        data=json.dumps({"hour": 7}),
                        content_type="application/json",
                    )
                    self.assertEqual(response.status_code, 200)
                    raise RuntimeError("force rollback")
            except RuntimeError:
                pass
        self.config.refresh_from_db()
        self.assertEqual(self.config.open_slots, [])
        self.assertEqual(_audit_events(), [])
