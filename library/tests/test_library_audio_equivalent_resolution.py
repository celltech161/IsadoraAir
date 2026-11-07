"""Duplicate resolution for verified audio-equivalent pairs (roadmap 3.11 extension of the r0127 workflow).

A likely duplicate pair of two Tracks becomes resolvable only when the scan records, for each file, its full
decoded-audio identity bound to the file's stat witness, and the two identities match.  The identity comes from
the Track's trusted waveform cache when that cache was made from exactly the walked file, otherwise from a decode
in this scan -- only when the operator asked for verification, within its budgets.  Everything after eligibility
is the unchanged r0127 path (preview, single-use per-pair tokens, one pair per request, engine/log/voice-track
protections, track_deletion).

Real audio throughout: ffmpeg writes a tone, and a "metadata-only" duplicate is a stream copy with different ID3
tags (different bytes and size, identical decoded audio).  Every file lives in the test's own Library root; the
engine state is a file in the test's directory (see test_library_duplicate_resolution_corrections).
"""
import hashlib
import json
import os
import subprocess
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from library.models import DuplicateCandidate, LogItem, PlaylistLog, Track, VoiceTrack
from library.services import audio_identity
from library.services import library_duplicate_resolution as resolution
from library.services import library_reconciliation as reconciliation
from library.services import library_reconciliation_launch as launcher
from library.tests.test_library_duplicate_resolution import PAGE_EXTRA, _node
from library.tests.test_library_duplicate_resolution_corrections import _Fixture
from library.tests.test_library_reconciliation_b import _analyze
from library.tests.test_library_reconciliation_launch import PAGE_HARNESS, TEMPLATE, FakeProc, _Http

FINDINGS = "library:api-reconciliation-findings"
RUN = "library:api-reconciliation-run"
SECONDS = 2.0
FRAMES = 88200                          # 2 s at 44.1 kHz


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args], check=True,
                   stdin=subprocess.DEVNULL)


def _tone(path, freq=440, seconds=SECONDS):
    path.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg("-f", "lavfi", "-i", f"aevalsrc=exprs='0.5*sin(2*PI*{freq}*t)':d={seconds}:s=44100", "-ac", "2", str(path))
    return path


def _retag(source, target, **tags):
    """The same audio stream, other tags: a stream copy (no re-encode) with only the given ID3 metadata."""
    target.parent.mkdir(parents=True, exist_ok=True)
    meta = [arg for k, v in tags.items() for arg in ("-metadata", f"{k}={v}")]
    _ffmpeg("-i", str(source), "-c", "copy", "-map_metadata", "-1", *meta, "-id3v2_version", "3", str(target))
    return target


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _witness(path):
    info = os.lstat(path)
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink]


