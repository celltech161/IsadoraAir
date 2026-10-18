"""D3E-B: Monitoring of isadoraair-active-message-worker.service.

A plain "Systemd Service" MonitorCheck of that exact unit combines systemd's
state and automatic-restart count with the worker's D3E-A heartbeat, read
through active_messages.supervision.observe -- which accepts the heartbeat
only when it belongs to the running process (MainPID and
ExecMainStartTimestamp) and otherwise reports active_messages.heartbeat's own
verdict. Monitoring stays an observer: no Active Messages row, render, expiry
or restart.

* the probe's health mapping (every row of the D3E-B contract);
* operator detail: concise, no path, no raw document, no Restart button;
* read-only and failure containment;
* the real poll cycle: debounce, transition events, notification, recovery;
* provision_active_message_worker_check (idempotent, never edits a row).
"""
import io
import json
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import SimpleTestCase, TestCase, override_settings

from active_messages import heartbeat, services, supervision
from active_messages.models import ActiveBroadcastExecution, ActiveBroadcastMessageEvent, ActiveBroadcastPolicy
from monitoring.models import MonitorCheck, SystemEvent
from monitoring.services import monitor as monitor_module
from monitoring.services import probes

UNIT = "isadoraair-active-message-worker.service"
PID = 31337
EXECUTION = "0f8fad5b-d9cb-469f-a165-70867728950e"
SHOWN = {"active_state", "sub_state", "uptime_seconds", "restart_via_dashboard", "n_restarts", "reason",
         "native_airing", "phase", "outcome", "code", "execution", "heartbeat_age_seconds", "problem", "error"}


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%a %Y-%m-%d %H:%M:%S UTC")


def systemctl(active="active", sub="running", restarts="0", pid=PID, started=None):
    started = time.time() - 600 if started is None else started
    stdout = (f"ActiveState={active}\nSubState={sub}\nActiveEnterTimestamp={stamp(started + 3)}\n"
              f"NRestarts={restarts}\nMainPID={pid}\nExecMainStartTimestamp={stamp(started)}\n")
    return subprocess.CompletedProcess(["systemctl"], 0, stdout=stdout, stderr="")


def document(*, pid=PID, started=None, age=2.0, **changes):
    """A valid heartbeat written ``age`` seconds ago by ``pid`` (started ``started``)."""
    now = time.time()
    started = now - 590 if started is None else started
    base = {"schema": heartbeat.SCHEMA, "pid": pid, "started_at": started, "checked_at": now - age,
            "recent_starts": [started], "previous": "none", "phase": "polling", "cycle": 40, "result": "idle",
            "code": "", "execution": "", "repeats": 40, "consecutive_errors": 0, "last_progress_at": None,
            "render_deadline": None}
    base.update(changes)
    return base


class Heartbeat:
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "active_message_worker.json"
        settings = override_settings(ACTIVE_MESSAGE_WORKER_STATE_PATH=str(self.path))
        settings.enable()
        self.addCleanup(settings.disable)
        probes._last_restarts.clear()
        self.addCleanup(probes._last_restarts.clear)

    def beat(self, doc=None, **changes):
        self.path.write_text(json.dumps(doc if doc is not None else document(**changes)))


