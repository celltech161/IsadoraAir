"""3.11: Review & Resolve Category Drift on the Library Reconciliation page (library.services.library_category_drift).

Every file lives in a per-test temporary Library root (library.tests.test_library_reconciliation_b's fixture) and
the engine state is a fresh file in the test's own directory (as in the duplicate-resolution tests); no station
media, cache, report or engine is touched.  Apply is one repair per request (its own single-use token), as the page
sends a confirmed batch; the page's batch logic runs under Node against the reconciliation launcher's fake DOM."""
import json
import os
import re
import threading
import time
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.core import signing
from django.db import connections
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from library.models import Category, CategoryKind, LogItem, PlaylistLog, Track
from library.services import library_category_drift as drift
from library.services import library_reconciliation as reconciliation
from library.services import media_relocation as m
from library.tests.test_library_duplicate_resolution import PAGE_EXTRA, _node
from library.tests.test_library_reconciliation_b import User, _ReconFixture
from library.tests.test_library_reconciliation_launch import PAGE_HARNESS, TEMPLATE
from webrequests import services as engine_status

PREVIEW = "library:api-reconciliation-drift-preview"
APPLY = "library:api-reconciliation-drift-apply"
FINDINGS = "library:api-reconciliation-findings"
AUDIO = b"\x03drift-audio" * 300


class _DriftFixture(_ReconFixture):
    def setUp(self):
        super().setUp()
        self.state_path = self.base / "engine_state.json"
        patch = mock.patch.object(engine_status, "ENGINE_STATE_PATH", self.state_path)
        patch.start()
        self.addCleanup(patch.stop)
        self.engine()
        self.jazz = Category.objects.create(code="JAZZ", name="Jazz", kind=self.rock.kind)

    def engine(self, **fields):
        state = {"transport": "PLAYING", "timestamp": time.time(), "decks": {"A": None, "B": None}, "queue": [],
                 "log_id": None}
        state.update(fields)
        self.state_path.write_text(json.dumps(state))

    def drifted(self, rel, category=None, data=AUDIO, **fields):
        """A Track whose file is at ``rel`` (folder = first component) with primary Category ``category``."""
        return self.track(rel, category=category or self.rock, data=data, **fields)

    def scanned(self):
        self.engine()
        self.report = reconciliation.run_scan()
        return self.report

    def finding(self, track):
        return next(f for f in self.report["findings"] if f["type"] == "category_drift"
                    and f["track"]["id"] == track.pk)

    def post(self, name, payload, client=None):
        response = (client or self.client).post(reverse(name), payload, content_type="application/json")
        return response.status_code, response.json()

    def preview(self, *selections, scan_id=None, client=None):
        self.engine()
        return self.post(PREVIEW, {"scan_id": scan_id or self.report["scan_id"], "selections": list(selections)},
                         client)

    def sel(self, track, direction=drift.MOVE_FILE, **extra):
        return {"finding_id": self.finding(track)["id"], "direction": direction, **extra}

    def apply(self, token, confirm=True, client=None):
        self.engine()
        return self.post(APPLY, {"token": token, "confirm": confirm}, client)

    def token(self, track, direction=drift.MOVE_FILE, **extra):
        status, body = self.preview(self.sel(track, direction, **extra))
        self.assertEqual(status, 200, body)
        item = body["items"][0]
        self.assertEqual(item["status"], drift.ELIGIBLE, item)
        return item["token"]

    def row(self, track):
        return Track.objects.get(pk=track.pk)

    def additional(self, track):
        return sorted(Track.objects.get(pk=track.pk).additional_categories.values_list("code", flat=True))

    def hour_log(self, offset_hours, track, played=False):
        local = timezone.localtime(timezone.now()) + timedelta(hours=offset_hours)
        log, _ = PlaylistLog.objects.get_or_create(date=local.date(), hour=local.hour, defaults={"status": "approved"})
        return LogItem.objects.create(playlist_log=log, position=LogItem.objects.filter(playlist_log=log).count(),
                                      scheduled_time=local, track=track,
                                      played_at=timezone.now() - timedelta(days=1) if played else None)


class _Drift(_DriftFixture, TestCase):
    pass


# --------------------------------------------------------------------------- B1-B7: the two directions

