"""Roadmap 3.11 Phase B: read-only Library reconciliation (scan, report, review page).

Every file lives in a per-test temporary Library root; LIBRARY_ROOT, WAVEFORMS_DIR and REPORTS_ROOT point
there, so no station media, cache or report is read or written."""
import json
import os
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
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
        self.assertIn("no version-2 waveform cache", " ".join(moved[0]["candidates"][0]["reasons"]))
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
