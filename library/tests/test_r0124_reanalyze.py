"""r0124: Reanalyze Track is the one waveform action. It regenerates the waveform with the existing
analyzer, recalculates only unprotected Cue In / Next Start through the cue-marker authority, keeps
every protected marker and approval, and reports what actually happened (verified, unverified,
not regenerated; cue points applied, kept or refused) -- including the KOGR Track 35884 case, where
the analyzer decodes the file but the stricter decoded-audio identity cannot be established.

Real audio is generated into per-test temporary directories; Track 35884's file is never used. No
station media, waveform, database or service is touched."""
import hashlib
import json
from datetime import datetime, timedelta, timezone as datetime_timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.test import Client

from library.management.commands import analyze_tracks
from library.models import Track
from library.services import audio_identity, cue_markers, waveform_cache
from library.tests.test_waveform_integrity_package_b import _audio, _Base, _row

CUE_IN, NEXT = "cue_in_seconds", "next_start_seconds"
OTHER_SIX = ("cue_out_seconds", "intro_until_seconds", "sweep_start_seconds", "outro_starts_seconds",
             "hook_in_seconds", "hook_out_seconds")
INVALID_SYNC = "the audio does not decode cleanly: [flac @ 0x5555] invalid sync code"


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class _Reanalyze(_Base):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.user)

    def post(self, track, **body):
        response = self.client.post(f"/api/tracks/{track.pk}/reanalyze/", data=json.dumps(body),
                                    content_type="application/json")
        return response.status_code, response.json()

    def lock(self, track, **values):
        result = cue_markers.operator_edit(track.pk, expected_revision=_row(track.pk)["cue_marker_revision"],
                                           user=self.user, values=values)
        self.assertTrue(result.ok, result.error or result.conflict)

    def unverifiable(self):
        """The production condition: the analyzer's own decode works; the strict identity does not."""
        return patch.object(audio_identity, "compute_or_none", return_value=(None, INVALID_SYNC))


class Track35884Tests(_Reanalyze):
    def test_a_file_the_analyzer_reads_but_cannot_identify_regenerates_an_unverified_waveform(self):
        track = self.track()
        self.lock(track, **{CUE_IN: 0.4, NEXT: 2.2})                          # both locked by the operator
        sha, state = _sha(track.filepath), _row(track.pk)["cue_marker_state"]
        with self.unverifiable():
            status, body = self.post(track)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["outcome"], "unverified")
        waveform = body["waveform"]
        self.assertEqual((waveform["regenerated"], waveform["audio_verified"], waveform["status"],
                          waveform["displayable"], waveform["trusted"], waveform["audio_problem"]),
                         (True, False, "unverified_audio", True, False, INVALID_SYNC))
        self.assertEqual(body["cue_markers"]["preserved"], {CUE_IN: "locked", NEXT: "locked"})
        row = _row(track.pk)
        self.assertEqual((row[CUE_IN], row[NEXT], row["cue_marker_state"]), (0.4, 2.2, state))   # untouched
        self.assertEqual(_sha(track.filepath), sha)                          # source bytes unchanged
        cache = json.loads(self.cache(track).read_text())
        self.assertIsNone(cache["audio_identity"])                           # no bogus audio revision
        self.assertTrue(cache["samples_left"])                               # a real, displayable waveform
        state_api = self.client.get(f"/api/tracks/{track.pk}/cue-markers/").json()
        self.assertEqual((state_api["audio_verified"], state_api["repick_available"]), (False, False))
        self.assertIn("could not be identified", state_api["waveform"]["explanation"])
        self.assertNotIn("Reanalyze Track to re-verify", state_api["waveform"]["explanation"])

    def test_no_audio_revision_is_recorded_for_markers_calculated_from_unidentified_audio(self):
        track = self.track(analyzed=False)                                    # nothing protected yet
        with self.unverifiable():
            status, body = self.post(track)
        self.assertEqual((status, body["outcome"]), (200, "unverified"))
        entry = _row(track.pk)["cue_marker_state"][NEXT]
        self.assertEqual((entry["source"], entry["audio_revision"]), ("analysis", None))


