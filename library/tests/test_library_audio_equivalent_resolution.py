"""Duplicate resolution for verified audio-equivalent pairs (roadmap 3.11 extension of the r0127 workflow).

A likely duplicate pair of two Tracks becomes resolvable only when the scan records, for each file, its full
decoded-audio identity bound to the file's stat witness, and the two identities match.  The identity comes from
the Track's trusted waveform cache when that cache was made from exactly the walked file, otherwise from a decode
in this scan -- only when the operator asked for verification, within its budgets.  Everything after eligibility
is the unchanged r0127 path (preview, single-use per-pair tokens, one pair per request, engine/log/voice-track
protections, track_deletion).

Codex corrections (this module's second half): the decode is bound to the walked file through one held
descriptor (a parent directory exchanged and restored around ffmpeg lends nothing); a disagreement between
identities from different ffmpeg versions is decoded again or left unverified, never "different"; the pair limit
counts pairs needing fresh verification, not decodes; one deadline covers probe and decode, and the scan's total
bounds each of them.  ``DECODE_ENTRY`` follows whichever decode function the scanner calls, so the module also
runs against the uncorrected implementation (where those regressions fail).

Real audio throughout: ffmpeg writes a tone, and a "metadata-only" duplicate is a stream copy with different ID3
tags (different bytes and size, identical decoded audio).  Every file lives in the test's own Library root; the
engine state is a file in the test's directory (see test_library_duplicate_resolution_corrections).
"""
import hashlib
import json
import os
import subprocess
import threading
import time
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
OLD_DECODER = "ffmpeg version 0.0 (an older build)"
#: What the scanner's verification calls to decode: a held descriptor (corrected) or a pathname (uncorrected).
DECODE_ENTRY = "compute_fd_or_none" if hasattr(audio_identity, "compute_fd_or_none") else "compute_or_none"


def _source_of(arg):
    """The file a decode call is about: a descriptor's file (as this process sees it), or a pathname."""
    return os.readlink(f"/proc/self/fd/{arg}") if isinstance(arg, int) else str(arg)


def _cli_source(cmd):
    """The input an ffprobe/ffmpeg command line names, resolved as this process sees it."""
    source = cmd[cmd.index("-i") + 1] if "-i" in cmd else cmd[-1]
    if source.startswith("/proc/self/fd/"):              # the child inherits the parent's descriptor number
        try:
            return os.readlink(source)
        except OSError:
            return source
    return source


class _SubprocessProxy:
    """Stands in for ``subprocess`` inside one module: hooks ``run``/``Popen``, delegates everything else."""

    def __init__(self, on_run=None, on_popen=None):
        self.on_run, self.on_popen, self.calls = on_run, on_popen, []

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def run(self, cmd, **kwargs):
        self.calls.append(("run", list(cmd), kwargs))
        if self.on_run:
            cmd, kwargs = self.on_run(list(cmd), dict(kwargs)) or (cmd, kwargs)
        return subprocess.run(cmd, **kwargs)

    def Popen(self, cmd, **kwargs):
        self.calls.append(("Popen", list(cmd), kwargs))
        if self.on_popen:
            cmd, kwargs = self.on_popen(list(cmd), dict(kwargs)) or (cmd, kwargs)
        return subprocess.Popen(cmd, **kwargs)


