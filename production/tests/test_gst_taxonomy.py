"""Codex Blocker 5: GStreamer runtime-capability failures are retryable
infrastructure, never an ``invalid`` verdict on good media.

Real conditions, not mocks, wherever GStreamer can produce them:
* a missing decoder -- the decoder's rank forced to NONE
  (GST_PLUGIN_FEATURE_RANK), so decodebin cannot autoplug it;
* missing elements -- an empty private plugin registry;
* no GI/GStreamer -- the probe interpreter run with ``-S`` (no site-packages);
* genuinely undecodable bytes -- real structurally corrupt files.
Every GStreamer environment override also points GST_REGISTRY at a private
temporary file so the user's registry cache is never touched.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase

from production.models import ProductionMedia
from production.services import gst_probe, intake, validation

from .support import IsolatedMediaRootMixin, fixture

PROBE = Path(gst_probe.__file__)


class GstEnv:
    def __init__(self, testcase, **variables):
        self.dir = Path(tempfile.mkdtemp(prefix="gst-env-"))
        testcase.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.variables = {"GST_REGISTRY": str(self.dir / "registry.bin"), **variables}

    def patch(self):
        return mock.patch.dict(os.environ, self.variables)


def no_flac_decoder(testcase):
    return GstEnv(testcase, GST_PLUGIN_FEATURE_RANK="flacdec:NONE,avdec_flac:NONE").patch()


def empty_registry(testcase):
    env = GstEnv(testcase)
    (env.dir / "plugins").mkdir()
    env.variables.update(GST_PLUGIN_SYSTEM_PATH_1_0=str(env.dir / "plugins"),
                         GST_PLUGIN_PATH_1_0=str(env.dir / "plugins"))
    return env.patch()


def write_fixture(testcase, name):
    directory = Path(tempfile.mkdtemp(prefix="gst-fixture-"))
    testcase.addCleanup(shutil.rmtree, directory, ignore_errors=True)
    path = directory / name.replace("/", "_")
    path.write_bytes(fixture(name))
    return path


def run_probe(path, *interpreter_flags):
    completed = subprocess.run([sys.executable, *interpreter_flags, str(PROBE), "--timeout", "30", str(path)],
                               capture_output=True, text=True, timeout=60)
    return json.loads(completed.stdout)


class ProbeChildTaxonomyTests(SimpleTestCase):
    """The child's own classification on real inputs and real runtimes."""

    def test_1_ordinary_valid_media_decodes(self):
        for name in ("wav16_mono.wav", "flac.flac", "opus.ogg", "aac.m4a", "mp3.mp3"):
            with self.subTest(fixture=name):
                result = run_probe(write_fixture(self, name))
                self.assertEqual(result["status"], "eos")
                self.assertGreater(result["buffers"], 0)

    def test_2_genuinely_undecodable_bytes_are_a_media_error(self):
        for name in ("truncated_aac.m4a", "lying_fmt.wav"):
            with self.subTest(fixture=name):
                result = run_probe(write_fixture(self, name))
                self.assertEqual((result["status"], result["reason"]), ("media_error", "stream_undecodable"))
                self.assertEqual(result["error_domain"], "gst-stream-error-quark")

    def test_3_a_missing_decoder_is_a_capability_error(self):
        with no_flac_decoder(self):
            result = run_probe(write_fixture(self, "flac.flac"))
        self.assertEqual((result["status"], result["reason"]), ("capability_error", "missing_plugin"))
        self.assertTrue(result["missing_plugins"])

    def test_4_no_gi_is_a_capability_error(self):
        result = run_probe(write_fixture(self, "flac.flac"), "-S")
        self.assertEqual((result["status"], result["reason"]), ("capability_error", "gi_unavailable"))

    def test_5_a_missing_element_is_a_capability_error(self):
        with empty_registry(self):
            result = run_probe(write_fixture(self, "flac.flac"))
        self.assertEqual((result["status"], result["reason"]), ("capability_error", "element_unavailable"))
        self.assertEqual(result["element"], "filesrc")

    def test_output_is_bounded_structured_evidence(self):
        result = run_probe(write_fixture(self, "lying_fmt.wav"))
        self.assertLess(len(json.dumps(result)), 1024)
        self.assertNotIn("debug", result)
        self.assertNotIn("message", result)


