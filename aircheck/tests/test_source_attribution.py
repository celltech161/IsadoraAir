"""Roadmap 2.0-A: the Aircheck capture source is the default encoder group's
shared ``input.alsa`` source (``DEFAULT_INPUT_DEVICE``, "airtap"). It is not a
per-recorder setting.

Three things are proven here, none of which opens an audio device, runs
Liquidsoap, talks to the live encoder group or touches a station path:

* Django Admin no longer offers ``AircheckConfig.source_device`` as an editable
  control, shows the canonical source read-only, and a normal or forged save
  never changes the stored legacy value;
* every new session records the canonical identifier, ignores a stale or wrong
  legacy configuration value, and leaves existing session labels alone;
* the generated encoder topology is unchanged: one shared capture input and one
  recorder in the default group only, and a change to the legacy value changes
  no script, fingerprint or running child.
"""
import re
import subprocess
from unittest.mock import patch

from django.contrib import admin as django_admin
from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

import encoders.services.encoder_manager as em
from aircheck.models import AircheckConfig, AircheckSession
from aircheck.services import recorder
from aircheck.tests.test_recorder import AircheckRecorderFixtureMixin
from encoders.services import lkg as lkg_module
from encoders.services import preflight as preflight_module
from encoders.tests.test_reconciliation import (
    ReconciliationFixtureMixin,
    make_saved_encoder,
)

LEGACY_WRONG = "plughw:3,1"
LEGACY_OTHER = "hw:Loopback_1,1,0"


def _admin_post_data(**overrides):
    data = {
        "audio_format": "mp3",
        "bitrate": "320k",
        "output_directory": "/tmp/aircheck-test-unused",
        "filename_template": "aircheck-test-%H%M%S",
    }
    data.update(overrides)
    return data


