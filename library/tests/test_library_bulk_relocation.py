"""3.11: the /library/ bulk editor's optional file relocation for a primary-category change.

Real files in a per-test temporary Library root and a fresh engine state file in the test's own directory (the
category-drift fixture); no station media, cache or engine is touched.  The r0125 metadata-only behaviour has its own
suite (test_library_bulk_edit_r0125), which this change must leave passing unchanged."""
import os
import threading
import time
from pathlib import Path
from unittest import mock

from django.db import connection, connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from library.models import Track
from library.services import media_relocation as m
from library.services import track_bulk_edit as bulk
from library.tests.test_library_category_drift import AUDIO, _DriftFixture

PREVIEW = "library:api-track-bulk-edit-preview"
APPLY = "library:api-track-bulk-edit-apply"


class _BulkFixture(_DriftFixture):
    def post(self, name, payload, client=None):
        response = (client or self.client).post(reverse(name), payload, content_type="application/json")
        return response.status_code, response.json()

    @staticmethod
    def ids(*tracks):
        return {"mode": "ids", "ids": [t.pk for t in tracks]}

    def to(self, category, **more):
        return {"category": {"op": "set", "value": category.pk}, **more}

    def preview(self, selection, changes, mode=None, client=None):
        self.engine()
        body = {"selection": selection, "changes": changes}
        if mode is not None:
            body["file_location"] = mode
        return self.post(PREVIEW, body, client)

    def apply(self, selection, changes, mode=None, token=None, apply_mode="same", client=None):
        if token is None:
            status, body = self.preview(selection, changes, mode, client)
            self.assertEqual(status, 200, body)
            token = body["token"]
        self.engine()
        payload = {"selection": selection, "changes": changes, "token": token}
        sent = mode if apply_mode == "same" else apply_mode
        if sent is not None:
            payload["file_location"] = sent
        return self.post(APPLY, payload, client)

    def put(self, rel, category=None, **fields):
        return self.track(rel, category=category or self.rock, data=AUDIO, **fields)

    def where(self, track):
        row = Track.objects.get(pk=track.pk)
        return row.category.code if row.category_id else None, os.path.relpath(row.filepath, self.root)


class _Bulk(_BulkFixture, TestCase):
    pass


class CompatibilityTests(_Bulk):
    def test_c1_an_omitted_or_metadata_only_policy_is_the_r0125_behaviour(self):
        for mode in (None, bulk.METADATA_ONLY):
            with self.subTest(mode=mode):
                track = self.put(f"ROCK/compat-{mode}.flac")
                inode = os.lstat(track.filepath).st_ino
                status, body = self.preview(self.ids(track), self.to(self.pop), mode)
                self.assertIn("stay in their current category folder", " ".join(body["warnings"]))
                self.assertNotIn("moves", body)
                status, body = self.apply(self.ids(track), self.to(self.pop), mode)
                self.assertEqual((status, body["updated"], body["files_left_in_previous_category_folder"],
                                  body["moved"]), (200, 1, 1, 0))
                self.assertEqual(self.where(track), ("POP", f"ROCK/compat-{mode}.flac"))
                self.assertEqual(os.lstat(track.filepath).st_ino, inode)

    def test_c1b_metadata_only_needs_neither_the_files_nor_the_engine(self):
        track = self.put("ROCK/nofile.flac")
        os.unlink(track.filepath)
        self.state_path.unlink()
        status, body = self.post(PREVIEW, {"selection": self.ids(track), "changes": self.to(self.pop)})
        self.assertEqual((status, body["tracks_changing"]), (200, 1))
        status, body = self.post(APPLY, {"selection": self.ids(track), "changes": self.to(self.pop),
                                         "token": body["token"]})
        self.assertEqual(body["updated"], 1)

    def test_c5_other_edits_in_move_mode_need_no_file_or_engine_checks(self):
        home = self.put("POP/already-home.flac", self.rock)          # will become POP: no move needed
        os.chmod(home.filepath, 0)                                    # unreadable: would block a move
        self.addCleanup(os.chmod, home.filepath, 0o644)
        status, body = self.preview(self.ids(home), self.to(self.pop, energy={"op": "set", "value": "upbeat"}),
                                    bulk.MOVE_FILES)
        self.assertEqual((body["moves_count"], body["no_move_needed"], body["refusals"]), (0, 1, []))
        self.state_path.unlink()                                      # no engine: irrelevant without a move
        status, result = self.post(APPLY, {"selection": self.ids(home), "file_location": bulk.MOVE_FILES,
                                           "changes": self.to(self.pop, energy={"op": "set", "value": "upbeat"}),
                                           "token": body["token"]})
        self.assertEqual((result["updated"], result["moved"]), (1, 0))
        self.assertEqual(Track.objects.get(pk=home.pk).energy, "upbeat")

    def test_c7_clearing_the_category_never_relocates(self):
        track = self.put("ROCK/cleared.flac")
        status, body = self.preview(self.ids(track), {"category": {"op": "clear"}}, bulk.MOVE_FILES)
        self.assertEqual(status, 400)
        self.assertIn("no folder", body["error"])
        status, body = self.apply(self.ids(track), {"category": {"op": "clear"}})
        self.assertEqual(body["updated"], 1)
        self.assertEqual(self.where(track), (None, "ROCK/cleared.flac"))

    def test_c9_an_invalid_policy_or_a_move_without_a_category_change_is_refused(self):
        track = self.put("ROCK/invalid.flac")
        for mode, changes in (("relocate", self.to(self.pop)), (True, self.to(self.pop)),
                              (bulk.MOVE_FILES, {"energy": {"op": "set", "value": "upbeat"}})):
            with self.subTest(mode=mode):
                status, body = self.preview(self.ids(track), changes, mode)
                self.assertEqual(status, 400, body)
        self.assertEqual(self.where(track), ("ROCK", "ROCK/invalid.flac"))

    def test_c10_the_1000_track_selection_limit_is_unchanged(self):
        status, body = self.preview({"mode": "ids", "ids": list(range(1, 1002))}, self.to(self.pop), bulk.MOVE_FILES)
        self.assertEqual(status, 400)
        self.assertIn("limited to 1000", body["error"])


