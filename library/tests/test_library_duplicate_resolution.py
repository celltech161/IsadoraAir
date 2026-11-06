"""Roadmap 3.12: resolving exact duplicate Track pairs from the Library Reconciliation page.

Every file lives in a per-test temporary Library root (library.tests.test_library_reconciliation_b's fixture):
LIBRARY_ROOT, WAVEFORMS_DIR and REPORTS_ROOT point there, so no station media, cache or report is touched.
The engine state is patched (no /run file is read) and the clock the service uses is fixed where an hour
boundary would matter."""
import json
import os
import re
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import Group
from django.core import signing
from django.db import DatabaseError
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from library.models import (Category, DuplicateCandidate, LogItem, Playlist, PlaylistItem, PlaylistLog, Rotation,
                            RotationSlot, Track, VoiceTrack)
from library.services import library_duplicate_resolution as resolution
from library.services import library_reconciliation as reconciliation
from library.services import track_deletion
from library.tests.test_library_reconciliation_b import User, _ReconFixture
from library.tests.test_library_reconciliation_launch import PAGE_HARNESS, TEMPLATE

PREVIEW = "library:api-reconciliation-duplicates-preview"
APPLY = "library:api-reconciliation-duplicates-apply"
SAME = b"\x01identical-audio" * 400
LOGS_TEMPLATE = TEMPLATE.parent / "logs.html"


class _Pairs(_ReconFixture):
    """Two byte-identical Tracks (A kept by default) filed under different categories, scanned."""

    def setUp(self):
        super().setUp()
        engine = mock.patch.object(resolution, "_engine_state", return_value=None)
        engine.start()
        self.addCleanup(engine.stop)
        kind = self.rock.kind
        self.jazz = Category.objects.create(code="JAZZ", name="Jazz", kind=kind)
        self.blues = Category.objects.create(code="BLUES", name="Blues", kind=kind)

    def pair(self, name, *, data=None, keep_category=None, discard_category=None):
        data = data or (SAME + name.encode())
        a = self.track(f"ROCK/{name}-a.flac", data=data, category=keep_category or self.rock)
        b = self.track(f"POP/{name}-b.flac", data=data, category=discard_category or self.pop)
        return a, b

    def scanned(self):
        self.report = reconciliation.run_scan()
        return self.report

    def finding(self, *tracks):
        ids = sorted(t.pk for t in tracks)
        return next(f for f in self.report["findings"] if f["type"] == "duplicate" and f.get("members") == ids)

    def post(self, name, payload, client=None):
        response = (client or self.client).post(reverse(name), payload, content_type="application/json")
        return response.status_code, response.json()

    def preview(self, *selections, scan_id=None):
        return self.post(PREVIEW, {"scan_id": scan_id or self.report["scan_id"], "selections": list(selections)})

    def delete(self, keep, discard):
        return {"finding_id": self.finding(keep, discard)["id"], "action": "delete", "keep": keep.pk,
                "discard": discard.pk}

    def apply(self, token, choices=None, confirm=True):
        return self.post(APPLY, {"token": token, "choices": choices or {}, "confirm": confirm})

    def additional(self, track):
        return sorted(track.additional_categories.values_list("code", flat=True))


class _Resolution(_Pairs, TestCase):
    pass


class EligibilityTests(_Resolution):
    def test_only_exact_findings_with_exactly_two_registered_tracks_are_deletable(self):
        a, b = self.pair("two")
        trio = [self.track(f"ROCK/trio-{i}.flac", data=SAME + b"trio") for i in range(3)]
        lone = self.track("ROCK/lone.flac", data=SAME + b"lone")
        self.file("POP/lone-copy.flac", SAME + b"lone")                       # a file-only copy
        like_a = self.track("ROCK/likely-1.flac", title="Same Song", data=b"x" * 900, duration=200.0)
        like_b = self.track("POP/likely-2.flac", title="Same Song", data=b"y" * 901, duration=200.5)
        self.scanned()
        response = self.client.get(reverse("library:api-reconciliation-findings"), {"type": "duplicate", "open": "1"})
        items = {tuple(f["members"]): f for f in response.json()["items"]}
        self.assertEqual(items[(a.pk, b.pk)]["resolution"], {"deletable": True, "reason": ""})
        self.assertFalse(items[tuple(sorted(t.pk for t in trio))]["resolution"]["deletable"])
        self.assertIn("3 Tracks share these bytes", items[tuple(sorted(t.pk for t in trio))]["resolution"]["reason"])
        self.assertIn("file review only", items[(lone.pk,)]["resolution"]["reason"])
        self.assertIn("this one is likely", items[(like_a.pk, like_b.pk)]["resolution"]["reason"])
        # The server, not the page, decides: a deletion of a review-only finding is blocked at preview.
        trio_finding = self.finding(*trio)
        status, body = self.preview({"finding_id": trio_finding["id"], "action": "delete", "keep": trio[0].pk,
                                     "discard": trio[1].pk})
        self.assertEqual((status, body["items"][0]["status"], body["token"]), (200, "blocked", None))
        likely = self.finding(like_a, like_b)
        status, body = self.preview({"finding_id": likely["id"], "action": "delete", "keep": like_a.pk,
                                     "discard": like_b.pk})
        self.assertIn("only identical-byte (exact)", body["items"][0]["reasons"][0])
        self.assertTrue(Track.objects.filter(pk__in=[t.pk for t in (*trio, like_a, like_b)]).count() == 5)

    def test_an_unregistered_copy_in_an_eligible_group_is_never_touched(self):
        a, b = self.pair("extra")
        copy = self.file("INBOX/extra-copy.flac", SAME + b"extra")
        self.scanned()
        finding = self.finding(a, b)
        self.assertEqual((finding["file_count"], finding["unregistered_file_count"]), (3, 1))
        status, body = self.preview(self.delete(a, b))
        with mock.patch.object(track_deletion, "SOURCE_FRESHNESS_GRACE_SECONDS", 0), \
                self.captureOnCommitCallbacks(execute=True):
            status, result = self.apply(body["token"])
        self.assertEqual(result["items"][0]["outcome"], "deleted")
        self.assertTrue(copy.exists())
        self.assertTrue(Path(a.filepath).exists())
        self.assertFalse(Path(b.filepath).exists())

    def test_a_suspect_scan_offers_no_deletion(self):
        a, b = self.pair("suspect")
        self.scanned()
        self.report["status"] = "suspect"
        reconciliation.write_report(self.report)
        status, body = self.preview(self.delete(a, b))
        self.assertEqual(status, 409)
        self.assertIn("not complete", body["error"])