class ProbeTests(Heartbeat, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.check = MonitorCheck(id=951, name="Active Message Worker", kind="systemd", systemd_unit=UNIT)

    def probe(self, unit_state=None):
        with mock.patch.object(probes.subprocess, "run", return_value=unit_state or systemctl()) as ran:
            status, detail = probes.probe_systemd(self.check)
        self.assertEqual([call.args[0][:2] for call in ran.call_args_list], [["systemctl", "show"]])   # read only
        self.assertLessEqual(set(detail), SHOWN)
        self.assertIs(detail["restart_via_dashboard"], False)             # never a Restart button
        return status, detail

    def verdict(self, unit_state=None):
        status, detail = self.probe(unit_state)
        return status, detail.get("reason")

    # -- healthy -------------------------------------------------------------------------
    def test_active_with_a_fresh_idle_heartbeat(self):
        self.beat()
        status, detail = self.probe()
        self.assertEqual((status, detail["reason"]), ("ok", "idle"))
        self.assertEqual((detail["phase"], detail["outcome"], detail["native_airing"]), ("polling", "idle", "closed"))
        self.assertLess(detail["heartbeat_age_seconds"], 10)
        self.assertGreater(detail["uptime_seconds"], 500)
        self.assertEqual(detail["n_restarts"], 0)
        text = json.dumps(detail)
        for secret in (str(self.path), "/run/", str(PID), "recent_starts", "started_at"):
            self.assertNotIn(secret, text)                                 # concise; no path or raw document

    def test_ready_behind_the_closed_native_airing_gate_is_ok(self):
        self.beat(result="idle", code="gated", execution=EXECUTION)
        status, detail = self.probe()
        self.assertEqual((status, detail["reason"]), ("ok", "ready_waiting_for_gate"))
        self.assertEqual((detail["execution"], detail["native_airing"]), (EXECUTION, "closed"))

    def test_a_long_render_within_its_deadline_then_past_it(self):
        now = time.time()
        unit_state = systemctl(started=now - 4005)
        self.beat(phase="rendering", execution=EXECUTION, started=now - 4000, age=1800, render_deadline=now + 600)
        self.assertEqual(self.verdict(unit_state), ("ok", "rendering"))    # 30 minutes in: not stale
        self.beat(phase="rendering", execution=EXECUTION, started=now - 4000, age=3700, render_deadline=now - 10)
        self.assertEqual(self.verdict(unit_state), ("critical", "render_overdue"))

    def test_retry_backoff_and_in_flight_work_are_ok(self):
        for result, reason in (("retry", "retry_backoff"), ("backoff", "retry_backoff"),
                               ("playout", "playout_in_flight"), ("ready", "working")):
            with self.subTest(result=result):
                self.beat(result=result, repeats=1, execution=EXECUTION if result != "playout" else "",
                          last_progress_at=time.time() - 3 if result == "ready" else None)
                self.assertEqual(self.verdict(), ("ok", reason))

    # -- degraded -------------------------------------------------------------------------
    def test_blocked_lock_held_elsewhere_and_database_errors(self):
        self.beat(result="blocked", code="render_failed", repeats=5)
        self.assertEqual(self.verdict(), ("warning", "blocked"))
        self.beat(result="busy", repeats=heartbeat.BUSY_WARNING_REPEATS)
        self.assertEqual(self.verdict(), ("warning", "lock_held_elsewhere"))
        self.beat(result="error", code="OperationalError", repeats=1, consecutive_errors=1)
        self.assertEqual(self.verdict(), ("warning", "database_errors"))
        repeats = heartbeat.ERROR_CRITICAL_REPEATS
        self.beat(result="error", code="OperationalError", repeats=repeats, consecutive_errors=repeats)
        self.assertEqual(self.verdict(), ("critical", "malfunction"))

    def test_stale_malformed_and_future_heartbeats_are_critical(self):
        self.beat(age=heartbeat.STALE_AFTER_SECONDS + 5)
        self.assertEqual(self.verdict(), ("critical", "stale"))
        self.path.write_text("{not json")
        self.assertEqual(self.verdict(), ("critical", "malformed"))
        self.beat(schema=1)
        status, detail = self.probe()
        self.assertEqual((status, detail["reason"], detail["problem"]), ("critical", "malformed", "unsupported_schema"))
        self.beat(age=-600)
        status, detail = self.probe()
        self.assertEqual((status, detail["reason"], detail["problem"]), ("critical", "malformed", "future_timestamp"))
        self.assertNotIn("phase", detail)                                  # nothing read from a rejected document

    def test_missing_heartbeat_is_starting_in_the_grace_then_critical(self):
        just_started = time.time() - 10
        self.assertEqual(self.verdict(systemctl(started=just_started)), ("unknown", "starting"))
        self.assertEqual(self.verdict(), ("critical", "no_heartbeat"))     # 10 minutes after the start

    def test_a_heartbeat_from_a_previous_process(self):
        self.beat(pid=PID - 1)                                             # the old process's healthy record
        self.assertEqual(self.verdict(systemctl(started=time.time() - 10)), ("unknown", "starting"))
        status, detail = self.probe()
        self.assertEqual((status, detail["reason"]), ("critical", "heartbeat_not_current"))
        self.assertNotIn("phase", detail)
        earlier = time.time() - 900                                        # same pid number, earlier process
        self.beat(started=earlier, recent_starts=[earlier])
        self.assertEqual(self.verdict(systemctl(started=time.time() - 600)), ("critical", "heartbeat_not_current"))

    def test_stopped_or_failed_service_with_a_healthy_heartbeat(self):
        self.beat()
        with mock.patch.object(heartbeat, "read_state", side_effect=AssertionError("not read")):
            for (active, sub), reason in {("failed", "failed"): "service_failed",
                                          ("inactive", "dead"): "service_inactive",
                                          ("deactivating", "stop-sigterm"): "service_inactive",
                                          ("activating", "auto-restart"): "restarting"}.items():
                with self.subTest(active=active, sub=sub):
                    self.assertEqual(self.verdict(systemctl(active, sub)), ("critical", reason))
            self.assertEqual(self.verdict(systemctl("activating", "start-post")), ("unknown", "starting"))

    def test_a_recorded_stop_while_systemd_says_active_is_critical(self):
        self.beat(phase="stopped")
        self.assertEqual(self.verdict(), ("critical", "stopped"))

    def test_automatic_restart_count_increasing(self):
        self.beat()
        self.assertEqual(self.verdict(systemctl(restarts="2")), ("ok", "idle"))
        status, detail = self.probe(systemctl(restarts="3"))
        self.assertEqual((status, detail["reason"], detail["n_restarts"]), ("warning", "restarted", 3))
        self.assertEqual(self.verdict(systemctl(restarts="3")), ("ok", "idle"))     # stable again

    def test_three_rapid_crashes_are_a_crash_loop(self):
        now = time.time()
        starts = [now - 120, now - 60, now - 10]
        self.beat(started=starts[-1], recent_starts=starts, previous="unclean", cycle=1, repeats=1)
        self.assertEqual(self.verdict(systemctl(restarts="2", started=starts[-1])), ("critical", "crash_looping"))

    def test_three_clean_operator_restarts_are_not(self):
        now = time.time()
        for started in (now - 120, now - 60, now - 10):
            self.beat(started=started, recent_starts=[started], previous="clean", cycle=1, repeats=1)
            self.assertEqual(self.verdict(systemctl(started=started)), ("ok", "idle"))

    def test_unverifiable_history_is_a_warning_not_a_crash_loop(self):
        started = time.time() - 30
        self.beat(started=started, recent_starts=[started], previous="malformed", cycle=2, repeats=2)
        self.assertEqual(self.verdict(systemctl(started=started)), ("warning", "history_unverified"))

    def test_no_main_pid_is_unknown(self):
        self.beat()
        self.assertEqual(self.verdict(systemctl(pid=0)), ("unknown", "identity_unavailable"))

    # -- containment -------------------------------------------------------------------------
    def test_state_and_database_read_failures_never_crash_monitoring(self):
        self.path.mkdir()                                                  # not a regular file
        self.assertEqual(self.verdict(), ("critical", "malformed"))
        self.path.rmdir()
        self.beat()
        with mock.patch.object(heartbeat, "gate_open_read_only",
                               side_effect=OperationalError("could not connect to server at db.internal")):
            status, detail = self.probe()
        self.assertEqual((status, detail["reason"], detail["error"]), ("unknown", "observer_error", "OperationalError"))
        self.assertNotIn("db.internal", json.dumps(detail))
        with mock.patch.object(supervision, "observe", side_effect=RuntimeError("bug")):
            self.assertEqual(self.verdict(), ("unknown", "observer_error"))

    def test_other_systemd_checks_are_unchanged(self):
        other = MonitorCheck(id=952, name="Engine", kind="systemd", systemd_unit="isadoraair-engine.service")
        with mock.patch.object(probes.subprocess, "run", return_value=systemctl()), \
                mock.patch.object(supervision, "observe") as observer:
            status, detail = probes.probe_systemd(other)
        self.assertEqual((status, set(detail)), ("ok", {"active_state", "sub_state", "uptime_seconds"}))
        observer.assert_not_called()


class ReadOnlyProbeTests(Heartbeat, TestCase):
    """Observing the worker never touches Active Messages: no policy row, no
    execution, no event, no render, no expiry -- not even a query."""

    def test_nothing_is_created_rendered_or_expired(self):
        check = MonitorCheck(id=953, name="Active Message Worker", kind="systemd", systemd_unit=UNIT)
        refuse = AssertionError("Monitoring must not act")
        for scenario in ({}, {"result": "idle", "code": "gated", "execution": EXECUTION},
                         {"result": "blocked", "code": "render_failed"}, {"age": 600}):
            self.beat(**scenario)
            with mock.patch.object(services, "materialize_due_execution", side_effect=refuse), \
                    mock.patch.object(services, "current_policy", side_effect=refuse), \
                    mock.patch("active_messages.rendering.render", side_effect=refuse), \
                    mock.patch.object(probes.subprocess, "run", return_value=systemctl()), \
                    self.assertNumQueries(0):
                probes.probe_systemd(check)
        self.assertFalse(ActiveBroadcastPolicy.objects.exists())
        self.assertFalse(ActiveBroadcastExecution.objects.exists())
        self.assertFalse(ActiveBroadcastMessageEvent.objects.exists())


class MonitorCycleTests(Heartbeat, TestCase):
    """Through the real poll cycle: debounce, transition events, notifications."""

    def setUp(self):
        super().setUp()
        scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, scratch, True)
        for name in ("STATE_PATH", "LISTENER_STATE_PATH"):              # never the station's own state files
            patcher = mock.patch.object(monitor_module, name, scratch / f"{name.lower()}.json")
            patcher.start()
            self.addCleanup(patcher.stop)
        MonitorCheck.objects.all().update(enabled=False)
        self.check = MonitorCheck.objects.create(name="Active Message Worker", kind="systemd", systemd_unit=UNIT,
                                                 consecutive_failures_required=2, sort_order=1)
        self.other = MonitorCheck.objects.create(name="Disk", kind="disk", disk_path="/", critical_threshold=101,
                                                 sort_order=2)
        self.manager = monitor_module.MonitorManager()

    def cycle(self, unit_state=None):
        written = []
        with mock.patch.object(self.manager, "_write_state", side_effect=written.append), \
                mock.patch.object(self.manager, "_poll_shoutcast_listeners"), \
                mock.patch.object(monitor_module, "close_old_connections"), \
                mock.patch.object(monitor_module, "create_transmitter_driver", return_value=None), \
                mock.patch.object(probes.subprocess, "run", return_value=unit_state or systemctl()), \
                mock.patch.object(monitor_module, "maybe_notify") as notify:
            self.manager._run_cycle()
        results = {result["id"]: result for result in written[0]}
        return results[self.check.id], [c for c in notify.call_args_list if c.args[0].pk == self.check.pk], results

    def events(self):
        return list(SystemEvent.objects.filter(title__startswith="Active Message Worker").order_by("pk")
                    .values_list("title", flat=True))

    def test_debounce_transition_notification_and_recovery(self):
        self.beat()
        result, notified, _ = self.cycle()
        self.assertEqual((result["status"], len(notified)), ("ok", 1))     # first observation (not an edge)
        self.beat(age=heartbeat.STALE_AFTER_SECONDS + 5)
        result, notified, _ = self.cycle()                                 # one bad poll: debounced
        self.assertEqual((result["status"], notified), ("ok", []))
        result, notified, _ = self.cycle()                                 # second: the edge
        self.assertEqual((result["status"], result["detail"]["reason"]), ("critical", "stale"))
        [call] = notified
        self.assertEqual(call.args[1:4], ("critical", result["detail"], "ok"))
        self.assertEqual(self.events(), ["Active Message Worker: ok → critical"])
        for _ in range(3):                                                 # still stale: no repeat alert
            result, notified, _ = self.cycle()
            self.assertEqual(notified, [])
        self.assertEqual(len(self.events()), 1)
        self.beat()                                                        # recovery
        result, notified, _ = self.cycle()
        self.assertEqual((result["status"], len(notified)), ("ok", 1))
        self.assertEqual(self.events(), ["Active Message Worker: ok → critical", "Active Message Worker: critical → ok"])

    def test_a_service_failure_alerts_and_the_dashboard_offers_no_restart(self):
        self.beat()
        self.cycle()
        self.cycle(systemctl("failed", "failed"))
        result, notified, _ = self.cycle(systemctl("failed", "failed"))
        self.assertEqual((result["status"], result["detail"]["reason"]), ("critical", "service_failed"))
        self.assertEqual(len(notified), 1)
        self.assertIs(result["detail"]["restart_via_dashboard"], False)
        self.assertEqual(result["systemd_unit"], UNIT)

    def test_a_probe_failure_stays_contained(self):
        with mock.patch.object(probes, "_active_message_worker_readiness", side_effect=RuntimeError("probe bug")), \
                mock.patch.dict(probes.APPLICATION_READINESS, {UNIT: probes._active_message_worker_readiness}):
            result, _, results = self.cycle()
        self.assertEqual(result["status"], "unknown")
        self.assertIn(self.other.id, results)                              # the rest of the cycle still ran


