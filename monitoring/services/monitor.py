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
from pathlib import Path

import django
django.setup()

import psutil  # noqa: E402
from django.db import close_old_connections  # noqa: E402

from monitoring.models import MonitorCheck, TransmitterConfig  # noqa: E402
from monitoring.services.notify import maybe_notify  # noqa: E402
from monitoring.services.probes import PROBE_DISPATCH  # noqa: E402
from monitoring.services.transmitter_client import TransmitterClient  # noqa: E402

STATE_PATH = Path("/run/isadoraair/monitoring_state.json")
POLL_SECONDS = 10

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
                self._since[check.id] = time.time()
                maybe_notify(check, status, detail, prev_status, self._cooldowns)

            results.append(self._build_result(check, status, detail))
            if is_transmitter:
                self._last_tx_result[check.id] = {"status": status, "detail": detail}

        if tx_client is not None:
            tx_client.__exit__(None, None, None)

        self._write_state(results)

    def _build_result(self, check, status, detail):
        return {
            "id": check.id,
            "name": check.name,
            "kind": check.kind,
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