class SingleAndBulkDeletionTests(_Resolution):
    def test_one_pair_preview_then_delete_with_the_suggested_category(self):
        a, b = self.pair("one")
        b.additional_categories.add(self.jazz)
        a.additional_categories.add(self.blues)
        self.scanned()
        status, body = self.preview(self.delete(a, b))
        self.assertEqual(status, 200, body)
        item = body["items"][0]
        self.assertEqual((item["status"], body["counts"]), ("eligible", {"eligible": 1, "needs_acknowledgment": 0,
                                                                         "blocked": 0}))
        self.assertEqual(sorted(c["code"] for c in item["suggested_categories"]), ["JAZZ", "POP"])
        self.assertEqual((item["keep_track"]["url"], item["discard_track"]["url"]),
                         (f"/track/{a.pk}/", f"/track/{b.pk}/"))
        self.assertEqual(item["discard_track"]["rel"], "POP/one-b.flac")
        self.assertEqual(item["discard_track"]["size"], len(SAME + b"one"))
        self.assertEqual(item["discard_track"]["category"]["code"], "POP")
        self.assertEqual(Track.objects.count(), 2)                       # a preview writes nothing
        self.assertEqual(self.additional(a), ["BLUES"])
        # The patch is the outer context: the captured on-commit cleanup runs while it still applies.
        with mock.patch.object(track_deletion, "SOURCE_FRESHNESS_GRACE_SECONDS", 0), \
                self.captureOnCommitCallbacks(execute=True):
            status, result = self.apply(body["token"], {item["finding_id"]: {
                "categories": [self.pop.pk, self.jazz.pk]}})
        self.assertEqual(status, 200, result)
        done = result["items"][0]
        self.assertEqual((done["outcome"], sorted(done["categories_added"]), result["rescan_recommended"]),
                         ("deleted", ["JAZZ", "POP"], True))
        self.assertFalse(Track.objects.filter(pk=b.pk).exists())
        a.refresh_from_db()
        self.assertEqual((a.category_id, self.additional(a)), (self.rock.pk, ["BLUES", "JAZZ", "POP"]))
        self.assertFalse(Path(b.filepath).exists())                     # removed once the deletion committed
        self.assertTrue(Path(a.filepath).exists())
        self.assertEqual(Path(a.filepath).read_bytes(), SAME + b"one")  # the survivor's audio is untouched
        self.assertFalse(DuplicateCandidate.objects.exists())           # no keep_a / keep_b row for the old CLI
        from monitoring.models import SystemEvent
        actions = sorted(e.detail.get("action") for e in SystemEvent.objects.filter(category="library"))
        self.assertEqual(actions, ["reconciliation.duplicate_delete", "reconciliation.duplicate_resolution"])

    def test_bulk_partial_success_processes_each_pair_independently(self):
        pairs = [self.pair(f"bulk{i}") for i in range(4)]
        playlist = Playlist.objects.create(name="Morning")
        PlaylistItem.objects.create(playlist=playlist, position=1, track=pairs[1][1])
        rotation = Rotation.objects.create(name="Hot")
        RotationSlot.objects.create(rotation=rotation, position=1, track=pairs[2][1])
        self.scanned()
        status, body = self.preview(*(self.delete(a, b) for a, b in pairs))
        self.assertEqual(body["counts"], {"eligible": 2, "needs_acknowledgment": 0, "blocked": 2})
        self.assertIn("playlist(s) 'Morning'", body["items"][1]["reasons"][0])
        self.assertIn("rotation(s) 'Hot'", body["items"][2]["reasons"][0])
        status, result = self.apply(body["token"])
        self.assertEqual(status, 200)
        self.assertEqual([r["finding_id"] for r in result["items"]],
                         [body["items"][0]["finding_id"], body["items"][3]["finding_id"]])
        self.assertEqual(result["counts"], {"deleted": 2})
        self.assertEqual(result["items"][0]["cleanup"]["outcome"], "pending")   # this test's transaction never commits
        remaining = set(Track.objects.values_list("pk", flat=True))
        self.assertEqual(remaining, {pairs[0][0].pk, pairs[1][0].pk, pairs[1][1].pk, pairs[2][0].pk, pairs[2][1].pk,
                                     pairs[3][0].pk})
        # Unblock one and retry: the same scan still offers it, and only it is applied.
        PlaylistItem.objects.all().delete()
        status, body = self.preview(self.delete(*pairs[1]), self.delete(*pairs[2]))
        self.assertEqual([i["status"] for i in body["items"]], ["eligible", "blocked"])
        status, result = self.apply(body["token"])
        self.assertEqual(result["counts"], {"deleted": 1})
        self.assertFalse(Track.objects.filter(pk=pairs[1][1].pk).exists())
        self.assertTrue(Track.objects.filter(pk=pairs[2][1].pk).exists())

    def test_ten_pairs_eight_eligible(self):
        pairs = [self.pair(f"ten{i}") for i in range(10)]
        rotation = Rotation.objects.create(name="Gold")
        RotationSlot.objects.create(rotation=rotation, position=1, track=pairs[3][1])
        RotationSlot.objects.create(rotation=rotation, position=2, track=pairs[7][1])
        self.scanned()
        status, body = self.preview(*(self.delete(a, b) for a, b in pairs))
        self.assertEqual(body["counts"], {"eligible": 8, "needs_acknowledgment": 0, "blocked": 2})
        status, result = self.apply(body["token"])
        self.assertEqual((result["counts"], result["outcome"]), ({"deleted": 8}, "completed"))
        self.assertEqual(Track.objects.count(), 12)
        self.assertTrue(Track.objects.filter(pk__in=[pairs[3][1].pk, pairs[7][1].pk]).count() == 2)


