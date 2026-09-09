"""Last-known-good publication coverage for weather.publication --
P1 2.4 Pass G. Mirrors the r0055 generic announcement renderer's own
Rotation Asset LKG test shape (isadoraair/tests/test_announcements_
renderer.py) without touching that module at all -- this command is a
narrow, Weather-specific bridge, not an extension of the generic
renderer's contract."""
import shutil
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, override_settings

from library.management.commands.analyze_tracks import get_waveforms_dir
from library.management.commands.sync_track_file import sync_track_file as real_sync_track_file
from library.models import Artist, Category, CategoryKind, Track
from weather import provenance as provenance_mod
from weather.publication import PublicationError, publish_weather_asset


@override_settings(WAVEFORMS_DIR="/tmp/isadoraair-r0057-test-waveforms")
class PublicationTestCase(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.library_root = Path(self.temporary.name) / "library"
        self.data_dir = Path(self.temporary.name) / "weather-data"
        self.library_root.mkdir()
        self.data_dir.mkdir()

        self.settings_patcher = override_settings(
            LIBRARY_ROOT=str(self.library_root), WEATHER_DATA_DIR=str(self.data_dir),
        )
        self.settings_patcher.enable()
        self.addCleanup(self.settings_patcher.disable)

        kind, _ = CategoryKind.objects.get_or_create(code="imaging", defaults={"name": "Imaging"})
        self.category = Category.objects.create(code="WxTemp", name="WxTemp", kind=kind)
        self.artist, _ = Artist.get_or_create_ci("Oak Grove Radio")

        wave_dir = get_waveforms_dir()
        self.addCleanup(shutil.rmtree, wave_dir, ignore_errors=True)

    def make_candidate(self, name="candidate.mp3", content=b"ID3fake-mp3-bytes"):
        path = Path(self.temporary.name) / name
        path.write_bytes(content)
        return path

    def _patch_analyze(self, succeed=True, write_waveform=True):
        """Realistic analyzer test double -- mirrors analyze_one_track()'s
        actual bookkeeping (writes <track_id>.json into the supplied
        scratch wave_dir and points Track.waveform_path at that SCRATCH
        file, exactly as the real function does) rather than a bare
        return_value=True that would silently skip past the very
        waveform-promotion behavior this test module exists to prove.

        write_waveform=False simulates analyze_one_track's own
        documented non-fatal case: a waveform WRITE failure that is
        merely logged, with Track fields still updated and True still
        returned -- see this module's MissingScratchWaveformTests."""
        def fake_analyze(row, cfg_values, wave_dir_arg, force, existing_artist_names_casefolded=None):
            if succeed and write_waveform:
                track_id = row[0]
                scratch_path = Path(wave_dir_arg) / f"{track_id}.json"
                scratch_path.write_text('{"marker": "GENERATED-WAVEFORM"}')
                Track.objects.filter(id=track_id).update(waveform_path=str(scratch_path))
            return succeed
        return patch(
            "library.management.commands.sync_track_file.analyze_one_track",
            side_effect=fake_analyze,
        )


class ValidationTests(PublicationTestCase):
    def test_missing_candidate_rejected(self):
        with self.assertRaises(PublicationError):
            publish_weather_asset(
                str(self.library_root / "does-not-exist.mp3"), "WxTemp", "current_temp.mp3",
            )

    def test_non_mp3_suffix_rejected(self):
        candidate = self.make_candidate(name="candidate.wav")
        with self.assertRaises(PublicationError):
            publish_weather_asset(str(candidate), "WxTemp", "current_temp.mp3")

    def test_empty_candidate_rejected(self):
        candidate = self.make_candidate(content=b"")
        with self.assertRaises(PublicationError):
            publish_weather_asset(str(candidate), "WxTemp", "current_temp.mp3")

    def test_unknown_category_rejected(self):
        candidate = self.make_candidate()
        with self.assertRaises(PublicationError):
            publish_weather_asset(str(candidate), "NoSuchCategory", "current_temp.mp3")


class FirstGenerationTests(PublicationTestCase):
    def test_successful_first_publish_creates_track_and_provenance(self):
        candidate = self.make_candidate(content=b"FIRST-GENERATION")
        with self._patch_analyze(succeed=True):
            result = publish_weather_asset(
                str(candidate), "WxTemp", "current_temp.mp3",
                producer="current_temp.py", voice="Claira_Sky",
                source_kind="derived_local", source_age_seconds=42.0,
            )

        dest = self.library_root / "WxTemp" / "current_temp.mp3"
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.read_bytes(), b"FIRST-GENERATION")
        self.assertTrue(result.created)
        self.assertEqual(result.track.filepath, str(dest))
        self.assertIsNone(result.provenance_error)

        payload, error = provenance_mod.read_provenance(self.data_dir, "WxTemp")
        self.assertIsNone(error)
        self.assertEqual(payload["category_code"], "WxTemp")
        self.assertEqual(payload["final_path"], str(dest))
        self.assertEqual(payload["producer"], "current_temp.py")
        self.assertEqual(payload["voice"], "Claira_Sky")
        self.assertEqual(payload["source_kind"], "derived_local")
        self.assertEqual(payload["source_age_seconds"], 42.0)
        self.assertFalse(payload["used_fallback"])
        self.assertEqual(payload["sha256"], provenance_mod.sha256_of(dest))

    def test_successful_publish_promotes_waveform_path_off_scratch(self):
        """P1 2.4 Pass G correction: analyze_one_track() persists
        Track.waveform_path pointing at the SCRATCH copy (it has no
        idea publish_weather_asset will relocate it) -- the committed
        invariant must be Track.waveform_path == the REAL waveform
        path, with that exact file existing, and no trace of the
        (already-deleted) scratch directory anywhere."""
        candidate = self.make_candidate(content=b"WAVEFORM-INVARIANT")
        with self._patch_analyze(succeed=True):
            result = publish_weather_asset(str(candidate), "WxTemp", "current_temp.mp3")

        real_wave_dir = get_waveforms_dir()
        real_waveform_path = real_wave_dir / f"{result.track.id}.json"

        self.assertTrue(real_waveform_path.is_file(), "the real waveform file must exist")
        self.assertEqual(real_waveform_path.read_text(), '{"marker": "GENERATED-WAVEFORM"}')
        self.assertEqual(
            result.track.waveform_path, str(real_waveform_path),
            "the returned Track object must already reflect the REAL path",
        )
        # Re-fetch independently -- the DB row itself must agree.
        refetched = Track.objects.get(id=result.track.id)
        self.assertEqual(refetched.waveform_path, str(real_waveform_path))
        self.assertNotIn(".publish-", refetched.waveform_path, "no DB field may reference the scratch directory")
        self.assertEqual(
            Path(refetched.waveform_path).parent, real_wave_dir,
            "waveform_path must live in the real WAVEFORMS_DIR, not any scratch subdirectory",
        )

        # The scratch directory itself is gone.
        scratch_dirs = [
            p for p in (self.library_root / "WxTemp").iterdir()
            if p.is_dir() and ".publish-" in p.name
        ]
        self.assertEqual(scratch_dirs, [], "the publication scratch directory must not survive")

        # Audio/Track identity and provenance remain correct.
        dest = self.library_root / "WxTemp" / "current_temp.mp3"
        self.assertEqual(dest.read_bytes(), b"WAVEFORM-INVARIANT")
        self.assertEqual(refetched.filepath, str(dest))
        payload, error = provenance_mod.read_provenance(self.data_dir, "WxTemp")
        self.assertIsNone(error)
        self.assertEqual(payload["final_path"], str(dest))

    def test_first_generation_analysis_failure_leaves_no_orphan(self):
        candidate = self.make_candidate(content=b"WILL-FAIL")
        with self._patch_analyze(succeed=False):
            with self.assertRaises(PublicationError):
                publish_weather_asset(str(candidate), "WxTemp", "current_temp.mp3")

        dest = self.library_root / "WxTemp" / "current_temp.mp3"
        self.assertFalse(dest.exists(), "no file should be left behind for a first-generation failure")
        self.assertFalse(Track.objects.filter(filepath=str(dest)).exists())
        wave_dir = get_waveforms_dir()
        self.assertEqual(list(wave_dir.glob("*.json")), [], "no orphan waveform file should remain")
        # No scratch directories left behind either -- the one expected
        # survivor is the per-destination lock file itself (P1 2.4 Pass
        # G correction), which is deliberately permanent: it's the same
        # file every future publish attempt for this destination must
        # find and flock(), not a scratch artifact to clean up.
        leftovers = [p for p in (self.library_root / "WxTemp").iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [self.library_root / "WxTemp" / ".current_temp.mp3.publish.lock"])

    def test_unrelated_waveform_is_never_touched_by_a_failed_first_generation_publish(self):
        """P1 2.4 Pass G correction: rollback must never scan/delete
        arbitrary waveform files by before/after directory diff -- an
        unrelated waveform (e.g. from a concurrent, uncommitted
        analysis elsewhere in the library) that happens to appear
        during a failed Weather first-generation publish must survive
        untouched. Weather's own analysis writes only into a
        publication-owned scratch directory, so its own failed attempt
        never reaches the real WAVEFORMS_DIR at all."""
        wave_dir = get_waveforms_dir()
        unrelated_id = 999999
        unrelated_waveform = wave_dir / f"{unrelated_id}.json"
        unrelated_waveform.write_text('{"marker": "UNRELATED"}')
        self.addCleanup(unrelated_waveform.unlink, missing_ok=True)

        candidate = self.make_candidate(content=b"WILL-FAIL")
        with self._patch_analyze(succeed=False):
            with self.assertRaises(PublicationError):
                publish_weather_asset(str(candidate), "WxTemp", "current_temp.mp3")

        self.assertEqual(unrelated_waveform.read_text(), '{"marker": "UNRELATED"}')
        dest = self.library_root / "WxTemp" / "current_temp.mp3"
        self.assertFalse(dest.exists())
        self.assertFalse(Track.objects.filter(filepath=str(dest)).exists())
        self.assertEqual(
            sorted(p.name for p in wave_dir.glob("*.json")), [unrelated_waveform.name],
            "Weather's own failed attempt must leave no waveform of its own behind either",
        )

    def test_provenance_write_failure_does_not_undo_successful_publish(self):
        candidate = self.make_candidate(content=b"PUBLISH-OK")
        with self._patch_analyze(succeed=True), patch.object(
            provenance_mod, "write_provenance", side_effect=OSError("disk full"),
        ):
            result = publish_weather_asset(str(candidate), "WxTemp", "current_temp.mp3")

        dest = self.library_root / "WxTemp" / "current_temp.mp3"
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.read_bytes(), b"PUBLISH-OK")
        self.assertIsNotNone(result.track.id)
        self.assertIsNotNone(result.provenance_error)
        self.assertIsNone(result.provenance_path)


