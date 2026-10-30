"""check_waveform_integrity: a read-only diagnostic for suspect waveform JSON.

The deterministic fixtures reproduce the 2026-10-29 incident signature -- a 36-point,
all-255, flat -20.77 dB envelope spanning 1.77 s, produced by mocked 8 kHz/3000
amplitude test audio -- and prove a genuine 1.9 s clip is not accused.
"""
import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from library.management.commands import analyze_tracks
from library.models import Artist, Track

INCIDENT_SPAN = 1.7709750566893423
INCIDENT_DB = -20.76657360480111


def incident_payload(track_id):
    """Exactly what mocked test audio wrote over real waveforms on 2026-10-29."""
    times = [0.024943310657596373 + 0.05 * i for i in range(36)]
    return {"track_id": track_id, "samples_left": [255] * 36, "samples_right": [255] * 36,
            "next_start": INCIDENT_SPAN, "cue_in_seconds": 0.0, "analysis_duration": INCIDENT_SPAN,
            "envelope_times": times, "envelope_db": [INCIDENT_DB] * 36}


def genuine_payload(track_id, span, points=40, flat=False):
    left = [128] * points if flat else [(i * 37) % 200 + 20 for i in range(points)]
    right = list(left)
    env = [-20.0] * points if flat else [-30.0 + (i % 9) for i in range(points)]
    return {"track_id": track_id, "samples_left": left, "samples_right": right, "next_start": span,
            "cue_in_seconds": 0.1, "analysis_duration": span,
            "envelope_times": [0.05 * i for i in range(points)], "envelope_db": env}


