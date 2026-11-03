"""r0125: SystemEvent audit events for Library bulk editing, relinking and duplicate review.

Under TestCase every request runs inside the test's transaction, so the events -- registered with
transaction.on_commit -- are captured with captureOnCommitCallbacks(execute=True), exactly as a real commit
would run them."""
import json
from unittest import mock

from django.db import transaction
from django.test import TransactionTestCase
from django.urls import reverse

from library.models import Track
from library.services import library_audit, track_bulk_edit
from library.tests.test_library_bulk_edit_r0125 import _Fixture, _FixtureMixin
from library.tests.test_library_reconciliation_b import _Recon, _RelinkSetup
from monitoring.models import SystemEvent

ENERGY = {"energy": {"op": "set", "value": "upbeat"}}


def audit_events(action):
    return list(SystemEvent.objects.filter(dedupe_key__startswith=f"library|audit|{action}|").order_by("pk"))


class BulkEditAuditTests(_Fixture):
    def run_apply(self, selection, changes, token=None):
        with self.captureOnCommitCallbacks(execute=True):
            return self.apply(selection, changes, token=token)

    def test_a_completed_apply_records_one_attributed_event_without_the_token(self):
        tracks = [self.track(f"a{i}") for i in range(3)]
        status, preview = self.preview(self.ids(*tracks), ENERGY)
        status, body = self.run_apply(self.ids(*tracks), ENERGY, token=preview["token"])
        self.assertEqual((status, body["outcome"]), (200, "completed"))
        event, = audit_events("bulk_edit.apply")
        detail = event.detail
        self.assertEqual((event.category, event.level, event.dedupe_key),
                         ("library", "info", f"library|audit|bulk_edit.apply|{body['audit_action_id']}"))
        self.assertEqual((detail["operator"], detail["operator_id"], detail["outcome"]),
                         (self.admin.username, self.admin.pk, "completed"))
        self.assertEqual((detail["requested"], detail["updated"], detail["unchanged"], detail["stale"],
                          detail["refused"], detail["failed"], detail["missing"]), (3, 3, 0, 0, 0, 0, 0))
        self.assertEqual(detail["operations"], ["energy:set"])
        self.assertEqual(detail["updated_tracks"], {"count": 3, "ids": [t.pk for t in tracks], "truncated": False})
        self.assertNotIn(preview["token"], json.dumps(detail))
        self.assertIn("3 of 3", event.title)

    def test_a_partial_apply_is_never_recorded_as_complete(self):
        good, stale = self.track("good"), self.track("stale")
        _status, preview = self.preview(self.ids(good, stale), ENERGY)
        Track.objects.filter(pk=stale.pk).update(composer="edited meanwhile")
        _status, body = self.run_apply(self.ids(good, stale), ENERGY, token=preview["token"])
        event, = audit_events("bulk_edit.apply")
        self.assertEqual((body["outcome"], event.level, event.detail["outcome"]), ("partial", "warning", "partial"))
        self.assertEqual((event.detail["updated"], event.detail["stale"]), (1, 1))
        self.assertEqual(event.detail["problem_tracks"]["ids"], [stale.pk])

    def test_nothing_applied_and_no_change_outcomes(self):
        stale = self.track("only-stale")
        _status, preview = self.preview(self.ids(stale), ENERGY)
        Track.objects.filter(pk=stale.pk).update(composer="edited meanwhile")
        self.run_apply(self.ids(stale), ENERGY, token=preview["token"])
        same = self.track("already", energy="upbeat")
        self.run_apply(self.ids(same), ENERGY)
        self.assertEqual([e.detail["outcome"] for e in audit_events("bulk_edit.apply")], ["nothing_applied", "no_change"])

    def test_a_refused_replay_is_recorded_as_refused_and_writes_nothing_more(self):
        one = self.track("replayed")
        _status, preview = self.preview(self.ids(one), ENERGY)
        self.run_apply(self.ids(one), ENERGY, token=preview["token"])
        status, _body = self.run_apply(self.ids(one), ENERGY, token=preview["token"])
        self.assertEqual(status, 409)
        events = audit_events("bulk_edit.apply")
        self.assertEqual([(e.detail["outcome"], e.level) for e in events], [("completed", "info"), ("refused", "warning")])
        self.assertIn("already applied", events[1].detail["reason"])

    def test_two_identical_operations_are_two_events_never_coalesced(self):
        one = self.track("twice")
        for value in ("upbeat", "slow"):
            self.run_apply(self.ids(one), {"energy": {"op": "set", "value": value}})
        events = audit_events("bulk_edit.apply")
        self.assertEqual(len(events), 2)
        self.assertEqual([e.repeat_count for e in events], [1, 1])
        self.assertNotEqual(events[0].detail["action_id"], events[1].detail["action_id"])

    def test_large_selections_record_a_bounded_sample_and_the_full_count(self):
        tracks = [self.track(f"many-{i:02d}") for i in range(25)]
        self.run_apply(self.ids(*tracks), ENERGY)
        sample = audit_events("bulk_edit.apply")[0].detail["updated_tracks"]
        self.assertEqual((sample["count"], len(sample["ids"]), sample["truncated"]), (25, library_audit.ID_SAMPLE, True))

    def test_a_rolled_back_apply_records_nothing(self):
        one = self.track("rolled-back")
        changes = track_bulk_edit.parse_changes(ENERGY)
        selection = track_bulk_edit.resolve_selection(self.ids(one))

        class Previews(dict):
            def issue(self, nonce, record):
                self[nonce] = record

            def claim(self, nonce):
                return self.pop(nonce, None)

        previews = Previews()
        token = track_bulk_edit.preview(selection, changes, user=self.admin, previews=previews)["token"]
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    track_bulk_edit.apply(selection, changes, token, user=self.admin, previews=previews)
                    raise RuntimeError("the caller's transaction rolls back")
        self.assertEqual(callbacks, [])
        self.assertEqual(audit_events("bulk_edit.apply"), [])
        self.assertEqual(Track.objects.get(pk=one.pk).energy, "")

    def test_audit_failures_never_fail_the_committed_operation(self):
        one, two = self.track("fail-write"), self.track("fail-schedule")
        with mock.patch("monitoring.models.emit_event", side_effect=RuntimeError("event log down")):
            status, body = self.run_apply(self.ids(one), ENERGY)
        self.assertEqual((status, body["updated"]), (200, 1))
        with mock.patch.object(library_audit.transaction, "on_commit", side_effect=RuntimeError("broken")):
            status, body = self.run_apply(self.ids(two), ENERGY)
        self.assertEqual((status, body["updated"]), (200, 1))
        self.assertEqual(Track.objects.filter(energy="upbeat").count(), 2)
        self.assertEqual(audit_events("bulk_edit.apply"), [])

    def test_library_audit_events_appear_in_monitoring_recent_events(self):
        one = self.track("visible")
        self.run_apply(self.ids(one), ENERGY)
        body = self.client.get(reverse("monitoring:api-recent-events"), secure=True).json()
        self.assertIn("Library bulk edit completed", [e["title"].split(":")[0] for e in body["events"]])