class RepairTests(_Drift):
    def test_b1_option_a_moves_the_file_and_keeps_every_category(self):
        track = self.drifted("POP/left-behind.flac", self.rock)
        track.additional_categories.add(self.jazz)
        self.scanned()
        status, body = self.apply(self.token(track))
        self.assertEqual(status, 200, body)
        item = body["item"]
        self.assertEqual((item["outcome"], item["cleanup"], item["file_moved"], item["stop_batch"]),
                         (m.MOVED, m.CLEANUP_REMOVED, True, False))
        row = self.row(track)
        self.assertEqual((row.filepath, row.category_id), (str(self.root / "ROCK" / "left-behind.flac"), self.rock.pk))
        self.assertEqual(self.additional(track), ["JAZZ"])
        self.assertEqual(Path(row.filepath).read_bytes(), AUDIO)
        self.assertFalse(os.path.lexists(track.filepath))
        self.assertTrue(body["rescan_recommended"])

    def test_b2_option_b_changes_the_category_and_never_touches_the_file(self):
        track = self.drifted("POP/belongs-here.flac", self.rock)
        inode = os.lstat(track.filepath).st_ino
        self.scanned()
        status, body = self.apply(self.token(track, drift.SET_CATEGORY))
        item = body["item"]
        self.assertEqual((item["outcome"], item["file_moved"], item["category_from"], item["category_to"]),
                         (m.UPDATED, False, "ROCK", "POP"))
        row = self.row(track)
        self.assertEqual((row.filepath, row.category_id), (track.filepath, self.pop.pk))
        self.assertEqual(os.lstat(track.filepath).st_ino, inode)
        self.assertEqual(Path(track.filepath).read_bytes(), AUDIO)

    def test_b3_option_b_is_unavailable_without_an_exactly_matching_category(self):
        unknown = self.drifted("MISC/stray.flac", self.rock)
        lower = self.drifted("pop/case.flac", self.rock)            # codes are matched exactly
        rooted = self.drifted("rooted.flac", self.rock)
        self.scanned()
        for track, text in ((unknown, "no Category has the code 'MISC'"), (lower, "no Category has the code 'pop'"),
                            (rooted, "Library root")):
            with self.subTest(track=track.filepath):
                summary = drift.summary(self.finding(track))
                self.assertFalse(summary["set_category"]["available"])
                self.assertIn(text, summary["set_category"]["reason"])
                status, body = self.preview(self.sel(track, drift.SET_CATEGORY))
                self.assertEqual(body["items"][0]["status"], drift.BLOCKED)
                self.assertNotIn("token", body["items"][0])
        self.assertFalse(Category.objects.filter(code__in=["MISC", "pop"]).exists())     # never created

    def test_b4_option_a_is_unavailable_for_a_category_code_that_is_not_a_folder_name(self):
        bad = Category.objects.create(code="A/B", name="Nested", kind=self.rock.kind)     # a legacy row
        track = self.drifted("POP/nested.flac", bad)
        self.scanned()
        summary = drift.summary(self.finding(track))
        self.assertFalse(summary["move"]["available"])
        status, body = self.preview(self.sel(track))
        self.assertEqual(body["items"][0]["status"], drift.BLOCKED)
        self.assertFalse(os.path.lexists(self.root / "A"))

    def test_b5_an_already_correct_track_is_not_mutated(self):
        track = self.drifted("POP/renamed-code.flac", self.rock)
        self.scanned()
        token = self.token(track)
        # The Categories' codes were edited so that the folder now IS the primary Category's code.
        Category.objects.filter(pk=self.pop.pk).update(code="POPOLD")
        Category.objects.filter(pk=self.rock.pk).update(code="POP")
        status, body = self.preview(self.sel(track))
        self.assertEqual(body["items"][0]["status"], drift.RESOLVED)
        status, body = self.apply(token)
        self.assertEqual(body["item"]["outcome"], m.UNCHANGED)
        self.assertEqual(self.row(track).filepath, track.filepath)

    def test_b6_option_b_keeps_additional_memberships_but_never_its_own_primary(self):
        track = self.drifted("POP/members.flac", self.rock)
        track.additional_categories.add(self.pop, self.jazz)
        self.scanned()
        status, body = self.preview(self.sel(track, drift.SET_CATEGORY))
        self.assertEqual([c["code"] for c in body["items"][0]["additional_remove"]], ["POP"])
        self.apply(body["items"][0]["token"])
        self.assertEqual(self.row(track).category_id, self.pop.pk)
        self.assertEqual(self.additional(track), ["JAZZ"])                # ROCK not silently added

    def test_b7_keeping_the_old_primary_is_an_explicit_previewed_choice(self):
        track = self.drifted("POP/carry.flac", self.rock)
        self.scanned()
        status, body = self.preview(self.sel(track, drift.SET_CATEGORY, keep_old_primary=True))
        self.assertEqual([c["code"] for c in body["items"][0]["additional_add"]], ["ROCK"])
        status, result = self.apply(body["items"][0]["token"])
        self.assertEqual(result["item"]["additional_added"], ["ROCK"])
        self.assertEqual(self.additional(track), ["ROCK"])
        status, body = self.preview(self.sel(track, drift.MOVE_FILE, keep_old_primary=True))
        self.assertEqual(status, 400)


