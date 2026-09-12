"""P1 1.5 Pass B2 -- pure-Python link-quality classifier tests.

No Gst/Django imports needed for this module (it has none itself) --
plain unittest with a fake, explicitly-advanced monotonic clock. Every
scenario here mirrors one of the 40 completion-criteria test items in
the B2 task spec; see each test's docstring for which.
"""
import unittest

from library.services.remote_dj_quality import (
    DEGRADE_STREAK_REQUIRED,
    RECOVER_STREAK_REQUIRED,
    RemoteDJQualityTracker,
)


class _Clock:
    def __init__(self, start=1_000.0, step=1.0):
        self.now = start
        self.step = step

    def tick(self):
        self.now += self.step
        return self.now


class HealthyEvidenceTests(unittest.TestCase):
    """#14/#15 -- LAN-like and mobile-like healthy evidence."""

    def test_healthy_lan_like_evidence_is_good(self):
        t = RemoteDJQualityTracker()
        clock = _Clock()
        for i in range(10):
            t.note_uplink_sample(clock.now, packets_received=100 * (i + 1),
                                  packets_lost=0, jitter_ms=4.0, media_age_s=0.1)
            t.note_downlink_sample(clock.now, packets_received=100 * (i + 1),
                                    packets_lost=0, jitter_ms=6.0,
                                    concealed_samples=0, concealment_events=0, rtt_ms=4.0)
            snap = t.snapshot(attempt_status="connected", now=clock.now)
            clock.tick()
        self.assertEqual(snap["overall"], "good")
        self.assertEqual(snap["remote_mic"], "good")
        self.assertEqual(snap["monitor_return"], "good")

    def test_healthy_mobile_like_moderate_rtt_jitter_no_loss_is_not_poor(self):
        """Production field evidence: srflx<->srflx cellular, browser RTT
        ~40ms, Remote Mic jitter ~11ms, monitor jitter ~110ms in a short
        healthy establishment snapshot, zero loss, no demonstrated
        sustained audible failure. Must never classify Poor merely for
        being cellular-like."""
        t = RemoteDJQualityTracker()
        clock = _Clock()
        for i in range(10):
            t.note_uplink_sample(clock.now, packets_received=100 * (i + 1),
                                  packets_lost=0, jitter_ms=11.0, media_age_s=0.1)
            t.note_downlink_sample(clock.now, packets_received=100 * (i + 1),
                                    packets_lost=0, jitter_ms=110.0,
                                    concealed_samples=0, concealment_events=0, rtt_ms=40.0)
            snap = t.snapshot(attempt_status="connected", now=clock.now)
            clock.tick()
        self.assertNotEqual(snap["overall"], "poor")
        self.assertNotEqual(snap["monitor_return"], "poor")


class ImpairmentTests(unittest.TestCase):
    """#16/#17 -- sustained moderate/severe impairment."""

    def _feed_downlink_loss(self, t, clock, count, lost_per_tick, jitter_ms=15.0):
        recv, lost = 0, 0
        snap = None
        for _ in range(count):
            recv += 80
            lost += lost_per_tick
            t.note_downlink_sample(clock.now, packets_received=recv, packets_lost=lost,
                                    jitter_ms=jitter_ms, concealed_samples=0,
                                    concealment_events=0, rtt_ms=30.0)
            t.note_uplink_sample(clock.now, packets_received=recv, packets_lost=0,
                                  jitter_ms=10.0, media_age_s=0.1)
            snap = t.snapshot(attempt_status="connected", now=clock.now)
            clock.tick()
        return snap

    def test_sustained_moderate_impairment_is_fair(self):
        t = RemoteDJQualityTracker()
        clock = _Clock()
        # ~2 lost per 80 delivered -> ~2.4% loss, inside the Fair band.
        snap = self._feed_downlink_loss(t, clock, 10, lost_per_tick=2)
        self.assertEqual(snap["overall"], "fair")

    def test_sustained_severe_impairment_is_poor(self):
        t = RemoteDJQualityTracker()
        clock = _Clock()
        # 20 lost per 80 delivered -> 20% loss, well past the Poor line.
        snap = self._feed_downlink_loss(t, clock, 10, lost_per_tick=20)
        self.assertEqual(snap["overall"], "poor")