@override_settings(SECURE_SSL_REDIRECT=False)
class AircheckAdminSourceTests(TestCase):
    def setUp(self):
        self.cfg = AircheckConfig.objects.create(
            pk=1, audio_format="mp3", bitrate="320k", source_device=LEGACY_WRONG,
            output_directory="/tmp/aircheck-test-unused",
            filename_template="aircheck-test-%H%M%S",
        )
        self.url = reverse("admin:aircheck_aircheckconfig_change", args=[1])
        admin_user = User.objects.create_superuser(
            "aircheck-admin-test", "aircheck-admin-test@example.invalid", "password")
        self.client.force_login(admin_user)

    def _stored_source(self):
        return AircheckConfig.objects.values_list("source_device", flat=True).get(pk=1)

    def test_page_shows_canonical_source_read_only_with_explanation(self):
        # ALSA discovery must play no part in rendering the page.
        with patch.object(subprocess, "run", side_effect=AssertionError("device enumeration")), \
             patch.object(subprocess, "Popen", side_effect=AssertionError("device enumeration")):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn(em.DEFAULT_INPUT_DEVICE, body)
        self.assertIn("shared default encoder-group source", body)
        self.assertIn("same program feed used by the default Liquidsoap encoder group", body)
        self.assertIn("no independent capture device selection", body.lower())

    def test_page_does_not_show_the_legacy_value_or_an_input_for_it(self):
        body = self.client.get(self.url).content.decode("utf-8")
        self.assertNotIn(LEGACY_WRONG, body)
        self.assertNotIn('name="source_device"', body)
        self.assertNotIn('id="id_source_device"', body)
        self.assertNotRegex(body, r"<select[^>]*source_device")

    def test_source_device_is_not_an_editable_admin_field(self):
        model_admin = django_admin.site._registry[AircheckConfig]
        request = self.client.get(self.url).wsgi_request
        self.assertNotIn("source_device", model_admin.get_form(request, self.cfg).base_fields)
        flattened = [
            name for _, options in model_admin.get_fieldsets(request, self.cfg)
            for name in options["fields"]
        ]
        self.assertNotIn("source_device", flattened)
        self.assertIn("effective_source", model_admin.get_readonly_fields(request, self.cfg))

    def test_unrelated_valid_save_preserves_the_legacy_value(self):
        response = self.client.post(self.url, _admin_post_data(bitrate="256k"))
        self.assertEqual(response.status_code, 302, getattr(response, "context_data", None))
        self.cfg.refresh_from_db()
        self.assertEqual(self.cfg.bitrate, "256k")
        self.assertEqual(self._stored_source(), LEGACY_WRONG)

    def test_forged_legacy_source_post_cannot_change_the_stored_value(self):
        response = self.client.post(
            self.url, _admin_post_data(bitrate="192k", source_device="hw:0,0"))
        self.assertEqual(response.status_code, 302)
        self.cfg.refresh_from_db()
        self.assertEqual(self.cfg.bitrate, "192k")
        self.assertEqual(self._stored_source(), LEGACY_WRONG)

    def test_admin_save_starts_no_encoder_work_and_opens_no_device(self):
        # A save of this singleton must not reach the telnet control, spawn a
        # process or touch the encoder supervisor.
        with patch.object(recorder, "_send_telnet", side_effect=AssertionError("telnet")), \
             patch.object(subprocess, "Popen", side_effect=AssertionError("spawn")), \
             patch.object(em.EncoderManager, "start", side_effect=AssertionError("manager")):
            response = self.client.post(self.url, _admin_post_data(source_device="hw:0,0"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._stored_source(), LEGACY_WRONG)

    def test_model_field_and_default_remain_for_compatibility(self):
        field = AircheckConfig._meta.get_field("source_device")
        self.assertEqual(field.max_length, 64)
        self.assertEqual(field.default, "airtap")


@override_settings(SECURE_SSL_REDIRECT=False)
class NewSessionSourceAttributionTests(AircheckRecorderFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.cfg.source_device = LEGACY_WRONG
        self.cfg.save()

    def _history(self):
        completed = AircheckSession.objects.create(
            filename="/tmp/a-complete.mp3", audio_format="mp3", bitrate="320k",
            source_device=LEGACY_OTHER, still_running=False)
        failed = AircheckSession.objects.create(
            filename="/tmp/a-failed.mp3", audio_format="mp3", bitrate="320k",
            source_device="legacy:label", still_running=False,
            exit_note="telnet reopen failed at Stop: cannot reach liquidsoap telnet")
        return {completed.pk: LEGACY_OTHER, failed.pk: "legacy:label"}

    def _labels(self, pks):
        return dict(AircheckSession.objects.filter(pk__in=pks).values_list("pk", "source_device"))

    def test_new_session_records_canonical_source_not_the_legacy_value(self):
        history = self._history()
        with patch.object(recorder, "_send_telnet", return_value="reopened") as telnet:
            session, error = recorder.start_recording()
        self.assertIsNone(error)
        self.assertEqual(session.source_device, em.DEFAULT_INPUT_DEVICE)
        self.assertEqual(session.source_device, "airtap")
        session.refresh_from_db()
        self.assertEqual(session.source_device, "airtap")
        telnet.assert_called_once_with(f"{recorder.AIRCHECK_OUTPUT_ID}.reopen")
        self.assertEqual(self._labels(history), history)

    def test_existing_active_session_keeps_its_label_and_is_not_rewritten(self):
        active = AircheckSession.objects.create(
            filename="/tmp/a-active.mp3", audio_format="mp3", bitrate="320k",
            source_device=LEGACY_OTHER, still_running=True)
        with patch.object(recorder, "_send_telnet", side_effect=AssertionError("telnet")):
            session, note = recorder.start_recording()
        self.assertEqual(note, "already recording")
        self.assertEqual(session.pk, active.pk)
        active.refresh_from_db()
        self.assertEqual(active.source_device, LEGACY_OTHER)

    def test_stop_and_finalize_leave_every_label_unchanged(self):
        history = self._history()
        with patch.object(recorder, "_send_telnet", return_value="reopened"):
            session, _ = recorder.start_recording()
        self.write_working_file(1234)
        with patch.object(recorder, "_send_telnet", return_value="reopened"):
            stopped, error = recorder.stop_recording()
        self.assertIsNone(error)
        self.assertEqual(stopped.source_device, "airtap")
        stopped.refresh_from_db()
        self.assertEqual(stopped.source_device, "airtap")
        self.assertEqual(stopped.pk, session.pk)
        self.assertEqual(self._labels(history), history)

    def test_telnet_failure_creates_no_session_and_no_label(self):
        with patch.object(recorder, "_send_telnet", side_effect=recorder.TelnetError("down")):
            session, error = recorder.start_recording()
        self.assertIsNone(session)
        self.assertIn("liquidsoap telnet", error)
        self.assertFalse(AircheckSession.objects.exists())

    def test_start_api_is_the_only_creation_path_and_records_canonical_source(self):
        # api_aircheck_start -> start_recording is the single caller of
        # AircheckSession.objects.create, so this exercises the real path.
        staff = User.objects.create_superuser(
            "aircheck-start-test", "aircheck-start-test@example.invalid", "password")
        self.client.force_login(staff)
        with patch.object(recorder, "_send_telnet", return_value="reopened"):
            response = self.client.post(reverse("aircheck:api-start"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()["session"]
        self.assertEqual(payload["source_device"], "airtap")
        self.assertEqual(AircheckSession.objects.get(pk=payload["id"]).source_device, "airtap")

    def test_every_source_label_in_the_whole_tree_has_one_writer(self):
        # Static guard for "every actual new-session path": the only place a
        # session row is created is the recorder's start_recording.
        import pathlib
        root = pathlib.Path(recorder.__file__).resolve().parents[2]
        writers = []
        for path in root.rglob("*.py"):
            parts = path.relative_to(root).parts
            if "tests" in parts or "migrations" in parts or parts[0] in {"venv", "node_modules"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"(?<!class )AircheckSession\s*\(|AircheckSession\.objects\.(create|bulk_create|get_or_create|update_or_create)", text):
                writers.append(str(path.relative_to(root)))
        self.assertEqual(writers, ["aircheck/services/recorder.py"])


class SharedSourceTopologyTests(ReconciliationFixtureMixin, TransactionTestCase):
    """Isolated harness only: Popen is faked, script/state/LKG/candidate dirs
    are per-test temporaries, preflight is stubbed, and no liquidsoap runs."""

    def setUp(self):
        super().setUp()
        AircheckConfig.objects.update_or_create(
            pk=1, defaults=dict(
                audio_format="mp3", bitrate="320k", source_device=LEGACY_WRONG,
                output_directory="/tmp/aircheck-test-unused"))

    def _script(self, input_device):
        return (self.script_dir / f"encoders_{em._slug(input_device)}.liq").read_text()

    def test_default_group_has_one_shared_input_and_one_recorder(self):
        manager = em.EncoderManager()
        self.bootstrap_accepted(
            manager, em.DEFAULT_INPUT_DEVICE, make_saved_encoder(name="air", host="10.0.0.1"))
        script = self._script(em.DEFAULT_INPUT_DEVICE)
        self.assertEqual(len(re.findall(r"\binput\.alsa\(", script)), 1)
        self.assertIn('input.alsa(buffer_size=1.0, device="airtap")', script)
        self.assertEqual(script.count("aircheck_output = output.file("), 1)
        self.assertEqual(script.count("settings.server.telnet.set(true)"), 1)
        # The recorder consumes the same `source` the encoder outputs consume.
        self.assertRegex(script, r"aircheck_output = output\.file\((?:.|\n)*?\n  source\n\)")
        self.assertNotIn(LEGACY_WRONG, script)
        self.assertNotIn("ffmpeg", script)

    def test_nondefault_group_has_no_recorder_telnet_or_aircheck_control(self):
        manager = em.EncoderManager()
        self.bootstrap_accepted(
            manager, em.DEFAULT_INPUT_DEVICE, make_saved_encoder(name="air", host="10.0.0.1"))
        self.bootstrap_accepted(
            manager, LEGACY_WRONG,
            make_saved_encoder(name="other", host="10.0.0.2", input_device=LEGACY_WRONG))
        other = self._script(LEGACY_WRONG)
        self.assertIn(f'input.alsa(buffer_size=1.0, device="{LEGACY_WRONG}")', other)
        for forbidden in ("aircheck", "output.file(", "telnet"):
            self.assertNotIn(forbidden, other)
        self.assertEqual(len(re.findall(r"\binput\.alsa\(", other)), 1)

    def test_legacy_source_value_changes_no_script_fingerprint_or_child(self):
        manager = em.EncoderManager()
        encoder = make_saved_encoder(name="air", host="10.0.0.1")
        pid, generation = self.bootstrap_accepted(manager, em.DEFAULT_INPUT_DEVICE, encoder)
        before_script = self._script(em.DEFAULT_INPUT_DEVICE)
        before_fp = lkg_module.compute_fingerprint(em.DEFAULT_INPUT_DEVICE, [encoder])
        before_popens = self.popen_call_count()
        proc = self._live_procs[pid]

        AircheckConfig.objects.filter(pk=1).update(source_device="hw:0,0")
        rendered = em.build_liquidsoap_script(
            em.DEFAULT_INPUT_DEVICE, [encoder], host_aircheck=True, generation=generation)
        self.assertEqual(
            re.sub(r'generation = "[^"]*"', "", rendered),
            re.sub(r'generation = "[^"]*"', "", before_script))
        self.assertEqual(
            lkg_module.compute_fingerprint(em.DEFAULT_INPUT_DEVICE, [encoder]), before_fp)

        with patch.object(preflight_module, "run_preflight",
                          return_value=preflight_module.PreflightResult(ok=True)):
            manager._check_health()
        self.assertEqual(manager._current[em.DEFAULT_INPUT_DEVICE]["pid"], pid)
        self.assertEqual(manager._current[em.DEFAULT_INPUT_DEVICE]["generation"], generation)
        self.assertEqual(self.popen_call_count(), before_popens)
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()

    def test_canonical_source_is_the_default_group_contract(self):
        self.assertEqual(em.DEFAULT_INPUT_DEVICE, "airtap")
        # An Encoder row with no input_device belongs to the default group.
        self.assertEqual(em._effective_input_device(make_saved_encoder(name="blank")), "airtap")