# --------------------------------------------------------------------------- B8-B14: evidence and tokens

class EvidenceAndTokenTests(_Drift):
    def test_b8_track_metadata_changed_after_the_scan_or_the_preview_is_stale(self):
        track = self.drifted("POP/meta.flac", self.rock)
        self.scanned()
        token = self.token(track)
        Track.objects.filter(pk=track.pk).update(filename="other.flac")
        status, body = self.apply(token)
        self.assertEqual(body["item"]["outcome"], m.STALE)
        status, body = self.preview(self.sel(track))
        self.assertIn("changed since the scan", " ".join(body["items"][0]["reasons"]))
        self.assertTrue(os.path.exists(track.filepath))

    def test_b9_a_category_changed_after_the_preview_is_stale(self):
        track = self.drifted("POP/recat.flac", self.rock)
        self.scanned()
        token = self.token(track, drift.SET_CATEGORY)
        Track.objects.filter(pk=track.pk).update(category=self.jazz)
        status, body = self.apply(token)
        self.assertEqual(body["item"]["outcome"], m.STALE)
        self.assertEqual(self.row(track).category_id, self.jazz.pk)

    def test_b9b_additional_categories_changed_after_the_preview_are_stale(self):
        # Not part of the scan's evidence: only the preview's fingerprint (bound to the token) catches it.
        for direction in drift.DIRECTIONS:
            with self.subTest(direction=direction):
                track = self.drifted(f"POP/members-{direction}.flac", self.rock)
                self.scanned()
                token = self.token(track, direction)
                track.additional_categories.add(self.jazz)
                status, body = self.apply(token)
                self.assertEqual(body["item"]["outcome"], m.STALE, body)
                row = self.row(track)
                self.assertEqual((row.filepath, row.category_id), (track.filepath, self.rock.pk))

    def test_b10_a_source_file_changed_after_the_scan_or_the_preview_is_stale(self):
        track = self.drifted("POP/touched.flac", self.rock)
        self.scanned()
        token = self.token(track)
        with open(track.filepath, "ab") as handle:
            handle.write(b"retagged")
        status, body = self.apply(token)
        self.assertEqual(body["item"]["outcome"], m.STALE)
        self.assertFalse(os.path.lexists(self.root / "ROCK" / "touched.flac"))
        status, body = self.preview(self.sel(track))
        self.assertIn("modified or replaced", " ".join(body["items"][0]["reasons"]))

    def test_b11_and_b24_reports_without_complete_evidence_stay_review_only(self):
        track = self.drifted("POP/legacy.flac", self.rock)
        self.scanned()
        legacy = json.loads(json.dumps(self.report))
        finding = next(f for f in legacy["findings"] if f["type"] == "category_drift")
        del finding["drift"]
        reconciliation.write_report(legacy)
        self.report = legacy
        self.assertEqual(drift.summary(finding)["evidence"], False)
        status, body = self.preview(self.sel(track))
        self.assertEqual(body["items"][0]["reasons"], [drift.NO_EVIDENCE])
        for broken in ({"witness": [1, 2, 3]}, {"witness": None}, {"path": "relative.flac"},
                       {"fingerprint": {"filepath": "/elsewhere"}}):
            with self.subTest(broken=broken):
                malformed = json.loads(json.dumps(self.report))
                f = next(f for f in malformed["findings"] if f["type"] == "category_drift")
                f["drift"] = {**self.finding_with_drift(track), **broken}
                self.assertIsNone(drift.evidence(f)[0])

    def finding_with_drift(self, track):
        report = self.scanned()
        self.report = report
        return self.finding(track)["drift"]

    def test_b12_an_incomplete_or_suspect_scan_authorizes_nothing(self):
        track = self.drifted("POP/suspect.flac", self.rock)
        self.scanned()
        suspect = {**self.report, "status": "suspect"}
        reconciliation.write_report(suspect)
        status, body = self.preview(self.sel(track))
        self.assertEqual(status, 409)
        self.assertIn("not complete", body["error"])

    def test_b13_expired_replayed_tampered_and_foreign_tokens_are_refused(self):
        track = self.drifted("POP/token.flac", self.rock)
        self.scanned()
        token = self.token(track)
        other = User.objects.create_superuser("drift-other", "o@example.invalid", "pw")
        stranger = Client()
        stranger.force_login(other)
        self.assertEqual(self.apply(token, client=stranger)[0], 403)
        self.assertEqual(self.apply(token[:-2] + ("AA" if token[-2:] != "AA" else "BB"))[0], 400)
        self.assertEqual(self.apply(token, confirm=False)[0], 400)
        with mock.patch.object(drift, "TOKEN_MAX_AGE_SECONDS", -1):
            self.assertEqual(self.apply(token)[0], 409)
        status, body = self.apply(token)
        self.assertEqual((status, body["item"]["outcome"]), (200, m.MOVED))
        status, body = self.apply(token)
        self.assertEqual(status, 409)
        self.assertIn("already used", body["error"])

    def test_m3_a_token_works_only_in_the_session_that_previewed_it(self):
        from django.contrib.sessions.models import Session
        track = self.drifted("POP/session.flac", self.rock)
        self.scanned()
        token = self.token(track)
        payload = signing.loads(token, salt=drift.TOKEN_SALT)
        self.assertNotIn(self.client.session.session_key, json.dumps(payload))   # only a keyed digest
        same_user_elsewhere = Client()
        same_user_elsewhere.force_login(self.admin)
        status, body = self.apply(token, client=same_user_elsewhere)
        self.assertEqual(status, 403)
        self.assertIn("another sign-in session", body["error"])
        status, body = self.apply(token)                                  # the issuing session; unspent
        self.assertEqual((status, body["item"]["outcome"]), (200, m.MOVED))
        self.assertEqual(self.apply(token)[0], 409)                       # replay

    def test_m3_a_renewed_or_ended_session_cannot_apply(self):
        from django.contrib.sessions.models import Session
        a = self.drifted("POP/renewed.flac", self.rock)
        b = self.drifted("POP/ended.flac", self.rock)
        self.scanned()
        token_a, token_b = self.token(a), self.token(b)
        from django.conf import settings
        session = self.client.session
        session.cycle_key()                                                # a renewed session key, same login
        session.save()
        self.client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key
        status, body = self.apply(token_a)
        self.assertEqual(status, 403, body)
        Session.objects.filter(session_key=self.client.session.session_key).delete()
        response = self.client.post(reverse(APPLY), {"token": token_b, "confirm": True}, content_type="application/json")
        self.assertNotEqual(response.status_code, 200)
        self.assertEqual((self.row(a).filepath, self.row(b).filepath), (a.filepath, b.filepath))

    def test_b14_a_token_authorizes_exactly_its_direction_track_and_destination(self):
        track = self.drifted("POP/direction.flac", self.rock)
        self.scanned()
        token_a = self.token(track, drift.MOVE_FILE)
        token_b = self.token(track, drift.SET_CATEGORY)
        payload = signing.loads(token_a, salt=drift.TOKEN_SALT)
        self.assertEqual((payload["p"]["d"], payload["p"]["t"], payload["p"]["cat"]),
                         (drift.MOVE_FILE, track.pk, self.rock.pk))
        forged = signing.dumps({**payload, "p": {**payload["p"], "d": drift.SET_CATEGORY}}, salt="another.salt")
        self.assertEqual(self.apply(forged)[0], 400)
        status, body = self.post(APPLY, {"token": token_a, "confirm": True, "direction": drift.SET_CATEGORY})
        self.assertEqual(body["item"]["outcome"], m.MOVED)               # the token's direction, never the request's
        self.assertEqual(self.row(track).category_id, self.rock.pk)
        status, body = self.apply(token_b)
        self.assertEqual(body["item"]["outcome"], m.STALE)              # previewed before the move