class HysteresisTests(unittest.TestCase):
    """#18/#19/#20 -- no flapping, sustained-evidence degrade, slower recover."""

    def test_one_isolated_bad_sample_does_not_flip_good_to_poor(self):
        t = RemoteDJQualityTracker()
        clock = _Clock()
        recv = 0
        overall_seen = []
        for i in range(6):
            recv += 100
            t.note_downlink_sample(clock.now, packets_received=recv, packets_lost=0,
                                    jitter_ms=10.0, concealed_samples=0,
                                    concealment_events=0, rtt_ms=20.0)
            t.note_uplink_sample(clock.now, packets_received=recv, packets_lost=0,
                                  jitter_ms=10.0, media_age_s=0.1)
            overall_seen.append(t.snapshot(attempt_status="connected", now=clock.now)["overall"])
            clock.tick()
        # One single-tick spike, then back to perfectly healthy growth.
        recv += 100
        t.note_downlink_sample(clock.now, packets_received=recv, packets_lost=90,
                                jitter_ms=10.0, concealed_samples=0, concealment_events=0,
                                rtt_ms=20.0)
        t.note_uplink_sample(clock.now, packets_received=recv, packets_lost=0,
                              jitter_ms=10.0, media_age_s=0.1)
        spike_overall = t.snapshot(attempt_status="connected", now=clock.now)["overall"]
        clock.tick()
        for i in range(6):
            recv += 100
            t.note_downlink_sample(clock.now, packets_received=recv, packets_lost=90,
                                    jitter_ms=10.0, concealed_samples=0, concealment_events=0,
                                    rtt_ms=20.0)
            t.note_uplink_sample(clock.now, packets_received=recv, packets_lost=0,
                                  jitter_ms=10.0, media_age_s=0.1)
            t.snapshot(attempt_status="connected", now=clock.now)
            clock.tick()
        self.assertTrue(all(level == "good" for level in overall_seen))
        self.assertNotEqual(spike_overall, "poor")

    def test_degrade_requires_sustained_evidence_not_first_bad_tick(self):
        t = RemoteDJQualityTracker()
        clock = _Clock()
        recv, lost = 0, 0
        levels = []
        for _ in range(DEGRADE_STREAK_REQUIRED + 2):
            recv += 80
            lost += 20
            t.note_downlink_sample(clock.now, packets_received=recv, packets_lost=lost,
                                    jitter_ms=10.0, concealed_samples=0,
                                    concealment_events=0, rtt_ms=20.0)
            t.note_uplink_sample(clock.now, packets_received=recv, packets_lost=0,
                                  jitter_ms=10.0, media_age_s=0.1)
            levels.append(t.snapshot(attempt_status="connected", now=clock.now)["overall"])
            clock.tick()
        # Never poor before the required streak has elapsed.
        self.assertTrue(all(level != "poor" for level in levels[:DEGRADE_STREAK_REQUIRED - 1]))
        self.assertEqual(levels[-1], "poor")

    def test_recovery_requires_more_sustained_evidence_than_degrade(self):
        t = RemoteDJQualityTracker()
        clock = _Clock()
        recv, lost = 0, 0
        for _ in range(8):
            recv += 80
            lost += 20
            t.note_downlink_sample(clock.now, packets_received=recv, packets_lost=lost,
                                    jitter_ms=10.0, concealed_samples=0,
                                    concealment_events=0, rtt_ms=20.0)
            t.note_uplink_sample(clock.now, packets_received=recv, packets_lost=0,
                                  jitter_ms=10.0, media_age_s=0.1)
            clock.tick()
        self.assertEqual(t.snapshot(attempt_status="connected", now=clock.now)["overall"], "poor")
        recover_ticks = 0
        overall = "poor"
        while overall != "good" and recover_ticks < 40:
            recv += 100
            t.note_downlink_sample(clock.now, packets_received=recv, packets_lost=lost,
                                    jitter_ms=10.0, concealed_samples=0,
                                    concealment_events=0, rtt_ms=20.0)
            t.note_uplink_sample(clock.now, packets_received=recv, packets_lost=0,
                                  jitter_ms=10.0, media_age_s=0.1)
            overall = t.snapshot(attempt_status="connected", now=clock.now)["overall"]
            clock.tick()
            recover_ticks += 1
        self.assertEqual(overall, "good")
        self.assertGreaterEqual(recover_ticks, RECOVER_STREAK_REQUIRED)


