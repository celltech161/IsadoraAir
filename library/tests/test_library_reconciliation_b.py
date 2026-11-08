"""Roadmap 3.11 Phase B: read-only Library reconciliation (scan, report, review page).

Every file lives in a per-test temporary Library root; LIBRARY_ROOT, WAVEFORMS_DIR and REPORTS_ROOT point
there, so no station media, cache or report is read or written."""
import json
import os
import re
import subprocess
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import models
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from library.models import Artist, Category, CategoryKind, DuplicateCandidate, Track
from library.services import library_reconciliation as reconciliation

User = get_user_model()


class _ReconFixture:
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / "music"
        self.waves = self.base / "waveforms"
        self.reports = self.base / "reports"
        for d in (self.root, self.waves, self.reports):
            d.mkdir()
        settings = override_settings(LIBRARY_ROOT=str(self.root), WAVEFORMS_DIR=str(self.waves),
                                     REPORTS_ROOT=str(self.reports), SECURE_SSL_REDIRECT=False)
        settings.enable()
        self.addCleanup(settings.disable)
        # Fixture files are written moments before the scan; the "may still be arriving" rule is exercised
        # explicitly (RecentFileTests stops this patch).
        self.fresh = mock.patch.object(reconciliation, "FRESH_SECONDS", 0)
        self.fresh.start()
        self.addCleanup(self.fresh.stop)
        from authz.models import Capability
        Capability.objects.get_or_create(slug="library.manage_tracks", defaults={"label": "Manage tracks"})
        kind, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        self.rock = Category.objects.create(code="ROCK", name="Rock", kind=kind)
        self.pop = Category.objects.create(code="POP", name="Pop", kind=kind)
        self.artist = Artist.objects.create(name="Recon Artist")
        self.admin = User.objects.create_superuser("recon-admin", "recon@example.invalid", "pw")
        self.client.force_login(self.admin)

    def file(self, rel, data=b"\x00audio-bytes" * 64):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def track(self, rel, *, title=None, category=None, create_file=True, data=None, **fields):
        path = self.root / rel
        if create_file:
            self.file(rel, data if data is not None else f"audio of {rel}".encode() * 50)
        return Track.objects.create(
            filepath=str(path), filename=path.name, title=title or path.stem, artist=fields.pop("artist", self.artist),
            category=category or self.rock, format=path.suffix.lstrip("."), duration_seconds=fields.pop("duration", 180.0),
            **fields)

    def scan(self, **kwargs):
        return reconciliation.Scanner(**kwargs).run()

    @staticmethod
    def of(report, kind):
        return [f for f in report["findings"] if f["type"] == kind]


class _Recon(_ReconFixture, TestCase):
    pass


class ClassificationTests(_Recon):
    def test_healthy_missing_unregistered_drift_and_unsupported(self):
        healthy = self.track("ROCK/healthy.flac")
        missing = self.track("ROCK/gone.flac", create_file=False)
        drifted = self.track("POP/drifted.flac", category=self.rock)
        self.file("ROCK/stray.mp3")
        self.file("ROCK/cover.jpg")
        self.file("notes.txt")
        report = self.scan()
        self.assertEqual(report["status"], "complete")
        counts = report["counts"]
        self.assertEqual((counts["healthy"], counts["tracks_total"], counts["files_total"]), (2, 3, 3))
        self.assertEqual([f["track"]["id"] for f in self.of(report, "missing")], [missing.pk])
        self.assertEqual([f["file"]["rel"] for f in self.of(report, "unregistered")], ["ROCK/stray.mp3"])
        drift = self.of(report, "category_drift")
        self.assertEqual([f["track"]["id"] for f in drift], [drifted.pk])
        self.assertIn("'POP'", drift[0]["reason"])
        self.assertEqual(report["unsupported_extensions"], {".jpg": 1, ".txt": 1})
        self.assertNotIn(healthy.pk, [f.get("track", {}).get("id") for f in report["findings"]])

    def test_symlinks_are_not_followed_and_paths_outside_the_root_are_protected(self):
        outside = self.base / "elsewhere"
        outside.mkdir()
        (outside / "secret.flac").write_bytes(b"outside audio" * 40)
        os.symlink(outside, self.root / "ROCK-link")
        os.symlink(outside / "secret.flac", self.root / "linked.flac")
        foreign = Track.objects.create(filepath=str(outside / "secret.flac"), filename="secret.flac", title="x",
                                       artist=self.artist, category=self.rock)
        alias = Track.objects.create(filepath=str(self.root / "ROCK-link" / "secret.flac"), filename="secret.flac",
                                     title="y", artist=self.artist, category=self.rock)
        report = self.scan()
        uncertain = {f["file"]["rel"]: f["reason"] for f in self.of(report, "uncertain") if f.get("file")}
        self.assertIn("ROCK-link", uncertain)
        self.assertIn("linked.flac", uncertain)
        self.assertTrue(all("not followed" in r for r in uncertain.values()))
        self.assertEqual(report["counts"]["files_total"], 0)            # nothing outside was walked
        protected = {f["track"]["id"]: f["reason"] for f in self.of(report, "protected")}
        self.assertIn("outside the managed Library root", protected[foreign.pk])
        shared = {f["track"]["id"] for f in self.of(report, "shared_source") if f.get("track")}
        self.assertIn(alias.pk, shared)

    def test_hardlinks_are_a_shared_source_not_duplicates_or_strays(self):
        first = self.track("ROCK/one.flac")
        os.link(first.filepath, self.root / "ROCK" / "one-alias.flac")
        second = self.track("ROCK/two.flac")
        os.unlink(second.filepath)
        os.link(first.filepath, second.filepath)
        report = self.scan()
        self.assertEqual(self.of(report, "unregistered"), [])
        self.assertEqual(self.of(report, "duplicate"), [])
        shared = self.of(report, "shared_source")
        self.assertIn(sorted([first.pk, second.pk]), [f.get("track_ids") for f in shared])
        self.assertIn("ROCK/one-alias.flac", [f.get("file", {}).get("rel") for f in shared])

    def test_empty_unreadable_hidden_and_special_entries_are_uncertain_or_skipped(self):
        self.file("ROCK/empty.flac", b"")
        locked = self.root / "LOCKED"
        locked.mkdir()
        self.file("LOCKED/inside.flac")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        self.file(".am-quarantine/held.flac")
        os.mkfifo(self.root / "ROCK" / "pipe.flac")
        report = self.scan()
        reasons = {f["file"]["rel"]: f["reason"] for f in self.of(report, "uncertain") if f.get("file")}
        self.assertIn("empty", reasons["ROCK/empty.flac"])
        self.assertIn("cannot be listed", reasons["LOCKED"])
        self.assertIn("not a regular file", reasons["ROCK/pipe.flac"])
        self.assertEqual(report["counts"]["hidden_entries"], 1)
        self.assertNotIn(".am-quarantine/held.flac", json.dumps(report["findings"]))

    def test_protected_tracks_and_files_are_reported_not_reconciled(self):
        remote = self.track("ROCK/remote.flac", create_file=False, remote_url="https://example.invalid/a.mp3")
        self.file("SPOKEN/spoken-" + "a" * 32 + ".flac")
        report = self.scan()
        protected = self.of(report, "protected")
        self.assertIn(remote.pk, [f.get("track", {}).get("id") for f in protected])
        self.assertIn("SPOKEN/spoken-" + "a" * 32 + ".flac", [f.get("file", {}).get("rel") for f in protected])
        self.assertEqual(self.of(report, "missing"), [])
        self.assertEqual(self.of(report, "unregistered"), [])

    def test_a_missing_track_with_files_of_its_name_is_a_moved_candidate_for_manual_review(self):
        gone = self.track("ROCK/moved.flac", create_file=False)
        self.file("POP/moved.flac")
        report = self.scan()
        moved = self.of(report, "moved")
        self.assertEqual(len(moved), 1)
        self.assertIsNone(moved[0]["relink"])
        self.assertEqual(moved[0]["track"]["id"], gone.pk)
        self.assertEqual([c["rel"] for c in moved[0]["candidates"]], ["POP/moved.flac"])
        self.assertIn("no authoritative waveform cache", " ".join(moved[0]["candidates"][0]["reasons"]))
        self.assertTrue(self.of(report, "unregistered")[0]["relink_candidate"] is False)

    def test_too_many_candidates_are_not_compared(self):
        self.track("ROCK/common.flac", create_file=False)
        for i in range(reconciliation.MAX_CANDIDATES + 1):
            self.file(f"DIR{i}/common.flac")
        moved = self.of(self.scan(), "moved")[0]
        self.assertIn("too many", moved["reason"])
        self.assertEqual(len(moved["candidates"]), reconciliation.MAX_CANDIDATES)
        self.assertIsNone(moved["relink"])