class _Exchange:
    """Puts ``other`` where the directory ``live`` is, and back (Codex's parent-directory exchange)."""

    def __init__(self, live, other):
        self.live, self.other, self.aside, self.swaps = Path(live), Path(other), Path(f"{live}.aside"), 0

    def swap(self):
        os.rename(self.live, self.aside)
        os.rename(self.other, self.live)
        self.swaps += 1

    def restore(self):
        if self.aside.exists():
            os.rename(self.live, self.other)
            os.rename(self.aside, self.live)


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def monotonic(self):
        return self.now

    def __call__(self):
        return self.now


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
        real = getattr(audio_identity, DECODE_ENTRY)

        def spy(source, **kwargs):
            self.decoded.append((_source_of(source), kwargs.get("timeout")))
            return real(source, **kwargs)
        patcher = mock.patch.object(audio_identity, DECODE_ENTRY, side_effect=spy)
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

    def cached(self, track, **identity_changes):
        """The station's analysis of the Track (a trusted cache), its recorded identity then edited."""
        track = _analyze(self, track)
        if identity_changes:
            cache = Path(track.waveform_path)
            payload = json.loads(cache.read_text())
            payload["audio_identity"].update(identity_changes)
            cache.write_text(json.dumps(payload))
        return track

    def assert_not_resolvable(self, a, b):
        self.assertNotEqual(self.finding(a, b)["confidence"], "audio_equivalent")
        listed = self.findings_api()[tuple(sorted((a.pk, b.pk)))]
        self.assertFalse(listed["resolution"]["deletable"], listed["resolution"])
        status, body = self.preview(self.delete(a, b))
        self.assertEqual((status, body["items"][0]["status"]), (200, "blocked"), body)
        self.assertEqual(self.tokens_of(body), [])


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
        self.cached(a)
        self.cached(b, sha256="0" * 64, decoder=OLD_DECODER)
        self.decoded.clear()
        self.scan()
        finding = self.finding(a, b)
        self.assertEqual(finding["confidence"], "likely")
        self.assertIn("different decoder versions", finding["audio_verification"]["reason"])
        self.scan(verify=25)
        finding = self.finding(a, b)
        self.assertEqual(finding["confidence"], "audio_equivalent")
        # Only the identity the running ffmpeg did not make is decoded again; the current cache stands.
        sources = {f["track_id"]: f["identity_source"] for f in finding["audio_verification"]["files"]}
        self.assertEqual(sources, {a.pk: "waveform_cache", b.pk: "decoded"})
        self.assertEqual([p for p, _t in self.decoded], [b.filepath])

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
        real = getattr(audio_identity, DECODE_ENTRY)

        def appending(path):
            with open(path, "ab") as handle:
                handle.write(b"\0")

        def overwriting(path):                     # same size, mtime put back: only ctime tells
            info = os.stat(path)
            with open(path, "r+b") as handle:
                handle.seek(info.st_size // 2)
                handle.write(b"\0\0\0\0")
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        for name, modify in (("append", appending), ("in place", overwriting)):
            with self.subTest(name):
                def changing(source, **kwargs):
                    result = real(source, **kwargs)
                    if _source_of(source) == b.filepath:
                        modify(b.filepath)
                    return result
                with mock.patch.object(audio_identity, DECODE_ENTRY, side_effect=changing):
                    self.scan(verify=25)
                finding = self.finding(a, b)
                self.assertEqual(finding["confidence"], "likely")
                self.assertIn("changed while its audio was being decoded", finding["audio_verification"]["reason"])
                self.assert_not_resolvable(a, b)

    def test_a_failed_scan_keeps_the_last_complete_report(self):
        a, b = self.audio_pair("keep")
        good = self.scan(verify=25)["scan_id"]
        with mock.patch.object(reconciliation.Scanner, "verify_pair", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.scan(verify=25)
        self.assertEqual(reconciliation.load_report()["scan_id"], good)
        self.assertEqual(reconciliation.scan_status()["last_attempt"]["status"], "failed")


# --------------------------------------------------------------------------- Codex finding 1: descriptor-bound decode

class DescriptorBindingTests(_Audio):
    """The decoded identity is of the file the walk witnessed, whatever happens to its pathname meanwhile."""

    def exchange_scenario(self, name):
        """Genuinely different recordings a and b; a stand-in directory holds a file of b's name that carries a's
        audio (a tagged copy).  Exchanging b's parent directory with it would lend b the audio of a."""
        a, b = self.audio_pair(name, differ=True)
        live = Path(b.filepath).parent
        decoy = self.root.parent / f"{name}-decoy"
        _retag(Path(a.filepath), decoy / Path(b.filepath).name, title="decoy")
        self.genuine_b = audio_identity.compute(b.filepath)
        self.assertFalse(audio_identity.same_audio(self.genuine_b, audio_identity.compute(a.filepath)))
        return a, b, _Exchange(live, decoy)

    def test_codex_parent_directory_exchange_restored_before_the_final_check_lends_nothing(self):
        """Codex's reproduction: witness confirmed, the parent directory exchanged as ffprobe/ffmpeg start, the
        decode finishes, the directory is restored, the pathname's witness is unchanged."""
        a, b, exchange = self.exchange_scenario("swap")
        self.addCleanup(exchange.restore)
        real = getattr(audio_identity, DECODE_ENTRY)

        def on_run(cmd, kwargs):
            if cmd[0] == "ffprobe" and _cli_source(cmd) == b.filepath and not exchange.swaps:
                exchange.swap()

        def restoring(source, **kwargs):
            try:
                return real(source, **kwargs)
            finally:
                exchange.restore()
        proxy = _SubprocessProxy(on_run=on_run)
        with mock.patch.object(audio_identity, "subprocess", proxy), \
                mock.patch.object(audio_identity, DECODE_ENTRY, side_effect=restoring):
            self.scan(verify=25)
        self.assertEqual(exchange.swaps, 1, "the exchange must actually have happened")
        finding = self.finding(a, b)
        self.assertNotEqual(finding["confidence"], "audio_equivalent", finding)
        # The held descriptor was decoded: the genuine b, which differs from a.
        self.assertEqual((finding["confidence"], finding["audio_verification"]["status"]), ("uncertain", "different"))
        recorded = {f["track_id"]: f["identity"] for f in finding["audio_verification"]["files"]}
        self.assertTrue(audio_identity.same_audio(recorded[b.pk], self.genuine_b))
        # ffprobe and ffmpeg were given the inherited descriptor, never the Library pathname.
        for kind, cmd, kwargs in proxy.calls:
            if cmd[0] in ("ffprobe", "ffmpeg") and "-version" not in cmd:
                source = cmd[cmd.index("-i") + 1] if "-i" in cmd else cmd[-1]
                self.assertTrue(source.startswith("/proc/self/fd/"), cmd)
                self.assertEqual(tuple(kwargs.get("pass_fds")), (int(source.rsplit("/", 1)[1]),))
                self.assertNotIn(b.filepath, cmd)
                self.assertNotIn(a.filepath, cmd)
        self.assert_not_resolvable(a, b)

    def test_an_exchange_left_in_place_after_the_decode_is_refused(self):
        a, b, exchange = self.exchange_scenario("left")
        self.addCleanup(exchange.restore)

        def on_run(cmd, kwargs):
            if cmd[0] == "ffprobe" and _cli_source(cmd) == b.filepath and not exchange.swaps:
                exchange.swap()
        with mock.patch.object(audio_identity, "subprocess", _SubprocessProxy(on_run=on_run)):
            self.scan(verify=25)
        exchange.restore()
        self.assertEqual(exchange.swaps, 1)
        finding = self.finding(a, b)
        self.assertEqual(finding["confidence"], "likely")
        self.assertIn("changed while its audio was being decoded", finding["audio_verification"]["reason"])
        self.assert_not_resolvable(a, b)

    def test_an_exchange_before_the_descriptor_is_taken_is_refused(self):
        """The pathname passes its check, then names the stand-in when it is opened: the open file's fstat is not
        the walk's witness, so nothing is decoded (restored at once, so only the descriptor check can tell)."""
        a, b, exchange = self.exchange_scenario("early")
        self.addCleanup(exchange.restore)
        state = {"armed": False}
        real_decode, real_open = reconciliation.Scanner._decode, os.open

        def armed_decode(scanner, entry):
            state["armed"] = entry.path == b.filepath
            try:
                return real_decode(scanner, entry)
            finally:
                state["armed"] = False

        class _Os:
            def __getattr__(self, name):
                return getattr(os, name)

            @staticmethod
            def open(path, flags, *args, **kwargs):
                if state["armed"] and str(path) == b.filepath and not exchange.swaps:
                    exchange.swap()
                    try:
                        return real_open(path, flags, *args, **kwargs)
                    finally:
                        exchange.restore()
                return real_open(path, flags, *args, **kwargs)
        with mock.patch.object(reconciliation.Scanner, "_decode", armed_decode), \
                mock.patch.object(reconciliation, "os", _Os()):
            self.scan(verify=25)
        self.assertEqual(exchange.swaps, 1)
        finding = self.finding(a, b)
        self.assertEqual(finding["confidence"], "likely")
        self.assertIn("changed after the filesystem walk", finding["audio_verification"]["reason"])
        self.assertNotIn(b.filepath, [p for p, _t in self.decoded])
        self.assert_not_resolvable(a, b)

    def test_an_exchange_before_the_decode_starts_is_refused(self):
        a, b, exchange = self.exchange_scenario("before")
        self.addCleanup(exchange.restore)
        real_decode = reconciliation.Scanner._decode

        def swapped_decode(scanner, entry):
            if entry.path == b.filepath and not exchange.swaps:
                exchange.swap()
            return real_decode(scanner, entry)
        with mock.patch.object(reconciliation.Scanner, "_decode", swapped_decode):
            self.scan(verify=25)
        exchange.restore()
        finding = self.finding(a, b)
        self.assertEqual(finding["confidence"], "likely")
        self.assertIn("changed after the filesystem walk", finding["audio_verification"]["reason"])
        self.assert_not_resolvable(a, b)

    def test_failed_descriptor_decoding_has_no_pathname_fallback(self):
        """If ffprobe/ffmpeg cannot read the inherited descriptor (here: it is not passed), nothing is identified
        and the pathname is never decoded instead."""
        a, b = self.audio_pair("nofd")
        by_path = mock.Mock(side_effect=AssertionError("pathname decode"))

        def withhold(cmd, kwargs):
            kwargs.pop("pass_fds", None)
            return cmd, kwargs
        with mock.patch.object(audio_identity, "subprocess", _SubprocessProxy(on_run=withhold, on_popen=withhold)), \
                mock.patch.object(audio_identity, "compute", by_path):
            self.scan(verify=25)
        by_path.assert_not_called()
        finding = self.finding(a, b)
        self.assertEqual(finding["confidence"], "likely")
        self.assertIn("its audio could not be identified (ffprobe could not read the file)",
                      finding["audio_verification"]["reason"])
        self.assert_not_resolvable(a, b)


class AudioIdentityDescriptorTests(TestCase):
    """``audio_identity.compute_fd``: the open file, not its pathname; equal to ``compute`` for every format."""

    def setUp(self):
        import tempfile
        self.dir = Path(tempfile.mkdtemp(prefix="isadoraair-fd-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.dir)], check=False))

    def test_the_held_file_is_decoded_even_after_its_pathname_names_another_recording(self):
        path = _tone(self.dir / "held.mp3")
        original = audio_identity.compute(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        self.addCleanup(os.close, fd)
        os.unlink(path)
        _tone(path, freq=300)                                   # the pathname now names another recording
        proxy = _SubprocessProxy()
        with mock.patch.object(audio_identity, "subprocess", proxy):
            held = audio_identity.compute_fd(fd, timeout=60)
        self.assertTrue(audio_identity.same_audio(held, original))
        self.assertFalse(audio_identity.same_audio(held, audio_identity.compute(path)))
        tools = [(cmd, kwargs) for _k, cmd, kwargs in proxy.calls if cmd[0] in ("ffprobe", "ffmpeg")
                 and "-version" not in cmd]
        self.assertEqual([cmd[0] for cmd, _kw in tools], ["ffprobe", "ffmpeg"])
        for cmd, kwargs in tools:
            self.assertIn(f"/proc/self/fd/{fd}", cmd)
            self.assertNotIn(str(path), cmd)
            self.assertEqual(tuple(kwargs["pass_fds"]), (fd,))

    def test_every_supported_format_has_the_same_identity_through_its_descriptor(self):
        """Seeking works through ``/proc/self/fd/N`` (an M4A with its index at the end needs it)."""
        for name, extra in (("t.mp3", ()), ("t.flac", ()), ("t.wav", ()), ("t.ogg", ()), ("t.aiff", ()),
                            ("t.mp2", ()), ("t.m4a", ()), ("alac.m4a", ("-c:a", "alac"))):
            with self.subTest(name):
                path = self.dir / name
                _ffmpeg("-f", "lavfi", "-i", "aevalsrc=exprs='0.5*sin(2*PI*440*t)':d=2:s=44100", "-ac", "2",
                        *extra, str(path))
                if name == "t.m4a":
                    data = path.read_bytes()
                    self.assertGreater(data.find(b"moov"), data.find(b"mdat"))     # the index is at the end
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    self.assertTrue(audio_identity.same_audio(audio_identity.compute_fd(fd, timeout=60),
                                                              audio_identity.compute(path, timeout=60)))
                finally:
                    os.close(fd)

    def test_what_is_not_an_open_regular_file_is_refused(self):
        fd = os.open(self.dir, os.O_RDONLY)
        try:
            with self.assertRaisesRegex(audio_identity.IdentityUnavailable, "not a regular file"):
                audio_identity.compute_fd(fd)
        finally:
            os.close(fd)
        closed = os.open(_tone(self.dir / "closed.mp3"), os.O_RDONLY)
        os.close(closed)
        self.assertEqual(audio_identity.compute_fd_or_none(closed), (None, "not an open file"))
        broken = self.dir / "broken.mp3"
        broken.write_bytes(b"\xff\xfb not an mp3 at all " * 400)
        fd = os.open(broken, os.O_RDONLY)
        try:
            identity, reason = audio_identity.compute_fd_or_none(fd, timeout=60)
        finally:
            os.close(fd)
        self.assertIsNone(identity)
        self.assertTrue(reason, reason)


# --------------------------------------------------------------------------- Codex finding 2: mixed decoder versions

class MixedDecoderVersionTests(_Audio):
    """A disagreement is only conclusive between identities made by the same ffmpeg."""

    def test_same_version_matching_identities_need_no_decode(self):
        a, b = self.audio_pair("samematch")
        self.cached(a)
        self.cached(b)
        self.decoded.clear()
        self.scan(verify=25)
        self.assertEqual(self.finding(a, b)["confidence"], "audio_equivalent")
        self.assertEqual((self.decoded, self.audio_budget()["pairs_attempted"]), ([], 0))

    def test_same_version_differing_identities_are_a_real_mismatch(self):
        a, b = self.audio_pair("samediff")
        self.cached(a)
        self.cached(b, sha256="1" * 64)                    # the running ffmpeg's own (cached) disagreement
        self.decoded.clear()
        self.scan(verify=25)
        finding = self.finding(a, b)
        self.assertEqual((finding["confidence"], finding["audio_verification"]["status"]), ("uncertain", "different"))
        self.assertEqual((self.decoded, self.audio_budget()["pairs_attempted"]), ([], 0))
        self.assert_not_resolvable(a, b)

    def test_an_old_cache_disagreeing_with_a_fresh_decode_is_decoded_again_and_matches(self):
        """a has no cache (decoded now); b's cache, from an older ffmpeg, records another digest for the same
        audio.  Uncorrected, the fresh decode against the old cache was declared different."""
        a, b = self.audio_pair("mixmatch")
        self.cached(b, sha256="2" * 64, decoder=OLD_DECODER)
        self.decoded.clear()
        self.scan(verify=25)
        finding = self.finding(a, b)
        self.assertEqual(finding["confidence"], "audio_equivalent", finding["audio_verification"])
        self.assertEqual({f["identity_source"] for f in finding["audio_verification"]["files"]}, {"decoded"})
        self.assertEqual(sorted(p for p, _t in self.decoded), sorted([a.filepath, b.filepath]))
        self.assertEqual((self.audio_budget()["pairs_attempted"], self.audio_budget()["decodes"]), (1, 2))
        self.assertTrue(self.findings_api()[(a.pk, b.pk)]["resolution"]["deletable"])

    def test_an_old_cache_decoded_again_can_establish_a_real_mismatch(self):
        a, b = self.audio_pair("mixdiff", differ=True)
        self.cached(b, sha256="3" * 64, decoder=OLD_DECODER)
        self.decoded.clear()
        self.scan(verify=25)
        finding = self.finding(a, b)
        self.assertEqual((finding["confidence"], finding["audio_verification"]["status"]), ("uncertain", "different"))
        self.assertEqual({f["identity_source"] for f in finding["audio_verification"]["files"]}, {"decoded"})
        self.assertEqual(len(self.decoded), 2)
        self.assert_not_resolvable(a, b)

    def test_an_old_cache_matching_a_fresh_decode_is_not_decoded_again(self):
        a, b = self.audio_pair("oldmatch")
        self.cached(b, decoder=OLD_DECODER)                 # same digest, older ffmpeg: an exact match stands
        self.decoded.clear()
        self.scan(verify=25)
        self.assertEqual(self.finding(a, b)["confidence"], "audio_equivalent")
        self.assertEqual([p for p, _t in self.decoded], [a.filepath])

    def test_a_needed_redecode_that_cannot_run_leaves_the_pair_unverified(self):
        for limit in ("pairs", "decodes", "time"):
            with self.subTest(limit):
                Track.objects.all().delete()
                a, b = self.audio_pair(f"short{limit}")
                self.cached(b, sha256="4" * 64, decoder=OLD_DECODER)
                scanner = reconciliation.Scanner(verify_audio_pairs=1)
                patcher = None
                if limit == "pairs":
                    scanner.verify_pairs_attempted = 1      # the scan's only slot already taken
                elif limit == "decodes":
                    scanner.verify_max_decodes = 1          # room for a's decode only, not b's again
                else:
                    clock = _Clock()
                    scanner.clock = clock
                    real = getattr(audio_identity, DECODE_ENTRY)

                    def slow(source, **kwargs):
                        clock.now += reconciliation.VERIFY_AUDIO_MAX_SECONDS   # a's decode uses all the time
                        return real(source, **kwargs)
                    patcher = mock.patch.object(audio_identity, DECODE_ENTRY, side_effect=slow)
                    patcher.start()
                try:
                    self.report = scanner.run()
                finally:
                    if patcher is not None:
                        patcher.stop()
                reconciliation.write_report(self.report)          # what the findings API and preview read
                finding = self.finding(a, b)
                self.assertEqual((finding["confidence"], finding["audio_verification"]["status"]),
                                 ("likely", "unverified"), finding["audio_verification"])
                self.assertIn("budget was used up" if limit != "time" else "time was used up",
                              finding["audio_verification"]["reason"])
                self.assertEqual(self.audio_budget()["budget_exhausted"], 1)
                self.assertEqual(self.audio_budget()["different"], 0)
                self.assert_not_resolvable(a, b)

    def test_without_verification_a_cross_version_disagreement_is_inconclusive(self):
        a, b = self.audio_pair("noverify")
        self.cached(a)
        self.cached(b, sha256="5" * 64, decoder=OLD_DECODER)
        self.decoded.clear()
        self.scan()
        finding = self.finding(a, b)
        self.assertEqual(finding["confidence"], "likely")
        self.assertIn("different decoder versions", finding["audio_verification"]["reason"])
        self.assertEqual(self.decoded, [])
        self.assert_not_resolvable(a, b)


# --------------------------------------------------------------------------- Codex finding 3: the pair limit

class PairLimitTests(_Audio):
    """``verify_audio_pairs=N`` limits the pairs that take fresh verification, not the decoder invocations."""

    def half_cached(self, name):
        a, b = self.audio_pair(name)
        self.cached(a)
        return a, b

    def test_two_half_cached_pairs_with_a_limit_of_one(self):
        """Codex: a 2 x N decode budget let one requested pair verify two half-cached pairs."""
        pairs = [self.half_cached(f"half{i}") for i in range(2)]
        self.decoded.clear()
        self.report = reconciliation.Scanner(verify_audio_pairs=1).run()
        confidences = sorted(self.finding(a, b)["confidence"] for a, b in pairs)
        self.assertEqual(confidences, ["audio_equivalent", "likely"])
        self.assertEqual(len(self.decoded), 1)
        budget = self.audio_budget()
        self.assertEqual((budget["requested_pairs"], budget["pairs_attempted"], budget["decodes"],
                          budget["verified"], budget["skipped_pair_limit"], budget["budget_exhausted"]),
                         (1, 1, 1, 1, 1, 1))
        skipped = next(self.finding(a, b) for a, b in pairs if self.finding(a, b)["confidence"] == "likely")
        self.assertIn("at most 1 pair(s) needing a decode", skipped["audio_verification"]["reason"])
        self.assertTrue(any("1 by the limit of 1 pair(s)" in e for e in self.report["errors"]), self.report["errors"])

    def test_a_fully_cached_pair_takes_no_slot(self):
        cached_a, cached_b = self.audio_pair("full")
        self.cached(cached_a)
        self.cached(cached_b)
        fresh_a, fresh_b = self.audio_pair("fresh")
        self.decoded.clear()
        self.report = reconciliation.Scanner(verify_audio_pairs=1).run()
        self.assertEqual(self.finding(cached_a, cached_b)["confidence"], "audio_equivalent")
        self.assertEqual(self.finding(fresh_a, fresh_b)["confidence"], "audio_equivalent")
        budget = self.audio_budget()
        self.assertEqual((budget["pairs_attempted"], budget["decodes"], budget["budget_exhausted"]), (1, 2, 0))

    def test_a_pair_needing_two_decodes_takes_one_slot(self):
        a, b = self.audio_pair("two")
        self.report = reconciliation.Scanner(verify_audio_pairs=1).run()
        self.assertEqual(self.finding(a, b)["confidence"], "audio_equivalent")
        self.assertEqual((self.audio_budget()["pairs_attempted"], self.audio_budget()["decodes"]), (1, 2))

    def test_an_exhausted_decode_limit_skips_the_pair_whole(self):
        a, b = self.audio_pair("declimit")
        scanner = reconciliation.Scanner(verify_audio_pairs=1)
        scanner.verify_max_decodes = 1                      # independent of the pair limit
        self.report = scanner.run()
        finding = self.finding(a, b)
        self.assertEqual(finding["confidence"], "likely")
        self.assertIn("(decode limit)", finding["audio_verification"]["reason"])
        budget = self.audio_budget()
        self.assertEqual((budget["pairs_attempted"], budget["decodes"], budget["skipped_decode_limit"]), (0, 0, 1))
        self.assertEqual(self.decoded, [])

    def test_a_cross_version_redecode_counts_against_the_pair_limit(self):
        old_a, old_b = self.audio_pair("cross")
        self.cached(old_a)
        self.cached(old_b, sha256="6" * 64, decoder=OLD_DECODER)
        other = self.half_cached("other")
        self.decoded.clear()
        self.report = reconciliation.Scanner(verify_audio_pairs=1).run()
        budget = self.audio_budget()
        self.assertEqual((budget["pairs_attempted"], budget["decodes"], budget["verified"],
                          budget["skipped_pair_limit"]), (1, 1, 1, 1))
        self.assertEqual(sorted([self.finding(old_a, old_b)["confidence"], self.finding(*other)["confidence"]]),
                         ["audio_equivalent", "likely"])

    def test_findings_sharing_a_decoded_file(self):
        """The shared file is decoded once; a later pair needing no new decode takes no slot, one that needs one
        does."""
        shared = self.registered(_tone(self.root / "ROCK" / "shared.mp3"), title="Shared", isrc="USRC10000002")
        b = self.registered(_retag(Path(shared.filepath), self.root / "POP" / "shared-b.mp3", title="x"),
                            title="Shared")
        c = self.registered(_retag(Path(shared.filepath), self.root / "POP" / "shared-c.mp3", title="y"),
                            title="Other title", isrc="USRC10000002")
        self.report = reconciliation.Scanner(verify_audio_pairs=1).run()
        verified = [o for o in (b, c) if self.finding(shared, o)["confidence"] == "audio_equivalent"]
        self.assertEqual(len(verified), 1)                  # the second pair needs c decoded: no slot left
        self.assertEqual(len(self.decoded), 2)
        self.assertEqual((self.audio_budget()["pairs_attempted"], self.audio_budget()["decodes"]), (1, 2))
        self.cached(c)
        self.decoded.clear()
        self.report = reconciliation.Scanner(verify_audio_pairs=1).run()
        self.assertEqual([self.finding(shared, o)["confidence"] for o in (b, c)], ["audio_equivalent"] * 2)
        self.assertEqual((self.audio_budget()["pairs_attempted"], self.audio_budget()["decodes"]), (1, 2))
        self.assertEqual(sorted(p for p, _t in self.decoded), sorted([shared.filepath, b.filepath]))


# --------------------------------------------------------------------------- Codex finding 4: deadlines

class DeadlineTests(TestCase):
    """One deadline per file shared by ffprobe and ffmpeg; nothing starts once it has passed."""

    def setUp(self):
        import tempfile
        self.dir = Path(tempfile.mkdtemp(prefix="isadoraair-deadline-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.dir)], check=False))
        self.path = _tone(self.dir / "tone.mp3")
        self.clock = _Clock()
        self.timers = []
        real_timer = threading.Timer

        def timer(interval, function):
            self.timers.append(interval)
            return real_timer(interval, function)
        fake_threading = mock.Mock(wraps=threading, Timer=timer, Thread=threading.Thread, Event=threading.Event)
        for patcher in (mock.patch.object(audio_identity, "time", self.clock),
                        mock.patch.object(audio_identity, "threading", fake_threading)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def probing_takes(self, seconds):
        def on_run(cmd, kwargs):
            if cmd[0] == "ffprobe":
                self.probe_timeout = kwargs.get("timeout")
                self.clock.now += seconds
        return _SubprocessProxy(on_run=on_run)

    def test_probe_time_reduces_the_decode_allowance(self):
        for compute, arg in ((audio_identity.compute, lambda: self.path), (audio_identity.compute_fd, None)):
            with self.subTest(compute.__name__):
                self.timers.clear()
                fd = os.open(self.path, os.O_RDONLY) if arg is None else None
                try:
                    with mock.patch.object(audio_identity, "subprocess", self.probing_takes(250)):
                        identity = compute(fd if arg is None else arg(), timeout=300)
                finally:
                    if fd is not None:
                        os.close(fd)
                self.assertTrue(audio_identity.well_formed(identity))
                self.assertEqual(self.probe_timeout, audio_identity.PROBE_TIMEOUT_SECONDS)
                self.assertEqual(self.timers, [50])          # 300 - 250, not a fresh 300

    def test_the_probe_gets_no_more_than_the_whole_allowance(self):
        with mock.patch.object(audio_identity, "subprocess", self.probing_takes(0)):
            audio_identity.compute(self.path, timeout=30)
        self.assertEqual((self.probe_timeout, self.timers), (30, [30]))

    def test_an_exhausted_allowance_starts_no_decoder(self):
        proxy = self.probing_takes(300)
        with mock.patch.object(audio_identity, "subprocess", proxy):
            identity, reason = audio_identity.compute_or_none(self.path, timeout=300)
        self.assertIsNone(identity)
        self.assertIn("ran out before decoding", reason)
        self.assertEqual([kind for kind, cmd, _kw in proxy.calls if cmd[0] == "ffmpeg" and "-version" not in cmd], [])
        self.assertEqual(self.timers, [])

    def test_no_time_at_all_starts_nothing(self):
        proxy = _SubprocessProxy()
        with mock.patch.object(audio_identity, "subprocess", proxy):
            identity, reason = audio_identity.compute_or_none(self.path, timeout=0)
        self.assertIsNone(identity)
        self.assertIn("ran out before probing", reason)
        self.assertEqual([c for c in proxy.calls if c[1][0] in ("ffprobe", "ffmpeg") and "-version" not in c[1]], [])

    def test_a_decoder_timeout_never_yields_an_identity_and_the_child_is_reaped(self):
        """A real child that outlives the allowance (it never ends by itself) is killed at the deadline."""
        children = []

        def stubborn(cmd, kwargs):
            if cmd[0] == "ffmpeg":
                return ["sleep", "30"], kwargs
        proxy = _SubprocessProxy(on_popen=stubborn)
        real_popen = proxy.Popen

        def popen(cmd, **kwargs):
            child = real_popen(cmd, **kwargs)
            children.append(child)
            return child
        proxy.Popen = popen
        with mock.patch.object(audio_identity, "time", time), mock.patch.object(audio_identity, "subprocess", proxy):
            started = time.monotonic()
            identity, reason = audio_identity.compute_or_none(self.path, timeout=1.5)
            elapsed = time.monotonic() - started
        self.assertIsNone(identity)
        self.assertIn("took longer than", reason)
        self.assertLess(elapsed, 5)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)         # killed and reaped


class ScanDeadlineTests(_Audio):
    def test_the_scan_total_bounds_each_decode_and_an_exhausted_total_starts_no_other(self):
        a, b = self.audio_pair("total")
        c, d = self.audio_pair("total2")
        clock = _Clock()
        scanner = reconciliation.Scanner(verify_audio_pairs=25, clock=clock)
        real = getattr(audio_identity, DECODE_ENTRY)
        timeouts = []

        def spending(source, **kwargs):
            timeouts.append(kwargs["timeout"])
            # each decode "takes" 1,000 s of the scan's 1,800
            clock.now += 1000
            return real(source, **kwargs)
        with mock.patch.object(audio_identity, DECODE_ENTRY, side_effect=spending):
            self.report = scanner.run()
        statuses = sorted(self.finding(x, y)["audio_verification"]["status"] for x, y in ((a, b), (c, d)))
        self.assertEqual(statuses, ["unverified", "verified"])
        # First decode: the per-file 300 s; second: what the total had left (800 s) capped at 300; then 1800 - 2000
        # < 0: no third probe or decode.
        self.assertEqual(timeouts, [300, 300])
        budget = self.audio_budget()
        self.assertEqual((budget["decodes"], budget["skipped_time_limit"]), (2, 1))
        clock.now = 0
        scanner = reconciliation.Scanner(verify_audio_pairs=25, clock=clock)
        timeouts.clear()

        def late(source, **kwargs):
            timeouts.append(kwargs["timeout"])
            clock.now += reconciliation.VERIFY_AUDIO_MAX_SECONDS - 10   # leaves 10 s for the next file
            return real(source, **kwargs)
        with mock.patch.object(audio_identity, DECODE_ENTRY, side_effect=late):
            self.report = scanner.run()
        self.assertEqual(timeouts, [300, 10])               # the scan's remainder, not a fresh 300

    def test_a_decoder_timeout_in_the_scan_is_never_proof(self):
        a, b = self.audio_pair("timeout")

        def stubborn(cmd, kwargs):
            if cmd[0] == "ffmpeg":
                return ["sleep", "30"], kwargs
        with mock.patch.object(reconciliation, "VERIFY_DECODE_TIMEOUT_SECONDS", 1.5), \
                mock.patch.object(audio_identity, "subprocess", _SubprocessProxy(on_popen=stubborn)):
            self.scan(verify=25)
        finding = self.finding(a, b)
        self.assertEqual(finding["confidence"], "likely")
        self.assertIn("took longer than", finding["audio_verification"]["reason"])
        self.assert_not_resolvable(a, b)


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