class RegenerationTests(PublicationTestCase):
    def _publish_once(self, content):
        candidate = self.make_candidate(content=content)
        with self._patch_analyze(succeed=True):
            return publish_weather_asset(str(candidate), "WxTemp", "current_temp.mp3")

    def test_regeneration_preserves_track_identity(self):
        first = self._publish_once(b"VERSION-ONE")
        second = self._publish_once(b"VERSION-TWO")

        self.assertEqual(first.track.id, second.track.id, "Track identity must remain stable across regenerations")
        dest = self.library_root / "WxTemp" / "current_temp.mp3"
        self.assertEqual(dest.read_bytes(), b"VERSION-TWO")

    def test_failed_regeneration_restores_last_known_good_file_and_waveform(self):
        self._publish_once(b"LAST-GOOD")
        dest = self.library_root / "WxTemp" / "current_temp.mp3"
        wave_dir = get_waveforms_dir()
        track = Track.objects.get(filepath=str(dest))
        waveform_path = wave_dir / f"{track.id}.json"
        waveform_path.write_text('{"marker": "LAST-GOOD-WAVEFORM"}')

        def overwrite_waveform_then_fail(row, cfg_values, wave_dir_arg, force, existing_artist_names_casefolded=None):
            (wave_dir_arg / f"{row[0]}.json").write_text('{"marker": "NEW-WAVEFORM"}')
            return False

        candidate = self.make_candidate(content=b"WILL-FAIL-REGEN")
        with patch(
            "library.management.commands.sync_track_file.analyze_one_track",
            side_effect=overwrite_waveform_then_fail,
        ):
            with self.assertRaises(PublicationError):
                publish_weather_asset(str(candidate), "WxTemp", "current_temp.mp3")

        self.assertEqual(dest.read_bytes(), b"LAST-GOOD")
        self.assertEqual(waveform_path.read_text(), '{"marker": "LAST-GOOD-WAVEFORM"}')