class RecentFileTests(_Recon):
    def test_recently_changed_files_may_still_be_arriving(self):
        self.fresh.stop()
        self.track("ROCK/registered-now.flac")
        self.file("ROCK/arriving.flac")
        report = self.scan()
        reasons = {f.get("file", {}).get("rel") for f in self.of(report, "in_delivery")}
        self.assertEqual(reasons, {"ROCK/registered-now.flac", "ROCK/arriving.flac"})
        self.assertEqual(self.of(report, "unregistered"), [])
        self.assertEqual(report["counts"]["healthy"], 0)
        self.fresh.start()


class StorageAndPersistenceTests(_Recon):
    def test_an_unavailable_root_fails_visibly_and_keeps_the_last_good_report(self):
        self.track("ROCK/a.flac")
        good = reconciliation.run_scan()
        self.assertEqual(good["status"], "complete")
        with override_settings(LIBRARY_ROOT=str(self.base / "not-mounted")):
            with self.assertRaisesRegex(CommandError, "unavailable.*nothing was reported"):
                call_command("reconcile_library", stdout=StringIO())
        status = reconciliation.scan_status()
        self.assertEqual(status["latest"]["scan_id"], good["scan_id"])
        self.assertEqual(status["last_attempt"]["status"], "failed")
        self.assertEqual(reconciliation.load_report()["scan_id"], good["scan_id"])

    def test_an_empty_mount_is_suspect_and_reports_no_track_as_missing(self):
        for i in range(25):
            self.track(f"ROCK/t{i}.flac", create_file=False)
        report = reconciliation.run_scan()
        self.assertEqual(report["status"], "suspect")
        self.assertIn("unmounted", report["status_reason"])
        self.assertEqual(report["counts"]["missing"], 0)
        self.assertEqual(report["counts"]["uncertain"], 25)
        self.assertEqual(reconciliation.scan_status()["latest"]["status"], "suspect")

    def test_the_scan_changes_nothing(self):
        kept = self.track("ROCK/keep.flac", waveform_path="")
        self.track("ROCK/missing.flac", create_file=False)
        self.file("ROCK/stray.flac")
        (self.waves / f"{kept.pk}.json").write_text("{}")
        DuplicateCandidate.objects.create(track_a=kept, track_b=Track.objects.exclude(pk=kept.pk).first(),
                                          confidence="probable")
        rows = list(Track.objects.order_by("pk").values())
        candidates = list(DuplicateCandidate.objects.values())
        files = sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in self.base.rglob("*")
                       if p.is_file() and self.reports not in p.parents)
        reconciliation.run_scan()
        self.assertEqual(list(Track.objects.order_by("pk").values()), rows)
        self.assertEqual(list(DuplicateCandidate.objects.values()), candidates)
        self.assertEqual(sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in self.base.rglob("*")
                                if p.is_file() and self.reports not in p.parents), files)

    def test_one_scan_at_a_time_and_an_interrupted_scan_is_not_presented_as_finished(self):
        with reconciliation.scan_lock():
            with self.assertRaises(reconciliation.ScanBusy):
                reconciliation.run_scan()
            self.assertTrue(reconciliation._lock_holder_alive(reconciliation.report_dir()))
        (reconciliation.report_dir() / "running.json").write_text(json.dumps({"phase": "walk", "done": 4000}))
        status = reconciliation.scan_status()
        self.assertFalse(status["running"]["alive"])
        self.assertIsNone(status["latest"])
        self.assertIsNone(reconciliation.load_report())

    def test_the_command_reports_counts(self):
        self.track("ROCK/a.flac")
        self.file("ROCK/b.flac")
        out = StringIO()
        call_command("reconcile_library", stdout=out)
        self.assertIn("unregistered: 1", out.getvalue())
        self.assertIn("/library/reconciliation/", out.getvalue())


class ReviewPageTests(_Recon):
    def user_in(self, group_name):
        user = User.objects.create_user(f"u-{group_name}", f"{group_name}@example.invalid", "pw")
        group, _ = Group.objects.get_or_create(name=group_name)
        user.groups.add(group)
        return user

    def test_only_track_managers_reach_the_page_and_its_data(self):
        reconciliation.run_scan()
        for group in ("Contributor", "remote_dj", "No Capability Group"):
            self.client.force_login(self.user_in(group))
            for name in ("library:library-reconciliation", "library:api-reconciliation-summary",
                         "library:api-reconciliation-findings"):
                with self.subTest(group=group, name=name):
                    # Denied by the capability check (403) or, for a page, by GroupAccess (redirect).
                    self.assertIn(self.client.get(reverse(name)).status_code, (302, 403))
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("library:library-reconciliation")).status_code, 200)

    def test_findings_are_filtered_searched_and_paginated(self):
        for i in range(30):
            self.track(f"ROCK/missing-{i:02d}.flac", create_file=False, title=f"Lost Song {i:02d}")
        for i in range(40):                 # fewer than half missing: not a suspect storage
            self.track(f"ROCK/ok-{i:02d}.flac")
        reconciliation.run_scan()
        body = self.client.get(reverse("library:api-reconciliation-findings"),
                               {"type": "missing", "per_page": 25}).json()
        self.assertEqual((body["total"], body["pages"], len(body["items"])), (30, 2, 25))
        body = self.client.get(reverse("library:api-reconciliation-findings"),
                               {"type": "missing", "per_page": 25, "page": 2}).json()
        self.assertEqual(len(body["items"]), 5)
        body = self.client.get(reverse("library:api-reconciliation-findings"), {"q": "lost song 07"}).json()
        self.assertEqual([f["track"]["title"] for f in body["items"]], ["Lost Song 07"])
        track_id = body["items"][0]["track"]["id"]
        body = self.client.get(reverse("library:api-reconciliation-findings"), {"q": str(track_id)}).json()
        self.assertEqual([f["track"]["id"] for f in body["items"]], [track_id])
        self.assertEqual(self.client.get(reverse("library:api-reconciliation-findings"),
                                         {"type": "bogus"}).status_code, 400)
        summary = self.client.get(reverse("library:api-reconciliation-summary")).json()
        self.assertEqual(summary["report"]["counts"]["missing"], 30)
        self.assertEqual(summary["report"]["counts"]["healthy"], 40)

    def test_no_report_yet_is_shown_as_such(self):
        summary = self.client.get(reverse("library:api-reconciliation-summary")).json()
        self.assertIsNone(summary["report"])
        body = self.client.get(reverse("library:api-reconciliation-findings")).json()
        self.assertEqual((body["items"], body["scan_id"]), ([], None))

    def test_the_library_page_links_managers_to_reconciliation(self):
        self.assertContains(self.client.get(reverse("library:library")), reverse("library:library-reconciliation"))
        self.client.force_login(self.user_in("Contributor"))
        self.assertNotContains(self.client.get(reverse("library:library")), reverse("library:library-reconciliation"))