# --------------------------------------------------------------------------- B15-B19: safety

class SafetyTests(_Drift):
    def test_b15_on_air_queued_imminent_or_unknown_engine_state_blocks_both_directions(self):
        track = self.drifted("POP/live.flac", self.rock)
        self.scanned()
        cases = {
            "deck": lambda: self.engine(decks={"A": {"track_id": track.pk}, "B": None}),
            "queue": lambda: self.engine(queue=[{"track_id": track.pk}]),
            "no engine": lambda: self.state_path.unlink(),
        }
        for name, setup in cases.items():
            for direction in drift.DIRECTIONS:
                with self.subTest(name=name, direction=direction):
                    self.engine()
                    setup()
                    status, body = self.post(PREVIEW, {"scan_id": self.report["scan_id"],
                                                       "selections": [self.sel(track, direction)]})
                    item = body["items"][0]
                    self.assertEqual(item["status"], drift.BLOCKED, item)
                    self.assertTrue(item["conflicts"]["scheduling"])
        for offset in (0, 1):
            with self.subTest(hour=offset):
                entry = self.hour_log(offset, track)
                status, body = self.preview(self.sel(track))
                self.assertIn("current or next log hour", " ".join(body["items"][0]["reasons"]))
                entry.delete()
        token = self.token(track)
        self.hour_log(0, track)                                       # scheduled after the preview
        status, body = self.apply(token)
        self.assertEqual(body["item"]["outcome"], m.REFUSED)
        self.assertTrue(os.path.exists(track.filepath))

    def test_b16_later_approved_logs_are_a_warning_not_a_block(self):
        track = self.drifted("POP/later.flac", self.rock)
        self.hour_log(-30, track, played=True)                       # history: neither a block nor a warning
        self.hour_log(48, track)
        self.scanned()
        status, body = self.preview(self.sel(track, drift.SET_CATEGORY))
        item = body["items"][0]
        self.assertEqual(item["status"], drift.ELIGIBLE, item)
        self.assertTrue(any("1 later log hour(s)" in w for w in item["warnings"]))
        self.assertTrue(any("Entries already in approved logs are not changed" in w for w in item["warnings"]))

    def test_b16b_a_content_kind_change_is_reported_prominently(self):
        imaging = CategoryKind.objects.create(code="drift-imaging", name="Drift Imaging")
        Category.objects.create(code="SWEEP", name="Sweepers", kind=imaging)
        track = self.drifted("SWEEP/sweeper.flac", self.rock)
        self.scanned()
        status, body = self.preview(self.sel(track, drift.SET_CATEGORY))
        self.assertIn(f"Content kind changes from {self.rock.kind.name} to Drift Imaging",
                      body["items"][0]["warnings"][0])

    def test_b17_protected_shared_and_remote_sources_are_blocked(self):
        spoken = self.drifted("POP/spoken-" + "b" * 32 + ".flac", self.rock)
        shared = self.drifted("POP/hardlinked.flac", self.rock)
        os.link(shared.filepath, self.root / "POP" / "hardlinked-copy.flac")
        remote = self.drifted("POP/remote.flac", self.rock, remote_url="https://example.invalid/x")
        self.scanned()
        drifting = {f["track"]["id"] for f in self.report["findings"] if f["type"] == "category_drift"}
        self.assertNotIn(remote.pk, drifting)          # the scan files a remote-hosted Track as protected
        for track in (spoken, shared):
            if track.pk not in drifting:
                continue
            with self.subTest(track=track.filepath):
                status, body = self.preview(self.sel(track))
                self.assertEqual(body["items"][0]["status"], drift.BLOCKED)
                self.assertTrue(body["items"][0]["conflicts"]["file_review"])
        self.assertIn(shared.pk, drifting)

    def test_b18_each_track_is_its_own_transaction(self):
        first = self.drifted("POP/first.flac", self.rock)
        second = self.drifted("POP/second.flac", self.rock)
        self.scanned()
        status, body = self.preview(self.sel(first), self.sel(second))
        tokens = [i["token"] for i in body["items"]]
        self.assertEqual(self.apply(tokens[0])[1]["item"]["outcome"], m.MOVED)
        real_save = Track.save

        def failing(instance, *args, **kwargs):
            if instance.pk == second.pk:
                raise RuntimeError("injected")
            return real_save(instance, *args, **kwargs)

        with mock.patch.object(Track, "save", failing):
            status, result = self.apply(tokens[1])
        self.assertEqual(result["item"]["outcome"], m.FAILED_BEFORE_COMMIT)
        self.assertEqual(self.row(first).filepath, str(self.root / "ROCK" / "first.flac"))
        self.assertEqual(self.row(second).filepath, second.filepath)
        self.assertTrue(os.path.exists(second.filepath))
        self.assertFalse(os.path.lexists(self.root / "ROCK" / "second.flac"))

    def test_b19_a_committed_move_whose_cleanup_failed_is_reported_and_stops_the_batch(self):
        track = self.drifted("POP/attention.flac", self.rock)
        self.scanned()
        token = self.token(track)
        with mock.patch.object(m.StagedMediaRelocation, "remove_source", side_effect=OSError("busy entry")):
            status, body = self.apply(token)
        item = body["item"]
        self.assertEqual((item["outcome"], item["committed"], item["stop_batch"]),
                         (m.COMMITTED_CLEANUP_REQUIRED, True, True))
        self.assertIn("It is not retried", item["message"])
        self.assertEqual(self.row(track).filepath, str(self.root / "ROCK" / "attention.flac"))