class CategoryTests(_Resolution):
    def test_suggestions_can_be_deselected_and_nothing_else_changes(self):
        a, b = self.pair("cats", keep_category=self.rock, discard_category=self.pop)
        b.additional_categories.add(self.jazz, self.rock)          # ROCK is A's primary: never suggested
        a.additional_categories.add(self.blues)
        self.scanned()
        status, body = self.preview(self.delete(a, b))
        item = body["items"][0]
        self.assertEqual(sorted(c["code"] for c in item["suggested_categories"]), ["JAZZ", "POP"])
        status, result = self.apply(body["token"], {item["finding_id"]: {"categories": [self.jazz.pk]}})
        self.assertEqual(result["items"][0]["categories_added"], ["JAZZ"])
        a.refresh_from_db()
        self.assertEqual((a.category_id, self.additional(a)), (self.rock.pk, ["BLUES", "JAZZ"]))

    def test_no_category_is_written_unless_chosen(self):
        a, b = self.pair("none")
        self.scanned()
        status, body = self.preview(self.delete(a, b))
        status, result = self.apply(body["token"])                 # no choices sent: nothing added
        self.assertEqual((result["items"][0]["outcome"], result["items"][0]["categories_added"]), ("deleted", []))
        self.assertEqual(self.additional(a), [])

    def test_a_category_that_was_not_suggested_is_refused(self):
        a, b = self.pair("forged")
        self.scanned()
        status, body = self.preview(self.delete(a, b))
        fid = body["items"][0]["finding_id"]
        status, result = self.apply(body["token"], {fid: {"categories": [self.blues.pk]}})
        self.assertEqual(result["items"][0]["outcome"], "refused")
        self.assertTrue(Track.objects.filter(pk=b.pk).exists())
        self.assertEqual(self.additional(a), [])
        status, result = self.apply(body["token"], {fid: {"categories": ["POP"]}})
        self.assertEqual(status, 400)

    def test_categories_and_deletion_commit_together_or_not_at_all(self):
        """A real PROTECT refusal at deletion time (the inspection is made to miss the playlist) rolls back the
        category already written in the same transaction; a database failure does too; the next pair is
        unaffected."""
        a, b = self.pair("atomic")
        c, d = self.pair("other")
        self.scanned()
        status, body = self.preview(self.delete(a, b), self.delete(c, d))
        PlaylistItem.objects.create(playlist=Playlist.objects.create(name="Late"), position=1, track=b)
        real = resolution._references
        with mock.patch.object(resolution, "_references",
                               lambda track_id: {**real(track_id), "playlist_count": 0, "playlists": []}):
            status, result = self.apply(body["token"], {f["finding_id"]: {"categories": [self.pop.pk]}
                                                        for f in body["items"]})
        self.assertEqual([r["outcome"] for r in result["items"]], ["blocked", "deleted"])
        self.assertIn("PlaylistItem", result["items"][0]["message"])
        self.assertEqual(self.additional(a), [])                    # rolled back with the refused deletion
        self.assertTrue(Track.objects.filter(pk=b.pk).exists())
        self.assertEqual(self.additional(c), ["POP"])
        PlaylistItem.objects.all().delete()
        status, body = self.preview(self.delete(a, b))
        with mock.patch("library.services.track_deletion.delete_track", side_effect=DatabaseError("boom")):
            status, result = self.apply(body["token"], {body["items"][0]["finding_id"]: {"categories": [self.pop.pk]}})
        self.assertEqual(result["items"][0]["outcome"], "failed")
        self.assertEqual(self.additional(a), [])
        self.assertTrue(Track.objects.filter(pk=b.pk).exists())


class LogAndOnAirTests(_Resolution):
    def at(self, hours=0):
        now = timezone.localtime().replace(minute=20, second=0, microsecond=0) + timedelta(hours=hours)
        return now

    def schedule(self, track, when, **fields):
        log, _ = PlaylistLog.objects.get_or_create(date=when.date(), hour=when.hour, defaults={"status": "approved"})
        return LogItem.objects.create(playlist_log=log, position=LogItem.objects.filter(playlist_log=log).count() + 1,
                                      scheduled_time=when, track=track, **fields)

    def test_current_and_upcoming_logs_block_with_links_and_a_retry_rechecks(self):
        a, b = self.pair("logs")
        now = self.at()
        current = self.schedule(b, now)
        later = self.schedule(b, now + timedelta(hours=26))
        self.schedule(b, now - timedelta(hours=30))                 # history never blocks
        self.scanned()
        with mock.patch.object(resolution, "_now", return_value=now):
            status, body = self.preview(self.delete(a, b))
        item = body["items"][0]
        self.assertEqual(item["status"], "blocked")
        logs = item["discard_track"]["references"]["upcoming_logs"]
        self.assertEqual([(l["date"], l["hour"]) for l in logs],
                         [(now.date().isoformat(), now.hour), ((now + timedelta(hours=26)).date().isoformat(),
                                                                (now + timedelta(hours=26)).hour)])
        self.assertEqual(logs[0]["url"], f"/logs/?date={now.date().isoformat()}&hour={now.hour}&return=reconciliation")
        self.assertIsNone(body["token"])
        # The operator edits the logs (here: removes the entries) and re-checks: now eligible.
        current.delete()
        later.delete()
        with mock.patch.object(resolution, "_now", return_value=now):
            status, body = self.preview(self.delete(a, b))
            self.assertEqual(body["items"][0]["status"], "eligible")
            # ...but a log entry added after the preview blocks the apply.
            self.schedule(b, now + timedelta(hours=1))
            status, result = self.apply(body["token"])
        self.assertEqual(result["items"][0]["outcome"], "blocked")
        self.assertTrue(Track.objects.filter(pk=b.pk).exists())

    def test_the_engines_active_log_and_on_air_evidence_block(self):
        a, b = self.pair("air")
        now = self.at()
        self.scanned()
        earlier = self.schedule(b, now - timedelta(hours=5))
        cases = {
            "active log": dict(engine={"log_id": earlier.playlist_log_id}),
            "deck": dict(engine={"decks": {"A": {"track_id": b.pk}}}),
            "queue": dict(engine={"queue": [{"item_id": 1, "track_id": b.pk}]}),
        }
        for name, case in cases.items():
            with self.subTest(name), mock.patch.object(resolution, "_now", return_value=now), \
                    mock.patch.object(resolution, "_engine_state", return_value=case["engine"]):
                status, body = self.preview(self.delete(a, b))
                self.assertEqual(body["items"][0]["status"], "blocked", body["items"][0])
        for name, fields in {"claimed": {"playback_claimed_at": now - timedelta(minutes=2)},
                             "playing": {"played_at": now - timedelta(minutes=1)}}.items():
            with self.subTest(name):
                item = self.schedule(b, now - timedelta(hours=6), **fields)
                with mock.patch.object(resolution, "_now", return_value=now):
                    status, body = self.preview(self.delete(a, b))
                self.assertIn("may be on air", " ".join(body["items"][0]["reasons"]))
                item.delete()
        with mock.patch.object(resolution, "_now", return_value=now):
            status, body = self.preview(self.delete(a, b))
        self.assertEqual(body["items"][0]["status"], "eligible")


