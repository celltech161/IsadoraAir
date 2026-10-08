"""r0108: Monitoring makes a failed isadoraair-validation visible.

A plain "Systemd Service" MonitorCheck of ``isadoraair-validation.service``
also asks the service's own read-only readiness observer
(production.services.validation_health): socket present, status file valid,
and an empty-request handshake answered. Monitoring stays an observer -- no
validation run, no admission change, no restart -- and every failure is
reported through the existing debounce, event and notification path.

Three layers:
* the observer against scripted sockets (every failure shape, hostile replies);
* the observer against the REAL service (transient user unit with the
  production lifecycle): healthy, at its limits, stopped, killed, recovered,
  and proof the handshake creates no run and disturbs no validation;
* the Monitoring probe and poll cycle: status mapping, automatic-restart
  evidence, debounce, events, notifications, isolation.
"""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from monitoring.models import MonitorCheck, SystemEvent
from monitoring.services import monitor as monitor_module
from monitoring.services import probes
from production.services import admission, validation_health as health

UNIT = "isadoraair-validation.service"


def _runtime_dir(testcase) -> Path:
    directory = Path(tempfile.mkdtemp(prefix="vh-", dir=os.environ.get("XDG_RUNTIME_DIR") or None))
    directory.chmod(0o700)
    testcase.addCleanup(shutil.rmtree, directory, True)
    return directory


class ScriptedService:
    """A socket that answers each connection with ``reply`` (bytes), after
    reading until the client half-closes -- or ``hang``s, or never accepts."""

    def __init__(self, path, reply=b'{"status": "unavailable"}\n', *, hang=False, accept=True):
        self.path, self.reply, self.hang = path, reply, hang
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(4)
        self.connections = 0
        self.received = []
        self.stop = threading.Event()
        if accept:
            threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        self.listener.settimeout(0.1)
        while not self.stop.is_set():
            try:
                conn, _ = self.listener.accept()
            except OSError:
                continue
            self.connections += 1
            with conn:
                conn.settimeout(5)
                data = b""
                try:
                    while chunk := conn.recv(4096):
                        data += chunk
                except OSError:
                    pass
                self.received.append(data)
                if self.hang:
                    self.stop.wait(5)
                    continue
                try:
                    conn.sendall(self.reply)
                except OSError:
                    pass

    def close(self):
        self.stop.set()
        self.listener.close()