class ClassifyErrorTableTests(SimpleTestCase):
    """Every GError domain/code decision, by structured identity only."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import GLib, Gst
        Gst.init(None)
        cls.Gst, cls.GLib = Gst, GLib

    def classify(self, quark, code, missing=False):
        error = self.GLib.Error.new_literal(quark, "localized text is never consulted", int(code))
        return gst_probe._classify_error(self.Gst, error, missing)

    def test_the_table(self):
        Gst = self.Gst
        stream, core, resource = Gst.StreamError, Gst.CoreError, Gst.ResourceError
        expected = [
            (stream.quark(), stream.DECODE, "media_error"), (stream.quark(), stream.DEMUX, "media_error"),
            (stream.quark(), stream.FORMAT, "media_error"), (stream.quark(), stream.WRONG_TYPE, "media_error"),
            (stream.quark(), stream.DECRYPT, "media_error"), (stream.quark(), stream.DECRYPT_NOKEY, "media_error"),
            (stream.quark(), stream.CODEC_NOT_FOUND, "capability_error"),
            (stream.quark(), stream.TYPE_NOT_FOUND, "capability_error"),
            (stream.quark(), stream.NOT_IMPLEMENTED, "capability_error"),
            (core.quark(), core.MISSING_PLUGIN, "capability_error"),
            (core.quark(), core.NEGOTIATION, "capability_error"),
            (stream.quark(), stream.FAILED, "infrastructure_error"),
            (resource.quark(), resource.READ, "infrastructure_error"),
            (resource.quark(), resource.NOT_FOUND, "infrastructure_error"),
            (core.quark(), core.STATE_CHANGE, "infrastructure_error"),
            (Gst.LibraryError.quark(), Gst.LibraryError.INIT, "infrastructure_error"),
        ]
        for quark, code, status in expected:
            with self.subTest(code=code):
                self.assertEqual(self.classify(quark, code)["status"], status)

    def test_a_missing_plugin_message_overrides_any_error_code(self):
        stream = self.Gst.StreamError
        self.assertEqual(self.classify(stream.quark(), stream.DECODE, missing=True)["status"], "capability_error")


class ParentMappingTests(IsolatedMediaRootMixin, TestCase):
    """validation maps ONLY media_error to an invalid verdict."""

    def stored(self, name="flac.flac"):
        return intake.ingest_stream(io.BytesIO(fixture(name)), kind="upload", validate=False).media

    def assert_retryable(self, media, code):
        outcome = validation.validate_media(media)
        self.assertEqual((outcome.status, outcome.code), ("infrastructure_error", code))
        row = ProductionMedia.objects.get(pk=media.pk)
        self.assertEqual((row.validation_state, row.validation_code), ("unvalidated", code))

    def test_3_valid_media_with_its_decoder_unavailable_stays_retryable(self):
        media = self.stored()
        with no_flac_decoder(self):
            self.assert_retryable(media, "engine_capability_unavailable")

    def test_4_no_gi_runtime_stays_retryable(self):
        media = self.stored()
        with mock.patch.object(validation, "GSTREAMER_PROBE_INTERPRETER", (sys.executable, "-S")):
            self.assert_retryable(media, "engine_capability_unavailable")

    def test_5_missing_elements_stay_retryable(self):
        media = self.stored()
        with empty_registry(self):
            self.assert_retryable(media, "engine_capability_unavailable")

    def test_6_after_the_capability_is_restored_the_retry_succeeds(self):
        media = self.stored()
        with no_flac_decoder(self):
            self.assert_retryable(media, "engine_capability_unavailable")
        with empty_registry(self):
            self.assert_retryable(media, "engine_capability_unavailable")
        outcome = validation.validate_media(media)                       # runtime restored
        self.assertEqual(outcome.status, "valid")
        row = ProductionMedia.objects.get(pk=media.pk)
        self.assertEqual((row.validation_state, row.validation_code), ("valid", "ok"))
        self.assertEqual(row.probe["engine_decode"]["status"], "eos")

    def test_2_genuinely_undecodable_bytes_get_an_engine_media_verdict(self):
        for name in ("truncated_aac.m4a", "lying_fmt.wav"):
            with self.subTest(fixture=name):
                evidence, facts = {}, {"probe": None}
                outcome = validation._engine_decode(write_fixture(self, name), evidence, facts, None)
                self.assertEqual((outcome.status, outcome.code), ("invalid", "engine_decode_failed"))
                self.assertEqual(evidence["engine_decode"]["status"], "media_error")

    def test_every_child_status_maps_deterministically(self):
        table = {
            "eos": None, "media_error": ("invalid", "engine_decode_failed"),
            "capability_error": ("infrastructure_error", "engine_capability_unavailable"),
            "timeout": ("infrastructure_error", "engine_probe_timeout"),
            "infrastructure_error": ("infrastructure_error", "engine_probe_failed"),
            "something-new": ("infrastructure_error", "engine_probe_failed"),
            "error": ("infrastructure_error", "engine_probe_failed"),            # the OLD verdict word: never invalid
        }
        for status, expected in table.items():
            fake = {"status": "ok", "stdout": json.dumps({"status": status, "reason": "x"}), "stderr": ""}
            with self.subTest(status=status), mock.patch.object(validation, "_run", return_value=fake):
                outcome = validation._engine_decode("/unused", {}, {}, None)
                self.assertEqual(None if outcome is None else (outcome.status, outcome.code), expected)