class MarkerProtectionTests(_Reanalyze):
    def test_a_both_calculated_markers_locked(self):
        track = self.track()
        self.lock(track, **{CUE_IN: 0.4, NEXT: 2.2})
        self.lock(track, **{CUE_IN: 0.3})                                     # some approval history too
        before = _row(track.pk)
        status, body = self.post(track)
        self.assertEqual((status, body["outcome"]), (200, "verified"))
        after = _row(track.pk)
        self.assertEqual((after[CUE_IN], after[NEXT]), (0.3, 2.2))
        self.assertEqual(after["cue_marker_state"], before["cue_marker_state"])   # protection and history intact
        self.assertEqual(after["cue_marker_revision"], before["cue_marker_revision"])
        self.assertEqual(waveform_cache.validate(Track.objects.get(pk=track.pk)).status, waveform_cache.TRUSTED)

    def test_b_one_marker_locked_either_way(self):
        for locked, free in ((CUE_IN, NEXT), (NEXT, CUE_IN)):
            with self.subTest(locked=locked):
                track = self.track(name=f"{locked}.flac")
                cue_markers.operator_edit(track.pk, expected_revision=0, user=self.user,
                                          values={locked: 0.2 if locked == CUE_IN else 2.9}, unlock=[free]
                                          if free == CUE_IN else [])
                before = _row(track.pk)
                status, body = self.post(track)
                self.assertEqual((status, body["outcome"]), (200, "verified"))
                after = _row(track.pk)
                self.assertEqual(after[locked], before[locked])
                self.assertEqual(after["cue_marker_state"][locked], before["cue_marker_state"][locked])
                self.assertIn(free, body["cue_markers"]["changed"])           # recalculated
                entry = after["cue_marker_state"][free]
                self.assertEqual((entry["protection"], entry["source"]), ("unlocked", "analysis"))
                self.assertEqual(entry["audio_revision"], audio_identity.compute(track.filepath)["sha256"])
                self.assertEqual(after["cue_marker_revision"], before["cue_marker_revision"] + 1)
                self.assertAlmostEqual(after[CUE_IN] if free == CUE_IN else after[NEXT],
                                       0.5 if free == CUE_IN else 2.5, delta=0.1)

    def test_c_nothing_locked_detects_both_as_before(self):
        track = self.track(analyzed=False)
        status, body = self.post(track)
        self.assertEqual((status, body["outcome"]), (200, "verified"))
        row = _row(track.pk)
        self.assertAlmostEqual(row[CUE_IN], 0.5, delta=0.1)
        self.assertAlmostEqual(row[NEXT], 2.5, delta=0.1)

    def test_d_the_other_six_markers_are_never_touched(self):
        track = self.track()
        self.lock(track, cue_out_seconds=2.95, intro_until_seconds=0.9, sweep_start_seconds=1.0,
                  outro_starts_seconds=2.4, hook_in_seconds=1.2, hook_out_seconds=1.8)
        before = _row(track.pk)
        self.post(track)
        after = _row(track.pk)
        for name in OTHER_SIX:
            self.assertEqual((after[name], after["cue_marker_state"][name]), (before[name], before["cue_marker_state"][name]))

    def test_e_an_operator_edit_during_analysis_wins(self):
        track = self.track(analyzed=False)
        real = analyze_tracks.decode_audio_to_pcm

        def decode_and_meanwhile_edit(path, rate):
            pcm = real(path, rate)
            cue_markers.operator_edit(track.pk, expected_revision=0, user=self.user, values={NEXT: 1.5})
            return pcm
        with patch.object(analyze_tracks, "decode_audio_to_pcm", side_effect=decode_and_meanwhile_edit):
            status, body = self.post(track)
        self.assertEqual((status, body["outcome"]), (200, "verified"))      # the waveform still regenerated
        self.assertIn("stale", body["cue_markers"]["conflict"])               # ... the calculation was refused
        self.assertEqual(_row(track.pk)[NEXT], 1.5)

    def test_f_historical_values_stay_protected(self):
        track = self.track()
        with cue_markers._authorized():
            Track.objects.filter(pk=track.pk).update(next_start_seconds=2.2)  # pre-r0123 value, no state
        status, body = self.post(track)
        self.assertEqual(body["cue_markers"]["preserved"], {CUE_IN: "legacy", NEXT: "legacy"})
        row = _row(track.pk)
        self.assertEqual((row[CUE_IN], row[NEXT], row["cue_marker_state"]), (0.0, 2.2, {}))

    def test_g_a_cache_publication_failure_is_reported_and_keeps_the_previous_cache(self):
        track = self.track()
        self.lock(track, **{CUE_IN: 0.4})
        old, sha = self.cache(track).read_bytes(), _sha(track.filepath)
        with patch.object(waveform_cache, "promote",
                          side_effect=waveform_cache.PromotionRefused("superseded", "injected promotion failure")):
            status, body = self.post(track)
        self.assertEqual(status, 200)
        self.assertEqual((body["outcome"], body["waveform"]["regenerated"]), ("not_regenerated", False))
        self.assertIn("injected promotion failure", body["waveform"]["error"])
        self.assertEqual((self.cache(track).read_bytes(), _sha(track.filepath)), (old, sha))
        self.assertEqual(_row(track.pk)[CUE_IN], 0.4)
        self.assertEqual(body["cue_markers"]["preserved"], {CUE_IN: "locked"})

    def test_g2_a_failed_stereo_decode_never_replaces_or_claims_the_waveform(self):
        track = self.track()
        self.lock(track, **{CUE_IN: 0.4, NEXT: 2.2})
        old, source_sha = self.cache(track).read_bytes(), _sha(track.filepath)
        before = _row(track.pk)
        with patch.object(analyze_tracks, "decode_audio_to_pcm_stereo", return_value=b""):
            status, body = self.post(track)
        self.assertEqual(status, 200, body)
        self.assertEqual((body["outcome"], body["waveform"]["regenerated"]),
                         ("not_regenerated", False))
        self.assertIn("displayable waveform", body["waveform"]["error"])
        self.assertEqual(self.cache(track).read_bytes(), old)
        self.assertEqual(_sha(track.filepath), source_sha)
        after = _row(track.pk)
        self.assertEqual((after[CUE_IN], after[NEXT], after["cue_marker_state"]),
                         (before[CUE_IN], before[NEXT], before["cue_marker_state"]))
        self.assertNotEqual(waveform_cache.validate(Track.objects.get(pk=track.pk)).status,
                            waveform_cache.INCOMPLETE)

    def test_g3_operator_error_is_single_line_bounded_and_redacts_absolute_paths(self):
        track = self.track()
        reason = "publication failed at /srv/private/Station Audio/O'Brien/1.json\n" + "x" * 500
        with patch.object(waveform_cache, "promote",
                          side_effect=waveform_cache.PromotionRefused("failed", reason)):
            status, body = self.post(track)
        error = body["waveform"]["error"]
        self.assertEqual((status, body["outcome"]), (200, "not_regenerated"))
        self.assertNotIn("/srv/private", error)
        self.assertNotIn("Station Audio", error)
        self.assertNotIn("O'Brien", error)
        self.assertNotIn("\n", error)
        self.assertLessEqual(len(error), 300)

    def test_g4_installed_but_nondisplayable_fallback_does_not_claim_the_old_cache_was_kept(self):
        track = self.track()
        outcome = SimpleNamespace(
            waveform_installed=True, waveform_error="", audio_problem="", markers=None,
        )
        verdict = SimpleNamespace(displayable=False, status="incomplete", trusted=False)
        with patch.object(analyze_tracks, "analyze_one_track", return_value=outcome), \
                patch.object(waveform_cache, "validate", return_value=verdict):
            status, body = self.post(track)
        self.assertEqual((status, body["outcome"]), (200, "not_regenerated"))
        self.assertEqual(body["waveform"]["error"], "the installed waveform is not displayable")
        self.assertNotIn("previous waveform", body["waveform"]["error"])

    def test_h_repeated_reanalysis_is_idempotent_for_protection_and_history(self):
        track = self.track()
        self.lock(track, **{CUE_IN: 0.4})
        self.lock(track, **{CUE_IN: 0.45})
        self.post(track)
        first = _row(track.pk)
        for _ in range(2):
            status, body = self.post(track)
            self.assertEqual((status, body["outcome"]), (200, "verified"))
        again = _row(track.pk)
        self.assertEqual((again["cue_marker_state"], again["cue_marker_revision"]),
                         (first["cue_marker_state"], first["cue_marker_revision"]))
        self.assertEqual(len(again["cue_marker_state"][CUE_IN]["approval_history"]), 1)
        self.assertEqual(again["cue_marker_state"][CUE_IN]["protection"], "locked")

    def test_an_unreadable_file_is_reported_as_not_regenerated(self):
        track = self.track()
        old = self.cache(track).read_bytes()
        Path(track.filepath).write_bytes(b"not audio " * 200)
        status, body = self.post(track)
        self.assertEqual((status, body["outcome"]), (400, "not_regenerated"))
        self.assertEqual(self.cache(track).read_bytes(), old)