# --------------------------------------------------------------------------- B20-B23: batches and the page

class BatchTests(_Drift):
    def test_b20_a_preview_takes_at_most_25_findings(self):
        tracks = [self.drifted(f"POP/b{i:02d}.flac", self.rock) for i in range(26)]
        self.scanned()
        status, body = self.preview(*[self.sel(t) for t in tracks])
        self.assertEqual(status, 400)
        status, body = self.preview(*[self.sel(t) for t in tracks[:25]])
        self.assertEqual((status, body["counts"]["eligible"], body["counts"]["expected_moves"]), (200, 25, 25))
        status, body = self.preview(self.sel(tracks[0]), self.sel(tracks[0], drift.SET_CATEGORY))
        self.assertEqual(status, 400)                                 # one decision per finding

    def test_b21_mixed_results_are_reported_per_track(self):
        moved = self.drifted("POP/m1.flac", self.rock)
        changed = self.drifted("POP/m2.flac", self.rock)
        clash = self.drifted("POP/m3.flac", self.rock)
        stale = self.drifted("POP/m4.flac", self.rock)
        unknown = self.drifted("MISC/m5.flac", self.rock)
        self.scanned()
        self.file("ROCK/m3.flac", b"IN THE WAY")
        status, body = self.preview(self.sel(moved), self.sel(changed, drift.SET_CATEGORY), self.sel(clash),
                                    self.sel(stale), self.sel(unknown, drift.SET_CATEGORY))
        c = body["counts"]
        self.assertEqual((c["selected"], c["eligible"], c["refused"], c["expected_moves"],
                          c["expected_category_changes"], c["destination_collisions"]), (5, 3, 2, 2, 1, 1))
        Track.objects.filter(pk=stale.pk).update(category=self.jazz)
        outcomes = [self.apply(i["token"])[1]["item"]["outcome"] for i in body["items"] if i.get("token")]
        self.assertEqual(outcomes, [m.MOVED, m.UPDATED, m.STALE])

    def test_b23_a_new_scan_no_longer_reports_repaired_findings(self):
        a = self.drifted("POP/r1.flac", self.rock)
        b = self.drifted("POP/r2.flac", self.rock)
        keep = self.drifted("POP/r3.flac", self.rock)
        self.scanned()
        status, body = self.preview(self.sel(a), self.sel(b, drift.SET_CATEGORY))
        for item in body["items"]:
            self.apply(item["token"])
        self.scanned()
        remaining = [f["track"]["id"] for f in self.report["findings"] if f["type"] == "category_drift"]
        self.assertEqual(remaining, [keep.pk])

    def test_findings_api_filters_groups_sorts_and_describes_drift(self):
        self.drifted("POP/f1.flac", self.rock)
        self.drifted("POP/f2.flac", self.rock)
        self.drifted("JAZZ/f3.flac", self.pop)
        self.drifted("MISC/f4.flac", self.rock)
        self.scanned()
        url = reverse(FINDINGS)
        body = self.client.get(url, {"type": "category_drift", "sort": "mismatch"}).json()
        self.assertEqual(body["drift_groups"][0], {"folder": "POP", "category": "ROCK", "count": 2})
        self.assertEqual(body["drift_max_selections"], 25)
        self.assertEqual([drift.keys(f) for f in body["items"]],
                         [("JAZZ", "POP"), ("MISC", "ROCK"), ("POP", "ROCK"), ("POP", "ROCK")])
        first = body["items"][0]
        self.assertNotIn("witness", first["drift"])
        self.assertEqual((first["drift_review"]["move"]["destination"], first["drift_review"]["set_category"]
                          ["category"]["code"]), ("POP/f3.flac", "JAZZ"))
        filtered = self.client.get(url, {"type": "category_drift", "folder": "POP", "category": "ROCK"}).json()
        self.assertEqual(filtered["total"], 2)
        misc = self.client.get(url, {"type": "category_drift", "folder": "MISC"}).json()
        self.assertFalse(misc["items"][0]["drift_review"]["set_category"]["available"])

    def test_the_scan_records_drift_evidence_and_writes_nothing(self):
        track = self.drifted("POP/evidence.flac", self.rock)
        before = (self.row(track).filepath, self.row(track).category_id, os.lstat(track.filepath).st_ctime_ns)
        self.scanned()
        evidence = self.finding(track)["drift"]
        info = os.lstat(track.filepath)
        self.assertEqual(evidence["witness"], [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                                               info.st_ctime_ns, info.st_nlink])
        self.assertEqual(evidence["fingerprint"], {"filepath": track.filepath, "filename": "evidence.flac",
                                                   "category_id": self.rock.pk})
        self.assertEqual((evidence["folder"], evidence["category"], evidence["rel"]), ("POP", "ROCK", "POP/evidence.flac"))
        self.assertEqual(before, (self.row(track).filepath, self.row(track).category_id,
                                  os.lstat(track.filepath).st_ctime_ns))


