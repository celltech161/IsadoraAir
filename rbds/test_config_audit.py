from types import SimpleNamespace
from unittest import mock

from django.contrib import admin as django_admin
from django.db import transaction
from django.test import TestCase

from monitoring.models import SystemEvent
from rbds.admin import (
    RBDSConfigAdmin,
    RBDSMessageAdmin,
    RBDSPSFrameAdmin,
    _RBDS_CONFIG_AUDIT_FIELDS,
)
from rbds.models import RBDSConfig, RBDSMessage, RBDSPSFrame
from updatecenter.backend_client import BackendTransportError


def _request():
    return SimpleNamespace(
        user=SimpleNamespace(get_username=lambda: "rbds-audit-operator")
    )


def _audit_events():
    return [
        event
        for event in SystemEvent.objects.order_by("pk")
        if event.detail.get("event_type") == "configuration_change"
        and event.detail.get("object_type") == "rbds.RBDSConfig"
    ]


class RBDSConfigAuditTests(TestCase):
    def setUp(self):
        self.site = django_admin.AdminSite()
        self.admin = RBDSConfigAdmin(RBDSConfig, self.site)
        self.config = RBDSConfig.load()

    def _save(self, updates=None, *, change=True, restart_result=None):
        updates = updates or {}
        for field, value in updates.items():
            setattr(self.config, field, value)
        form = SimpleNamespace(changed_data=list(updates))
        with mock.patch("rbds.admin.UpdaterClient") as client, mock.patch(
            "rbds.admin.messages.warning"
        ), mock.patch("rbds.admin.messages.error"):
            client.return_value.restart_operator_service.return_value = (
                restart_result or {}
            )
            with self.captureOnCommitCallbacks(execute=True):
                self.admin.save_model(
                    _request(), self.config, form, change=change
                )
        return client

    def test_allowlist_covers_every_config_field_and_not_database_identity(self):
        model_fields = {
            field.name
            for field in RBDSConfig._meta.concrete_fields
            if field.name != "id"
        }
        self.assertEqual(set(_RBDS_CONFIG_AUDIT_FIELDS), model_fields)

    def test_live_polled_station_change_records_one_event(self):
        client = self._save({"station_ps": "AUDITFM"})

        event = _audit_events()[0]
        self.assertEqual(event.title, "RBDS configuration updated")
        self.assertEqual(event.level, "info")
        self.assertEqual(event.detail["changed_by"], "rbds-audit-operator")
        self.assertEqual(event.detail["changed_fields"], ["station_ps"])
        self.assertEqual(
            event.detail["apply_modes"], {"station_ps": "next_rbds_poll"}
        )
        self.assertFalse(event.detail["restart_required"])
        client.return_value.restart_operator_service.assert_not_called()

    def test_topology_change_records_restart_required_and_preserves_restart(self):
        client = self._save({"host": "rbds-audit.example.invalid"})

        event = _audit_events()[0]
        self.assertEqual(event.detail["changed_fields"], ["host"])
        self.assertEqual(
            event.detail["apply_modes"],
            {"host": "protected_rbds_restart_required"},
        )
        self.assertTrue(event.detail["restart_required"])
        client.return_value.restart_operator_service.assert_called_once_with(
            "isadoraair-rbds.service"
        )

    def test_mixed_topology_and_live_change_is_one_truthful_event(self):
        self._save({"port": 4011, "nowplaying_min_seconds": 31})

        event = _audit_events()[0]
        self.assertEqual(
            event.detail["changed_fields"], ["port", "nowplaying_min_seconds"]
        )
        self.assertEqual(
            event.detail["apply_modes"],
            {
                "port": "protected_rbds_restart_required",
                "nowplaying_min_seconds": "next_rbds_poll",
            },
        )
        self.assertTrue(event.detail["restart_required"])

    def test_no_effective_change_records_nothing(self):
        client = self._save()
        self.assertEqual(_audit_events(), [])
        client.return_value.restart_operator_service.assert_not_called()

    def test_rollback_records_no_event_and_restores_config(self):
        original = self.config.station_ps
        with mock.patch("rbds.admin.UpdaterClient"):
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        self.config.station_ps = "ROLLBACK"
                        self.admin.save_model(
                            _request(),
                            self.config,
                            SimpleNamespace(changed_data=["station_ps"]),
                            change=True,
                        )
                        self.assertEqual(_audit_events(), [])
                        raise RuntimeError("force rollback")
                except RuntimeError:
                    pass

        self.config.refresh_from_db()
        self.assertEqual(self.config.station_ps, original)
        self.assertEqual(_audit_events(), [])

    def test_rapid_saves_remain_separate_events(self):
        self._save({"station_ps": "FIRST"})
        self._save({"station_ps": "SECOND"})

        events = _audit_events()
        self.assertEqual(len(events), 2)
        self.assertNotEqual(events[0].dedupe_key, events[1].dedupe_key)
        self.assertEqual([event.repeat_count for event in events], [1, 1])

    def test_pending_restart_event_remains_separate_from_config_audit(self):
        self._save(
            {"protocol": "ascii"},
            restart_result={"state": "accepted", "operation_id": "op-audit"},
        )

        audit = _audit_events()[0]
        pending = SystemEvent.objects.get(
            title="Protected RBDS restart remains pending"
        )
        self.assertEqual(audit.level, "info")
        self.assertEqual(pending.level, "warning")
        self.assertNotEqual(audit.pk, pending.pk)

    def test_restart_failure_event_remains_separate_and_save_persists(self):
        self.config.host = "saved-despite-restart-failure.invalid"
        with mock.patch("rbds.admin.UpdaterClient") as client, mock.patch(
            "rbds.admin.messages.error"
        ):
            client.return_value.restart_operator_service.side_effect = (
                BackendTransportError("broker unavailable")
            )
            with self.captureOnCommitCallbacks(execute=True):
                self.admin.save_model(
                    _request(),
                    self.config,
                    SimpleNamespace(changed_data=["host"]),
                    change=True,
                )

        self.config.refresh_from_db()
        self.assertEqual(self.config.host, "saved-despite-restart-failure.invalid")
        self.assertEqual(len(_audit_events()), 1)
        failure = SystemEvent.objects.get(
            title="Protected RBDS restart request failed after topology save"
        )
        self.assertEqual(failure.level, "error")

    def test_explicit_creation_is_audited_but_lazy_load_is_not(self):
        self.config.delete()
        SystemEvent.objects.all().delete()

        lazy = RBDSConfig.load()
        self.assertEqual(_audit_events(), [])
        lazy.delete()
        self.config = RBDSConfig(station_ps="CREATE")
        self._save(change=False)

        event = _audit_events()[0]
        self.assertEqual(event.detail["action"], "create")
        self.assertEqual(
            event.detail["changed_fields"], list(_RBDS_CONFIG_AUDIT_FIELDS)
        )
        self.assertTrue(event.detail["restart_required"])

    def test_emitter_failure_never_breaks_config_save(self):
        self.config.station_ps = "AUDITOK"
        with mock.patch("rbds.admin.UpdaterClient"), mock.patch(
            "monitoring.services.config_audit.emit_event",
            side_effect=RuntimeError("audit unavailable"),
        ), self.captureOnCommitCallbacks(execute=True):
            self.admin.save_model(
                _request(),
                self.config,
                SimpleNamespace(changed_data=["station_ps"]),
                change=True,
            )

        self.config.refresh_from_db()
        self.assertEqual(self.config.station_ps, "AUDITOK")
        self.assertEqual(_audit_events(), [])


class RBDSContentAuditExclusionTests(TestCase):
    def setUp(self):
        self.site = django_admin.AdminSite()

    def test_ps_frame_admin_save_is_not_configuration_audited(self):
        obj = RBDSPSFrame(text="AUDIT")
        RBDSPSFrameAdmin(RBDSPSFrame, self.site).save_model(
            _request(), obj, SimpleNamespace(changed_data=["text"]), change=False
        )
        self.assertFalse(
            SystemEvent.objects.filter(
                detail__event_type="configuration_change"
            ).exists()
        )

    def test_message_admin_save_is_not_configuration_audited(self):
        obj = RBDSMessage(name="Audit exclusion", source_type="static", text="Hi")
        RBDSMessageAdmin(RBDSMessage, self.site).save_model(
            _request(), obj, SimpleNamespace(changed_data=["text"]), change=False
        )
        self.assertFalse(
            SystemEvent.objects.filter(
                detail__event_type="configuration_change"
            ).exists()
        )