class _Audio(_Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.decoded = []
        real = audio_identity.compute_or_none

        def spy(path, **kwargs):
            self.decoded.append((str(path), kwargs.get("timeout")))
            return real(path, **kwargs)
        patcher = mock.patch.object(audio_identity, "compute_or_none", side_effect=spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def registered(self, path, *, title, category=None, **fields):
        return Track.objects.create(filepath=str(path), filename=path.name, title=title, artist=self.artist,
                                    category=category or self.rock, duration_seconds=fields.pop("duration", SECONDS),
                                    format=path.suffix.lstrip("."), **fields)

    def audio_pair(self, name, *, differ=False):
        """Two Tracks of the same title, artist and duration: a tagged copy of one recording, or (``differ``)
        another recording."""
        a_path = _tone(self.root / "ROCK" / f"{name}-a.mp3")
        b_path = self.root / "POP" / f"{name}-b.mp3"
        if differ:
            _tone(b_path, freq=445)
        else:
            _retag(a_path, b_path, title=f"{name} (radio copy)", artist="Someone Else", comment="tagged")
        return (self.registered(a_path, title=f"Song {name}", category=self.rock),
                self.registered(b_path, title=f"Song {name}", category=self.pop))

    def scan(self, verify=0, **kwargs):
        self.report = reconciliation.run_scan(verify_audio_pairs=verify, **kwargs)
        return self.report

    def findings_api(self):
        response = self.client.get(reverse(FINDINGS), {"type": "duplicate"})
        self.assertEqual(response.status_code, 200)
        return {tuple(f["members"]): f for f in response.json()["items"]}

    @staticmethod
    def tokens_of(body):
        return [i["token"] for i in body["items"] if i.get("token")]

    def audio_budget(self):
        return self.report["budgets"]["audio_verification"]


# --------------------------------------------------------------------------- the scan's verification

class VerificationScanTests(_Audio):
    def test_fixture1_metadata_only_duplicates_are_verified_and_bound_to_their_files(self):
        a, b = self.audio_pair("meta")
        before = {p: (_sha(p), _witness(p)) for p in (a.filepath, b.filepath)}
        caches = list(Track.objects.order_by("pk").values_list("waveform_path", flat=True))
        self.assertNotEqual(before[a.filepath][0], before[b.filepath][0])             # different bytes
        self.assertNotEqual(os.path.getsize(a.filepath), os.path.getsize(b.filepath))
        self.scan(verify=25)
        finding = self.finding(a, b)
        self.assertEqual(finding["confidence"], "audio_equivalent", finding)
        self.assertIn("decoded audio identical (decoded in this scan)", finding["reason"])
        evidence = finding["audio_verification"]
        self.assertEqual((evidence["status"], evidence["bytes"]), ("verified", "different_size"))
        files = {f["track_id"]: f for f in evidence["files"]}
        self.assertEqual(set(files), {a.pk, b.pk})
        for track in (a, b):
            entry = files[track.pk]
            self.assertEqual((entry["path"], entry["witness"]), (track.filepath, before[track.filepath][1]))
            self.assertEqual(entry["identity_source"], "decoded")
            identity = entry["identity"]
            self.assertEqual((identity["algorithm"], identity["sample_rate"], identity["frames"]),
                             (audio_identity.ALGORITHM, 44100, FRAMES))
        self.assertEqual(files[a.pk]["identity"]["sha256"], files[b.pk]["identity"]["sha256"])
        self.assertEqual(sorted(p for p, _t in self.decoded), sorted([a.filepath, b.filepath]))
        self.assertEqual(self.audio_budget()["decodes"], 2)
        self.assertEqual((self.audio_budget()["verified"], self.audio_budget()["requested_pairs"]), (1, 25))
        # Read only: neither file changed (bytes, inode, times), and no cache was written.
        self.assertEqual({p: (_sha(p), _witness(p)) for p in before}, before)
        self.assertEqual(list(self.waves.iterdir()), [])
        self.assertEqual(list(Track.objects.order_by("pk").values_list("waveform_path", flat=True)), caches)

    def test_a_normal_scan_decodes_nothing_and_the_pair_stays_likely(self):
        a, b = self.audio_pair("plain")
        self.scan()
        finding = self.finding(a, b)
        self.assertEqual(finding["confidence"], "likely")
        self.assertEqual(finding["audio_verification"]["status"], "unverified")
        self.assertIn("Verify audio for likely duplicates", finding["audio_verification"]["reason"])
        self.assertEqual(self.decoded, [])
        listed = self.findings_api()[(a.pk, b.pk)]
        self.assertFalse(listed["resolution"]["deletable"])
        self.assertIn("this one is likely", listed["resolution"]["reason"])
        status, body = self.preview(self.delete(a, b))
        self.assertEqual((status, body["items"][0]["status"]), (200, "blocked"))
        self.assertEqual(self.tokens_of(body), [])

    def test_fixture2_same_metadata_different_audio_is_uncertain_and_never_resolvable(self):
        a, b = self.audio_pair("live", differ=True)
        self.scan(verify=25)
        finding = self.finding(a, b)
        self.assertEqual(finding["confidence"], "uncertain")
        self.assertEqual(finding["audio_verification"]["status"], "different")
        self.assertIn("the decoded audio differs (decoded in this scan)", finding["reason"])
        listed = self.findings_api()[(a.pk, b.pk)]
        self.assertFalse(listed["resolution"]["deletable"])
        status, body = self.preview(self.delete(a, b))
        self.assertEqual(body["items"][0]["status"], "blocked")
        self.assertEqual(self.exists(a, b), [True, True])
        status, body = self.preview(self.keep_both(a, b))             # the non-destructive review stays available
        self.assertEqual(body["items"][0]["status"], "eligible")

    def test_fixture3_unidentifiable_audio_stays_review_only(self):
        a = self.registered(_tone(self.root / "ROCK" / "broken-a.mp3"), title="Song broken")
        bad = self.root / "POP" / "broken-b.mp3"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"\xff\xfb not an mp3 at all " * 400)
        b = self.registered(bad, title="Song broken")
        self.scan(verify=25)
        finding = self.finding(a, b)
        self.assertEqual(finding["confidence"], "likely")
        self.assertIn("its audio could not be identified", finding["audio_verification"]["reason"])
        self.assertFalse(self.findings_api()[(a.pk, b.pk)]["resolution"]["deletable"])
        status, body = self.preview(self.delete(a, b))
        self.assertEqual(body["items"][0]["status"], "blocked")

    def test_fixture4_trusted_caches_are_reused_without_decoding(self):
        a, b = self.audio_pair("cached")
        _analyze(self, a)
        _analyze(self, b)
        self.decoded.clear()
        for verify in (0, 25):
            with self.subTest(verify=verify):
                self.scan(verify=verify)
                finding = self.finding(a, b)
                self.assertEqual(finding["confidence"], "audio_equivalent")
                self.assertIn("(trusted waveform caches)", finding["reason"])
                self.assertEqual({f["identity_source"] for f in finding["audio_verification"]["files"]},
                                 {"waveform_cache"})
                self.assertEqual(self.decoded, [])
                self.assertEqual(self.audio_budget()["decodes"], 0)
                self.assertTrue(self.findings_api()[(a.pk, b.pk)]["resolution"]["deletable"])

    def test_a_file_in_two_findings_is_decoded_once(self):
        shared = self.registered(_tone(self.root / "ROCK" / "shared.mp3"), title="Shared", isrc="USRC10000001")
        b = self.registered(_retag(Path(shared.filepath), self.root / "POP" / "shared-b.mp3", title="x"),
                            title="Shared")
        c = self.registered(_retag(Path(shared.filepath), self.root / "POP" / "shared-c.mp3", title="y"),
                            title="Other title", isrc="USRC10000001")
        self.scan(verify=25)
        for other in (b, c):
            self.assertEqual(self.finding(shared, other)["confidence"], "audio_equivalent")
        self.assertEqual(sorted(p for p, _t in self.decoded), sorted([shared.filepath, b.filepath, c.filepath]))

    def test_fixture5_a_stale_or_untrusted_cache_is_never_proof(self):
        a, b = self.audio_pair("stale")
        _analyze(self, a)
        _analyze(self, b)
        info = os.stat(b.filepath)
        os.utime(b.filepath, ns=(info.st_atime_ns, info.st_mtime_ns - 1_000_000_000))   # the cache is now stale
        self.decoded.clear()
        self.scan()
        finding = self.finding(a, b)
        self.assertEqual(finding["confidence"], "likely")
        self.assertIn("no trusted waveform cache records it", finding["audio_verification"]["reason"])
        self.assertFalse(self.findings_api()[(a.pk, b.pk)]["resolution"]["deletable"])
        self.scan(verify=25)                       # verification decodes the stale one only
        self.assertEqual(self.finding(a, b)["confidence"], "audio_equivalent")
        self.assertEqual([p for p, _t in self.decoded], [b.filepath])

    def test_identities_from_different_decoder_versions_never_disprove_a_match(self):
        """A trusted cache made by an older ffmpeg may record another digest for the same audio: that is not an
        explicit mismatch.  Without verification the pair stays unverified; with it both are decoded now."""
        a, b = self.audio_pair("decoder")
        _analyze(self, a)
        b_track = _analyze(self, b)
        cache = Path(b_track.waveform_path)
        payload = json.loads(cache.read_text())
        payload["audio_identity"].update(sha256="0" * 64, decoder="ffmpeg version 0.0 (an older build)")
        cache.write_text(json.dumps(payload))
        self.decoded.clear()
        self.scan()
        finding = self.finding(a, b)
        self.assertEqual(finding["confidence"], "likely")
        self.assertIn("different decoder versions", finding["audio_verification"]["reason"])
        self.scan(verify=25)
        finding = self.finding(a, b)
        self.assertEqual(finding["confidence"], "audio_equivalent")
        self.assertEqual({f["identity_source"] for f in finding["audio_verification"]["files"]}, {"decoded"})

    def test_fixture9_groups_of_three_are_not_verified_or_resolvable(self):
        first = _tone(self.root / "ROCK" / "trio-a.mp3")
        trio = [self.registered(first, title="Trio")] + [
            self.registered(_retag(first, self.root / "POP" / f"trio-{n}.mp3", title=n), title="Trio") for n in "bc"]
        self.scan(verify=25)
        finding = self.finding(*trio)
        self.assertEqual((finding["confidence"], finding["member_count"]), ("likely", 3))
        self.assertNotIn("audio_verification", finding)
        self.assertEqual(self.decoded, [])
        for track in trio:
            _analyze(self, track)
        self.scan()                                # caches label the group, as before; it stays review only
        finding = self.finding(*trio)
        self.assertEqual(finding["confidence"], "audio_equivalent")
        listed = self.findings_api()[tuple(sorted(t.pk for t in trio))]
        self.assertFalse(listed["resolution"]["deletable"])
        self.assertIn("3 Tracks share this audio", listed["resolution"]["reason"])

    def test_fixture10_an_exhausted_decode_budget_never_yields_a_match(self):
        pairs = [self.audio_pair(f"budget{i}") for i in range(3)]
        report = reconciliation.Scanner(verify_audio_pairs=1).run()
        budget = report["budgets"]["audio_verification"]
        self.assertEqual((budget["max_decodes"], budget["decodes"], budget["verified"], budget["budget_exhausted"]),
                         (2, 2, 1, 2))
        self.report = report
        confidences = [self.finding(a, b)["confidence"] for a, b in pairs]
        self.assertEqual(sorted(confidences), ["audio_equivalent", "likely", "likely"])
        for a, b in pairs:
            finding = self.finding(a, b)
            if finding["confidence"] == "likely":
                self.assertIn("budget was used up", finding["audio_verification"]["reason"])
                self.assertEqual(finding["audio_verification"]["files"], [])
        self.assertTrue(any("audio verification budget was used up" in e for e in report["errors"]))

    def test_fixture10_an_exhausted_time_budget_never_yields_a_match_and_each_decode_is_bounded(self):
        a, b = self.audio_pair("clock")
        with mock.patch.object(reconciliation, "VERIFY_AUDIO_MAX_SECONDS", 0):
            report = reconciliation.Scanner(verify_audio_pairs=25).run()
        self.report = report
        self.assertEqual(self.finding(a, b)["confidence"], "likely")
        self.assertEqual(report["budgets"]["audio_verification"]["budget_exhausted"], 1)
        self.assertEqual(self.decoded, [])
        self.scan(verify=25)
        self.assertTrue(all(0 < t <= reconciliation.VERIFY_DECODE_TIMEOUT_SECONDS for _p, t in self.decoded))

    def test_a_file_changed_while_it_is_decoded_is_not_evidence(self):
        a, b = self.audio_pair("race")
        real = audio_identity.compute

        def changing(path, **kwargs):
            identity = real(path, **kwargs)
            if str(path) == b.filepath:
                with open(path, "ab") as handle:
                    handle.write(b"\0")
            return identity, ""
        with mock.patch.object(audio_identity, "compute_or_none", side_effect=changing):
            self.scan(verify=25)
        finding = self.finding(a, b)
        self.assertEqual(finding["confidence"], "likely")
        self.assertIn("changed while its audio was being decoded", finding["audio_verification"]["reason"])

    def test_a_failed_scan_keeps_the_last_complete_report(self):
        a, b = self.audio_pair("keep")
        good = self.scan(verify=25)["scan_id"]
        with mock.patch.object(reconciliation.Scanner, "verify_pair", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.scan(verify=25)
        self.assertEqual(reconciliation.load_report()["scan_id"], good)
        self.assertEqual(reconciliation.scan_status()["last_attempt"]["status"], "failed")


# --------------------------------------------------------------------------- resolution (the r0127 path)

class ResolutionTests(_Audio):
    def test_a_verified_pair_is_resolved_through_the_existing_workflow(self):
        a, b = self.audio_pair("resolve")
        survivor_bytes = _sha(a.filepath)
        self.scan(verify=25)
        listed = self.findings_api()[(a.pk, b.pk)]
        self.assertTrue(listed["resolution"]["deletable"], listed["resolution"])
        evidence = listed["evidence"]
        self.assertEqual((evidence["kind"], evidence["bytes"], evidence["frames"]),
                         ("decoded_audio", "different_size", FRAMES))
        status, body = self.preview(self.delete(a, b))
        item = body["items"][0]
        self.assertEqual((status, item["status"]), (200, "eligible"), item)
        self.assertEqual(item["evidence"]["kind"], "decoded_audio")
        self.assertEqual([c["code"] for c in item["suggested_categories"]], ["POP"])
        from library.services import track_deletion
        # The file was written moments ago; as in the r0127 tests, the "possibly mid-delivery" grace is off.
        with mock.patch.object(track_deletion, "SOURCE_FRESHNESS_GRACE_SECONDS", 0), \
                self.captureOnCommitCallbacks(execute=True):
            status, results = self.apply_token(item["token"], {"categories": [self.pop.pk]})
        self.assertEqual((status, results[0]["outcome"]), (200, "deleted"), results)
        self.assertEqual(results[0]["categories_added"], ["POP"])
        a.refresh_from_db()
        self.assertEqual(a.category_id, self.rock.pk)                                # primary unchanged
        self.assertEqual(list(a.additional_categories.values_list("code", flat=True)), ["POP"])
        self.assertEqual(self.exists(a, b), [True, False])
        self.assertFalse(Path(b.filepath).exists())
        self.assertEqual(_sha(a.filepath), survivor_bytes)                            # the survivor's file untouched
        from monitoring.models import SystemEvent
        deleted = [e.detail for e in SystemEvent.objects.filter(category="library")
                   if e.detail.get("action") == "reconciliation.duplicate_delete"]
        self.assertEqual([(d["kept"], d["deleted"], d["evidence"]) for d in deleted], [(a.pk, b.pk, "decoded_audio")])
        status, again = self.apply_token(item["token"], {"categories": []})          # single use, as before
        self.assertEqual(status, 409, again)

    def test_keep_both_is_offered_and_recorded_as_before(self):
        a, b = self.audio_pair("both")
        self.scan(verify=25)
        status, body = self.preview(self.keep_both(a, b))
        self.assertEqual(body["items"][0]["status"], "eligible")
        self.assertEqual([r["outcome"] for r in self.apply_all(body, confirm=False)], ["recorded"])
        self.assertEqual(DuplicateCandidate.objects.get().resolution, "keep_both")
        self.assertEqual(self.exists(a, b), [True, True])

    def test_fixture6_a_file_changed_after_verification_is_blocked_at_preview(self):
        a, b = self.audio_pair("after")
        self.scan(verify=25)
        info = os.stat(b.filepath)
        os.utime(b.filepath, ns=(info.st_atime_ns, info.st_mtime_ns))                # ctime changes, nothing else
        status, body = self.preview(self.delete(a, b))
        item = body["items"][0]
        self.assertEqual(item["status"], "blocked")
        self.assertTrue(any("modified or replaced since the scan" in r and "decoded audio was verified" in r
                            for r in item["reasons"]), item["reasons"])
        self.assertEqual(self.tokens_of(body), [])

    def test_fixture7_a_file_changed_between_preview_and_apply_is_refused(self):
        a, b = self.audio_pair("between")
        self.scan(verify=25)
        status, body = self.preview(self.delete(a, b))
        self.assertEqual(body["items"][0]["status"], "eligible")
        _retag(Path(a.filepath), self.root / "ROCK" / "between-a.tmp.mp3", title="new tags")
        os.replace(self.root / "ROCK" / "between-a.tmp.mp3", a.filepath)              # the survivor replaced
        results = self.apply_all(body)
        self.assertIn(results[0]["outcome"], ("stale", "blocked"))
        self.assertEqual(self.exists(a, b), [True, True])

    def test_fixture8_missing_or_malformed_evidence_authorizes_nothing(self):
        a, b = self.audio_pair("evidence")
        self.scan(verify=25)
        good = json.loads(json.dumps(self.report))
        other = audio_identity.compute(_tone(self.root / "x" / "other.mp3", freq=300))

        def record(report):
            return next(f for f in report["findings"] if f.get("members") == sorted([a.pk, b.pk]))["audio_verification"]

        damage = {
            "no record": lambda r, f: f.pop("audio_verification"),
            "not verified": lambda r, f: record(r).update(status="unverified"),
            "one file": lambda r, f: record(r)["files"].pop(),
            "files not a list": lambda r, f: record(r).update(files={"a": 1}),
            "no witness": lambda r, f: record(r)["files"][0].pop("witness"),
            "short witness": lambda r, f: record(r)["files"][0].update(witness=[1, 2, 3]),
            "witness of another size": lambda r, f: record(r)["files"][0]["witness"].__setitem__(2, 1),
            "no identity": lambda r, f: record(r)["files"][1].pop("identity"),
            "identity of other audio": lambda r, f: record(r)["files"][1].update(identity=other),
            "other algorithm": lambda r, f: record(r)["files"][1]["identity"].update(algorithm="md5"),
            "other sample rate": lambda r, f: record(r)["files"][1]["identity"].update(sample_rate=48000),
            "other length": lambda r, f: record(r)["files"][1]["identity"].update(frames=FRAMES + 1),
            "malformed digest": lambda r, f: [e["identity"].update(sha256="x" * 64) for e in record(r)["files"]],
            "same file twice": lambda r, f: record(r)["files"][1].update(path=record(r)["files"][0]["path"]),
            "a foreign Track": lambda r, f: record(r)["files"][1].update(track_id=999999),
            "likely label": lambda r, f: f.update(confidence="likely"),
        }
        for name, change in damage.items():
            with self.subTest(evidence=name):
                report = json.loads(json.dumps(good))
                finding = next(f for f in report["findings"] if f.get("members") == sorted([a.pk, b.pk]))
                change(report, finding)
                reconciliation.write_report(report)
                self.report = report
                self.assertFalse(self.findings_api()[(a.pk, b.pk)]["resolution"]["deletable"])
                status, body = self.preview(self.delete(a, b))
                self.assertEqual((status, body["items"][0]["status"]), (200, "blocked"), body)
                self.assertEqual(self.tokens_of(body), [])
        self.assertEqual(self.exists(a, b), [True, True])

    def test_fixture11_exact_and_decoded_audio_pairs_are_resolved_side_by_side(self):
        x, y = self.pair("bytes")                                      # the r0127 exact pair
        a, b = self.audio_pair("audio")
        self.scan(verify=25)
        self.assertEqual(self.finding(x, y)["confidence"], "exact")
        listed = self.findings_api()
        self.assertEqual(listed[tuple(sorted([x.pk, y.pk]))]["evidence"]["kind"], "exact_bytes")
        self.assertEqual(listed[(a.pk, b.pk)]["evidence"]["kind"], "decoded_audio")
        status, body = self.preview(self.delete(x, y), self.delete(a, b))
        self.assertEqual([i["status"] for i in body["items"]], ["eligible", "eligible"])
        self.assertEqual([i["evidence"]["kind"] for i in body["items"]], ["exact_bytes", "decoded_audio"])
        self.assertEqual([r["outcome"] for r in self.apply_all(body)], ["deleted", "deleted"])
        self.assertEqual(self.exists(x, y, a, b), [True, False, True, False])

    def test_fixture12_the_r0127_protections_apply_unchanged(self):
        from django.utils import timezone
        cases = {}
        for name in ("engine", "voicetracks", "log", "replay"):
            cases[name] = self.audio_pair(f"protect-{name}")
        VoiceTrack.objects.create(track=cases["voicetracks"][1], position="intro", filepath="/nonexistent/vt.wav")
        now = timezone.localtime()
        log, _ = PlaylistLog.objects.get_or_create(date=now.date(), hour=now.hour, defaults={"status": "approved"})
        LogItem.objects.create(playlist_log=log, position=1, scheduled_time=now, track=cases["log"][1])
        self.scan(verify=25)
        self.assertTrue(all(self.finding(*p)["confidence"] == "audio_equivalent" for p in cases.values()))

        self.state_path.unlink()
        status, body = self.preview(self.delete(*cases["engine"]))
        self.assertTrue(any("playout engine's current state cannot be confirmed" in r
                            for r in body["items"][0]["reasons"]))
        self.engine()

        status, body = self.preview(self.delete(*cases["voicetracks"]))
        self.assertEqual(body["items"][0]["status"], "needs_acknowledgment")
        status, results = self.apply_token(self.first_token(body), {"categories": []})
        self.assertEqual(results[0]["outcome"], "needs_acknowledgment")
        status, body = self.preview(self.delete(*cases["log"]))
        self.assertTrue(any("current or upcoming log hour" in r for r in body["items"][0]["reasons"]))

        status, body = self.preview(self.delete(*cases["replay"]))
        token = self.first_token(body)
        self.assertEqual(self.apply_token(token)[1][0]["outcome"], "deleted")
        self.assertEqual(self.apply_token(token)[0], 409)
        self.assertEqual(self.exists(*cases["engine"], *cases["voicetracks"], *cases["log"]), [True] * 6)


# --------------------------------------------------------------------------- launching a verifying scan

class LauncherTests(_Http):
    def test_the_command_is_unchanged_unless_a_fixed_choice_is_made(self):
        default = launcher.scan_command()
        for body, extra in ((None, []), ({}, []), ({"verify_audio_pairs": 0}, []),
                            ({"verify_audio_pairs": 25}, ["--verify-audio-pairs", "25"]),
                            ({"verify_audio_pairs": 100}, ["--verify-audio-pairs", "100"])):
            with self.subTest(body=body):
                self.calls.clear()
                with self.fake_popen(FakeProc(exit_code=0)):
                    self.client.post(reverse(RUN), data=json.dumps(body) if body is not None else "",
                                     content_type="application/json")
                (argv, _kwargs), = self.calls
                self.assertEqual(argv, default + extra)

    def test_a_form_post_is_answered_and_its_fields_are_ignored(self):
        """Only a JSON body carries the choice; a form body (read by the CSRF middleware) is no choice at all."""
        with self.fake_popen(FakeProc(exit_code=0)):
            response = self.client.post(reverse(RUN), {"verify_audio_pairs": "25"})
        self.assertNotEqual(response.status_code, 400)
        (argv, _kwargs), = self.calls
        self.assertEqual(argv, launcher.scan_command())

    def test_any_other_value_is_refused_and_nothing_starts(self):
        for value in (7, -25, 1000, "25", 25.0, True, None, [25]):
            with self.subTest(value=value):
                with self.fake_popen(FakeProc(exit_code=0)):
                    response = self.client.post(reverse(RUN), data=json.dumps({"verify_audio_pairs": value}),
                                                content_type="application/json")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["code"], "invalid_choice")
        self.assertEqual(self.calls, [])
        self.assertEqual(launcher.audio_verification_args(25.0), [])
        self.assertEqual(launcher.audio_verification_args(True), [])

    def test_the_command_line_bounds_the_choice(self):
        with self.assertRaises(CommandError):
            call_command("reconcile_library", "--verify-audio-pairs", str(reconciliation.MAX_VERIFY_AUDIO_PAIRS + 1),
                         stdout=StringIO())
        out = StringIO()
        call_command("reconcile_library", "--verify-audio-pairs", "5", stdout=out)
        self.assertEqual(reconciliation.load_report()["budgets"]["audio_verification"]["requested_pairs"], 5)


# --------------------------------------------------------------------------- the page

PAGE_SCENARIO_EXTRA = r"""
const identity = { algorithm: 'isadoraair-audio/v1:pcm_s32le:2ch:native-rate:sha256', sha256: 'ab'.repeat(32),
                   sample_rate: 44100, channels: 2, sample_format: 's32le', frames: 88200 };
const audioDup = (id, a, b, status = 'verified') => ({ ...dup(id, a, b, status === 'verified'),
  confidence: status === 'verified' ? 'audio_equivalent' : 'likely', files: undefined,
  audio_verification: { status, bytes: 'different_size',
    reason: status === 'verified' ? 'decoded audio identical (decoded in this scan)'
                                  : 'decoded audio not verified: no trusted waveform cache records it',
    files: status === 'verified' ? [a, b].map(t => ({ track_id: t, rel: `X/${t}.mp3`, size: 1000 + t,
                                                      identity, identity_source: 'decoded' })) : [] },
  evidence: status === 'verified' ? { kind: 'decoded_audio', sha256: identity.sha256, sample_rate: 44100,
                                      frames: 88200, bytes: 'different_size' } : null,
  resolution: status === 'verified' ? { deletable: true, reason: '' }
    : { deletable: false, reason: 'only identical-byte (exact) findings ... this one is likely' } });
"""


class PageTests(_Audio):
    def run_page(self, scenario):
        source = TEMPLATE.read_text()
        page = source[source.index("<script>") + len("<script>"):source.index("</script>")]
        return _node(self, PAGE_HARNESS + PAGE_EXTRA + page + PAGE_SCENARIO_EXTRA + "\n(async () => {\n" + scenario +
                     "\n})().catch(e => { console.error(e); process.exit(1); });\n")

    def test_the_two_kinds_of_evidence_are_named_apart(self):
        out = self.run_page(r"""
  console.log(JSON.stringify({
    exact: evidenceText({ kind: 'exact_bytes' }),
    audio: evidenceText({ kind: 'decoded_audio', sample_rate: 44100, frames: 88200, bytes: 'different_size' }),
    same: evidenceText({ kind: 'decoded_audio', sample_rate: 44100, frames: 88200, bytes: 'identical_bytes' }),
    none: evidenceText(null) }));
""")
        self.assertIn("Exact file match", out["exact"])
        self.assertIn("identical bytes", out["exact"])
        self.assertIn("Verified decoded-audio match", out["audio"])
        self.assertIn("The two files are different (different sizes)", out["audio"])
        self.assertIn("tags, artwork, cue points and other Track details may differ", out["audio"])
        self.assertIn("Nothing is merged", out["audio"])
        self.assertNotIn("identical bytes", out["audio"])
        self.assertIn("also byte-identical", out["same"])
        self.assertEqual(out["none"], "")

    def test_cards_show_the_verified_match_and_offer_the_existing_choices_only_when_deletable(self):
        out = self.run_page(r"""
  await bootDup({ page: 1, pages: 1, total: 2, scan_id: 'A', items: [audioDup('G1', 11, 12), audioDup('G2', 13, 14, 'unverified')] });
  const cards = ids.findings.children;
  const radios = name => walk(ids.findings, n => n.tag === 'input' && n.name === name).map(n => n.value);
  console.log(JSON.stringify({ verified: cards[0].text, unverified: cards[1].text,
    choices1: radios('resolve-G1'), choices2: radios('resolve-G2') }));
""")
        self.assertIn("Verified decoded-audio match", out["verified"])
        self.assertIn("decoded in this scan", out["verified"])
        self.assertIn("different sizes", out["verified"])
        self.assertEqual(out["choices1"], ["none", "keep_a", "keep_b", "keep_both"])
        self.assertEqual(out["choices2"], [])
        self.assertIn("Decoded audio: decoded audio not verified", out["unverified"])
        self.assertIn("Review only", out["unverified"])

    def test_run_offers_verification_off_by_default_and_posts_only_the_choice(self):
        out = self.run_page(r"""
  await bootDup({ page: 1, pages: 1, total: 0, scan_id: 'A', items: [] });
  const bodies = [];
  for (const choice of [null, '50']) {
    ids.runBtn.listeners.click[0]();
    const select = walk(ids.actionPanel, n => n.tag === 'select')[0];
    const options = select.children.map(o => o.value);
    const initial = select.value;
    if (choice) select.value = choice;
    walk(ids.actionPanel, n => n.tag === 'button' && n.textContent === 'Start read-only scan')[0].listeners.click[0]();
    await flush();
    const req = open(r => r.url === RUN)[0];
    bodies.push({ options, initial, body: JSON.parse(req.body) });
    respond(req, 409, { error: 'busy', code: 'scan_running' });
    await flush();
    open(r => r.url === SUMMARY).forEach(r => respond(r, 200, status(A, A)));
    await flush();
  }
  console.log(JSON.stringify(bodies));
""")
        self.assertEqual(out[0]["options"], ["0", "25", "50", "100"])
        self.assertEqual(out[0]["initial"], "0")
        self.assertEqual(out[0]["body"], {"verify_audio_pairs": 0})
        self.assertEqual(out[1]["body"], {"verify_audio_pairs": 50})