DRIFT_PAGE = r"""
const drifted = (id, track, extra = {}) => ({ id, type: 'category_drift', reason: 'drift', track: { id: track,
  title: 'T' + track, artist: 'Ar', category: 'ROCK', filepath: '/srv/POP/x.flac' }, file: { rel: 'POP/x.flac', size: 9 },
  drift: { folder: 'POP', category: 'ROCK', rel: 'POP/x.flac' }, drift_review: { evidence: true, folder: 'POP',
  category: { id: 1, code: 'ROCK', name: 'Rock', kind: 'Music' }, move: { available: true, destination: 'ROCK/x.flac' },
  set_category: { available: true, category: { id: 2, code: 'POP', name: 'Pop', kind: 'Music' } } }, ...extra });
const DRIFT_PREVIEW = '/api/tracks/reconciliation/drift/preview/', DRIFT_APPLY = '/api/tracks/reconciliation/drift/apply/';
"""


class PageTests(_Drift):
    def run_page(self, scenario):
        source = TEMPLATE.read_text()
        script = re.search(r"<script>(.*?)</script>", source, re.S).group(1)
        return _node(self, PAGE_HARNESS + PAGE_EXTRA + DRIFT_PAGE + script + "\n(async () => {\n" + scenario
                     + "\n})().catch(e => { console.error(e); process.exit(1); });")

    def test_pure_batch_helpers(self):
        source = TEMPLATE.read_text()
        code = source[source.index("// drift:begin"):source.index("// drift:end")]
        out = _node(self, code + r"""
const map = new Map([['F1', { direction: 'move_file' }], ['F2', { direction: 'set_category', keepOld: true }]]);
const preview = { items: [{ finding_id: 'F1', direction: 'move_file', status: 'eligible', token: 't1' },
  { finding_id: 'F2', direction: 'set_category', status: 'eligible', token: 't2' },
  { finding_id: 'F3', direction: 'move_file', status: 'blocked' }] };
console.log(JSON.stringify({
  payload: driftSelectionsPayload(map),
  queue: driftQueue(preview, new Map([['F2', false]])),
  answers: [driftAnswer({ finding_id: 'F1' }, { ok: true, status: 200 }, { item: { outcome: 'moved' } }),
            driftAnswer({ finding_id: 'F1', direction: 'move_file' }, { ok: false, status: 409 }, { error: 'used' }),
            driftAnswer({ finding_id: 'F1' }, { ok: false, status: 502 }, { item: { outcome: 'moved' } }),
            driftAnswer({ finding_id: 'F1' }, { ok: false, status: 0 }, {})],
  stops: [driftStops(null), driftStops({ outcome: 'moved' }), driftStops({ outcome: 'uncertain', stop_batch: true })],
}));""")
        self.assertEqual(out["payload"], [{"finding_id": "F1", "direction": "move_file"},
                                          {"finding_id": "F2", "direction": "set_category", "keep_old_primary": True}])
        self.assertEqual(out["queue"]["send"], [{"finding_id": "F1", "direction": "move_file", "token": "t1"}])
        self.assertEqual(out["queue"]["cancelled"][0]["outcome"], "cancelled")
        self.assertEqual(out["answers"][0], {"outcome": "moved"})
        self.assertEqual(out["answers"][1]["outcome"], "refused")
        self.assertIsNone(out["answers"][2])                          # every 5xx: unknown, whatever its body
        self.assertIsNone(out["answers"][3])
        self.assertEqual(out["stops"], [True, False, True])

    def test_b22_a_lost_answer_stops_the_batch_and_nothing_is_retried(self):
        out = self.run_page(r"""
await boot();
ids.typeFilter.value = 'category_drift';
loadFindings();
await flush();
respond(open(isFindings)[0], 200, { page: 1, pages: 1, total: 3, scan_id: 'S', per_page: 25,
  items: [drifted('F1', 1), drifted('F2', 2), drifted('F3', 3)],
  drift_groups: [{ folder: 'POP', category: 'ROCK', count: 3 }] });
await flush();
button(ids.driftTools, 'Select visible: move files').listeners.click[0]();
await flush();
respond(open(isFindings)[0], 200, { page: 1, pages: 1, total: 3, scan_id: 'S', per_page: 25,
  items: [drifted('F1', 1), drifted('F2', 2), drifted('F3', 3)], drift_groups: [] });
await flush();
const selected = driftChoices.size;
button(ids.driftBar, 'Review').listeners.click[0]();
await flush();
const sent = JSON.parse(open(r => r.url === DRIFT_PREVIEW)[0].body);
respond(open(r => r.url === DRIFT_PREVIEW)[0], 200, { scan_id: 'S', counts: { selected: 3, eligible: 3, refused: 0,
  already_resolved: 0, expected_moves: 3, expected_category_changes: 0, destination_collisions: 0, scheduling_conflicts: 0,
  files_requiring_review: 0 }, items: ['F1', 'F2', 'F3'].map((f, i) => ({ finding_id: f, direction: 'move_file',
  status: 'eligible', token: 'tok' + i, reasons: [], warnings: [] })) });
await flush();
const applyBtn = button(ids.driftPanel, 'Apply');
const disabledBeforeConfirm = applyBtn.disabled;
walk(ids.driftPanel, n => n.tag === 'input' && n.type === 'checkbox').pop().checked = true;
walk(ids.driftPanel, n => n.tag === 'input' && n.type === 'checkbox').pop().listeners.change[0]();
applyBtn.listeners.click[0]();
await flush();
respond(open(r => r.url === DRIFT_APPLY)[0], 200, { item: { finding_id: 'F1', outcome: 'moved', track_id: 1,
  source: 'POP/x.flac', destination: 'ROCK/x.flac', stop_batch: false } });
await flush();
respond(open(r => r.url === DRIFT_APPLY)[0], 502, { item: { outcome: 'moved' } });
await flush();
const applies = requests.filter(r => r.url === DRIFT_APPLY).length;
console.log(JSON.stringify({ selected, sent: sent.selections, disabledBeforeConfirm, applies,
  text: ids.driftPanel.text, cleared: driftChoices.size }));
""")
        self.assertEqual(out["selected"], 3)
        self.assertEqual([s["direction"] for s in out["sent"]], ["move_file"] * 3)
        self.assertTrue(out["disabledBeforeConfirm"])
        self.assertEqual(out["applies"], 2)                           # the third was never sent
        self.assertIn("Unknown (no answer): 1", out["text"])
        self.assertIn("Not attempted: 1", out["text"])
        self.assertIn("whether it was applied is not known", out["text"])
        self.assertEqual(out["cleared"], 0)                           # a fresh view is required afterwards

    def test_an_attention_outcome_stops_the_batch(self):
        out = self.run_page(r"""
await boot();
const preview = { scan_id: 'S', items: ['F1', 'F2'].map((f, i) => ({ finding_id: f, direction: 'move_file',
  status: 'eligible', token: 't' + i })) };
applyDrift(preview, new Map(), true);
await flush();
respond(open(r => r.url === DRIFT_APPLY)[0], 200, { item: { finding_id: 'F1', outcome: 'committed_cleanup_required',
  track_id: 1, message: 'The move committed', cleanup_message: 'old entry kept', attention: true, stop_batch: true } });
await flush();
console.log(JSON.stringify({ applies: requests.filter(r => r.url === DRIFT_APPLY).length, text: ids.driftPanel.text }));
""")
        self.assertEqual(out["applies"], 1)
        self.assertIn("needs your attention", out["text"])
        self.assertIn("Not attempted: 1", out["text"])


