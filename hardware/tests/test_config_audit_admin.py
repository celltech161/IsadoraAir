from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.db import transaction
from django.test import TestCase

from hardware.admin import AudioInputAdmin, AudioOutputAdmin, AudioPipelineAdmin
from hardware.models import (
    AudioInput,
    AudioOutput,
    AudioPipeline,
    DuckingConfig,
    RemoteDJAudioInput,
)
from monitoring.models import SystemEvent
from updatecenter.backend_client import BackendError


def _audit_events():
    return [
        event
        for event in SystemEvent.objects.order_by("pk")
        if event.detail.get("event_type") == "configuration_change"
    ]


def _plain_form():
    return SimpleNamespace(cleaned_data={}, initial={})


class AudioPipelineConfigurationAuditTests(TestCase):
    def setUp(self):
        self.pipeline = AudioPipeline.load()
        self.ducking = DuckingConfig.load()
        self.remote = RemoteDJAudioInput.load()
        self.admin = AudioPipelineAdmin(AudioPipeline, None)

    def _save(self, *, changed_data, **values):
        cleaned = {
            "ducking_enabled": self.ducking.enabled,
            "duck_level_db": self.ducking.duck_level_db,
            "remote_dj_gain_db": self.remote.gain_db,
        }
        cleaned.update(values)
        for field in ("sample_rate", "program_gain_db", "vu_meter_min_db"):
            if field in values:
                setattr(self.pipeline, field, values[field])
        form = SimpleNamespace(cleaned_data=cleaned, changed_data=list(changed_data))
        client = Mock()
        client.restart_operator_service.return_value = {"state": "completed"}
        with patch("hardware.admin.UpdaterClient", return_value=client), \
             self.captureOnCommitCallbacks(execute=True):
            self.admin.save_model(None, self.pipeline, form, change=True)
        return client

    def test_single_folded_field_emits_one_non_restart_event(self):
        client = self._save(changed_data=["ducking_enabled"], ducking_enabled=True)

        event = _audit_events()[0]
        self.assertEqual(event.title, "Audio pipeline configuration updated")
        self.assertEqual(event.detail["changed_fields"], ["ducking_enabled"])
        self.assertEqual(
            event.detail["changes"]["ducking_enabled"],
            {"old": False, "new": True},
        )
        self.assertEqual(
            event.detail["apply_modes"],
            {"ducking_enabled": "next_ptt_transition"},
        )
        self.assertFalse(event.detail["restart_required"])
        client.restart_operator_service.assert_not_called()

    def test_fields_across_three_models_emit_one_event_and_one_restart_request(self):
        new_rate = 44100 if self.pipeline.sample_rate != 44100 else 48000
        client = self._save(
            changed_data=["sample_rate", "duck_level_db", "remote_dj_gain_db"],
            sample_rate=new_rate,
            duck_level_db=-9.0,
            remote_dj_gain_db=8.0,
        )

        events = _audit_events()
        self.assertEqual(len(events), 1)
        detail = events[0].detail
        self.assertEqual(
            detail["changed_fields"],
            ["sample_rate", "duck_level_db", "remote_dj_gain_db"],
        )
        self.assertEqual(
            detail["apply_modes"],
            {
                "sample_rate": "engine_restart_required",
                "duck_level_db": "next_ptt_transition",
                "remote_dj_gain_db": "next_remote_dj_session",
            },
        )
        self.assertTrue(detail["restart_required"])
        client.restart_operator_service.assert_called_once_with(
            "isadoraair-engine.service"
        )

    def test_vu_meter_only_change_is_not_audited_or_restarted(self):
        client = self._save(
            changed_data=["vu_meter_min_db"],
            vu_meter_min_db=self.pipeline.vu_meter_min_db - 5,
        )
        self.assertEqual(_audit_events(), [])
        client.restart_operator_service.assert_not_called()

    def test_vu_meter_is_excluded_from_mixed_audit(self):
        self._save(
            changed_data=["program_gain_db", "vu_meter_min_db"],
            program_gain_db=self.pipeline.program_gain_db - 1,
            vu_meter_min_db=self.pipeline.vu_meter_min_db - 5,
        )
        self.assertEqual(
            _audit_events()[0].detail["changed_fields"], ["program_gain_db"]
        )

    def test_pending_restart_warning_remains_separate_from_saved_config_audit(self):
        client = Mock()
        client.restart_operator_service.return_value = {
            "state": "accepted",
            "operation_id": "op-123",
        }
        self.pipeline.program_gain_db -= 1
        form = SimpleNamespace(
            cleaned_data={
                "ducking_enabled": self.ducking.enabled,
                "duck_level_db": self.ducking.duck_level_db,
                "remote_dj_gain_db": self.remote.gain_db,
            },
            changed_data=["program_gain_db"],
        )
        with patch("hardware.admin.UpdaterClient", return_value=client), \
             self.captureOnCommitCallbacks(execute=True):
            self.admin.save_model(None, self.pipeline, form, change=True)

        self.assertEqual(len(_audit_events()), 1)
        self.assertTrue(
            SystemEvent.objects.filter(
                title="Protected engine restart remains pending", level="warning"
            ).exists()
        )

    def test_failed_restart_event_remains_separate_from_saved_config_audit(self):
        client = Mock()
        client.restart_operator_service.side_effect = BackendError("broker failed")
        self.pipeline.program_gain_db -= 1
        form = SimpleNamespace(
            cleaned_data={
                "ducking_enabled": self.ducking.enabled,
                "duck_level_db": self.ducking.duck_level_db,
                "remote_dj_gain_db": self.remote.gain_db,
            },
            changed_data=["program_gain_db"],
        )
        with patch("hardware.admin.UpdaterClient", return_value=client), \
             self.captureOnCommitCallbacks(execute=True):
            self.admin.save_model(None, self.pipeline, form, change=True)

        self.assertEqual(len(_audit_events()), 1)
        self.assertTrue(
            SystemEvent.objects.filter(
                title="Protected engine restart request failed after audio pipeline save",
                level="error",
            ).exists()
        )


