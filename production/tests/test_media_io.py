"""open_media / verify_integrity / range primitives: identity in, safe handle out."""
import os
import pathlib

from django.test import TestCase

from production.errors import MediaInconsistent, MediaNotValidated, MediaPurged, RangeNotSatisfiable
from production.models import ProductionMedia
from production.services import intake, layout, media_io, validation

from .support import IsolatedMediaRootMixin, fixture


class FileLike:
    def __init__(self, data):
        self.data, self.position = data, 0

    def read(self, size=-1):
        chunk = self.data[self.position:self.position + (size if size and size > 0 else len(self.data))]
        self.position += len(chunk)
        return chunk


def valid_media(name="wav16_mono.wav", **kwargs):
    return intake.ingest_stream(FileLike(fixture(name)), kind="upload", validate=True, **kwargs).media


class OpenMediaTests(IsolatedMediaRootMixin, TestCase):
    def test_a_valid_media_opens_by_identity_with_the_validated_content_type(self):
        media = valid_media("mp3.mp3", declared_content_type="audio/wav", original_filename="x.wav")
        with media_io.open_media(media.pk) as opened:
            self.assertEqual(opened.file.read(), fixture("mp3.mp3"))
            self.assertEqual(opened.size, media.byte_size)
            self.assertEqual(opened.content_type, "audio/mpeg")       # from the bytes, not the declared type
            self.assertEqual(opened.media.pk, media.pk)
        self.assertTrue(opened.file.closed)

    def test_an_instance_is_accepted_but_only_its_identity_is_used(self):
        media = valid_media()
        media.storage_key = "ab/" + "0" * 32            # a lying in-memory object changes nothing
        with media_io.open_media(media) as opened:
            self.assertEqual(opened.file.read(), fixture("wav16_mono.wav"))

    def test_the_handle_is_read_only_and_seekable(self):
        with media_io.open_media(valid_media()) as opened:
            self.assertFalse(opened.file.writable())
            opened.file.seek(10)
            self.assertEqual(opened.file.tell(), 10)

    def test_unvalidated_and_invalid_media_do_not_open_unless_asked(self):
        stored = intake.ingest_stream(FileLike(fixture("flac.flac")), kind="upload", validate=False).media
        with self.assertRaises(MediaNotValidated):
            media_io.open_media(stored)
        with media_io.open_media(stored, require_valid=False) as opened:
            self.assertEqual(opened.content_type, "application/octet-stream")
        invalid = intake.ingest_stream(FileLike(fixture("truncated_flac.flac")), kind="upload",
                                       validate=True, retain_invalid=True).media
        with self.assertRaises(MediaNotValidated):
            media_io.open_media(invalid)

    def test_purged_media_fails_safely_even_if_bytes_linger(self):
        media = valid_media()
        ProductionMedia.objects.filter(pk=media.pk).update(retention_state="purged", purged_at=media.created_at)
        self.assertTrue(layout.resolve_storage_path(media.storage_key).exists())
        with self.assertRaises(MediaPurged):
            media_io.open_media(media)

    def test_missing_bytes_are_an_explicit_inconsistency_never_a_path_fallback(self):
        media = valid_media()
        layout.resolve_storage_path(media.storage_key).unlink()
        with self.assertRaises(MediaInconsistent) as caught:
            media_io.open_media(media)
        self.assertEqual(caught.exception.code, "missing_bytes")

    def test_a_size_that_disagrees_with_the_row_is_an_inconsistency(self):
        media = valid_media()
        path = layout.resolve_storage_path(media.storage_key)
        path.chmod(0o640)
        path.write_bytes(path.read_bytes()[:-1])
        with self.assertRaises(MediaInconsistent) as caught:
            media_io.open_media(media)
        self.assertEqual(caught.exception.code, "size_mismatch")

    def test_a_symlink_where_the_bytes_should_be_is_never_followed(self):
        media = valid_media()
        other = valid_media("flac.flac")
        path = layout.resolve_storage_path(media.storage_key)
        # Pointing OUTSIDE the store: refused by the containment check.
        secret = pathlib.Path(self.root.parent) / "secret.bin"
        secret.write_bytes(b"x" * media.byte_size)
        path.unlink()
        os.symlink(secret, path)
        with self.assertRaises(MediaInconsistent) as caught:
            media_io.open_media(media)
        self.assertEqual(caught.exception.code, "path_escape")
        # Pointing at ANOTHER media inside the store: containment passes, O_NOFOLLOW refuses.
        path.unlink()
        os.symlink(layout.resolve_storage_path(other.storage_key), path)
        with self.assertRaises(MediaInconsistent) as caught:
            media_io.open_media(media)
        self.assertEqual(caught.exception.code, "not_a_regular_file")

    def test_a_directory_where_the_bytes_should_be_is_refused(self):
        media = valid_media()
        path = layout.resolve_storage_path(media.storage_key)
        path.unlink()
        path.mkdir()
        with self.assertRaises(MediaInconsistent):
            media_io.open_media(media)

    def test_a_shard_directory_swapped_for_a_symlink_cannot_escape_the_store(self):
        media = valid_media()
        shard = layout.resolve_storage_path(media.storage_key).parent
        elsewhere = pathlib.Path(self.root.parent) / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / media.storage_key.split("/")[1]).write_bytes(fixture("wav16_mono.wav"))
        for child in shard.iterdir():
            child.unlink()
        shard.rmdir()
        os.symlink(elsewhere, shard)
        with self.assertRaises(MediaInconsistent) as caught:
            media_io.open_media(media)
        self.assertEqual(caught.exception.code, "path_escape")

    def test_no_arbitrary_path_or_garbage_identifier_is_ever_accepted(self):
        for bad in ("/etc/passwd", "../../etc/passwd", pathlib.Path("/etc/passwd"), "", None, 12345,
                    "not-a-uuid", "00000000-0000-0000-0000-000000000000"):
            with self.subTest(identifier=bad), self.assertRaises(MediaInconsistent) as caught:
                media_io.open_media(bad)
            self.assertEqual(caught.exception.code, "unknown_media")

    def test_a_corrupted_stored_key_can_not_traverse(self):
        media = valid_media()
        ProductionMedia.objects.filter(pk=media.pk).update()          # (no-op; storage_key is frozen)
        row = ProductionMedia.objects.get(pk=media.pk)
        row.__dict__["storage_key"] = "../../../etc/passwd"           # simulate an impossible DB value
        from production.errors import IntakeError
        with self.assertRaises(IntakeError):
            layout.resolve_storage_path(row.storage_key)


