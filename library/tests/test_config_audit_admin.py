from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib import admin as django_admin
from django.db import transaction
from django.test import TestCase

from library.admin import (
    FXBusConfigAdmin,
    RemoteDJConfigAdmin,
    VoiceTrackConfigAdmin,
    _FX_BUS_CONFIG_AUDIT_FIELDS,
    _REMOTE_DJ_CONFIG_AUDIT_FIELDS,
    _VOICE_TRACK_CONFIG_AUDIT_FIELDS,
)
from library.models import FXBusConfig, RemoteDJConfig, VoiceTrackConfig
from monitoring.models import SystemEvent


STUN_SECRET = "stun://operator:secret@stun.example.invalid:3478?token=hidden"


def _request():
    return SimpleNamespace(
        user=SimpleNamespace(get_username=lambda: "library-audit-operator")
    )


def _audit_events(object_type):
    return [
        event
        for event in SystemEvent.objects.order_by("pk")
        if event.detail.get("event_type") == "configuration_change"
        and event.detail.get("object_type") == object_type
    ]


class SingletonAuditTestCase(TestCase):
    model = None
    admin_class = None
    object_type = None

    def setUp(self):
        self.admin = self.admin_class(self.model, django_admin.AdminSite())
        self.config = self.model.load()

    def save(self, updates=None, *, change=True):
        updates = updates or {}
        for field, value in updates.items():
            setattr(self.config, field, value)
        with self.captureOnCommitCallbacks(execute=True):
            self.admin.save_model(
                _request(),
                self.config,
                SimpleNamespace(changed_data=list(updates)),
                change=change,
            )

    def assert_noop_and_rollback(self, field, rollback_value):
        self.save()
        self.assertEqual(_audit_events(self.object_type), [])

        original = getattr(self.config, field)
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    setattr(self.config, field, rollback_value)
                    self.admin.save_model(
                        _request(),
                        self.config,
                        SimpleNamespace(changed_data=[field]),
                        change=True,
                    )
                    self.assertEqual(_audit_events(self.object_type), [])
                    raise RuntimeError("force rollback")
            except RuntimeError:
                pass
        self.config.refresh_from_db()
        self.assertEqual(getattr(self.config, field), original)
        self.assertEqual(_audit_events(self.object_type), [])

    def assert_rapid_saves_are_distinct(self, field, first, second):
        self.save({field: first})
        self.save({field: second})
        events = _audit_events(self.object_type)
        self.assertEqual(len(events), 2)
        self.assertNotEqual(events[0].dedupe_key, events[1].dedupe_key)


class RemoteDJConfigAuditTests(SingletonAuditTestCase):
    model = RemoteDJConfig
    admin_class = RemoteDJConfigAdmin
    object_type = "library.RemoteDJConfig"

    def test_enabled_change_requires_engine_restart(self):
        self.save({"enabled": True})
        detail = _audit_events(self.object_type)[0].detail
        self.assertEqual(detail["changed_fields"], ["enabled"])
        self.assertEqual(
            detail["apply_modes"], {"enabled": "engine_restart_required"}
        )
        self.assertTrue(detail["restart_required"])

    def test_stun_change_is_redacted_and_applies_to_next_session(self):
        self.save({"stun_server": STUN_SECRET})
        event = _audit_events(self.object_type)[0]
        detail = event.detail
        self.assertEqual(detail["changed_fields"], ["stun_server"])
        self.assertEqual(detail["redacted_fields"], ["stun_server"])
        self.assertNotIn("stun_server", detail["changes"])
        self.assertEqual(
            detail["apply_modes"], {"stun_server": "next_remote_dj_session"}
        )
        self.assertNotIn(STUN_SECRET, repr((event.detail, event.dedupe_key)))
        self.assertFalse(detail["restart_required"])

    def test_ice_port_changes_are_one_next_session_event(self):
        self.save({"ice_udp_min_port": 41000, "ice_udp_max_port": 41010})
        detail = _audit_events(self.object_type)[0].detail
        self.assertEqual(
            detail["changed_fields"], ["ice_udp_min_port", "ice_udp_max_port"]
        )
        self.assertEqual(
            set(detail["apply_modes"].values()), {"next_remote_dj_session"}
        )
        self.assertFalse(detail["restart_required"])

    def test_reconnect_grace_uses_next_recovery_decision(self):
        self.save({"reconnect_grace_seconds": 20})
        detail = _audit_events(self.object_type)[0].detail
        self.assertEqual(
            detail["apply_modes"],
            {"reconnect_grace_seconds": "next_remote_dj_recovery_decision"},
        )

    def test_multiple_fields_still_emit_once_with_per_field_semantics(self):
        self.save({"enabled": True, "ice_udp_min_port": 42000,
                   "reconnect_grace_seconds": 15})
        events = _audit_events(self.object_type)
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].detail["changed_fields"],
            ["enabled", "ice_udp_min_port", "reconnect_grace_seconds"],
        )
        self.assertTrue(events[0].detail["restart_required"])

    def test_noop_rollback_and_rapid_saves(self):
        self.assert_noop_and_rollback("reconnect_grace_seconds", 21)
        self.assert_rapid_saves_are_distinct(
            "reconnect_grace_seconds", 12, 13
        )

    def test_admin_save_does_not_add_runtime_command_side_effect(self):
        with patch.object(Path, "write_text") as write_text:
            self.save({"ice_udp_min_port": 43000})
        write_text.assert_not_called()

    def test_explicit_disabled_creation_does_not_claim_topology_change(self):
        self.config.delete()
        self.config = RemoteDJConfig(enabled=False)
        self.save(change=False)

        detail = _audit_events(self.object_type)[0].detail
        self.assertEqual(detail["action"], "create")
        self.assertEqual(
            detail["apply_modes"]["enabled"],
            "saved_disabled_state_no_runtime_change",
        )
        self.assertFalse(detail["restart_required"])