class VoiceTrackTests(_Resolution):
    def test_voice_tracks_need_a_specific_acknowledgment(self):
        a, b = self.pair("vt")
        VoiceTrack.objects.create(track=b, position="intro", filepath="/nonexistent/vt-intro.wav")
        VoiceTrack.objects.create(track=b, position="outro", filepath="/nonexistent/vt-outro.wav")
        self.scanned()
        status, body = self.preview(self.delete(a, b))
        item = body["items"][0]
        self.assertEqual((item["status"], item["voicetracks"]), ("needs_acknowledgment", 2))
        fid = item["finding_id"]
        status, result = self.apply(body["token"])
        self.assertEqual(result["items"][0]["outcome"], "needs_acknowledgment")
        self.assertEqual(VoiceTrack.objects.filter(track=b).count(), 2)
        status, result = self.apply(body["token"], {fid: {"include": False}})
        self.assertEqual(result["items"][0]["outcome"], "cancelled")
        status, result = self.apply(body["token"], {fid: {"acknowledge_voicetracks": True}})
        self.assertEqual((result["items"][0]["outcome"], result["items"][0]["voicetracks_deleted"]), ("deleted", 2))
        self.assertFalse(VoiceTrack.objects.filter(track_id=b.pk).exists())

    def test_a_voice_track_added_after_the_preview_is_never_deleted_unacknowledged(self):
        a, b = self.pair("vtlate")
        self.scanned()
        status, body = self.preview(self.delete(a, b))
        VoiceTrack.objects.create(track=b, position="intro", filepath="/nonexistent/late.wav")
        status, result = self.apply(body["token"], {body["items"][0]["finding_id"]: {"acknowledge_voicetracks": True}})
        self.assertEqual(result["items"][0]["outcome"], "stale")
        self.assertEqual(VoiceTrack.objects.filter(track=b).count(), 1)


class RevalidationTests(_Resolution):
    def ready(self, name="re"):
        a, b = self.pair(name)
        self.scanned()
        status, body = self.preview(self.delete(a, b))
        self.assertEqual(body["items"][0]["status"], "eligible")
        return a, b, body

    def test_changes_after_the_preview_make_the_pair_stale(self):
        changes = {
            "title": lambda a, b: Track.objects.filter(pk=b.pk).update(title="Renamed"),
            "survivor category": lambda a, b: a.additional_categories.add(self.jazz),
            "discard category": lambda a, b: Track.objects.filter(pk=b.pk).update(category=self.jazz),
            "discard deleted": lambda a, b: Track.objects.filter(pk=b.pk).delete(),
            "survivor deleted": lambda a, b: Track.objects.filter(pk=a.pk).delete(),
            "file touched": lambda a, b: os.utime(b.filepath, ns=(1, 1)),
            "file replaced": lambda a, b: (os.unlink(b.filepath), Path(b.filepath).write_bytes(SAME + b"re4")),
        }
        for i, (name, change) in enumerate(changes.items()):
            with self.subTest(name):
                a, b, body = self.ready(f"re{i}")
                change(a, b)
                status, result = self.apply(body["token"])
                self.assertEqual(result["items"][0]["outcome"], "stale", result["items"][0])
                # Nothing was deleted by the apply: each Track is gone only if the change itself removed it.
                self.assertEqual(Track.objects.filter(pk=b.pk).exists(), name != "discard deleted")
                self.assertEqual(Track.objects.filter(pk=a.pk).exists(), name != "survivor deleted")

    def test_scan_evidence_that_no_longer_holds_blocks_the_preview(self):
        a, b = self.pair("evidence")
        self.scanned()
        Path(b.filepath).write_bytes(SAME + b"evidence-changed")      # different bytes since the scan
        status, body = self.preview(self.delete(a, b))
        self.assertIn("changed since the scan", body["items"][0]["reasons"][0])
        Path(a.filepath).unlink()
        status, body = self.preview(self.delete(a, b))
        self.assertTrue(any("kept Track: its file no longer exists" in r for r in body["items"][0]["reasons"]))
        self.assertEqual(Track.objects.count(), 2)

    def test_a_newer_scan_refuses_the_preview_and_the_apply(self):
        a, b, body = self.ready("newer")
        reconciliation.run_scan()
        status, result = self.apply(body["token"])
        self.assertEqual(status, 409)
        self.assertIn("newer scan", result["error"])
        status, result = self.preview(self.delete(a, b), scan_id=body["scan_id"])
        self.assertEqual(status, 409)
        self.assertTrue(Track.objects.filter(pk=b.pk).exists())

    def test_tokens_are_bound_to_the_operator_and_expire_and_need_confirmation(self):
        a, b, body = self.ready("token")
        status, result = self.apply(body["token"], confirm=False)
        self.assertEqual((status, Track.objects.filter(pk=b.pk).exists()), (400, True))
        other = User.objects.create_superuser("other-admin", "o@example.invalid", "pw")
        client = Client()
        client.force_login(other)
        status, result = self.post(APPLY, {"token": body["token"], "confirm": True}, client=client)
        self.assertEqual(status, 403)
        with mock.patch.object(resolution, "TOKEN_MAX_AGE_SECONDS", -1), \
                self.captureOnCommitCallbacks(execute=True):
            status, result = self.apply(body["token"])
        self.assertEqual(status, 409)
        from monitoring.models import SystemEvent
        refused = [e.detail for e in SystemEvent.objects.filter(category="library")
                   if e.detail.get("action") == "reconciliation.duplicate_resolution"]
        self.assertEqual([(e["outcome"], e["status"]) for e in refused], [("refused", 409)])
        forged = signing.dumps({"u": self.admin.pk, "s": body["scan_id"], "p": []}, salt="something.else")
        self.assertEqual(self.apply(forged)[0], 400)
        self.assertTrue(Track.objects.filter(pk=b.pk).exists())
        status, result = self.apply(body["token"])                   # replay-safe: the first apply deletes,
        status, again = self.apply(body["token"])                    # the replay finds nothing to delete
        self.assertEqual((result["items"][0]["outcome"], again["items"][0]["outcome"]), ("deleted", "stale"))


