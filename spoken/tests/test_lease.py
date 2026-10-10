"""2.22C2A corrective -- an EXPIRED processing lease authorizes nothing.

Every lease-authorized transition (complete_processing, fail_processing; both
through transitions._apply's ``lease_token``) requires, under the row lock at
ONE server time ``now``: a lease, the caller's token, and
``lease_expires_at > now`` (``== now`` is already expired) -- even while no
other worker has reclaimed the item yet. A replaced token and an expired
token are refused independently, each for its own reason.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.utils import timezone

from spoken import transitions, worker
from spoken.models import ItemState, SpokenItem, SpokenItemEvent

from .test_domain import Fixtures
from .test_worker import FakeRenders

S = ItemState
NOW = "django.utils.timezone.now"


class LeaseFreshnessTests(Fixtures):
    def at(self, moment):
        return mock.patch(NOW, return_value=moment)

    def snapshot(self, item):
        row = SpokenItem.objects.get(pk=item.pk)
        return (row.state, row.lease_token, row.lease_expires_at, row.rendition_media_id, row.failure_code,
                row.processing_attempts, SpokenItemEvent.objects.filter(item=item).count())

    def assert_refused(self, code, call):
        with self.assertRaises(transitions.EvidenceRefused) as raised:
            call()
        self.assertEqual(raised.exception.code, code)

    def test_an_expired_lease_cannot_complete_even_before_reclaim(self):
        item, token = self.processing()
        rendition = self.rendition_for(item)
        before = self.snapshot(item)
        with self.at(item.lease_expires_at + timedelta(seconds=1)):
            self.assert_refused("lease_expired", lambda: self.complete(item, token, rendition))
        self.assertEqual(self.snapshot(item), before)                   # no state change, no event
        self.assertEqual(before[0], S.PROCESSING)
        with self.at(item.lease_expires_at + timedelta(seconds=1)):
            self.assertIn(item.pk, worker.candidates())                 # left to the next claim
            reclaimed, new_token = transitions.start_processing(item, lease_seconds=60)
        self.assertNotEqual(new_token, token)

    def test_an_expired_lease_cannot_fail_the_item(self):
        item, token = self.processing()
        before = self.snapshot(item)
        with self.at(item.lease_expires_at + timedelta(seconds=1)):
            self.assert_refused("lease_expired", lambda: transitions.fail_processing(
                item, lease_token=token, code="render_failed"))
        self.assertEqual(self.snapshot(item), before)

    def test_expiry_is_exact_at_the_boundary(self):
        item, token = self.processing()
        rendition = self.rendition_for(item)
        with self.at(item.lease_expires_at):                           # == now: expired
            self.assert_refused("lease_expired", lambda: self.complete(item, token, rendition))
            self.assert_refused("lease_expired", lambda: transitions.fail_processing(
                item, lease_token=token, code="render_failed"))
            self.assertIn(item.pk, worker.candidates())
        with self.at(item.lease_expires_at - timedelta(microseconds=1)):
            done = self.complete(item, token, rendition)                # just before: still fresh
        self.assertEqual((done.state, done.rendition_media_id, done.lease_token), (S.PROCESSED, rendition.pk, None))

    def test_a_fresh_lease_still_fails_the_item(self):
        item, token = self.processing()
        with self.at(item.lease_expires_at - timedelta(microseconds=1)):
            failed = transitions.fail_processing(item, lease_token=token, code="render_failed")
        self.assertEqual((failed.state, failed.failure_code, failed.lease_token), (S.FAILED, "render_failed", None))

    def test_replaced_and_expired_tokens_are_refused_each_for_its_own_reason(self):
        item, token = self.processing()
        later = item.lease_expires_at + timedelta(seconds=1)
        with self.at(later):
            item, new_token = transitions.start_processing(item, lease_seconds=60)   # reclaimed
            rendition = self.rendition_for(item)
            # the old token, while the NEW lease is fresh: replaced
            self.assert_refused("stale_lease", lambda: self.complete(item, token, rendition))
            self.assert_refused("stale_lease", lambda: transitions.fail_processing(
                item, lease_token=token, code="x"))
            self.assert_refused("stale_lease", lambda: self.complete(item, None, rendition))
        with self.at(item.lease_expires_at + timedelta(seconds=1)):
            # the CURRENT token, expired
            self.assert_refused("lease_expired", lambda: self.complete(item, new_token, rendition))
            self.assert_refused("lease_expired", lambda: transitions.fail_processing(
                item, lease_token=new_token, code="x"))
        self.assertEqual(SpokenItem.objects.get(pk=item.pk).state, S.PROCESSING)

    def test_every_lease_authorized_transition_goes_through_the_central_rule(self):
        item, token = self.processing()
        rendition = self.rendition_for(item)
        calls = []
        real = transitions._require_lease

        def spy(row, supplied, now):
            calls.append((supplied, now))
            return real(row, supplied, now)
        with mock.patch.object(transitions, "_require_lease", spy):
            self.complete(item, token, rendition)
            other, other_token = self.processing()
            transitions.fail_processing(other, lease_token=other_token, code="x")
        self.assertEqual([supplied for supplied, _ in calls], [token, other_token])
        self.assertTrue(all(now.tzinfo is not None for _, now in calls))
        # the transition's timestamps are that same server time
        processed = SpokenItem.objects.get(pk=item.pk)
        self.assertEqual((processed.processed_at, processed.state_changed_at), (calls[0][1], calls[0][1]))


class WorkerLeaseExpiryTests(Fixtures):
    """A render that outlives its lease: the old worker abandons the item."""

    def setUp(self):
        super().setUp()
        self.addCleanup(mock.patch.stopall)

    def expire_during_render(self, item, result):
        def behaviour():
            moment = SpokenItem.objects.get(pk=item.pk).lease_expires_at + timedelta(seconds=1)
            mock.patch(NOW, return_value=moment).start()
            if isinstance(result, Exception):
                raise result
            rendition = self.rendition_for(item)
            return SimpleNamespace(evidence=self.evidence(rendition), media=rendition)
        return behaviour

    def test_completion_after_expiry_is_abandoned(self):
        item = self.validated()
        outcome = worker.process_one(render_fn=FakeRenders(self, self.expire_during_render(item, "ok")),
                                     sleep=lambda seconds: None)
        self.assertEqual((outcome.result, outcome.code), ("lost_lease", "lease_expired"))
        row = SpokenItem.objects.get(pk=item.pk)
        self.assertEqual((row.state, row.rendition_media_id), (S.PROCESSING, None))
        self.assertFalse(SpokenItemEvent.objects.filter(item=item, event="processed").exists())

    def test_failure_after_expiry_is_abandoned(self):
        from production.services import render
        item = self.validated()
        failing = self.expire_during_render(item, render.RenderError("render_failed", "x"))
        outcome = worker.process_one(render_fn=FakeRenders(self, failing), sleep=lambda seconds: None)
        self.assertEqual((outcome.result, outcome.code), ("lost_lease", "lease_expired"))
        row = SpokenItem.objects.get(pk=item.pk)
        self.assertEqual((row.state, row.failure_code), (S.PROCESSING, ""))
        self.assertFalse(SpokenItemEvent.objects.filter(item=item, event="failed").exists())

    def test_a_retry_that_no_longer_fits_the_lease_is_not_started(self):
        from production.services import render
        item = self.validated()
        renders = FakeRenders(self, render.RenderError("busy", "x", retryable=True))

        def sleep(seconds):                     # the backoff runs the lease almost out
            moment = SpokenItem.objects.get(pk=item.pk).lease_expires_at - timedelta(
                seconds=worker.LEASE_MARGIN_SECONDS + worker.MIN_RENDER_SECONDS - 1)
            mock.patch(NOW, return_value=moment).start()
        outcome = worker.process_one(render_fn=renders, sleep=sleep)
        self.assertEqual((outcome.result, outcome.code, len(renders.calls)), ("lost_lease", "lease_expiring", 1))
        self.assertEqual(SpokenItem.objects.get(pk=item.pk).state, S.PROCESSING)

    def test_render_deadlines_end_inside_the_lease(self):
        now = timezone.now()
        expires = now + timedelta(seconds=worker.LEASE_SECONDS)
        self.assertEqual(worker.render_timeout_seconds(3600, expires, now),
                         worker.LEASE_SECONDS - worker.LEASE_MARGIN_SECONDS)
        self.assertEqual(worker.render_timeout_seconds(60, expires, now), 120.0 + 360.0)
        self.assertEqual(worker.render_timeout_seconds(3600, now + timedelta(seconds=900), now), 300.0)