class FXBusConfigAuditTests(SingletonAuditTestCase):
    model = FXBusConfig
    admin_class = FXBusConfigAdmin
    object_type = "library.FXBusConfig"

    def test_volume_change_does_not_overclaim_unwired_runtime_reload(self):
        self.save({"volume_db": -3.5})
        detail = _audit_events(self.object_type)[0].detail
        self.assertEqual(detail["changed_fields"], ["volume_db"])
        self.assertEqual(
            detail["apply_modes"],
            {"volume_db": "runtime_adoption_not_confirmed"},
        )
        self.assertFalse(detail["restart_required"])

    def test_polyphony_cap_is_read_on_next_fx_fire(self):
        self.save({"polyphony_cap": 6})
        detail = _audit_events(self.object_type)[0].detail
        self.assertEqual(
            detail["apply_modes"], {"polyphony_cap": "next_fx_fire"}
        )
        self.assertFalse(detail["restart_required"])

    def test_both_fields_emit_one_event_with_distinct_apply_modes(self):
        self.save({"volume_db": -2.0, "polyphony_cap": 7})
        events = _audit_events(self.object_type)
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].detail["apply_modes"],
            {
                "volume_db": "runtime_adoption_not_confirmed",
                "polyphony_cap": "next_fx_fire",
            },
        )

    def test_noop_rollback_and_rapid_saves(self):
        self.assert_noop_and_rollback("volume_db", -4.0)
        self.assert_rapid_saves_are_distinct("volume_db", -1.0, -2.0)

    def test_admin_save_preserves_absence_of_runtime_command_writer(self):
        with patch.object(Path, "write_text") as write_text:
            self.save({"volume_db": -5.0})
        write_text.assert_not_called()


class VoiceTrackConfigAuditTests(SingletonAuditTestCase):
    model = VoiceTrackConfig
    admin_class = VoiceTrackConfigAdmin
    object_type = "library.VoiceTrackConfig"

    def test_each_field_has_truthful_runtime_read_metadata(self):
        cases = (
            ("program_duck_db", -9.0, "next_voice_track_sequence"),
            ("duck_ramp_ms", 450, "next_voice_track_phase"),
            ("min_gap_ms", 350, "next_voice_track_gap"),
        )
        for field, value, apply_mode in cases:
            with self.subTest(field=field):
                self.save({field: value})
                detail = _audit_events(self.object_type)[0].detail
                self.assertEqual(detail["changed_fields"], [field])
                self.assertEqual(detail["apply_modes"], {field: apply_mode})
                self.assertFalse(detail["restart_required"])
                SystemEvent.objects.all().delete()

    def test_multiple_fields_emit_once(self):
        self.save({"program_duck_db": -8.0, "duck_ramp_ms": 500,
                   "min_gap_ms": 275})
        events = _audit_events(self.object_type)
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].detail["changed_fields"],
            ["program_duck_db", "duck_ramp_ms", "min_gap_ms"],
        )

    def test_noop_rollback_and_rapid_saves(self):
        self.assert_noop_and_rollback("min_gap_ms", 425)
        self.assert_rapid_saves_are_distinct("min_gap_ms", 250, 300)

    def test_admin_save_does_not_claim_or_dispatch_reload_command(self):
        with patch.object(Path, "write_text") as write_text:
            self.save({"duck_ramp_ms": 550})
        write_text.assert_not_called()
        retained = repr(_audit_events(self.object_type)[0].detail).lower()
        self.assertNotIn("reload succeeded", retained)
        self.assertNotIn("running engine updated", retained)

    def test_emitter_failure_does_not_break_save(self):
        self.config.min_gap_ms = 325
        with patch(
            "monitoring.services.config_audit.emit_event",
            side_effect=RuntimeError("audit unavailable"),
        ), self.captureOnCommitCallbacks(execute=True):
            self.admin.save_model(
                _request(), self.config,
                SimpleNamespace(changed_data=["min_gap_ms"]), change=True,
            )
        self.config.refresh_from_db()
        self.assertEqual(self.config.min_gap_ms, 325)
        self.assertEqual(_audit_events(self.object_type), [])


class LibrarySingletonLazyCreationAuditTests(TestCase):
    def test_allowlists_cover_only_each_models_configuration_fields(self):
        cases = (
            (RemoteDJConfig, _REMOTE_DJ_CONFIG_AUDIT_FIELDS),
            (FXBusConfig, _FX_BUS_CONFIG_AUDIT_FIELDS),
            (VoiceTrackConfig, _VOICE_TRACK_CONFIG_AUDIT_FIELDS),
        )
        for model, allowlist in cases:
            with self.subTest(model=model.__name__):
                model_fields = {
                    field.name
                    for field in model._meta.concrete_fields
                    if field.name != "id"
                }
                self.assertEqual(set(allowlist), model_fields)

    def test_load_created_defaults_are_not_operator_audited(self):
        RemoteDJConfig.load()
        FXBusConfig.load()
        VoiceTrackConfig.load()
        self.assertFalse(
            SystemEvent.objects.filter(
                detail__event_type="configuration_change"
            ).exists()
        )
