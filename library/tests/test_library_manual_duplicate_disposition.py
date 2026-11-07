"""Operator-reviewed (manual editorial) duplicate disposition (roadmap 3.11, final operator-review layer).

A likely or uncertain duplicate finding -- no decoded-audio proof, decoded audio known to differ, a failed decode,
or a group of three or more Tracks -- can be resolved by the operator's own editorial decision: a third,
separate authority (``manual_editorial``) beside the scan's exact-byte and decoded-audio evidence, never a
relabelling of the finding.  It is bound to the files the scan recorded (``member_files``: path and six-field walk
witness), needs an explicit acknowledgment before the preview issues a token and again at apply, and otherwise
runs the unchanged r0127/r0128 deletion (inspection, blockers, engine state, locks, transaction, categories,
voice-track acknowledgment, cleanup, audit).

Every file lives in the test's own Library root (library.tests.test_library_reconciliation_b's fixture); the
engine state is a file in the test's directory.  The decoded-audio cases use real audio written by ffmpeg
(library.tests.test_library_audio_equivalent_resolution's fixture).  The page script runs under Node with the
fake DOM of the earlier page tests.
"""
import contextlib
import json
import os
import time
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.core import signing
from django.db import DatabaseError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from library.models import (DuplicateCandidate, LogItem, Playlist, PlaylistItem, PlaylistLog, Rotation, RotationSlot,
                            Track, VoiceTrack)
from library.services import library_duplicate_resolution as resolution
from library.services import library_reconciliation as reconciliation
from library.services import track_deletion
from library.tests.test_library_audio_equivalent_resolution import _Audio, _tone
from library.tests.test_library_duplicate_resolution import PAGE_EXTRA, _node, _Pairs
from library.tests.test_library_reconciliation_launch import PAGE_HARNESS, TEMPLATE

FINDINGS = "library:api-reconciliation-findings"
MANUAL = "manual_editorial"


def _events(action):
    from monitoring.models import SystemEvent
    return [e.detail for e in SystemEvent.objects.filter(category="library").order_by("pk")
            if e.detail.get("action") == action]


