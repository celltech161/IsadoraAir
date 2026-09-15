from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from monitoring.admin import (
    MonitorCheckAdmin,
    NotificationConfigAdmin,
    TransmitterConfigAdmin,
    TransmitterConfigAdminForm,
)
from monitoring.models import (
    MonitorCheck,
    NotificationConfig,
    SystemEvent,
    TransmitterConfig,
)


TRANSMITTER_PASSWORD_BEFORE = "AUDIT_TX_PASSWORD_BEFORE_ONLY"
TRANSMITTER_PASSWORD_AFTER = "AUDIT_TX_PASSWORD_AFTER_ONLY"
RECIPIENTS_BEFORE = "first-audit-recipient@example.invalid"
RECIPIENTS_AFTER = (
    "second-audit-recipient@example.invalid\nthird-audit-recipient@example.invalid"
)


def audit_events(object_type):
    return [
        event
        for event in SystemEvent.objects.order_by("pk")
        if event.detail.get("event_type") == "configuration_change"
        and event.detail.get("object_type") == object_type
    ]


def make_check(**overrides):
    values = {
        "name": "Audit systemd check",
        "kind": "systemd",
        "enabled": True,
        "systemd_unit": "audit-before.service",
        "consecutive_failures_required": 2,
        "notify_on_warning": True,
        "notify_on_critical": True,
        "show_as_card": True,
        "sort_order": 0,
    }
    values.update(overrides)
    return MonitorCheck.objects.create(**values)