class ProvisionCommandTests(TestCase):
    def run_command(self, *args):
        out = io.StringIO()
        call_command("provision_active_message_worker_check", *args, stdout=out)
        return out.getvalue()

    def test_creates_once_and_never_changes_an_existing_row(self):
        MonitorCheck.objects.filter(systemd_unit=UNIT).delete()
        self.assertIn("would create", self.run_command("--dry-run"))
        self.assertFalse(MonitorCheck.objects.filter(systemd_unit=UNIT).exists())
        self.assertIn("created", self.run_command())
        check = MonitorCheck.objects.get(systemd_unit=UNIT)
        self.assertEqual((check.name, check.kind, check.enabled, check.consecutive_failures_required),
                         ("Active Message Worker", "systemd", True, 2))
        check.name, check.enabled, check.consecutive_failures_required = "AM worker", False, 4
        check.save()
        self.assertIn("unchanged", self.run_command())
        again = MonitorCheck.objects.get(systemd_unit=UNIT)
        self.assertEqual((again.pk, again.name, again.enabled, again.consecutive_failures_required),
                         (check.pk, "AM worker", False, 4))
        self.assertEqual(MonitorCheck.objects.filter(systemd_unit=UNIT).count(), 1)

    def test_a_name_held_by_another_check_is_refused(self):
        MonitorCheck.objects.filter(systemd_unit=UNIT).delete()
        MonitorCheck.objects.create(name="Active Message Worker", kind="disk", disk_path="/", critical_threshold=90)
        with self.assertRaisesRegex(CommandError, "already named"):
            self.run_command()
        self.assertFalse(MonitorCheck.objects.filter(systemd_unit=UNIT).exists())