def _analyze(test, track):
    """The station's own analysis of the Track's real file: a version-2 cache with its decoded identity."""
    from library.management.commands import analyze_tracks
    track = Track.objects.get(pk=track.pk)
    row = (track.pk, track.filepath, track.filename, track.duration_seconds, track.title, "A", track.related_artists)
    with mock.patch.object(analyze_tracks, "emit_event"):
        outcome = analyze_tracks.analyze_one_track(row, (4410, 0.05, 1000, -26.0, -45.0, 0.1), test.waves, force=True)
    test.assertTrue(outcome, getattr(outcome, "reason", ""))
    return Track.objects.get(pk=track.pk)


class DuplicateReviewTests(_Recon):
    def duplicate_groups(self, report):
        return self.of(report, "duplicate")

    def test_identical_bytes_are_exact_whatever_the_names_and_hardlinks_are_not(self):
        data = b"identical recording bytes" * 400
        a = self.track("ROCK/first.flac", data=data)
        self.file("POP/copy-of-first.flac", data)
        self.track("ROCK/other.flac", data=b"different bytes!" * 625)       # same size, other content
        report = self.scan()
        exact = [g for g in self.duplicate_groups(report) if g["confidence"] == "exact"]
        self.assertEqual(len(exact), 1)
        self.assertEqual({f["rel"] for f in exact[0]["files"]}, {"ROCK/first.flac", "POP/copy-of-first.flac"})
        self.assertEqual(exact[0]["members"], [a.pk])

    def test_a_stale_historical_file_hash_is_not_evidence(self):
        self.track("ROCK/a.flac", data=b"A" * 1000, file_hash="f" * 64)
        self.track("ROCK/b.flac", data=b"B" * 1001, file_hash="f" * 64)
        self.assertEqual(self.duplicate_groups(self.scan()), [])

    def test_metadata_groups_need_close_durations_and_isrc_collisions_stay_uncertain(self):
        a = self.track("ROCK/a.flac", title="Same Song", duration=200.0)
        b = self.track("POP/b.flac", title="  same   SONG ", duration=201.5)
        self.track("ROCK/c.flac", title="Same Song", duration=260.0)          # a different edit
        x = self.track("ROCK/x.flac", title="One", isrc="USRC17607839", duration=100.0)
        y = self.track("ROCK/y.flac", title="Two", isrc="usrc17607839", duration=300.0)
        groups = self.duplicate_groups(self.scan())
        likely = [g for g in groups if g["confidence"] == "likely"]
        self.assertEqual([g["members"] for g in likely], [sorted([a.pk, b.pk])])
        collision = [g for g in groups if g["confidence"] == "uncertain" and "ISRC" in g["reason"]]
        self.assertEqual([g["members"] for g in collision], [sorted([x.pk, y.pk])])

    def test_large_metadata_groups_are_reported_once_without_pairs(self):
        for i in range(reconciliation.DUPLICATE_GROUP_LIMIT + 5):
            self.track(f"ROCK/intro-{i}.flac", title="Station ID", duration=10.0)
        groups = self.duplicate_groups(self.scan())
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["confidence"], "uncertain")
        self.assertEqual(len(groups[0]["members"]), reconciliation.DUPLICATE_GROUP_LIMIT + 5)
        self.assertEqual(groups[0]["decisions"], {})

    def test_decoded_audio_decides_between_audio_equivalent_and_different_recordings(self):
        from library.tests.test_waveform_integrity_package_b import _audio
        for folder in ("ROCK", "POP"):
            (self.root / folder).mkdir(exist_ok=True)
        # The same recording in two containers: a WAV, and a FLAC made losslessly from it.
        same_wav = _audio(self.root / "POP" / "song.wav", freq=440)
        same_flac = self.root / "ROCK" / "song.flac"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(same_wav), str(same_flac)],
                       check=True, stdin=subprocess.DEVNULL)
        other = _audio(self.root / "POP" / "song-live.flac", freq=660)
        tracks = []
        for path, title in ((same_flac, "Equal"), (same_wav, "Equal"), (other, "Differs")):
            tracks.append(Track.objects.create(filepath=str(path), filename=path.name, title=title, artist=self.artist,
                                               category=self.rock, duration_seconds=3.0, format=path.suffix[1:]))
        twin = self.track("ROCK/differs-twin.flac", title="Differs", duration=3.0)
        _audio(Path(twin.filepath), freq=880)
        for track in (*tracks, twin):
            _analyze(self, track)
        groups = {tuple(g["members"]): g for g in self.duplicate_groups(self.scan())}
        self.assertEqual(groups[tuple(sorted([tracks[0].pk, tracks[1].pk]))]["confidence"], "audio_equivalent")
        differs = groups[tuple(sorted([tracks[2].pk, twin.pk]))]
        self.assertEqual(differs["confidence"], "uncertain")
        self.assertIn("decoded audio differs", differs["reason"])

    def test_existing_decisions_are_shown_and_a_keep_both_pair_stays_closed(self):
        a = self.track("ROCK/a.flac", title="Twin", duration=100.0)
        b = self.track("POP/b.flac", title="Twin", duration=100.5)
        c = self.track("ROCK/c.flac", title="Triplet", duration=50.0)
        d = self.track("POP/d.flac", title="Triplet", duration=50.0)
        DuplicateCandidate.objects.create(track_a=a, track_b=b, confidence="probable", resolution="keep_both")
        DuplicateCandidate.objects.create(track_a=c, track_b=d, confidence="probable", resolution="keep_a")
        groups = {tuple(g["members"]): g for g in self.duplicate_groups(self.scan())}
        closed = groups[(a.pk, b.pk)]
        self.assertFalse(closed["open"])
        self.assertEqual(closed["decisions"], {f"{a.pk}-{b.pk}": {"resolution": "keep_both", "applied": False}})
        pending = groups[(c.pk, d.pk)]
        self.assertTrue(pending["open"])
        self.assertEqual(pending["pending_deletion_decisions"], [f"{c.pk}-{d.pk}"])
        self.assertEqual(DuplicateCandidate.objects.get(track_a=a, track_b=b).resolution, "keep_both")


