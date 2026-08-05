"""probe_audio_silence tests -- the null-is_blank three-state fix from
the 2026-08-05 hardening pass is the headline regression test here."""
import json
import time
from pathlib import Path

from django.test import TestCase

from monitoring.models import MonitorCheck
from monitoring.services import probes


def make_check(**overrides):
    defaults = dict(name="Encoder Audio (test)", kind="audio_silence", silence_device_slug="unittestslug")
    defaults.update(overrides)
    return MonitorCheck(**defaults)


class ProbeAudioSilenceTests(TestCase):
    def setUp(self):
        self.path = Path("/run/isadoraair/liquidsoap_silence_unittestslug.json")
        self.addCleanup(lambda: self.path.unlink(missing_ok=True))

    def write(self, **fields):
        self.path.write_text(json.dumps(fields), encoding="utf-8")

    def test_no_state_file_is_unknown(self):
        status, detail = probes.probe_audio_silence(make_check())
        self.assertEqual(status, "unknown")

    def test_is_blank_null_is_unknown_not_ok(self):
        """The exact regression: a truthiness test (`if is_blank:`)
        treats None the same as False, which is what let a
        crash-looping encoder -- re-asserting is_blank=null every
        15-20s, always fresher than the staleness window -- report
        "ok" throughout a real production outage. Must be "unknown"."""
        self.write(status="starting", is_blank=None, timestamp=time.time(), since=time.time())
        status, detail = probes.probe_audio_silence(make_check())
        self.assertEqual(status, "unknown")
        self.assertIsNone(detail["is_blank"])

    def test_is_blank_true_is_critical(self):
        self.write(is_blank=True, timestamp=time.time(), since=time.time())
        status, detail = probes.probe_audio_silence(make_check())
        self.assertEqual(status, "critical")

    def test_is_blank_false_is_ok(self):
        self.write(is_blank=False, timestamp=time.time(), since=time.time())
        status, detail = probes.probe_audio_silence(make_check())
        self.assertEqual(status, "ok")

    def test_stale_timestamp_is_unknown_even_with_is_blank_false(self):
        self.write(is_blank=False, timestamp=time.time() - 1000, since=time.time() - 1000)
        status, detail = probes.probe_audio_silence(make_check())
        self.assertEqual(status, "unknown")

    def test_unreadable_file_is_unknown(self):
        self.path.write_text("not json", encoding="utf-8")
        status, detail = probes.probe_audio_silence(make_check())
        self.assertEqual(status, "unknown")
        self.assertIn("error", detail)