class SelectionTests(_Resolution):
    def test_conflicting_and_foreign_selections_are_blocked(self):
        a, b = self.pair("conflict")
        c, d = self.pair("fine")
        stranger = self.track("ROCK/stranger.flac")
        self.scanned()
        fid = self.finding(a, b)["id"]
        status, body = self.preview(
            {"finding_id": fid, "action": "delete", "keep": a.pk, "discard": b.pk},
            {"finding_id": fid, "action": "delete", "keep": b.pk, "discard": a.pk},
            self.delete(c, d),
            {"finding_id": self.finding(c, d)["id"], "action": "delete", "keep": c.pk, "discard": stranger.pk})
        statuses = [i["status"] for i in body["items"]]
        self.assertEqual(statuses, ["blocked", "blocked", "blocked", "blocked"])
        self.assertIn("same finding or Track", body["items"][0]["reasons"][0])
        self.assertIsNone(body["token"])
        status, body = self.preview(self.delete(c, d), {"finding_id": "F999999", "action": "delete", "keep": a.pk,
                                                        "discard": b.pk})
        self.assertEqual([i["status"] for i in body["items"]], ["eligible", "blocked"])

    def test_malformed_and_oversized_requests_are_refused(self):
        a, b = self.pair("shape")
        self.scanned()
        fid = self.finding(a, b)["id"]
        for selections in ([], "x", [{"finding_id": fid, "action": "keep_a"}],
                           [{"finding_id": fid, "action": "delete", "keep": "1", "discard": b.pk}],
                           [self.delete(a, b)] * (resolution.MAX_SELECTIONS + 1)):
            with self.subTest(selections=str(selections)[:40]):
                self.assertEqual(self.preview(*selections)[0] if isinstance(selections, list) and selections
                                 else self.post(PREVIEW, {"scan_id": self.report["scan_id"],
                                                          "selections": selections})[0], 400)

    def test_keep_both_records_the_existing_decision_and_never_deletes(self):
        a, b = self.pair("both")
        c, d = self.pair("legacy")
        DuplicateCandidate.objects.create(track_a=c, track_b=d, confidence="exact", resolution="keep_a")
        self.scanned()
        status, body = self.preview({"finding_id": self.finding(a, b)["id"], "action": "keep_both"},
                                    {"finding_id": self.finding(c, d)["id"], "action": "keep_both"})
        self.assertIn("legacy decision (keep_a)", body["items"][1]["warnings"][0])
        status, result = self.apply(body["token"], confirm=False)          # no deletion: no confirmation needed
        self.assertEqual([r["outcome"] for r in result["items"]], ["recorded", "already_decided"])
        self.assertEqual(sorted(DuplicateCandidate.objects.values_list("resolution", flat=True)),
                         ["keep_a", "keep_both"])
        self.assertEqual(Track.objects.count(), 4)

    def test_deleting_a_track_removes_its_pending_legacy_decision(self):
        a, b = self.pair("supersede")
        DuplicateCandidate.objects.create(track_a=a, track_b=b, confidence="exact", resolution="keep_a")
        self.scanned()
        status, body = self.preview(self.delete(b, a))                     # the opposite of the legacy decision
        self.assertEqual(body["items"][0]["legacy_decision"], {"resolution": "keep_a", "applied": False})
        self.apply(body["token"])
        self.assertFalse(Track.objects.filter(pk=a.pk).exists())
        self.assertFalse(DuplicateCandidate.objects.exists())              # nothing left for the old CLI to apply


class PermissionTests(_Resolution):
    def test_managers_only_and_csrf_is_enforced(self):
        a, b = self.pair("perm")
        self.scanned()
        user = User.objects.create_user("contrib-res", "c@example.invalid", "pw")
        user.groups.add(Group.objects.get_or_create(name="Contributor")[0])
        client = Client()
        client.force_login(user)
        self.assertEqual(self.post(PREVIEW, {"scan_id": self.report["scan_id"], "selections": [self.delete(a, b)]},
                                   client=client)[0], 403)
        status, body = self.preview(self.delete(a, b))
        self.assertEqual(self.post(APPLY, {"token": body["token"], "confirm": True}, client=client)[0], 403)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.admin)
        response = strict.post(reverse(APPLY), {"token": body["token"], "confirm": True}, content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get(reverse(PREVIEW)).status_code, 405)
        self.assertEqual(Track.objects.count(), 2)


class CleanupReportingTests(_Pairs, TransactionTestCase):
    """Real commits: the source cleanup runs after each pair's transaction and its result is reported exactly."""

    def test_completed_retained_and_failed_cleanup_are_distinguished(self):
        pairs = [self.pair(f"clean{i}") for i in range(3)]
        self.scanned()
        status, body = self.preview(*(self.delete(a, b) for a, b in pairs))
        real_unlink = Path.unlink
        failing = Path(pairs[2][1].filepath)

        def unlink(path, *args, **kwargs):
            if path == failing:
                raise PermissionError(13, "denied")
            return real_unlink(path, *args, **kwargs)

        # Cleanup's clock is two minutes ahead, so every file is past the freshness grace -- except the second,
        # whose identity is made to look changed at that very moment (a delivery in progress?).
        later = time.time_ns() + 120 * 10 ** 9
        fresh = pairs[1][1].filepath
        real_witness = track_deletion._source_witness

        def witness(path):
            value = real_witness(path)
            if str(path) == fresh and isinstance(value, tuple):
                value = (*value[:4], later, value[5])
            return value

        with mock.patch.object(track_deletion, "SOURCE_FRESHNESS_GRACE_SECONDS", 60), \
                mock.patch.object(track_deletion, "_now_ns", lambda: later), \
                mock.patch.object(track_deletion, "_source_witness", witness), \
                mock.patch.object(Path, "unlink", unlink):
            status, result = self.apply(body["token"])
        outcomes = [(r["outcome"], r["cleanup"]["status"], r["cleanup"]["outcome"]) for r in result["items"]]
        self.assertEqual(outcomes, [("deleted", "removed", "completed"), ("deleted", "retained_recent", "retained"),
                                    ("deleted", "failed", "failed")])
        self.assertFalse(Path(pairs[0][1].filepath).exists())
        self.assertTrue(Path(pairs[1][1].filepath).exists())
        self.assertTrue(failing.exists())
        self.assertIn("remove the file by hand", result["items"][2]["cleanup"]["message"])
        self.assertEqual(Track.objects.count(), 3)


def _ungranted_locks():
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_locks WHERE NOT granted")
        return cursor.fetchone()[0]


