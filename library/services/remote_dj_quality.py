"""P1 1.5 Pass B2 -- sustained, bidirectional Remote DJ link-quality
classification.

Pure Python: no Gst/Django imports, no wall-clock reads of its own (every
method takes `now` as a monotonic-seconds float from the caller) so this
is exercised in tests with a fake clock and deterministic sample
sequences. This module OBSERVES; it never mutes/unmutes anything, never
touches Auto/Manual, and never starts or cancels a B1/B1.1 recovery
deadline -- attempt.status (in particular "reconnecting") is passed in
by the caller and is always authoritative over whatever this module
would otherwise compute.

Direction ownership (see completion report for the full rationale):
  - "remote_mic" (DJ -> station, uplink) is judged from the SERVER's own
    inbound-rtp stats (already parsed by remote_dj_stats.parse_webrtc_stats
    into RemoteDJConnectionAttempt.media_stats["remote_mic"]) plus the
    existing B1.1 decoded-media liveness clock -- the server IS the
    receive end of this direction, so its own counters are authoritative.
  - "monitor_return" (station -> DJ, downlink) is judged from the
    BROWSER's own inbound-rtp stats (already parsed by
    remote_dj_stats.sanitize_browser_stats_payload into
    media_stats["browser_monitor"]) -- the browser IS the receive end of
    this direction. The server's own remote-inbound-rtp echo of this
    same stream (media_stats["monitor_return"]) is kept as supplementary
    raw diagnostic evidence elsewhere; it is deliberately NOT folded into
    this classifier to avoid double-counting one direction from two
    partially-redundant vantage points.

All thresholds below are PROVISIONAL -- see the module-level
constants' own comments and the completion report's threshold table.
"""
from collections import deque


# -- sampling / window -------------------------------------------------

# ~1 sample/second is the smallest cadence that gives useful rolling
# evidence without adding meaningful browser/server work -- one
# Promise-based get-stats() round trip and one small JSON send per
# side per second. See engine.py's REMOTE_DJ_QUALITY_SAMPLE_INTERVAL_MS
# and dashboard.html's matching interval for the actual scheduling.
SAMPLE_INTERVAL_SECONDS = 1.0
# "Roughly 5-10 seconds of recent evidence" -- 8 one-second samples.
WINDOW_SECONDS = 8.0
MAX_WINDOW_SAMPLES = 16  # generous cap; WINDOW_SECONDS trims by age first
MIN_SAMPLES_FOR_LOSS_DELTA = 2
MIN_SAMPLES_FOR_CLASSIFICATION = 2

# -- hysteresis ----------------------------------------------------------
# Degrade only after sustained evidence; recover more slowly than we
# degrade (production convention requested explicitly). At 1 sample/sec
# these are roughly seconds-of-evidence, not sample counts in the
# abstract.
DEGRADE_STREAK_REQUIRED = 3
RECOVER_STREAK_REQUIRED = 5

LEVEL_RANK = {"good": 0, "fair": 1, "poor": 2}

# -- provisional thresholds (see completion report table) ---------------
# Loss over the rolling window, as a percentage of (delta_lost +
# delta_received) since the oldest still-in-window sample.
LOSS_GOOD_MAX_PCT = 1.0
LOSS_FAIR_MAX_PCT = 5.0
# Jitter: average of in-window samples, milliseconds (both APIs are
# normalized to ms well before reaching this module -- see
# remote_dj_stats.py and dashboard.html's rdjSafeMs). Deliberately
# generous: production field evidence showed a healthy short cellular
# snapshot with ~110ms monitor jitter and no demonstrated audible
# failure -- that must classify no worse than "fair".
JITTER_GOOD_MAX_MS = 50.0
JITTER_FAIR_MAX_MS = 150.0
# RTT is one weak signal among several, not a dominant one -- a stable
# 80-120ms link with no loss/concealment is usable. Thresholds are set
# high enough that RTT alone essentially never drives a healthy link to
# Poor; it can only additionally corroborate what loss/jitter/concealment
# already show.
RTT_GOOD_MAX_MS = 150.0
RTT_FAIR_MAX_MS = 300.0
# Concealment (browser downlink only): event-COUNT delta over the
# window, not a lifetime total and not a fabricated proportion (no
# reliable total-samples-decoded denominator is available from the
# sanitized payload -- see module docstring and completion report).
CONCEALMENT_EVENTS_GOOD_MAX = 0
CONCEALMENT_EVENTS_FAIR_MAX = 3
# Media-flow freshness (uplink only -- from the existing B1.1
# last_media_monotonic clock, seconds since the last decoded buffer).
# Kept comfortably under B1.1's REMOTE_DJ_MEDIA_LIVENESS_TIMEOUT_S
# (2.0s) so this can flag "getting worse" before B1.1 would force
# Reconnecting outright.
MEDIA_AGE_GOOD_MAX_S = 1.0
MEDIA_AGE_FAIR_MAX_S = 1.5