class DuplicateBoundingTests(_Recon):
    class DecisionRows:
        def __init__(self, rows):
            self.rows = rows
            self.filters = []

        def filter(self, **kwargs):
            self.filters.append(kwargs)
            return self

        def values_list(self, *fields):
            self.fields = fields
            return self

        def iterator(self, **kwargs):
            yield from self.rows

        def __iter__(self):
            yield from self.rows

    def test_groups_of_30_1000_and_several_thousand_consult_only_recorded_decisions(self):
        groups = [
            {"confidence": "uncertain", "track_ids": list(range(1, 31))},
            {"confidence": "uncertain", "track_ids": list(range(10_000, 11_000))},
            {"confidence": "uncertain", "track_ids": list(range(20_000, 25_000))},
            {"confidence": "exact", "files": [
                {"track_id": track_id} for track_id in range(30_000, 31_000)
            ]},
        ]
        rows = self.DecisionRows([
            (1, 2, "keep_both", False),
            (20_000, 20_001, "keep_both", False),
            (20_001, 20_002, "keep_a", False),
            (20_002, 20_003, "keep_b", True),
            (1, 10_000, "unresolved", False),             # both ids exist, but not in one group
        ])
        candidate = type("Candidate", (), {"objects": rows})
        scanner = reconciliation.Scanner()
        scanner.attach_decisions(groups, candidate)

        self.assertEqual([g["member_count"] for g in groups], [30, 1000, 5000, 1000])
        self.assertEqual(groups[2]["pair_count"], 12_497_500)
        self.assertTrue(all(g["open"] for g in groups))
        self.assertEqual(groups[2]["decision_count"], 3)
        self.assertEqual(groups[2]["keep_both_decision_count"], 1)
        self.assertEqual(groups[2]["pending_deletion_decisions"], ["20001-20002"])
        self.assertEqual(groups[2]["pending_deletion_decision_count"], 1)
        self.assertEqual(scanner.duplicate_decision_rows, 5)
        self.assertEqual(scanner.duplicate_decision_group_links, 4)
        self.assertEqual(len(rows.filters), 1)

    def test_large_duplicate_http_detail_is_explicitly_bounded_with_exact_totals(self):
        members = list(range(1, 5001))
        decisions = {f"{i}-{i + 1}": {"resolution": "keep_a", "applied": False}
                     for i in range(1, 1001)}
        files = [{"rel": f"ROCK/{i:05d}-{'x' * 80}.flac", "size": 1234, "track_id": i,
                  "sha256": "a" * 64} for i in members]
        report = {
            "schema": reconciliation.SCHEMA, "schema_version": reconciliation.SCHEMA_VERSION,
            "scan_id": "20261102T120000-deadbeef", "status": "complete", "finished_at": "now",
            "counts": {"duplicate": 1}, "findings": [{
                "id": "F000001", "type": "duplicate", "reason": "large exact group", "confidence": "exact",
                "members": members, "member_count": len(members), "pair_count": len(members) * 4999 // 2,
                "files": files, "file_count": len(files), "decisions": decisions,
                "decision_count": len(decisions), "pending_deletion_decisions": list(decisions),
                "pending_deletion_decision_count": len(decisions), "open": True,
            }],
        }
        reconciliation.write_report(report)
        with mock.patch("library.views._track_review_details", wraps=__import__(
                "library.views", fromlist=["_track_review_details"])._track_review_details) as details:
            response = self.client.get(reverse("library:api-reconciliation-findings"),
                                       {"type": "duplicate", "per_page": 1})
        self.assertEqual(response.status_code, 200)
        finding = response.json()["items"][0]
        self.assertEqual((finding["member_count"], finding["members_returned"]), (5000, 50))
        self.assertEqual((finding["file_count"], finding["files_returned"]), (5000, 50))
        self.assertEqual((finding["decision_count"], finding["decisions_returned"]), (1000, 50))
        self.assertEqual(finding["pending_deletion_decision_count"], 1000)
        self.assertTrue(finding["members_truncated"] and finding["files_truncated"]
                        and finding["decisions_truncated"])
        self.assertLessEqual(len(details.call_args.args[0]), reconciliation.DUPLICATE_DETAIL_RESPONSE_LIMIT)
        self.assertLess(len(response.content), 100_000)


class DuplicateHashBoundaryTests(_Recon):
    def walked(self, rel, data):
        path = self.file(rel, data)
        scanner = reconciliation.Scanner(root=self.root)
        scanner.check_root()
        scanner.walk()
        return scanner, scanner.files[str(path)], path

    def test_growth_replacement_and_symlink_substitution_never_become_exact_evidence(self):
        data = b"a" * (reconciliation.HASH_CHUNK + 4096)
        for mutation in ("grow", "replace", "symlink"):
            with self.subTest(mutation=mutation):
                scanner, entry, path = self.walked(f"ROCK/{mutation}.flac", data)
                target = self.base / f"{mutation}-target.flac"
                target.write_bytes(b"z" * len(data))
                real_read, changed = os.read, [False]

                def mutating_read(fd, amount):
                    chunk = real_read(fd, amount)
                    if not changed[0]:
                        changed[0] = True
                        if mutation == "grow":
                            with open(path, "ab") as handle:
                                handle.write(b"growth")
                        elif mutation == "replace":
                            os.replace(target, path)
                        else:
                            os.unlink(path)
                            os.symlink(target, path)
                    return chunk

                with mock.patch.object(reconciliation.os, "read", mutating_read):
                    with self.assertRaises(reconciliation.HashUncertain):
                        scanner._hash(entry)
                self.assertLessEqual(scanner.hashed_bytes, entry.size)

    def test_a_hash_race_reclassifies_the_file_and_removes_it_from_metadata_groups(self):
        data = b"r" * (reconciliation.HASH_CHUNK + 1024)
        unstable = self.track("ROCK/unstable.flac", title="Twin", data=data)
        self.track("ROCK/stable.flac", title="Twin", data=data)
        real_hash, changed = reconciliation.Scanner._hash, [False]

        def change_before_hash(scanner, entry):
            if entry.path == unstable.filepath and not changed[0]:
                changed[0] = True
                with open(entry.path, "ab") as handle:
                    handle.write(b"changed")
            return real_hash(scanner, entry)

        with mock.patch.object(reconciliation.Scanner, "_hash", change_before_hash):
            report = self.scan()
        self.assertEqual(self.of(report, "duplicate"), [])
        uncertain = [f for f in self.of(report, "uncertain")
                     if (f.get("file") or {}).get("path") == unstable.filepath]
        self.assertEqual(len(uncertain), 1)
        self.assertIn("changed", uncertain[0]["reason"])

    def test_protected_and_in_delivery_media_are_not_read_by_the_duplicate_hasher(self):
        from spoken.models import AssetMode, SpokenAsset, SpokenContentType

        payload = b"same-sized-audio" * 100
        ordinary = [self.track(f"ROCK/ordinary-{i}.flac", data=payload) for i in range(2)]

        protected_track = self.track("ROCK/remote.flac", data=payload,
                                     remote_url="https://example.invalid/remote.flac")
        spoken = self.track("ROCK/spoken-" + "a" * 32 + ".flac", data=payload)
        ctype = SpokenContentType.objects.create(
            code="recon-spoken", name="Recon spoken", delivery_mode="current_edition", recipe_key="lcl_psa",
            target_category=self.rock, enabled=True, requires_review=False, deliver_ready_to_air=True,
        )
        asset = SpokenAsset(content_type=ctype, mode=AssetMode.CURRENT_EDITION, slot_key="recon",
                            track=spoken, library_relpath=f"ROCK/{Path(spoken.filepath).name}")
        models.QuerySet.bulk_create(SpokenAsset._base_manager.all(), [asset])
        active_path = self.file("UrgentPA/am-" + "b" * 32 + ".flac", payload)
        arriving = self.file("Syndicated/episode.flac", payload)

        hashed = []
        real_hash = reconciliation.Scanner._hash

        def recording_hash(scanner, entry):
            hashed.append(entry.path)
            return real_hash(scanner, entry)

        with mock.patch.object(reconciliation.Scanner, "fresh",
                               lambda _scanner, entry: entry.path == str(arriving)), \
             mock.patch.object(reconciliation.Scanner, "_hash", recording_hash):
            report = self.scan()
        self.assertEqual(set(hashed), {t.filepath for t in ordinary})
        self.assertNotIn(protected_track.filepath, hashed)
        self.assertNotIn(spoken.filepath, hashed)
        self.assertNotIn(str(active_path), hashed)
        self.assertNotIn(str(arriving), hashed)
        protected_paths = {f.get("file", {}).get("path") for f in self.of(report, "protected")}
        self.assertIn(str(active_path), protected_paths)
        self.assertIn(str(arriving), {f.get("file", {}).get("path") for f in self.of(report, "in_delivery")})