class MissingScratchWaveformTests(PublicationTestCase):
    """P1 2.4 Pass G correction: sync_track_file() returning
    successfully does NOT by itself prove a waveform was persisted --
    analyze_one_track() historically treats its own waveform-file write
    failure as non-fatal (logs it, still updates Track fields, still
    returns True). Weather's own publication contract is stronger:
    audio + Track + analysis + a REAL persisted waveform, together or
    not at all. These tests simulate that exact non-fatal-write-failure
    shape via write_waveform=False (analysis "succeeds" -- returns True
    -- but produces no scratch waveform file)."""

    def test_first_generation_missing_waveform_is_a_publication_failure(self):
        candidate = self.make_candidate(content=b"NO-WAVEFORM-FIRST-GEN")
        with self._patch_analyze(succeed=True, write_waveform=False):
            with self.assertRaises(PublicationError):
                publish_weather_asset(str(candidate), "WxTemp", "current_temp.mp3")

        dest = self.library_root / "WxTemp" / "current_temp.mp3"
        self.assertFalse(dest.exists(), "no destination audio may remain")
        self.assertFalse(Track.objects.filter(filepath=str(dest)).exists(), "no Track may remain")
        wave_dir = get_waveforms_dir()
        self.assertEqual(list(wave_dir.glob("*.json")), [], "no real Weather waveform may remain")

    def test_first_generation_missing_waveform_leaves_unrelated_waveform_untouched(self):
        wave_dir = get_waveforms_dir()
        unrelated_waveform = wave_dir / "999999.json"
        unrelated_waveform.write_text('{"marker": "UNRELATED"}')
        self.addCleanup(unrelated_waveform.unlink, missing_ok=True)

        candidate = self.make_candidate(content=b"NO-WAVEFORM-FIRST-GEN")
        with self._patch_analyze(succeed=True, write_waveform=False):
            with self.assertRaises(PublicationError):
                publish_weather_asset(str(candidate), "WxTemp", "current_temp.mp3")

        self.assertEqual(unrelated_waveform.read_text(), '{"marker": "UNRELATED"}')
        self.assertEqual(sorted(p.name for p in wave_dir.glob("*.json")), [unrelated_waveform.name])

    def test_regeneration_missing_waveform_restores_prior_lkg_state(self):
        candidate = self.make_candidate(content=b"LAST-GOOD-AUDIO")
        with self._patch_analyze(succeed=True):
            first = publish_weather_asset(str(candidate), "WxTemp", "current_temp.mp3")

        dest = self.library_root / "WxTemp" / "current_temp.mp3"
        wave_dir = get_waveforms_dir()
        real_waveform_path = wave_dir / f"{first.track.id}.json"
        self.assertTrue(real_waveform_path.is_file())
        prior_waveform_content = real_waveform_path.read_text()
        prior_title = first.track.title

        second_candidate = self.make_candidate(name="second.mp3", content=b"WILL-FAIL-NO-WAVEFORM")
        with self._patch_analyze(succeed=True, write_waveform=False):
            with self.assertRaises(PublicationError):
                publish_weather_asset(str(second_candidate), "WxTemp", "current_temp.mp3")

        # Prior audio restored.
        self.assertEqual(dest.read_bytes(), b"LAST-GOOD-AUDIO")
        # Prior DB Track values restored by transaction rollback.
        track = Track.objects.get(id=first.track.id)
        self.assertEqual(track.title, prior_title)
        self.assertEqual(track.filepath, str(dest))
        # Prior real waveform remains/restores unchanged.
        self.assertTrue(real_waveform_path.is_file())
        self.assertEqual(real_waveform_path.read_text(), prior_waveform_content)
        # Track.waveform_path still points to the valid prior real waveform.
        self.assertEqual(track.waveform_path, str(real_waveform_path))


