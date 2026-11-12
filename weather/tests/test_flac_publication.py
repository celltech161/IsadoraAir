"""Weather FLAC publication (r0134): a FLAC is an ordinary new library asset published by the same last-known-good
publisher as before. There is deliberately NO cross-format Track adoption -- the station operator retires the legacy
MP3 Tracks by hand -- so these tests prove the opposite: a FLAC publication creates or updates the FLAC's own Track
and never touches an MP3 Track or file."""
import shutil
import subprocess
import sys
import threading
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch

from django.core.management import call_command
from django.test import TransactionTestCase, override_settings

from library.management.commands.analyze_tracks import get_waveforms_dir
from library.management.commands.sync_track_file import sync_track_file as real_sync_track_file
from library.models import Artist, Category, CategoryKind, Track
from weather import provenance as provenance_mod
from weather.publication import PublicationError, publish_weather_asset

from .test_publication import PublicationTestCase, _generated_waveform

SPEECH_LIB = Path(__file__).resolve().parents[2] / "weather_ingest" / "lib"
HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


class FlacPublicationTests(PublicationTestCase):
    def _publish(self, name, filename, content=b"fLaC-bytes", **kwargs):
        candidate = self.make_candidate(name=name, content=content)
        with self._patch_analyze(succeed=True):
            return publish_weather_asset(str(candidate), "WxTemp", filename, **kwargs)

    # -- ordinary registration ----------------------------------------------------------------------------------

    def test_new_flac_creates_a_new_track_at_its_own_path(self):
        result = self._publish("c.flac", "current_temp.flac", producer="current_temp.py", voice="Max")
        dest = self.library_root / "WxTemp" / "current_temp.flac"
        self.assertTrue(dest.is_file())
        self.assertTrue(result.created)
        self.assertEqual(result.final_path, dest)
        self.assertEqual(result.track.filepath, str(dest))
        self.assertEqual(result.track.filename, "current_temp.flac")
        self.assertEqual(result.track.format, "flac")
        self.assertTrue(result.track.ready2air)
        self.assertEqual(Track.objects.filter(category=self.category).count(), 1)

    def test_regenerating_the_same_flac_updates_the_same_track(self):
        first = self._publish("a.flac", "current_temp.flac", content=b"A")
        second = self._publish("b.flac", "current_temp.flac", content=b"B")
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.track.id, second.track.id)
        self.assertEqual((self.library_root / "WxTemp" / "current_temp.flac").read_bytes(), b"B")
        self.assertEqual(Track.objects.filter(category=self.category).count(), 1)

    def test_an_mp3_candidate_is_still_accepted(self):
        result = self._publish("c.mp3", "current_temp.mp3")
        self.assertEqual(result.track.format, "mp3")

    # -- the legacy MP3 is left strictly alone ------------------------------------------------------------------

    def test_publishing_a_flac_leaves_an_existing_mp3_track_and_file_untouched(self):
        old = self._publish("old.mp3", "current_temp.mp3", content=b"OLD-MP3")
        old_path = self.library_root / "WxTemp" / "current_temp.mp3"
        before = Track.objects.filter(id=old.track.id).values().get()

        new = self._publish("new.flac", "current_temp.flac", content=b"NEW-FLAC")

        self.assertNotEqual(new.track.id, old.track.id)
        self.assertTrue(new.created)
        self.assertEqual(Track.objects.filter(id=old.track.id).values().get(), before)
        self.assertEqual(old_path.read_bytes(), b"OLD-MP3")
        # An operator-managed, temporary condition: both Tracks exist until the operator retires the MP3.
        self.assertEqual(Track.objects.filter(category=self.category).count(), 2)

    def test_the_legacy_mp3_is_not_rewritten_when_the_flac_is_regenerated(self):
        self._publish("old.mp3", "current_temp.mp3", content=b"OLD-MP3")
        old_path = self.library_root / "WxTemp" / "current_temp.mp3"
        stamp = old_path.stat().st_mtime_ns
        self._publish("a.flac", "current_temp.flac", content=b"A")
        self._publish("b.flac", "current_temp.flac", content=b"B")
        self.assertEqual((old_path.read_bytes(), old_path.stat().st_mtime_ns), (b"OLD-MP3", stamp))

    # -- failure ------------------------------------------------------------------------------------------------

    def test_failed_analysis_keeps_the_previously_published_flac_and_its_track(self):
        good = self._publish("good.flac", "current_temp.flac", content=b"LAST-KNOWN-GOOD")
        dest = self.library_root / "WxTemp" / "current_temp.flac"
        candidate = self.make_candidate(name="bad.flac", content=b"BAD")
        with self._patch_analyze(succeed=False):
            with self.assertRaises(PublicationError):
                publish_weather_asset(str(candidate), "WxTemp", "current_temp.flac")
        self.assertEqual(dest.read_bytes(), b"LAST-KNOWN-GOOD")
        self.assertEqual(Track.objects.filter(category=self.category).count(), 1)
        self.assertEqual(Track.objects.get().id, good.track.id)
        self.assertTrue(Path(Track.objects.get().waveform_path).is_file())

    def test_failed_first_flac_publication_leaves_nothing_and_the_mp3_alone(self):
        old = self._publish("old.mp3", "current_temp.mp3", content=b"OLD-MP3")
        candidate = self.make_candidate(name="new.flac", content=b"NEW")
        with self._patch_analyze(succeed=False):
            with self.assertRaises(PublicationError):
                publish_weather_asset(str(candidate), "WxTemp", "current_temp.flac")
        self.assertFalse((self.library_root / "WxTemp" / "current_temp.flac").exists())
        self.assertEqual(list(Track.objects.filter(category=self.category).values_list("id", flat=True)),
                         [old.track.id])
        self.assertEqual((self.library_root / "WxTemp" / "current_temp.mp3").read_bytes(), b"OLD-MP3")

    def test_waveform_publish_failure_rolls_the_flac_back(self):
        good = self._publish("good.flac", "current_temp.flac", content=b"GOOD")
        candidate = self.make_candidate(name="bad.flac", content=b"BAD")
        with self._patch_analyze(succeed=True), \
             patch("weather.publication.waveform_cache.install_file", side_effect=OSError("disk full")):
            with self.assertRaises(PublicationError):
                publish_weather_asset(str(candidate), "WxTemp", "current_temp.flac")
        self.assertEqual((self.library_root / "WxTemp" / "current_temp.flac").read_bytes(), b"GOOD")
        self.assertEqual(Track.objects.get().id, good.track.id)

    # -- validation ---------------------------------------------------------------------------------------------

    def test_unsupported_candidate_formats_are_rejected(self):
        candidate = self.make_candidate(name="c.wav")
        with self.assertRaisesRegex(PublicationError, "FLAC"):
            publish_weather_asset(str(candidate), "WxTemp", "current_temp.wav")

    def test_destination_extension_must_match_the_candidate(self):
        for name, filename in (("c.flac", "current_temp.mp3"), ("c.mp3", "current_temp.flac")):
            with self.subTest(candidate=name, destination=filename):
                candidate = self.make_candidate(name=name)
                with self.assertRaisesRegex(PublicationError, "extension"):
                    publish_weather_asset(str(candidate), "WxTemp", filename)
                self.assertFalse((self.library_root / "WxTemp" / filename).exists())
        self.assertEqual(Track.objects.count(), 0)

    def test_destination_filename_must_be_a_plain_name(self):
        candidate = self.make_candidate(name="c.flac")
        with self.assertRaisesRegex(PublicationError, "plain name"):
            publish_weather_asset(str(candidate), "WxTemp", "../escape.flac")

    # -- lock ---------------------------------------------------------------------------------------------------

    def test_the_lock_is_the_ordinary_per_destination_lock(self):
        self._publish("a.flac", "current_temp.flac")
        locks = sorted(p.name for p in (self.library_root / "WxTemp").iterdir() if p.name.endswith(".lock"))
        self.assertEqual(locks, [".current_temp.flac.publish.lock"])

    # -- provenance, waveform, command --------------------------------------------------------------------------

    def test_provenance_and_waveform_are_generated_for_a_flac(self):
        result = self._publish("c.flac", "current_temp.flac", producer="current_temp.py", voice="Max_Weatherly")
        self.assertIsNone(result.provenance_error)
        prov, error = provenance_mod.read_provenance(self.data_dir, "WxTemp")
        self.assertIsNone(error)
        self.assertEqual(prov.get("filename"), "current_temp.flac")
        self.assertEqual(prov.get("final_path"), str(self.library_root / "WxTemp" / "current_temp.flac"))
        self.assertEqual(result.track.waveform_path, str(get_waveforms_dir() / f"{result.track.id}.json"))
        self.assertTrue(Path(result.track.waveform_path).is_file())

    def test_command_publishes_a_flac_like_any_other_asset(self):
        candidate = self.make_candidate(name="new.flac")
        out = StringIO()
        with self._patch_analyze(succeed=True):
            call_command("publish_weather_asset", str(candidate), "WxTemp", "current_temp.flac", stdout=out)
        self.assertIn("Created track", out.getvalue())
        self.assertIn("WxTemp/current_temp.flac", out.getvalue())


