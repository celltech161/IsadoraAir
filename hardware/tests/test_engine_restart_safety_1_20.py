"""Roadmap 1.20 (revised): Django Admin Audio Pipeline saves and the engine safeguard.

Drives the real admin change view so blocked-save atomicity (no configuration
is written when the safeguard refuses) is proven end to end, not by calling
save_model on a stub.
"""
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from hardware.models import AudioPipeline, DuckingConfig, RemoteDJAudioInput
from library.services import engine_restart_safety as safety
from library.tests.engine_restart_state_support import EngineStateFile
from monitoring.models import SystemEvent
from updatecenter.backend_client import BackendRejectedError

REASON = "Pipeline rate fix needed now, engineer present"


@override_settings(SECURE_SSL_REDIRECT=False)
class AudioPipelineRestartSafetyTests(TestCase):
    def setUp(self):
        self.state = EngineStateFile(self)
        self.state.write()
        self.pipeline = AudioPipeline.load()
        DuckingConfig.load()
        RemoteDJAudioInput.load()
        self.new_rate = 44100 if self.pipeline.sample_rate != 44100 else 48000
        self.superuser = User.objects.create_superuser("ap-root", password="x")
        # An ordinary administrator: staff with change permission on the
        # pipeline, but NOT a superuser.
        self.admin_user = User.objects.create_user("ap-admin", password="x", is_staff=True)
        self.admin_user.user_permissions.add(
            Permission.objects.get(codename="change_audiopipeline"),
            Permission.objects.get(codename="view_audiopipeline"),
        )
        patcher = patch("hardware.admin.UpdaterClient")
        self.broker = patcher.start()
        self.addCleanup(patcher.stop)
        self.restart = self.broker.return_value.restart_operator_service
        self.restart.return_value = {"operation_id": "op-ap", "state": "accepted"}
        self.url = reverse("admin:hardware_audiopipeline_change", args=[self.pipeline.pk])

    # ------------------------------------------------------------ helpers
    def form_data(self, **overrides):
        ducking = DuckingConfig.load()
        data = {
            "sample_rate": self.pipeline.sample_rate,
            "program_gain_db": self.pipeline.program_gain_db,
            "vu_meter_min_db": self.pipeline.vu_meter_min_db,
            "ducking_enabled": "on" if ducking.enabled else "",
            "duck_level_db": ducking.duck_level_db,
            "ptt_auto_manual_enabled": "on" if ducking.ptt_auto_manual_enabled else "",
            "remote_dj_gain_db": RemoteDJAudioInput.load().gain_db,
            "_save": "Save",
        }
        if self.pipeline.audio_gap_diagnostics_enabled:
            data["audio_gap_diagnostics_enabled"] = "on"
        data.update(overrides)
        return {k: v for k, v in data.items() if v is not None}

    def post(self, user, **overrides):
        client = Client()
        client.force_login(user)
        # The configuration audit event is queued on_commit; TestCase never commits.
        with self.captureOnCommitCallbacks(execute=True):
            return client.post(self.url, self.form_data(**overrides))

    def snapshot(self):
        pipeline = AudioPipeline.objects.get(pk=self.pipeline.pk)
        return (pipeline.sample_rate, pipeline.program_gain_db, pipeline.vu_meter_min_db,
                pipeline.audio_gap_diagnostics_enabled,
                DuckingConfig.load().duck_level_db, RemoteDJAudioInput.load().gain_db)

    def audit(self, title=None):
        qs = SystemEvent.objects.filter(category="restart_safety").order_by("pk")
        return [e for e in qs if title is None or e.title == title]

    def config_events(self):
        return [e for e in SystemEvent.objects.all() if e.detail.get("event_type") == "configuration_change"]

    def assert_blocked_and_atomic(self, response, before, *, code=200):
        # A refused save re-renders the form with an error (admin form validation)
        # and writes nothing.
        self.assertEqual(response.status_code, code)
        self.restart.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.config_events(), [])

    # ----------------------------------------------- quiet engine: unchanged
    def test_quiet_engine_rate_change_saves_and_requests_exactly_one_restart(self):
        response = self.post(self.superuser, sample_rate=self.new_rate)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AudioPipeline.objects.get(pk=self.pipeline.pk).sample_rate, self.new_rate)
        self.restart.assert_called_once_with("isadoraair-engine.service")
        self.assertEqual(self.audit(), [])

    def test_changing_rate_and_gain_together_restarts_exactly_once(self):
        self.post(self.superuser, sample_rate=self.new_rate, program_gain_db=-3.5)
        self.assertEqual(self.restart.call_count, 1)

    def test_manual_mode_only_warns_but_does_not_block(self):
        self.state.write(manual_mode=True)
        self.post(self.superuser, program_gain_db=self.pipeline.program_gain_db - 2)
        self.restart.assert_called_once_with("isadoraair-engine.service")

    def test_non_restart_fields_save_normally_even_when_the_engine_is_blocked_or_unknown(self):
        for arrange in (lambda: self.state.write(mic_live=True),
                        lambda: self.state.write(remote_dj_connected=True, remote_dj_slot="occupied"),
                        lambda: self.state.remove()):
            arrange()
            self.restart.reset_mock()
            with patch.object(safety, "assess_engine_restart") as assess:
                response = self.post(
                    self.admin_user, vu_meter_min_db=self.pipeline.vu_meter_min_db - 3,
                    duck_level_db=-11.0, remote_dj_gain_db=7.5, ducking_enabled="on")
            self.assertEqual(response.status_code, 302)
            assess.assert_not_called()  # a no-restart save never consults engine status
            self.restart.assert_not_called()
            self.pipeline.refresh_from_db()
        self.assertEqual(DuckingConfig.load().duck_level_db, -11.0)

    def test_audio_gap_switch_alone_is_not_restart_triggering_and_saves(self):
        self.state.write(mic_live=True)
        response = self.post(self.admin_user, audio_gap_diagnostics_enabled="on")
        self.assertEqual(response.status_code, 302)
        self.restart.assert_not_called()

    # ------------------------------------------------------ blocked saves
    def test_studio_mic_live_refuses_a_restart_triggering_save_atomically(self):
        before = self.snapshot()
        self.state.write(mic_live=True)
        response = self.post(self.admin_user, sample_rate=self.new_rate,
                             duck_level_db=-14.0, remote_dj_gain_db=9.0, vu_meter_min_db=-55.0)
        self.assert_blocked_and_atomic(response, before)
        self.assertContains(response, "studio microphone is live")

    def test_remote_dj_connected_with_mic_off_refuses_a_gain_save(self):
        before = self.snapshot()
        self.state.write(remote_dj_connected=True, remote_dj_live=False, remote_dj_slot="occupied")
        response = self.post(self.superuser, program_gain_db=-1.0)
        self.assert_blocked_and_atomic(response, before)
        self.assertContains(response, "Remote DJ is connected")

    def test_webrtc_establishing_reconnecting_cleaning_and_live_all_refuse(self):
        before = self.snapshot()
        for overrides in (
            {"remote_dj_connection": {"status": "connecting"}},
            {"remote_dj_connected": True, "remote_dj_reconnecting": True, "remote_dj_slot": "occupied"},
            {"remote_dj_slot": "cleaning"},
            {"remote_dj_connected": True, "remote_dj_live": True, "remote_dj_slot": "occupied"},
        ):
            with self.subTest(overrides=overrides):
                self.state.write(**overrides)
                self.assert_blocked_and_atomic(self.post(self.superuser, sample_rate=self.new_rate), before)

    def test_missing_stale_or_malformed_status_refuses_and_writes_nothing(self):
        import time
        before = self.snapshot()
        for arrange in (lambda: self.state.remove(),
                        lambda: self.state.write(timestamp=time.time() - 120),
                        lambda: self.state.write_raw("}{")):
            arrange()
            self.assert_blocked_and_atomic(self.post(self.superuser, sample_rate=self.new_rate), before)

    def test_refused_save_is_audited_as_a_refusal_and_not_as_a_config_change(self):
        self.state.write(mic_live=True)
        self.post(self.admin_user, sample_rate=self.new_rate)
        (event,) = self.audit("Engine restart refused by restart safeguard")
        self.assertEqual(event.detail["entry_point"], "audio_pipeline")
        self.assertEqual(event.detail["target"]["changed_fields"], ["sample_rate"])
        self.assertEqual(event.detail["operator"]["username"], "ap-admin")
        self.assertEqual(self.config_events(), [])

    # ---------------------------------------------------- override contract
    def test_ordinary_administrator_cannot_override(self):
        before = self.snapshot()
        self.state.write(mic_live=True)
        response = self.post(self.admin_user, sample_rate=self.new_rate,
                             engine_restart_override="on", engine_restart_override_reason=REASON)
        self.assert_blocked_and_atomic(response, before)
        self.assertContains(response, "Only a superuser may override")

    def test_superuser_override_without_reason_is_refused(self):
        before = self.snapshot()
        self.state.write(mic_live=True)
        response = self.post(self.superuser, sample_rate=self.new_rate, engine_restart_override="on")
        self.assert_blocked_and_atomic(response, before)
        self.assertContains(response, "written reason")

    def test_superuser_override_saves_restarts_once_and_audits(self):
        self.state.write(mic_live=True)
        response = self.post(self.superuser, sample_rate=self.new_rate,
                             engine_restart_override="on", engine_restart_override_reason=REASON)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AudioPipeline.objects.get(pk=self.pipeline.pk).sample_rate, self.new_rate)
        self.restart.assert_called_once_with("isadoraair-engine.service")
        (decision,) = self.audit("Superuser override of engine restart safeguard")  # once, not twice
        self.assertEqual(decision.detail["operator"]["username"], "ap-root")
        self.assertEqual(decision.detail["reason"], REASON)
        self.assertEqual(decision.detail["target"],
                         {"unit": "isadoraair-engine.service", "changed_fields": ["sample_rate"]})
        self.assertEqual(decision.detail["engine_state"]["reasons"], [safety.STUDIO_MIC_LIVE])
        (outcome,) = self.audit("Overridden engine restart request outcome")
        self.assertEqual(outcome.detail["outcome"], "submitted")
        self.assertEqual(outcome.detail["detail"]["operation_id"], "op-ap")
        self.assertIs(outcome.detail["completion_claimed"], False)
        self.assertEqual(len(self.config_events()), 1)  # normal config audit is unchanged

    def test_unknown_status_override_recovers_a_failed_engine_configuration(self):
        self.state.remove()
        response = self.post(self.superuser, program_gain_db=-2.0,
                             engine_restart_override="on", engine_restart_override_reason=REASON)
        self.assertEqual(response.status_code, 302)
        self.restart.assert_called_once()

    def test_override_applies_to_one_save_only(self):
        self.state.write(mic_live=True)
        self.post(self.superuser, sample_rate=self.new_rate,
                  engine_restart_override="on", engine_restart_override_reason=REASON)
        self.restart.reset_mock()
        SystemEvent.objects.all().delete()  # forget the first save's audit rows
        pipeline = AudioPipeline.objects.get(pk=self.pipeline.pk)
        before = self.snapshot()
        # The next gain save, without the override fields, is refused again.
        response = self.post(self.superuser, sample_rate=pipeline.sample_rate,
                             program_gain_db=pipeline.program_gain_db - 1)
        self.assert_blocked_and_atomic(response, before)

    def test_unneeded_override_on_a_quiet_engine_is_not_recorded(self):
        self.post(self.superuser, sample_rate=self.new_rate,
                  engine_restart_override="on", engine_restart_override_reason=REASON)
        self.restart.assert_called_once()
        self.assertEqual(self.audit(), [])

    # ---------------------------------------------------- race: validate -> save
    def test_status_turning_blocked_after_validation_is_refused_by_save_model(self):
        # Form validation sees a quiet engine; the DJ connects before save_model.
        before = self.snapshot()
        real_assess = safety.assess_engine_restart
        calls = {"n": 0}

        def assess_then_block(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] > 1:
                self.state.write(remote_dj_connected=True, remote_dj_slot="occupied")
            return real_assess(*args, **kwargs)

        with patch.object(safety, "assess_engine_restart", side_effect=assess_then_block):
            response = self.post(self.superuser, sample_rate=self.new_rate)
        self.assertEqual(response.status_code, 403)  # save_model's authoritative recheck
        self.restart.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.config_events(), [])

    # ---------------------------------------------------- broker failure
    def test_failed_broker_submission_after_an_allowed_save_keeps_existing_behaviour(self):
        self.restart.side_effect = BackendRejectedError("down", error_code="ProtocolError")
        response = self.post(self.superuser, sample_rate=self.new_rate)
        self.assertEqual(response.status_code, 302)  # the config save is not rolled back (unchanged)
        self.assertEqual(AudioPipeline.objects.get(pk=self.pipeline.pk).sample_rate, self.new_rate)
        self.assertEqual(self.audit(), [])
        self.assertTrue(SystemEvent.objects.filter(
            title="Protected engine restart request failed after audio pipeline save").exists())

    def test_failed_broker_submission_after_an_override_is_audited_without_claiming_a_restart(self):
        self.state.write(mic_live=True)
        self.restart.side_effect = BackendRejectedError("down", error_code="ProtocolError")
        self.post(self.superuser, sample_rate=self.new_rate,
                  engine_restart_override="on", engine_restart_override_reason=REASON)
        (outcome,) = self.audit("Overridden engine restart request outcome")
        self.assertEqual(outcome.detail["outcome"], "broker_rejected")
        self.assertIs(outcome.detail["completion_claimed"], False)

    # ---------------------------------------------------- form surface
    def test_change_form_shows_the_override_controls(self):
        client = Client()
        client.force_login(self.superuser)
        html = client.get(self.url).content.decode()
        self.assertIn('name="engine_restart_override"', html)
        self.assertIn('name="engine_restart_override_reason"', html)