class _ManualMixin:
    def committed(self):
        """A deletion whose after-commit file cleanup runs (fixture files are seconds old: grace off)."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(track_deletion, "SOURCE_FRESHNESS_GRACE_SECONDS", 0))
        stack.enter_context(self.captureOnCommitCallbacks(execute=True))
        return stack

    @staticmethod
    def token(body, index=0):
        return body["items"][index]["token"]

    def manual(self, finding, keep, discard, ack=True):
        return {"finding_id": finding["id"], "action": "delete", "keep": keep.pk, "discard": discard.pk,
                "authority": MANUAL, "acknowledge_manual": ack}

    def manual_apply(self, token, choice=None, ack=True, confirm=True):
        payload = {"categories": [], **(choice or {})}
        if ack is not None:
            payload["acknowledge_manual"] = ack
        return self.post("library:api-reconciliation-duplicates-apply",
                         {"token": token, "choice": payload, "confirm": confirm})

    def group(self, *tracks):
        ids = sorted(t.pk for t in tracks)
        return next(f for f in self.report["findings"] if f["type"] == "duplicate" and f.get("members") == ids)

    def listed(self):
        response = self.client.get(reverse(FINDINGS), {"type": "duplicate"})
        self.assertEqual(response.status_code, 200)
        return {tuple(f["members"]): f for f in response.json()["items"]}

    def alive(self, *tracks):
        return [Track.objects.filter(pk=t.pk).exists() for t in tracks]


class _Manual(_ManualMixin, _Pairs, TestCase):
    def likely(self, name, n=2, *, categories=None, durations=None, **fields):
        """``n`` Tracks of one artist and title, durations within 2 s, different bytes: a likely group (no proof)."""
        categories = categories or [self.rock, self.pop, self.jazz, self.blues]
        tracks = []
        for i in range(n):
            tracks.append(self.track(f"{categories[i % len(categories)].code}/{name}-{i}.flac", title=f"Song {name}",
                                     data=f"{name} take {i} ".encode() * (300 + 7 * i),
                                     category=categories[i % len(categories)],
                                     duration=(durations or [200.0 + 0.4 * j for j in range(n)])[i], **fields))
        return tracks


# --------------------------------------------------------------------------- the scan's file record

class MemberFileRecordTests(_Manual):
    def test_every_group_records_its_members_files_from_the_walk_and_the_page_never_receives_them(self):
        a, b = self.likely("rec")
        x, y = self.pair("exact")
        self.scanned()
        finding = self.group(a, b)
        self.assertEqual(finding["confidence"], "likely")
        files = {f["track_id"]: f for f in finding["member_files"]}
        for track in (a, b):
            info = os.lstat(track.filepath)
            self.assertEqual((files[track.pk]["path"], files[track.pk]["witness"]),
                             (track.filepath, [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                                               info.st_ctime_ns, info.st_nlink]))
        self.assertEqual({f["track_id"] for f in self.group(x, y)["member_files"]}, {x.pk, y.pk})
        listed = self.listed()
        self.assertNotIn("member_files", listed[(a.pk, b.pk)])
        self.assertEqual(listed[(a.pk, b.pk)]["resolution"]["deletable"], False)
        manual = listed[(a.pk, b.pk)]["manual"]
        self.assertEqual((manual["available"], manual["members"], manual["audio"]),
                         (True, [a.pk, b.pk], "unverified"))
        self.assertEqual(manual["warning"], resolution.MANUAL_WARNING)
        # A pair the scan proved keeps its verified route; the manual one is not offered for it.
        self.assertEqual(listed[(x.pk, y.pk)]["resolution"], {"deletable": True, "reason": ""})
        self.assertFalse(listed[(x.pk, y.pk)]["manual"]["available"])
        status, body = self.preview(self.manual(self.group(x, y), x, y))
        self.assertEqual((body["items"][0]["status"], self.tokens(body)), ("blocked", []))
        self.assertIn("resolve it with its verified evidence", body["items"][0]["reasons"][0])

    def test_shared_inode_members_are_not_recorded(self):
        a, b = self.likely("hard")
        c = Track.objects.create(filepath=str(self.root / "POP" / "hard-link.flac"), filename="hard-link.flac",
                                 title="Song hard", artist=self.artist, category=self.pop, format="flac",
                                 duration_seconds=200.2)
        os.link(b.filepath, c.filepath)
        self.scanned()
        finding = self.group(a, c) if any(f.get("members") == sorted([a.pk, c.pk]) for f in self.report["findings"]
                                          if f["type"] == "duplicate") else None
        recorded = {f["track_id"] for g in self.of(self.report, "duplicate") for f in g.get("member_files", [])}
        self.assertNotIn(b.pk, recorded)
        self.assertNotIn(c.pk, recorded)
        self.assertIsNone(finding)            # the hardlinked pair is a shared source, not a duplicate group


# --------------------------------------------------------------------------- 1, 4, 5: likely pairs and the acknowledgment

class LikelyPairTests(_Manual):
    def test_1_5_a_likely_pair_with_no_audio_proof_is_deleted_by_acknowledged_editorial_decision(self):
        a, b = self.likely("crew")
        self.scanned()
        finding = self.group(a, b)
        status, body = self.preview(self.manual(finding, a, b))
        self.assertEqual(status, 200, body)
        item = body["items"][0]
        self.assertEqual((item["status"], item["authority"], item["evidence"]), ("eligible", MANUAL, {"kind": MANUAL}))
        self.assertEqual((item["manual"]["warning"], item["manual"]["audio"]), (resolution.MANUAL_WARNING, "unverified"))
        self.assertTrue(item["manual_acknowledged"])
        self.assertEqual([c["code"] for c in item["suggested_categories"]], ["POP"])
        plan = signing.loads(item["token"], salt=resolution.TOKEN_SALT)
        self.assertEqual((plan["p"]["e"], plan["p"]["m"], plan["p"]["g"]), (MANUAL, True, [a.pk, b.pk]))
        self.assertEqual(Track.objects.count(), 2)                          # a preview writes nothing
        with self.committed():
            status, result = self.manual_apply(item["token"], {"categories": [self.pop.pk]})
        self.assertEqual(status, 200, result)
        done = result["item"]
        self.assertEqual((done["outcome"], done["authority"], done["categories_added"]), ("deleted", MANUAL, ["POP"]))
        self.assertEqual(self.alive(a, b), [True, False])
        self.assertFalse(Path(b.filepath).exists())
        self.assertTrue(Path(a.filepath).exists())
        # The scan's finding is never relabelled: still likely, no byte or audio evidence made up.
        self.assertEqual(reconciliation.load_report()["findings"], self.report["findings"])
        self.assertEqual(self.group(a, b)["confidence"], "likely")
        self.assertFalse(DuplicateCandidate.objects.exists())               # no keep_a / keep_b row

    def test_4_without_the_acknowledgment_there_is_no_token_and_apply_refuses(self):
        a, b = self.likely("noack")
        self.scanned()
        finding = self.group(a, b)
        status, body = self.preview(self.manual(finding, a, b, ack=False))
        item = body["items"][0]
        self.assertEqual((item["status"], self.tokens(body)), ("blocked", []))
        self.assertIn("acknowledge that this is your own editorial decision", item["reasons"][-1])
        status, body = self.preview(self.manual(finding, a, b))
        token = self.token(body)
        for ack in (None, False, "yes", 1):
            with self.subTest(ack=ack), self.captureOnCommitCallbacks(execute=True):
                status, result = self.manual_apply(token, ack=ack)
                self.assertEqual(status, 400, result)
                self.assertIn("editorial decision", result["error"])
        self.assertEqual(self.alive(a, b), [True, True])
        refused = _events("reconciliation.duplicate_resolution")
        self.assertEqual({(e["outcome"], e["status"]) for e in refused}, {("refused", 400)})
        status, result = self.manual_apply(token)                           # the refusals did not spend it
        self.assertEqual(result["item"]["outcome"], "deleted")

    def test_a_verified_selection_of_a_likely_pair_is_still_refused(self):
        a, b = self.likely("plain")
        self.scanned()
        status, body = self.preview(self.delete(a, b))
        self.assertIn("only identical-byte (exact)", body["items"][0]["reasons"][0])
        self.assertEqual(self.tokens(body), [])

    def test_unknown_authority_is_refused(self):
        a, b = self.likely("auth")
        self.scanned()
        status, body = self.preview({**self.manual(self.group(a, b), a, b), "authority": "exact_bytes"})
        self.assertEqual(status, 400)


# --------------------------------------------------------------------------- 6, 7, 8: voice tracks and categories

class VoiceTrackAndCategoryTests(_Manual):
    def test_6_the_voice_track_acknowledgment_stays_separate(self):
        a, b = self.likely("vt")
        VoiceTrack.objects.create(track=b, position="intro", filepath="/nonexistent/vt-intro.wav")
        self.scanned()
        status, body = self.preview(self.manual(self.group(a, b), a, b))
        item = body["items"][0]
        self.assertEqual((item["status"], item["voicetracks"]), ("needs_acknowledgment", 1))
        token = item["token"]
        status, result = self.manual_apply(token, {"acknowledge_voicetracks": True}, ack=False)
        self.assertEqual(status, 400)                                       # voice tracks never stand for it
        status, result = self.manual_apply(token)                           # the manual one never stands for them
        self.assertEqual(result["item"]["outcome"], "needs_acknowledgment")
        self.assertTrue(Track.objects.filter(pk=b.pk).exists())
        status, body = self.preview(self.manual(self.group(a, b), a, b))
        status, result = self.manual_apply(self.token(body), {"acknowledge_voicetracks": True})
        self.assertEqual((result["item"]["outcome"], result["item"]["voicetracks_deleted"]), ("deleted", 1))
        self.assertFalse(VoiceTrack.objects.exists())

    def test_7_8_suggestions_selection_primary_preserved_and_atomic(self):
        a, b = self.likely("cats")
        b.additional_categories.add(self.jazz, self.rock)                   # ROCK is a's primary: never suggested
        a.additional_categories.add(self.blues)
        self.scanned()
        finding = self.group(a, b)
        status, body = self.preview(self.manual(finding, a, b))
        item = body["items"][0]
        self.assertEqual(sorted(c["code"] for c in item["suggested_categories"]), ["JAZZ", "POP"])
        # A failure inside the pair's transaction rolls the category back with the deletion.
        with mock.patch("library.services.track_deletion.delete_track", side_effect=DatabaseError("boom")):
            status, result = self.manual_apply(item["token"], {"categories": [self.pop.pk, self.jazz.pk]})
        self.assertEqual(result["item"]["outcome"], "failed")
        self.assertEqual(self.additional(a), ["BLUES"])
        self.assertTrue(Track.objects.filter(pk=b.pk).exists())
        status, body = self.preview(self.manual(finding, a, b))
        status, result = self.manual_apply(self.token(body), {"categories": [self.blues.pk]})  # not suggested
        self.assertEqual(result["item"]["outcome"], "refused")
        status, body = self.preview(self.manual(finding, a, b))
        status, result = self.manual_apply(self.token(body), {"categories": [self.jazz.pk]})   # POP deselected
        self.assertEqual((result["item"]["outcome"], result["item"]["categories_added"]), ("deleted", ["JAZZ"]))
        a.refresh_from_db()
        self.assertEqual((a.category_id, self.additional(a)), (self.rock.pk, ["BLUES", "JAZZ"]))
        self.assertEqual((a.title, a.artist_id, a.duration_seconds), ("Song cats", self.artist.pk, 200.0))

    def test_keeping_b_suggests_a_and_never_replaces_b_primary(self):
        a, b = self.likely("rev")
        self.scanned()
        status, body = self.preview(self.manual(self.group(a, b), b, a))
        item = body["items"][0]
        self.assertEqual([c["code"] for c in item["suggested_categories"]], ["ROCK"])
        status, result = self.manual_apply(item["token"], {"categories": [self.rock.pk]})
        self.assertEqual(result["item"]["outcome"], "deleted")
        b.refresh_from_db()
        self.assertEqual((b.category_id, self.additional(b)), (self.pop.pk, ["ROCK"]))


# --------------------------------------------------------------------------- 9, 10, 11: groups of three or more

class GroupTests(_Manual):
    def test_9_an_explicit_pair_of_a_three_track_group(self):
        a, b, c = self.likely("trio", 3)
        self.scanned()
        finding = self.group(a, b, c)
        self.assertEqual((finding["confidence"], finding["member_count"]), ("likely", 3))
        listed = self.listed()[(a.pk, b.pk, c.pk)]
        self.assertEqual((listed["manual"]["available"], listed["manual"]["members"], listed["manual"]["audio"]),
                         (True, [a.pk, b.pk, c.pk], "not_compared"))
        status, body = self.preview(self.manual(finding, a, c))
        item = body["items"][0]
        self.assertEqual(item["status"], "eligible", item)
        with self.committed():
            status, result = self.manual_apply(item["token"])
        self.assertEqual(result["item"]["outcome"], "deleted")
        self.assertEqual(self.alive(a, b, c), [True, True, False])           # only the chosen Track

    def test_10_sequential_decisions_each_reviewed_again_after_a_deletion(self):
        a, b, c = self.likely("seq", 3)
        self.scanned()
        finding = self.group(a, b, c)
        _s, first = self.preview(self.manual(finding, a, c))
        _s, second = self.preview(self.manual(finding, c, b))               # previewed before the first applied
        self.assertEqual(self.manual_apply(self.token(first))[1]["item"]["outcome"], "deleted")
        # The group changed (c is gone, and was the survivor of the other decision): never applied blindly.
        status, result = self.manual_apply(self.token(second))
        self.assertEqual(result["item"]["outcome"], "stale", result)
        self.assertEqual(self.alive(a, b, c), [True, True, False])
        status, body = self.preview(self.manual(finding, c, b))
        self.assertIn("the kept Track no longer exists", body["items"][0]["reasons"][0])
        # Reviewed again: the remaining pair, then keep both for nothing left, as separate decisions.
        _s, third = self.preview(self.manual(finding, b, a))
        self.assertEqual(signing.loads(self.token(third), salt=resolution.TOKEN_SALT)["p"]["g"], [a.pk, b.pk])
        self.assertEqual(self.manual_apply(self.token(third))[1]["item"]["outcome"], "deleted")
        self.assertEqual(self.alive(a, b, c), [False, True, False])
        status, body = self.preview(self.manual(finding, b, a))
        self.assertEqual(body["items"][0]["status"], "blocked")

    def test_another_pair_of_the_group_deleted_between_preview_and_apply_makes_it_stale(self):
        a, b, c, d = self.likely("quad", 4)
        self.scanned()
        finding = self.group(a, b, c, d)
        _s, first = self.preview(self.manual(finding, a, b))
        _s, second = self.preview(self.manual(finding, c, d))               # disjoint pair, same group
        self.assertEqual(self.manual_apply(self.token(first))[1]["item"]["outcome"], "deleted")
        self.assertEqual(self.manual_apply(self.token(second))[1]["item"]["outcome"], "stale")
        self.assertEqual(self.alive(a, b, c, d), [True, False, True, True])

    def test_11_invalid_membership_same_track_overlapping_groups_and_keep_both_pairs(self):
        a, b, c = self.likely("bad", 3, isrc="USAAA2600001")
        other, = self.likely("elsewhere", 1)
        d = self.track("JAZZ/isrc-d.flac", title="Another Title", data=b"d" * 2000, category=self.jazz,
                       duration=200.1, isrc="USAAA2600001")
        self.scanned()
        trio = self.group(a, b, c)
        isrc = next(f for f in self.of(self.report, "duplicate") if d.pk in f["members"])
        self.assertIn(a.pk, isrc["members"])                               # a is in both findings
        cases = {
            "outside": self.manual(trio, a, other), "same": self.manual(trio, a, a),
            "missing": {**self.manual(trio, a, b), "finding_id": "F999999"},
        }
        for name, selection in cases.items():
            with self.subTest(name):
                status, body = self.preview(selection)
                self.assertEqual((status, body["items"][0]["status"], self.tokens(body)), (200, "blocked", []))
        status, body = self.preview(self.manual(trio, a, b), self.manual(isrc, d, a))
        self.assertEqual([i["status"] for i in body["items"]], ["blocked", "blocked"])
        self.assertIn("another selection in this batch", body["items"][0]["reasons"][0])
        for tracks, ok in (([a.pk, b.pk], True), ([a.pk, a.pk], False), ([a.pk, other.pk], False), (None, False)):
            with self.subTest(tracks=tracks):
                selection = {"finding_id": trio["id"], "action": "keep_both",
                             **({"tracks": tracks} if tracks is not None else {})}
                status, body = self.preview(selection)
                self.assertEqual(body["items"][0]["status"], "eligible" if ok else "blocked", body)
        for bad in ([a.pk], [a.pk, "b"], "ab"):
            status, body = self.preview({"finding_id": trio["id"], "action": "keep_both", "tracks": bad})
            self.assertEqual(status, 400)
        self.assertEqual(self.alive(a, b, c, d, other), [True] * 5)


# --------------------------------------------------------------------------- 12, 13, 14, 15, 16: files and scans

class FileAndScanTests(_Manual):
    def test_12_changed_or_replaced_between_scan_and_preview(self):
        for how in ("append", "replace", "touch"):
            with self.subTest(how):
                a, b = self.likely(f"pre{how}")
                self.scanned()
                path = Path(b.filepath)
                if how == "append":
                    with open(path, "ab") as handle:
                        handle.write(b"more")
                elif how == "replace":
                    replacement = path.with_name("replacement.tmp")
                    replacement.write_bytes(path.read_bytes())
                    os.replace(replacement, path)
                else:
                    info = os.stat(path)
                    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))     # only the ctime changes
                status, body = self.preview(self.manual(self.group(a, b), a, b))
                self.assertEqual((body["items"][0]["status"], self.tokens(body)), ("blocked", []))
                self.assertIn("modified or replaced since the scan", " ".join(body["items"][0]["reasons"]))
                self.assertIn("the file the scan recorded", " ".join(body["items"][0]["reasons"]))

    def test_13_changed_or_replaced_between_preview_and_apply(self):
        for how in ("append", "replace", "survivor"):
            with self.subTest(how):
                a, b = self.likely(f"post{how}")
                self.scanned()
                _s, body = self.preview(self.manual(self.group(a, b), a, b))
                path = Path(a.filepath if how == "survivor" else b.filepath)
                if how == "replace":
                    replacement = path.with_name("replacement.tmp")
                    replacement.write_bytes(b"another recording" * 100)
                    os.replace(replacement, path)
                else:
                    with open(path, "ab") as handle:
                        handle.write(b"more")
                status, result = self.manual_apply(self.token(body))
                self.assertEqual(result["item"]["outcome"], "stale", result)
                self.assertEqual(self.alive(a, b), [True, True])

    def test_14_an_older_report_without_member_files_is_review_only(self):
        a, b = self.likely("old")
        self.scanned()
        for finding in self.report["findings"]:
            finding.pop("member_files", None)
        reconciliation.write_report(self.report)
        listed = self.listed()[(a.pk, b.pk)]
        self.assertFalse(listed["manual"]["available"])
        self.assertEqual(listed["manual"]["reason"], resolution.NO_MANUAL_WITNESS)
        status, body = self.preview(self.manual(self.group(a, b), a, b))
        self.assertEqual((body["items"][0]["status"], body["items"][0]["reasons"]),
                         ("blocked", [resolution.NO_MANUAL_WITNESS]))
        # Nothing is reconstructed from names or durations: a malformed record is no better.
        for broken in ([{"track_id": a.pk, "path": a.filepath}], [{"track_id": a.pk, "path": a.filepath, "size": 1,
                                                                    "mtime_ns": 1, "witness": [1, 2, 3]}], "x"):
            with self.subTest(broken=broken):
                self.report["findings"] = [dict(f, member_files=broken) for f in self.report["findings"]]
                reconciliation.write_report(self.report)
                status, body = self.preview(self.manual(self.group(a, b), a, b))
                self.assertEqual(self.tokens(body), [])

    def test_15_a_suspect_or_replaced_scan_refuses(self):
        a, b = self.likely("suspect")
        self.scanned()
        _s, body = self.preview(self.manual(self.group(a, b), a, b))
        token = self.token(body)
        self.report["status"] = "suspect"
        reconciliation.write_report(self.report)
        status, body = self.preview(self.manual(self.group(a, b), a, b))
        self.assertEqual(status, 409)
        self.assertFalse(self.listed()[(a.pk, b.pk)]["manual"]["available"])
        status, result = self.manual_apply(token)
        self.assertEqual(status, 409)
        old = self.report["scan_id"]
        self.scanned()                                                      # a newer scan replaces the report
        status, body = self.post("library:api-reconciliation-duplicates-preview", {"scan_id": old, "selections": [self.manual(self.group(a, b), a, b)]})
        self.assertEqual(status, 409)
        self.assertEqual(self.alive(a, b), [True, True])

    def test_16_missing_alias_and_protected_sources(self):
        a, b = self.likely("missing")
        c, d = self.likely("alias")
        e, f = self.likely("protected")
        self.scanned()
        os.unlink(b.filepath)
        target = Path(d.filepath).with_name("elsewhere.bin")
        os.replace(d.filepath, target)
        os.symlink(target, d.filepath)
        protection = resolution._protection()
        with mock.patch.object(resolution, "_protection",
                               return_value=(protection[0], lambda t: "projected spoken Track" if t == f.pk else None)):
            status, body = self.preview(self.manual(self.group(a, b), a, b), self.manual(self.group(c, d), c, d),
                                        self.manual(self.group(e, f), e, f))
        reasons = [" ".join(i["reasons"]) for i in body["items"]]
        self.assertEqual([i["status"] for i in body["items"]], ["blocked"] * 3)
        self.assertIn("its file no longer exists", reasons[0])
        self.assertIn("no longer a regular file", reasons[1])
        self.assertIn("protected: projected spoken Track", reasons[2])
        self.assertEqual(self.tokens(body), [])

    def test_16_a_spoken_projection_file_is_never_recorded(self):
        a, = self.likely("speech", 1)
        spoken = self.track("ROCK/spoken-" + "a" * 32 + ".flac", title="Song speech", data=b"s" * 4000,
                            duration=200.2)
        self.scanned()
        recorded = {f["track_id"] for g in self.of(self.report, "duplicate") for f in g.get("member_files", [])}
        self.assertNotIn(spoken.pk, recorded)


# --------------------------------------------------------------------------- 17, 18: engine, schedule, playout

class EngineAndReferenceTests(_Manual):
    def at(self, hours=0):
        return timezone.localtime().replace(minute=20, second=0, microsecond=0) + timedelta(hours=hours)

    def schedule(self, track, when, **fields):
        log, _ = PlaylistLog.objects.get_or_create(date=when.date(), hour=when.hour, defaults={"status": "approved"})
        return LogItem.objects.create(playlist_log=log, position=LogItem.objects.filter(playlist_log=log).count() + 1,
                                      scheduled_time=when, track=track, **fields)

    def test_17_missing_stale_or_malformed_engine_state_refuses(self):
        a, b = self.likely("engine")
        self.scanned()
        cases = {
            "missing": lambda: self.state_path.unlink(),
            "stale": lambda: self.engine(timestamp=time.time() - 3600),
            "future": lambda: self.engine(timestamp=time.time() + 3600),
            "no decks": lambda: self.engine(decks={"A": None}),
            "bad queue": lambda: self.engine(queue=[{"track": 1}]),
            "nan": lambda: self.state_path.write_text(json.dumps({"timestamp": float("nan"), "decks": {"A": None, "B": None},
                                                                  "queue": [], "log_id": None})),
            "garbage": lambda: self.state_path.write_text("{"),
        }
        for name, break_it in cases.items():
            with self.subTest(name):
                self.engine()
                _s, body = self.preview(self.manual(self.group(a, b), a, b))
                token = self.token(body)
                break_it()
                status, body = self.preview(self.manual(self.group(a, b), a, b))
                self.assertEqual((body["items"][0]["status"], self.tokens(body)), ("blocked", []))
                self.assertIn("playout engine's current state cannot be confirmed", body["items"][0]["reasons"][-1])
                status, result = self.manual_apply(token)
                self.assertEqual(result["item"]["outcome"], "blocked", (name, result))
                self.assertEqual(self.alive(a, b), [True, True])

    def test_18_playlist_rotation_logs_and_live_playout_refuse(self):
        pairs = {name: self.likely(f"ref{name}") for name in
                 ("playlist", "rotation", "current", "future", "active", "deck", "queue", "claimed", "history")}
        PlaylistItem.objects.create(playlist=Playlist.objects.create(name="Morning"), position=1,
                                    track=pairs["playlist"][1])
        RotationSlot.objects.create(rotation=Rotation.objects.create(name="Gold"), position=1,
                                    track=pairs["rotation"][1])
        now = self.at()
        self.schedule(pairs["current"][1], now)
        self.schedule(pairs["future"][1], now + timedelta(hours=26))
        older = self.schedule(pairs["active"][1], now - timedelta(hours=2))
        self.schedule(pairs["claimed"][1], now - timedelta(hours=1), playback_claimed_at=timezone.now())
        self.schedule(pairs["history"][1], now - timedelta(hours=30))           # history never blocks
        self.scanned()
        self.engine(log_id=older.playlist_log_id, decks={"A": {"track_id": pairs["deck"][1].pk}, "B": None},
                    queue=[{"track_id": pairs["queue"][1].pk}])
        status, body = self.preview(*(self.manual(self.group(*p), *p) for p in pairs.values()))
        outcome = {name: (item["status"], " ".join(item.get("reasons") or []))
                   for name, item in zip(pairs, body["items"])}
        expect = {"playlist": "playlist(s) 'Morning'", "rotation": "rotation(s) 'Gold'",
                  "current": "current or upcoming log hour", "future": "current or upcoming log hour",
                  "active": "current or upcoming log hour", "deck": "loaded on a playback deck",
                  "queue": "engine's upcoming queue", "claimed": "has claimed a log entry"}
        for name, text in expect.items():
            self.assertEqual(outcome[name][0], "blocked", name)
            self.assertIn(text, outcome[name][1], name)
        self.assertEqual(outcome["history"][0], "eligible")
        results = self.apply_all(body, {i["finding_id"]: {"acknowledge_manual": True} for i in body["items"]})
        self.assertEqual([r["outcome"] for r in results], ["deleted"])
        self.assertEqual(Track.objects.count(), 17)


# --------------------------------------------------------------------------- 19, 20: legacy decisions, keep both

class LegacyAndKeepBothTests(_Manual):
    def test_19_a_pending_legacy_decision_is_preserved_and_authorizes_nothing(self):
        a, b = self.likely("legacy")
        DuplicateCandidate.objects.create(track_a=a, track_b=b, resolution="keep_b", confidence="probable")
        self.scanned()
        finding = self.group(a, b)
        self.assertEqual(finding["pending_deletion_decisions"], [f"{a.pk}-{b.pk}"])
        # The legacy decision says keep b; nothing here applies it, and a manual preview without the operator's
        # acknowledgment authorizes nothing.
        status, body = self.preview(self.manual(finding, b, a, ack=False))
        self.assertEqual((body["items"][0]["legacy_decision"]["resolution"], self.tokens(body)), ("keep_b", []))
        status, body = self.preview({"finding_id": finding["id"], "action": "keep_both", "tracks": [a.pk, b.pk]})
        self.assertIn("left unchanged", body["items"][0]["warnings"][0])
        result = self.apply_all(body, confirm=False)
        self.assertEqual(result[0]["outcome"], "already_decided")
        candidate = DuplicateCandidate.objects.get()
        self.assertEqual((candidate.resolution, candidate.applied), ("keep_b", False))
        from library.management.commands import apply_duplicate_resolutions
        with mock.patch.object(apply_duplicate_resolutions.Command, "handle") as legacy:
            _s, body = self.preview(self.manual(finding, a, b))             # the operator's own choice: keep a
            self.assertEqual(self.manual_apply(self.token(body))[1]["item"]["outcome"], "deleted")
        legacy.assert_not_called()
        self.assertFalse(DuplicateCandidate.objects.exists())               # removed with the deleted Track
        self.assertEqual(self.alive(a, b), [True, False])

    def test_20_keep_both_records_only_the_chosen_pair(self):
        a, b, c = self.likely("both", 3)
        self.scanned()
        finding = self.group(a, b, c)
        status, body = self.preview({"finding_id": finding["id"], "action": "keep_both", "tracks": [c.pk, a.pk]})
        self.assertEqual((body["items"][0]["track_a"], body["items"][0]["track_b"]), (a.pk, c.pk))
        with self.captureOnCommitCallbacks(execute=True):
            results = self.apply_all(body, confirm=False)
        self.assertEqual(results[0]["outcome"], "recorded")
        self.assertEqual(list(DuplicateCandidate.objects.values_list("track_a", "track_b", "resolution")),
                         [(a.pk, c.pk, "keep_both")])
        self.assertEqual(self.alive(a, b, c), [True, True, True])
        self.scanned()
        group = self.group(a, b, c)
        self.assertTrue(group["track_pair_review_open"])                    # two pairs still unreviewed
        self.assertEqual(group["keep_both_decision_count"], 1)
        self.assertEqual(_events("reconciliation.not_duplicate")[-1]["resolution"], "keep_both")


# --------------------------------------------------------------------------- 21, 22: tokens and batches

class TokenAndBatchTests(_Manual):
    def test_21_tampering_expiry_replay_wrong_user_and_inconsistent_manual_tokens(self):
        a, b = self.likely("tok")
        self.scanned()
        finding = self.group(a, b)
        _s, body = self.preview(self.manual(finding, a, b))
        token = self.token(body)
        tampered = token[:-3] + ("AAA" if not token.endswith("AAA") else "BBB")
        self.assertEqual(self.manual_apply(tampered)[0], 400)
        from library.tests.test_library_reconciliation_b import User
        other = User.objects.create_superuser("other-admin", "other@example.invalid", "pw")
        client = self.client_class()
        client.force_login(other)
        status, result = self.post("library:api-reconciliation-duplicates-apply",
                                   {"token": token, "confirm": True, "choice": {"acknowledge_manual": True}}, client)
        self.assertEqual(status, 403)
        with mock.patch.object(resolution, "TOKEN_MAX_AGE_SECONDS", -1):
            self.assertEqual(self.manual_apply(token)[0], 409)
        plan = signing.loads(token, salt=resolution.TOKEN_SALT)
        for name, change in (("no acknowledgment", {"m": False}), ("no group", {"g": None}),
                             ("keep both", {"a": "keep_both"}), ("other authority", {"e": "audio_guess"})):
            with self.subTest(name):
                forged = signing.dumps({**plan, "n": f"forged-{name}", "p": {**plan["p"], **change}},
                                       salt=resolution.TOKEN_SALT, compress=True)
                self.assertEqual(self.manual_apply(forged)[0], 400)
        # A verified token's shape cannot be used to skip the manual checks: without "e" it is judged by the
        # scan's evidence, which a likely finding does not have.
        verified = signing.dumps({**plan, "n": "verified-shape", "p": {k: v for k, v in plan["p"].items()
                                                                        if k not in ("e", "m", "g")}},
                                 salt=resolution.TOKEN_SALT, compress=True)
        status, result = self.manual_apply(verified)
        self.assertEqual(result["item"]["outcome"], "stale")
        self.assertEqual(self.alive(a, b), [True, True])
        self.assertEqual(self.manual_apply(token)[1]["item"]["outcome"], "deleted")
        status, again = self.manual_apply(token)
        self.assertEqual(status, 409)
        self.assertIn("already used", again["error"])

    def test_22_a_mixed_batch_applies_each_pair_independently(self):
        x, y = self.pair("exactone")
        a, b = self.likely("batch1")
        c, d = self.likely("batch2")
        e, f = self.likely("batch3")
        PlaylistItem.objects.create(playlist=Playlist.objects.create(name="Late"), position=1, track=d)
        self.scanned()
        status, body = self.preview(self.delete(x, y), self.manual(self.group(a, b), a, b),
                                    self.manual(self.group(c, d), c, d), self.manual(self.group(e, f), e, f, ack=False))
        self.assertEqual([i["status"] for i in body["items"]], ["eligible", "eligible", "blocked", "blocked"])
        results = self.apply_all(body, {i["finding_id"]: {"acknowledge_manual": True} for i in body["items"]})
        self.assertEqual([(r["outcome"], r.get("authority")) for r in results], [("deleted", None), ("deleted", MANUAL)])
        self.assertEqual(self.alive(x, y, a, b, c, d, e, f), [True, False, True, False, True, True, True, True])


# --------------------------------------------------------------------------- 2, 3, 23: real audio and the audit

class _AudioManual(_ManualMixin, _Audio):
    pass


class DecodedAudioTests(_AudioManual):
    def test_2_decoded_audio_confirmed_different(self):
        a, b = self.audio_pair("live", differ=True)
        self.scan(verify=25)
        finding = self.finding(a, b)
        self.assertEqual((finding["confidence"], finding["audio_verification"]["status"]), ("uncertain", "different"))
        listed = self.findings_api()[(a.pk, b.pk)]
        self.assertEqual((listed["manual"]["available"], listed["manual"]["audio"]), (True, "different"))
        status, body = self.preview(self.manual(finding, a, b))
        item = body["items"][0]
        self.assertEqual((item["status"], item["manual"]["audio"]), ("eligible", "different"))
        self.assertIn("the decoded audio differs", item["manual"]["audio_reason"])
        with self.committed():
            status, result = self.manual_apply(item["token"])
        self.assertEqual((result["item"]["outcome"], result["item"]["authority"]), ("deleted", MANUAL))
        self.assertEqual(self.exists(a, b), [True, False])
        self.assertEqual(reconciliation.load_report()["findings"], self.report["findings"])   # still "uncertain"

    def test_3_a_failed_decode_is_shown_as_such_and_the_ordinary_safeguards_decide(self):
        a = self.registered(_tone(self.root / "ROCK" / "broken-a.mp3"), title="Song broken")
        bad = self.root / "POP" / "broken-b.mp3"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"\xff\xfb not an mp3 at all " * 400)
        b = self.registered(bad, title="Song broken", category=self.pop)
        self.scan(verify=25)
        finding = self.finding(a, b)
        self.assertEqual(finding["confidence"], "likely")
        status, body = self.preview(self.manual(finding, a, b))
        item = body["items"][0]
        self.assertEqual(item["manual"]["audio"], "unverified")
        self.assertIn("its audio could not be identified", item["manual"]["audio_reason"])
        PlaylistItem.objects.create(playlist=Playlist.objects.create(name="Hold"), position=1, track=b)
        status, result = self.manual_apply(item["token"])
        self.assertEqual(result["item"]["outcome"], "blocked")                  # the manual decision overrides nothing
        PlaylistItem.objects.all().delete()
        _s, body = self.preview(self.manual(finding, a, b))
        with self.committed():
            status, result = self.manual_apply(self.token(body))
        self.assertEqual(result["item"]["outcome"], "deleted")
        self.assertFalse(bad.exists())

    def test_23_the_audit_keeps_the_three_authorities_apart(self):
        x, y = self.pair("bytes")
        v1, v2 = self.audio_pair("same")
        m1, m2 = self.audio_pair("other", differ=True)
        self.scan(verify=25)
        self.assertEqual([self.finding(*p)["confidence"] for p in ((x, y), (v1, v2), (m1, m2))],
                         ["exact", "audio_equivalent", "uncertain"])
        status, body = self.preview(self.delete(x, y), self.delete(v1, v2), self.manual(self.finding(m1, m2), m1, m2))
        self.assertEqual([i.get("authority") for i in body["items"]], [None, None, MANUAL])
        self.assertEqual([i["evidence"]["kind"] for i in body["items"]], ["exact_bytes", "decoded_audio", MANUAL])
        with self.committed():
            results = [self.manual_apply(i["token"], ack=True if i.get("authority") else None)[1]["item"]
                       for i in body["items"]]
        self.assertEqual([r["outcome"] for r in results], ["deleted"] * 3)
        deleted = _events("reconciliation.duplicate_delete")
        self.assertEqual([(d["deleted"], d["evidence"]) for d in deleted],
                         [(y.pk, "exact_bytes"), (v2.pk, "decoded_audio"), (m2.pk, MANUAL)])
        manual = deleted[2]
        self.assertEqual((manual["kept"], manual["manual_acknowledged"], manual["finding_confidence"], manual["audio"],
                          manual["members"], manual["scan_id"]),
                         (m1.pk, True, "uncertain", "different", [m1.pk, m2.pk], self.report["scan_id"]))
        for verified in deleted[:2]:
            self.assertNotIn("manual_acknowledged", verified)
        outcomes = [e for e in _events("reconciliation.duplicate_resolution") if e.get("outcome") == "deleted"]
        self.assertEqual([e.get("authority") for e in outcomes], [None, None, MANUAL])
        self.assertEqual(outcomes[2]["cleanup"], results[2]["cleanup"]["status"])     # reported as returned
        self.assertEqual(outcomes[2]["tracks"], [m1.pk, m2.pk])


# --------------------------------------------------------------------------- the page

PAGE_MANUAL = r"""
const manualDup = (id, members, extra = {}) => ({ id, type: 'duplicate', confidence: 'likely',
  reason: 'same artist and title, durations within 2 s', members, member_count: members.length,
  pair_count: members.length * (members.length - 1) / 2, open: true, track_pair_review_open: true,
  tracks: members.map(m => trackRow(m)), decisions: {},
  resolution: { deletable: false, reason: 'only identical-byte (exact) findings ... this one is likely' },
  manual: { available: true, reason: '', members, audio: 'different', audio_reason: 'the decoded audio differs (decoded in this scan)',
            warning: 'These recordings have not been verified as identical. They may contain different edits, masters, audio quality or performances. You are authorizing deletion of the selected version based on your own review.' },
  ...extra });