class ReconciliationAuditTests(_RelinkSetup, _Recon):
    def test_a_relink_is_recorded_after_commit_with_library_relative_paths(self):
        self.prepare()
        self.scan_and_find()
        _status, preview = self.preview()
        with self.captureOnCommitCallbacks(execute=True):
            status, result = self.apply(preview["token"])
        self.assertEqual(status, 200, result)
        event, = audit_events("reconciliation.relink")
        detail = event.detail
        self.assertEqual((detail["outcome"], detail["track_id"], detail["operator"]),
                         ("relinked", self.track_obj.pk, self.admin.username))
        self.assertEqual((detail["from"], detail["to"]), ("ROCK/song.flac", "POP/moved-song.flac"))
        serialized = json.dumps(detail)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(preview["token"], serialized)
        with self.captureOnCommitCallbacks(execute=True):
            status, _ = self.apply(preview["token"])                  # a replay
        self.assertEqual(status, 409)
        refused = audit_events("reconciliation.relink")[1]
        self.assertEqual((refused.detail["outcome"], refused.level), ("refused", "warning"))
        self.assertIn("changed since the scan", refused.detail["reason"])
        with self.captureOnCommitCallbacks(execute=True):
            self.apply("forged")
        forged = audit_events("reconciliation.relink")[2]
        self.assertEqual((forged.detail["outcome"], forged.detail["track_id"]), ("refused", None))

    def test_not_duplicate_decisions_are_recorded_and_already_decided_is_distinguished(self):
        from library.services import library_reconciliation as reconciliation
        a = self.track("ROCK/a.flac", title="Twin", duration=100.0)
        b = self.track("POP/b.flac", title="Twin", duration=100.5)
        report = reconciliation.run_scan()
        group = self.of(report, "duplicate")[0]
        payload = {"scan_id": report["scan_id"], "finding_id": group["id"], "track_a": b.pk, "track_b": a.pk}
        url = reverse("library:api-reconciliation-not-duplicate")
        for _ in range(2):
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(url, payload, content_type="application/json")
        events = audit_events("reconciliation.not_duplicate")
        self.assertEqual([e.detail["outcome"] for e in events], ["recorded", "already_decided"])
        self.assertEqual((events[0].detail["track_a"], events[0].detail["track_b"]), (a.pk, b.pk))
        self.assertEqual(events[1].detail["resolution"], "keep_both")


class AutocommitAuditTests(_FixtureMixin, TransactionTestCase):
    """Real commits (no test transaction): the event is written after the Tracks' own transactions committed."""

    def test_the_event_follows_the_committed_work(self):
        one = self.track("committed")
        status, body = self.apply(self.ids(one), ENERGY)
        self.assertEqual((status, body["outcome"]), (200, "completed"))
        event, = audit_events("bulk_edit.apply")
        self.assertGreaterEqual(event.created_at, Track.objects.get(pk=one.pk).updated_at)
        self.assertEqual(event.detail["updated_tracks"]["ids"], [one.pk])