class DuplicateDecisionEndpointTests(_Recon):
    URL = "library:api-reconciliation-not-duplicate"

    def setUp(self):
        super().setUp()
        self.a = self.track("ROCK/a.flac", title="Twin", duration=100.0)
        self.b = self.track("POP/b.flac", title="Twin", duration=100.5)
        self.c = self.track("ROCK/c.flac", title="Twin", duration=101.0)
        self.report = reconciliation.run_scan()
        self.group = self.of(self.report, "duplicate")[0]

    def decide(self, a, b, **extra):
        payload = {"scan_id": self.report["scan_id"], "finding_id": self.group["id"], "track_a": a.pk,
                   "track_b": b.pk, **extra}
        response = self.client.post(reverse(self.URL), payload, content_type="application/json")
        return response.status_code, response.json()

    def test_records_not_a_duplicate_once_and_never_overrides_a_decision(self):
        self.assertEqual(self.decide(self.b, self.a), (200, {"result": "recorded", "resolution": "keep_both"}))
        row = DuplicateCandidate.objects.get(track_a=self.a, track_b=self.b)
        self.assertEqual((row.resolution, row.applied, row.confidence), ("keep_both", False, "probable"))
        self.assertEqual(self.decide(self.a, self.b)[1]["result"], "already_decided")       # a replay
        DuplicateCandidate.objects.create(track_a=self.a, track_b=self.c, confidence="probable", resolution="keep_a")
        status, body = self.decide(self.a, self.c)
        self.assertEqual((status, body["result"], body["resolution"]), (200, "already_decided", "keep_a"))
        self.assertEqual(DuplicateCandidate.objects.get(track_a=self.a, track_b=self.c).resolution, "keep_a")
        self.assertEqual(Track.objects.count(), 3)
        self.assertTrue(all(Path(t.filepath).exists() for t in Track.objects.all()))

    def test_an_unresolved_pair_is_resolved_not_duplicated(self):
        DuplicateCandidate.objects.create(track_a=self.b, track_b=self.c, confidence="probable")
        self.assertEqual(self.decide(self.c, self.b)[1]["result"], "recorded")
        self.assertEqual(list(DuplicateCandidate.objects.filter(track_a=self.b, track_b=self.c)
                              .values_list("resolution", flat=True)), ["keep_both"])

    def test_stale_scans_foreign_pairs_and_non_managers_are_refused(self):
        newer = reconciliation.run_scan()
        self.assertNotEqual(newer["scan_id"], self.report["scan_id"])
        self.assertEqual(self.decide(self.a, self.b)[0], 409)
        self.report = newer
        self.group = self.of(newer, "duplicate")[0]
        stranger = self.track("ROCK/z.flac", title="Unrelated")
        self.assertEqual(self.decide(self.a, stranger)[0], 400)
        self.assertEqual(self.decide(self.a, self.a)[0], 400)
        user = User.objects.create_user("contrib-dup", "c@example.invalid", "pw")
        user.groups.add(Group.objects.get_or_create(name="Contributor")[0])
        self.client.force_login(user)
        self.assertEqual(self.decide(self.a, self.b)[0], 403)
        self.assertFalse(DuplicateCandidate.objects.exists())