def _worse(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return a if LEVEL_RANK[a] >= LEVEL_RANK[b] else b


class _BoundedCounterHistory:
    """Raw cumulative-counter snapshots for ONE direction, trimmed by
    both age and count. Detects a counter regression (a value going
    backwards, which a monotonic RTP/webrtcbin/browser counter should
    never do outside a genuine reset) and treats it as a fresh baseline
    rather than ever producing a negative delta."""

    def __init__(self):
        self._samples = deque()  # (ts, dict) oldest-first

    def add(self, ts, counters):
        if self._samples:
            prev_ts, prev = self._samples[-1]
            for key, value in counters.items():
                prev_value = prev.get(key)
                if (
                    isinstance(value, (int, float))
                    and isinstance(prev_value, (int, float))
                    and value < prev_value
                ):
                    # A cumulative counter went backwards -- stats object
                    # replaced/reset under us. Start a clean baseline
                    # rather than ever computing a negative rate.
                    self._samples.clear()
                    break
        self._samples.append((ts, dict(counters)))
        self._trim(ts)

    def _trim(self, now):
        while self._samples and now - self._samples[0][0] > WINDOW_SECONDS:
            self._samples.popleft()
        while len(self._samples) > MAX_WINDOW_SAMPLES:
            self._samples.popleft()

    def reset(self):
        self._samples.clear()

    def __len__(self):
        return len(self._samples)

    def delta(self, *keys):
        """Return {key: newest - oldest} for numeric keys present on both
        the oldest and newest in-window samples, or None per key if
        either side is missing/non-numeric. None (not this method) if
        fewer than MIN_SAMPLES_FOR_LOSS_DELTA samples are available."""
        if len(self._samples) < MIN_SAMPLES_FOR_LOSS_DELTA:
            return None
        oldest = self._samples[0][1]
        newest = self._samples[-1][1]
        result = {}
        for key in keys:
            a, b = oldest.get(key), newest.get(key)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
                result[key] = b - a
            else:
                result[key] = None
        return result

    def average(self, key):
        values = [c.get(key) for _ts, c in self._samples
                  if isinstance(c.get(key), (int, float)) and not isinstance(c.get(key), bool)]
        if not values:
            return None
        return sum(values) / len(values)

    def latest(self, key):
        if not self._samples:
            return None
        value = self._samples[-1][1].get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _loss_pct(history, lost_key, received_key):
    delta = history.delta(lost_key, received_key)
    if delta is None:
        return None
    lost, received = delta[lost_key], delta[received_key]
    if lost is None or received is None:
        return None
    expected = lost + received
    if expected <= 0:
        # No traffic at all in the window is a media-flow question, not
        # a loss-rate question -- callers combine this with a flow check.
        return None
    if lost < 0:
        # A signed "packets lost" counter CAN legitimately decrease
        # (RFC3550 allows small negative excursions on reordering), but
        # a resulting negative interval delta is not a meaningful rate.
        return None
    return 100.0 * lost / expected


def _level_for(value, good_max, fair_max, *, higher_is_worse=True):
    if value is None:
        return None
    if higher_is_worse:
        if value <= good_max:
            return "good"
        if value <= fair_max:
            return "fair"
        return "poor"
    if value >= good_max:
        return "good"
    if value >= fair_max:
        return "fair"
    return "poor"


def _direction_level(*, loss_pct, jitter_ms, rtt_ms, concealment_events_delta=None,
                      media_age_s=None):
    """Worst-of the available signals for one direction. None inputs are
    simply skipped -- missing evidence never counts as bad evidence."""
    level = None
    level = _worse(level, _level_for(loss_pct, LOSS_GOOD_MAX_PCT, LOSS_FAIR_MAX_PCT))
    level = _worse(level, _level_for(jitter_ms, JITTER_GOOD_MAX_MS, JITTER_FAIR_MAX_MS))
    level = _worse(level, _level_for(rtt_ms, RTT_GOOD_MAX_MS, RTT_FAIR_MAX_MS))
    if concealment_events_delta is not None:
        level = _worse(level, _level_for(
            concealment_events_delta, CONCEALMENT_EVENTS_GOOD_MAX, CONCEALMENT_EVENTS_FAIR_MAX,
        ))
    if media_age_s is not None:
        level = _worse(level, _level_for(media_age_s, MEDIA_AGE_GOOD_MAX_S, MEDIA_AGE_FAIR_MAX_S))
    return level


class RemoteDJQualityTracker:
    """One instance per Remote DJ session. Feed it raw sanitized counter
    snapshots from each direction; read back a hysteresis-smoothed
    Good/Fair/Poor/Reconnecting/initializing summary."""

    def __init__(self):
        self._uplink = _BoundedCounterHistory()
        self._downlink = _BoundedCounterHistory()
        self._stable_overall = None
        self._stable_remote_mic = None
        self._stable_monitor_return = None
        self._degrade_streak = 0
        self._recover_streak = 0
        self._last_rtt_ms = None
        self._last_sample_monotonic = None
        # Most recent B1.1 media-liveness age reported alongside an
        # uplink sample -- NOT part of _uplink's counter history (it
        # isn't a cumulative counter), so it needs its own explicit
        # reset() handling below rather than aging out on its own.
        self._uplink_media_age_s = None

    def reset(self):
        """Called on entering Reconnecting (see engine.py's
        _remote_dj_begin_recovery) -- clears rolling evidence so a
        recovered session gets a genuine warm-up instead of instantly
        reusing pre-interruption samples (which could be stale Poor OR
        stale Good either way)."""
        self._uplink.reset()
        self._downlink.reset()
        self._stable_overall = None
        self._stable_remote_mic = None
        self._stable_monitor_return = None
        self._degrade_streak = 0
        self._recover_streak = 0
        self._last_rtt_ms = None
        self._uplink_media_age_s = None

    def note_uplink_sample(self, now, *, packets_received=None, packets_lost=None,
                            jitter_ms=None, media_age_s=None):
        """`media_age_s`, if given, is seconds since the B1.1 decoded-
        media liveness clock last advanced -- the most direct available
        "is the uplink actually flowing" evidence, independent of RTP
        counters."""
        self._uplink.add(now, {
            "packets_received": packets_received,
            "packets_lost": packets_lost,
            "jitter_ms": jitter_ms,
        })
        self._uplink_media_age_s = media_age_s
        self._last_sample_monotonic = now

    def note_downlink_sample(self, now, *, packets_received=None, packets_lost=None,
                              jitter_ms=None, concealed_samples=None,
                              concealment_events=None, rtt_ms=None):
        self._downlink.add(now, {
            "packets_received": packets_received,
            "packets_lost": packets_lost,
            "jitter_ms": jitter_ms,
            "concealed_samples": concealed_samples,
            "concealment_events": concealment_events,
        })
        if isinstance(rtt_ms, (int, float)) and not isinstance(rtt_ms, bool):
            self._last_rtt_ms = rtt_ms
        self._last_sample_monotonic = now

    def _raw_levels(self):
        uplink_delta = self._uplink.delta("packets_lost", "packets_received")
        downlink_delta = self._downlink.delta("packets_lost", "packets_received")

        uplink_loss = _loss_pct(self._uplink, "packets_lost", "packets_received")
        downlink_loss = _loss_pct(self._downlink, "packets_lost", "packets_received")

        uplink_jitter = self._uplink.average("jitter_ms")
        downlink_jitter = self._downlink.average("jitter_ms")

        concealment_delta = None
        cd = self._downlink.delta("concealment_events")
        if cd is not None:
            concealment_delta = cd.get("concealment_events")
            if concealment_delta is not None and concealment_delta < 0:
                concealment_delta = None  # reset -- see _BoundedCounterHistory

        media_age_s = getattr(self, "_uplink_media_age_s", None)

        # Downlink "no flow at all in the window" -- expected traffic is
        # zero, not merely unknown -- is treated as Poor evidence in its
        # own right (the loss-rate calc above already returns None for
        # this case on purpose, since a 0/0 rate is not a rate).
        downlink_no_flow = (
            downlink_delta is not None
            and downlink_delta.get("packets_received") == 0
            and downlink_delta.get("packets_lost") in (0, None)
        )

        remote_mic = _direction_level(
            loss_pct=uplink_loss, jitter_ms=uplink_jitter, rtt_ms=None,
            media_age_s=media_age_s,
        )
        monitor_return = _direction_level(
            loss_pct=downlink_loss, jitter_ms=downlink_jitter, rtt_ms=self._last_rtt_ms,
            concealment_events_delta=concealment_delta,
        )
        if downlink_no_flow:
            monitor_return = _worse(monitor_return, "poor")

        have_uplink_evidence = len(self._uplink) >= MIN_SAMPLES_FOR_CLASSIFICATION or media_age_s is not None
        have_downlink_evidence = len(self._downlink) >= MIN_SAMPLES_FOR_CLASSIFICATION

        if not have_uplink_evidence:
            remote_mic = None
        if not have_downlink_evidence:
            monitor_return = None

        overall = _worse(remote_mic, monitor_return)
        return overall, remote_mic, monitor_return

    def _apply_hysteresis(self, raw_overall):
        if raw_overall is None:
            # No evidence at all yet this tick -- hold whatever we had
            # (do not manufacture a transition either way).
            return self._stable_overall

        if self._stable_overall is None:
            # First-ever classification: adopt immediately. There is no
            # "stale" prior value to protect against yet.
            self._stable_overall = raw_overall
            self._degrade_streak = 0
            self._recover_streak = 0
            return self._stable_overall

        current_rank = LEVEL_RANK[self._stable_overall]
        raw_rank = LEVEL_RANK[raw_overall]

        if raw_rank > current_rank:
            self._degrade_streak += 1
            self._recover_streak = 0
            if self._degrade_streak >= DEGRADE_STREAK_REQUIRED:
                self._stable_overall = raw_overall
                self._degrade_streak = 0
        elif raw_rank < current_rank:
            self._recover_streak += 1
            self._degrade_streak = 0
            if self._recover_streak >= RECOVER_STREAK_REQUIRED:
                self._stable_overall = raw_overall
                self._recover_streak = 0
        else:
            self._degrade_streak = 0
            self._recover_streak = 0
        return self._stable_overall

    def snapshot(self, *, attempt_status, now=None):
        """Compact, JSON-safe quality summary. `attempt_status` (the
        owning RemoteDJConnectionAttempt's own .status) is authoritative
        whenever it is "reconnecting" -- packet statistics never override
        it, per B1/B1.1 policy this module must respect but never own."""
        if attempt_status == "reconnecting":
            return {
                "overall": "reconnecting",
                "remote_mic": "reconnecting",
                "monitor_return": "reconnecting",
                "sample_age_ms": None,
                "rtt_ms": None,
                "remote_mic_metrics": {"loss_pct": None, "jitter_ms": None},
                "monitor_return_metrics": {
                    "loss_pct": None, "jitter_ms": None, "concealment_events_delta": None,
                },
            }

        raw_overall, raw_remote_mic, raw_monitor_return = self._raw_levels()
        overall = self._apply_hysteresis(raw_overall)
        # Direction-level labels are informational/diagnostic and are not
        # separately smoothed -- only the operator-facing `overall` value
        # carries the hysteresis contract. A None here (no evidence yet)
        # is reported as "initializing", never guessed as "good".
        remote_mic = raw_remote_mic or "initializing"
        monitor_return = raw_monitor_return or "initializing"
        overall_out = overall or "initializing"

        sample_age_ms = None
        if now is not None and self._last_sample_monotonic is not None:
            sample_age_ms = round(max(0.0, (now - self._last_sample_monotonic) * 1000.0), 1)

        uplink_loss = _loss_pct(self._uplink, "packets_lost", "packets_received")
        downlink_loss = _loss_pct(self._downlink, "packets_lost", "packets_received")
        concealment_delta = None
        cd = self._downlink.delta("concealment_events")
        if cd is not None and cd.get("concealment_events") is not None and cd["concealment_events"] >= 0:
            concealment_delta = cd["concealment_events"]

        return {
            "overall": overall_out,
            "remote_mic": remote_mic,
            "monitor_return": monitor_return,
            "sample_age_ms": sample_age_ms,
            "rtt_ms": self._last_rtt_ms,
            "remote_mic_metrics": {
                "loss_pct": None if uplink_loss is None else round(uplink_loss, 2),
                "jitter_ms": self._uplink.average("jitter_ms"),
            },
            "monitor_return_metrics": {
                "loss_pct": None if downlink_loss is None else round(downlink_loss, 2),
                "jitter_ms": self._downlink.average("jitter_ms"),
                "concealment_events_delta": concealment_delta,
            },
        }
