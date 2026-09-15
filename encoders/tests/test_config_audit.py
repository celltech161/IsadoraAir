from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from encoders.admin import EncoderAdmin
from encoders.models import Encoder, RUNTIME_AFFECTING_FIELDS
from monitoring.models import SystemEvent


PASSWORD_BEFORE = "AUDIT_TEST_PASSWORD_BEFORE_ONLY"
PASSWORD_AFTER = "AUDIT_TEST_PASSWORD_AFTER_ONLY"
URL_BEFORE = "https://example.invalid/station?audit_test_value=before"
URL_AFTER = "https://example.invalid/station?audit_test_value=after"

DEFAULTS = {
    "name": "audit-encoder",
    "enabled": True,
    "protocol": "shoutcast2",
    "host": "192.0.2.10",
    "port": 8000,
    "mount": "/4",
    "username": "source",
    "password": PASSWORD_BEFORE,
    "format": "mp3",
    "bitrate_kbps": 192,
    "provider": "generic",
    "mp3_rate_mode": "auto",
    "input_device": "",
    "station_name": "Audit Test Station",
    "genre": "Test",
    "description": "",
    "url": URL_BEFORE,
    "public": False,
    "sort_order": 0,
}


def make_encoder(**overrides):
    values = dict(DEFAULTS)
    values.update(overrides)
    return Encoder.objects.create(**values)


def audit_events():
    return [
        event
        for event in SystemEvent.objects.order_by("pk")
        if event.detail.get("event_type") == "configuration_change"
        and event.detail.get("object_type") == "encoders.Encoder"
    ]


def admin_form(*, enabled, changed_data):
    return SimpleNamespace(
        initial={"enabled": enabled},
        changed_data=list(changed_data),
    )