class PreservationTests(_Reanalyze):
    def test_the_analyzer_ffmpeg_commands_are_exactly_r0122s(self):
        path = Path("/x/song.flac")
        for channels, decoder in ((1, analyze_tracks.decode_audio_to_pcm), (2, analyze_tracks.decode_audio_to_pcm_stereo)):
            expected = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-ac", str(channels),
                        "-ar", "4410", "-f", "s16le", "-"]
            with self.subTest(channels=channels):
                self.assertEqual(analyze_tracks.pcm_command(path, 4410, channels), expected)
                with patch.object(analyze_tracks.subprocess, "run") as run:
                    run.return_value.stdout = b""
                    decoder(path, 4410)
                self.assertEqual(run.call_args.args[0], expected)
                self.assertEqual(run.call_args.kwargs["check"], True)          # same failure handling

    def test_the_retired_service_and_command_are_gone(self):
        import importlib.util
        self.assertIsNone(importlib.util.find_spec("library.services.waveform_generation"))
        self.assertIsNone(importlib.util.find_spec("library.management.commands.refresh_waveform"))
        for helper in ("try_operation_lock", "create_request_lock", "request_lock_held"):
            self.assertFalse(hasattr(waveform_cache, helper), helper)
        for shared in ("promote", "install_file", "restore_file", "remove", "track_lock", "validate"):
            self.assertTrue(hasattr(waveform_cache, shared), shared)        # shared cache machinery kept

    def test_the_page_refuses_to_reanalyze_over_unsaved_cue_point_edits(self):
        source = (Path(__file__).resolve().parents[2] / "library/templates/library/track_detail.html").read_text()
        handler = source[source.index("async function reanalyzeTrack"):source.index("// --- Fast cue-point repick")]
        self.assertIn("editedCueMarkers()", handler)
        self.assertIn("before reanalyzing", handler)

    def test_the_page_treats_queued_as_pending_and_displays_safe_failure_detail(self):
        source = (Path(__file__).resolve().parents[2] / "library/templates/library/track_detail.html").read_text()
        handler = source[source.index("async function reanalyzeTrack"):source.index("// --- Fast cue-point repick")]
        self.assertIn("resp.status === 202 && data.outcome === 'queued'", handler)
        self.assertIn("background analyzer will attempt reanalysis", source)
        self.assertNotIn("background analyzer will regenerate", source)
        queued = handler.index("data.outcome === 'queued'")
        redraw = handler.index("loadWaveform()")
        self.assertLess(queued, redraw)
        self.assertIn("data.waveform.error", handler)
        self.assertIn("slice(0, 300)", handler)
        self.assertIn("textContent", source[source.index("function reanalyzeStatus"):handler.index("async function")
                                            + source.index("async function reanalyzeTrack")])