class UnregisteredDuplicateVisibilityTests(_Recon):
    """Exact-byte groups with unregistered copies stay visible as a *file* review, separately from Track pairs."""

    DATA = b"one recording, byte for byte" * 300

    def exact(self, report):
        return [g for g in self.of(report, "duplicate") if g["confidence"] == "exact"]

    def open_ids(self):
        body = self.client.get(reverse("library:api-reconciliation-findings"),
                               {"type": "duplicate", "open": "1"}).json()
        return [f["id"] for f in body["items"]]

    def assert_status(self, group, *, track_pair, file_review, unregistered, open_):
        self.assertEqual((group["track_pair_review_open"], group["file_review_open"],
                          group["unregistered_file_count"], group["open"]),
                         (track_pair, file_review, unregistered, open_))

    def test_one_track_and_one_unregistered_copy_is_an_open_file_review(self):
        track = self.track("ROCK/song.flac", data=self.DATA)
        self.file("INBOX/song-copy.flac", self.DATA)
        report = reconciliation.run_scan()
        group, = self.exact(report)
        self.assertEqual((group["members"], group["pair_count"], group["file_count"]), ([track.pk], 0, 2))
        self.assert_status(group, track_pair=False, file_review=True, unregistered=1, open_=True)
        self.assertEqual(self.open_ids(), [group["id"]])
        self.assertEqual(report["counts"]["duplicate_groups_open"], 1)
        self.assertEqual(report["counts"]["duplicate_groups_file_review_open"], 1)
        self.assertEqual(report["counts"]["duplicate_groups_track_pair_review_open"], 0)

    def test_one_track_and_several_unregistered_copies(self):
        self.track("ROCK/song.flac", data=self.DATA)
        for i in range(3):
            self.file(f"INBOX/copy-{i}.flac", self.DATA)
        group, = self.exact(reconciliation.run_scan())
        self.assertEqual(group["file_count"], 4)
        self.assert_status(group, track_pair=False, file_review=True, unregistered=3, open_=True)
        self.assertEqual(self.open_ids(), [group["id"]])

    def test_two_unregistered_copies_with_no_track(self):
        self.file("INBOX/a.flac", self.DATA)
        self.file("INBOX/b.flac", self.DATA)
        group, = self.exact(reconciliation.run_scan())
        self.assertEqual((group["members"], group["pair_count"]), ([], 0))
        self.assert_status(group, track_pair=False, file_review=True, unregistered=2, open_=True)
        self.assertEqual(self.open_ids(), [group["id"]])

    def test_an_unresolved_registered_pair_is_open_as_before(self):
        self.track("ROCK/a.flac", data=self.DATA)
        self.track("POP/b.flac", data=self.DATA)
        group, = self.exact(reconciliation.run_scan())
        self.assert_status(group, track_pair=True, file_review=False, unregistered=0, open_=True)
        self.assertEqual(self.open_ids(), [group["id"]])

    def test_a_reviewed_registered_pair_without_unregistered_copies_stays_closed(self):
        a = self.track("ROCK/a.flac", data=self.DATA)
        b = self.track("POP/b.flac", data=self.DATA)
        DuplicateCandidate.objects.create(track_a=a, track_b=b, confidence="exact", resolution="keep_both")
        group, = self.exact(reconciliation.run_scan())
        self.assert_status(group, track_pair=False, file_review=False, unregistered=0, open_=False)
        self.assertEqual(self.open_ids(), [])
        everything = self.client.get(reverse("library:api-reconciliation-findings"), {"type": "duplicate"}).json()
        self.assertEqual([f["id"] for f in everything["items"]], [group["id"]])

    def test_an_unregistered_copy_keeps_a_reviewed_pair_visible_without_reopening_the_pair(self):
        a = self.track("ROCK/a.flac", data=self.DATA)
        b = self.track("POP/b.flac", data=self.DATA)
        DuplicateCandidate.objects.create(track_a=a, track_b=b, confidence="exact", resolution="keep_both")
        self.file("INBOX/third.flac", self.DATA)
        group, = self.exact(reconciliation.run_scan())
        self.assert_status(group, track_pair=False, file_review=True, unregistered=1, open_=True)
        self.assertEqual(group["decisions"], {f"{a.pk}-{b.pk}": {"resolution": "keep_both", "applied": False}})
        self.assertEqual(self.open_ids(), [group["id"]])
        self.assertEqual(DuplicateCandidate.objects.get(track_a=a, track_b=b).resolution, "keep_both")

    def test_a_large_exact_group_has_exact_totals_and_a_bounded_response(self):
        track = self.track("ROCK/song.flac", data=self.DATA)
        copies = 120
        for i in range(copies):
            self.file(f"INBOX/copy-{i:03d}.flac", self.DATA)
        report = reconciliation.run_scan()
        group, = self.exact(report)
        self.assertEqual((group["file_count"], group["unregistered_file_count"], group["members"], group["pair_count"]),
                         (copies + 1, copies, [track.pk], 0))
        self.assertEqual(report["budgets"]["hashed_bytes"], (copies + 1) * len(self.DATA))   # each file read once
        item, = self.client.get(reverse("library:api-reconciliation-findings"),
                                {"type": "duplicate", "open": "1"}).json()["items"]
        self.assertTrue(item["files_truncated"])
        self.assertEqual(item["file_count"], copies + 1)
        self.assertLessEqual(len(item["files"]), reconciliation.DUPLICATE_DETAIL_MEMBER_LIMIT)
        self.assertEqual(item["unregistered_file_count"], copies)

    def test_no_track_pair_decision_can_be_fabricated_for_file_only_groups(self):
        track = self.track("ROCK/song.flac", data=self.DATA)
        self.file("INBOX/copy.flac", self.DATA)
        self.file("INBOX/x.flac", b"x" * 7)
        self.file("INBOX/y.flac", b"x" * 7)                    # a second group with no Track at all
        outsider = self.track("ROCK/outsider.flac")
        report = reconciliation.run_scan()
        url = reverse("library:api-reconciliation-not-duplicate")
        for group in self.exact(report):
            for a, b in ((track.pk, outsider.pk), (outsider.pk, track.pk), (track.pk, track.pk)):
                response = self.client.post(url, {"scan_id": report["scan_id"], "finding_id": group["id"],
                                                  "track_a": a, "track_b": b}, content_type="application/json")
                self.assertEqual(response.status_code, 400, (group["members"], a, b))
        self.assertFalse(DuplicateCandidate.objects.exists())

    def test_the_page_states_the_file_review_and_offers_no_file_action(self):
        html = self.client.get(reverse("library:library-reconciliation")).content.decode()
        self.assertIn("unregistered — requires review", html)
        self.assertIn("does not mean they should be ", html)
        self.assertIn("File review only", html)
        # The page calls exactly the reconciliation endpoints: no import, cleanup or merge action, and no action on
        # a file. (3.11: plus Run reconciliation, which starts the same read-only scan. 3.12: plus the previewed
        # duplicate resolution, which deletes only a registered Track of an exact two-Track pair, never an
        # unregistered file -- library.tests.test_library_duplicate_resolution. 3.11 category drift: plus the
        # previewed drift repair, which moves a registered Track's own file or changes its primary Category, never
        # an unregistered file -- library.tests.test_library_category_drift.)
        endpoints = set(re.findall(r"'(/api/[^'?]+)", html))
        self.assertEqual(endpoints, {
            "/api/tracks/reconciliation/", "/api/tracks/reconciliation/findings/",
            "/api/tracks/reconciliation/relink/preview/", "/api/tracks/reconciliation/relink/apply/",
            "/api/tracks/reconciliation/duplicates/not-duplicate/", "/api/tracks/reconciliation/run/",
            "/api/tracks/reconciliation/duplicates/preview/", "/api/tracks/reconciliation/duplicates/apply/",
            "/api/tracks/reconciliation/drift/preview/", "/api/tracks/reconciliation/drift/apply/"})


class _RelinkSetup:
    """A real recording, analyzed by the station's own analysis (trusted v2 cache with its decoded identity),
    with an approved cue point, then moved and renamed outside the application."""

    def prepare(self, *, freq=440, approve=True):
        from library.models import LogItem, PlayEvent, PlaylistLog
        from library.services import cue_markers, waveform_cache
        from library.tests.test_waveform_integrity_package_b import _audio
        from django.utils import timezone as tz

        (self.root / "ROCK").mkdir(exist_ok=True)
        (self.root / "POP").mkdir(exist_ok=True)
        path = _audio(self.root / "ROCK" / "song.flac", freq=freq)
        track = Track.objects.create(filepath=str(path), filename=path.name, title="Moved Song", artist=self.artist,
                                     category=self.rock, duration_seconds=3.0, format="flac", ready2air=True,
                                     play_count=7)
        track = _analyze(self, track)
        if approve:
            revision = waveform_cache.current_audio_revision(track)
            self.assertIsNotNone(revision)
            result = cue_markers.operator_edit(track.pk, expected_revision=track.cue_marker_revision, user=self.admin,
                                               values={"intro_until_seconds": 0.8}, audio_revision=revision)
            self.assertTrue(result.ok, result.error)
        log = PlaylistLog.objects.create(date=tz.localdate(), hour=3, status="approved")
        LogItem.objects.create(playlist_log=log, position=0, scheduled_time=tz.now(), track=track,
                               track_title=track.title, category=self.rock)
        PlayEvent.objects.create(track=track, track_title=track.title, started_at=tz.now())
        self.moved = self.root / "POP" / "moved-song.flac"
        os.rename(path, self.moved)
        self.track_obj = Track.objects.get(pk=track.pk)
        return self.track_obj

    def scan_and_find(self):
        self.report = reconciliation.run_scan()
        moved = [f for f in self.report["findings"] if f["type"] == "moved"]
        self.assertEqual(len(moved), 1, self.report["findings"])
        self.finding = moved[0]
        return self.finding

    def preview(self, client=None, finding_id=None, scan_id=None):
        client = client or self.client
        response = client.post(reverse("library:api-reconciliation-relink-preview"),
                               {"scan_id": scan_id or self.report["scan_id"], "finding_id": finding_id or self.finding["id"]},
                               content_type="application/json")
        return response.status_code, response.json()

    def apply(self, token, client=None):
        client = client or self.client
        response = client.post(reverse("library:api-reconciliation-relink-apply"), {"token": token},
                               content_type="application/json")
        return response.status_code, response.json()

    def frozen(self, track_id):
        from library.models import LogItem, PlayEvent, PlaylistLog
        row = Track.objects.filter(pk=track_id).values().get()
        for key in ("filepath", "filename", "updated_at"):
            row.pop(key)
        return (row, sorted(Track.objects.get(pk=track_id).additional_categories.values_list("pk", flat=True)),
                list(LogItem.objects.values()), list(PlayEvent.objects.values()), list(PlaylistLog.objects.values()))