class MissingDataTests(unittest.TestCase):
    """#11/#12/#13 -- zero-division safety, unknown != zero, graceful degrade."""

    def test_zero_packets_does_not_divide_by_zero(self):
        t = RemoteDJQualityTracker()
        for i in range(3):
            t.note_downlink_sample(1000.0 + i, packets_received=0, packets_lost=0,
                                    jitter_ms=None, concealed_samples=0,
                                    concealment_events=0, rtt_ms=None)
        snap = t.snapshot(attempt_status="connected", now=1003.0)  # must not raise
        self.assertIn(snap["monitor_return"], {"poor", "initializing"})

    def test_unknown_metric_is_not_treated_as_zero(self):
        t = RemoteDJQualityTracker()
        clock = _Clock()
        for i in range(5):
            # jitter/rtt entirely unknown, loss perfect -- must read Good,
            # not be dragged toward Poor by treating None as 0 loss *and*
            # somehow bad jitter simultaneously (there's nothing to punish).
            t.note_downlink_sample(clock.now, packets_received=100 * (i + 1), packets_lost=0,
                                    jitter_ms=None, concealed_samples=None,
                                    concealment_events=None, rtt_ms=None)
            t.note_uplink_sample(clock.now, packets_received=100 * (i + 1), packets_lost=0,
                                  jitter_ms=None, media_age_s=None)
            clock.tick()
        snap = t.snapshot(attempt_status="connected", now=clock.now)
        self.assertEqual(snap["monitor_return"], "good")

    def test_missing_metric_alone_does_not_force_poor(self):
        t = RemoteDJQualityTracker()
        clock = _Clock()
        for i in range(5):
            t.note_downlink_sample(clock.now, packets_received=100 * (i + 1), packets_lost=0,
                                    jitter_ms=10.0, concealed_samples=0,
                                    concealment_events=0, rtt_ms=None)  # RTT never observed
            clock.tick()
        snap = t.snapshot(attempt_status="connected", now=clock.now)
        self.assertEqual(snap["monitor_return"], "good")

    def test_insufficient_evidence_reports_initializing_not_good(self):
        t = RemoteDJQualityTracker()
        snap = t.snapshot(attempt_status="connected", now=1000.0)
        self.assertEqual(snap["overall"], "initializing")
        self.assertEqual(snap["remote_mic"], "initializing")
        self.assertEqual(snap["monitor_return"], "initializing")


class CounterResetTests(unittest.TestCase):
    """#9/#10 -- correct interval deltas; a counter regression is a new
    baseline, never a negative rate."""

    def test_cumulative_counters_produce_correct_interval_delta(self):
        t = RemoteDJQualityTracker()
        t.note_downlink_sample(1000.0, packets_received=100, packets_lost=1,
                                jitter_ms=10.0, concealed_samples=0, concealment_events=0, rtt_ms=20.0)
        t.note_downlink_sample(1001.0, packets_received=200, packets_lost=3,
                                jitter_ms=10.0, concealed_samples=0, concealment_events=0, rtt_ms=20.0)
        snap = t.snapshot(attempt_status="connected", now=1001.0)
        # delta_lost=2, delta_received=100 -> 2/(102) ~= 1.96%
        self.assertAlmostEqual(snap["monitor_return_metrics"]["loss_pct"], 100 * 2 / 102, places=2)

    def test_counter_regression_starts_a_new_baseline_not_negative_loss(self):
        t = RemoteDJQualityTracker()
        t.note_downlink_sample(1000.0, packets_received=500, packets_lost=50,
                                jitter_ms=10.0, concealed_samples=0, concealment_events=0, rtt_ms=20.0)
        # Counter goes BACKWARDS (stats object reset) -- must never yield
        # a negative loss rate.
        t.note_downlink_sample(1001.0, packets_received=10, packets_lost=0,
                                jitter_ms=10.0, concealed_samples=0, concealment_events=0, rtt_ms=20.0)
        snap = t.snapshot(attempt_status="connected", now=1001.0)
        loss_pct = snap["monitor_return_metrics"]["loss_pct"]
        self.assertTrue(loss_pct is None or loss_pct >= 0)