class ObserverTests(SimpleTestCase):
    def setUp(self):
        self.dir = _runtime_dir(self)
        self.sock = self.dir / "validator.sock"

    def serve(self, *args, **kwargs):
        service = ScriptedService(self.sock, *args, **kwargs)
        self.addCleanup(service.close)
        return service

    def status(self, payload):
        Path(admission.status_path(str(self.sock))).write_text(payload)

    def observe(self, timeout=0.5):
        started = time.monotonic()
        result = health.observe(str(self.sock), timeout=timeout)
        self.assertLess(time.monotonic() - started, timeout + 1.0)          # always bounded
        return result

    def test_healthy(self):
        service = self.serve()
        self.status(json.dumps({"max_active": 2, "max_pending": 4}))
        self.assertEqual(self.observe(), {"ready": True, "reason": None, "max_active": 2, "max_pending": 4,
                                          "at_capacity": False})
        self.assertEqual(service.received, [b""])                          # the empty request: nothing sent

    def test_at_capacity_is_still_ready(self):
        self.serve(b'{"status": "busy", "returncode": null, "stdout": "", "stderr": "x"}\n')
        self.status(json.dumps({"max_active": 1, "max_pending": 0}))
        result = self.observe()
        self.assertTrue(result["ready"])
        self.assertTrue(result["at_capacity"])

    def test_missing_and_invalid_socket(self):
        self.assertEqual(self.observe()["reason"], health.SOCKET_MISSING)
        self.sock.write_text("not a socket")
        self.assertEqual(self.observe()["reason"], health.SOCKET_INVALID)

    def test_a_stale_socket_file_nobody_listens_on(self):
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.sock))
        stale.close()                                                       # the file stays, nobody listens
        self.status(json.dumps({"max_active": 2, "max_pending": 4}))
        self.assertEqual(self.observe()["reason"], health.NOT_LISTENING)

    def test_unresponsive_service(self):
        self.serve(hang=True)
        self.assertEqual(self.observe(timeout=0.3)["reason"], health.UNRESPONSIVE)
        self.sock.unlink()
        self.serve(accept=False)                                            # listening, accept loop stuck
        self.assertEqual(self.observe(timeout=0.3)["reason"], health.UNRESPONSIVE)

    def test_malformed_and_hostile_replies(self):
        replies = {
            "not json": b"garbage\n",
            "not an object": b"[1, 2]\n",
            "unknown status": b'{"status": "ok"}\n',
            "a validation result": b'{"status": "valid"}\n',
            "binary": b"\xff\xfe\x00\n",
            "closed without a reply": b"",
            "oversized, no newline": b"x" * (2 * 1024 * 1024),
        }
        self.status(json.dumps({"max_active": 2, "max_pending": 4}))
        for label, reply in replies.items():
            with self.subTest(label):
                if self.sock.exists():
                    self.sock.unlink()
                self.serve(reply)
                result = self.observe()
                self.assertFalse(result["ready"])
                self.assertEqual(result["reason"], health.UNEXPECTED_REPLY)

    def test_status_file_problems_are_distinguished(self):
        self.serve()
        self.assertEqual(self.observe()["reason"], health.STATUS_MISSING)
        for payload in ("{", "[]", json.dumps({"max_active": 9, "max_pending": 4}),
                        json.dumps({"max_active": True, "max_pending": 4}), json.dumps({"max_active": 2})):
            with self.subTest(payload=payload):
                self.status(payload)
                self.assertEqual(self.observe()["reason"], health.STATUS_MALFORMED)

    def test_the_default_socket_is_the_configured_one(self):
        self.serve()
        self.status(json.dumps({"max_active": 3, "max_pending": 8}))
        with override_settings(PRODUCTION_VALIDATION_SOCKET=str(self.sock)):
            self.assertEqual(health.observe(timeout=0.5)["max_active"], 3)


class RealServiceTests(SimpleTestCase):
    """The observer against the real service in its production lifecycle."""

    def start(self, *args):
        from production.tests.validation_service_support import TransientValidationService
        service = TransientValidationService(args=args)
        self.addCleanup(service.stop)
        return service

    def test_healthy_with_the_running_limits(self):
        service = self.start()
        self.assertEqual(health.observe(service.socket), {"ready": True, "reason": None, "max_active": 2,
                                                          "max_pending": 4, "at_capacity": False})
        configured = self.start("--max-active=3", "--max-pending=0")
        result = health.observe(configured.socket)
        self.assertEqual((result["ready"], result["max_active"], result["max_pending"]), (True, 3, 0))

    def test_the_handshake_runs_nothing_and_disturbs_no_validation(self):
        from production.services import validation
        from production.tests.support import fixture
        service = self.start()
        pid = service.main_pid()
        stop = threading.Event()
        observed, leaves_seen = [], []

        def monitor():
            while not stop.is_set():
                observed.append(health.observe(service.socket)["ready"])

        source = Path(tempfile.mkdtemp()) / "take.wav"
        self.addCleanup(shutil.rmtree, source.parent, True)
        source.write_bytes(fixture("wav16_mono.wav"))
        thread = threading.Thread(target=monitor, daemon=True)
        with service.active():
            thread.start()
            try:
                outcomes = [validation._analyze_path(source) for _ in range(3)]
                leaves_seen.append(len(service.leaves()))
            finally:
                stop.set()
                thread.join(10)
        self.assertTrue(all(outcome.status == validation.STATUS_VALID for outcome in outcomes), outcomes)
        self.assertGreater(len(observed), 3)
        self.assertTrue(all(observed))
        self.assertEqual(service.main_pid(), pid)                          # the service was never restarted
        for _ in range(50):
            self.assertTrue(health.observe(service.socket)["ready"])
        self.assertEqual(service.leaves(), [])                              # a handshake never leaves a run
        self.assertEqual(leaves_seen, [0])                                  # (the validations' own were destroyed)

    def test_stopped_killed_and_recovered(self):
        service = self.start()
        pid = service.kill_supervisor(9)                                  # crash: socket file left behind
        reasons = set()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = health.observe(service.socket, timeout=0.5)
            if result["ready"]:
                break
            reasons.add(result["reason"])
            time.sleep(0.05)
        self.assertTrue(result["ready"], reasons)                          # Restart=always brought it back
        self.assertNotEqual(service.main_pid(), pid)
        self.assertTrue(reasons and reasons <= {health.NOT_LISTENING, health.SOCKET_MISSING, health.STATUS_MISSING},
                        reasons)
        socket_path = service.socket
        service.stop()
        self.assertEqual(health.observe(socket_path)["reason"], health.SOCKET_MISSING)