def _wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class ConcurrencyTests(_Pairs, TransactionTestCase):
    """Real concurrent transactions: the pair's row locks serialize competing writers."""

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

    def holding_locks(self, holder):
        """Patch _inspect so thread ``holder`` stops right after it has locked the pair's rows."""
        import threading
        held, release, real = threading.Event(), threading.Event(), resolution._inspect

        def inspect(*args, **kwargs):
            if kwargs.get("tracks") is not None and threading.current_thread().name == holder and not held.is_set():
                held.set()
                release.wait(20)
            return real(*args, **kwargs)
        return mock.patch.object(resolution, "_inspect", inspect), held, release

    def apply_as(self, token, user):
        return resolution.apply(token, {}, confirm=True, user=user)

    def test_two_operators_applying_the_same_pair_delete_it_once(self):
        a, b = self.pair("race")
        self.scanned()
        status, body = self.preview(self.delete(a, b))
        other = User.objects.create_superuser("race-admin", "r@example.invalid", "pw")
        client = Client()
        client.force_login(other)
        status, body2 = self.post(PREVIEW, {"scan_id": self.report["scan_id"], "selections": [self.delete(a, b)]},
                                  client=client)
        patcher, held, release = self.holding_locks("first")
        first, second = {}, {}
        with patcher:
            t1 = self.start("first", lambda: self.apply_as(body["token"], self.admin), first)
            self.assertTrue(held.wait(10))
            t2 = self.start("second", lambda: self.apply_as(body2["token"], other), second)
            self.assertTrue(_wait_until(lambda: _ungranted_locks() >= 1), "the second apply did not wait")
            release.set()
            t1.join(20)
            t2.join(20)
        self.assertNotIn("error", first)
        self.assertNotIn("error", second)
        self.assertEqual((first["value"]["items"][0]["outcome"], second["value"]["items"][0]["outcome"]),
                         ("deleted", "stale"))
        self.assertEqual(list(Track.objects.values_list("pk", flat=True)), [a.pk])

    def test_a_log_entry_inserted_during_the_deletion_waits_and_then_fails(self):
        from django.db import IntegrityError
        a, b = self.pair("insert")
        self.scanned()
        status, body = self.preview(self.delete(a, b))
        log = PlaylistLog.objects.create(date=timezone.localdate() + timedelta(days=2), hour=3, status="draft")
        patcher, held, release = self.holding_locks("apply")
        applied, inserted = {}, {}
        with patcher:
            t1 = self.start("apply", lambda: self.apply_as(body["token"], self.admin), applied)
            self.assertTrue(held.wait(10))
            t2 = self.start("insert", lambda: LogItem.objects.create(
                playlist_log=log, position=1, scheduled_time=timezone.now() + timedelta(days=2), track_id=b.pk),
                inserted)
            self.assertTrue(_wait_until(lambda: _ungranted_locks() >= 1), "the insert did not wait for the row lock")
            release.set()
            t1.join(20)
            t2.join(20)
        self.assertEqual(applied["value"]["items"][0]["outcome"], "deleted")
        self.assertIsInstance(inserted.get("error"), IntegrityError)
        self.assertFalse(LogItem.objects.filter(track_id=b.pk).exists())
        self.assertFalse(LogItem.objects.filter(playlist_log=log).exists())


class LogsDeepLinkTests(_Resolution):
    def test_the_logs_page_has_the_return_link(self):
        body = self.client.get("/logs/?date=2026-11-06&hour=14&return=reconciliation").content.decode()
        self.assertIn('id="returnLink"', body)
        self.assertIn(reverse("library:library-reconciliation"), body)

    def test_linked_hour_parsing(self):
        source = LOGS_TEMPLATE.read_text()
        code = re.search(r"function linkedHour\(search\) \{.*?\n\}\n", source, re.S).group(0)
        out = _node(self, code + """
console.log(JSON.stringify(['?date=2026-11-06&hour=14&return=reconciliation', '?date=2026-11-06&hour=7',
  '?date=2026-11-06&hour=24', '?date=bad&hour=1', '?hour=3', ''].map(linkedHour)));""")
        self.assertEqual(out, [{"date": "2026-11-06", "hour": 14, "back": True},
                               {"date": "2026-11-06", "hour": 7, "back": False}, None, None, None, None])


def _node(test, code):
    import shutil
    node = shutil.which("node")
    if node is None:
        try:
            import playwright
            candidate = Path(playwright.__file__).parent / "driver" / "node"
            node = str(candidate) if candidate.exists() else None
        except ImportError:
            node = None
    if node is None:
        test.skipTest("no Node.js available to run the page script")
    result = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=60)
    test.assertEqual(result.returncode, 0, result.stderr)
    return json.loads(result.stdout)


# The page script under Node, as the launcher tests run it (library.tests.test_library_reconciliation_launch),
# plus request bodies and a search of the fake DOM.
PAGE_EXTRA = r"""
globalThis.Node = El;                    // the page's trackTable() tests `instanceof Node`
const realFetch = globalThis.fetch;
globalThis.fetch = (url, opts = {}) => { const p = realFetch(url, opts); requests[requests.length - 1].body = opts.body; return p; };
function walk(node, pred, out = []) {
  if (!node || typeof node === 'string') return out;
  if (pred(node)) out.push(node);
  (node.children || []).forEach(c => walk(c, pred, out));
  return out;
}
const byText = (root, text) => walk(root, n => n.textContent === text);
const button = (root, prefix) => walk(root, n => n.tag === 'button' && n.textContent.startsWith(prefix))[0];
const radio = (name, value) => walk(ids.findings, n => n.tag === 'input' && n.name === name && n.value === value)[0];
const PREVIEW = '/api/tracks/reconciliation/duplicates/preview/', APPLY = '/api/tracks/reconciliation/duplicates/apply/';
const trackRow = (id, extra = {}) => ({ id, title: 'T' + id, artist: 'Ar', album: '', duration: 180, format: 'flac',
  size: 1000, file_present: true, directory: '/srv', category: 'ROCK', additional_categories: [], playlist_refs: 0,
  rotation_refs: 0, upcoming_log_items: 0, voicetracks: 0, play_count: 0, ...extra });
const dup = (id, a, b, deletable = true) => ({ id, type: 'duplicate', confidence: deletable ? 'exact' : 'likely',
  reason: 'identical file bytes', members: [a, b], member_count: 2, pair_count: 1, open: true,
  track_pair_review_open: true, files: [], tracks: [trackRow(a), trackRow(b)], decisions: {},
  resolution: { deletable, reason: deletable ? '' : 'only identical-byte (exact) findings can be resolved here' } });
const PAGE1 = { page: 1, pages: 2, total: 30, scan_id: 'A',
  items: [dup('F1', 1, 2), dup('F3', 3, 4), dup('F4', 5, 6), dup('F5', 7, 8, false)] };
async function bootDup(items = PAGE1) {
  await flush();
  respond(open(r => r.url === SUMMARY)[0], 200, status(A, A));
  respond(open(isFindings)[0], 200, items);
  await flush();
}
function choose(name, value) { const r = radio(name, value); r.checked = true; r.listeners.change[0](); }
const desc = (id, extra = {}) => ({ id, url: `/track/${id}/`, title: 'T' + id, artist: 'Ar', rel: `X/${id}.flac`,
  format: 'flac', size: 1000, category: { id: 1, code: 'ROCK', name: 'Rock' }, additional_categories: [],
  references: { playlist_count: 0, rotation_count: 0, upcoming_log_items: 0, upcoming_log_count: 0, upcoming_logs: [],
                voicetracks: 0 }, ...extra });
"""