class MonitorCheckConfigurationAuditTests(TestCase):
    object_type = "monitoring.MonitorCheck"

    def setUp(self):
        self.admin = MonitorCheckAdmin(MonitorCheck, None)

    def _save(self, obj, *, change):
        with self.captureOnCommitCallbacks(execute=True):
            self.admin.save_model(None, obj, SimpleNamespace(), change=change)

    def test_create_records_compact_effective_operational_configuration(self):
        obj = MonitorCheck(
            name="Created audit check",
            kind="systemd",
            enabled=True,
            systemd_unit="created-audit.service",
            show_as_card=False,
            sort_order=91,
        )
        self._save(obj, change=False)

        event = audit_events(self.object_type)[0]
        detail = event.detail
        self.assertEqual(event.category, "monitor")
        self.assertEqual(event.level, "info")
        self.assertEqual(event.title, "Monitoring check configuration created")
        self.assertEqual(detail["action"], "create")
        self.assertEqual(detail["object_id"], obj.pk)
        self.assertEqual(detail["object_name"], "Created audit check")
        self.assertEqual(
            detail["changed_fields"],
            [
                "name",
                "kind",
                "enabled",
                "systemd_unit",
                "consecutive_failures_required",
                "notify_on_warning",
                "notify_on_critical",
            ],
        )
        self.assertNotIn("show_as_card", detail["changed_fields"])
        self.assertNotIn("sort_order", detail["changed_fields"])
        self.assertEqual(
            set(detail["apply_modes"].values()), {"next_monitoring_poll"}
        )
        self.assertFalse(detail["restart_required"])
        retained = repr(detail).lower()
        for false_claim in ("poll succeeded", "evaluated successfully", "healthy"):
            self.assertNotIn(false_claim, retained)

    def test_operational_update_records_multiple_exact_diffs_once(self):
        obj = make_check()
        obj.systemd_unit = "audit-after.service"
        obj.consecutive_failures_required = 4
        obj.notify_on_warning = False
        self._save(obj, change=True)

        events = audit_events(self.object_type)
        self.assertEqual(len(events), 1)
        detail = events[0].detail
        self.assertEqual(
            detail["changed_fields"],
            [
                "systemd_unit",
                "consecutive_failures_required",
                "notify_on_warning",
            ],
        )
        self.assertEqual(
            detail["changes"]["systemd_unit"],
            {"old": "audit-before.service", "new": "audit-after.service"},
        )

    def test_enabled_and_operational_name_changes_are_audited(self):
        obj = make_check(enabled=True)
        obj.name = "Renamed operational alert"
        obj.enabled = False
        obj.show_as_card = False
        obj.sort_order = 17
        self._save(obj, change=True)

        event = audit_events(self.object_type)[0]
        self.assertEqual(event.detail["changed_fields"], ["name", "enabled"])
        self.assertEqual(event.detail["object_name"], "Renamed operational alert")

    def test_noop_show_as_card_and_sort_order_only_emit_none(self):
        cases = (
            ("noop", None, None),
            ("show", "show_as_card", False),
            ("sort", "sort_order", 12),
        )
        for label, field, value in cases:
            with self.subTest(label=label):
                SystemEvent.objects.all().delete()
                obj = make_check(name=f"Audit {label} check")
                if field is not None:
                    setattr(obj, field, value)
                self._save(obj, change=True)
                self.assertEqual(audit_events(self.object_type), [])

    def test_individual_delete_records_effective_pre_delete_snapshot(self):
        obj = make_check(name="Delete audit check")
        object_id = obj.pk
        with self.captureOnCommitCallbacks(execute=True):
            self.admin.delete_model(None, obj)

        event = audit_events(self.object_type)[0]
        self.assertEqual(event.title, "Monitoring check configuration deleted")
        self.assertEqual(event.detail["action"], "delete")
        self.assertEqual(event.detail["object_id"], object_id)
        self.assertEqual(event.detail["object_name"], "Delete audit check")
        self.assertNotIn("show_as_card", event.detail["changed_fields"])
        self.assertNotIn("sort_order", event.detail["changed_fields"])

    def test_bulk_delete_emits_one_event_per_check(self):
        first = make_check(name="Bulk audit A", sort_order=1)
        second = make_check(name="Bulk audit B", sort_order=2)
        with self.captureOnCommitCallbacks(execute=True):
            self.admin.delete_queryset(
                None, MonitorCheck.objects.filter(pk__in=[first.pk, second.pk])
            )

        events = audit_events(self.object_type)
        self.assertEqual(len(events), 2)
        self.assertEqual(
            {event.detail["object_name"] for event in events},
            {"Bulk audit A", "Bulk audit B"},
        )

    def test_bulk_delete_rollback_restores_checks_and_emits_none(self):
        first = make_check(name="Bulk rollback A", sort_order=1)
        second = make_check(name="Bulk rollback B", sort_order=2)
        object_ids = [first.pk, second.pk]
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    self.admin.delete_queryset(
                        None, MonitorCheck.objects.filter(pk__in=object_ids)
                    )
                    raise RuntimeError("force rollback")
            except RuntimeError:
                pass

        self.assertEqual(
            MonitorCheck.objects.filter(pk__in=object_ids).count(), 2
        )
        self.assertEqual(audit_events(self.object_type), [])

    def test_rollback_restores_config_and_emits_no_event(self):
        obj = make_check(consecutive_failures_required=2)
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    obj.consecutive_failures_required = 5
                    self.admin.save_model(None, obj, SimpleNamespace(), change=True)
                    raise RuntimeError("force rollback")
            except RuntimeError:
                pass

        obj.refresh_from_db()
        self.assertEqual(obj.consecutive_failures_required, 2)
        self.assertEqual(audit_events(self.object_type), [])

    def test_rapid_saves_remain_distinct_rows(self):
        obj = make_check(consecutive_failures_required=2)
        obj.consecutive_failures_required = 3
        self._save(obj, change=True)
        obj.consecutive_failures_required = 4
        self._save(obj, change=True)

        events = audit_events(self.object_type)
        self.assertEqual(len(events), 2)
        self.assertNotEqual(events[0].dedupe_key, events[1].dedupe_key)
        self.assertEqual([event.repeat_count for event in events], [1, 1])

    def test_emitter_failure_does_not_fail_save_or_delete(self):
        obj = make_check(consecutive_failures_required=2)
        object_id = obj.pk
        obj.consecutive_failures_required = 3
        with patch(
            "monitoring.services.config_audit.emit_event",
            side_effect=RuntimeError("audit unavailable"),
        ), self.captureOnCommitCallbacks(execute=True):
            self.admin.save_model(None, obj, SimpleNamespace(), change=True)
        obj.refresh_from_db()
        self.assertEqual(obj.consecutive_failures_required, 3)

        with patch(
            "monitoring.services.config_audit.emit_event",
            side_effect=RuntimeError("audit unavailable"),
        ), self.captureOnCommitCallbacks(execute=True):
            self.admin.delete_model(None, obj)
        self.assertFalse(MonitorCheck.objects.filter(pk=object_id).exists())


