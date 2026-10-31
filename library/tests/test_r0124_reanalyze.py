"""r0124: Reanalyze Track is the one waveform action. It regenerates the waveform with the existing
analyzer, recalculates only unprotected Cue In / Next Start through the cue-marker authority, keeps
every protected marker and approval, and reports what actually happened (verified, unverified,
not regenerated; cue points applied, kept or refused) -- including the KOGR Track 35884 case, where
the analyzer decodes the file but the stricter decoded-audio identity cannot be established.

Real audio is generated into per-test temporary directories; Track 35884's file is never used. No
station media, waveform, database or service is touched."""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

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