class AudioInputConfigurationAuditTests(TestCase):
    def setUp(self):
        self.admin = AudioInputAdmin(AudioInput, None)

    def _save(self, obj, *, change, form=None):
        with self.captureOnCommitCallbacks(execute=True):
            self.admin.save_model(None, obj, form or _plain_form(), change=change)

    def test_create_and_delete_emit_safe_snapshots(self):
        obj = AudioInput(
            name="Audit Mic",
            device="plughw:7,0",
            device_identity_kind="alsa_card_id",
            device_identity="CODEC",
            gain_db=2.5,
        )
        self._save(obj, change=False)
        created = _audit_events()[0]
        self.assertEqual(created.detail["action"], "create")
        self.assertEqual(created.detail["object_id"], obj.pk)
        self.assertEqual(created.detail["changes"]["gain_db"], {"old": None, "new": 2.5})

        SystemEvent.objects.all().delete()
        with self.captureOnCommitCallbacks(execute=True):
            self.admin.delete_model(None, obj)
        deleted = _audit_events()[0]
        self.assertEqual(deleted.detail["action"], "delete")
        self.assertEqual(deleted.detail["object_name"], "Audit Mic")
        self.assertEqual(
            deleted.detail["changes"]["device_identity"],
            {"old": "CODEC", "new": None},
        )

    def test_update_records_device_gain_and_identity_before_after(self):
        obj = AudioInput.objects.create(name="Mic", device="plughw:1,0")
        obj.device = "plughw:2,0"
        obj.device_identity_kind = "alsa_card_id"
        obj.device_identity = "PCH"
        obj.gain_db = 4.0
        self._save(obj, change=True)

        detail = _audit_events()[0].detail
        self.assertEqual(
            detail["changed_fields"],
            ["device", "device_identity_kind", "device_identity", "gain_db"],
        )
        self.assertEqual(
            detail["changes"]["device"],
            {"old": "plughw:1,0", "new": "plughw:2,0"},
        )
        self.assertEqual(detail["changes"]["gain_db"], {"old": 0.0, "new": 4.0})
        self.assertTrue(detail["restart_required"])
        self.assertEqual(
            set(detail["apply_modes"].values()), {"engine_restart_required"}
        )

    def test_noop_and_sort_order_only_emit_no_audit(self):
        obj = AudioInput.objects.create(name="Quiet Mic")
        self._save(obj, change=True)
        obj.sort_order = 5
        self._save(obj, change=True)
        self.assertEqual(_audit_events(), [])

    def test_mixer_change_records_only_changed_logical_control_and_still_applies(self):
        obj = AudioInput.objects.create(
            name="Mixer Mic",
            device="plughw:9,0",
            mixer_control_values={"Capture,0": 40, "Mic Boost,0": True},
        )
        control = {
            "control_id": "Capture,0",
            "label": "Capture",
            "has_enum": False,
            "has_switch": False,
            "has_volume": True,
        }
        form = SimpleNamespace(
            _mixer_control_map={"mixer_0": control},
            initial={
                "device_identity_kind": "",
                "device_identity": "",
                "device": "plughw:9,0",
            },
            cleaned_data={
                "device_identity_kind": "",
                "device_identity": "",
                "device": "plughw:9,0",
                "mixer_0": 55,
            },
        )
        with patch("hardware.admin.subprocess.run") as run, \
             patch("hardware.admin._alsa_store") as store:
            self._save(obj, change=True, form=form)

        detail = _audit_events()[0].detail
        self.assertEqual(detail["changed_fields"], ["mixer_control_values.Capture,0"])
        self.assertEqual(
            detail["changes"],
            {"mixer_control_values.Capture,0": {"old": 40, "new": 55}},
        )
        self.assertEqual(
            detail["apply_modes"],
            {
                "mixer_control_values.Capture,0":
                    "immediate_hardware_apply_attempted"
            },
        )
        self.assertFalse(detail["restart_required"])
        run.assert_called_once()
        store.assert_called_once()

    def test_bulk_delete_emits_one_event_per_object(self):
        first = AudioInput.objects.create(name="Bulk Mic A")
        second = AudioInput.objects.create(name="Bulk Mic B")
        with self.captureOnCommitCallbacks(execute=True):
            self.admin.delete_queryset(
                None, AudioInput.objects.filter(pk__in=[first.pk, second.pk])
            )
        self.assertEqual(len(_audit_events()), 2)
        self.assertEqual(
            {event.detail["object_name"] for event in _audit_events()},
            {"Bulk Mic A", "Bulk Mic B"},
        )