class EncoderConfigurationAuditSaveTests(TestCase):
    def setUp(self):
        self.admin = EncoderAdmin(Encoder, None)

    def _save(self, obj, *, change, changed_data, was_enabled=None, request=None):
        if was_enabled is None:
            was_enabled = obj.enabled
        form = admin_form(enabled=was_enabled, changed_data=changed_data)
        with patch("encoders.admin._notify_reconciliation_pending") as notify, \
             self.captureOnCommitCallbacks(execute=True):
            self.admin.save_model(request, obj, form, change=change)
        return notify

    def test_bitrate_change_emits_one_desired_configuration_event(self):
        obj = make_encoder(bitrate_kbps=192)
        obj.bitrate_kbps = 256
        notify = self._save(obj, change=True, changed_data=["bitrate_kbps"])

        event = audit_events()[0]
        self.assertEqual(event.category, "encoder")
        self.assertEqual(event.level, "info")
        self.assertEqual(event.title, "Encoder configuration updated")
        self.assertEqual(event.detail["action"], "update")
        self.assertEqual(event.detail["changed_fields"], ["bitrate_kbps"])
        self.assertEqual(
            event.detail["changes"]["bitrate_kbps"],
            {"old": 192, "new": 256},
        )
        self.assertEqual(
            event.detail["apply_modes"]["bitrate_kbps"],
            "desired_configuration_saved_pending_encoder_manager_reconciliation",
        )
        self.assertFalse(event.detail["restart_required"])
        notify.assert_called_once()

    def test_connection_fields_emit_one_event_with_exact_safe_diffs(self):
        obj = make_encoder(
            protocol="shoutcast2", host="192.0.2.10", port=8000, mount="/4"
        )
        obj.protocol = "icecast"
        obj.host = "stream.example.invalid"
        obj.port = 8443
        obj.mount = "/main"
        self._save(
            obj,
            change=True,
            changed_data=["protocol", "host", "port", "mount"],
        )

        events = audit_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].detail["changed_fields"],
            ["protocol", "host", "port", "mount"],
        )
        self.assertEqual(
            events[0].detail["changes"]["host"],
            {"old": "192.0.2.10", "new": "stream.example.invalid"},
        )

    def test_multiple_runtime_fields_still_emit_one_event(self):
        obj = make_encoder()
        obj.format = "aac"
        obj.bitrate_kbps = 128
        obj.station_name = "Committed Desired Station"
        self._save(
            obj,
            change=True,
            changed_data=["format", "bitrate_kbps", "station_name"],
        )
        self.assertEqual(len(audit_events()), 1)
        self.assertEqual(
            audit_events()[0].detail["changed_fields"],
            ["format", "bitrate_kbps", "station_name"],
        )

    def test_enabled_change_is_audited_without_claiming_process_stopped(self):
        obj = make_encoder(enabled=True)
        obj.enabled = False
        self._save(
            obj,
            change=True,
            changed_data=["enabled"],
            was_enabled=True,
        )
        detail = audit_events()[0].detail
        self.assertEqual(detail["changes"]["enabled"], {"old": True, "new": False})
        retained = repr(detail).lower()
        self.assertNotIn("stopped successfully", retained)
        self.assertNotIn("started successfully", retained)

    def test_password_change_records_only_field_name_and_redaction(self):
        obj = make_encoder(password=PASSWORD_BEFORE)
        obj.password = PASSWORD_AFTER
        self._save(obj, change=True, changed_data=["password"])

        event = audit_events()[0]
        self.assertEqual(event.detail["changed_fields"], ["password"])
        self.assertEqual(event.detail["redacted_fields"], ["password"])
        self.assertNotIn("password", event.detail["changes"])
        retained = f"{event.title!r} {event.detail!r} {event.dedupe_key!r}"
        self.assertNotIn(PASSWORD_BEFORE, retained)
        self.assertNotIn(PASSWORD_AFTER, retained)

    def test_safe_field_and_password_share_one_event_without_secret_values(self):
        obj = make_encoder(host="192.0.2.10", password=PASSWORD_BEFORE)
        obj.host = "192.0.2.20"
        obj.password = PASSWORD_AFTER
        self._save(obj, change=True, changed_data=["host", "password"])

        events = audit_events()
        self.assertEqual(len(events), 1)
        detail = events[0].detail
        self.assertEqual(detail["changed_fields"], ["host", "password"])
        self.assertEqual(
            detail["changes"],
            {"host": {"old": "192.0.2.10", "new": "192.0.2.20"}},
        )
        self.assertEqual(detail["redacted_fields"], ["password"])

    def test_url_change_is_conservatively_redacted(self):
        obj = make_encoder(url=URL_BEFORE)
        obj.url = URL_AFTER
        self._save(obj, change=True, changed_data=["url"])

        event = audit_events()[0]
        self.assertEqual(event.detail["changed_fields"], ["url"])
        self.assertEqual(event.detail["redacted_fields"], ["url"])
        self.assertNotIn("url", event.detail["changes"])
        retained = f"{event.title!r} {event.detail!r} {event.dedupe_key!r}"
        self.assertNotIn(URL_BEFORE, retained)
        self.assertNotIn(URL_AFTER, retained)

    def test_noop_sort_description_and_name_only_updates_emit_none(self):
        cases = (
            ("noop", None, None),
            ("sort_order", "sort_order", 9),
            ("description", "description", "new description"),
            ("name", "name", "renamed-display-label"),
        )
        for label, field, new_value in cases:
            with self.subTest(label=label):
                SystemEvent.objects.all().delete()
                obj = make_encoder(name=f"audit-{label}")
                if field is not None:
                    setattr(obj, field, new_value)
                self._save(
                    obj,
                    change=True,
                    changed_data=[] if field is None else [field],
                )
                self.assertEqual(audit_events(), [])

    def test_audited_change_uses_committed_display_name(self):
        obj = make_encoder(name="old display", bitrate_kbps=192)
        obj.name = "new display"
        obj.bitrate_kbps = 256
        self._save(
            obj,
            change=True,
            changed_data=["name", "bitrate_kbps"],
        )
        event = audit_events()[0]
        self.assertEqual(event.detail["object_name"], "new display")
        self.assertEqual(event.detail["changed_fields"], ["bitrate_kbps"])

    def test_disabled_row_change_is_saved_without_false_reconciliation_claim(self):
        obj = make_encoder(enabled=False, bitrate_kbps=192)
        obj.bitrate_kbps = 256
        notify = self._save(
            obj,
            change=True,
            changed_data=["bitrate_kbps"],
            was_enabled=False,
        )
        event = audit_events()[0]
        self.assertEqual(
            event.detail["apply_modes"]["bitrate_kbps"],
            "desired_configuration_saved_disabled_no_reconciliation",
        )
        notify.assert_not_called()

    def test_create_records_effective_runtime_configuration_and_redacts_private_values(self):
        obj = Encoder(**DEFAULTS)
        notify = self._save(
            obj,
            change=False,
            changed_data=RUNTIME_AFFECTING_FIELDS,
            was_enabled=False,
        )

        event = audit_events()[0]
        detail = event.detail
        self.assertEqual(event.title, "Encoder configuration created")
        self.assertEqual(detail["action"], "create")
        self.assertEqual(detail["object_id"], obj.pk)
        self.assertEqual(detail["object_name"], obj.name)
        self.assertEqual(set(detail["changed_fields"]), RUNTIME_AFFECTING_FIELDS)
        self.assertEqual(set(detail["redacted_fields"]), {"password", "url"})
        self.assertNotIn("description", detail["changed_fields"])
        self.assertNotIn("sort_order", detail["changed_fields"])
        self.assertNotIn("name", detail["changed_fields"])
        retained = f"{event.title!r} {detail!r} {event.dedupe_key!r}".lower()
        self.assertNotIn(PASSWORD_BEFORE.lower(), retained)
        self.assertNotIn(URL_BEFORE.lower(), retained)
        for false_claim in ("started successfully", "accepted", "lkg promoted", "running"):
            self.assertNotIn(false_claim, retained)
        notify.assert_called_once()

    def test_rapid_bitrate_saves_remain_distinct_rows(self):
        obj = make_encoder(bitrate_kbps=192)
        obj.bitrate_kbps = 224
        self._save(obj, change=True, changed_data=["bitrate_kbps"])
        obj.bitrate_kbps = 256
        self._save(obj, change=True, changed_data=["bitrate_kbps"])

        events = audit_events()
        self.assertEqual(len(events), 2)
        self.assertNotEqual(events[0].dedupe_key, events[1].dedupe_key)
        self.assertEqual([event.repeat_count for event in events], [1, 1])

    def test_rollback_creates_no_event_and_restores_desired_configuration(self):
        obj = make_encoder(bitrate_kbps=192)
        with patch("encoders.admin._notify_reconciliation_pending"), \
             self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    obj.bitrate_kbps = 256
                    self.admin.save_model(
                        None,
                        obj,
                        admin_form(enabled=True, changed_data=["bitrate_kbps"]),
                        change=True,
                    )
                    raise RuntimeError("force rollback")
            except RuntimeError:
                pass

        self.assertEqual(audit_events(), [])
        obj.refresh_from_db()
        self.assertEqual(obj.bitrate_kbps, 192)

    def test_password_audit_is_deferred_with_no_secret_in_callback_closure(self):
        obj = make_encoder(password=PASSWORD_BEFORE, url=URL_BEFORE)
        obj.password = PASSWORD_AFTER
        obj.url = URL_AFTER
        with patch("encoders.admin._notify_reconciliation_pending"), \
             self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.admin.save_model(
                None,
                obj,
                admin_form(enabled=True, changed_data=["password", "url"]),
                change=True,
            )
            self.assertEqual(audit_events(), [])

        self.assertEqual(len(callbacks), 1)
        closure_text = repr(
            [cell.cell_contents for cell in (callbacks[0].__closure__ or ())]
        )
        for private_value in (
            PASSWORD_BEFORE,
            PASSWORD_AFTER,
            URL_BEFORE,
            URL_AFTER,
        ):
            self.assertNotIn(private_value, closure_text)
        callbacks[0]()
        self.assertEqual(len(audit_events()), 1)

    def test_audit_emitter_failure_cannot_fail_save_or_delete(self):
        obj = make_encoder(bitrate_kbps=192)
        obj.bitrate_kbps = 256
        with patch("encoders.admin._notify_reconciliation_pending"), patch(
            "monitoring.services.config_audit.emit_event",
            side_effect=RuntimeError("audit unavailable"),
        ), self.captureOnCommitCallbacks(execute=True):
            self.admin.save_model(
                None,
                obj,
                admin_form(enabled=True, changed_data=["bitrate_kbps"]),
                change=True,
            )
        obj.refresh_from_db()
        self.assertEqual(obj.bitrate_kbps, 256)

        with patch("encoders.admin._notify_reconciliation_pending"), patch(
            "monitoring.services.config_audit.emit_event",
            side_effect=RuntimeError("audit unavailable"),
        ), self.captureOnCommitCallbacks(execute=True):
            self.admin.delete_model(None, obj)
        self.assertFalse(Encoder.objects.filter(pk=obj.pk).exists())
        self.assertEqual(audit_events(), [])