class DirectionAwarenessTests(unittest.TestCase):
    """#21/#22/#23/#24 -- direction-specific evidence preserved; candidate
    type and high-but-plausible RTT alone don't dominate."""

    def test_uplink_good_downlink_poor_preserves_direction_and_degrades_overall(self):
        t = RemoteDJQualityTracker()
        clock = _Clock()
        recv, lost = 0, 0
        for _ in range(8):
            recv += 80
            lost += 20
            t.note_downlink_sample(clock.now, packets_received=recv, packets_lost=lost,
                                    jitter_ms=10.0, concealed_samples=0,
                                    concealment_events=0, rtt_ms=20.0)
            t.note_uplink_sample(clock.now, packets_received=recv * 5, packets_lost=0,
                                  jitter_ms=5.0, media_age_s=0.1)
            clock.tick()
        snap = t.snapshot(attempt_status="connected", now=clock.now)
        self.assertEqual(snap["remote_mic"], "good")
        self.assertEqual(snap["monitor_return"], "poor")
        self.assertEqual(snap["overall"], "poor")

    def test_uplink_poor_downlink_good_preserves_direction_and_degrades_overall(self):
        t = RemoteDJQualityTracker()
        clock = _Clock()
        for i in range(8):
            # Uplink media has stopped flowing entirely (age stuck high);
            # downlink keeps growing normally and healthily.
            t.note_uplink_sample(clock.now, packets_received=1000, packets_lost=0,
                                  jitter_ms=5.0, media_age_s=1.8)
            t.note_downlink_sample(clock.now, packets_received=100 * (i + 1), packets_lost=0,
                                    jitter_ms=10.0, concealed_samples=0,
                                    concealment_events=0, rtt_ms=20.0)
            clock.tick()
        snap = t.snapshot(attempt_status="connected", now=clock.now)
        self.assertEqual(snap["monitor_return"], "good")
        self.assertEqual(snap["remote_mic"], "poor")
        self.assertEqual(snap["overall"], "poor")

    def test_candidate_type_alone_is_not_part_of_the_classifier(self):
        """The classifier module never even accepts a candidate type --
        confirms by construction that srflx cannot be penalized here."""
        import inspect
        from library.services import remote_dj_quality as mod
        source = inspect.getsource(mod)
        self.assertNotIn("srflx", source)
        self.assertNotIn("candidate_type", source)

    def test_high_but_plausible_rtt_alone_does_not_force_poor(self):
        t = RemoteDJQualityTracker()
        clock = _Clock()
        for i in range(6):
            t.note_downlink_sample(clock.now, packets_received=100 * (i + 1), packets_lost=0,
                                    jitter_ms=10.0, concealed_samples=0,
                                    concealment_events=0, rtt_ms=110.0)  # 80-120ms range
            t.note_uplink_sample(clock.now, packets_received=100 * (i + 1), packets_lost=0,
                                  jitter_ms=5.0, media_age_s=0.1)
            clock.tick()
        snap = t.snapshot(attempt_status="connected", now=clock.now)
        self.assertNotEqual(snap["overall"], "poor")


class ConcealmentTests(unittest.TestCase):
    """#25/#26 -- interval evidence, not lifetime count."""

    def test_concealment_uses_interval_delta_not_lifetime_total(self):
        t = RemoteDJQualityTracker()
        clock = _Clock()
        # A large LIFETIME concealment total from long before this window
        # (simulated by starting the delta baseline high) but ZERO new
        # events in the recent window.
        t.note_downlink_sample(clock.now, packets_received=100, packets_lost=0,
                                jitter_ms=10.0, concealed_samples=5000,
                                concealment_events=500, rtt_ms=20.0)
        clock.tick()
        for i in range(6):
            t.note_downlink_sample(clock.now, packets_received=100 * (i + 2), packets_lost=0,
                                    jitter_ms=10.0, concealed_samples=5000,
                                    concealment_events=500, rtt_ms=20.0)
            t.note_uplink_sample(clock.now, packets_received=100, packets_lost=0,
                                  jitter_ms=5.0, media_age_s=0.1)
            clock.tick()
        snap = t.snapshot(attempt_status="connected", now=clock.now)
        self.assertEqual(snap["monitor_return"], "good")
        self.assertEqual(snap["monitor_return_metrics"]["concealment_events_delta"], 0)

    def test_one_old_concealment_event_does_not_poison_a_long_session(self):
        t = RemoteDJQualityTracker()
        clock = _Clock()
        t.note_downlink_sample(clock.now, packets_received=100, packets_lost=0,
                                jitter_ms=10.0, concealed_samples=10,
                                concealment_events=1, rtt_ms=20.0)
        clock.tick()
        # The window (8s) ages the one-time event out entirely.
        for i in range(20):
            t.note_downlink_sample(clock.now, packets_received=100 * (i + 2), packets_lost=0,
                                    jitter_ms=10.0, concealed_samples=10,
                                    concealment_events=1, rtt_ms=20.0)
            t.note_uplink_sample(clock.now, packets_received=100, packets_lost=0,
                                  jitter_ms=5.0, media_age_s=0.1)
            clock.tick()
        snap = t.snapshot(attempt_status="connected", now=clock.now)
        self.assertEqual(snap["monitor_return"], "good")