def monitor_changelist_data(objs, overrides=None):
    overrides = overrides or {}
    data = {
        "form-TOTAL_FORMS": str(len(objs)),
        "form-INITIAL_FORMS": str(len(objs)),
        "form-MIN_NUM_FORMS": "0",
        "form-MAX_NUM_FORMS": "1000",
        "_save": "Save",
    }
    for index, obj in enumerate(objs):
        values = overrides.get(obj.pk, {})
        data[f"form-{index}-id"] = str(obj.pk)
        if values.get("enabled", obj.enabled):
            data[f"form-{index}-enabled"] = "on"
        if values.get("show_as_card", obj.show_as_card):
            data[f"form-{index}-show_as_card"] = "on"
        data[f"form-{index}-sort_order"] = str(
            values.get("sort_order", obj.sort_order)
        )
    return data


@override_settings(SECURE_SSL_REDIRECT=False)
class MonitorCheckConfigurationAuditListEditableTests(TestCase):
    object_type = "monitoring.MonitorCheck"

    def setUp(self):
        self.staff = User.objects.create_superuser(
            "monitor-audit-admin", "monitor-audit-admin@example.invalid", "pw"
        )
        self.client.force_login(self.staff)
        self.url = reverse("admin:monitoring_monitorcheck_changelist")

    def _post(self, objs, overrides):
        ordered = sorted(objs, key=lambda obj: (obj.sort_order, obj.name))
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self.url, monitor_changelist_data(ordered, overrides)
            )
        self.assertEqual(response.status_code, 302)

    def test_enabled_toggle_emits_one_event_for_changed_check(self):
        obj = make_check(enabled=True)
        self._post([obj], {obj.pk: {"enabled": False}})

        event = audit_events(self.object_type)[0]
        self.assertEqual(event.detail["object_id"], obj.pk)
        self.assertEqual(event.detail["changed_fields"], ["enabled"])
        self.assertEqual(event.detail["changed_by"], "monitor-audit-admin")

    def test_show_as_card_and_sort_order_only_emit_none(self):
        obj = make_check(show_as_card=True, sort_order=0)
        self._post(
            [obj], {obj.pk: {"show_as_card": False, "sort_order": 8}}
        )
        self.assertEqual(audit_events(self.object_type), [])

    def test_mixed_formset_audits_only_enabled_changed_row(self):
        presentation = make_check(
            name="Presentation-only check", sort_order=0, show_as_card=True
        )
        enabled = make_check(
            name="Enabled-change check", sort_order=1, enabled=True
        )
        self._post(
            [presentation, enabled],
            {
                presentation.pk: {"show_as_card": False, "sort_order": 9},
                enabled.pk: {"enabled": False},
            },
        )

        events = audit_events(self.object_type)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].detail["object_name"], "Enabled-change check")

    def test_multiple_enabled_toggles_emit_one_event_per_changed_check(self):
        first = make_check(name="Toggle check A", sort_order=0, enabled=True)
        second = make_check(name="Toggle check B", sort_order=1, enabled=False)
        self._post(
            [first, second],
            {
                first.pk: {"enabled": False},
                second.pk: {"enabled": True},
            },
        )

        events = audit_events(self.object_type)
        self.assertEqual(len(events), 2)
        self.assertEqual(
            {event.detail["object_name"] for event in events},
            {"Toggle check A", "Toggle check B"},
        )