class EncoderConfigurationAuditDeleteTests(TestCase):
    def setUp(self):
        self.admin = EncoderAdmin(Encoder, None)

    def test_individual_delete_records_safe_snapshot_and_reconciles_once(self):
        obj = make_encoder(enabled=True, password=PASSWORD_BEFORE, url=URL_BEFORE)
        object_id = obj.pk
        with patch("encoders.admin._notify_reconciliation_pending") as notify, \
             self.captureOnCommitCallbacks(execute=True):
            self.admin.delete_model(None, obj)

        event = audit_events()[0]
        detail = event.detail
        self.assertEqual(event.title, "Encoder configuration deleted")
        self.assertEqual(detail["action"], "delete")
        self.assertEqual(detail["object_id"], object_id)
        self.assertEqual(detail["object_name"], "audit-encoder")
        self.assertEqual(set(detail["changed_fields"]), RUNTIME_AFFECTING_FIELDS)
        self.assertEqual(set(detail["redacted_fields"]), {"password", "url"})
        self.assertNotIn(PASSWORD_BEFORE, repr(detail))
        self.assertNotIn(URL_BEFORE, repr(detail))
        notify.assert_called_once()

    def test_bulk_delete_emits_one_event_per_encoder_and_one_existing_notification(self):
        enabled = make_encoder(name="bulk-enabled", enabled=True)
        disabled = make_encoder(name="bulk-disabled", enabled=False)
        with patch("encoders.admin._notify_reconciliation_pending") as notify, \
             self.captureOnCommitCallbacks(execute=True):
            self.admin.delete_queryset(
                None,
                Encoder.objects.filter(pk__in=[enabled.pk, disabled.pk]),
            )

        events = audit_events()
        self.assertEqual(len(events), 2)
        self.assertEqual(
            {event.detail["object_name"] for event in events},
            {"bulk-enabled", "bulk-disabled"},
        )
        notify.assert_called_once()

    def test_bulk_delete_rollback_restores_rows_and_emits_no_events(self):
        first = make_encoder(name="rollback-a")
        second = make_encoder(name="rollback-b")
        ids = [first.pk, second.pk]
        with patch("encoders.admin._notify_reconciliation_pending"), \
             self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    self.admin.delete_queryset(
                        None, Encoder.objects.filter(pk__in=ids)
                    )
                    raise RuntimeError("force rollback")
            except RuntimeError:
                pass
        self.assertEqual(Encoder.objects.filter(pk__in=ids).count(), 2)
        self.assertEqual(audit_events(), [])