const MPAGE = { page: 1, pages: 2, total: 30, scan_id: 'A', items: [manualDup('F7', [1, 2]), manualDup('F8', [3, 4, 5]), dup('F1', 11, 12)] };
const mradio = (id, value) => radio('manual-' + id, value);
const mchoose = (id, value) => { const r = mradio(id, value); r.checked = true; r.listeners.change[0](); };
const ackBox = id => walk(ids.findings, n => n.tag === 'input' && n.name === 'manual-ack-' + id)[0];
const selects = () => walk(ids.findings, n => n.tag === 'select' && (n.attrs['aria-label'] || '').startsWith('Track '));
"""


class ManualPageTests(_Manual):
    def run_page(self, scenario):
        source = TEMPLATE.read_text()
        page = source[source.index("<script>") + len("<script>"):source.index("</script>")]
        return _node(self, PAGE_HARNESS + PAGE_EXTRA + PAGE_MANUAL + page + "\n(async () => {\n" + scenario +
                     "\n})().catch(e => { console.error(e); process.exit(1); });\n")

    def test_pure_helpers(self):
        source = TEMPLATE.read_text()
        code = (source[source.index("// resolve:begin"):source.index("// resolve:end")] +
                source[source.index("// evidence:begin"):source.index("// evidence:end")])
        out = _node(self, code + r"""