class WaveformIntegrityCheckTests(TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.artist = Artist.get_or_create_ci("Fixture Artist")[0]

    def track(self, title, duration):
        return Track.objects.create(filepath=f"/nonexistent/{title}.flac", filename=f"{title}.flac", title=title,
                                    artist=self.artist, duration_seconds=duration, related_artists="")

    def put(self, track_id, payload=None, raw=None):
        path = self.dir / f"{track_id}.json"
        path.write_bytes(raw if raw is not None else json.dumps(payload).encode())
        return path

    def run_check(self, *args):
        out = StringIO()
        call_command("check_waveform_integrity", f"--waveforms-dir={self.dir}", "--json", *args, stdout=out)
        data = json.loads(out.getvalue())
        return data["summary"], {row["track_id"]: row for row in data["findings"]}

    def test_flags_the_incident_signature_on_a_full_length_track(self):
        track = self.track("full length", 327.13)
        self.put(track.id, incident_payload(track.id))
        summary, found = self.run_check()
        self.assertEqual(summary["suspect"], 1)
        self.assertEqual(found[track.id]["severity"], "suspect")
        self.assertIn("SYNTHETIC_FLAT_SHORT", found[track.id]["findings"])
        self.assertIn("DURATION_MISMATCH", found[track.id]["findings"])

    def test_does_not_accuse_a_genuine_short_clip(self):
        clip = self.track("short clip", 1.92)                      # a real 1.9 s clip, analysed span 1.77 s
        self.put(clip.id, genuine_payload(clip.id, 1.77, points=36))
        flat_clip = self.track("flat tone clip", 1.9209)           # even a flat tone is fine when the span agrees
        self.put(flat_clip.id, genuine_payload(flat_clip.id, 1.77, points=36, flat=True))
        summary, found = self.run_check()
        self.assertEqual(summary["suspect"], 0)
        self.assertNotIn(clip.id, found)
        self.assertEqual(found[flat_clip.id]["findings"], ["FLAT_BUT_CONSISTENT"])
        self.assertEqual(found[flat_clip.id]["severity"], "note")

    def test_the_incident_signature_on_an_unknown_duration_is_not_a_suspect(self):
        # No Track row: an orphan is a note, never an accusation of corruption.
        self.put(987654, incident_payload(987654))
        summary, found = self.run_check()
        self.assertEqual(summary["suspect"], 0)
        self.assertEqual(found[987654]["findings"], ["ORPHAN_FILE"])

    def test_flags_other_corruption_classes(self):
        by_name = {}
        for name, duration in (("wrong id", 100.0), ("not json", 100.0), ("nan", 100.0), ("lengths", 100.0),
                               ("missing", 100.0), ("array", 100.0), ("span", 100.0), ("good", 100.0)):
            by_name[name] = self.track(name, duration)
        self.put(by_name["wrong id"].id, genuine_payload(by_name["wrong id"].id + 1, 100.0, points=1000))
        self.put(by_name["not json"].id, raw=b'{"track_id": ')
        self.put(by_name["nan"].id,
                 raw=b'{"track_id": %d, "samples_left": [NaN], "samples_right": [1], "analysis_duration": 100.0}'
                 % by_name["nan"].id)
        bad_lengths = genuine_payload(by_name["lengths"].id, 100.0)
        bad_lengths["samples_right"] = bad_lengths["samples_right"][:-3]
        self.put(by_name["lengths"].id, bad_lengths)
        self.put(by_name["missing"].id, {"track_id": by_name["missing"].id, "samples_left": [], "samples_right": []})
        self.put(by_name["array"].id, raw=b"[1, 2, 3]")
        self.put(by_name["span"].id, genuine_payload(by_name["span"].id, 20.0))     # 20 s span for a 100 s track
        self.put(by_name["good"].id, genuine_payload(by_name["good"].id, 100.0, points=1000))
        summary, found = self.run_check()
        codes = lambda name: found[by_name[name].id]["findings"]                       # noqa: E731
        self.assertIn("ID_MISMATCH", codes("wrong id"))
        self.assertEqual(codes("not json"), ["INVALID_JSON"])
        self.assertEqual(codes("nan"), ["NON_FINITE"])
        self.assertIn("LENGTH_MISMATCH", codes("lengths"))
        self.assertIn("MISSING_FIELDS", codes("missing"))
        self.assertEqual(codes("array"), ["NOT_AN_OBJECT"])
        self.assertEqual(codes("span"), ["DURATION_MISMATCH"])
        self.assertNotIn(by_name["good"].id, found)
        self.assertEqual(summary["suspect"], 7)

    def test_is_strictly_read_only(self):
        good, bad = self.track("good", 200.0), self.track("bad", 200.0)
        self.put(good.id, genuine_payload(good.id, 200.0, points=1000))
        self.put(bad.id, incident_payload(bad.id))
        before_files = {p.name: (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes()) for p in self.dir.iterdir()}
        before_db = list(Track.objects.order_by("id").values())
        with mock.patch.object(analyze_tracks, "analyze_one_track", side_effect=AssertionError("analysis called")), \
             mock.patch.object(analyze_tracks, "repick_cue_points_from_json", side_effect=AssertionError("repick called")), \
             mock.patch.object(analyze_tracks, "decode_audio_to_pcm", side_effect=AssertionError("decoded audio")), \
             mock.patch("os.remove", side_effect=AssertionError("removed")), \
             mock.patch("os.rename", side_effect=AssertionError("renamed")), \
             mock.patch("pathlib.Path.write_text", side_effect=AssertionError("wrote")), \
             mock.patch("pathlib.Path.write_bytes", side_effect=AssertionError("wrote")):
            self.run_check()
        after_files = {p.name: (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes()) for p in self.dir.iterdir()}
        self.assertEqual(before_files, after_files)
        self.assertEqual(before_db, list(Track.objects.order_by("id").values()))

    def test_scans_only_numeric_json_names_and_honours_ids_and_limit(self):
        a, b = self.track("a", 100.0), self.track("b", 100.0)
        self.put(a.id, incident_payload(a.id))
        self.put(b.id, incident_payload(b.id))
        (self.dir / "notes.txt").write_text("ignore me")
        (self.dir / f"{a.id}.json.bak").write_bytes(b"{")
        summary, found = self.run_check(f"--ids={a.id}")
        self.assertEqual((summary["scanned"], sorted(found)), (1, [a.id]))
        summary, _ = self.run_check("--limit=1")
        self.assertEqual(summary["scanned"], 1)

    def test_fail_on_suspect_sets_exit_status(self):
        bad = self.track("bad", 200.0)
        self.put(bad.id, incident_payload(bad.id))
        with self.assertRaises(SystemExit) as raised:
            call_command("check_waveform_integrity", f"--waveforms-dir={self.dir}", "--fail-on-suspect", stdout=StringIO())
        self.assertEqual(raised.exception.code, 1)

    def test_scanning_station_storage_requires_explicit_authority(self):
        from isadoraair import test_isolation
        with test_isolation.protect_for_test(self.dir):
            with self.assertRaises(CommandError) as refused:
                call_command("check_waveform_integrity", f"--waveforms-dir={self.dir}", stdout=StringIO())
            self.assertIn("--allow-production-read", str(refused.exception))
            call_command("check_waveform_integrity", f"--waveforms-dir={self.dir}",
                         "--allow-production-read", stdout=StringIO())

    def test_missing_directory_is_an_error(self):
        with self.assertRaises(CommandError):
            call_command("check_waveform_integrity", f"--waveforms-dir={self.dir / 'absent'}", stdout=StringIO())