class ReconnectingInteractionTests(unittest.TestCase):
    """#27/#31/#32 -- Reconnecting is authoritative; reset() clears
    evidence for a genuine post-recovery warm-up; a new attempt starts
    with no inherited history."""

    def test_reconnecting_status_is_authoritative_over_good_evidence(self):
        t = RemoteDJQualityTracker()
        for i in range(5):
            t.note_uplink_sample(1000.0 + i, packets_received=100, packets_lost=0,
                                  jitter_ms=5.0, media_age_s=0.1)
            t.note_downlink_sample(1000.0 + i, packets_received=100, packets_lost=0,
                                    jitter_ms=5.0, concealed_samples=0,
                                    concealment_events=0, rtt_ms=10.0)
        snap = t.snapshot(attempt_status="reconnecting", now=1005.0)
        self.assertEqual(snap["overall"], "reconnecting")
        self.assertEqual(snap["remote_mic"], "reconnecting")
        self.assertEqual(snap["monitor_return"], "reconnecting")

    def test_reset_clears_evidence_for_a_genuine_warm_up(self):
        t = RemoteDJQualityTracker()
        recv, lost = 0, 0
        clock = _Clock()
        for _ in range(8):
            recv += 80
            lost += 20
            t.note_downlink_sample(clock.now, packets_received=recv, packets_lost=lost,
                                    jitter_ms=10.0, concealed_samples=0,
                                    concealment_events=0, rtt_ms=20.0)
            clock.tick()
        self.assertEqual(t.snapshot(attempt_status="connected", now=clock.now)["overall"], "poor")
        t.reset()  # simulates _remote_dj_begin_recovery's call
        # Immediately after reset, even though attempt_status is already
        # back to "connected" (recovered), there is no stale Poor left.
        snap = t.snapshot(attempt_status="connected", now=clock.now)
        self.assertEqual(snap["overall"], "initializing")

    def test_new_tracker_never_inherits_prior_attempt_history(self):
        old = RemoteDJQualityTracker()
        recv, lost = 0, 0
        for i in range(8):
            recv += 80
            lost += 20
            old.note_downlink_sample(1000.0 + i, packets_received=recv, packets_lost=lost,
                                      jitter_ms=10.0, concealed_samples=0,
                                      concealment_events=0, rtt_ms=20.0)
        self.assertEqual(old.snapshot(attempt_status="connected", now=1008.0)["overall"], "poor")
        fresh = RemoteDJQualityTracker()  # a brand-new attempt's own tracker
        self.assertEqual(fresh.snapshot(attempt_status="connected", now=1008.0)["overall"], "initializing")


class PrivacyTests(unittest.TestCase):
    """#40 -- no sensitive candidate/address information anywhere in the
    quality snapshot shape."""

    def test_snapshot_never_contains_address_port_or_candidate_fields(self):
        t = RemoteDJQualityTracker()
        t.note_uplink_sample(1000.0, packets_received=100, packets_lost=0,
                              jitter_ms=5.0, media_age_s=0.1)
        t.note_downlink_sample(1000.0, packets_received=100, packets_lost=0,
                                jitter_ms=5.0, concealed_samples=0,
                                concealment_events=0, rtt_ms=10.0)
        snap = t.snapshot(attempt_status="connected", now=1000.0)
        import json
        rendered = json.dumps(snap)
        for forbidden in ("address", "port", "candidate", "ip", "ufrag", "sdp"):
            self.assertNotIn(forbidden, rendered.lower())


if __name__ == "__main__":
    unittest.main()
