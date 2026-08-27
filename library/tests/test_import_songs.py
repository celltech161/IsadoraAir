import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from library.models import Category, CategoryKind, Track


class ImportSongsCategoryOverrideTests(TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        kind = CategoryKind.objects.create(code="import-test", name="Import Test")
        self.rock = Category.objects.create(code="Rock", name="Rock", kind=kind)
        self.local_news = Category.objects.create(
            code="LocalNews", name="Local News", kind=kind
        )
        self.other = Category.objects.create(
            code="SomeOtherFolder", name="Some Other Folder", kind=kind
        )

    def _write_file(self, relative_path, contents=b"not really an mp3"):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        return path

    def _parsed(self, *, title="Bulletin", duration=179.8,
                sample_rate=44100, channels=2, bit_depth=16):
        return (
            {
                "title": title,
                "artist": "Test Artist",
                "album": "",
                "album_artist": "",
                "genre": "",
                "year": None,
                "track_number": None,
                "disc_number": None,
                "isrc": "",
            },
            {
                "duration_seconds": duration,
                "sample_rate": sample_rate,
                "channels": channels,
                "bit_depth": bit_depth,
            },
        )

    def _import(self, *args, parsed=None):
        with patch(
            "library.management.commands.import_songs.parse_tags",
            return_value=parsed or self._parsed(),
        ):
            call_command("import_songs", *args, stdout=StringIO(), stderr=StringIO())

    def test_without_override_preserves_folder_category_inference(self):
        path = self._write_file("Rock/song.mp3")

        self._import(str(self.root))

        track = Track.objects.get(filepath=str(path))
        self.assertEqual(track.category_id, self.rock.id)

    def test_override_assigns_direct_child_to_requested_category(self):
        path = self._write_file("fsn_bulletin.mp3")

        self._import(str(self.root), "--category", "LocalNews")

        track = Track.objects.get(filepath=str(path))
        self.assertEqual(track.category_id, self.local_news.id)

    def test_override_wins_over_folder_inference(self):
        path = self._write_file("SomeOtherFolder/song.mp3")

        self._import(str(self.root), "--category", "LocalNews")

        track = Track.objects.get(filepath=str(path))
        self.assertEqual(track.category_id, self.local_news.id)
        self.assertNotEqual(track.category_id, self.other.id)

    def test_reimport_refreshes_existing_track_metadata(self):
        path = self._write_file("fsn_bulletin.mp3", b"old bulletin")
        self._import(
            str(self.root), "--category", "LocalNews",
            parsed=self._parsed(
                title="Old Bulletin", duration=302.3, sample_rate=44100,
                channels=2, bit_depth=16,
            ),
        )
        original = Track.objects.get(filepath=str(path))
        original_id = original.id

        path.write_bytes(b"fresh replacement bulletin")
        self._import(
            str(self.root), "--category", "LocalNews",
            parsed=self._parsed(
                title="Fresh Bulletin", duration=179.8, sample_rate=48000,
                channels=1, bit_depth=24,
            ),
        )

        refreshed = Track.objects.get(filepath=str(path))
        self.assertEqual(refreshed.id, original_id)
        self.assertEqual(Track.objects.filter(filepath=str(path)).count(), 1)
        self.assertEqual(refreshed.category_id, self.local_news.id)
        self.assertEqual(refreshed.title, "Fresh Bulletin")
        self.assertEqual(refreshed.duration_seconds, 179.8)
        self.assertEqual(refreshed.sample_rate, 48000)
        self.assertEqual(refreshed.channels, 1)
        self.assertEqual(refreshed.bit_depth, 24)

    def test_invalid_override_fails_before_mutating_tracks(self):
        existing_path = self._write_file("fsn_bulletin.mp3", b"old bulletin")
        self._import(
            str(self.root), "--category", "LocalNews",
            parsed=self._parsed(title="Old Bulletin", duration=302.3),
        )
        pending_path = self._write_file("another.mp3")
        category_count = Category.objects.count()

        with patch(
            "library.management.commands.import_songs.parse_tags",
            return_value=self._parsed(title="Should Not Import", duration=1.0),
        ) as parse_mock:
            with self.assertRaisesMessage(CommandError, "DoesNotExist"):
                call_command(
                    "import_songs", str(self.root),
                    "--category", "DoesNotExist",
                    stdout=StringIO(), stderr=StringIO(),
                )

        parse_mock.assert_not_called()
        self.assertEqual(Category.objects.count(), category_count)
        self.assertFalse(Category.objects.filter(code="DoesNotExist").exists())
        self.assertFalse(Track.objects.filter(filepath=str(pending_path)).exists())
        existing = Track.objects.get(filepath=str(existing_path))
        self.assertEqual(existing.title, "Old Bulletin")
        self.assertEqual(existing.duration_seconds, 302.3)
        self.assertEqual(existing.category_id, self.local_news.id)

    def test_dry_run_with_override_does_not_write(self):
        self._write_file("fsn_bulletin.mp3")

        self._import(
            str(self.root), "--category", "LocalNews", "--dry-run"
        )

        self.assertEqual(Track.objects.count(), 0)