const m = new Map([['F1', manualChoice('keep_a', 3, 5, false)], ['F2', manualChoice('keep_b', 3, 5, true)],
                   ['F3', manualChoice('keep_both', 4, 6)], ['F4', choiceFor('keep_a', 1, 2)]]);
const preview = { items: [
  { finding_id: 'F2', action: 'delete', authority: 'manual_editorial', manual_acknowledged: true, status: 'eligible',
    suggested_categories: [{ id: 7 }], token: 't2' },
  { finding_id: 'F4', action: 'delete', status: 'eligible', suggested_categories: [], token: 't4' } ] };
console.log(JSON.stringify({
  same: manualChoice('keep_a', 3, 3, true), none: manualChoice('none', 3, 5), missing: manualChoice('keep_a', 3, null),
  payload: selectionsPayload(m), counts: manualCounts(m),
  keys: [choiceKey(m.get('F1'), 3, 5), choiceKey(m.get('F1'), 3, 6), choiceKey(m.get('F2'), 3, 5)],
  queue: applyQueue(preview, initialOptions(preview)).send.map(s => s.choice),
  text: [manualAudioText({ audio: 'different', audio_reason: 'x' }), manualAudioText({ audio: 'unverified', audio_reason: 'decode failed' }),
         manualAudioText({ audio: 'not_compared' }), evidenceText({ kind: 'manual_editorial' })] }));