def _systemctl(active="active", sub="running", restarts="0"):
    stdout = f"ActiveState={active}\nSubState={sub}\nActiveEnterTimestamp=\nNRestarts={restarts}\n"
    return subprocess.CompletedProcess(["systemctl"], 0, stdout=stdout, stderr="")


READY = {"ready": True, "reason": None, "max_active": 2, "max_pending": 4, "at_capacity": False}


class ProbeTests(SimpleTestCase):
    def setUp(self):
        probes._last_restarts.clear()
        self.check = MonitorCheck(id=901, name="Validation Service", kind="systemd", systemd_unit=UNIT)

    def probe(self, systemctl=None, observed=READY, observe_error=None):
        run = mock.patch.object(probes.subprocess, "run", return_value=systemctl or _systemctl())
        watch = mock.patch.object(health, "observe", return_value=observed, side_effect=observe_error)
        with run as ran, watch as observer:
            status, detail = probes.probe_systemd(self.check)
        self.assertEqual([call.args[0][:2] for call in ran.call_args_list], [["systemctl", "show"]])   # read only
        return status, detail, observer

    def test_healthy(self):
        status, detail, _ = self.probe()
        self.assertEqual(status, "ok")
        self.assertEqual((detail["max_active"], detail["max_pending"], detail["at_capacity"]), (2, 4, False))
        self.assertIs(detail["restart_via_dashboard"], False)
        self.assertNotIn("reason", detail)

    def test_stopped_failed_and_restarting(self):
        cases = {("inactive", "dead"): "service_inactive", ("failed", "failed"): "service_inactive",
                 ("activating", "auto-restart"): "restarting", ("activating", "start"): "restarting"}
        for (active, sub), reason in cases.items():
            with self.subTest(active=active, sub=sub):
                status, detail, observer = self.probe(_systemctl(active, sub))
                self.assertEqual((status, detail["reason"]), ("critical", reason))
                observer.assert_not_called()                    # no socket contact with a dead service

    def test_automatic_restarts_since_the_previous_poll_are_critical(self):
        self.assertEqual(self.probe(_systemctl(restarts="3"))[0], "ok")
        status, detail, _ = self.probe(_systemctl(restarts="4"))
        self.assertEqual((status, detail["reason"], detail["n_restarts"]), ("critical", "restarted", 4))
        self.assertEqual(self.probe(_systemctl(restarts="4"))[0], "ok")          # stable again

    def test_socket_and_status_failures(self):
        for reason in (health.SOCKET_MISSING, health.NOT_LISTENING, health.UNRESPONSIVE, health.UNEXPECTED_REPLY,
                       health.UNREACHABLE, health.SOCKET_INVALID):
            with self.subTest(reason=reason):
                status, detail, _ = self.probe(observed={**READY, "ready": False, "reason": reason})
                self.assertEqual((status, detail["reason"]), ("critical", reason))
        for reason in (health.STATUS_MISSING, health.STATUS_MALFORMED):
            with self.subTest(reason=reason):
                status, detail, _ = self.probe(observed={**READY, "ready": False, "reason": reason,
                                                         "max_active": None, "max_pending": None})
                self.assertEqual((status, detail["reason"]), ("warning", reason))

    def test_an_observer_failure_is_unknown_never_a_crash(self):
        status, detail, _ = self.probe(observe_error=RuntimeError("boom"))
        self.assertEqual((status, detail["reason"]), ("unknown", "observer_error"))

    def test_without_systemctl_the_check_is_unknown(self):
        with mock.patch.object(probes.subprocess, "run", side_effect=FileNotFoundError("systemctl")):
            self.assertEqual(probes.probe_systemd(self.check)[0], "unknown")

    def test_other_systemd_checks_are_unchanged(self):
        other = MonitorCheck(id=902, name="Engine", kind="systemd", systemd_unit="isadoraair-engine.service")
        with mock.patch.object(probes.subprocess, "run", return_value=_systemctl(restarts="7")), \
                mock.patch.object(health, "observe") as observer:
            self.assertEqual(probes.probe_systemd(other), ("ok", {"active_state": "active", "sub_state": "running"}))
            with mock.patch.object(probes.subprocess, "run", return_value=_systemctl("failed", "failed")):
                self.assertEqual(probes.probe_systemd(other)[0], "critical")
        observer.assert_not_called()