class AudioOutputConfigurationAuditTests(TestCase):
    def setUp(self):
        self.admin = AudioOutputAdmin(AudioOutput, None)

    def _save(self, obj, *, change, form=None):
        writer = Mock()
        with patch("hardware.signals._write_engine_command", writer), \
             self.captureOnCommitCallbacks(execute=True):
            self.admin.save_model(None, obj, form or _plain_form(), change=change)
        return writer

    def test_create_and_delete_emit(self):
        obj = AudioOutput(name="Audit Output", device="plughw:4,0")
        writer = self._save(obj, change=False)
        self.assertEqual(_audit_events()[0].detail["action"], "create")
        writer.assert_called_once_with(
            {"command": "reload_audio_output_recovery_config"}
        )

        SystemEvent.objects.all().delete()
        with self.captureOnCommitCallbacks(execute=True):
            self.admin.delete_model(None, obj)
        self.assertEqual(_audit_events()[0].detail["action"], "delete")
        self.assertTrue(_audit_events()[0].detail["restart_required"])

    def test_studio_device_identity_and_agc_changes_are_audited_truthfully(self):
        obj, _created = AudioOutput.objects.get_or_create(name="Studio Monitor")
        AudioOutput.objects.filter(pk=obj.pk).update(
            device="plughw:1,0",
            device_identity_kind="",
            device_identity="",
            agc_enabled=False,
        )
        obj.refresh_from_db()
        obj.device = "plughw:2,0"
        obj.device_identity_kind = "alsa_card_id"
        obj.device_identity = "PCH"
        obj.agc_enabled = True
        writer = self._save(obj, change=True)

        detail = _audit_events()[0].detail
        self.assertEqual(
            detail["changed_fields"],
            ["device", "device_identity_kind", "device_identity", "agc_enabled"],
        )
        self.assertEqual(
            detail["apply_modes"],
            {
                "device": "live_output_reload_requested",
                "device_identity_kind": "live_recovery_config_refresh_requested",
                "device_identity": "live_recovery_config_refresh_requested",
                "agc_enabled": "live_output_reload_requested",
            },
        )
        self.assertFalse(detail["restart_required"])
        writer.assert_called_once_with({"command": "reload_audio_output"})

    def test_non_studio_raw_device_change_requires_restart_but_identity_is_live_refresh(self):
        obj = AudioOutput.objects.create(name="Stereotool Input", device="plughw:1,0")
        obj.device = "plughw:2,0"
        obj.device_identity_kind = "alsa_card_id"
        obj.device_identity = "Loopback"
        writer = self._save(obj, change=True)

        detail = _audit_events()[0].detail
        self.assertEqual(
            detail["apply_modes"]["device"], "engine_restart_required"
        )
        self.assertEqual(
            detail["apply_modes"]["device_identity"],
            "live_recovery_config_refresh_requested",
        )
        self.assertTrue(detail["restart_required"])
        writer.assert_called_once_with(
            {"command": "reload_audio_output_recovery_config"}
        )

    def test_noop_and_sort_order_only_emit_none_and_do_not_add_an_ipc_writer(self):
        obj = AudioOutput.objects.create(name="Unchanged Output")
        writer = self._save(obj, change=True)
        obj.sort_order = 8
        second_writer = self._save(obj, change=True)
        self.assertEqual(_audit_events(), [])
        writer.assert_called_once()
        second_writer.assert_called_once()

    def test_output_mixer_change_is_compact_and_existing_application_is_preserved(self):
        obj = AudioOutput.objects.create(
            name="Mixer Output",
            device="plughw:9,0",
            mixer_control_values={"Master,0": 40, "Headphone,0": 70},
        )
        control = {
            "control_id": "Master,0",
            "label": "Master",
            "has_enum": False,
            "has_switch": False,
            "has_volume": True,
        }
        form = SimpleNamespace(
            _mixer_control_map={"mixer_0": control},
            initial={
                "device_identity_kind": "",
                "device_identity": "",
                "device": "plughw:9,0",
            },
            cleaned_data={
                "device_identity_kind": "",
                "device_identity": "",
                "device": "plughw:9,0",
                "mixer_0": 60,
            },
        )
        with patch("hardware.admin.subprocess.run") as run, \
             patch("hardware.admin._alsa_store") as store:
            self._save(obj, change=True, form=form)

        detail = _audit_events()[0].detail
        self.assertEqual(detail["changed_fields"], ["mixer_control_values.Master,0"])
        self.assertEqual(
            detail["changes"],
            {"mixer_control_values.Master,0": {"old": 40, "new": 60}},
        )
        run.assert_called_once()
        store.assert_called_once()

    def test_rollback_publishes_neither_runtime_command_nor_audit(self):
        obj = AudioOutput.objects.create(name="Rollback Output", device="plughw:1,0")
        writer = Mock()
        with patch("hardware.signals._write_engine_command", writer), \
             self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    obj.device = "plughw:2,0"
                    self.admin.save_model(None, obj, _plain_form(), change=True)
                    raise RuntimeError("force rollback")
            except RuntimeError:
                pass

        writer.assert_not_called()
        self.assertEqual(_audit_events(), [])
        obj.refresh_from_db()
        self.assertEqual(obj.device, "plughw:1,0")
