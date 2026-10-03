"""Streaming intake: caps, hashing, path safety, atomic promotion, crash ordering."""
import hashlib
import os
import stat
import tracemalloc
from unittest import mock

from django.test import TestCase

from production.errors import IntakeError, MediaRejected
from production.models import ProductionMedia
from production.policy import MediaPolicy
from production.services import intake, layout, reconcile

from .support import ChunkedSource, IsolatedMediaRootMixin, fixture


class SimulatedCrash(BaseException):
    """SIGKILL/power loss: escapes every `except Exception` cleanup."""


class FileLike:
    def __init__(self, data):
        self.data, self.position = data, 0

    def read(self, size=-1):
        chunk = self.data[self.position:self.position + (size if size and size > 0 else len(self.data))]
        self.position += len(chunk)
        return chunk


def store(data, **kwargs):
    kwargs.setdefault("kind", "upload")
    kwargs.setdefault("validate", False)
    return intake.ingest_stream(FileLike(data), **kwargs)


class OrdinaryIntakeTests(IsolatedMediaRootMixin, TestCase):
    def test_a_valid_stream_becomes_immutable_media_with_the_right_evidence(self):
        data = fixture("wav16_mono.wav")
        result = store(data, original_filename="take 1.wav", declared_content_type="audio/wav")
        media = result.media
        self.assertEqual(media.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(media.byte_size, len(data))
        self.assertEqual(media.validation_state, "unvalidated")
        self.assertIsNone(result.outcome)
        path = layout.resolve_storage_path(media.storage_key)
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(self.list_files("incoming"), [])
        self.assertEqual(self.list_files("media"), [media.storage_key])

    def test_validation_at_intake_records_the_verdict_and_facts_in_one_insert(self):
        result = store(fixture("flac.flac"), validate=True)
        media = ProductionMedia.objects.get(pk=result.media.pk)
        self.assertTrue(result.outcome.is_valid)
        self.assertEqual((media.validation_state, media.container, media.codec), ("valid", "flac", "flac"))
        self.assertEqual(str(media.decoded_duration_seconds), "4.000000")

    def test_layout_modes_are_restrictive(self):
        media = store(fixture("wav16_mono.wav")).media
        for sub in ("", "media", "incoming", "work", "locks"):
            self.assertEqual(stat.S_IMODE((self.root / sub).stat().st_mode), 0o750, sub)
        permanent = layout.resolve_storage_path(media.storage_key)
        self.assertEqual(stat.S_IMODE(permanent.stat().st_mode), 0o440)
        self.assertEqual(stat.S_IMODE(permanent.parent.stat().st_mode), 0o750)
        with self.assertRaises(PermissionError):
            open(permanent, "r+b")                     # permanent bytes cannot be reopened for writing

    def test_the_part_file_is_private_while_streaming(self):
        seen = {}

        class Peek(FileLike):
            def read(inner, size=-1):
                names = os.listdir(layout.incoming_dir())
                if names and "mode" not in seen:
                    seen["mode"] = stat.S_IMODE(os.stat(layout.incoming_dir() / names[0]).st_mode)
                return super().read(size)
        intake.ingest_stream(Peek(b"x" * 100), kind="upload", validate=False)
        self.assertEqual(seen["mode"], 0o600)

    def test_each_intake_gets_an_independent_identity_even_for_identical_bytes(self):
        data = fixture("wav16_mono.wav")
        first, second = store(data).media, store(data).media
        self.assertNotEqual(first.pk, second.pk)
        self.assertNotEqual(first.storage_key, second.storage_key)
        self.assertEqual(first.sha256, second.sha256)

    def test_storage_key_is_extensionless_and_independent_of_content(self):
        media = store(fixture("mp3.mp3"), original_filename="x.wav").media
        self.assertRegex(media.storage_key, r"^[0-9a-f]{2}/[0-9a-f]{32}$")
        self.assertEqual(media.storage_key, layout.storage_key_for(media.pk))


class PathSafetyTests(IsolatedMediaRootMixin, TestCase):
    HOSTILE_NAMES = [
        "../../../etc/passwd", "..\\..\\windows\\system32\\x.wav", "/etc/cron.d/evil", "a/b/c.wav",
        "foo\x00.wav", "con\nfig.wav", "....//....//x", "%2e%2e%2fx.wav", "~root/x", "x" * 400 + ".wav",
        "", None,
    ]

    def test_no_client_supplied_text_ever_reaches_a_path(self):
        for index, name in enumerate(self.HOSTILE_NAMES):
            with self.subTest(name=name):
                media = store(b"payload-%d" % index, original_filename=name, declared_content_type="../../x").media
                self.assertRegex(media.storage_key, r"^[0-9a-f]{2}/[0-9a-f]{32}$")
                self.assertNotIn("\x00", media.original_filename)
                self.assertNotIn("/", media.original_filename)
                self.assertLessEqual(len(media.original_filename), 255)
        # Everything written lives under media/<shard>/<hex>; nothing escaped the root.
        parent = self.root.parent
        for current, _dirs, files in os.walk(parent):
            for name in files:
                relative = os.path.relpath(os.path.join(current, name), self.root)
                self.assertTrue(relative.startswith("media" + os.sep), relative)
        self.assertEqual(len(self.list_files("media")), len(self.HOSTILE_NAMES))
        self.assertFalse(os.path.exists("/etc/cron.d/evil"))

    def test_the_filename_never_appears_in_any_stored_path(self):
        media = store(b"hello", original_filename="secret-marker-name.wav").media
        path = str(layout.resolve_storage_path(media.storage_key))
        self.assertNotIn("secret-marker", path)
        self.assertEqual(media.original_filename, "secret-marker-name.wav")

    def test_resolve_storage_path_refuses_anything_but_a_generated_key(self):
        for bad in ("../x", "/etc/passwd", "ab/../../etc/passwd", "ab/" + "z" * 32, "", None, 5, "ab/" + "a" * 32 + "/x"):
            with self.subTest(key=bad), self.assertRaises(IntakeError):
                layout.resolve_storage_path(bad)

    def test_layout_refuses_a_symlinked_subdirectory(self):
        layout.ensure_layout()
        os.rmdir(self.root / "media")
        os.symlink("/tmp", self.root / "media")
        with self.assertRaises(IntakeError):
            layout.ensure_layout()

    def test_a_relative_or_traversing_root_is_refused(self):
        from django.core.exceptions import ImproperlyConfigured
        from django.test import override_settings
        for bad in ("relative/path", "/srv/../etc", ""):
            with self.subTest(root=bad), override_settings(PRODUCTION_MEDIA_ROOT=bad), \
                    self.assertRaises(ImproperlyConfigured):
                layout.media_root()


class MisleadingMetadataTests(IsolatedMediaRootMixin, TestCase):
    def test_extension_and_mime_never_decide_what_the_bytes_are(self):
        cases = [
            ("mp3.mp3", "recording.wav", "audio/wav", "mp3"),
            ("wav16_mono.wav", "recording.mp3", "audio/mpeg", "wav"),
            ("flac.flac", "recording.m4a", "video/mp4", "flac"),
            ("opus_live.webm", "recording.ogg", "application/octet-stream", "matroska"),
        ]
        for name, filename, declared, container in cases:
            with self.subTest(fixture=name, claimed=filename):
                result = store(fixture(name), original_filename=filename, declared_content_type=declared, validate=True)
                self.assertEqual(result.media.container, container)
                self.assertEqual(result.media.declared_content_type, declared)     # evidence, not truth

    def test_audio_claims_do_not_rescue_bytes_that_are_not_audio(self):
        for name in ("text.wav", "garbage.bin"):
            with self.subTest(fixture=name), self.assertRaises(MediaRejected) as caught:
                store(fixture(name), original_filename="x.mp3", declared_content_type="audio/mpeg", validate=True)
            self.assertEqual(caught.exception.code, "unsupported_container")
        self.assertEqual(ProductionMedia.objects.count(), 0)
        self.assertEqual(self.list_files("media") + self.list_files("incoming"), [])


class BoundedStreamingTests(IsolatedMediaRootMixin, TestCase):
    def test_the_byte_cap_stops_the_stream_early_and_leaves_nothing(self):
        source = ChunkedSource(3 * intake.CHUNK_SIZE)
        with self.assertRaises(MediaRejected) as caught:
            intake.ingest_stream(source, kind="upload", validate=False,
                                 policy=MediaPolicy(max_bytes=intake.CHUNK_SIZE + 1))
        self.assertEqual(caught.exception.code, "too_large")
        self.assertEqual(len(source.requests), 2)               # cut off: the third chunk was never read
        self.assertLess(source.sent, source.total)
        self.assertEqual(self.list_files("incoming"), [])
        self.assertEqual(ProductionMedia.objects.count(), 0)

    def test_exactly_the_cap_is_accepted(self):
        data = b"z" * 1000
        self.assertEqual(store(data, policy=MediaPolicy(max_bytes=1000)).media.byte_size, 1000)
        with self.assertRaises(MediaRejected):
            store(data + b"z", policy=MediaPolicy(max_bytes=1000))

    def test_the_platform_ceiling_cannot_be_exceeded_by_policy(self):
        with self.assertRaises(ValueError):
            MediaPolicy(max_bytes=10 ** 12)

    def test_empty_input_is_refused(self):
        with self.assertRaises(MediaRejected) as caught:
            store(b"")
        self.assertEqual(caught.exception.code, "empty")
        self.assertEqual(self.list_files("incoming"), [])

    def test_an_interrupted_upload_leaves_no_row_and_no_part(self):
        source = ChunkedSource(5 * intake.CHUNK_SIZE, fail_after=2 * intake.CHUNK_SIZE)
        with self.assertRaises(IntakeError) as caught:
            intake.ingest_stream(source, kind="upload", validate=False)
        self.assertEqual(caught.exception.code, "source_read_failed")
        self.assertEqual(self.list_files("incoming"), [])
        self.assertEqual(ProductionMedia.objects.count(), 0)

    def test_a_source_that_is_not_file_like_is_refused(self):
        for bad in (b"bytes", "text", object(), None):
            with self.subTest(source=bad), self.assertRaises(IntakeError):
                intake.ingest_stream(bad, kind="upload", validate=False)

    def test_a_source_that_returns_text_is_refused(self):
        class Texty:
            def read(self, size=-1):
                return "not bytes"
        with self.assertRaises(IntakeError):
            intake.ingest_stream(Texty(), kind="upload", validate=False)
        self.assertEqual(self.list_files("incoming"), [])

    def test_data_is_streamed_in_bounded_chunks_never_buffered_whole(self):
        total = 24 * 1024 * 1024
        source = ChunkedSource(total, fill=b"\x07")
        tracemalloc.start()
        try:
            intake.ingest_stream(source, kind="upload", validate=False)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(set(source.requests[:-1]), {intake.CHUNK_SIZE})
        self.assertGreaterEqual(len(source.requests), 24)
        self.assertLess(peak, 8 * 1024 * 1024)                    # input was 24 MiB
        self.assertEqual(ProductionMedia.objects.get().byte_size, total)


class CrashOrderingTests(IsolatedMediaRootMixin, TestCase):
    """A hard kill runs no cleanup: SimulatedCrash is a BaseException."""

    def test_crash_before_promotion_leaves_a_part_and_no_row_and_the_sweeper_reclaims_it(self):
        with mock.patch("production.services.intake.os.link", side_effect=SimulatedCrash), \
                self.assertRaises(SimulatedCrash):
            store(fixture("wav16_mono.wav"))
        self.assertEqual(ProductionMedia.objects.count(), 0)
        self.assertEqual(self.list_files("media"), [])
        [part] = self.list_files("incoming")
        self.assertRegex(part, r"^[0-9a-f]{32}\.part$")

        young = reconcile.sweep_stale_parts(apply=True)
        self.assertEqual((young.removed, young.kept_young), ([], 1))   # an upload in flight is left alone
        from datetime import timedelta
        from django.utils import timezone
        old = reconcile.sweep_stale_parts(apply=True, now=timezone.now() + timedelta(hours=25))
        self.assertEqual(old.removed, [f"incoming/{part}"])
        self.assertEqual(self.list_files("incoming"), [])

    def test_crash_after_promotion_before_the_row_leaves_an_orphan_no_row_points_at_missing_data(self):
        with mock.patch.object(ProductionMedia.objects, "create", side_effect=SimulatedCrash), \
                self.assertRaises(SimulatedCrash):
            store(fixture("wav16_mono.wav"))
        self.assertEqual(ProductionMedia.objects.count(), 0)          # no row ever points at the file
        [orphan] = self.list_files("media")
        self.assertEqual(self.list_files("incoming"), [])             # the part was already promoted away

        from datetime import timedelta
        from django.utils import timezone
        self.assertEqual(reconcile.sweep_orphan_media(apply=True).removed, [])        # too young: kept
        self.assertEqual(self.list_files("media"), [orphan])
        report = reconcile.sweep_orphan_media(apply=True, now=timezone.now() + timedelta(hours=25))
        self.assertEqual(report.removed, [f"media/{orphan}"])
        self.assertEqual(self.list_files("media"), [])

    def test_an_ordinary_failure_after_promotion_cleans_up_after_itself(self):
        with mock.patch.object(ProductionMedia.objects, "create", side_effect=RuntimeError("db went away")), \
                self.assertRaises(RuntimeError):
            store(fixture("wav16_mono.wav"))
        self.assertEqual(self.list_files("media") + self.list_files("incoming"), [])

    def test_a_failure_while_finishing_promotion_removes_the_new_permanent_file(self):
        real_unlink = os.unlink

        def fail_only_for_the_part(path, *args, **kwargs):
            if str(path).endswith(".part"):
                raise OSError("boom")
            return real_unlink(path, *args, **kwargs)
        with mock.patch("production.services.intake.os.unlink", side_effect=fail_only_for_the_part), \
                self.assertRaises(IntakeError):
            store(fixture("wav16_mono.wav"))
        self.assertEqual(ProductionMedia.objects.count(), 0)
        self.assertEqual(self.list_files("media"), [])               # not left behind as an orphan
        self.assertEqual(len(self.list_files("incoming")), 1)        # the stale part is the sweeper's

    def test_promotion_can_never_overwrite_an_existing_permanent_file(self):
        first = store(b"first-bytes").media
        original = layout.resolve_storage_path(first.storage_key).read_bytes()
        with mock.patch("production.services.layout.new_media_id", return_value=first.pk), \
                self.assertRaises(IntakeError) as caught:
            store(b"second-bytes")
        self.assertEqual(caught.exception.code, "storage_collision")
        self.assertEqual(layout.resolve_storage_path(first.storage_key).read_bytes(), original)
        self.assertEqual(ProductionMedia.objects.count(), 1)
        self.assertEqual(self.list_files("incoming"), [])

    def test_a_committed_row_always_resolves_to_complete_bytes(self):
        data = fixture("flac.flac")
        media = store(data, validate=True).media
        self.assertEqual(layout.resolve_storage_path(media.storage_key).read_bytes(), data)
        self.assertEqual(reconcile.find_inconsistent_media(deep=True), [])