""")
        self.assertEqual((out["same"], out["none"], out["missing"]), (None, None, None))
        self.assertEqual(out["payload"], [
            {"finding_id": "F1", "action": "delete", "keep": 3, "discard": 5, "authority": MANUAL, "acknowledge_manual": False},
            {"finding_id": "F2", "action": "delete", "keep": 5, "discard": 3, "authority": MANUAL, "acknowledge_manual": True},
            {"finding_id": "F3", "action": "keep_both", "tracks": [4, 6]},
            {"finding_id": "F4", "action": "delete", "keep": 1, "discard": 2}])        # the verified shape unchanged
        self.assertEqual(out["counts"], {"manual": 2, "unacknowledged": 1})
        self.assertEqual(out["keys"], ["keep_a", "none", "keep_b"])
        self.assertEqual(out["queue"], [{"categories": [7], "acknowledge_voicetracks": False, "acknowledge_manual": True},
                                        {"categories": [], "acknowledge_voicetracks": False}])
        self.assertIn("audio differs (x)", out["text"][0])
        self.assertEqual(out["text"][1], "The decoded audio was not verified: decode failed.")
        self.assertEqual(out["text"][2], "The decoded audio of these files was not compared.")
        self.assertIn("did not verify these recordings as identical", out["text"][3])

    def test_manual_acknowledgment_preview_and_apply(self):
        out = self.run_page(r"""
  await bootDup(MPAGE);
  const offered = { verified: !!radio('resolve-F1', 'keep_a'), toggle: !!button(ids.findings, 'Review & Resolve Manually'),
                    hidden: walk(ids.findings, n => n.tag === 'fieldset' && n.hidden).length };
  mchoose('F7', 'keep_b');
  const warned = ids.findings.text.includes('have not been verified as identical') &&
                 ids.findings.text.includes('The scan decoded both files and their audio differs');
  const bar1 = ids.resolveBar.text;
  button(ids.resolveBar, 'Review selected').listeners.click[0]();
  await flush();
  const first = JSON.parse(open(r => r.url === PREVIEW)[0].body);
  respond(open(r => r.url === PREVIEW)[0], 200, { scan_id: 'A', applicable: 0, deletions: 0,
    counts: { eligible: 0, needs_acknowledgment: 0, blocked: 1 }, items: [{ finding_id: 'F7', action: 'delete',
    authority: 'manual_editorial', status: 'blocked', keep: 2, discard: 1, reasons: ['acknowledge that this is your own editorial decision'],
    manual: MPAGE.items[0].manual }] });
  await flush();
  const box = ackBox('F7');
  box.checked = true; box.listeners.change[0]();
  const panelWithdrawn = ids.resolvePanel.hidden;
  const bar2 = ids.resolveBar.text;
  button(ids.resolveBar, 'Review selected').listeners.click[0]();
  await flush();
  const second = JSON.parse(open(r => r.url === PREVIEW)[0].body);
  respond(open(r => r.url === PREVIEW)[0], 200, { scan_id: 'A', applicable: 1, deletions: 1,
    counts: { eligible: 1, needs_acknowledgment: 0, blocked: 0 }, items: [{ finding_id: 'F7', action: 'delete',
    authority: 'manual_editorial', manual_acknowledged: true, status: 'eligible', keep: 2, discard: 1,
    keep_track: desc(2), discard_track: desc(1), suggested_categories: [], evidence: { kind: 'manual_editorial' },
    manual: MPAGE.items[0].manual, voicetracks: 0, reasons: [], warnings: [], token: 'tok-F7' }] });
  await flush();
  const panelText = ids.resolvePanel.text;
  const labels = walk(ids.resolvePanel, n => n.tag === 'label' && n.text.startsWith('I understand'));
  const understand = labels[0].children[0];
  understand.checked = true; understand.listeners.change[0]();
  button(ids.resolvePanel, 'Delete ').listeners.click[0]();
  await flush();
  const applyBody = JSON.parse(open(r => r.url === APPLY)[0].body);
  respond(open(r => r.url === APPLY)[0], 200, { scan_id: 'A', rescan_recommended: true, item: { finding_id: 'F7',
    action: 'delete', authority: 'manual_editorial', keep: 2, discard: 1, outcome: 'deleted', message: 'Track #1 deleted; Track #2 kept.',
    file: 'X/1.flac', categories_added: [], voicetracks_deleted: 0, cleanup: { status: 'removed', outcome: 'completed', message: 'The discarded file was removed.' } } });
  await flush();
  console.log(JSON.stringify({ offered, warned, bar1, first, panelWithdrawn, bar2, second, panelText, understandText: labels[0].text,
                               applyBody, results: ids.resolvePanel.text }));
