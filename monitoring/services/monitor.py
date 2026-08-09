"""Monitoring poller -- periodic health checks for system resources,
IsadoraAir's own systemd services, the transmitter, and Liquidsoap's
audio-silence self-reports. Follows encoders/services/encoder_manager.py's
simple time.sleep()-loop shape rather than engine.py's GLib/GStreamer
pattern, since this is periodic polling, not a real-time audio pipeline.

Admin config changes here do NOT trigger a systemd restart -- unlike
Liquidsoap subprocess topology (can't change live) or the audio pipeline
sample rate (needs the engine to restart), this loop just re-reads
MonitorCheck.objects.filter(enabled=True) fresh every cycle, so a saved
change takes effect on the next ~10s tick with no restart needed."""
import json
import signal
import time
from collections import defaultdict
from pathlib import Path

import django
django.setup()

import psutil  # noqa: E402
from django.db import close_old_connections  # noqa: E402
from django.utils import timezone as django_tz  # noqa: E402

from encoders.models import Encoder  # noqa: E402
from monitoring.models import ListenerPeak, MonitorCheck, TransmitterConfig, emit_event  # noqa: E402
from monitoring.services.notify import maybe_notify  # noqa: E402
from monitoring.services.probes import PROBE_DISPATCH  # noqa: E402
from monitoring.services.shoutcast import fetch_shoutcast_stats  # noqa: E402
from monitoring.services.transmitter_client import TransmitterClient  # noqa: E402

STATE_PATH = Path("/run/isadoraair/monitoring_state.json")
# Separate state file for the Shoutcast listener widget so its 10 s poll
# doesn't have to piggyback on `monitoring_state.json`'s more heavyweight
# check-status payload and vice versa. Dashboard JS fetches this via the
# `listener_status` monitoring endpoint, not directly from disk.
LISTENER_STATE_PATH = Path("/run/isadoraair/listeners.json")
POLL_SECONDS = 10
SHOUTCAST_STATS_TIMEOUT = 3.0  # seconds -- LAN localhost; anything above ~1s is a real problem

_TRANSMITTER_KINDS = ("transmitter_param", "transmitter_indicator")


