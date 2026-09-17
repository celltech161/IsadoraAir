"""Admin-to-engine live reload coverage for FXBusConfig.volume_db."""

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib import admin as django_admin
from django.db import transaction
from django.test import TestCase

from isadoraair import engine_commands as command_queue
from isadoraair.engine_commands import EngineCommandQueueFull
import library.services.engine as engine_module
from library.admin import FXBusConfigAdmin
from library.models import FXBusConfig
from monitoring.models import SystemEvent


RELOAD_PAYLOAD = {"command": "reload_fx_config"}


def _audit_events():
    return [
        event
        for event in SystemEvent.objects.order_by("pk")
        if event.detail.get("event_type") == "configuration_change"
        and event.detail.get("object_type") == "library.FXBusConfig"
    ]


def _form(*changed_fields):
    return SimpleNamespace(changed_data=list(changed_fields))


def _engine_stand_in():
    engine = object.__new__(engine_module.PlaybackEngine)
    engine.fx_bus_gain = MagicMock(name="fx_bus_gain")
    engine.main_pipeline = MagicMock(name="main_pipeline")
    engine._fx_fires = {
        1: {"gain": MagicMock(name="per_cart_gain"), "cart_id": 10}
    }
    return engine


class FXBusAdminLiveReloadTests(TestCase):
    def setUp(self):
        self.admin = FXBusConfigAdmin(FXBusConfig, django_admin.AdminSite())
        self.config = FXBusConfig.load()

    def _save(self, updates=None, *, execute=True):
        updates = updates or {}
        for field, value in updates.items():
            setattr(self.config, field, value)
        with self.captureOnCommitCallbacks(execute=execute) as callbacks:
            self.admin.save_model(
                None,
                self.config,
                _form(*updates),
                change=True,
            )
        return callbacks

    def test_operator_help_describes_polyphony_next_fire_semantics(self):
        description = self.admin.fieldsets[0][1]["description"]
        self.assertIn("subsequent FX fire admission decisions", description)
        self.assertIn("without a restart", description)
        self.assertIn("currently active fires continue", description)
        self.assertNotIn("requires an engine restart", description)

    def test_volume_change_waits_for_commit_then_publishes_exact_command(self):
        observed = []

        def record_committed_payload(payload):
            observed.append(
                (payload, FXBusConfig.objects.get(pk=1).volume_db)
            )

        with patch(
            "library.admin._write_engine_command",
            side_effect=record_committed_payload,
        ) as writer:
            callbacks = self._save({"volume_db": -3.5}, execute=False)
            writer.assert_not_called()
            self.assertEqual(_audit_events(), [])
            for callback in callbacks:
                callback()

        self.assertEqual(observed, [(RELOAD_PAYLOAD, -3.5)])
        self.assertEqual(len(_audit_events()), 1)

    def test_rollback_publishes_nothing_and_restores_volume(self):
        with patch("library.admin._write_engine_command") as writer:
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        self.config.volume_db = -4.0
                        self.admin.save_model(
                            None,
                            self.config,
                            _form("volume_db"),
                            change=True,
                        )
                        raise RuntimeError("force rollback")
                except RuntimeError:
                    pass

        self.config.refresh_from_db()
        self.assertEqual(self.config.volume_db, 0.0)
        writer.assert_not_called()
        self.assertEqual(_audit_events(), [])

    def test_noop_save_publishes_no_command_and_no_audit(self):
        with patch("library.admin._write_engine_command") as writer:
            self._save()
        writer.assert_not_called()
        self.assertEqual(_audit_events(), [])

    def test_polyphony_only_save_publishes_no_volume_command(self):
        with patch("library.admin._write_engine_command") as writer:
            self._save({"polyphony_cap": 6})
        writer.assert_not_called()
        events = _audit_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].detail["apply_modes"],
            {"polyphony_cap": "next_fx_fire"},
        )

    def test_combined_save_publishes_once_and_audits_once(self):
        with patch("library.admin._write_engine_command") as writer:
            self._save({"volume_db": -2.0, "polyphony_cap": 7})
        writer.assert_called_once_with(RELOAD_PAYLOAD)
        events = _audit_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].detail["apply_modes"],
            {
                "volume_db": "live_runtime_update_after_commit",
                "polyphony_cap": "next_fx_fire",
            },
        )

    def test_unavailable_command_endpoint_does_not_undo_committed_save(self):
        with self.assertLogs("hardware.signals", level="ERROR") as logs:
            with patch(
                "hardware.signals.enqueue_engine_command",
                side_effect=EngineCommandQueueFull("queue full"),
            ):
                self._save({"volume_db": -5.0})

        self.config.refresh_from_db()
        self.assertEqual(self.config.volume_db, -5.0)
        self.assertEqual(len(_audit_events()), 1)
        self.assertIn("live-reload command was not queued", logs.output[0])

    def test_real_publisher_payload_drives_real_engine_handler_contract(self):
        engine = _engine_stand_in()
        active_fire_before = dict(engine._fx_fires)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_dir = root / "engine_cmd.d"
            lock_path = root / "engine_cmd.lock"
            legacy_path = root / "engine_cmd.json"
            with patch.object(command_queue, "ENGINE_COMMAND_QUEUE_DIR", queue_dir), \
                 patch.object(command_queue, "ENGINE_COMMAND_LOCK_PATH", lock_path), \
                 patch.object(engine_module, "CMD_PATH", legacy_path):
                self._save({"volume_db": -6.0})
                paths = command_queue.list_committed_engine_commands()
                self.assertEqual(len(paths), 1)
                self.assertEqual(
                    json.loads(paths[0].read_text(encoding="utf-8")),
                    RELOAD_PAYLOAD,
                )
                engine._check_commands()

        engine.fx_bus_gain.set_property.assert_called_once_with(
            "volume", 10 ** (-6.0 / 20.0)
        )
        self.assertEqual(engine._fx_fires, active_fire_before)
        active_fire_before[1]["gain"].set_property.assert_not_called()
        self.assertEqual(engine.main_pipeline.mock_calls, [])


class FXBusEngineReloadHandlerTests(TestCase):
    def test_apply_volume_updates_only_shared_bus_gain_from_database_db_value(self):
        config = FXBusConfig.load()
        config.volume_db = 6.0
        config.save()
        engine = _engine_stand_in()
        active_fire_before = dict(engine._fx_fires)

        engine._fx_apply_volume()

        engine.fx_bus_gain.set_property.assert_called_once_with(
            "volume", 10 ** (6.0 / 20.0)
        )
        self.assertEqual(engine._fx_fires, active_fire_before)
        active_fire_before[1]["gain"].set_property.assert_not_called()
        self.assertEqual(engine.main_pipeline.mock_calls, [])