""")
        self.assertEqual(out["offered"], {"verified": True, "toggle": True, "hidden": 2})
        self.assertTrue(out["warned"])
        self.assertIn("1 deletion(s) by manual editorial decision (1 not yet acknowledged)", out["bar1"])
        self.assertEqual(out["first"]["selections"], [{"finding_id": "F7", "action": "delete", "keep": 2, "discard": 1,
                                                       "authority": MANUAL, "acknowledge_manual": False}])
        self.assertTrue(out["panelWithdrawn"])
        self.assertNotIn("not yet acknowledged", out["bar2"])
        self.assertTrue(out["second"]["selections"][0]["acknowledge_manual"])
        self.assertIn("(manual editorial decision)", out["panelText"])
        self.assertIn("have not been verified as identical", out["panelText"])
        self.assertIn("1 of them by my own editorial decision", out["understandText"])
        self.assertEqual(out["applyBody"], {"token": "tok-F7", "confirm": True, "choice": {
            "categories": [], "acknowledge_voicetracks": False, "acknowledge_manual": True}})
        self.assertIn("Deleted by your manual editorial decision", out["results"])

    def test_group_member_selection(self):
        out = self.run_page(r"""
  await bootDup(MPAGE);
  const before = { radios: !!mradio('F8', 'keep_a'), selects: selects().length };
  const [sa, sb] = selects();
  sa.value = '3'; sa.listeners.change[0]();
  let [sa2, sb2] = selects();
  sb2.value = '3'; sb2.listeners.change[0]();
  const same = { radios: !!mradio('F8', 'keep_a'), text: ids.findings.text.includes('Choose two different Tracks.') };
  [sa2, sb2] = selects();
  sb2.value = '5'; sb2.listeners.change[0]();
  const offered = !!mradio('F8', 'keep_a');
  mchoose('F8', 'keep_a');
  const ack = ackBox('F8'); ack.checked = true; ack.listeners.change[0]();
  const deletePayload = selectionsPayload(choices);
  mchoose('F8', 'keep_both');
  const keepPayload = selectionsPayload(choices);
  const [sa3, sb3] = selects();
  sb3.value = '4'; sb3.listeners.change[0]();                    // a new pair starts undecided
  const afterRepick = { size: choices.size, radio: mradio('F8', 'none').checked };
  // After a deletion the reloaded finding lists the deleted Track: it is no longer offered.
  mchoose('F8', 'keep_b');
  ids.nextBtn.listeners.click[0]();                                  // unsaved selection: asked first
  await flush();
  const guarded = { shown: !ids.navGuard.hidden, requests: open(isFindings).length };
  button(ids.navGuard, 'Stay').listeners.click[0]();
  loadFindings();
  await flush();
  respond(open(isFindings)[0], 200, { ...MPAGE, items: [manualDup('F8', [3, 4, 5], { tracks: [trackRow(3), { id: 4, deleted: true }, trackRow(5)] })] });
  await flush();
  const remaining = { selects: selects().length, size: choices.size, radios: !!mradio('F8', 'keep_a'),
                      text: ids.findings.text.includes('Track #4 was deleted after this scan') };
  console.log(JSON.stringify({ before, same, offered, deletePayload, keepPayload, afterRepick, guarded, remaining }));