class MonitorCycleTests(TransactionTestCase):
    """Through the real poll cycle: debounce, transition events, notifications."""

    def setUp(self):
        probes._last_restarts.clear()
        MonitorCheck.objects.all().update(enabled=False)
        self.check = MonitorCheck.objects.create(name="Validation Service", kind="systemd", systemd_unit=UNIT,
                                                 consecutive_failures_required=2, sort_order=1)
        self.other = MonitorCheck.objects.create(name="Disk", kind="disk", disk_path="/", critical_threshold=101,
                                                 sort_order=2)
        self.manager = monitor_module.MonitorManager()

    def cycle(self, observed=READY, systemctl=None, observe_error=None):
        written = []
        with mock.patch.object(self.manager, "_write_state", side_effect=written.append), \
                mock.patch.object(self.manager, "_poll_shoutcast_listeners"), \
                mock.patch.object(monitor_module, "create_transmitter_driver", return_value=None), \
                mock.patch.object(probes.subprocess, "run", return_value=systemctl or _systemctl()), \
                mock.patch.object(health, "observe", return_value=observed, side_effect=observe_error), \
                mock.patch.object(monitor_module, "maybe_notify") as notify:
            self.manager._run_cycle()
        results = {result["id"]: result for result in written[0]}
        return results, notify

    def mine(self, notify):
        return [call for call in notify.call_args_list if call.args[0].pk == self.check.pk]

    def events(self):
        return list(SystemEvent.objects.filter(title__startswith="Validation Service").order_by("pk")
                    .values_list("title", flat=True))

    def test_debounce_transition_notification_and_recovery(self):
        missing = {**READY, "ready": False, "reason": health.SOCKET_MISSING}
        results, notify = self.cycle()
        self.assertEqual(results[self.check.id]["status"], "ok")
        self.assertEqual(len(self.mine(notify)), 1)                         # first observation (not an edge)
        self.assertEqual(self.events(), [])

        results, notify = self.cycle(missing)                               # one bad poll: debounced
        self.assertEqual(results[self.check.id]["status"], "ok")
        self.assertEqual(self.mine(notify), [])

        results, notify = self.cycle(missing)                               # second: the edge
        self.assertEqual(results[self.check.id]["status"], "critical")
        self.assertEqual(results[self.check.id]["detail"]["reason"], health.SOCKET_MISSING)
        [call] = self.mine(notify)
        self.assertEqual(call.args[1:4], ("critical", results[self.check.id]["detail"], "ok"))
        self.assertIs(call.args[4], self.manager._cooldowns)              # the existing cooldown ledger
        self.assertEqual(self.events(), ["Validation Service: ok → critical"])

        for _ in range(3):                                                  # still down: no repeat alert
            results, notify = self.cycle(missing)
            self.assertEqual(self.mine(notify), [])
        self.assertEqual(len(self.events()), 1)

        results, notify = self.cycle()                                      # recovery
        self.assertEqual(results[self.check.id]["status"], "ok")
        self.assertEqual(len(self.mine(notify)), 1)
        self.assertEqual(self.events(), ["Validation Service: ok → critical", "Validation Service: critical → ok"])

    def test_a_restart_loop_alerts_through_the_debounce(self):
        self.cycle(systemctl=_systemctl(restarts="1"))
        self.cycle(systemctl=_systemctl("activating", "auto-restart", "2"))
        results, notify = self.cycle(systemctl=_systemctl(restarts="3"))
        self.assertEqual((results[self.check.id]["status"], results[self.check.id]["detail"]["reason"]),
                         ("critical", "restarted"))
        self.assertEqual(len(self.mine(notify)), 1)

    def test_monitoring_failures_stay_contained(self):
        results, _ = self.cycle(observe_error=RuntimeError("observer bug"))
        self.assertEqual(results[self.check.id]["status"], "unknown")
        self.assertIn(self.other.id, results)                               # the rest of the cycle still ran
        with mock.patch.object(probes, "_validation_readiness", side_effect=RuntimeError("probe bug")), \
                mock.patch.dict(probes.APPLICATION_READINESS, {UNIT: probes._validation_readiness}):
            results, _ = self.cycle()
        self.assertEqual(results[self.check.id]["status"], "unknown")
        self.assertIn(self.other.id, results)