class RelinkTests(_RelinkSetup, _Recon):
    def test_an_explicitly_reviewed_relink_reconnects_the_same_track_and_changes_nothing_else(self):
        from library.management.commands import analyze_tracks
        from library.services import cue_markers, waveform_cache
        track = self.prepare()
        finding = self.scan_and_find()
        self.assertEqual(finding["relink"], 0)
        self.assertTrue(finding["candidates"][0]["eligible"])
        self.assertTrue(finding["candidates"][0]["identity_match"])
        cache = Path(track.waveform_path).read_bytes()
        before = self.frozen(track.pk)
        status, preview = self.preview()
        self.assertEqual(status, 200, preview)
        self.assertEqual((preview["from"], preview["to"]), (track.filepath, str(self.moved)))
        self.assertEqual([c["field"] for c in preview["changes"]], ["filepath", "filename"])
        with mock.patch.object(analyze_tracks, "analyze_one_track") as analysis, \
             mock.patch.object(cue_markers, "request_analysis") as requested, \
             mock.patch("os.unlink") as unlink, mock.patch("os.rename") as rename, \
             mock.patch("shutil.move") as move:
            status, result = self.apply(preview["token"])
        self.assertEqual((status, result["result"]), (200, "relinked"), result)
        for spy in (analysis, requested, unlink, rename, move):
            spy.assert_not_called()
        relinked = Track.objects.get(pk=track.pk)
        self.assertEqual((relinked.filepath, relinked.filename), (str(self.moved), "moved-song.flac"))
        self.assertEqual(self.frozen(track.pk), before)            # metadata, markers, readiness, history, logs
        self.assertEqual(Path(relinked.waveform_path).read_bytes(), cache)
        verdict = waveform_cache.validate(relinked)
        self.assertEqual((verdict.trusted, verdict.displayable), (False, True))
        self.assertTrue(self.moved.exists())

    def test_a_completed_relink_cannot_be_replayed(self):
        self.prepare()
        self.scan_and_find()
        _status, preview = self.preview()
        self.assertEqual(self.apply(preview["token"])[0], 200)
        status, body = self.apply(preview["token"])
        self.assertEqual((status, body["result"]), (409, "refused"))
        self.assertIn("changed since the scan", body["error"])

    def test_a_candidate_that_changed_is_refused_at_preview_and_at_apply(self):
        self.prepare()
        self.scan_and_find()
        _status, preview = self.preview()
        with open(self.moved, "ab") as handle:
            handle.write(b"\0")
        status, body = self.apply(preview["token"])
        self.assertEqual(status, 409)
        self.assertIn("changed since its audio was compared", body["error"])
        self.assertEqual(self.preview()[0], 409)
        self.assertNotEqual(Track.objects.get(pk=self.track_obj.pk).filepath, str(self.moved))

    def test_a_candidate_that_vanished_or_was_claimed_is_refused(self):
        self.prepare()
        self.scan_and_find()
        _status, preview = self.preview()
        Track.objects.create(filepath=str(self.moved), filename="moved-song.flac", title="Claimed", artist=self.artist,
                             category=self.pop)
        status, body = self.apply(preview["token"])
        self.assertEqual(status, 409)
        self.assertIn("Another Track already uses", body["error"])
        Track.objects.filter(filepath=str(self.moved)).delete()
        os.unlink(self.moved)
        status, body = self.apply(preview["token"])
        self.assertIn("no longer exists", body["error"])
        self.assertEqual(Track.objects.get(pk=self.track_obj.pk).filepath, str(self.root / "ROCK" / "song.flac"))

    def test_the_original_file_reappearing_or_the_track_changing_refuses(self):
        import shutil
        from library.services import cue_markers
        track = self.prepare()
        self.scan_and_find()
        _status, preview = self.preview()
        shutil.copy2(self.moved, track.filepath)
        status, body = self.apply(preview["token"])
        self.assertIn("original file is back", body["error"])
        os.unlink(track.filepath)
        current = Track.objects.get(pk=track.pk)
        cue_markers.operator_edit(track.pk, expected_revision=current.cue_marker_revision, user=self.admin,
                                  values={"outro_starts_seconds": 2.0})
        status, body = self.apply(preview["token"])
        self.assertIn("changed since the scan", body["error"])
        Track.objects.get(pk=track.pk).delete()
        status, body = self.apply(preview["token"])
        self.assertIn("no longer exists", body["error"])

    def test_tokens_are_signed_operator_bound_expiring_and_tied_to_the_latest_scan(self):
        self.prepare()
        self.scan_and_find()
        _status, preview = self.preview()
        self.assertEqual(self.apply(preview["token"][:-2] + "zz")[0], 400)
        self.assertEqual(self.apply("")[0], 400)
        other = User.objects.create_superuser("other-recon", "o@example.invalid", "pw")
        from django.test import Client
        second = Client()
        second.force_login(other)
        self.assertEqual(self.apply(preview["token"], client=second)[0], 403)
        from library.services import library_relink
        with mock.patch.object(library_relink, "TOKEN_MAX_AGE_SECONDS", -1):
            self.assertIn("expired", self.apply(preview["token"])[1]["error"])
        self.assertEqual(self.preview(scan_id="20000101T000000-deadbeef")[0], 409)
        self.assertEqual(self.preview(finding_id="F999999")[0], 400)
        self.assertNotEqual(Track.objects.get(pk=self.track_obj.pk).filepath, str(self.moved))

    def test_a_write_guard_refusal_rolls_the_relink_back(self):
        from library import models as library_models
        self.prepare()
        self.scan_and_find()
        _status, preview = self.preview()

        def refuse(queryset, columns):
            if "filepath" in columns:
                raise library_models.TrackWriteRefused("projected spoken Track path")

        guards = library_models.TRACK_WRITE_GUARDS + [(lambda t, f: None, refuse, lambda o, f: None)]
        with mock.patch.object(library_models, "TRACK_WRITE_GUARDS", guards):
            status, body = self.apply(preview["token"])
        self.assertEqual(status, 409)
        self.assertIn("Protected", body["error"])
        self.assertEqual(Track.objects.get(pk=self.track_obj.pk).filepath, str(self.root / "ROCK" / "song.flac"))

    def test_only_track_managers_may_relink(self):
        self.prepare()
        self.scan_and_find()
        _status, preview = self.preview()
        user = User.objects.create_user("contrib-relink", "cr@example.invalid", "pw")
        user.groups.add(Group.objects.get_or_create(name="Contributor")[0])
        self.client.force_login(user)
        self.assertEqual(self.preview()[0], 403)
        self.assertEqual(self.apply(preview["token"])[0], 403)


