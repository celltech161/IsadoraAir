"""r0123 Package A: the cue-marker authority, its model-level guards, fail-closed waveform-cache
validation and the routed writers (repick, reset, reanalyze, Track Detail, analysis, admin).

Every file lives in a per-test temporary directory; no station media, waveform or database is
touched (the suite runs only inside the test sandbox)."""
import hashlib
import json
import math
import struct
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings

from library.admin import TrackAdmin
from library.management.commands import analyze_tracks
from library.models import Artist, Category, CategoryKind, PlayEvent, Track, TrackWriteRefused
from library.services import cue_markers, waveform_cache
from monitoring.models import SystemEvent

M = cue_markers.MARKERS
REPO = Path(__file__).resolve().parents[2]


def _make_track(directory, *, duration=200.0, category=None, **markers):
    audio = Path(directory) / f"audio-{len(list(Path(directory).iterdir()))}.flac"
    audio.write_bytes(b"pretend audio bytes " * 64)
    artist, _ = Artist.get_or_create_ci("Cue Marker Test Artist")
    return Track.objects.create(filepath=str(audio), filename=audio.name, title="Cue Test", artist=artist,
                                duration_seconds=duration, category=category, **markers)


def _row(track):
    return cue_markers.snapshot(track.pk)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class _Fixture(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.user = User.objects.create_superuser("cue-op", "cue-op@example.invalid", "pw")
        # A historical Track: every marker of unknown provenance, analyzed before.
        self.legacy = _make_track(self.dir, cue_in_seconds=1.0, cue_out_seconds=195.0, next_start_seconds=190.0,
                                  intro_until_seconds=10.0, sweep_start_seconds=12.0,
                                  outro_starts_seconds=180.0, hook_in_seconds=60.0, hook_out_seconds=70.0,
                                  ready2air=True)
        Track.objects.filter(pk=self.legacy.pk).update(waveform_path=str(self.dir / "old.json"))
        self.legacy.refresh_from_db()


# ------------------------------------------------------------------ migration / persistence

class MigrationPreservesMarkersTests(TransactionTestCase):
    """Deliberately not serialized_rollback (the documented ContentType duplicate-key collision
    with other TransactionTestCase classes in one run); the schema is always returned to 0099."""
    BEFORE = [("library", "0098_hour_refill_automatic_failure_cause")]
    AFTER = [("library", "0099_track_cue_marker_authority")]

    def test_all_eight_values_and_readiness_survive_the_migration_without_invented_provenance(self):
        self.addCleanup(call_command, "migrate", verbosity=0)          # every app back to its leaf
        executor = MigrationExecutor(connection)
        executor.migrate(self.BEFORE)
        apps = executor.loader.project_state(self.BEFORE).apps
        HistoricalTrack, HistoricalArtist = apps.get_model("library", "Track"), apps.get_model("library", "Artist")
        artist = HistoricalArtist.objects.create(name="Migration Artist")
        values = dict(zip(M, (2.5, 201.0, 199.25, 11.0, None, 185.5, 61.0, 72.0)))
        row = HistoricalTrack.objects.create(filepath="/tmp/isadoraair-test-migration.flac", filename="m.flac",
                                             title="M", artist=artist, duration_seconds=210.0, ready2air=True,
                                             waveform_path="/tmp/isadoraair-test-migration.json", **values)
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(self.AFTER)
        after = Track.objects.filter(pk=row.pk).values(*M, "cue_marker_state", "cue_marker_revision",
                                                       "analysis_requested_at", "ready2air").get()
        self.assertEqual({name: after[name] for name in M}, values)
        self.assertEqual((after["cue_marker_state"], after["cue_marker_revision"]), ({}, 0))
        self.assertIsNone(after["analysis_requested_at"])
        self.assertIs(after["ready2air"], True)
        for name in M:
            expected = cue_markers.UNSET if values[name] is None else cue_markers.LEGACY
            self.assertEqual(cue_markers.protection_of(name, after | {"waveform_path": "x"}), expected, name)

    def test_the_migration_is_schema_only_with_database_defaults(self):
        source = (REPO / "library/migrations/0099_track_cue_marker_authority.py").read_text()
        self.assertNotIn("RunPython", source)
        self.assertNotIn("RunSQL", source)
        self.assertIn("db_default={}", source)
        self.assertIn("db_default=0", source)


class ProtectionModelTests(TestCase):
    def test_historical_values_are_protected_and_only_safe_initial_cases_are_unset(self):
        analyzed = {"waveform_path": "/w.json", "cue_marker_state": {}}
        fresh = {"waveform_path": "", "cue_marker_state": {}}
        self.assertEqual(cue_markers.protection_of("next_start_seconds", analyzed | {"next_start_seconds": 3.0}),
                         cue_markers.LEGACY)
        self.assertEqual(cue_markers.protection_of("next_start_seconds", analyzed | {"next_start_seconds": None}),
                         cue_markers.UNSET)
        # Cue In's column default 0 on a never-analyzed Track: nothing was set, nothing is guessed.
        self.assertEqual(cue_markers.protection_of("cue_in_seconds", fresh | {"cue_in_seconds": 0.0}),
                         cue_markers.UNSET)
        self.assertEqual(cue_markers.protection_of("cue_in_seconds", analyzed | {"cue_in_seconds": 0.0}),
                         cue_markers.LEGACY)
        self.assertEqual(cue_markers.protection_of("cue_in_seconds", fresh | {"cue_in_seconds": 0.4}),
                         cue_markers.LEGACY)
        locked = analyzed | {"cue_marker_state": {"hook_in_seconds": {"protection": "locked"}}, "hook_in_seconds": 1}
        self.assertEqual(cue_markers.protection_of("hook_in_seconds", locked), cue_markers.LOCKED)


# ------------------------------------------------------------------ operator edits

class OperatorEditTests(_Fixture):
    def test_a_single_edit_locks_only_that_marker_and_is_audited_after_commit(self):
        with self.captureOnCommitCallbacks(execute=True):
            result = cue_markers.operator_edit(self.legacy.pk, expected_revision=0, user=self.user,
                                               values={"outro_starts_seconds": 182.5})
        self.assertTrue(result.ok, result.error)
        row = _row(self.legacy)
        self.assertEqual(row["outro_starts_seconds"], 182.5)
        self.assertEqual(row["cue_marker_revision"], 1)
        self.assertEqual(set(row["cue_marker_state"]), {"outro_starts_seconds"})
        entry = row["cue_marker_state"]["outro_starts_seconds"]
        self.assertEqual((entry["protection"], entry["source"], entry["approved_by"]),
                         ("locked", "operator", self.user.pk))
        for name in set(M) - {"outro_starts_seconds"}:
            self.assertEqual(row[name], getattr(self.legacy, name), name)
        self.assertTrue(SystemEvent.objects.filter(title="Cue markers changed by operator").exists())
        self.assertIs(Track.objects.get(pk=self.legacy.pk).ready2air, True)

    def test_a_stale_or_missing_revision_changes_nothing(self):
        cue_markers.operator_edit(self.legacy.pk, expected_revision=0, user=self.user, values={"hook_in_seconds": 62.0})
        stale = cue_markers.operator_edit(self.legacy.pk, expected_revision=0, user=self.user,
                                          values={"hook_in_seconds": 65.0})
        self.assertFalse(stale.ok)
        self.assertIn("reload", stale.conflict)
        missing = cue_markers.operator_edit(self.legacy.pk, expected_revision=None, user=self.user,
                                            values={"hook_in_seconds": 65.0})
        self.assertFalse(missing.ok)
        row = _row(self.legacy)
        self.assertEqual((row["hook_in_seconds"], row["cue_marker_revision"]), (62.0, 1))

    def test_approval_locks_an_unchanged_value_and_unlock_keeps_provenance(self):
        approved = cue_markers.operator_edit(self.legacy.pk, expected_revision=0, user=self.user,
                                             approve=["next_start_seconds"])
        self.assertEqual((approved.approved, approved.changed), (["next_start_seconds"], {}))
        row = _row(self.legacy)
        self.assertEqual((row["next_start_seconds"], row["cue_marker_revision"]), (190.0, 1))
        entry = row["cue_marker_state"]["next_start_seconds"]
        self.assertEqual((entry["protection"], entry["source"], entry["approved_by"]),
                         ("locked", "unknown", self.user.pk))           # approval, not an invented edit
        again = cue_markers.operator_edit(self.legacy.pk, expected_revision=1, user=self.user,
                                          approve=["next_start_seconds"])
        self.assertEqual((again.revision, again.skipped), (1, {"next_start_seconds": "already approved and locked"}))
        unlocked = cue_markers.operator_edit(self.legacy.pk, expected_revision=1, user=self.user,
                                             unlock=["next_start_seconds"])
        self.assertEqual((unlocked.unlocked, unlocked.revision), (["next_start_seconds"], 2))
        entry = _row(self.legacy)["cue_marker_state"]["next_start_seconds"]
        self.assertEqual((entry["protection"], entry["approved_by"], entry["unlocked_by"]),
                         ("unlocked", self.user.pk, self.user.pk))
        self.assertEqual(_row(self.legacy)["next_start_seconds"], 190.0)

    def test_invalid_values_are_refused_and_change_nothing(self):
        for values, fragment in (
            ({"cue_in_seconds": -1.0}, "negative"),
            ({"cue_in_seconds": float("nan")}, "finite"),
            ({"cue_out_seconds": 250.0}, "past the end"),
            ({"cue_in_seconds": 191.0}, "Cue In"),                       # after Next Start and Cue Out
            ({"hook_out_seconds": 60.0}, "Hook In"),                     # strictly after Hook In
            ({"cue_in_seconds": None}, "Cue In cannot be empty"),
        ):
            with self.subTest(values=values):
                result = cue_markers.operator_edit(self.legacy.pk, expected_revision=0, user=self.user, values=values)
                self.assertFalse(result.ok)
                self.assertIn(fragment, result.error)
        self.assertFalse(cue_markers.operator_edit(self.legacy.pk, expected_revision=0, user=self.user,
                                                   values={"duration_seconds": 1.0}).ok)
        row = _row(self.legacy)
        self.assertEqual((row["cue_marker_revision"], row["cue_marker_state"]), (0, {}))
        self.assertEqual({name: row[name] for name in M}, {name: getattr(self.legacy, name) for name in M})


# ------------------------------------------------------------------ calculations

class CalculationTests(_Fixture):
    def setUp(self):
        super().setUp()
        cue_markers.operator_edit(self.legacy.pk, expected_revision=0, user=self.user, unlock=["cue_in_seconds"])
        cue_markers.operator_edit(self.legacy.pk, expected_revision=1, user=self.user,
                                  values={"intro_until_seconds": 9.0})
        # cue_in unlocked; next_start historical; intro_until locked by the operator. Revision 2.

    def test_automatic_analysis_updates_only_eligible_markers(self):
        result = cue_markers.apply_calculation(self.legacy.pk, {"cue_in_seconds": 2.0, "next_start_seconds": 150.0,
                                                                "intro_until_seconds": 5.0}, basis_revision=2)
        self.assertTrue(result.ok)
        self.assertEqual(result.changed, {"cue_in_seconds": [1.0, 2.0]})
        self.assertEqual(result.preserved, {"next_start_seconds": "legacy", "intro_until_seconds": "locked"})
        row = _row(self.legacy)
        self.assertEqual((row["cue_in_seconds"], row["next_start_seconds"], row["intro_until_seconds"]),
                         (2.0, 190.0, 9.0))
        self.assertEqual(row["cue_marker_state"]["cue_in_seconds"]["source"], "analysis")
        self.assertEqual(row["cue_marker_revision"], 3)

    def test_a_stale_calculation_cannot_overwrite_a_newer_operator_approval(self):
        cue_markers.operator_edit(self.legacy.pk, expected_revision=2, user=self.user, approve=["cue_in_seconds"])
        with self.captureOnCommitCallbacks(execute=True):
            result = cue_markers.apply_calculation(self.legacy.pk, {"cue_in_seconds": 3.0}, basis_revision=2)
        self.assertFalse(result.ok)
        self.assertIn("stale", result.conflict)
        self.assertEqual(_row(self.legacy)["cue_in_seconds"], 1.0)
        self.assertTrue(SystemEvent.objects.filter(title="Stale cue-marker calculation discarded").exists())

    def test_overrides_need_an_operator_request_and_explicit_confirmation(self):
        for kwargs in ({}, {"requested_by": self.user}, {"override_confirmed": True}):
            with self.subTest(kwargs=kwargs):
                result = cue_markers.apply_calculation(self.legacy.pk, {"next_start_seconds": 150.0}, basis_revision=2,
                                                       override=["next_start_seconds"], **kwargs)
                self.assertFalse(result.ok)
        result = cue_markers.apply_calculation(self.legacy.pk, {"next_start_seconds": 150.0}, basis_revision=2,
                                               requested_by=self.user, override=["next_start_seconds"],
                                               override_confirmed=True)
        self.assertEqual((result.ok, result.overridden), (True, ["next_start_seconds"]))
        entry = _row(self.legacy)["cue_marker_state"]["next_start_seconds"]
        self.assertEqual((entry["protection"], entry["overridden_from"]), ("unlocked", {"source": "unknown"}))

    def test_new_media_supersedes_protection_and_is_audited(self):
        with self.captureOnCommitCallbacks(execute=True):
            result = cue_markers.apply_calculation(
                self.legacy.pk, {"intro_until_seconds": 4.0, "next_start_seconds": 120.0}, basis_revision=2,
                source=cue_markers.SOURCE_GENERATED, media_replaced=True)
        self.assertEqual(result.superseded, {"intro_until_seconds": "locked", "next_start_seconds": "legacy"})
        row = _row(self.legacy)
        self.assertEqual((row["intro_until_seconds"], row["next_start_seconds"]), (4.0, 120.0))
        self.assertEqual(row["cue_marker_state"]["intro_until_seconds"]["source"], "generated")
        self.assertEqual(row["hook_in_seconds"], 60.0)                 # markers it does not supply: untouched
        self.assertTrue(SystemEvent.objects.filter(title="Cue-marker protection superseded by new media").exists())

    def test_copy_to_equivalent_media_keeps_values_and_provenance(self):
        twin = _make_track(self.dir)
        result = cue_markers.copy_for_equivalent_media(self.legacy.pk, twin.pk,
                                                       ("intro_until_seconds", "hook_in_seconds"))
        self.assertTrue(result.ok)
        row = _row(twin)
        self.assertEqual((row["intro_until_seconds"], row["hook_in_seconds"]), (9.0, 60.0))
        self.assertEqual(row["cue_marker_state"]["intro_until_seconds"]["protection"], "locked")
        self.assertEqual(cue_markers.protection_of("hook_in_seconds", row), cue_markers.LEGACY)
        self.assertFalse(cue_markers.copy_for_equivalent_media(self.legacy.pk, twin.pk, ("hook_out_seconds",)).ok)


# ------------------------------------------------------------------ model-level guards

class GuardTests(_Fixture):
    def test_bulk_paths_cannot_write_markers(self):
        with self.assertRaises(TrackWriteRefused):
            Track.objects.filter(pk=self.legacy.pk).update(next_start_seconds=None)
        self.legacy.hook_in_seconds = 1.0
        with self.assertRaises(TrackWriteRefused):
            Track.objects.bulk_update([self.legacy], ["hook_in_seconds"])
        with self.assertRaises(TrackWriteRefused):
            Track.objects.filter(pk=self.legacy.pk).update(cue_marker_revision=9)
        self.assertEqual(_row(self.legacy)["next_start_seconds"], 190.0)

    def test_a_save_cannot_change_a_marker_and_a_stale_instance_cannot_overwrite_one(self):
        self.legacy.next_start_seconds = 100.0
        with self.assertRaises(TrackWriteRefused):
            self.legacy.save()
        stale = Track.objects.get(pk=self.legacy.pk)
        cue_markers.operator_edit(self.legacy.pk, expected_revision=0, user=self.user, values={"cue_out_seconds": 196.0})
        stale.title = "Renamed"
        stale.save()                                         # metadata save of an instance loaded earlier
        row = Track.objects.filter(pk=self.legacy.pk).values("title", "cue_out_seconds", "cue_marker_revision").get()
        self.assertEqual(row, {"title": "Renamed", "cue_out_seconds": 196.0, "cue_marker_revision": 1})
        stale.refresh_from_db()
        stale.save(update_fields=["title", "cue_out_seconds"])   # unchanged marker named: simply not written

    def test_creation_may_carry_values_but_never_state(self):
        created = _make_track(self.dir, next_start_seconds=50.0)
        self.assertEqual(cue_markers.protection_of("next_start_seconds", _row(created) | {"waveform_path": "w"}),
                         cue_markers.LEGACY)
        artist = Artist.get_or_create_ci("Cue Marker Test Artist")[0]
        with self.assertRaises(TrackWriteRefused):
            Track.objects.create(filepath=str(self.dir / "s.flac"), filename="s.flac", title="S", artist=artist,
                                 cue_marker_state={"cue_in_seconds": {"protection": "unlocked"}})
        with self.assertRaises(TrackWriteRefused):
            Track.objects.bulk_create([Track(filepath=str(self.dir / "b.flac"), filename="b.flac", title="B",
                                             artist=artist, cue_marker_revision=4)])


# ------------------------------------------------------------------ analysis

def _pcm(seconds=1, rate=8000, amplitude=3000, channels=1):
    frames = seconds * rate * channels
    return struct.pack(f"<{frames}h", *([amplitude] * frames))


class AnalyzerTests(_Fixture):
    CFG = (8000, 0.05, 200, -26.0, -40.0, 0.1)

    def analyze(self, track, **kwargs):
        wave_dir = self.dir / "waves"
        wave_dir.mkdir(exist_ok=True)
        row = (track.id, track.filepath, track.filename, track.duration_seconds, track.title, "A", "")
        with patch.object(analyze_tracks, "transcode_lossless_to_flac", side_effect=lambda t, fp, fn: (fp, fn)), \
                patch.object(analyze_tracks, "decode_audio_to_pcm", return_value=_pcm()), \
                patch.object(analyze_tracks, "decode_audio_to_pcm_stereo", return_value=_pcm(channels=2)), \
                patch.object(analyze_tracks, "emit_event"):
            return analyze_tracks.analyze_one_track(row, self.CFG, wave_dir, force=True, **kwargs), wave_dir

    def test_historical_markers_survive_analysis_and_the_cache_never_claims_unapplied_values(self):
        before = {name: getattr(self.legacy, name) for name in M}
        outcome, wave_dir = self.analyze(self.legacy)
        self.assertTrue(outcome)
        self.assertEqual(outcome.markers.preserved, {"cue_in_seconds": "legacy", "next_start_seconds": "legacy"})
        row = _row(self.legacy)
        self.assertEqual({name: row[name] for name in M}, before)
        payload = json.loads((wave_dir / f"{self.legacy.pk}.json").read_text())
        self.assertEqual((payload["cue_in_seconds"], payload["next_start"]), (1.0, 190.0))
        self.assertEqual(waveform_cache.validate(Track.objects.get(pk=self.legacy.pk)).status, waveform_cache.LEGACY)

    def test_a_new_track_gets_its_markers_and_its_request_is_cleared(self):
        fresh = _make_track(self.dir, duration=None)
        cue_markers.request_analysis(Track.objects.filter(pk=fresh.pk))
        outcome, _ = self.analyze(fresh)
        self.assertEqual(set(outcome.markers.changed), {"next_start_seconds"})   # Cue In stays 0.0 (detected 0)
        row = _row(fresh)
        self.assertIsNotNone(row["next_start_seconds"])
        self.assertEqual(row["cue_marker_state"]["next_start_seconds"]["source"], "analysis")
        self.assertIsNone(row["analysis_requested_at"])
        self.assertTrue(row["waveform_path"])

    def test_an_operator_edit_during_analysis_wins(self):
        fresh = _make_track(self.dir, duration=None)

        def decode_and_meanwhile_edit(*_args):
            cue_markers.operator_edit(fresh.pk, expected_revision=0, user=self.user, values={"next_start_seconds": 0.5})
            return _pcm()
        wave_dir = self.dir / "waves"
        wave_dir.mkdir(exist_ok=True)
        row = (fresh.id, fresh.filepath, fresh.filename, None, fresh.title, "A", "")
        with patch.object(analyze_tracks, "transcode_lossless_to_flac", side_effect=lambda t, fp, fn: (fp, fn)), \
                patch.object(analyze_tracks, "decode_audio_to_pcm", side_effect=decode_and_meanwhile_edit), \
                patch.object(analyze_tracks, "decode_audio_to_pcm_stereo", return_value=_pcm(channels=2)), \
                patch.object(analyze_tracks, "emit_event"):
            outcome = analyze_tracks.analyze_one_track(row, self.CFG, wave_dir, force=True)
        self.assertIn("stale", outcome.markers.conflict)
        self.assertEqual(_row(fresh)["next_start_seconds"], 0.5)

    def test_new_media_rebinds_the_calculated_markers(self):
        outcome, _ = self.analyze(self.legacy, media_replaced=True)
        self.assertEqual(set(outcome.markers.superseded), {"cue_in_seconds", "next_start_seconds"})
        self.assertLess(_row(self.legacy)["next_start_seconds"], 2.0)

    def test_the_timer_queue_is_requests_and_tracks_without_next_start_only(self):
        requested = _make_track(self.dir, next_start_seconds=5.0)
        cue_markers.request_analysis(Track.objects.filter(pk=requested.pk))
        unanalyzed = _make_track(self.dir)
        seen = []
        with patch.object(analyze_tracks, "analyze_one_track",
                          side_effect=lambda row, *a, **k: seen.append(row[0]) or True):
            analyze_tracks.Command().handle(force=False, limit=0)
        self.assertEqual(sorted(seen), sorted([requested.pk, unanalyzed.pk]))
        self.assertNotIn(self.legacy.pk, seen)


# ------------------------------------------------------------------ waveform cache

def _v2(track, *, duration=None, times=None, levels=None, identity=None, **extra):
    duration = duration if duration is not None else track.duration_seconds
    times = times if times is not None else [i * 0.5 for i in range(int(duration / 0.5))]
    levels = levels if levels is not None else [-60.0] * len(times)
    payload = {"schema": waveform_cache.SCHEMA, "schema_version": 2, "track_id": track.pk,
               "audio_identity": identity or {"sha256": "a" * 64},
               "analysis": {"sample_rate": 8000, "window_seconds": 0.5, "waveform_points": 4},
               "analysis_duration": duration, "envelope_times": times, "envelope_db": levels,
               "samples_left": [0, 10, 200, 255], "samples_right": [0, 12, 199, 250],
               "marker_revision": track.cue_marker_revision,
               "markers": {name: getattr(track, name) for name in M}}
    payload.update(extra)
    return payload


def _write(directory, name, payload):
    path = Path(directory) / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return path


class WaveformCacheTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.identity = patch.object(waveform_cache, "AUDIO_IDENTITY_PROVIDER", lambda _t: {"sha256": "a" * 64})

    def verdict(self, payload, track=None):
        track = track or Track.objects.get(pk=self.legacy.pk)
        return waveform_cache.validate(track, _write(self.dir, "cache.json", payload))

    def test_damaged_or_foreign_caches_are_refused(self):
        good = _v2(self.legacy)
        cases = {
            "truncated": (json.dumps(good)[:200], waveform_cache.MALFORMED),
            "not an object": ("[1, 2]", waveform_cache.MALFORMED),
            "NaN": (json.dumps(good).replace('"analysis_duration": 200.0', '"analysis_duration": NaN'),
                    waveform_cache.MALFORMED),
            "non-finite level": (_v2(self.legacy, levels=[-60.0] * 399 + [1e999]), waveform_cache.MALFORMED),
            "wrong track": (_v2(self.legacy) | {"track_id": self.legacy.pk + 1}, waveform_cache.WRONG_TRACK),
            "negative duration": (_v2(self.legacy) | {"analysis_duration": -1.0}, waveform_cache.MALFORMED),
            "shape": (_v2(self.legacy) | {"envelope_db": [-60.0]}, waveform_cache.MALFORMED),
            "unordered times": (_v2(self.legacy, times=[0.0, 1.0, 0.5], levels=[-60.0] * 3, duration=200.0),
                                waveform_cache.MALFORMED),
            "samples": (_v2(self.legacy) | {"samples_left": [0, 300, 1, 2]}, waveform_cache.MALFORMED),
            "duration mismatch": (_v2(self.legacy, duration=120.0), waveform_cache.INCONSISTENT),
            "future revision": (_v2(self.legacy) | {"marker_revision": 7}, waveform_cache.INCONSISTENT),
            "snapshot mismatch": (_v2(self.legacy) | {"markers": {name: 0.0 for name in M}},
                                  waveform_cache.INCONSISTENT),
        }
        with self.identity:
            for label, (payload, status) in cases.items():
                with self.subTest(label):
                    verdict = self.verdict(payload)
                    self.assertEqual(verdict.status, status, verdict.reason)
                    self.assertFalse(verdict.trusted)
        self.assertEqual(waveform_cache.validate(self.legacy, self.dir / "absent.json").status, waveform_cache.MISSING)

    def test_legacy_and_unidentified_caches_are_displayable_but_never_trusted(self):
        legacy = {key: value for key, value in _v2(self.legacy).items()
                  if key not in ("schema", "schema_version", "audio_identity", "analysis", "marker_revision",
                                 "markers")}
        verdict = self.verdict(legacy)
        self.assertEqual((verdict.status, verdict.displayable, verdict.trusted), (waveform_cache.LEGACY, True, False))
        # Version 2 is not enough: without an authoritative identity of the current audio (Package B)
        # nothing is trusted -- and Track.file_hash is never consulted.
        Track.objects.filter(pk=self.legacy.pk).update(file_hash="a" * 64)
        self.assertEqual(self.verdict(_v2(self.legacy)).status, waveform_cache.UNVERIFIED_AUDIO)
        with self.identity:
            self.assertEqual(self.verdict(_v2(self.legacy, identity={"sha256": "b" * 64})).status,
                             waveform_cache.UNVERIFIED_AUDIO)
            self.assertTrue(self.verdict(_v2(self.legacy)).trusted)

    def test_a_legitimately_short_clip_is_trusted(self):
        short = _make_track(self.dir, duration=0.3)
        with self.identity:
            verdict = self.verdict(_v2(short, duration=0.3, times=[0.0], levels=[-20.0]), track=short)
        self.assertTrue(verdict.trusted, verdict.reason)


# ------------------------------------------------------------------ routed views

@override_settings(SECURE_SSL_REDIRECT=False)
class EndpointTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.user)
        kind, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        self.category = Category.objects.create(code="CUETEST", name="Cue Test", kind=kind)
        Track.objects.filter(pk=self.legacy.pk).update(category=self.category)
        self.audio_hashes = {self.legacy.pk: _sha(self.legacy.filepath)}
        self.plays = PlayEvent.objects.count()

    def tearDown(self):
        self.assertEqual(_sha(self.legacy.filepath), self.audio_hashes[self.legacy.pk])   # audio never rewritten
        self.assertEqual(PlayEvent.objects.count(), self.plays)

    def patch(self, body):
        return self.client.patch(f"/api/tracks/{self.legacy.pk}/", data=json.dumps(body),
                                 content_type="application/json")

    def test_a_metadata_save_from_an_old_page_touches_no_marker_state(self):
        unchanged = {name: getattr(self.legacy, name) for name in M}
        for body in ({"title": "Only Metadata"}, {"title": "Old Page", **unchanged}):
            with self.subTest(body=sorted(body)):
                response = self.patch(body)
                self.assertEqual(response.status_code, 200, response.content)
                row = _row(self.legacy)
                self.assertEqual((row["cue_marker_revision"], row["cue_marker_state"]), (0, {}))
                self.assertEqual(response.json()["cue_marker_revision"], 0)

    def test_an_edit_with_the_current_revision_locks_only_the_edited_marker(self):
        response = self.patch({"title": "Edited", "hook_out_seconds": 75.0, "cue_marker_revision": 0})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["cue_marker_revision"], 1)
        row = _row(self.legacy)
        self.assertEqual(set(row["cue_marker_state"]), {"hook_out_seconds"})
        self.assertEqual(Track.objects.get(pk=self.legacy.pk).title, "Edited")

    def test_a_stale_or_revisionless_edit_is_refused_before_anything_changes(self):
        cue_markers.operator_edit(self.legacy.pk, expected_revision=0, user=self.user, values={"hook_out_seconds": 74.0})
        for body in ({"title": "Stale", "hook_out_seconds": 71.0, "cue_marker_revision": 0},
                     {"title": "Old", "hook_out_seconds": 71.0}):
            with self.subTest(body=body):
                response = self.patch(body)
                self.assertEqual(response.status_code, 409)
                self.assertTrue(response.json()["conflict"])
        track = Track.objects.get(pk=self.legacy.pk)
        self.assertEqual((track.title, track.hook_out_seconds, track.cue_marker_revision), ("Cue Test", 74.0, 1))

    def test_category_reset_requests_analysis_without_clearing_any_marker(self):
        response = self.client.post(f"/api/categories/{self.category.pk}/reset-analysis/")
        self.assertEqual(response.status_code, 200, response.content)
        row = _row(self.legacy)
        self.assertIsNotNone(row["analysis_requested_at"])
        self.assertEqual((row["next_start_seconds"], row["cue_marker_revision"]), (190.0, 0))

    def test_repick_abstains_on_any_untrusted_cache(self):
        _write(self.dir, "old.json", _v2(Track.objects.get(pk=self.legacy.pk)))      # v2 but unidentifiable audio
        response = self.client.post(f"/api/tracks/{self.legacy.pk}/repick-cue-points/")
        self.assertEqual(response.status_code, 409)
        self.assertIn("unchanged", response.json()["error"])
        row = _row(self.legacy)
        self.assertEqual((row["next_start_seconds"], row["cue_marker_revision"], row["analysis_requested_at"]),
                         (190.0, 0, None))

    def test_a_mixed_category_repicks_trusted_members_and_skips_the_rest_untouched(self):
        cue_markers.operator_edit(self.legacy.pk, expected_revision=0, user=self.user, unlock=["cue_in_seconds"])
        trusted = Track.objects.get(pk=self.legacy.pk)
        levels = [-90.0] * 8 + [-10.0] * 380 + [-90.0] * 12            # quiet, programme, quiet tail
        _write(self.dir, "old.json", _v2(trusted, levels=levels))
        legacy_cache = _make_track(self.dir, category=self.category, next_start_seconds=150.0)
        Track.objects.filter(pk=legacy_cache.pk).update(
            waveform_path=str(_write(self.dir, "legacy.json", {k: v for k, v in _v2(legacy_cache).items()
                                                               if k not in ("schema", "schema_version")})))
        missing = _make_track(self.dir, category=self.category, next_start_seconds=140.0)
        Track.objects.filter(pk=missing.pk).update(waveform_path=str(self.dir / "gone.json"))
        broken = _make_track(self.dir, category=self.category, next_start_seconds=130.0)
        Track.objects.filter(pk=broken.pk).update(waveform_path=str(_write(self.dir, "broken.json", "{\"track_id")))
        others = {t.pk: _row(t) for t in (legacy_cache, missing, broken)}
        with patch.object(waveform_cache, "AUDIO_IDENTITY_PROVIDER", lambda _t: {"sha256": "a" * 64}):
            response = self.client.post(f"/api/categories/{self.category.pk}/repick-cue-points/")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual((body["repicked"], body["skipped"], body["queued_full_analyze"]), (1, 3, 0))
        self.assertEqual(body["skipped_by_reason"], {"legacy": [legacy_cache.pk], "missing": [missing.pk],
                                                     "malformed": [broken.pk]})
        self.assertEqual(body["preserved_by_marker"], {"next_start_seconds": [self.legacy.pk]})
        row = _row(self.legacy)
        self.assertEqual(row["cue_in_seconds"], 4.0)                    # re-picked: the first loud window
        self.assertEqual(row["next_start_seconds"], 190.0)             # historical: preserved
        for pk, before in others.items():
            self.assertEqual(_row(Track.objects.get(pk=pk)), before)   # skipped: nothing touched or queued

    def test_reanalyze_preserves_protected_markers_unless_an_override_is_confirmed(self):
        with patch.object(analyze_tracks, "transcode_lossless_to_flac", side_effect=lambda t, fp, fn: (fp, fn)), \
                patch.object(analyze_tracks, "decode_audio_to_pcm", return_value=_pcm()), \
                patch.object(analyze_tracks, "decode_audio_to_pcm_stereo", return_value=_pcm(channels=2)), \
                patch.object(analyze_tracks, "get_waveforms_dir", return_value=self.dir), \
                patch.object(analyze_tracks, "emit_event"):
            plain = self.client.post(f"/api/tracks/{self.legacy.pk}/reanalyze/")
            self.assertEqual(plain.status_code, 200, plain.content)
            self.assertEqual(plain.json()["cue_markers"]["preserved"],
                             {"cue_in_seconds": "legacy", "next_start_seconds": "legacy"})
            self.assertEqual(_row(self.legacy)["next_start_seconds"], 190.0)
            unconfirmed = self.client.post(f"/api/tracks/{self.legacy.pk}/reanalyze/",
                                           data=json.dumps({"override": ["next_start_seconds"]}),
                                           content_type="application/json")
            self.assertEqual(unconfirmed.status_code, 409)
            confirmed = self.client.post(f"/api/tracks/{self.legacy.pk}/reanalyze/",
                                         data=json.dumps({"override": ["cue_in_seconds"], "confirm": True}),
                                         content_type="application/json")
        self.assertEqual(confirmed.status_code, 200, confirmed.content)
        self.assertEqual(confirmed.json()["cue_markers"]["overridden"], ["cue_in_seconds"])
        row = _row(self.legacy)
        self.assertEqual((row["cue_in_seconds"], row["next_start_seconds"]), (0.0, 190.0))


# ------------------------------------------------------------------ admin and source

class AdminAndSourceTests(_Fixture):
    def test_the_admin_form_cannot_edit_markers(self):
        request = RequestFactory().get("/admin/")
        request.user = self.user
        form = TrackAdmin(Track, AdminSite()).get_form(request, self.legacy)
        for name in M + ("cue_marker_state", "cue_marker_revision", "analysis_requested_at"):
            self.assertNotIn(name, form.base_fields, name)

    def test_no_writer_clears_next_start_as_an_analysis_request(self):
        for path in ("library/views.py", "library/management/commands/prep_mitd_show.py",
                     "isadoraair/announcements/renderer.py", "library/management/commands/analyze_tracks.py"):
            source = (REPO / path).read_text()
            with self.subTest(path):
                self.assertNotIn("update(next_start_seconds=None)", source)
                self.assertNotIn("next_start_seconds=None)", source)
                self.assertNotRegex(source, r"\.update\([^)]*(cue_in|next_start)_seconds\s*=")