@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class DashboardCardBrowserTests(StaticLiveServerTestCase):
    """The card as an operator sees it: the dashboard's own JS rendering a
    status document (served by a stubbed api/status/), in headless Chromium."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"DJANGO_ALLOW_ASYNC_UNSAFE": "true"})
        patcher.start()
        self.addCleanup(patcher.stop)
        from django.contrib.auth import get_user_model
        from django.test import Client
        user = get_user_model().objects.create_superuser("mon-dashboard", password="x")
        client = Client()
        client.force_login(user)
        self.cookie = client.cookies["sessionid"].value

    def card(self, name, status, detail):
        return {"id": len(name), "name": name, "kind": "systemd", "sort_order": 1, "status": status,
                "detail": detail, "since": None, "show_as_card": True, "tx_ref": None, "systemd_unit": None}

    @staticmethod
    def _cards(page):
        return page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('.mon-card')]
            .filter(card => card.querySelector('.mon-led-caption')).map(card => [
            card.querySelector('.mon-card-name').textContent,
            {caption: card.querySelector('.mon-led-caption').textContent,
             detail: [...card.querySelectorAll('.mon-card-detail')].map(e => e.textContent),
             restart: !!card.querySelector('.mon-restart-btn')}]))""")

    def render(self, checks):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                context = browser.new_context()
                context.add_cookies([{"name": "sessionid", "value": self.cookie, "url": self.live_server_url}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.route("**/monitoring/api/status/**", lambda route: route.fulfill(
                    status=200, content_type="application/json",
                    body=json.dumps({"checks": checks, "timestamp": time.time(), "stale": False})))
                page.goto(f"{self.live_server_url}/monitoring/")
                page.wait_for_selector(".mon-card .mon-led-caption")
                cards = self._cards(page)
                self.assertEqual(errors, [])
                return cards
            finally:
                browser.close()
                from django.db import connections
                connections.close_all()

    def test_validation_card_states(self):
        base = {"active_state": "active", "sub_state": "running", "restart_via_dashboard": False}
        cards = self.render([
            self.card("Validation OK", "ok", {**base, "max_active": 2, "max_pending": 4, "at_capacity": False}),
            self.card("Validation Busy", "ok", {**base, "max_active": 1, "max_pending": 0, "at_capacity": True}),
            self.card("Validation Down", "critical", {**base, "reason": "not_listening", "max_active": None,
                                                      "max_pending": None}),
            self.card("Validation Loop", "critical", {**base, "active_state": "activating", "reason": "restarting"}),
            self.card("Validation Status", "warning", {**base, "reason": "status_malformed"}),
            self.card("Engine Service", "ok", {"active_state": "active", "sub_state": "running"}),
        ])
        self.assertEqual(cards["Validation OK"], {"caption": "Running", "detail": ["2 at once · 4 waiting"],
                                                  "restart": False})
        self.assertEqual(cards["Validation Busy"]["caption"], "Running · busy")
        self.assertEqual(cards["Validation Down"], {"caption": "Not listening", "detail": [], "restart": False})
        self.assertEqual(cards["Validation Loop"]["caption"], "Restarting")
        self.assertEqual(cards["Validation Status"]["caption"], "Running · bad status")
        self.assertEqual(cards["Engine Service"], {"caption": "Running", "detail": [], "restart": True})   # unchanged