class LongFormQueueTests(_Reanalyze):
    LONG_SECONDS = 15 * 60 + 1

    def long_track(self, **kwargs):
        track = self.track(**kwargs)
        Track.objects.filter(pk=track.pk).update(duration_seconds=self.LONG_SECONDS)
        return Track.objects.get(pk=track.pk)

    def test_only_positive_finite_durations_through_900_seconds_run_synchronously(self):
        for label, duration in (("240", 240), ("900", 900)):
            with self.subTest(duration=duration):
                track = self.track(name=f"sync-{label}.flac")
                Track.objects.filter(pk=track.pk).update(duration_seconds=duration)
                track.refresh_from_db()
                with patch.object(cue_markers, "request_analysis", wraps=cue_markers.request_analysis) as queued:
                    status, body = self.post(track)
                self.assertEqual((status, body["outcome"]), (200, "verified"))
                queued.assert_not_called()

    def test_long_track_queues_promptly_without_decode_and_changes_no_media_or_markers(self):
        track = self.long_track()
        before, old_cache, source_sha = _row(track.pk), self.cache(track).read_bytes(), _sha(track.filepath)
        with patch.object(analyze_tracks, "analyze_one_track", side_effect=AssertionError("decoded in request")):
            status, body = self.post(track)
        self.assertEqual((status, body["outcome"], body["queued"]), (202, "queued", True))
        self.assertIn("background analyzer", body["message"])
        after = _row(track.pk)
        self.assertIsNotNone(after["analysis_requested_at"])
        for name in (*cue_markers.MARKERS, "cue_marker_state", "cue_marker_revision"):
            self.assertEqual(after[name], before[name], name)
        self.assertEqual((self.cache(track).read_bytes(), _sha(track.filepath)), (old_cache, source_sha))

    def test_unbounded_or_invalid_stored_durations_queue_without_touching_media_or_markers(self):
        cases = (("901", 901), ("missing", None), ("zero", 0), ("negative", -1),
                 ("nan", float("nan")), ("infinity", float("inf")))
        for label, duration in cases:
            with self.subTest(duration=label):
                track = self.track(name=f"queued-{label}.flac")
                Track.objects.filter(pk=track.pk).update(duration_seconds=duration)
                track.refresh_from_db()
                before = _row(track.pk)
                old_cache = self.cache(track).read_bytes()
                source_sha = _sha(track.filepath)
                with patch.object(analyze_tracks, "analyze_one_track",
                                  side_effect=AssertionError("analysis ran in request")), \
                        patch.object(analyze_tracks, "decode_audio_to_pcm") as mono_decode, \
                        patch.object(analyze_tracks, "decode_audio_to_pcm_stereo") as stereo_decode:
                    status, body = self.post(track)
                self.assertEqual((status, body["outcome"], body["queued"]), (202, "queued", True))
                self.assertIn("will attempt reanalysis", body["message"])
                self.assertNotIn("will regenerate", body["message"])
                mono_decode.assert_not_called()
                stereo_decode.assert_not_called()
                after = _row(track.pk)
                self.assertIsNotNone(after["analysis_requested_at"])
                for name in (*cue_markers.MARKERS, "cue_marker_state", "cue_marker_revision"):
                    self.assertEqual(after[name], before[name], name)
                self.assertEqual((self.cache(track).read_bytes(), _sha(track.filepath)),
                                 (old_cache, source_sha))

    def test_repeated_click_refreshes_the_request_generation_without_claiming_success(self):
        track = self.long_track()
        one = self.post(track)
        first = _row(track.pk)["analysis_requested_at"]
        # Give the second click a deliberately older stored generation so
        # the assertion does not depend on wall-clock or database precision.
        Track.objects.filter(pk=track.pk).update(
            analysis_requested_at=first - timedelta(seconds=1),
        )
        two = self.post(track)
        second = _row(track.pk)["analysis_requested_at"]
        self.assertEqual((one[0], one[1]["outcome"], two[0], two[1]["outcome"]),
                         (202, "queued", 202, "queued"))
        self.assertGreater(second, first)

    def test_queued_standard_analysis_regenerates_and_preserves_locked_markers(self):
        track = self.long_track()
        self.lock(track, **{NEXT: 2.2})
        released = cue_markers.operator_edit(
            track.pk, expected_revision=_row(track.pk)["cue_marker_revision"], user=self.user, unlock=[CUE_IN],
        )
        self.assertTrue(released.ok, released.error)
        old_cache = self.cache(track).read_bytes()
        self.assertEqual(self.post(track)[0], 202)
        call_command("analyze_tracks", stdout=StringIO())
        row = _row(track.pk)
        self.assertIsNone(row["analysis_requested_at"])
        self.assertEqual(row[NEXT], 2.2)
        self.assertEqual(row["cue_marker_state"][NEXT]["protection"], cue_markers.LOCKED)
        self.assertEqual(row["cue_marker_state"][CUE_IN]["source"], cue_markers.SOURCE_ANALYSIS)
        self.assertNotEqual(self.cache(track).read_bytes(), old_cache)
        self.assertTrue(waveform_cache.validate(Track.objects.get(pk=track.pk)).trusted)

    def test_a_newer_request_survives_an_older_background_analysis(self):
        track = self.long_track()
        first = datetime(2026, 10, 31, 12, 0, tzinfo=datetime_timezone.utc)
        second = first + timedelta(seconds=1)
        with patch.object(cue_markers.timezone, "now", return_value=first):
            self.assertEqual(self.post(track)[0], 202)
        real_decode = analyze_tracks.decode_audio_to_pcm
        requested = False

        def decode_and_request_again(path, rate):
            nonlocal requested
            if not requested:
                requested = True
                with patch.object(cue_markers.timezone, "now", return_value=second):
                    cue_markers.request_analysis(Track.objects.filter(pk=track.pk))
            return real_decode(path, rate)

        with patch.object(analyze_tracks, "decode_audio_to_pcm", side_effect=decode_and_request_again):
            call_command("analyze_tracks", stdout=StringIO())
        self.assertEqual(_row(track.pk)["analysis_requested_at"], second)

    def test_terminal_background_decode_failure_keeps_cache_and_does_not_retry_forever(self):
        track = self.long_track()
        before, old_cache = _row(track.pk), self.cache(track).read_bytes()
        status, body = self.post(track)
        self.assertEqual((status, body["outcome"]), (202, "queued"))
        with patch.object(analyze_tracks, "decode_audio_to_pcm", return_value=b""):
            call_command("analyze_tracks", stdout=StringIO())
        after = _row(track.pk)
        self.assertIsNone(after["analysis_requested_at"])
        self.assertEqual(self.cache(track).read_bytes(), old_cache)
        for name in (*cue_markers.MARKERS, "cue_marker_state", "cue_marker_revision"):
            self.assertEqual(after[name], before[name], name)

    def test_queued_override_is_rejected_instead_of_silently_ignored(self):
        track = self.long_track()
        status, body = self.post(track, override=[NEXT], confirm=True)
        self.assertEqual(status, 400)
        self.assertIn("cannot override", body["error"])
        self.assertIsNone(_row(track.pk)["analysis_requested_at"])

    def test_background_service_keeps_the_established_unlimited_oneshot_configuration(self):
        unit = (Path(__file__).resolve().parents[2] / "deploy/isadoraair-analyze.service").read_text()
        self.assertEqual(unit, """[Unit]
Description=IsadoraAir background track analysis (waveform + cue points for any unanalyzed track)
After=network.target postgresql.service

[Service]
Type=oneshot
User=@@ISA_USER@@
Group=@@ISA_USER@@
WorkingDirectory=@@ISA_ROOT@@
ExecStart=@@ISA_ROOT@@/venv/bin/python @@ISA_ROOT@@/manage.py analyze_tracks
EnvironmentFile=@@ISA_ROOT@@/.env
""")
        self.assertNotIn("TimeoutStartSec", unit)
