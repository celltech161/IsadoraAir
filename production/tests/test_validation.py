"""Authoritative validation: real ffmpeg/ffprobe/GStreamer, real fixtures.

Valid formats are proven by decoding them. Invalid classes are proven with real
malformed files. Infrastructure failures are proven to be retryable and are
NEVER recorded as an invalid verdict.
"""
import json
import threading
from decimal import Decimal
from unittest import mock

from django.test import TestCase

from production.errors import MediaInconsistent, MediaPurged, MediaRejected
from production.models import ProductionMedia
from production.policy import MediaPolicy
from production.services import intake, layout, validation

from .support import mark_purged_leaving_bytes, IsolatedMediaRootMixin, fixture


class FileLike:
    def __init__(self, data):
        self.data, self.position = data, 0

    def read(self, size=-1):
        chunk = self.data[self.position:self.position + (size if size and size > 0 else len(self.data))]
        self.position += len(chunk)
        return chunk


def ingest(name_or_bytes, **kwargs):
    data = fixture(name_or_bytes) if isinstance(name_or_bytes, str) else name_or_bytes
    kwargs.setdefault("kind", "upload")
    return intake.ingest_stream(FileLike(data), **kwargs)


class Base(IsolatedMediaRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        validation.clear_capability_cache()
        self.addCleanup(validation.clear_capability_cache)

    def stored(self, name):
        return ingest(name, validate=False).media


class SupportedFormatTests(Base):
    # fixture, container, codec, channels, sample rate
    VALID = [
        ("wav16_mono.wav", "wav", "pcm_s16le", 1, 44100),
        ("wav24_stereo.wav", "wav", "pcm_s24le", 2, 48000),
        ("flac.flac", "flac", "flac", 1, 44100),
        ("mp3.mp3", "mp3", "mp3", 1, 44100),
        ("opus.ogg", "ogg", "opus", 1, 48000),
        ("vorbis.ogg", "ogg", "vorbis", 1, 44100),
        ("opus_seekable.webm", "matroska", "opus", 1, 48000),
        ("opus_live.webm", "matroska", "opus", 1, 48000),
        ("aac.m4a", "mp4", "aac", 1, 44100),
        ("alac.m4a", "mp4", "alac", 1, 44100),
        ("pcm.aiff", "aiff", "pcm_s16be", 1, 44100),
        ("mp3_cover.mp3", "mp3", "mp3", 1, 44100),
    ]

    def test_every_advertised_format_family_decodes_in_ffmpeg_and_the_engines_gstreamer(self):
        families = set()
        for name, container, codec, channels, rate in self.VALID:
            with self.subTest(fixture=name):
                result = ingest(name, validate=True)
                media = ProductionMedia.objects.get(pk=result.media.pk)
                self.assertEqual(media.validation_state, "valid")
                self.assertEqual(media.validation_code, "ok")
                self.assertEqual((media.container, media.codec, media.channels, media.sample_rate),
                                 (container, codec, channels, rate))
                self.assertLess(abs(media.decoded_duration_seconds - Decimal(4)), Decimal("0.05"))
                self.assertEqual(media.probe["engine_decode"]["status"], "eos")     # GStreamer proved it
                families.add(container)
        self.assertEqual(families, {"wav", "aiff", "flac", "mp3", "ogg", "matroska", "mp4"})

    def test_browser_webm_without_a_header_duration_is_valid_and_its_duration_comes_from_decoding(self):
        media = ingest("opus_live.webm", validate=True).media
        self.assertIsNone(media.header_duration_seconds)
        self.assertEqual(str(media.decoded_duration_seconds), "4.007000")
        self.assertEqual(media.validation_state, "valid")

    def test_decoded_duration_has_microsecond_precision_and_is_a_decimal(self):
        media = ingest("alac.m4a", validate=True).media
        self.assertIsInstance(media.decoded_duration_seconds, Decimal)
        self.assertEqual(media.decoded_duration_seconds.as_tuple().exponent, -6)

    def test_attached_cover_art_is_tolerated_and_recorded(self):
        media = ingest("mp3_cover.mp3", validate=True).media
        pictures = [s for s in media.probe["streams"] if s["attached_pic"]]
        self.assertEqual(len(pictures), 1)
        self.assertEqual(media.validation_state, "valid")

    def test_a_silent_recording_is_valid_media(self):
        # Deliberate: silence is a domain/recorder policy, not an intrinsic defect
        # (see docs/PRODUCTION_MEDIA.md) -- Phase A does no level measurement.
        self.assertEqual(ingest("silence.wav", validate=True).media.validation_state, "valid")

    def test_a_very_short_clip_is_valid_at_the_platform_level(self):
        self.assertEqual(ingest("tiny.wav", validate=True).media.validation_state, "valid")

    def test_the_persisted_evidence_is_bounded_and_never_contains_tags_or_tool_output(self):
        media = ingest("big_tags.flac", validate=True).media         # 100 KB comment tag in the file
        encoded = json.dumps(media.probe)
        self.assertLess(len(encoded), 4096)
        self.assertNotIn("AAAA", encoded)
        self.assertEqual(set(media.probe), {
            "validator_version", "sniffed_container", "format_name", "nb_streams", "streams", "tools", "engine_decode",
        })

    def test_the_tool_versions_are_recorded_as_evidence(self):
        tools = ingest("flac.flac", validate=True).media.probe["tools"]
        self.assertTrue(tools["ffprobe"] and tools["ffmpeg"])


class InvalidMediaTests(Base):
    INVALID = [
        ("video_audio.mp4", "video_stream_present"),
        ("two_audio.mka", "multiple_audio_streams"),
        ("three_channel.wav", "channels_out_of_range"),
        ("rate_4k.wav", "sample_rate_out_of_range"),
        ("rate_384k.wav", "sample_rate_out_of_range"),
        ("adpcm.wav", "unsupported_codec"),
        ("flac_in_ogg.ogg", "unsupported_codec"),
        ("mp2.mp2", "unsupported_codec"),
        ("raw_aac.aac", "unsupported_container"),
        ("garbage.bin", "unsupported_container"),
        ("text.wav", "unsupported_container"),
        ("hls_playlist.m3u8", "unsupported_container"),
        ("concat_list.txt", "unsupported_container"),
        ("truncated_wav16_mono.wav", "decode_error"),
        ("truncated_flac.flac", "decode_error"),
        ("truncated_pcm.aiff", "decode_error"),
        ("corrupt_flac.flac", "decode_error"),
        ("truncated_mp3.mp3", "truncated_or_inconsistent"),
        ("truncated_opus_seekable.webm", "truncated_or_inconsistent"),
        ("truncated_aac.m4a", "unreadable_container"),
        ("truncated_alac.m4a", "unreadable_container"),
    ]

    def test_invalid_input_is_refused_at_intake_with_a_stable_code_and_creates_nothing(self):
        for name, code in self.INVALID:
            with self.subTest(fixture=name), self.assertRaises(MediaRejected) as caught:
                ingest(name, validate=True)
            self.assertEqual(caught.exception.code, code)
            self.assertIn(code, validation.MEDIA_VERDICT_CODES)
            self.assertEqual(caught.exception.outcome.status, "invalid")
        self.assertEqual(ProductionMedia.objects.count(), 0)
        self.assertEqual(self.list_files("media") + self.list_files("incoming"), [])

    def test_deferred_validation_records_the_same_verdict_on_the_row(self):
        for name, code in self.INVALID:
            with self.subTest(fixture=name):
                media = self.stored(name)
                outcome = validation.validate_media(media)
                row = ProductionMedia.objects.get(pk=media.pk)
                self.assertEqual((outcome.status, outcome.code), ("invalid", code))
                self.assertEqual((row.validation_state, row.validation_code), ("invalid", code))
                self.assertIsNotNone(row.validated_at)

    def test_invalid_media_can_be_retained_for_forensics_when_asked(self):
        result = ingest("truncated_flac.flac", validate=True, retain_invalid=True)
        self.assertEqual(result.media.validation_state, "invalid")
        self.assertEqual(result.media.validation_code, "decode_error")
        self.assertEqual(len(self.list_files("media")), 1)

    def test_truncation_is_caught_only_where_the_container_carries_an_expected_length(self):
        # Documented limit: a byte-truncated Ogg is a shorter, self-consistent file.
        self.assertEqual(ingest("truncated_opus.ogg", validate=True).media.validation_state, "valid")

    def test_unsupported_content_never_reaches_a_media_tool(self):
        # The sniff gate: playlists/descriptors are refused before ffprobe/ffmpeg
        # could be allowed to follow any reference inside them.
        for name in ("hls_playlist.m3u8", "concat_list.txt", "text.wav", "garbage.bin", "raw_aac.aac"):
            with self.subTest(fixture=name), mock.patch.object(validation, "_run", side_effect=AssertionError("tool run")), \
                    self.assertRaises(MediaRejected) as caught:
                ingest(name, validate=True)
            self.assertEqual(caught.exception.code, "unsupported_container")

    def test_every_tool_call_is_shell_free_forced_to_one_demuxer_and_file_only(self):
        calls = []
        real = validation._run

        def spy(args, **kwargs):
            calls.append(list(args))
            return real(args, **kwargs)
        with mock.patch.object(validation, "_run", side_effect=spy):
            ingest("wav16_mono.wav", validate=True)
        inspected = [args for args in calls if args[0] in ("ffprobe", "ffmpeg") and "-i" in args or "-show_entries" in args]
        self.assertGreaterEqual(len(inspected), 2)
        for args in inspected:
            self.assertTrue(all(isinstance(item, str) for item in args))
            self.assertEqual(args[args.index("-protocol_whitelist") + 1], "file")
            self.assertEqual(args[args.index("-f") + 1], "wav")
            self.assertTrue(args[-1].startswith("/") or args[-1] == "-")
        decode = next(args for args in inspected if args[0] == "ffmpeg")
        self.assertIn("-xerror", decode)
        self.assertEqual(decode[decode.index("-map") + 1], "0:a:0")

    def test_a_missing_file_for_a_stored_row_is_an_inconsistency_not_an_invalid_verdict(self):
        media = self.stored("wav16_mono.wav")
        layout.resolve_storage_path(media.storage_key).unlink()
        with self.assertRaises(MediaInconsistent) as caught:
            validation.validate_media(media)
        self.assertEqual(caught.exception.code, "missing_bytes")
        self.assertEqual(ProductionMedia.objects.get(pk=media.pk).validation_state, "unvalidated")


class PolicyTests(Base):
    def test_caller_bounds_refuse_the_intake_without_condemning_the_media(self):
        for policy, code in (
            (MediaPolicy(max_duration_seconds=2), "too_long"),
            (MediaPolicy(min_duration_seconds=10), "too_short"),
        ):
            with self.subTest(code=code), self.assertRaises(MediaRejected) as caught:
                ingest("wav16_mono.wav", validate=True, policy=policy)
            self.assertEqual(caught.exception.code, code)
            self.assertTrue(caught.exception.outcome.is_valid)          # good media, wrong for this consumer
        self.assertEqual(ProductionMedia.objects.count(), 0)
        # ...and the same bytes are fine for a consumer with different bounds.
        self.assertEqual(ingest("wav16_mono.wav", validate=True,
                                policy=MediaPolicy(min_duration_seconds=1, max_duration_seconds=10)).media.validation_state, "valid")

    def test_policy_object_rejects_nonsense(self):
        for kwargs in ({"min_duration_seconds": 0}, {"max_duration_seconds": -1},
                       {"min_duration_seconds": 5, "max_duration_seconds": 2}, {"max_bytes": 0},
                       {"max_bytes": True}, {"min_duration_seconds": "5"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                MediaPolicy(**kwargs)

    def test_engine_decode_can_be_skipped_by_a_consumer_that_never_airs_the_media(self):
        with mock.patch.object(validation, "_engine_decode", side_effect=AssertionError("must not run")):
            media = ingest("flac.flac", validate=True, policy=MediaPolicy(require_engine_decode=False)).media
        self.assertEqual(media.probe["engine_decode"], {"status": "skipped"})


class InfrastructureFailureTests(Base):
    """Nothing here says anything about the bytes, so nothing may be recorded
    as invalid, and every case must succeed when simply retried."""

    def fail_tool(self, matches, result):
        real = validation._run

        def fake(args, **kwargs):
            return result if matches(args) else real(args, **kwargs)
        return mock.patch.object(validation, "_run", side_effect=fake)

    def assert_retry_safe(self, name, patcher, expected_code):
        media = self.stored(name)
        with patcher:
            outcome = validation.validate_media(media)
        self.assertEqual((outcome.status, outcome.code), ("infrastructure_error", expected_code))
        self.assertIn(expected_code, validation.INFRASTRUCTURE_CODES)
        row = ProductionMedia.objects.get(pk=media.pk)
        self.assertEqual(row.validation_state, "unvalidated")         # NEVER invalid
        self.assertEqual(row.validation_code, expected_code)          # operator-visible reason
        self.assertIsNone(row.validated_at)
        retried = validation.validate_media(media)                    # simply try again
        self.assertEqual(retried.status, "valid")
        self.assertEqual(ProductionMedia.objects.get(pk=media.pk).validation_state, "valid")

    def test_ffprobe_not_installed(self):
        self.assert_retry_safe("flac.flac", mock.patch.object(validation, "FFPROBE", "no-such-ffprobe-binary"),
                               "probe_unavailable")

    def test_ffprobe_timeout(self):
        self.assert_retry_safe("flac.flac", self.fail_tool(lambda a: "-show_entries" in a, {"status": "timeout"}),
                               "probe_timeout")

    def test_ffprobe_killed_by_a_signal(self):
        self.assert_retry_safe("flac.flac", self.fail_tool(
            lambda a: "-show_entries" in a, {"status": "failed", "returncode": -9, "stdout": "", "stderr": ""}),
            "probe_killed")

    def test_ffprobe_emitting_nonsense(self):
        self.assert_retry_safe("flac.flac", self.fail_tool(
            lambda a: "-show_entries" in a, {"status": "ok", "stdout": "<html>", "stderr": ""}),
            "probe_output_invalid")

    def test_decode_timeout(self):
        self.assert_retry_safe("flac.flac", self.fail_tool(lambda a: "-xerror" in a, {"status": "timeout"}),
                               "decode_timeout")

    def test_decode_killed_by_a_signal(self):
        self.assert_retry_safe("flac.flac", self.fail_tool(
            lambda a: "-xerror" in a, {"status": "failed", "returncode": -9, "stdout": "", "stderr": ""}),
            "decode_killed")

    def test_decode_without_any_progress_report(self):
        self.assert_retry_safe("flac.flac", self.fail_tool(
            lambda a: "-xerror" in a, {"status": "ok", "stdout": "", "stderr": ""}), "decode_output_invalid")

    def test_decoder_not_installed_on_this_host(self):
        self.assert_retry_safe("opus.ogg", mock.patch.object(validation, "_decoder_available", return_value=False),
                               "decoder_unavailable")

    def test_decoder_availability_cannot_be_established(self):
        self.assert_retry_safe("opus.ogg", mock.patch.object(validation, "_decoder_available", return_value=None),
                               "decoder_unavailable")

    def test_engine_probe_timeout(self):
        self.assert_retry_safe("flac.flac", self.fail_tool(
            lambda a: "--timeout" in a, {"status": "timeout"}), "engine_probe_timeout")

    def test_engine_probe_child_crashing_is_a_broken_probe_not_a_bad_file(self):
        self.assert_retry_safe("flac.flac", self.fail_tool(
            lambda a: "--timeout" in a, {"status": "failed", "returncode": 1, "stdout": "", "stderr": "ImportError"}),
            "engine_probe_failed")

    def test_engine_probe_reporting_an_infrastructure_error(self):
        self.assert_retry_safe("flac.flac", self.fail_tool(
            lambda a: "--timeout" in a,
            {"status": "ok", "stdout": json.dumps({"status": "infrastructure_error", "error": "x"}), "stderr": ""}),
            "engine_probe_failed")

    def test_engine_probe_internal_timeout(self):
        self.assert_retry_safe("flac.flac", self.fail_tool(
            lambda a: "--timeout" in a,
            {"status": "ok", "stdout": json.dumps({"status": "timeout"}), "stderr": ""}), "engine_probe_timeout")

    def test_interrupted_validation(self):
        stop = threading.Event()
        stop.set()
        media = self.stored("flac.flac")
        with self.fail_tool(lambda a: True, {"status": "stopped"}):
            outcome = validation.validate_media(media, stop_event=stop)
        self.assertEqual(outcome.code, "validation_interrupted")
        self.assertEqual(ProductionMedia.objects.get(pk=media.pk).validation_state, "unvalidated")

    def test_the_engine_rejecting_a_file_ffmpeg_decoded_is_a_media_verdict(self):
        media = self.stored("flac.flac")
        with self.fail_tool(lambda a: "--timeout" in a,
                            {"status": "ok", "stdout": json.dumps({"status": "media_error", "reason": "stream_undecodable"}), "stderr": ""}):
            outcome = validation.validate_media(media)
        self.assertEqual((outcome.status, outcome.code), ("invalid", "engine_decode_failed"))
        self.assertEqual(ProductionMedia.objects.get(pk=media.pk).validation_code, "engine_decode_failed")

    def test_oversized_ffprobe_output_is_a_hostile_container_not_a_tool_failure(self):
        media = self.stored("flac.flac")
        with self.fail_tool(lambda a: "-show_entries" in a,
                            {"status": "ok", "stdout": "{", "stderr": "", "stdout_truncated": True}):
            outcome = validation.validate_media(media)
        self.assertEqual((outcome.status, outcome.code), ("invalid", "invalid_stream_topology"))

    def test_ingest_time_infrastructure_failure_keeps_the_bytes_and_stays_retryable(self):
        with self.fail_tool(lambda a: "-xerror" in a, {"status": "timeout"}):
            result = ingest("flac.flac", validate=True)
        self.assertTrue(result.outcome.is_infrastructure_error)
        row = ProductionMedia.objects.get(pk=result.media.pk)
        self.assertEqual((row.validation_state, row.validation_code), ("unvalidated", "decode_timeout"))
        self.assertEqual(len(self.list_files("media")), 1)
        self.assertEqual(validation.validate_media(row).status, "valid")


class ValidateMediaSemanticsTests(Base):
    def test_validation_is_idempotent_and_does_not_rerun_tools(self):
        media = self.stored("flac.flac")
        first = validation.validate_media(media)
        with mock.patch.object(validation, "_run", side_effect=AssertionError("must not run")):
            second = validation.validate_media(media)
        self.assertEqual((first.status, first.code), (second.status, second.code))
        self.assertEqual(second.facts["container"], "flac")

    def test_a_lost_race_returns_the_recorded_verdict_without_overwriting_it(self):
        media = self.stored("flac.flac")
        real = validation._analyze_path

        def racing(path, **kwargs):
            outcome = real(path, **kwargs)
            from production import transitions
            transitions.record_verdict(media.pk, dict(
                validation_state="invalid", validation_code="decode_error", validated_at=media.created_at,
            ))                                             # another worker got there first
            return outcome
        with mock.patch.object(validation, "_analyze_path", side_effect=racing):
            outcome = validation.validate_media(media)
        self.assertEqual((outcome.status, outcome.code), ("invalid", "decode_error"))
        self.assertEqual(ProductionMedia.objects.get(pk=media.pk).validation_code, "decode_error")

    def test_a_purge_committed_during_validation_is_a_typed_outcome_never_an_assertion(self):
        """Codex nonblocking finding: validation racing a purge used to hit a
        bare assertion. It is now a deterministic MediaPurged, for a verdict
        and for an infrastructure outcome alike, and nothing is recorded."""
        from production.services import retention
        for analysis in ("verdict", "infrastructure"):
            with self.subTest(analysis=analysis):
                media = self.stored("flac.flac")
                real = validation._analyze_path

                def purge_meanwhile(path, **kwargs):
                    outcome = real(path, **kwargs) if analysis == "verdict" else \
                        validation.ValidationOutcome("infrastructure_error", "decode_timeout", {})
                    with self.captureOnCommitCallbacks(execute=True):
                        retention.purge_media(media)
                    return outcome
                with mock.patch.object(validation, "_analyze_path", side_effect=purge_meanwhile), \
                        self.assertRaises(MediaPurged):
                    validation.validate_media(media)
                row = ProductionMedia.objects.get(pk=media.pk)
                self.assertEqual((row.retention_state, row.validation_state, row.validation_code),
                                 ("purged", "unvalidated", ""))

    def test_a_purged_media_cannot_be_validated(self):
        media = self.stored("flac.flac")
        mark_purged_leaving_bytes(media)
        with self.assertRaises(MediaPurged):
            validation.validate_media(media)

    def test_a_size_mismatch_is_an_inconsistency(self):
        media = self.stored("flac.flac")
        path = layout.resolve_storage_path(media.storage_key)
        path.chmod(0o640)
        path.write_bytes(path.read_bytes() + b"x")
        with self.assertRaises(MediaInconsistent) as caught:
            validation.validate_media(media)
        self.assertEqual(caught.exception.code, "size_mismatch")

    def test_every_code_the_validator_can_emit_is_catalogued(self):
        self.assertFalse(validation.MEDIA_VERDICT_CODES & validation.INFRASTRUCTURE_CODES)