class PageTests(_Resolution):
    def run_page(self, scenario):
        source = TEMPLATE.read_text()
        page = source[source.index("<script>") + len("<script>"):source.index("</script>")]
        return _node(self, PAGE_HARNESS + PAGE_EXTRA + page + "\n(async () => {\n" + scenario +
                     "\n})().catch(e => { console.error(e); process.exit(1); });\n")

    def test_pure_helpers(self):
        source = TEMPLATE.read_text()
        code = source[source.index("// resolve:begin"):source.index("// resolve:end")]
        out = _node(self, code + r"""
const m = new Map([['F1', choiceFor('keep_a', 1, 2)], ['F2', choiceFor('keep_b', 3, 4)], ['F3', choiceFor('keep_both', 5, 6)]]);
const preview = { items: [
  { finding_id: 'F1', action: 'delete', status: 'eligible', suggested_categories: [{ id: 7 }, { id: 8 }] },
  { finding_id: 'F2', action: 'delete', status: 'needs_acknowledgment', suggested_categories: [] },
  { finding_id: 'F3', action: 'keep_both', status: 'eligible' },
  { finding_id: 'F9', action: 'delete', status: 'blocked' } ] };
const o = initialOptions(preview);
const before = readiness(preview, o);
o.get('F1').categories.delete(7);
o.get('F2').ack = 'delete'; o.get('F2').include = true;
const after = readiness(preview, o);
console.log(JSON.stringify({
  payload: selectionsPayload(m), keys: [choiceKey(m.get('F1'), 1, 2), choiceKey(m.get('F2'), 3, 4), choiceKey(m.get('F3'), 5, 6),
    choiceKey(undefined, 1, 2)], none: choiceFor('none', 1, 2), blockedHasOptions: o.has('F9'), before, after,
  choices: applyChoices(preview, o),
  kept: [...choicesAfterApply(m, [{ finding_id: 'F1', outcome: 'deleted' }, { finding_id: 'F2', outcome: 'stale' },
                                  { finding_id: 'F3', outcome: 'recorded' }]).keys()] }));
""")
        self.assertEqual(out["payload"], [
            {"finding_id": "F1", "action": "delete", "keep": 1, "discard": 2},
            {"finding_id": "F2", "action": "delete", "keep": 4, "discard": 3},
            {"finding_id": "F3", "action": "keep_both"}])
        self.assertEqual(out["keys"], ["keep_a", "keep_b", "keep_both", "none"])
        self.assertIsNone(out["none"])
        self.assertFalse(out["blockedHasOptions"])
        self.assertEqual(out["before"], {"deletions": 1, "keeps": 1, "undecided": 1, "ready": False})
        self.assertEqual(out["after"], {"deletions": 2, "keeps": 1, "undecided": 0, "ready": True})
        self.assertEqual(out["choices"], {
            "F1": {"include": True, "categories": [8], "acknowledge_voicetracks": False},
            "F2": {"include": True, "categories": [], "acknowledge_voicetracks": True},
            "F3": {"include": True}})
        self.assertEqual(out["kept"], ["F2"])

    def test_select_preview_acknowledge_confirm_apply_and_keep_the_blocked(self):
        out = self.run_page(r"""
  await bootDup();
  const offered = { F1: !!radio('resolve-F1', 'keep_a'), F5: !!radio('resolve-F5', 'keep_a'),
                    defaultNone: radio('resolve-F1', 'none').checked, barHidden: ids.resolveBar.hidden };
  choose('resolve-F1', 'keep_a'); choose('resolve-F3', 'keep_b'); choose('resolve-F4', 'keep_a');
  const bar = ids.resolveBar.text;
  button(ids.resolveBar, 'Review selected').listeners.click[0]();
  await flush();
  const previewReq = open(r => r.url === PREVIEW)[0];
  const previewBody = JSON.parse(previewReq.body);
  respond(previewReq, 200, { scan_id: 'A', token: 'tok', deletions: 2, counts: { eligible: 1, needs_acknowledgment: 1, blocked: 1 },
    items: [
      { finding_id: 'F1', action: 'delete', status: 'eligible', keep: 1, discard: 2, keep_track: desc(1), discard_track: desc(2),
        suggested_categories: [{ id: 7, code: 'POP', name: 'Pop' }], voicetracks: 0, reasons: [], warnings: [] },
      { finding_id: 'F3', action: 'delete', status: 'needs_acknowledgment', keep: 4, discard: 3, keep_track: desc(4),
        discard_track: desc(3), suggested_categories: [{ id: 9, code: 'JAZZ', name: 'Jazz' }], voicetracks: 2, reasons: [], warnings: [] },
      { finding_id: 'F4', action: 'delete', status: 'blocked', keep: 5, discard: 6, keep_track: desc(5),
        discard_track: desc(6, { references: { playlist_count: 0, rotation_count: 0, upcoming_log_items: 1, upcoming_log_count: 1,
          voicetracks: 0, upcoming_logs: [{ date: '2026-11-06', hour: 14, status: 'approved', items: 1,
          url: '/logs/?date=2026-11-06&hour=14&return=reconciliation' }] } }),
        suggested_categories: [], voicetracks: 0, reasons: ['the discarded Track is scheduled in 1 current or upcoming log hour(s)'], warnings: [] } ] });
  await flush();
  const panel = ids.resolvePanel;
  const apply = button(panel, 'Delete ');
  const logLink = walk(panel, n => n.tag === 'a' && n.href && n.href.startsWith('/logs/'))[0];
  const states = { initial: apply.disabled };
  const confirm = walk(panel, n => n.tag === 'input' && n.type === 'checkbox' && !n.name &&
                               (n.listeners.change || []).length && walk(panel, l => l.tag === 'label' && l.children[0] === n &&
                               l.text.startsWith('I understand')).length)[0];
  confirm.checked = true; confirm.listeners.change[0]();
  states.confirmedButUndecided = apply.disabled;
  const pop = walk(panel, n => n.tag === 'label' && n.text.startsWith('POP'))[0].children[0];
  pop.checked = false; pop.listeners.change[0]();
  const ack = walk(panel, n => n.tag === 'input' && n.name === 'ack-F3')[1];
  ack.checked = true; ack.listeners.change[0]();
  states.ready = apply.disabled; states.label = apply.textContent;
  apply.listeners.click[0]();
  await flush();
  const applyReq = open(r => r.url === APPLY)[0];
  const applyBody = JSON.parse(applyReq.body);
  respond(applyReq, 200, { scan_id: 'A', outcome: 'completed', counts: { deleted: 2 }, rescan_recommended: true, items: [
    { finding_id: 'F1', action: 'delete', keep: 1, discard: 2, outcome: 'deleted', message: 'Track #2 deleted; Track #1 kept.',
      file: 'X/2.flac', categories_added: [], voicetracks_deleted: 0, cleanup: { status: 'removed', outcome: 'completed', message: 'The discarded file was removed.' } },
    { finding_id: 'F3', action: 'delete', keep: 4, discard: 3, outcome: 'deleted', message: 'Track #3 deleted; Track #4 kept.',
      file: 'X/3.flac', categories_added: ['JAZZ'], voicetracks_deleted: 2, cleanup: { status: 'pending', outcome: 'pending', message: 'File removal is pending.' } } ] });
  await flush();
  const results = ids.resolvePanel.text;
  const reload = open(isFindings);
  const after = { F1: PAGE1.items[0], F3: PAGE1.items[1] };
  respond(reload[0], 200, { ...PAGE1, items: [
    { ...dup('F1', 1, 2), tracks: [trackRow(1), { id: 2, deleted: true }] },
    { ...dup('F3', 3, 4), tracks: [{ id: 3, deleted: true }, trackRow(4)] }, dup('F4', 5, 6), dup('F5', 7, 8, false)] });
  await flush();
  const kept = { F4: radio('resolve-F4', 'keep_a').checked, F1gone: !radio('resolve-F1', 'keep_a'), bar: ids.resolveBar.text,
                 barHidden: ids.resolveBar.hidden };
  ids.nextBtn.listeners.click[0]();                                   // another page: the selection starts empty
  await flush();
  respond(open(isFindings)[0], 200, { ...PAGE1, page: 2, items: [dup('F9', 11, 12)] });
  await flush();
  console.log(JSON.stringify({ offered, bar, previewBody, logLink: logLink && { href: logLink.href, target: logLink.target },
    states, applyBody, results, kept, page2: { barHidden: ids.resolveBar.hidden, panelHidden: ids.resolvePanel.hidden } }));
""")
        self.assertEqual(out["offered"], {"F1": True, "F5": False, "defaultNone": True, "barHidden": True})
        self.assertIn("3 pair(s) selected on this page: 3 to delete, 0 to keep both.", out["bar"])
        self.assertEqual(out["previewBody"], {"scan_id": "A", "selections": [
            {"finding_id": "F1", "action": "delete", "keep": 1, "discard": 2},
            {"finding_id": "F3", "action": "delete", "keep": 4, "discard": 3},
            {"finding_id": "F4", "action": "delete", "keep": 5, "discard": 6}]})
        self.assertEqual(out["logLink"], {"href": "/logs/?date=2026-11-06&hour=14&return=reconciliation",
                                          "target": "_blank"})
        self.assertEqual(out["states"], {"initial": True, "confirmedButUndecided": True, "ready": False,
                                         "label": "Delete 2 duplicate Track(s)"})
        self.assertEqual(out["applyBody"], {"token": "tok", "confirm": True, "choices": {
            "F1": {"include": True, "categories": [], "acknowledge_voicetracks": False},
            "F3": {"include": True, "categories": [9], "acknowledge_voicetracks": True}}})
        self.assertIn("Deleted: 2", out["results"])
        self.assertIn("File removal is pending.", out["results"])
        self.assertIn("2 voice track(s) were deleted with it.", out["results"])
        self.assertIn("Run reconciliation (above) for a new scan", out["results"])
        self.assertIn("is not guaranteed", out["results"])
        self.assertEqual(out["kept"], {"F4": True, "F1gone": True, "bar": out["kept"]["bar"], "barHidden": False})
        self.assertIn("1 pair(s) selected on this page", out["kept"]["bar"])
        self.assertEqual(out["page2"], {"barHidden": True, "panelHidden": True})

    def test_a_closed_or_superseded_preview_answer_is_ignored_and_a_lost_apply_is_not_called_a_failure(self):
        out = self.run_page(r"""
  await bootDup();
  choose('resolve-F1', 'keep_both');
  button(ids.resolveBar, 'Review selected').listeners.click[0]();
  await flush();
  const first = open(r => r.url === PREVIEW)[0];
  button(ids.resolvePanel, 'Close');                                  // the panel shows "Checking…" only
  ids.resolvePanel.hidden = true; resolveGen += 1;                     // closed by the operator
  respond(first, 200, { scan_id: 'A', token: 'tok', deletions: 0, counts: { eligible: 1, needs_acknowledgment: 0, blocked: 0 },
    items: [{ finding_id: 'F1', action: 'keep_both', status: 'eligible', track_a: 1, track_b: 2 }] });
  await flush();
  const ignored = ids.resolvePanel.hidden;
  button(ids.resolveBar, 'Review selected').listeners.click[0]();
  await flush();
  respond(open(r => r.url === PREVIEW)[0], 200, { scan_id: 'A', token: 'tok', deletions: 0,
    counts: { eligible: 1, needs_acknowledgment: 0, blocked: 0 },
    items: [{ finding_id: 'F1', action: 'keep_both', status: 'eligible', track_a: 1, track_b: 2 }] });
  await flush();
  const record = button(ids.resolvePanel, 'Record 1 keep-both');
  const confirm = walk(ids.resolvePanel, n => n.tag === 'input' && n.type === 'checkbox')[1];
  confirm.checked = true; confirm.listeners.change[0]();
  record.listeners.click[0]();
  await flush();
  fail(open(r => r.url === APPLY)[0]);
  await flush();
  console.log(JSON.stringify({ ignored, lost: ids.resolvePanel.text, stillSelected: !ids.resolveBar.hidden,
                               reloaded: open(isFindings).length }));
""")
        self.assertTrue(out["ignored"])
        self.assertIn("whether the pairs were applied is not known", out["lost"])
        self.assertTrue(out["stillSelected"])
        self.assertEqual(out["reloaded"], 1)