class IntegrityTests(IsolatedMediaRootMixin, TestCase):
    def test_verify_integrity_accepts_untouched_bytes(self):
        media_io.verify_integrity(valid_media())

    def test_verify_integrity_detects_a_flipped_byte_that_keeps_the_size(self):
        media = valid_media()
        path = layout.resolve_storage_path(media.storage_key)
        data = bytearray(path.read_bytes())
        data[100] ^= 0xFF
        path.chmod(0o640)
        path.write_bytes(bytes(data))
        with self.assertRaises(MediaInconsistent) as caught:
            media_io.verify_integrity(media)
        self.assertEqual(caught.exception.code, "sha_mismatch")
        media_io.open_media(media).close()               # open's cheap check is size only: documented

    def test_verify_integrity_of_purged_media(self):
        media = valid_media()
        ProductionMedia.objects.filter(pk=media.pk).update(retention_state="purged", purged_at=media.created_at)
        with self.assertRaises(MediaPurged):
            media_io.verify_integrity(media)


class RangeTests(IsolatedMediaRootMixin, TestCase):
    def test_parse_byte_range(self):
        size = 100
        cases = {
            None: None, "": None, "garbage": None, "bytes=": None, "bytes=-": None,
            "bytes=0-9": (0, 9), "bytes=10-": (10, 99), "bytes=-5": (95, 99), "bytes=50-1000": (50, 99),
            "bytes=0-0": (0, 0), "bytes=99-99": (99, 99), "bytes=-1000": (0, 99),
            "bytes=0-1,5-6": None,           # multi-range: serve whole file
            "bytes=9-3": None,               # invalid spec is ignored (RFC 9110)
            "items=0-5": None,
        }
        for header, expected in cases.items():
            with self.subTest(header=header):
                self.assertEqual(media_io.parse_byte_range(header, size), expected)

    def test_unsatisfiable_ranges_raise(self):
        for header in ("bytes=100-", "bytes=500-600", "bytes=-0"):
            with self.subTest(header=header), self.assertRaises(RangeNotSatisfiable):
                media_io.parse_byte_range(header, 100)

    def test_iter_range_yields_exactly_the_requested_bytes_in_bounded_chunks(self):
        media = valid_media()
        data = fixture("wav16_mono.wav")
        with media_io.open_media(media) as opened:
            chunks = list(media_io.iter_range(opened.file, 1000, 5999, chunk_size=1024))
        self.assertEqual(b"".join(chunks), data[1000:6000])
        self.assertTrue(all(len(chunk) <= 1024 for chunk in chunks))

    def test_iter_range_stops_cleanly_at_end_of_file(self):
        media = valid_media()
        with media_io.open_media(media) as opened:
            tail = b"".join(media_io.iter_range(opened.file, media.byte_size - 10, media.byte_size + 500))
        self.assertEqual(len(tail), 10)