@skipUnless(HAVE_FFMPEG, "ffmpeg is required")
class FlacMetadataTests(PublicationTestCase):
    """A real ffmpeg-encoded FLAC (the producers' own encode) registers with the operator-visible title and artist
    the MP3 carried: Django's tag extraction reads FLAC Vorbis comments."""

    def test_vorbis_comments_populate_the_track(self):
        sys.path.insert(0, str(SPEECH_LIB))
        try:
            import speech_audio
        finally:
            sys.path.remove(str(SPEECH_LIB))
        wav = Path(self.temporary.name) / "in.wav"
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "anoisesrc=d=2:c=pink:r=24000:a=0.05:seed=1", "-ac", "1", "-sample_fmt", "s16", str(wav)],
                       check=True)
        flac = Path(self.temporary.name) / "current_temp.flac"
        speech_audio.encode_flac(["-i", str(wav)], flac, 4.75,
                                 {"artist": "Oak Grove Radio", "title": "Current Temp (Max)"})

        with self._patch_analyze(succeed=True):
            result = publish_weather_asset(str(flac), "WxTemp", "current_temp.flac")

        track = Track.objects.get(id=result.track.id)
        self.assertEqual(track.title, "Current Temp (Max)")
        self.assertEqual(track.artist.name, "Oak Grove Radio")
        self.assertEqual((track.format, track.filename), ("flac", "current_temp.flac"))
        self.assertAlmostEqual(track.duration_seconds, 3.0, delta=0.1)     # 2 s speech + the 1 s lead-in