class MonitorManager:
    def __init__(self):
        self.running = False
        self._consecutive_bad = {}   # check.id -> int
        self._since = {}             # check.id -> timestamp status last changed
        self._current_status = {}    # check.id -> "ok"|"warning"|"critical"|"unknown" (post-debounce)
        self._cooldowns = {}         # str(check.id) -> last_notified_at, persisted in state file
        self._last_tx_poll = 0.0
        self._last_tx_result = {}    # check.id -> {"status":.., "detail":..} from the last real poll

    def start(self):
        self.running = True
        close_old_connections()
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._load_persisted_cooldowns()
        psutil.cpu_percent(interval=None)  # prime the comparator -- first real call is meaningless otherwise

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        print("Monitoring started.")
        while self.running:
            self._run_cycle()
            time.sleep(POLL_SECONDS)
        print("Monitoring stopped.")

    def stop(self):
        self.running = False

    def _handle_signal(self, signum, frame):
        print("\nShutting down...")
        self.running = False

    def _run_cycle(self):
        close_old_connections()
        checks = list(MonitorCheck.objects.filter(enabled=True).order_by("sort_order"))
        tx_config = TransmitterConfig.load()

        tx_client = None
        needs_tx = any(c.kind in _TRANSMITTER_KINDS for c in checks)
        tx_due = (time.monotonic() - self._last_tx_poll) >= tx_config.poll_interval_seconds
        if needs_tx and tx_due and tx_config.host:
            self._last_tx_poll = time.monotonic()
            try:
                tx_client = TransmitterClient(tx_config.host, tx_config.port, tx_config.timeout_seconds)
                tx_client.__enter__()
            except Exception as exc:
                print(f"  [transmitter] Failed to connect: {exc}")
                tx_client = None

        results = []
        for check in checks:
            is_transmitter = check.kind in _TRANSMITTER_KINDS

            # Transmitter checks only get a REAL reading on the ~60s
            # cycle where tx_due is true (its own poll_interval_seconds,
            # slower than the 10s main loop since a TCP round-trip to RF
            # hardware is heavier than a local systemctl/psutil call).
            # Real bug hit live: re-running the probe on every 10s cycle
            # regardless meant tx_client was None 5 out of 6 cycles,
            # and "unknown" isn't debounced the way critical/warning are
            # -- it overwrote a perfectly good last reading with
            # "unknown" for 50 of every 60 seconds. Skip re-evaluating
            # these checks entirely on non-polling cycles and just keep
            # reporting the last real result instead.
            if is_transmitter and not tx_due and check.id in self._last_tx_result:
                cached = self._last_tx_result[check.id]
                results.append(self._build_result(check, cached["status"], cached["detail"]))
                continue

            probe_fn = PROBE_DISPATCH[check.kind]
            try:
                if is_transmitter:
                    status, detail = probe_fn(check, tx_client)
                else:
                    status, detail = probe_fn(check)
            except Exception as exc:
                status, detail = "unknown", {"error": str(exc)}

            prev_status = self._current_status.get(check.id, "ok")
            status = self._debounce(check, status)
            if status != prev_status or check.id not in self._since:
                # `self._since` membership BEFORE we assign it here is
                # the "have we ever observed this check before" signal
                # -- distinguishes a real edge from the first-tick
                # catch-up after a service restart. Only real edges get
                # an event and (independently) a notification.
                is_transition = check.id in self._since
                self._since[check.id] = time.time()
                if is_transition:
                    # Record the transition in SystemEvent BEFORE
                    # notifying so an operator on /monitoring/ sees it
                    # in the Recent Events feed even if the notification
                    # path is disabled (NotificationConfig.enabled=False)
                    # or on cooldown. The feed's whole point is
                    # at-a-glance visibility without email/SMS wired up.
                    self._emit_transition_event(check, prev_status, status, detail)
                maybe_notify(check, status, detail, prev_status, self._cooldowns)

            results.append(self._build_result(check, status, detail))
            if is_transmitter:
                self._last_tx_result[check.id] = {"status": status, "detail": detail}

        if tx_client is not None:
            tx_client.__exit__(None, None, None)

        self._write_state(results)
        # Separate from the check-status write above -- listener poll is
        # independent state (dashboard widget vs. monitoring cards) and
        # a Shoutcast unreachable shouldn't take down the check-status
        # write path (which is what the notification cooldowns and
        # monitoring dashboard depend on).
        try:
            self._poll_shoutcast_listeners()
        except Exception as exc:
            # Best-effort: any failure is contained and just leaves the
            # listener JSON stale for one tick. Log once so a persistent
            # regression shows up rather than swallowing silently.
            print(f"  [listeners] poll failed: {exc}")

    @staticmethod
    def _maybe_reset_tlh_for_new_month(peak, now):
        """Automatic TLH reset: midnight on the 1st of the month, in the
        station's own local time (settings.TIME_ZONE), per operator
        request. Implemented as "has a calendar-month boundary passed
        since tlh_since_at" rather than a precise wall-clock trigger --
        checked on every ~POLL_SECONDS tick, so the reset fires on
        whichever tick first notices the new month (within one poll
        interval of midnight in the common case) and still catches up
        correctly if a tick was missed right around the boundary
        (process restart, poll hiccup) instead of silently carrying the
        old month's total forward indefinitely.

        Mutates `peak` in place (tlh_hours/tlh_since_at) and returns
        whether a reset happened, so the caller knows to include those
        fields in its save(). Does not save() itself -- stays consistent
        with the rest of _poll_shoutcast_listeners' single-combined-save
        pattern."""
        since_local = django_tz.localtime(peak.tlh_since_at)
        now_local = django_tz.localtime(now)
        if (now_local.year, now_local.month) == (since_local.year, since_local.month):
            return False
        peak.tlh_hours = 0.0
        peak.tlh_since_at = now
        return True

    def _poll_shoutcast_listeners(self):
        """Fetch listener counts from every distinct Shoutcast server
        our enabled Encoder rows point at, match each encoder to its
        stream by Encoder.shoutcast_sid (the same normalized value
        Liquidsoap was given as icy_id -- see encoders/models.py), and
        write a compact status document to LISTENER_STATE_PATH for the
        dashboard widget to consume. Uses the shared
        monitoring.services.shoutcast.fetch_shoutcast_stats fetcher --
        the same one probe_encoder_group (probes.py) uses for its own,
        separate per-SID health determination, so there is exactly one
        piece of code that understands the Shoutcast /statistics wire
        format.

        LED semantics:
          green  -- every enabled encoder's SID appears in stats AND
                    its STREAMSTATUS == 1
          yellow -- at least one encoder is up, at least one is down
          red    -- none of our encoders is up (including the case
                    where the Shoutcast server itself is unreachable
                    and all encoders end up "down")

        Only enabled shoutcast1/shoutcast2 encoders count. Icecast
        encoders are silently skipped -- Icecast's stats live at a
        different endpoint (/status-json.xsl) and there aren't any
        Icecast encoders on this station today; a follow-up can add
        that path if the topology ever changes."""
        encoders = list(
            Encoder.objects.filter(
                enabled=True, protocol__in=("shoutcast1", "shoutcast2"),
            )
        )
        if not encoders:
            # No encoders configured -- write an empty-but-well-formed
            # state so the dashboard's fetch handler doesn't see stale
            # data from before the last encoder was disabled. Peak/TLH
            # are still whatever's persisted (not zeroed) -- disabling
            # the last encoder doesn't erase history, it just means
            # nothing is being measured right now.
            peak = ListenerPeak.load()
            if self._maybe_reset_tlh_for_new_month(peak, django_tz.now()):
                peak.save(update_fields=["tlh_hours", "tlh_since_at"])
            self._write_listener_state(
                [], 0, "red", peak.peak_total, peak.peak_since_at, None,
                peak.tlh_hours, peak.tlh_since_at,
            )
            return

        # Group by (host, port). One HTTP GET per distinct server.
        by_server = defaultdict(list)
        for enc in encoders:
            by_server[(enc.host, enc.port)].append(enc)

        # Per-encoder status: {encoder_id: {"name":.., "sid":.., "up":bool, "listeners":int}}
        per_encoder = []
        for (host, port), encs_here in by_server.items():
            stream_stats = fetch_shoutcast_stats(host, port, timeout=SHOUTCAST_STATS_TIMEOUT)
            for enc in encs_here:
                sid = enc.shoutcast_sid
                s = stream_stats.get(sid)
                if s is None:
                    per_encoder.append({
                        "encoder_id": enc.id, "name": enc.name, "sid": sid,
                        "host": host, "port": port,
                        "up": False, "listeners": 0,
                    })
                else:
                    per_encoder.append({
                        "encoder_id": enc.id, "name": enc.name, "sid": sid,
                        "host": host, "port": port,
                        "up": bool(s["up"]), "listeners": int(s["listeners"]),
                    })

        # LED state from per-encoder up-counts.
        up_count = sum(1 for r in per_encoder if r["up"])
        total_encoders = len(per_encoder)
        if up_count == total_encoders:
            led = "green"
        elif up_count == 0:
            led = "red"
        else:
            led = "yellow"

        # Total listeners across the ones that ARE up. A "down" encoder
        # obviously contributes zero -- we just don't count what we
        # can't measure.
        current_total = sum(r["listeners"] for r in per_encoder if r["up"])

        # Peak + TLH singleton update, combined into one save() when
        # either changes so a normal tick (peak unchanged, listeners
        # present) still costs only a single UPDATE, not two.
        #
        # Peak: only touch the DB when we're raising the peak; the
        # normal case is peak_total >= current_total, no write.
        #
        # TLH (Total Listening Hours): unlike peak, this is a running
        # SUM, not a snapshot -- it genuinely changes (goes up) on
        # every tick with at least one listener, so it can't be
        # deferred the same way. Each tick contributes
        # current_total * (POLL_SECONDS / 3600) hours -- a Riemann-sum
        # integration of listener-count over time at the poll's own
        # resolution. Skipped entirely (no write) when current_total
        # is 0 -- adding zero can't change the total, so there's no
        # reason to touch the row. Checked for an automatic monthly
        # reset BEFORE accumulating this tick's contribution, so a
        # reset and a fresh accumulation in the same tick compose
        # correctly (zero, then add) rather than the reset wiping out
        # what this tick just measured.
        peak = ListenerPeak.load()
        peak_reached_at = peak.peak_reached_at
        now = django_tz.now()
        update_fields = []
        if current_total > peak.peak_total:
            peak.peak_total = current_total
            peak.peak_reached_at = now
            peak_reached_at = peak.peak_reached_at
            update_fields += ["peak_total", "peak_reached_at"]
        if self._maybe_reset_tlh_for_new_month(peak, now):
            update_fields += ["tlh_hours", "tlh_since_at"]
        if current_total > 0:
            peak.tlh_hours = (peak.tlh_hours or 0.0) + current_total * (POLL_SECONDS / 3600.0)
            if "tlh_hours" not in update_fields:
                update_fields.append("tlh_hours")
        if update_fields:
            peak.save(update_fields=update_fields)

        self._write_listener_state(
            per_encoder, current_total, led,
            peak.peak_total, peak.peak_since_at, peak_reached_at,
            peak.tlh_hours, peak.tlh_since_at,
        )

    def _write_listener_state(self, per_encoder, current_total, led,
                              peak_total, peak_since_at, peak_reached_at,
                              tlh_hours, tlh_since_at):
        state = {
            "timestamp": time.time(),
            "led": led,                             # "green" | "yellow" | "red"
            "current_total": current_total,
            "peak_total": peak_total,
            "peak_since_at": peak_since_at.isoformat() if peak_since_at else None,
            "peak_reached_at": peak_reached_at.isoformat() if peak_reached_at else None,
            "tlh_hours": tlh_hours,
            "tlh_since_at": tlh_since_at.isoformat() if tlh_since_at else None,
            "encoders": per_encoder,                # per-stream detail for hover/tooltip
        }
        tmp = LISTENER_STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.rename(LISTENER_STATE_PATH)

    # Level of the recorded SystemEvent for each new status. Recovery
    # (into "ok") is info; degraded is warning; failure is error; unknown
    # is warning (a probe that stopped answering is worth surfacing but
    # isn't a confirmed fault). Recovery-from-critical still shows as
    # info -- the operator's eye is drawn to the earlier row, not the
    # relief that follows.
    _STATUS_TO_LEVEL = {
        "ok": "info",
        "warning": "warning",
        "critical": "error",
        "unknown": "warning",
    }

    def _emit_transition_event(self, check, prev_status, new_status, detail):
        level = self._STATUS_TO_LEVEL.get(new_status, "info")
        title = f"{check.name}: {prev_status} → {new_status}"
        emit_event(
            category="monitor",
            level=level,
            title=title,
            detail={
                "check_id": check.id,
                "kind": check.kind,
                "prev_status": prev_status,
                "new_status": new_status,
                "probe_detail": detail,
            },
            # Dedupe on the check + destination status so a flapping
            # check collapses to one row per (check, going-to-status)
            # inside the coalesce window instead of two rows per flap.
            dedupe_key=f"monitor|check={check.id}|to={new_status}",
        )

    def _build_result(self, check, status, detail):
        return {
            "id": check.id,
            "name": check.name,
            "kind": check.kind,
            "sort_order": check.sort_order,
            "status": status,
            "detail": detail,
            "since": self._since.get(check.id),
            "show_as_card": check.show_as_card,
            # The check's own COBALT parameter/indicator string (e.g.
            # "psu.fwd_power", "status.indicator.rf") -- lets the
            # dashboard pair an indicator's raw value with the parameter
            # card it colors, without depending on display names (which
            # an admin could rename).
            "tx_ref": check.transmitter_parameter or check.transmitter_indicator or None,
        }

    def _debounce(self, check, raw_status):
        cid = check.id
        if raw_status in ("critical", "warning"):
            self._consecutive_bad[cid] = self._consecutive_bad.get(cid, 0) + 1
        else:
            self._consecutive_bad[cid] = 0

        if raw_status in ("critical", "warning") and self._consecutive_bad[cid] < check.consecutive_failures_required:
            # Not enough consecutive bad polls yet -- keep reporting the last stable status.
            return self._current_status.get(cid, "ok")

        self._current_status[cid] = raw_status
        return raw_status

    def _load_persisted_cooldowns(self):
        if STATE_PATH.is_file():
            try:
                data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                self._cooldowns = data.get("_cooldowns", {})
            except (OSError, ValueError):
                pass

    def _write_state(self, results):
        state = {
            "timestamp": time.time(),
            "checks": results,
            "_cooldowns": self._cooldowns,
        }
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.rename(STATE_PATH)