def changelist_formset_data(objs, row_overrides=None):
    row_overrides = row_overrides or {}
    data = {
        "form-TOTAL_FORMS": str(len(objs)),
        "form-INITIAL_FORMS": str(len(objs)),
        "form-MIN_NUM_FORMS": "0",
        "form-MAX_NUM_FORMS": "1000",
        "_save": "Save",
    }
    for index, obj in enumerate(objs):
        override = row_overrides.get(obj.pk, {})
        if override.get("enabled", obj.enabled):
            data[f"form-{index}-enabled"] = "on"
        data[f"form-{index}-id"] = str(obj.pk)
        data[f"form-{index}-sort_order"] = str(
            override.get("sort_order", obj.sort_order)
        )
    return data


@override_settings(SECURE_SSL_REDIRECT=False)
class EncoderConfigurationAuditListEditableTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser(
            "audit-admin", "audit-admin@example.invalid", "test-login-password"
        )
        self.client.force_login(self.staff)
        self.url = reverse("admin:encoders_encoder_changelist")

    def _post(self, objs, overrides):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self.url,
                changelist_formset_data(objs, overrides),
                follow=False,
            )
        self.assertEqual(response.status_code, 302)

    def test_enabled_toggle_emits_one_event_for_changed_encoder(self):
        obj = make_encoder(enabled=True)
        self._post([obj], {obj.pk: {"enabled": False}})
        event = audit_events()[0]
        self.assertEqual(event.detail["object_id"], obj.pk)
        self.assertEqual(event.detail["changed_fields"], ["enabled"])
        self.assertEqual(event.detail["changed_by"], "audit-admin")

    def test_sort_order_only_row_emits_no_event(self):
        obj = make_encoder(sort_order=0)
        self._post([obj], {obj.pk: {"sort_order": 7}})
        self.assertEqual(audit_events(), [])

    def test_mixed_formset_audits_only_enabled_row(self):
        sort_only = make_encoder(name="sort-only", sort_order=0)
        toggle = make_encoder(name="toggle", sort_order=1, enabled=True)
        self._post(
            [sort_only, toggle],
            {
                sort_only.pk: {"sort_order": 9},
                toggle.pk: {"enabled": False},
            },
        )
        events = audit_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].detail["object_name"], "toggle")

    def test_multiple_enabled_toggles_emit_one_event_per_changed_encoder(self):
        first = make_encoder(name="toggle-a", sort_order=0, enabled=True)
        second = make_encoder(name="toggle-b", sort_order=1, enabled=False)
        self._post(
            [first, second],
            {
                first.pk: {"enabled": False},
                second.pk: {"enabled": True},
            },
        )
        events = audit_events()
        self.assertEqual(len(events), 2)
        self.assertEqual(
            {event.detail["object_name"] for event in events},
            {"toggle-a", "toggle-b"},
        )