class MoveTests(_Bulk):
    def test_c2_c3_a_move_enabled_change_relocates_and_commits_category_and_path_together(self):
        a = self.put("ROCK/a.flac")
        b = self.put("ROCK/b.flac")
        status, body = self.preview(self.ids(a, b), self.to(self.jazz), bulk.MOVE_FILES)
        self.assertEqual((body["file_location"], body["moves_count"]), (bulk.MOVE_FILES, 2))
        self.assertEqual(body["moves"][0]["to"], "JAZZ/a.flac")
        self.assertTrue(any("unverified until you run Reanalyze Track" in w for w in body["warnings"]))
        status, result = self.apply(self.ids(a, b), self.to(self.jazz), bulk.MOVE_FILES, token=body["token"])
        self.assertEqual((status, result["updated"], result["moved"], result["outcome"]), (200, 2, 2, "completed"))
        self.assertEqual(result["files_left_in_previous_category_folder"], 0)
        for track in (a, b):
            code, rel = self.where(track)
            self.assertEqual((code, rel), ("JAZZ", f"JAZZ/{Path(track.filepath).name}"))
            self.assertEqual((self.root / rel).read_bytes(), AUDIO)
            self.assertFalse(os.path.lexists(track.filepath))

    def test_c4_a_failed_move_does_not_commit_that_tracks_category(self):
        a = self.put("ROCK/fails.flac")
        b = self.put("ROCK/works.flac")
        status, body = self.preview(self.ids(a, b), self.to(self.jazz), bulk.MOVE_FILES)
        real = m.stage_no_replace

        def failing(source, destination, **kwargs):
            if Path(source).name == "fails.flac":
                raise OSError("injected staging failure")
            return real(source, destination, **kwargs)

        with mock.patch.object(m, "stage_no_replace", side_effect=failing):
            status, result = self.apply(self.ids(a, b), self.to(self.jazz), bulk.MOVE_FILES, token=body["token"])
        self.assertEqual((result["updated"], result["moved"], [f["id"] for f in result["failed"]]), (1, 1, [a.pk]))
        self.assertEqual(self.where(a), ("ROCK", "ROCK/fails.flac"))           # no metadata-only fallback
        self.assertEqual(self.where(b), ("JAZZ", "JAZZ/works.flac"))
        self.assertEqual(result["outcome"], "partial")

    def test_c6_combined_edits_stay_one_per_track_transaction(self):
        a = self.put("ROCK/combo.flac")
        refused = self.put("ROCK/blocked.flac")
        os.link(refused.filepath, self.root / "ROCK" / "blocked-alias.flac")     # shared: cannot move
        changes = self.to(self.jazz, energy={"op": "set", "value": "slow"},
                          additional_categories={"add": [self.pop.pk]})
        status, body = self.preview(self.ids(a, refused), changes, bulk.MOVE_FILES)
        self.assertEqual([r["id"] for r in body["refusals"]], [refused.pk])
        status, result = self.apply(self.ids(a, refused), changes, bulk.MOVE_FILES, token=body["token"])
        row = Track.objects.get(pk=a.pk)
        self.assertEqual((row.category_id, row.energy, self.additional(a)), (self.jazz.pk, "slow", ["POP"]))
        untouched = Track.objects.get(pk=refused.pk)
        self.assertEqual((untouched.category_id, untouched.energy, self.additional(refused)),
                         (self.rock.pk, refused.energy, []))
        self.assertEqual([r["id"] for r in result["refused"]], [refused.pk])

    def test_c8_the_token_binds_the_policy(self):
        track = self.put("ROCK/policy.flac")
        for preview_mode, apply_mode in ((bulk.MOVE_FILES, bulk.METADATA_ONLY), (bulk.MOVE_FILES, None),
                                         (bulk.METADATA_ONLY, bulk.MOVE_FILES), (None, bulk.MOVE_FILES)):
            with self.subTest(preview=preview_mode, apply=apply_mode):
                status, body = self.apply(self.ids(track), self.to(self.pop), preview_mode, apply_mode=apply_mode)
                self.assertEqual(status, 409, body)
                self.assertIn("file-location choice", body["error"])
        self.assertEqual(self.where(track), ("ROCK", "ROCK/policy.flac"))

    def test_c11_at_most_25_files_move_per_bulk_edit(self):
        tracks = [self.put(f"ROCK/limit-{i:02d}.flac") for i in range(26)]
        status, body = self.preview(self.ids(*tracks), self.to(self.pop), bulk.MOVE_FILES)
        self.assertEqual((status, body["count"]), (400, 26))
        status, body = self.preview(self.ids(*tracks[:25]), self.to(self.pop), bulk.MOVE_FILES)
        self.assertEqual((status, body["moves_count"]), (200, 25))
        status, body = self.preview(self.ids(*tracks), self.to(self.pop), bulk.METADATA_ONLY)
        self.assertEqual(status, 200)                                   # metadata-only keeps the 1000 limit

    def test_c12_the_preview_reports_collisions_and_blocked_moves(self):
        clash = self.put("ROCK/clash.flac")
        self.file("JAZZ/clash.flac", b"OCCUPANT")
        live = self.put("ROCK/live.flac")
        twin_a = self.put("ROCK/twin.flac")
        twin_b = self.put("POP/twin.flac")
        fine = self.put("ROCK/fine.flac")
        self.engine(decks={"A": {"track_id": live.pk}, "B": None})
        status, body = self.post(PREVIEW, {"selection": self.ids(clash, live, twin_a, twin_b, fine),
                                           "changes": self.to(self.jazz), "file_location": bulk.MOVE_FILES})
        self.assertEqual((body["moves_count"], body["collisions"], body["scheduling_conflicts"]), (2, 2, 1))
        reasons = {r["id"]: r["reason"] for r in body["refusals"]}
        self.assertIn("never overwritten", reasons[clash.pk])
        self.assertIn("on air", reasons[live.pk])
        self.assertIn("same name to the same folder", reasons[twin_b.pk])
        self.assertEqual((self.root / "JAZZ" / "clash.flac").read_bytes(), b"OCCUPANT")

    def test_c13_partial_results_are_reported_truthfully(self):
        moved = self.put("ROCK/p-moved.flac")
        stale = self.put("ROCK/p-stale.flac")
        busy = self.put("ROCK/p-cleanup.flac")
        status, body = self.preview(self.ids(moved, stale, busy), self.to(self.jazz), bulk.MOVE_FILES)
        Track.objects.filter(pk=stale.pk).update(energy="upbeat")       # changed since the preview
        real = m.StagedMediaRelocation.remove_source

        def cleanup(staged):
            if staged.source.name == "p-cleanup.flac":
                raise OSError("injected")
            return real(staged)

        with mock.patch.object(m.StagedMediaRelocation, "remove_source", cleanup):
            status, result = self.apply(self.ids(moved, stale, busy), self.to(self.jazz), bulk.MOVE_FILES,
                                        token=body["token"])
        self.assertEqual((result["updated"], result["moved"], result["outcome"]), (2, 2, "partial"))
        self.assertEqual([s["id"] for s in result["stale"]], [stale.pk])
        self.assertEqual([c["id"] for c in result["cleanup_required"]], [busy.pk])
        self.assertIn("It is not retried", result["cleanup_required"][0]["reason"])
        self.assertEqual(self.where(busy), ("JAZZ", "JAZZ/p-cleanup.flac"))
        self.assertEqual(self.where(stale), ("ROCK", "ROCK/p-stale.flac"))

    def test_an_uncertain_outcome_stops_the_remaining_tracks(self):
        first = self.put("ROCK/u1.flac")
        second = self.put("ROCK/u2.flac")
        status, body = self.preview(self.ids(first, second), self.to(self.jazz), bulk.MOVE_FILES)
        uncertain = m.Result(outcome=m.UNCERTAIN, message="unconfirmed", track_id=first.pk, source=first.filepath,
                             destination=str(self.root / "JAZZ" / "u1.flac"))
        with mock.patch.object(bulk, "_apply_move", return_value=uncertain) as move:
            status, result = self.apply(self.ids(first, second), self.to(self.jazz), bulk.MOVE_FILES,
                                        token=body["token"])
        self.assertEqual(move.call_count, 1)
        self.assertEqual(([u["id"] for u in result["uncertain"]], [n["id"] for n in result["not_attempted"]]),
                         ([first.pk], [second.pk]))
        self.assertEqual(result["outcome"], "nothing_applied")

    def test_the_audit_records_the_policy_and_each_move(self):
        track = self.put("ROCK/audited.flac")
        with mock.patch("library.services.library_audit.record", return_value="action") as record:
            self.apply(self.ids(track), self.to(self.jazz), bulk.MOVE_FILES)
        actions = {c.args[0]: c.kwargs for c in record.call_args_list}
        self.assertEqual((actions["relocation.track"]["outcome"], actions["relocation.track"]["detail"]["origin"]),
                         ("moved", "bulk_edit"))
        self.assertEqual((actions["bulk_edit.apply"]["detail"]["file_location"],
                          actions["bulk_edit.apply"]["detail"]["moved"]), (bulk.MOVE_FILES, 1))