class SameDestinationLockTests(TransactionTestCase):
    """Two publications of the SAME destination (NWS and AMBER alerts both publish WxAlert/wx_alert.flac) never
    interleave their stage / Track / waveform sequence."""

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.library_root = Path(self.temporary.name) / "library"
        self.data_dir = Path(self.temporary.name) / "weather-data"
        self.library_root.mkdir()
        self.data_dir.mkdir()
        patcher = override_settings(
            LIBRARY_ROOT=str(self.library_root), WEATHER_DATA_DIR=str(self.data_dir),
            WAVEFORMS_DIR=str(Path(self.temporary.name) / "waveforms"),
        )
        patcher.enable()
        self.addCleanup(patcher.disable)
        kind, _ = CategoryKind.objects.get_or_create(code="imaging", defaults={"name": "Imaging"})
        self.category, _ = Category.objects.get_or_create(code="WxAlert", defaults={"name": "WxAlert", "kind": kind})
        Artist.get_or_create_ci("Oak Grove Radio")
        self.addCleanup(shutil.rmtree, get_waveforms_dir(), ignore_errors=True)

    def test_concurrent_publications_of_one_destination_never_overlap(self):
        active = {"count": 0}
        seen = []
        guard = threading.Lock()

        def instrumented_sync(path, wave_dir=None):
            with guard:
                active["count"] += 1
                seen.append(active["count"])
            try:
                threading.Event().wait(timeout=0.15)
                return real_sync_track_file(path, wave_dir=wave_dir)
            finally:
                with guard:
                    active["count"] -= 1

        def fake_analyze(row, cfg_values, wave_dir_arg, force, existing_artist_names_casefolded=None, **kwargs):
            scratch_path = Path(wave_dir_arg) / f"{row[0]}.json"
            scratch_path.write_text(_generated_waveform(row[0]))
            Track.objects.filter(id=row[0]).update(waveform_path=str(scratch_path))
            return True

        candidates = []
        for name in ("nws.flac", "amber.flac"):
            path = Path(self.temporary.name) / name
            path.write_bytes(name.encode())
            candidates.append(path)
        errors = []

        def worker(candidate):
            try:
                publish_weather_asset(str(candidate), "WxAlert", "wx_alert.flac")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                from django.db import connection
                connection.close()

        with patch("weather.publication.sync_track_file", side_effect=instrumented_sync), \
             patch("library.management.commands.sync_track_file.analyze_one_track", side_effect=fake_analyze):
            threads = [threading.Thread(target=worker, args=(c,)) for c in candidates]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)

        self.assertEqual(errors, [])
        self.assertEqual(max(seen), 1, "two publications of one destination ran concurrently")
        tracks = list(Track.objects.filter(category=self.category))
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].format, "flac")
        self.assertTrue(Path(tracks[0].filepath).is_file())