# --------------------------------------------------------------------------- real concurrency

class ConcurrencyTests(_DriftFixture, TransactionTestCase):
    def thread(self, target, out, key):
        def body():
            try:
                out[key] = target()
            except BaseException as exc:
                out[key] = exc
            finally:
                connections.close_all()
        worker = threading.Thread(target=body, daemon=True)
        worker.start()
        return worker

    def apply_on_thread(self, token, out, key):
        def call():
            client = Client()
            client.cookies = self.client.cookies           # the issuing session (tokens are session-bound)
            response = client.post(reverse(APPLY), {"token": token, "confirm": True}, content_type="application/json")
            return response.status_code, response.json()
        return self.thread(call, out, key)

    def test_two_directions_for_one_track_serialize_one_wins_one_is_stale(self):
        track = self.drifted("POP/contended.flac", self.rock)
        self.scanned()
        token_a = self.token(track, drift.MOVE_FILE)
        token_b = self.token(track, drift.SET_CATEGORY)
        holding, release, out = threading.Event(), threading.Event(), {}
        real = m.stage_no_replace

        def paused(*args, **kwargs):
            staged = real(*args, **kwargs)
            holding.set()
            release.wait(20)
            return staged

        with mock.patch.object(m, "stage_no_replace", side_effect=paused):
            first = self.apply_on_thread(token_a, out, "a")
            self.assertTrue(holding.wait(10))
            second = self.apply_on_thread(token_b, out, "b")
            time.sleep(0.5)                               # B waits for A's locks (bounded budget is 3 s)
            release.set()
            first.join(20)
            second.join(20)
        self.assertEqual(out["a"][1]["item"]["outcome"], m.MOVED)
        self.assertIn(out["b"][1]["item"]["outcome"], (m.STALE, m.BUSY))
        row = self.row(track)
        self.assertEqual((row.filepath, row.category_id), (str(self.root / "ROCK" / "contended.flac"), self.rock.pk))