class TransmitterConfigurationAuditTests(TestCase):
    object_type = "monitoring.TransmitterConfig"

    def setUp(self):
        self.admin = TransmitterConfigAdmin(TransmitterConfig, None)
        self.config = TransmitterConfig.load()
        TransmitterConfig.objects.filter(pk=self.config.pk).update(
            transmitter_type=TransmitterConfig.TYPE_BW_TX300V3,
            host="192.0.2.10",
            port=23,
            password=TRANSMITTER_PASSWORD_BEFORE,
            timeout_seconds=3.0,
            poll_interval_seconds=30,
            full_power_watts=265.0,
        )
        self.config.refresh_from_db()

    def _form_data(self, **overrides):
        values = {
            "transmitter_type": TransmitterConfig.TYPE_BW_TX300V3,
            "host": "192.0.2.10",
            "port": 23,
            "password": "",
            "timeout_seconds": 3.0,
            "poll_interval_seconds": 30,
            "full_power_watts": 265.0,
        }
        values.update(overrides)
        return values

    def _save_form(self, **overrides):
        form = TransmitterConfigAdminForm(
            data=self._form_data(**overrides), instance=self.config
        )
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save(commit=False)
        with self.captureOnCommitCallbacks(execute=True):
            self.admin.save_model(None, obj, form, change=True)
        self.config = obj
        return form

    def test_safe_field_update_records_one_event_and_next_poll_semantics(self):
        self._save_form(host="192.0.2.20")

        event = audit_events(self.object_type)[0]
        detail = event.detail
        self.assertEqual(event.title, "Transmitter configuration updated")
        self.assertEqual(detail["changed_fields"], ["host"])
        self.assertEqual(
            detail["changes"]["host"],
            {"old": "192.0.2.10", "new": "192.0.2.20"},
        )
        self.assertEqual(detail["apply_modes"], {"host": "next_transmitter_poll"})
        retained = repr(detail).lower()
        for false_claim in ("authentication succeeded", "telemetry resumed", "connected"):
            self.assertNotIn(false_claim, retained)

    def test_several_safe_fields_emit_one_event(self):
        self._save_form(
            port=2323,
            timeout_seconds=5.0,
            poll_interval_seconds=45,
            full_power_watts=300.0,
        )
        events = audit_events(self.object_type)
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].detail["changed_fields"],
            [
                "port",
                "timeout_seconds",
                "poll_interval_seconds",
                "full_power_watts",
            ],
        )

    def test_blank_password_preserve_is_not_a_password_change(self):
        form = self._save_form(host="192.0.2.30", password="")
        self.config.refresh_from_db()
        self.assertEqual(self.config.password, TRANSMITTER_PASSWORD_BEFORE)
        self.assertIn("password", form.changed_data)
        detail = audit_events(self.object_type)[0].detail
        self.assertEqual(detail["changed_fields"], ["host"])
        self.assertNotIn("password", detail["redacted_fields"])

    def test_blank_password_preserve_noop_emits_none(self):
        self._save_form(password="")
        self.assertEqual(audit_events(self.object_type), [])

    def test_password_only_replacement_is_changed_and_redacted(self):
        self._save_form(password=TRANSMITTER_PASSWORD_AFTER)

        event = audit_events(self.object_type)[0]
        detail = event.detail
        self.assertEqual(detail["changed_fields"], ["password"])
        self.assertEqual(detail["redacted_fields"], ["password"])
        self.assertNotIn("password", detail["changes"])
        retained = f"{event.title!r} {detail!r} {event.dedupe_key!r}"
        self.assertNotIn(TRANSMITTER_PASSWORD_BEFORE, retained)
        self.assertNotIn(TRANSMITTER_PASSWORD_AFTER, retained)

    def test_secret_is_removed_before_on_commit_callback_registration(self):
        form = TransmitterConfigAdminForm(
            data=self._form_data(password=TRANSMITTER_PASSWORD_AFTER),
            instance=self.config,
        )
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save(commit=False)
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.admin.save_model(None, obj, form, change=True)
            self.assertEqual(audit_events(self.object_type), [])

        self.assertEqual(len(callbacks), 1)
        closure_text = repr(
            [cell.cell_contents for cell in (callbacks[0].__closure__ or ())]
        )
        self.assertNotIn(TRANSMITTER_PASSWORD_BEFORE, closure_text)
        self.assertNotIn(TRANSMITTER_PASSWORD_AFTER, closure_text)
        callbacks[0]()
        self.assertEqual(len(audit_events(self.object_type)), 1)

    def test_rollback_emits_none_and_restores_safe_and_private_values(self):
        form = TransmitterConfigAdminForm(
            data=self._form_data(
                host="192.0.2.99", password=TRANSMITTER_PASSWORD_AFTER
            ),
            instance=self.config,
        )
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save(commit=False)
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    self.admin.save_model(None, obj, form, change=True)
                    raise RuntimeError("force rollback")
            except RuntimeError:
                pass

        self.config.refresh_from_db()
        self.assertEqual(self.config.host, "192.0.2.10")
        self.assertEqual(self.config.password, TRANSMITTER_PASSWORD_BEFORE)
        self.assertEqual(audit_events(self.object_type), [])

    def test_explicit_admin_create_is_audited_but_lazy_load_is_not(self):
        TransmitterConfig.objects.all().delete()
        TransmitterConfig.load()
        self.assertEqual(audit_events(self.object_type), [])

        TransmitterConfig.objects.all().delete()
        obj = TransmitterConfig(
            transmitter_type=TransmitterConfig.TYPE_BW_TX300V3,
            host="192.0.2.40",
            password=TRANSMITTER_PASSWORD_AFTER,
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.admin.save_model(None, obj, SimpleNamespace(), change=False)
        detail = audit_events(self.object_type)[0].detail
        self.assertEqual(detail["action"], "create")
        self.assertEqual(detail["redacted_fields"], ["password"])


class NotificationConfigurationAuditTests(TestCase):
    object_type = "monitoring.NotificationConfig"

    def setUp(self):
        self.admin = NotificationConfigAdmin(NotificationConfig, None)
        self.config = NotificationConfig.load()
        NotificationConfig.objects.filter(pk=self.config.pk).update(
            enabled=True,
            recipients=RECIPIENTS_BEFORE,
            cooldown_minutes=30,
        )
        self.config.refresh_from_db()

    def _save(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.admin.save_model(
                None, self.config, SimpleNamespace(), change=True
            )

    def test_enabled_change_is_audited_for_next_notification_evaluation(self):
        self.config.enabled = False
        self._save()

        event = audit_events(self.object_type)[0]
        self.assertEqual(event.title, "Notification configuration updated")
        self.assertEqual(event.detail["changed_fields"], ["enabled"])
        self.assertEqual(
            event.detail["apply_modes"],
            {"enabled": "next_notification_evaluation"},
        )

    def test_cooldown_change_records_safe_values(self):
        self.config.cooldown_minutes = 60
        self._save()
        self.assertEqual(
            audit_events(self.object_type)[0].detail["changes"]["cooldown_minutes"],
            {"old": 30, "new": 60},
        )

    def test_recipient_change_is_private_and_contains_no_address_material(self):
        self.config.recipients = RECIPIENTS_AFTER
        self._save()

        event = audit_events(self.object_type)[0]
        detail = event.detail
        self.assertEqual(detail["changed_fields"], ["recipients"])
        self.assertEqual(detail["redacted_fields"], ["recipients"])
        self.assertNotIn("recipients", detail["changes"])
        retained = f"{event.title!r} {detail!r} {event.dedupe_key!r}"
        for address in (RECIPIENTS_BEFORE, *RECIPIENTS_AFTER.splitlines()):
            self.assertNotIn(address, retained)

    def test_multiple_policy_changes_emit_one_event_without_touching_smtp_env(self):
        self.config.enabled = False
        self.config.recipients = RECIPIENTS_AFTER
        self.config.cooldown_minutes = 45
        with patch("monitoring.admin.env_config.update_managed_values") as update_env:
            self._save()

        events = audit_events(self.object_type)
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].detail["changed_fields"],
            ["enabled", "recipients", "cooldown_minutes"],
        )
        self.assertEqual(events[0].detail["redacted_fields"], ["recipients"])
        update_env.assert_not_called()

    def test_noop_emits_none(self):
        self._save()
        self.assertEqual(audit_events(self.object_type), [])

    def test_private_addresses_are_removed_before_on_commit_callback(self):
        self.config.recipients = RECIPIENTS_AFTER
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.admin.save_model(
                None, self.config, SimpleNamespace(), change=True
            )
            self.assertEqual(audit_events(self.object_type), [])

        self.assertEqual(len(callbacks), 1)
        closure_text = repr(
            [cell.cell_contents for cell in (callbacks[0].__closure__ or ())]
        )
        for address in (RECIPIENTS_BEFORE, *RECIPIENTS_AFTER.splitlines()):
            self.assertNotIn(address, closure_text)
        callbacks[0]()
        self.assertEqual(len(audit_events(self.object_type)), 1)

    def test_transaction_rollback_emits_none(self):
        self.config.cooldown_minutes = 75
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    self.admin.save_model(
                        None, self.config, SimpleNamespace(), change=True
                    )
                    raise RuntimeError("force rollback")
            except RuntimeError:
                pass
        self.config.refresh_from_db()
        self.assertEqual(self.config.cooldown_minutes, 30)
        self.assertEqual(audit_events(self.object_type), [])

    def test_explicit_admin_create_redacts_recipients(self):
        NotificationConfig.objects.all().delete()
        obj = NotificationConfig(
            enabled=True,
            recipients=RECIPIENTS_AFTER,
            cooldown_minutes=30,
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.admin.save_model(None, obj, SimpleNamespace(), change=False)

        event = audit_events(self.object_type)[0]
        self.assertEqual(event.detail["action"], "create")
        self.assertEqual(event.detail["redacted_fields"], ["recipients"])
        self.assertNotIn(RECIPIENTS_AFTER, repr(event.detail))
