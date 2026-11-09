"""r0131: ``import_songs`` registers only genuinely decodable audio and never macOS archive metadata.

Real media throughout (ffmpeg writes the tones; malformed files are real bytes on disk); the race cases wrap the
real decoder.  Every rejection must leave the database exactly as it was: no Track, no Artist/Album/Genre, and an
existing Track's row, relationships and readiness state untouched."""
import os
import shutil
import subprocess
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from library.models import Album, Artist, Category, CategoryKind, Genre, Track
from library.services import audio_identity
from library.services.related_artists import resolve_fallback_metadata

COMMAND = "library.management.commands.import_songs"
APPLE_DOUBLE = b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        " + b"\x00" * 4000


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args], check=True,
                   stdin=subprocess.DEVNULL, timeout=60)


class _ImportFixture(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media = tempfile.TemporaryDirectory()
        media = Path(cls._media.name)
        tone = ["-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "libmp3lame", "-b:a", "64k"]
        _ffmpeg(*tone, "-map_metadata", "-1", "-id3v2_version", "0", "-write_xing", "0", str(media / "tagless.mp3"))
        _ffmpeg(*tone, "-metadata", "title=Real Title", "-metadata", "artist=Real Artist",
                "-metadata", "album=Real Album", "-metadata", "genre=Real Genre", str(media / "tagged.mp3"))
        tagged = (media / "tagged.mp3").read_bytes()
        id3_end = 10 + ((tagged[6] & 0x7F) << 21 | (tagged[7] & 0x7F) << 14 | (tagged[8] & 0x7F) << 7
                        | (tagged[9] & 0x7F))
        cls.BYTES = {
            "tagless": (media / "tagless.mp3").read_bytes(),
            "tagged": tagged,
            "random": bytes((i * 7919 + 13) % 251 for i in range(8192)),        # no MPEG frames at all
            "id3_garbage": tagged[:id3_end] + bytes((i * 31) % 256 for i in range(8192)),   # real tag, no audio
            "id3_zeros": tagged[:id3_end] + b"\x00" * 4096,
            "apple_double": APPLE_DOUBLE,
        }

    @classmethod
    def tearDownClass(cls):
        cls._media.cleanup()
        super().tearDownClass()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        kind = CategoryKind.objects.create(code="import-hardening", name="Import hardening")
        self.rock = Category.objects.create(code="Rock", name="Rock", kind=kind)
        self.news = Category.objects.create(code="LocalNews", name="Local News", kind=kind)

    def put(self, relative, kind):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.BYTES[kind])
        return path

    def run_import(self, *args):
        out, err = StringIO(), StringIO()
        call_command("import_songs", str(self.root), *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def counts(self):
        return {model.__name__: model.objects.count() for model in (Track, Artist, Album, Genre, Category)}

    def snapshot(self, track):
        row = Track.objects.filter(pk=track.pk).values().get()
        return row, sorted(track.additional_categories.values_list("pk", flat=True))

    def imported_paths(self):
        return sorted(Path(p).relative_to(self.root).as_posix() for p in Track.objects.values_list("filepath", flat=True))


class InvalidAudioTests(_ImportFixture):
    def test_1_malformed_mp3_is_rejected_without_any_database_write(self):
        for kind in ("random", "id3_garbage", "id3_zeros"):
            self.put(f"Rock/{kind}.mp3", kind)
        before = self.counts()
        out, err = self.run_import()
        self.assertEqual(self.counts(), before)                    # no Track, Artist, Album, Genre or Category
        for kind in ("random", "id3_garbage", "id3_zeros"):
            self.assertIn(f"Skipping {self.root / 'Rock' / kind}.mp3: not decodable audio", err)
        self.assertIn("inserted: 0, updated: 0, skipped: 3", out)

    def test_3_valid_tagless_audio_is_accepted_with_the_normal_fallback_metadata(self):
        self.put("Rock/Some_Band - Some_Song.mp3", "tagless")
        self.run_import()
        track = Track.objects.get()
        title, artist = resolve_fallback_metadata("Some_Band - Some_Song", None, None)
        self.assertEqual((track.title, track.artist.name, track.category, track.format),
                         (title, artist, self.rock, "mp3"))
        self.assertAlmostEqual(track.duration_seconds, 1.0, delta=0.2)
        self.assertEqual((track.sample_rate, track.channels), (44100, 1))

    def test_valid_tagged_audio_keeps_its_tags(self):
        self.put("Rock/tagged.mp3", "tagged")
        self.run_import()
        track = Track.objects.get()
        self.assertEqual((track.title, track.artist.name, track.album.title, track.genre.name),
                         ("Real Title", "Real Artist", "Real Album", "Real Genre"))


class ArchiveArtifactTests(_ImportFixture):
    def test_2_apple_double_files_and_macosx_trees_are_never_imported(self):
        self.put("Rock/song.mp3", "tagged")
        self.put("Rock/._song.mp3", "apple_double")
        self.put("Rock/._valid_looking.mp3", "tagless")            # even real audio under a "._" name
        self.put("__MACOSX/Rock/._song.mp3", "apple_double")
        self.put("__MACOSX/Rock/song.mp3", "tagged")               # the whole __MACOSX tree is packaging
        self.put("Rock/.AppleDouble/song.mp3", "tagged")
        self.put("Rock/Live._Sessions/a._b.mp3", "tagless")        # ordinary names that merely contain "._"
        self.put("Rock/Albums/deep.mp3", "tagless")                # legitimate organization is preserved
        before = self.counts()
        out, err = self.run_import()
        self.assertEqual(self.imported_paths(), ["Rock/Albums/deep.mp3", "Rock/Live._Sessions/a._b.mp3", "Rock/song.mp3"])
        self.assertEqual(self.counts()["Track"], before["Track"] + 3)
        self.assertTrue(all(t.category == self.rock for t in Track.objects.all()))
        for artifact in ("Rock/._song.mp3", "Rock/._valid_looking.mp3", "__MACOSX/Rock/._song.mp3",
                         "__MACOSX/Rock/song.mp3", "Rock/.AppleDouble/song.mp3"):
            self.assertIn(f"Skipping {self.root / artifact}: macOS archive metadata", err)
        self.assertIn("inserted: 3, updated: 0, skipped: 5", out)


class DryRunTests(_ImportFixture):
    def test_4_dry_run_reports_the_same_eligibility_as_the_import_and_writes_nothing(self):
        self.put("Rock/good.mp3", "tagged")
        self.put("Rock/tagless.mp3", "tagless")
        self.put("Rock/bad.mp3", "random")
        self.put("Rock/._good.mp3", "apple_double")
        self.put("__MACOSX/Rock/good.mp3", "tagged")
        self.put("notes.txt", "random")
        before = self.counts()
        out, err = self.run_import("--dry-run")
        self.assertEqual(self.counts(), before)
        would = sorted(Path(line.split("] ", 1)[1]).relative_to(self.root).as_posix()
                       for line in out.splitlines() if line.strip().startswith("[inserted]"))
        self.assertEqual(would, ["Rock/good.mp3", "Rock/tagless.mp3"])
        self.assertIn("inserted: 2, updated: 0, skipped: 4", out)
        for skipped in ("Rock/bad.mp3", "Rock/._good.mp3", "__MACOSX/Rock/good.mp3"):
            self.assertIn(f"Skipping {self.root / skipped}:", err)
        real_out, _ = self.run_import()
        self.assertEqual(self.imported_paths(), would)
        self.assertIn("inserted: 2, updated: 0, skipped: 4", real_out)


class ExistingTrackPreservationTests(_ImportFixture):
    def setUp(self):
        super().setUp()
        self.path = self.put("Rock/song.mp3", "tagged")
        self.run_import()
        self.track = Track.objects.get()
        # Readiness and editorial state established since (cue markers are guarded and change only through their
        # service; the whole-row snapshot below covers them too).
        Track.objects.filter(pk=self.track.pk).update(
            waveform_path="/waveforms/x.json", file_hash="abc", analysis_requested_at=self.track.created_at,
            rotation_weight=3, play_count=4, isrc="USABC1234567", title="Operator Title")
        self.track.additional_categories.add(self.news)
        self.before = self.snapshot(self.track)
        self.counts_before = self.counts()

    def assert_untouched(self):
        self.assertEqual(self.snapshot(self.track), self.before)
        self.assertEqual(self.counts(), self.counts_before)

    def test_5_an_invalid_replacement_leaves_the_existing_track_exactly_as_it_was(self):
        for kind in ("random", "id3_garbage", "apple_double"):
            with self.subTest(replacement=kind):
                self.path.write_bytes(self.BYTES[kind])
                out, err = self.run_import()
                self.assertIn("not decodable audio", err)
                self.assertIn("the existing Track is left unchanged", err)
                self.assertIn("inserted: 0, updated: 0, skipped: 1", out)
                self.assert_untouched()
                out, _ = self.run_import("--dry-run")
                self.assertIn("inserted: 0, updated: 0, skipped: 1", out)
                self.assert_untouched()
        # A valid file again: the ordinary update path still works.
        self.path.write_bytes(self.BYTES["tagged"])
        out, _ = self.run_import()
        self.assertIn("inserted: 0, updated: 1, skipped: 0", out)

    def test_6_a_file_that_disappears_or_is_replaced_during_validation_is_skipped(self):
        real = audio_identity.compute_fd_or_none

        def scenario(mutate):
            def decode(fd, **kwargs):
                result = real(fd, **kwargs)
                mutate()
                return result
            return decode

        def replace_with_other_audio():
            other = self.path.with_name("other.tmp")
            other.write_bytes(self.BYTES["tagless"])
            os.replace(other, self.path)

        def rewrite_in_place():
            with open(self.path, "r+b") as handle:
                handle.seek(0, os.SEEK_END)
                handle.write(b"\x00" * 128)

        cases = {"disappears": (self.path.unlink, "disappeared during validation"),
                 "is replaced": (replace_with_other_audio, "changed during validation"),
                 "is rewritten": (rewrite_in_place, "changed during validation")}
        for name, (mutate, message) in cases.items():
            with self.subTest(file=name):
                self.path.write_bytes(self.BYTES["tagged"])
                with patch(f"{COMMAND}.audio_identity.compute_fd_or_none", side_effect=scenario(mutate)):
                    out, err = self.run_import()
                self.assertIn(message, err)
                self.assertIn("inserted: 0, updated: 0, skipped: 1", out)
                self.assert_untouched()

    def test_6b_a_file_gone_before_it_is_opened_is_skipped(self):
        real_open = os.open

        def vanish_first(path, *args, **kwargs):
            if str(path) == str(self.path) and self.path.exists():
                self.path.unlink()
            return real_open(path, *args, **kwargs)

        with patch(f"{COMMAND}.os.open", side_effect=vanish_first):
            out, err = self.run_import()
        self.assertIn("cannot be opened", err)
        self.assertIn("inserted: 0, updated: 0, skipped: 1", out)
        self.assert_untouched()


class CategoryBehaviourTests(_ImportFixture):
    def test_7_category_override_and_folder_inference_are_unchanged(self):
        self.put("Rock/a.mp3", "tagless")
        self.put("SomeOtherFolder/b.mp3", "tagless")
        self.put("root-level.mp3", "tagless")
        self.run_import("--category", "LocalNews")
        self.assertEqual({Path(t.filepath).name: t.category for t in Track.objects.all()},
                         {"a.mp3": self.news, "b.mp3": self.news, "root-level.mp3": self.news})
        Track.objects.all().delete()
        out, err = self.run_import()
        self.assertEqual({Path(t.filepath).name: t.category for t in Track.objects.all()},
                         {"a.mp3": self.rock, "root-level.mp3": None})
        self.assertIn("no Category exists for folder 'SomeOtherFolder'", err)

    def test_the_decoder_runs_once_per_eligible_file_and_never_for_ineligible_ones(self):
        self.put("Rock/a.mp3", "tagless")
        self.put("Rock/._a.mp3", "apple_double")
        self.put("__MACOSX/Rock/a.mp3", "tagless")
        self.put("Rock/cover.jpg", "random")
        with patch(f"{COMMAND}.audio_identity.compute_fd_or_none",
                   side_effect=audio_identity.compute_fd_or_none) as decode:
            self.run_import()
        self.assertEqual(decode.call_count, 1)