class C14ConcurrencyTests(_BulkFixture, TransactionTestCase):
    def test_c14_a_bulk_move_and_a_held_track_serialize_with_a_bounded_wait(self):
        track = self.put("ROCK/contended.flac")
        status, body = self.preview(self.ids(track), self.to(self.jazz), bulk.MOVE_FILES)
        held, release, out = threading.Event(), threading.Event(), {}

        def holder():
            try:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT id FROM library_track WHERE id = %s FOR UPDATE", [track.pk])
                    held.set()
                    release.wait(20)
            finally:
                connections.close_all()

        worker = threading.Thread(target=holder, daemon=True)
        worker.start()
        self.assertTrue(held.wait(10))
        try:
            started = time.monotonic()
            status, result = self.apply(self.ids(track), self.to(self.jazz), bulk.MOVE_FILES, token=body["token"])
            elapsed = time.monotonic() - started
        finally:
            release.set()
            worker.join(10)
        self.assertEqual([f["id"] for f in result["failed"]], [track.pk])
        self.assertIn("busy", result["failed"][0]["reason"])
        self.assertLess(elapsed, bulk.MOVE_LOCK_BUDGET_SECONDS + 3)
        self.assertEqual(self.where(track), ("ROCK", "ROCK/contended.flac"))
        # Once free, a fresh preview moves it.
        status, result = self.apply(self.ids(track), self.to(self.jazz), bulk.MOVE_FILES)
        self.assertEqual(result["moved"], 1)