class DashboardTemplateTests(SimpleTestCase):
    def test_worker_reasons_have_captions(self):
        template = (Path(__file__).resolve().parents[1] / "templates" / "monitoring" / "dashboard.html").read_text()
        for reason in ("starting", "service_failed", "no_heartbeat", "heartbeat_not_current", "stale", "malformed",
                       "crash_looping", "render_overdue", "malfunction", "database_errors", "blocked",
                       "lock_held_elsewhere", "history_unverified", "identity_unavailable"):
            self.assertRegex(template, rf"\n  {reason}: \"")
        self.assertIn("${workerLine(d)}", template)
        self.assertIn("escapeHtmlAttr(parts.join(", template)


def systemctl_raw(*, start_line, active="active", sub="running", pid=PID, restarts="0"):
    """``systemctl show`` output with an exact ExecMainStartTimestamp line (or none)."""
    lines = [f"ActiveState={active}", f"SubState={sub}", f"ActiveEnterTimestamp={stamp(time.time() - 590)}",
             f"NRestarts={restarts}", f"MainPID={pid}"]
    if start_line is not None:
        lines.append(start_line)
    return subprocess.CompletedProcess(["systemctl"], 0, stdout="\n".join(lines) + "\n", stderr="")


class ProcessIdentityTests(Heartbeat, SimpleTestCase):
    """D3E-B correction B1: without a verified process start, a heartbeat is
    never current -- PID equality alone proves nothing (PIDs are reused)."""

    UNVERIFIABLE = {"missing": None, "empty": "ExecMainStartTimestamp=",
                    "malformed": "ExecMainStartTimestamp=yesterday-ish",
                    "n/a": "ExecMainStartTimestamp=n/a"}

    def setUp(self):
        super().setUp()
        self.check = MonitorCheck(id=954, name="Active Message Worker", kind="systemd", systemd_unit=UNIT)

    def probe(self, unit_state):
        with mock.patch.object(probes.subprocess, "run", return_value=unit_state):
            status, detail = probes.probe_systemd(self.check)
        self.assertLessEqual(set(detail), SHOWN)
        return status, detail

    def test_a_reused_pid_with_a_stale_healthy_heartbeat_is_never_current_without_a_start_time(self):
        earlier = time.time() - 3600                    # the previous process, same pid number, healthy-looking
        self.beat(started=earlier, recent_starts=[earlier])
        for label, line in self.UNVERIFIABLE.items():
            with self.subTest(label):
                status, detail = self.probe(systemctl_raw(start_line=line))
                self.assertEqual((status, detail["reason"]), ("unknown", "identity_unavailable"))
                self.assertNotIn("phase", detail)       # nothing reported from an unproven document
        # the known contradictory start is a confirmed mismatch (never weaker than unknown)
        now = time.time()
        status, detail = self.probe(systemctl_raw(start_line=f"ExecMainStartTimestamp={stamp(now - 600)}"))
        self.assertEqual((status, detail["reason"]), ("critical", "heartbeat_not_current"))
        status, detail = self.probe(systemctl_raw(start_line=f"ExecMainStartTimestamp={stamp(now - 10)}"))
        self.assertEqual((status, detail["reason"]), ("unknown", "starting"))

    def test_the_current_process_without_a_start_time_is_still_unverified(self):
        self.beat()                                     # truly current, but systemd gives no start time
        for label, line in self.UNVERIFIABLE.items():
            with self.subTest(label):
                status, detail = self.probe(systemctl_raw(start_line=line))
                self.assertEqual((status, detail["reason"]), ("unknown", "identity_unavailable"))
        self.path.unlink()                              # and with no heartbeat at all
        self.assertEqual(self.probe(systemctl_raw(start_line=None))[0], "unknown")

    def test_a_valid_start_time_keeps_its_two_second_precision_tolerance(self):
        now = time.time()
        start = float(int(now - 100))                   # what systemd prints: whole seconds
        for offset, current in ((0.0, True), (-1.5, True), (-2.0, True), (-2.5, False), (-30, False)):
            with self.subTest(offset=offset):
                self.beat(started=start + offset, recent_starts=[start + offset])
                status, detail = self.probe(systemctl_raw(start_line=f"ExecMainStartTimestamp={stamp(start)}"))
                if current:
                    self.assertEqual((status, detail["reason"]), ("ok", "idle"))
                else:                                   # 100 s after the start: past the grace, a mismatch
                    self.assertEqual((status, detail["reason"]), ("critical", "heartbeat_not_current"))

    def test_legitimate_current_states_are_unchanged(self):
        start = time.time() - 600
        line = f"ExecMainStartTimestamp={stamp(start)}"
        for changes, expected in (({}, ("ok", "idle")),
                                  ({"result": "idle", "code": "gated", "execution": EXECUTION},
                                   ("ok", "ready_waiting_for_gate")),
                                  ({"result": "blocked", "code": "render_failed"}, ("warning", "blocked")),
                                  ({"age": 120}, ("critical", "stale"))):
            with self.subTest(changes=changes):
                self.beat(started=start + 1, recent_starts=[start + 1], **changes)
                status, detail = self.probe(systemctl_raw(start_line=line))
                self.assertEqual((status, detail["reason"]), expected)

    def test_the_identity_rule_itself_fails_closed(self):
        beat = heartbeat.validate(document())
        for started in (None, float("nan"), float("inf"), "1760000000", True, 0, -5):
            with self.subTest(started=started):
                self.assertFalse(supervision.belongs(beat, pid=PID, process_started_at=started))
        self.assertTrue(supervision.belongs(beat, pid=PID, process_started_at=beat.started_at + 2.0))
        self.assertFalse(supervision.belongs(beat, pid=PID, process_started_at=beat.started_at + 2.01))
        found = supervision.observe(main_pid=PID, process_started_at=None, now=time.time(), path=self.path)
        self.assertEqual((found["status"], found["condition"]), ("unknown", "identity_unavailable"))