""")
        self.assertEqual(out["before"], {"radios": False, "selects": 2})
        self.assertEqual(out["same"], {"radios": False, "text": True})
        self.assertTrue(out["offered"])
        self.assertEqual(out["deletePayload"], [{"finding_id": "F8", "action": "delete", "keep": 3, "discard": 5,
                                                 "authority": MANUAL, "acknowledge_manual": True}])
        self.assertEqual(out["keepPayload"], [{"finding_id": "F8", "action": "keep_both", "tracks": [3, 5]}])
        self.assertEqual(out["afterRepick"], {"size": 0, "radio": True})
        self.assertEqual(out["guarded"], {"shown": True, "requests": 0})
        # The pair 4-3 included a Track deleted since: the choice is dropped and the remaining two are offered.
        self.assertEqual(out["remaining"], {"selects": 0, "size": 0, "radios": True, "text": True})

    def test_scan_invalidation_and_an_uncertain_apply_answer(self):
        out = self.run_page(r"""
  await bootDup(MPAGE);
  mchoose('F7', 'keep_a');
  const ack = ackBox('F7'); ack.checked = true; ack.listeners.change[0]();
  button(ids.resolveBar, 'Review selected').listeners.click[0]();
  await flush();
  respond(open(r => r.url === PREVIEW)[0], 200, { scan_id: 'A', applicable: 1, deletions: 1,
    counts: { eligible: 1, needs_acknowledgment: 0, blocked: 0 }, items: [{ finding_id: 'F7', action: 'delete',
    authority: 'manual_editorial', manual_acknowledged: true, status: 'eligible', keep: 1, discard: 2,
    keep_track: desc(1), discard_track: desc(2), suggested_categories: [], manual: MPAGE.items[0].manual,
    voicetracks: 0, reasons: [], warnings: [], token: 'tok-F7' }] });
  await flush();
  const understand = walk(ids.resolvePanel, n => n.tag === 'label' && n.text.startsWith('I understand'))[0].children[0];
  understand.checked = true; understand.listeners.change[0]();
  button(ids.resolvePanel, 'Delete ').listeners.click[0]();
  await flush();
  respond(open(r => r.url === APPLY)[0], 502, {});
  await flush();
  const lost = ids.resolvePanel.text;
  const kept = choices.size;
  respond(open(isFindings)[0], 200, { ...MPAGE, scan_id: 'B' });   // a newer scan replaced the report
  await flush();
  console.log(JSON.stringify({ lost, kept, cleared: choices.size, guard: ids.navGuard.text, moreApply: open(r => r.url === APPLY).length }));
""")
        self.assertIn("Unknown (no answer): 1", out["lost"])
        self.assertEqual((out["kept"], out["cleared"], out["moreApply"]), (1, 0, 0))
        self.assertIn("A newer reconciliation scan (B) replaced the report", out["guard"])

    def test_an_unavailable_manual_route_says_why_and_keeps_the_review_control(self):
        out = self.run_page(r"""
  await bootDup({ ...MPAGE, items: [manualDup('F9', [1, 2], { manual: { available: false, members: [],
    reason: 'this scan did not record the Tracks\' files for manual resolution', audio: 'not_compared' } })] });
  console.log(JSON.stringify({ toggle: !!button(ids.findings, 'Review & Resolve Manually'),
    notDup: !!button(ids.findings, 'Record: not a duplicate'), text: ids.findings.text }));
""")
        self.assertFalse(out["toggle"])
        self.assertTrue(out["notDup"])
        self.assertIn("Manual resolution is not available: this scan did not record", out["text"])