class RelinkEvidenceTests(_RelinkSetup, _Recon):
    """Cases the available authority cannot prove: manual review, no relink offered."""

    def assert_manual(self, reason_part):
        finding = self.scan_and_find()
        self.assertIsNone(finding["relink"])
        self.assertIn(reason_part, json.dumps(finding))
        self.assertEqual(self.preview()[0], 400)

    def test_different_audio_under_the_same_name(self):
        from library.tests.test_waveform_integrity_package_b import _audio
        self.prepare()
        os.unlink(self.moved)
        _audio(self.root / "POP" / "song.flac", freq=880)       # the Track's file name, another recording
        self.assert_manual("decoded audio differs")

    def test_approvals_for_other_audio(self):
        from library.services import cue_markers
        track = self.prepare(approve=False)
        cue_markers.operator_edit(track.pk, expected_revision=track.cue_marker_revision, user=self.admin,
                                  values={"intro_until_seconds": 0.7}, audio_revision="e" * 64)
        self.assert_manual("approvals refer to different audio")

    def test_another_format(self):
        self.prepare()
        mp3 = self.root / "POP" / "moved-song.mp3"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(self.moved), str(mp3)],
                       check=True, stdin=subprocess.DEVNULL)
        os.unlink(self.moved)
        os.rename(mp3, self.root / "POP" / "song.mp3")         # same recording, other format
        report = reconciliation.run_scan()
        # A file of another format is never a relink candidate (name and size are matched per format).
        self.assertFalse([f for f in report["findings"] if f["type"] == "moved"])
        self.assertEqual([f["track"]["id"] for f in report["findings"] if f["type"] == "missing"],
                         [self.track_obj.pk])

    def test_two_copies_with_the_same_audio(self):
        import shutil
        self.prepare()
        shutil.copy2(self.moved, self.root / "song.flac")
        finding = self.scan_and_find()
        self.assertIsNone(finding["relink"])
        self.assertIn("more than one candidate", finding["reason"])

    def test_a_leftover_canonical_cache_is_not_authority_when_waveform_path_is_blank(self):
        track = self.prepare()
        canonical = self.waves / f"{track.pk}.json"
        self.assertTrue(canonical.exists())
        Track.objects.filter(pk=track.pk).update(waveform_path="")
        report = reconciliation.run_scan()
        self.assertFalse(any(f.get("relink") is not None for f in report["findings"]))
        self.assertIn("no authoritative waveform cache", json.dumps(report))

    def test_the_track_authoritative_alternate_cache_is_used_not_a_stale_canonical_file(self):
        track = self.prepare()
        canonical = Path(track.waveform_path)
        alternate = self.waves / "authoritative-alternate.json"
        os.rename(canonical, alternate)
        canonical.write_text("{}", encoding="utf-8")
        Track.objects.filter(pk=track.pk).update(waveform_path=str(alternate))
        finding = self.scan_and_find()
        self.assertEqual(finding["relink"], 0)
        self.assertTrue(finding["candidates"][0]["eligible"])

    def test_an_invalid_alternate_reference_does_not_fall_back_to_the_canonical_cache(self):
        track = self.prepare()
        alternate = self.waves / "not-a-cache.json"
        alternate.write_text("{}", encoding="utf-8")
        Track.objects.filter(pk=track.pk).update(waveform_path=str(alternate))
        report = reconciliation.run_scan()
        self.assertFalse(any(f.get("relink") is not None for f in report["findings"]))
        self.assertIn("waveform cache", json.dumps(report))

    def test_cache_source_disagreement_is_manual_review(self):
        track = self.prepare()
        cache = Path(track.waveform_path)
        payload = json.loads(cache.read_text(encoding="utf-8"))
        payload["source"]["path"] = str(self.root / "ROCK" / "another.flac")
        cache.write_text(json.dumps(payload), encoding="utf-8")
        self.assert_manual("different file path")

    def test_preview_and_apply_recheck_the_same_authoritative_cache(self):
        track = self.prepare()
        self.scan_and_find()
        status, preview = self.preview()
        self.assertEqual(status, 200, preview)
        cache = Path(track.waveform_path)
        payload = json.loads(cache.read_text(encoding="utf-8"))
        payload["source"]["path"] = str(self.root / "ROCK" / "changed.flac")
        cache.write_text(json.dumps(payload), encoding="utf-8")
        status, result = self.apply(preview["token"])
        self.assertEqual(status, 409)
        self.assertIn("recorded audio no longer matches", result["error"])
        self.assertEqual(Track.objects.get(pk=track.pk).filepath, track.filepath)


def _advisory_waiters():
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
        return cursor.fetchone()[0]


def _wait_until(predicate, timeout=10.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class RelinkConcurrencyTests(_RelinkSetup, _ReconFixture, TransactionTestCase):
    """A relink serializes with registration and recall of the same path on the source-path lock (path, then
    Track row), whichever comes first; every interleaving is forced on pg_locks."""

    def start(self, name, target, results):
        import threading
        from django.db import close_old_connections, connections

        def run():
            close_old_connections()
            try:
                results["value"] = target()
            except Exception as exc:
                results["error"] = exc
            finally:
                connections.close_all()
        thread = threading.Thread(target=run, name=name)
        thread.start()
        return thread

    def paused(self, holder):
        """Patch source_paths.acquire so thread ``holder`` stops right after its first path lock."""
        import threading
        from library.services import source_paths
        held, release, real = threading.Event(), threading.Event(), source_paths.acquire

        def acquire(path, using, *, timeout_ms):
            real(path, using, timeout_ms=timeout_ms)
            if threading.current_thread().name == holder and not held.is_set():
                held.set()
                release.wait(20)
        return mock.patch.object(source_paths, "acquire", acquire), held, release

    def relink(self, token):
        from library.services import library_relink
        return library_relink.apply(token, user=self.admin)

    def ready(self):
        self.prepare()
        self.scan_and_find()
        status, preview = self.preview()
        self.assertEqual(status, 200, preview)
        return preview["token"]

    def test_a_registration_holding_the_candidate_path_wins_and_the_relink_refuses(self):
        token = self.ready()
        patcher, held, release = self.paused("creator")
        created, relinked = {}, {}
        with patcher:
            creator = self.start("creator", lambda: Track.objects.create(
                filepath=str(self.moved), filename=self.moved.name, title="Registered", artist=self.artist,
                category=self.pop), created)
            self.assertTrue(held.wait(10))
            relink = self.start("relink", lambda: self.relink(token), relinked)
            self.assertTrue(_wait_until(lambda: _advisory_waiters() >= 1), "the relink did not wait for the path")
            release.set()
            creator.join(20)
            relink.join(20)
        self.assertNotIn("error", created)
        self.assertIn("Another Track already uses", str(relinked.get("error")))
        self.assertEqual(Track.objects.get(pk=self.track_obj.pk).filepath, str(self.root / "ROCK" / "song.flac"))

    def test_a_relink_holding_the_path_makes_a_concurrent_registration_fail_visibly(self):
        from django.db import IntegrityError
        token = self.ready()
        patcher, held, release = self.paused("relink")
        created, relinked = {}, {}
        with patcher:
            relink = self.start("relink", lambda: self.relink(token), relinked)
            self.assertTrue(held.wait(10))
            creator = self.start("creator", lambda: Track.objects.create(
                filepath=str(self.moved), filename=self.moved.name, title="Registered", artist=self.artist,
                category=self.pop), created)
            self.assertTrue(_wait_until(lambda: _advisory_waiters() >= 1), "the registration did not wait")
            release.set()
            relink.join(20)
            creator.join(20)
        self.assertEqual(relinked.get("value", {}).get("result"), "relinked", relinked)
        self.assertIsInstance(created.get("error"), IntegrityError)
        self.assertEqual(list(Track.objects.filter(filepath=str(self.moved)).values_list("pk", flat=True)),
                         [self.track_obj.pk])

    def test_a_recall_holding_the_candidate_removes_it_and_the_relink_refuses(self):
        token = self.ready()
        patcher, held, release = self.paused("recall")
        recalled, relinked = {}, {}
        with patcher:
            recall = self.start("recall", lambda: call_command("remove_track_file", str(self.moved),
                                                               stdout=StringIO()), recalled)
            self.assertTrue(held.wait(10))
            relink = self.start("relink", lambda: self.relink(token), relinked)
            self.assertTrue(_wait_until(lambda: _advisory_waiters() >= 1), "the relink did not wait for the recall")
            release.set()
            recall.join(20)
            relink.join(20)
        self.assertNotIn("error", recalled)
        self.assertFalse(self.moved.exists())
        self.assertIn("no longer exists", str(relinked.get("error")))
        self.assertEqual(Track.objects.get(pk=self.track_obj.pk).filepath, str(self.root / "ROCK" / "song.flac"))