@override_settings(WAVEFORMS_DIR="/tmp/isadoraair-r0057-test-waveforms-lock")
class DestinationLockTests(TransactionTestCase):
    """P1 2.4 Pass G correction: two publish_weather_asset() calls
    targeting the SAME final destination (e.g. wx_alert.py and
    amber_alert.py, which have entirely separate producer-side
    lockfiles but both publish WxAlert/wx_alert.mp3) must never
    interleave their backup -> replace -> Track/analysis -> waveform ->
    provenance sequence. flock() genuinely serializes distinct file
    descriptors even within one process/multiple threads (each open()
    call is its own "open file description"), so a threading-based
    test is a faithful proof of the same mutual-exclusion property that
    holds across real separate processes.

    TransactionTestCase (not plain TestCase, see this project's own
    webrequests dedication-lifecycle tests for the same reasoning) --
    real worker threads mean real separate DB connections, which a
    plain TestCase's single wrapping transaction/rollback cannot
    correctly isolate."""

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.library_root = Path(self.temporary.name) / "library"
        self.data_dir = Path(self.temporary.name) / "weather-data"
        self.library_root.mkdir()
        self.data_dir.mkdir()

        self.settings_patcher = override_settings(
            LIBRARY_ROOT=str(self.library_root), WEATHER_DATA_DIR=str(self.data_dir),
        )
        self.settings_patcher.enable()
        self.addCleanup(self.settings_patcher.disable)

        kind, _ = CategoryKind.objects.get_or_create(code="imaging", defaults={"name": "Imaging"})
        self.category = Category.objects.create(code="WxTemp", name="WxTemp", kind=kind)
        Artist.get_or_create_ci("Oak Grove Radio")

        wave_dir = get_waveforms_dir()
        self.addCleanup(shutil.rmtree, wave_dir, ignore_errors=True)

    def make_candidate(self, name, content):
        path = Path(self.temporary.name) / name
        path.write_bytes(content)
        return path

    def test_two_same_destination_publications_never_overlap(self):
        active = {"count": 0}
        max_concurrent = []
        counter_lock = threading.Lock()

        def instrumented_sync(path, wave_dir=None):
            with counter_lock:
                active["count"] += 1
                max_concurrent.append(active["count"])
            try:
                # Hold the "critical section" long enough that a real
                # overlap would be observed if the destination lock
                # failed to serialize these two calls.
                threading.Event().wait(timeout=0.15)
                return real_sync_track_file(path, wave_dir=wave_dir)
            finally:
                with counter_lock:
                    active["count"] -= 1

        candidate_a = self.make_candidate(name="a.mp3", content=b"AAAA-CONTENT")
        candidate_b = self.make_candidate(name="b.mp3", content=b"BBBB-CONTENT")
        errors = []
        results = {}

        def worker(label, candidate):
            try:
                results[label] = publish_weather_asset(str(candidate), "WxTemp", "current_temp.mp3")
            except Exception as exc:  # pragma: no cover -- surfaced via errors below
                errors.append(exc)
            finally:
                # Each thread gets its own DB connection; close it
                # explicitly so Django's test-database teardown doesn't
                # find leftover sessions still attached.
                from django.db import connection
                connection.close()

        def fake_analyze(row, cfg_values, wave_dir_arg, force, existing_artist_names_casefolded=None):
            # Realistic bookkeeping (see PublicationTestCase._patch_
            # analyze's own docstring): writes into the supplied
            # scratch wave_dir and points Track.waveform_path there,
            # exactly like the real analyze_one_track().
            track_id = row[0]
            scratch_path = Path(wave_dir_arg) / f"{track_id}.json"
            scratch_path.write_text('{"marker": "GENERATED-WAVEFORM"}')
            Track.objects.filter(id=track_id).update(waveform_path=str(scratch_path))
            return True

        # The analyze_one_track patch is applied ONCE, outside both
        # threads, for the whole test -- unittest.mock.patch's own
        # __enter__/__exit__ mutate a shared module attribute and are
        # not safe to start/stop independently from two concurrent
        # threads.
        with patch("weather.publication.sync_track_file", side_effect=instrumented_sync), \
             patch("library.management.commands.sync_track_file.analyze_one_track", side_effect=fake_analyze):
            t1 = threading.Thread(target=worker, args=("first", candidate_a))
            t2 = threading.Thread(target=worker, args=("second", candidate_b))
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

        self.assertEqual(errors, [], f"unexpected publish failure(s): {errors}")
        self.assertEqual(len(results), 2)
        self.assertEqual(
            max(max_concurrent), 1,
            "both publications entered the critical section concurrently -- "
            "the destination lock did not serialize them",
        )
        # Exactly one of the two candidates' content ends up published
        # (whichever ran second wins) -- no torn/interleaved write.
        dest = self.library_root / "WxTemp" / "current_temp.mp3"
        self.assertIn(dest.read_bytes(), (b"AAAA-CONTENT", b"BBBB-CONTENT"))
        # Both publishes resolve to the same stable Track identity.
        self.assertEqual(results["first"].track.id, results["second"].track.id)


class ManagementCommandTests(PublicationTestCase):
    def test_command_success_reports_track(self):
        candidate = self.make_candidate(content=b"CLI-PUBLISH")
        with self._patch_analyze(succeed=True):
            call_command("publish_weather_asset", str(candidate), "WxTemp", "current_temp.mp3")

        dest = self.library_root / "WxTemp" / "current_temp.mp3"
        self.assertTrue(dest.is_file())

    def test_command_failure_raises_command_error_with_nonzero_exit(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command(
                "publish_weather_asset",
                str(self.library_root / "missing.mp3"), "WxTemp", "current_temp.mp3",
            )
