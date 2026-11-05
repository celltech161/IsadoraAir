"""Roadmap 3.11, minimal Run Reconciliation: the page's button, its POST endpoint and the launcher.

The scanner itself is covered by test_library_reconciliation_b; these tests cover only what the launcher adds:
who may start a scan, CSRF, the fixed command, a refused or failed launch, simultaneous requests, the scan
lock shared with the command line, the two-second confirmation, the page's status logic (including "Start not
confirmed"), a completed scan, an interrupted one, the GNU timeout limit reaching the scan's ffprobe, and that the
previous report is kept. Real child processes run against the runner's isolated roots and test database; time
limits are shortened for them (seconds, not hours), and nothing touches station media."""
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from library.services import library_reconciliation as reconciliation
from library.services import library_reconciliation_launch as launcher
from library.tests.test_library_reconciliation_b import _ReconFixture

User = get_user_model()
NOT_CONFIRMED = "Start not confirmed. No scan activity or result was detected. Check status before trying again."
UNKNOWN_OUTCOME = ("No answer from the server, so whether the scan started is not known. The scan status here "
                   "updates automatically; check it before trying again.")
RUN = "library:api-reconciliation-run"
TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "library" / "reconciliation.html"

# A stand-in scan: takes the REAL scan lock (or exits 1, as reconcile_library does when it is busy), writes the
# progress file the scanner writes, then holds or exits. Used where timing must be deterministic. "decode" runs
# the scanner's real audio_identity.compute() (which starts ffprobe as an ordinary child); "stubborn" does the
# same but ignores SIGTERM (its ffprobe inherits that); "slow" is still starting when its time limit ends it.
STAND_IN = r'''
import fcntl, json, os, signal, sys, time
reports, mode = sys.argv[1], sys.argv[2]
if mode == "slow":
    time.sleep(60)
d = os.path.join(reports, "library-reconciliation")
os.makedirs(d, exist_ok=True)
fd = os.open(os.path.join(d, ".scan.lock"), os.O_RDWR | os.O_CREAT, 0o640)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    print("another library reconciliation scan is running", file=sys.stderr)
    sys.exit(1)
tmp = os.path.join(d, ".running.json.tmp")
with open(tmp, "w") as handle:
    json.dump({"pid": os.getpid(), "started": time.time(), "phase": "walk", "done": 0, "heartbeat": time.time()}, handle)
os.replace(tmp, os.path.join(d, "running.json"))
if mode == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode in ("decode", "stubborn"):
    sys.path.insert(0, sys.argv[3])
    from library.services import audio_identity
    audio_identity.compute(sys.argv[4])
while mode == "hold":
    time.sleep(0.5)
'''


def wait_until(predicate, timeout=30.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def alive(pid) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in ("Z", "X")


# The whole page script, run under Node against a minimal fake DOM, with every fetch a promise the test resolves
# (in any order), timers that fire only when the test says, and a fixed clock: out-of-order answers made exact.
PAGE_HARNESS = r"""
class El {
  constructor(tag) { Object.assign(this, { tag, children: [], textContent: '', className: '', disabled: false,
    hidden: false, value: '', checked: false, dataset: {}, listeners: {}, attrs: {} }); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  setAttribute(k, v) { this.attrs[k] = v; }
  scrollIntoView() {}
  get text() { return [this.textContent, ...this.children.map(c => typeof c === 'string' ? c : c.text)].join(' ').trim(); }
}
const ids = {};
globalThis.document = {
  cookie: 'csrftoken=t',
  getElementById: id => (ids[id] = ids[id] || new El('div')),
  createElement: tag => new El(tag),
  querySelectorAll: () => [],
};
let NOW = 1000000;
Date.now = () => NOW;
const timers = new Map(); let timerId = 0;
globalThis.setTimeout = (fn, ms) => { timers.set(++timerId, { fn, ms }); return timerId; };
globalThis.clearTimeout = id => { timers.delete(id); };
const requests = [];
globalThis.fetch = (url, opts = {}) => new Promise((resolve, reject) =>
  requests.push({ url, method: opts.method || 'GET', resolve, reject, done: false }));
const SUMMARY = '/api/tracks/reconciliation/', RUN = '/api/tracks/reconciliation/run/';
const isFindings = r => r.url.startsWith('/api/tracks/reconciliation/findings/');
function open(pred) { return requests.filter(r => !r.done && pred(r)); }
function respond(req, status, body) { req.done = true; req.resolve({ ok: status >= 200 && status < 300, status, json: async () => body }); }
function fail(req) { req.done = true; req.reject(new TypeError('network')); }
async function flush() { for (let i = 0; i < 30; i++) await new Promise(r => setImmediate(r)); }
async function fire() { const due = [...timers.entries()]; timers.clear(); due.forEach(([, t]) => t.fn()); await flush(); }
const report = id => ({ scan_id: id, finished_at: '2026-11-05T10:00:00Z', library_root: '/srv/library', status: 'complete',
  counts: { tracks_total: 1, healthy: 1, files_total: 1, relink_reviewable: 0, duplicate_groups_open: 0 }, errors: [] });
const status = (attempt, latest, running = null) => ({ status: { last_attempt: attempt, latest, running },
  report: latest ? report(latest.scan_id) : null });
const A = { scan_id: 'A', status: 'complete' }, B = { scan_id: 'B', status: 'complete' }, F = { scan_id: 'F', status: 'failed' };
const findings = total => ({ page: 1, pages: 1, total, items: [], scan_id: 'x' });
function snapshot() {
  return { summary: ids.scanStatus.text, message: ids.runStatus.textContent, cls: ids.runStatus.className,
    button: ids.runBtn.disabled, scanId, launch: launch === null ? null : JSON.stringify(launch), lastStatus:
    typeof lastStatus === 'undefined' ? null : JSON.stringify(lastStatus), findings: ids.pageInfo.textContent, timers: [...timers.values()].map(t => t.ms),
    open: open(() => true).map(r => r.method + ' ' + r.url.split('?')[0]) };
}
async function clickStart() {
  ids.runBtn.listeners.click[0]();
  const start = ids.actionPanel.children.find(c => c.textContent === 'Start read-only scan');
  start.listeners.click[0]();
  await flush();
}
async function boot() {
  await flush();
  respond(open(r => r.url === SUMMARY)[0], 200, status(A, A));
  respond(open(isFindings)[0], 200, findings(111));
  await flush();
}
"""


class FakeProc:
    """A launched process that never ran a scan: ``exit_code`` None keeps it 'running' (not started)."""

    def __init__(self, exit_code=None, on_poll=None):
        self.pid, self.exit_code, self.on_poll = 2 ** 22 + 11, exit_code, on_poll

    def poll(self):
        if self.on_poll:
            self.on_poll(self)
        return self.exit_code


class _Http(_ReconFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.calls = []

    def fake_popen(self, proc=None, raises=None):
        def popen(argv, **kwargs):
            self.calls.append((argv, kwargs))
            if raises:
                raise raises
            return proc or FakeProc()
        return mock.patch.object(launcher.subprocess, "Popen", side_effect=popen)

    def post(self, client=None, **kwargs):
        return (client or self.client).post(reverse(RUN), **kwargs)

    def user_in(self, group_name, *capabilities, library_paths=False):
        from authz.models import Capability, GroupRole, Role, RoleCapability
        from library.models import GroupAccess
        user = User.objects.create_user(f"u-{group_name}", f"{group_name}@example.invalid", "pw")
        group, _ = Group.objects.get_or_create(name=group_name)
        user.groups.add(group)
        if library_paths:
            GroupAccess.objects.create(group=group, allowed_prefixes="/library/\n/api/tracks/")
        if capabilities:
            role = Role.objects.create(name=f"role-{group_name}")
            for slug in capabilities:
                capability, _ = Capability.objects.get_or_create(slug=slug, defaults={"label": slug})
                RoleCapability.objects.create(role=role, capability=capability)
            GroupRole.objects.create(group=group, role=role)
        return user


class AccessTests(_Http):
    def test_only_track_managers_start_a_scan(self):
        with self.fake_popen(FakeProc(exit_code=0)):
            self.client.logout()
            self.assertIn(self.post().status_code, (302, 401, 403))
            for user in (self.user_in("Contributor"), self.user_in("No Capability Group"),
                         self.user_in("Library Viewers", "library.view", library_paths=True)):
                with self.subTest(user=user.username):
                    self.client.force_login(user)
                    self.assertIn(self.post().status_code, (302, 403))
            self.assertEqual(self.calls, [])                       # nothing was started for any of them
            manager = self.user_in("Library Managers", "library.manage_tracks", library_paths=True)  # not staff
            self.client.force_login(manager)
            self.assertNotIn(self.post().status_code, (302, 401, 403))
        self.assertEqual(len(self.calls), 1)

    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        with self.fake_popen(FakeProc(exit_code=0)):
            self.assertEqual(self.post(client).status_code, 403)                     # no token
            self.assertEqual(self.post(client, HTTP_X_CSRFTOKEN="wrong").status_code, 403)
            self.assertEqual(self.calls, [])
            client.get(reverse("library:library-reconciliation"))                  # the page sets the cookie
            token = client.cookies["csrftoken"].value
            self.assertNotEqual(self.post(client, HTTP_X_CSRFTOKEN=token).status_code, 403)
        self.assertEqual(len(self.calls), 1)

    def test_only_post_is_allowed(self):
        with self.fake_popen():
            for method in ("get", "put", "delete"):
                self.assertEqual(getattr(self.client, method)(reverse(RUN)).status_code, 405)
        self.assertEqual(self.calls, [])


class LaunchTests(_Http):
    def test_the_command_and_environment_are_fixed_and_the_request_is_ignored(self):
        with self.fake_popen(FakeProc(exit_code=0)):
            self.client.post(reverse(RUN) + "?max_decodes=0&library_root=/etc",
                             data=json.dumps({"args": ["--max-hash-gb", "999"], "path": "/", "nice": -20}),
                             content_type="application/json", HTTP_X_FORWARDED_FOR="10.0.0.1")
        (argv, kwargs), = self.calls
        manage_py = str(Path(settings.BASE_DIR) / "manage.py")
        self.assertEqual(argv, ["/usr/bin/nice", "-n", "10", "/usr/bin/ionice", "-c", "3",
                                "/usr/bin/gnutimeout", "--signal=KILL", "2h",
                                sys.executable, manage_py, "reconcile_library"])
        self.assertEqual(argv, launcher.scan_command())
        self.assertNotIn("shell", kwargs)                          # an argv list, never a shell string
        self.assertEqual(kwargs["cwd"], str(settings.BASE_DIR))
        self.assertTrue(kwargs["start_new_session"])               # not in the request worker's process group
        self.assertEqual(kwargs["env"], dict(os.environ))          # the server's own environment, nothing added
        self.assertEqual((kwargs["stdin"], kwargs["stdout"]), (subprocess.DEVNULL, subprocess.DEVNULL))

    def test_the_time_limit_is_gnu_timeout(self):
        """GNU's KILL escalation reaches the scan's whole process group; uutils' timeout (Ubuntu's
        /usr/bin/timeout since 25.10) sends it only to the direct child."""
        version = subprocess.run([launcher.TIMEOUT, "--version"], capture_output=True, text=True, timeout=10).stdout
        self.assertIn("GNU coreutils", version)

    def test_confirmation_waits_two_seconds_at_most_then_answers_starting(self):
        """A deterministic clock: the request watches its process for two seconds, then answers "starting"
        (never "complete"), and never waits for the scan itself."""
        self.assertEqual(launcher.LAUNCH_WAIT_SECONDS, 2.0)
        now, polls = [1000.0], []

        def sleep(seconds):
            now[0] += seconds
        with self.fake_popen(FakeProc(on_poll=lambda proc: polls.append(now[0] - 1000.0))):
            result = launcher.launch(clock=lambda: now[0], sleep=sleep)
        self.assertEqual(result["state"], "starting")
        self.assertGreaterEqual(now[0] - 1000.0, 2.0)
        self.assertLess(now[0] - 1000.0, 2.0 + launcher.POLL_SECONDS + 1e-6)
        self.assertLess(max(polls), 2.0 + launcher.POLL_SECONDS)

    def test_an_existing_report_and_progress_file_are_not_taken_for_a_new_scan(self):
        """The last report and a stale progress file (an earlier interrupted scan) are recorded as "before";
        a process that has not scanned yet is "starting", not "finished" or "running"."""
        _previous, _pointer = self.publish()
        before = reconciliation.scan_status()["last_attempt"]
        stale = {"pid": os.getpid(), "started": 12345.5, "phase": "walk", "done": 0, "heartbeat": 12345.5}
        (self.reports / "library-reconciliation" / "running.json").write_text(json.dumps(stale))
        with self.fake_popen(FakeProc()), mock.patch.object(launcher, "LAUNCH_WAIT_SECONDS", 0.3):
            body = self.post().json()
        self.assertEqual(body["state"], "starting")
        self.assertEqual(body["previous_attempt"], before)
        self.assertEqual(body["previous_running"], {"pid": os.getpid(), "started": 12345.5})

    def test_a_launch_that_fails_is_reported_and_starts_nothing(self):
        _previous, pointer = self.publish()
        with self.fake_popen(raises=OSError(2, "No such file or directory")):
            response = self.post()
        self.assertEqual((response.status_code, response.json()["code"]), (500, "launch_failed"))
        with self.fake_popen(FakeProc(exit_code=1)):              # exits before scanning, lock free
            response = self.post()
        self.assertEqual((response.status_code, response.json()["code"]), (500, "launch_failed"))
        self.assertIn("exit status 1", response.json()["error"])
        self.assertEqual((self.reports / "library-reconciliation" / "latest.json").read_bytes(), pointer)

    def test_a_scan_that_already_finished_is_reported_as_finished_not_assumed(self):
        def finish(proc):
            if proc.exit_code is None:
                reconciliation.run_scan()                           # the scan ran and published its attempt
                proc.exit_code = 0
        with self.fake_popen(FakeProc(on_poll=finish)):
            body = self.post().json()
        self.assertEqual(body["state"], "finished")
        self.assertEqual(body["attempt"]["status"], "complete")
        self.assertIsNone(body["previous_attempt"])
        self.assertIsNone(body["previous_running"])

    def test_a_process_not_yet_confirmed_is_reported_as_starting(self):
        with self.fake_popen(FakeProc()), mock.patch.object(launcher, "LAUNCH_WAIT_SECONDS", 0.3):
            response = self.post()
        self.assertEqual((response.status_code, response.json()["state"]), (202, "starting"))   # never "complete"

    def test_a_scan_holding_the_lock_refuses_the_request_and_starts_nothing(self):
        with self.fake_popen(), reconciliation.scan_lock():          # e.g. a command-line scan
            response = self.post()
        self.assertEqual((response.status_code, response.json()["code"]), (409, "scan_running"))
        self.assertEqual(self.calls, [])

    def test_a_request_while_another_is_starting_is_refused(self):
        with self.fake_popen(), launcher._launch_lock():
            response = self.post()
        self.assertEqual((response.status_code, response.json()["code"]), (409, "scan_starting"))
        self.assertEqual(self.calls, [])

    def publish(self):
        self.track("ROCK/earlier.flac")
        report = reconciliation.run_scan()
        return report["scan_id"], (self.reports / "library-reconciliation" / "latest.json").read_bytes()


class PageTests(_Http):
    def test_the_page_offers_the_button_and_the_endpoint(self):
        body = self.client.get(reverse("library:library-reconciliation")).content.decode()
        self.assertIn('id="runBtn"', body)
        self.assertIn("/api/tracks/reconciliation/run/", body)
        self.assertIn("csrftoken", self.client.cookies)
        self.assertNotIn("Run one with: manage.py reconcile_library", body)

    def node(self):
        node = shutil.which("node")
        if node is None:
            try:
                import playwright
                candidate = Path(playwright.__file__).parent / "driver" / "node"
                node = str(candidate) if candidate.exists() else None
            except ImportError:
                node = None
        if node is None:
            self.skipTest("no Node.js available to run the page script")
        return node

    def run_page_script(self, body):
        source = TEMPLATE.read_text()
        code = source[source.index("// runView:begin"):source.index("// runView:end")]
        result = subprocess.run([self.node(), "-e", code + "\n" + body], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def run_page(self, scenario):
        source = TEMPLATE.read_text()
        page = source[source.index("<script>") + len("<script>"):source.index("</script>")]
        body = PAGE_HARNESS + page + "\n(async () => {\n" + scenario + "\n})().catch(e => { console.error(e); process.exit(1); });\n"
        result = subprocess.run([self.node(), "-e", body], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_an_older_status_answer_never_overwrites_a_newer_one(self):
        """Codex Medium: (1) a poll's summary request is in flight; (2) the launch starts, invalidating it and
        pausing polling; (3) the newer state (a completed, or a failed, scan) is shown; (4) the older answer
        arrives afterwards; (5) nothing shown or held regresses. An older findings answer is ignored likewise."""
        for newer in ("complete", "failed"):
            with self.subTest(newer=newer):
                out = self.run_page(r"""
  await boot();
  await fire();                                              // a poll: its summary request stays in flight
  const older = open(r => r.url === SUMMARY)[0];
  ids.openOnly.listeners.change[0]();                        // and an older findings request (a filter change)
  const olderFindings = open(isFindings)[0];
  await clickStart();
  const duringPost = snapshot();
  refreshRun();                                              // a poll that slipped in during the POST is a no-op
  await flush();
  const afterSlip = snapshot();
  const NEWER = '""" + newer + r"""';
  respond(open(r => r.url === RUN)[0], 202, { state: 'finished', pid: 5, previous_running: null,
    previous_attempt: { scan_id: 'A', status: 'complete' }, attempt: NEWER === 'complete' ? B : F });
  await flush();
  respond(open(r => r.url === SUMMARY && r !== older)[0], 200, NEWER === 'complete' ? status(B, B) : status(F, A));
  await flush();
  const newerFindings = open(isFindings).filter(r => r !== olderFindings);
  newerFindings.forEach(r => respond(r, 200, findings(222)));
  if (!newerFindings.length) respond(olderFindings, 200, findings(111));   // no new report: it is the newest
  await flush();
  const before = snapshot();
  respond(older, 200, status(A, A, { alive: true, pid: 3, started: 1.5, phase: 'walk', done: 0 }));
  if (newerFindings.length) respond(olderFindings, 200, findings(111));
  await flush();
  const after = snapshot();
  process.stdout.write(JSON.stringify({ duringPost, afterSlip, newerFindings: newerFindings.length, before, after }));
""")
                before, after = out["before"], out["after"]
                self.assertEqual(after, dict(before, open=after["open"]))   # nothing shown or held regressed
                during = out["duringPost"]
                self.assertEqual(during["timers"], [])                       # polling paused during the POST
                self.assertTrue(during["button"])
                self.assertEqual(sorted(during["open"]), sorted([f"GET {s}" for s in (
                    "/api/tracks/reconciliation/", "/api/tracks/reconciliation/findings/")]
                    + ["POST /api/tracks/reconciliation/run/"]))
                self.assertEqual(out["afterSlip"]["open"], during["open"])   # no new status request
                self.assertEqual(after["open"], [])
                self.assertIsNone(after["launch"])
                self.assertFalse(after["button"])
                self.assertEqual(len(after["timers"]), 1)                   # exactly one poll scheduled
                if newer == "complete":
                    self.assertEqual(after["scanId"], "B")
                    self.assertIn("Latest completed scan B", after["summary"])
                    self.assertIn("Scan B finished (complete)", after["message"])
                    self.assertEqual(out["newerFindings"], 1)
                    self.assertIn("222 findings", after["findings"])
                else:
                    self.assertEqual(after["scanId"], "A")
                    self.assertIn("(F) did not complete: failed", after["summary"])
                    self.assertIn("Scan F failed", after["message"])
                self.assertNotIn("A scan is running", after["summary"] + after["message"])

    def test_polling_resumes_after_a_refused_launch_and_after_an_unknown_outcome(self):
        out = self.run_page(r"""
  const result = {};
  // A refusal (409 with the launcher's code): "Not started", polling resumes.
  await boot();
  await clickStart();
  result.pausedRefused = [...timers.values()].length;
  respond(open(r => r.url === RUN)[0], 409, { code: 'scan_running', error: 'A reconciliation scan is already running.' });
  await flush();
  respond(open(r => r.url === SUMMARY)[0], 200, status(A, A));
  await flush();
  result.refused = snapshot();
  await fire();
  result.refusedPolls = open(r => r.url === SUMMARY).length;
  respond(open(r => r.url === SUMMARY)[0], 200, status(A, A));
  await flush();
  // No answer (the connection failed): not "Not started"; watched from the status shown before; polling resumes.
  await clickStart();
  fail(open(r => r.url === RUN)[0]);
  await flush();
  respond(open(r => r.url === SUMMARY)[0], 200, status(A, A));
  await flush();
  result.unknown = snapshot();
  NOW += 31000;
  await fire();
  respond(open(r => r.url === SUMMARY)[0], 200, status(A, A));
  await flush();
  result.unknownLater = snapshot();
  await fire();
  respond(open(r => r.url === SUMMARY)[0], 200, status(B, B));
  await flush();
  result.unknownLate = snapshot();
  // A proxy error without the launcher's code (502) is just as unknown.
  open(isFindings).forEach(r => respond(r, 200, findings(1)));
  await flush();
  NOW += 60000;
  await clickStart();
  respond(open(r => r.url === RUN)[0], 502, {});
  await flush();
  respond(open(r => r.url === SUMMARY)[0], 200, status(B, B));
  await flush();
  result.gateway = snapshot();
  process.stdout.write(JSON.stringify(result));
""")
        self.assertEqual(out["pausedRefused"], 0)
        refused = out["refused"]
        self.assertIn("Not started: A reconciliation scan is already running.", refused["message"])
        self.assertIsNone(refused["launch"])
        self.assertEqual((refused["button"], refused["timers"]), (False, [15000]))
        self.assertEqual(out["refusedPolls"], 1)                             # the scheduled poll ran
        unknown = out["unknown"]
        self.assertNotIn("Not started", unknown["message"])
        self.assertEqual(unknown["message"], UNKNOWN_OUTCOME)
        self.assertEqual((unknown["button"], unknown["timers"]), (True, [2000]))
        self.assertIn('"previous":"A"', unknown["launch"])
        later = out["unknownLater"]
        self.assertEqual(later["message"], NOT_CONFIRMED)                    # the 30-second rule still applies
        self.assertEqual((later["button"], later["timers"]), (False, [15000]))
        late = out["unknownLate"]
        self.assertIn("Scan B finished (complete)", late["message"])        # a late result is still recognised
        gateway = out["gateway"]
        self.assertEqual(gateway["message"], UNKNOWN_OUTCOME)
        self.assertIn('"previous":"B"', gateway["launch"])
        self.assertEqual(gateway["timers"], [2000])

    def test_an_unknown_launch_answer_never_overwrites_a_definitive_refresh(self):
        """A network rejection or unstructured proxy error is older than the status refresh it triggers. If that
        refresh proves completion or failure, its terminal result wins; the unknown-outcome fallback is used only
        when the refresh still shows exactly the pre-launch state."""
        for outcome in ("network_then_complete", "proxy_then_failed", "network_without_new_evidence"):
            with self.subTest(outcome=outcome):
                out = self.run_page(r"""
  await boot();
  await clickStart();
  const OUTCOME = '""" + outcome + r"""';
  const post = open(r => r.url === RUN)[0];
  if (OUTCOME.startsWith('network')) fail(post); else respond(post, 502, {});
  await flush();
  const refreshed = open(r => r.url === SUMMARY)[0];
  respond(refreshed, 200, OUTCOME === 'network_then_complete' ? status(B, B)
                        : OUTCOME === 'proxy_then_failed' ? status(F, A) : status(A, A));
  await flush();
  open(isFindings).forEach(r => respond(r, 200, findings(222)));
  await flush();
  process.stdout.write(JSON.stringify(snapshot()));
""")
                if outcome == "network_then_complete":
                    self.assertIn("Scan B finished (complete)", out["message"])
                    self.assertIn("Latest completed scan B", out["summary"])
                    self.assertIsNone(out["launch"])
                elif outcome == "proxy_then_failed":
                    self.assertIn("Scan F failed", out["message"])
                    self.assertIn("(F) did not complete: failed", out["summary"])
                    self.assertIsNone(out["launch"])
                else:
                    self.assertEqual(out["message"], UNKNOWN_OUTCOME)
                    self.assertIn('"previous":"A"', out["launch"])
                if outcome != "network_without_new_evidence":
                    self.assertNotIn("whether the scan started is not known", out["message"])
                self.assertEqual(len(out["timers"]), 1)

    def test_the_status_logic_under_node(self):
        """The page's own runView(), extracted from the template and run under Node: the button is disabled
        while a scan runs or starts, a launch is never shown as complete until its new attempt appears, an older
        report or progress file is never taken for its own, and a failure or interruption says that nothing was
        replaced."""
        A = {"scan_id": "A", "status": "complete"}
        launch = {"previous": "A", "previousRunning": "3:100.5", "seen": 0, "confirmed": False}
        cases = {
            "unknown status": [None, None, 0],
            "idle": [{"running": None, "last_attempt": A}, None, 0],
            "cli scan running": [{"running": {"alive": True, "pid": 5, "phase": "walk", "done": 0}}, None, 0],
            "own scan running": [{"running": {"alive": True, "pid": 7, "started": 200.0, "phase": "duplicates",
                                              "done": 3}, "last_attempt": A}, launch, 1000],
            "starting": [{"running": None, "last_attempt": A}, launch, 1000],
            "finished complete": [{"running": None, "last_attempt": {"scan_id": "B", "status": "complete"}},
                                  launch, 1000],
            "finished failed": [{"running": None, "last_attempt": {"scan_id": "B", "status": "failed"}},
                                launch, 1000],
            "interrupted (new progress file)": [{"running": {"alive": False, "pid": 7, "started": 200.0,
                                                             "phase": "walk", "done": 0}, "last_attempt": A},
                                                launch, 1000],
            "stale file of an older scan": [{"running": {"alive": False, "pid": 3, "started": 100.5,
                                                         "phase": "walk", "done": 0}, "last_attempt": A},
                                            launch, 1000],
            "stale file and old report after 30 s": [{"running": {"alive": False, "pid": 3, "started": 100.5,
                                                                   "phase": "walk", "done": 0}, "last_attempt": A},
                                                      launch, 31000],
            "first ever scan finished": [{"running": None, "last_attempt": {"scan_id": "B", "status": "suspect"}},
                                         dict(launch, previous=None, previousRunning=None), 1000],
        }
        views = self.run_page_script(
            "const cases = " + json.dumps(cases) + ";\nconst out = {};\n"
            "for (const [k, a] of Object.entries(cases)) out[k] = runView(a[0], a[1], a[2]);\n"
            "process.stdout.write(JSON.stringify(out));\n")

        def shape(name):
            v = views[name]
            return (v["busy"], v["done"], bool(v.get("refresh")))
        self.assertEqual(shape("unknown status"), (True, False, False))
        self.assertEqual(shape("idle"), (False, False, False))
        self.assertEqual(shape("cli scan running"), (True, False, False))
        self.assertEqual(shape("own scan running"), (True, False, False))
        self.assertIn("duplicates, 3 done", views["own scan running"]["text"])
        self.assertEqual(shape("starting"), (True, False, False))
        self.assertEqual(views["starting"]["text"], "Starting the scan…")
        self.assertEqual(shape("finished complete"), (False, True, True))
        self.assertIn("Scan B finished (complete)", views["finished complete"]["text"])
        self.assertEqual(shape("finished failed"), (False, True, False))
        self.assertIn("Nothing was replaced", views["finished failed"]["text"])
        self.assertEqual(shape("interrupted (new progress file)"), (False, True, False))
        self.assertIn("stopped before it finished", views["interrupted (new progress file)"]["text"])
        self.assertIn("two-hour limit", views["interrupted (new progress file)"]["text"])
        self.assertEqual(shape("stale file of an older scan"), (True, False, False))   # still waiting for its own
        self.assertEqual(shape("stale file and old report after 30 s"), (False, False, False))
        self.assertEqual(views["stale file and old report after 30 s"]["text"], NOT_CONFIRMED)
        self.assertEqual(shape("first ever scan finished"), (False, True, True))

    def test_a_launch_answered_starting_is_followed_through_the_status_api(self):
        """Timelines with a deterministic clock (ms since the 202 "starting"), each step applying the page's
        observe() and runView() to one status answer, as refreshRun() does."""
        A = {"scan_id": "A", "status": "complete"}
        stale = {"alive": False, "pid": 3, "started": 100.5, "phase": "walk", "done": 0}
        new = {"pid": 9, "started": 500.25, "phase": "walk", "done": 0}
        timelines = {
            "to running then complete": [
                [{"running": stale, "last_attempt": A}, 1000],
                [{"running": dict(new, alive=True), "last_attempt": A}, 6000],
                [{"running": dict(new, alive=True, phase="hash", done=2000), "last_attempt": A}, 40000],
                [{"running": None, "last_attempt": A}, 42000],          # between its last progress and its report
                [{"running": None, "last_attempt": {"scan_id": "B", "status": "complete"}}, 44000]],
            "directly to a completed result": [
                [{"running": stale, "last_attempt": A}, 2000],
                [{"running": None, "last_attempt": {"scan_id": "B", "status": "suspect"}}, 4000]],
            "no evidence": [
                [{"running": stale, "last_attempt": A}, 2000],
                [{"running": stale, "last_attempt": A}, 29999],
                [{"running": stale, "last_attempt": A}, 30000],
                [{"running": stale, "last_attempt": A}, 45000]],
            "no evidence, then a late result": [
                [{"running": None, "last_attempt": A}, 31000],
                [{"running": None, "last_attempt": {"scan_id": "C", "status": "failed"}}, 46000]],
            "seen running, then nothing": [
                [{"running": dict(new, alive=True), "last_attempt": A}, 1000],
                [{"running": None, "last_attempt": A}, 31001]],
            "running, then stopped by its time limit": [
                [{"running": dict(new, alive=True), "last_attempt": A}, 1000],
                [{"running": dict(new, alive=False), "last_attempt": A}, 7200000]],
        }
        views = self.run_page_script(
            "const timelines = " + json.dumps(timelines) + ";\nconst out = {};\n"
            "for (const [k, steps] of Object.entries(timelines)) {\n"
            "  const launch = { previous: 'A', previousRunning: runKey({pid: 3, started: 100.5}), seen: 0,"
            " confirmed: false };\n"
            "  out[k] = steps.map(([st, now]) => { observe(st, launch, now); return runView(st, launch, now); });\n"
            "}\nprocess.stdout.write(JSON.stringify(out));\n")

        def shapes(name):
            return [(v["busy"], v["done"], v["text"]) for v in views[name]]
        running = shapes("to running then complete")
        self.assertEqual(running[0], (True, False, "Starting the scan…"))
        self.assertEqual(running[1][:2], (True, False))
        self.assertIn("A scan is running (walk, 0 done)", running[1][2])
        self.assertIn("hash, 2000 done", running[2][2])                  # past 30 s: running is not "not confirmed"
        self.assertEqual(running[3], (True, False, "Waiting for the scan’s result…"))
        self.assertEqual(running[4][:2], (False, True))
        self.assertIn("Scan B finished (complete)", running[4][2])
        direct = shapes("directly to a completed result")
        self.assertEqual(direct[0][:2], (True, False))
        self.assertEqual(direct[1][:2], (False, True))
        self.assertIn("Scan B finished (suspect)", direct[1][2])
        none = shapes("no evidence")
        self.assertEqual([v[:2] for v in none], [(True, False), (True, False), (False, False), (False, False)])
        self.assertEqual(none[1][2], "Starting the scan…")
        self.assertEqual(none[2][2], NOT_CONFIRMED)
        self.assertEqual(none[3][2], NOT_CONFIRMED)
        for _busy, _done, text in none:
            self.assertNotIn("fail", text.lower())                     # missing confirmation is not a failure
        late = shapes("no evidence, then a late result")
        self.assertEqual(late[0][2], NOT_CONFIRMED)
        self.assertEqual(late[1][:2], (False, True))
        self.assertIn("Scan C failed", late[1][2])
        vanished = shapes("seen running, then nothing")
        self.assertEqual(vanished[1][:2], (False, False))
        self.assertNotEqual(vanished[1][2], NOT_CONFIRMED)
        self.assertIn("no new result was detected", vanished[1][2])
        limit = shapes("running, then stopped by its time limit")
        self.assertEqual(limit[1][:2], (False, True))
        self.assertIn("stopped before it finished", limit[1][2])
        self.assertNotRegex(limit[1][2], r"Scan \S+ finished")          # never presented as a completed scan


class _Live(_ReconFixture, TransactionTestCase):
    """Real child processes: the runner gives them its isolated roots (whatever their environment says), so
    these tests use exactly those directories, emptied before and after, and the test database."""

    def setUp(self):
        super().setUp()
        state = settings.ISADORAAIR_TEST_ISOLATION
        namespace, roots = Path(state["namespace"]).resolve(), state["roots"]
        live = {name: Path(roots[name]).resolve() for name in ("LIBRARY_ROOT", "WAVEFORMS_DIR", "REPORTS_ROOT")}
        for path in live.values():
            self.assertTrue(str(path).startswith(str(namespace) + os.sep), path)
        override = override_settings(**{name: str(path) for name, path in live.items()})
        override.enable()
        self.addCleanup(override.disable)
        self.root, self.waves, self.reports = live["LIBRARY_ROOT"], live["WAVEFORMS_DIR"], live["REPORTS_ROOT"]

        def empty():
            for path in live.values():
                if path.exists():
                    path.chmod(0o755)
                    shutil.rmtree(path)
                path.mkdir(parents=True)
        empty()
        self.addCleanup(empty)
        self.directory = reconciliation.report_dir()
        env = mock.patch.dict(os.environ, {"DB_NAME": connection.settings_dict["NAME"]})
        env.start()
        self.addCleanup(env.stop)
        self.pids = []
        self.addCleanup(self.kill_all)

    def kill_all(self):
        for pid in self.pids:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass

    def stand_in(self, mode="hold", *extra):
        real = launcher.scan_command                                # the real nice/ionice/timeout prefix,
        return mock.patch.object(launcher, "scan_command", lambda: real()[:-3] + [   # read at launch time
            sys.executable, "-I", "-c", STAND_IN, str(self.reports), mode, *map(str, extra)])

    def launch(self, wait=10.0):
        """launcher.launch() with the real Popen. The two-second default is tested with a deterministic clock;
        here a longer confirmation wait keeps a loaded test machine from turning "running" into "starting"."""
        procs = []

        def popen(*args, **kwargs):
            procs.append(subprocess.Popen(*args, **kwargs))
            return procs[-1]
        try:
            result = launcher.launch(username="test", wait=wait, popen=popen)
        except launcher.LaunchRefused as exc:
            return exc.status, {"code": exc.code}
        finally:
            self.pids.extend(proc.pid for proc in procs)
        self.proc = procs[0]
        return 202, result

    def fake_ffprobe(self, *, ignore_term=False):
        """An ``ffprobe`` first on PATH that records its pid and hangs (as on storage that stopped answering)."""
        tools, record = self.base / "fake-tools", self.base / "fake-tools.pids"
        tools.mkdir(exist_ok=True)
        probe = tools / "ffprobe"
        probe.write_text("#!/bin/sh\n" + ("trap '' TERM\n" if ignore_term else "")
                         + f"echo $$ >> '{record}'\nexec sleep 600\n")
        probe.chmod(0o755)
        self.addCleanup(shutil.rmtree, tools, True)
        self.addCleanup(lambda: record.unlink(missing_ok=True))
        path = mock.patch.dict(os.environ, {"PATH": f"{tools}{os.pathsep}{os.environ.get('PATH', '')}"})
        path.start()
        self.addCleanup(path.stop)
        return lambda: [int(x) for x in record.read_text().split()] if record.exists() else []

    def finished(self, previous):
        status = reconciliation.scan_status()
        attempt = status["last_attempt"]
        return status["running"] is None and attempt is not None and attempt != previous

    def publish_previous(self):
        self.track("ROCK/earlier.flac")
        report = reconciliation.run_scan()
        return report["scan_id"], (self.directory / "latest.json").read_bytes()


class LiveLaunchTests(_Live):
    def test_the_scan_runs_detached_at_low_priority_and_holds_the_shared_lock(self):
        with self.stand_in():
            status, body = self.launch()
        self.assertEqual((status, body["state"]), (202, "running"))
        leader, pid = body["pid"], body["scan_pid"]                 # timeout, and the scan it runs
        self.assertNotEqual(pid, leader)
        self.assertEqual(os.getsid(leader), leader)                 # its own session: a worker's exit is not its end
        self.assertEqual((os.getsid(pid), os.getpgid(pid)), (leader, leader))
        self.assertIn("gnutimeout", Path(f"/proc/{leader}/cmdline").read_bytes().decode().replace("\0", " "))
        nice = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[16])
        self.assertEqual(nice, 10)
        self.assertIn("idle", subprocess.run(["/usr/bin/ionice", "-p", str(pid)], capture_output=True, text=True).stdout)
        st = reconciliation.scan_status()
        self.assertEqual((st["running"]["pid"], st["running"]["alive"]), (pid, True))
        # the command line is refused while it runs (the scanner's own lock), and changes nothing
        with self.assertRaisesMessage(CommandError, "another library reconciliation scan is running"):
            call_command("reconcile_library", stdout=open(os.devnull, "w"))
        self.assertFalse((self.directory / "last_attempt.json").exists())
        # and so is the page, while it runs
        self.assertEqual(self.launch(), (409, {"code": "scan_running"}))

    def test_simultaneous_requests_start_exactly_one_scan(self):
        results, barrier = [], threading.Barrier(4)

        def request():
            barrier.wait()
            try:
                results.append(self.launch())
            finally:
                connection.close()
        with self.stand_in():
            threads = [threading.Thread(target=request) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
        statuses = sorted(status for status, _ in results)
        self.assertEqual(statuses, [202, 409, 409, 409], results)
        self.assertTrue(all(body["code"] in ("scan_starting", "scan_running") for s, body in results if s == 409))
        (body,) = [body for s, body in results if s == 202]
        self.assertTrue(alive(body["scan_pid"]))
        self.assertEqual(reconciliation.scan_status()["running"]["pid"], body["scan_pid"])

    def test_a_command_line_scan_that_takes_the_lock_first_wins_and_the_launch_says_so(self):
        holder = subprocess.Popen([sys.executable, "-I", "-c", STAND_IN, str(self.reports), "hold"],
                                  start_new_session=True)
        self.pids.append(holder.pid)
        original = launcher.reconciliation._lock_holder_alive
        checks = []

        def free_at_first(directory):                             # the CLI takes the lock right after the check
            checks.append(1)
            return False if len(checks) == 1 else original(directory)
        self.assertTrue(wait_until(lambda: original(self.directory), 15))
        with self.stand_in(), mock.patch.object(launcher.reconciliation, "_lock_holder_alive", free_at_first):
            status, body = self.launch()
        self.assertEqual((status, body), (409, {"code": "scan_running"}))
        self.assertEqual(reconciliation.scan_status()["running"]["pid"], holder.pid)

    def test_an_interrupted_scan_is_reported_and_keeps_the_previous_report(self):
        """A Gunicorn restart (systemd stops the whole control group, KillMode=control-group) ends the scan.
        Simulated with the same SIGTERM to its process group: the scan stops, its progress file stays behind
        as 'not alive' (the page says interrupted), the previous report is kept, and a new scan can start."""
        previous, pointer = self.publish_previous()
        with self.stand_in():
            status, body = self.launch()
        self.assertEqual((status, body["state"]), (202, "running"))
        os.killpg(body["pid"], signal.SIGTERM)
        self.proc.wait(30)
        self.assertTrue(wait_until(lambda: not alive(body["scan_pid"]), 10))
        st = reconciliation.scan_status()
        self.assertEqual((st["running"]["pid"], st["running"]["alive"]), (body["scan_pid"], False))
        self.assertEqual(st["latest"]["scan_id"], previous)
        self.assertEqual((self.directory / "latest.json").read_bytes(), pointer)
        summary = self.client.get(reverse("library:api-reconciliation-summary")).json()
        self.assertEqual(summary["report"]["scan_id"], previous)
        with self.stand_in():
            self.assertEqual(self.launch()[0], 202)                # the lock went with the process

    # ---- The time limit (Codex High): KILL to the whole process group, shortened from 2 h to seconds.
    LIMIT = 5

    def run_into_the_limit(self, mode, *, ffprobe_ignores_term):
        """A scan (stand-in, through the scanner's real audio_identity code) whose ffprobe hangs, under the real
        timeout prefix with a LIMIT-second limit. Returns the launch answer, the ffprobe pid, timeout's exit
        status and the seconds from launch until it ended."""
        probes = self.fake_ffprobe(ignore_term=ffprobe_ignores_term)
        sample = self.file("ROCK/sample.flac")
        began = time.monotonic()
        with self.stand_in(mode, settings.BASE_DIR, sample), \
                mock.patch.object(launcher, "SCAN_TIME_LIMIT", f"{self.LIMIT}s"):
            status, body = self.launch()
        self.assertEqual((status, body["state"]), (202, "running"))
        self.assertTrue(wait_until(lambda: probes(), self.LIMIT - 1), "the scan never started ffprobe")
        (probe,) = probes()
        # the limit is reached while the scan and its ffprobe, an ordinary child in its process group, are active
        self.assertEqual(os.getpgid(probe), body["pid"])
        self.assertIsNone(self.proc.poll())
        self.assertTrue(alive(body["scan_pid"]) and alive(probe))
        code = self.proc.wait(60)
        return body, probe, code, time.monotonic() - began

    def assert_ended_by_the_limit(self, body, probe, code, elapsed, previous, pointer, previous_attempt):
        # nothing of the scan is left, allowing only the moment a reparented process takes to be reaped
        self.assertTrue(wait_until(lambda: not alive(body["scan_pid"]) and not alive(probe), 2),
                        f"still alive after the limit: scan {alive(body['scan_pid'])}, ffprobe {alive(probe)}")
        self.assertEqual(code, -signal.SIGKILL)                     # killed with its group: never a success status
        self.assertLess(elapsed, self.LIMIT + 10)                   # at the limit, not after a grace period
        self.assertFalse(reconciliation._lock_holder_alive(self.directory))      # the scan lock is free
        st = reconciliation.scan_status()
        self.assertEqual((st["running"]["pid"], st["running"]["alive"]), (body["scan_pid"], False))  # "stopped"
        self.assertEqual(st["last_attempt"], previous_attempt)      # no attempt recorded, so none shown as complete
        self.assertEqual(st["latest"]["scan_id"], previous)
        self.assertEqual((self.directory / "latest.json").read_bytes(), pointer)
        with self.stand_in():
            self.assertEqual(self.launch()[0], 202)                 # a new scan can start

    def test_the_limit_kills_a_scan_and_its_ffprobe_that_ignores_term(self):
        """The flaw Codex found: with TERM first, the scan exits at once, timeout exits with it (124), and a
        TERM-ignoring ffprobe outlives both. KILL to the group ends them together."""
        previous, pointer = self.publish_previous()
        previous_attempt = reconciliation.scan_status()["last_attempt"]
        result = self.run_into_the_limit("decode", ffprobe_ignores_term=True)
        self.assert_ended_by_the_limit(*result, previous, pointer, previous_attempt)

    def test_the_limit_kills_a_scan_and_its_ffprobe_that_both_ignore_term(self):
        previous, pointer = self.publish_previous()
        previous_attempt = reconciliation.scan_status()["last_attempt"]
        result = self.run_into_the_limit("stubborn", ffprobe_ignores_term=True)
        self.assert_ended_by_the_limit(*result, previous, pointer, previous_attempt)

    def test_a_scan_still_starting_at_the_limit_is_a_failed_launch_not_a_success(self):
        """A limit reached before the scan showed anything: the request reports the killed process as a failed
        launch (its exit status, never "running" or "finished"), and nothing is recorded."""
        previous, pointer = self.publish_previous()
        previous_attempt = reconciliation.scan_status()["last_attempt"]
        with self.stand_in("slow"), mock.patch.object(launcher, "SCAN_TIME_LIMIT", "1s"):
            status, body = self.launch(wait=30.0)
        self.assertEqual((status, body["code"]), (500, "launch_failed"))
        st = reconciliation.scan_status()
        self.assertEqual((st["last_attempt"], st["running"]), (previous_attempt, None))
        self.assertEqual((self.directory / "latest.json").read_bytes(), pointer)

    def test_a_scan_that_finishes_before_the_limit_ends_normally(self):
        """The real command, under the real prefix (limit shortened to 2 minutes): it completes, timeout exits 0
        with it, and the new report is published."""
        previous, _pointer = self.publish_previous()
        self.track("ROCK/new.flac")
        previous_attempt = reconciliation.scan_status()["last_attempt"]
        with mock.patch.object(launcher, "SCAN_TIME_LIMIT", "120s"):
            status, body = self.launch()
        self.assertEqual(status, 202, body)
        self.assertEqual(self.proc.wait(120), 0)
        self.assertTrue(wait_until(lambda: self.finished(previous_attempt), 10), reconciliation.scan_status())
        st = reconciliation.scan_status()
        self.assertEqual(st["last_attempt"]["status"], "complete")
        self.assertNotEqual(st["latest"]["scan_id"], previous)
        self.assertFalse(reconciliation._lock_holder_alive(self.directory))

    def test_end_to_end_the_real_command_completes_and_the_summary_shows_it(self):
        previous, _pointer = self.publish_previous()
        self.track("ROCK/new.flac")
        previous_attempt = reconciliation.scan_status()["last_attempt"]
        response = self.client.post(reverse(RUN))
        self.assertEqual(response.status_code, 202, response.content)
        body = response.json()
        self.pids.append(body["pid"])
        self.assertIn(body["state"], ("running", "finished", "starting"))
        self.assertNotEqual(body["state"], "complete")
        self.assertEqual(body["previous_attempt"], previous_attempt)
        self.assertTrue(wait_until(lambda: self.finished(previous_attempt), 120), reconciliation.scan_status())
        st = reconciliation.scan_status()
        self.assertIn(st["last_attempt"]["status"], ("complete", "suspect"))
        self.assertNotEqual(st["latest"]["scan_id"], previous)
        summary = self.client.get(reverse("library:api-reconciliation-summary")).json()
        self.assertEqual(summary["report"]["scan_id"], st["latest"]["scan_id"])
        self.assertEqual(summary["report"]["counts"]["tracks_total"], 2)

    def test_end_to_end_a_failed_scan_keeps_the_previous_report(self):
        previous, pointer = self.publish_previous()
        previous_attempt = reconciliation.scan_status()["last_attempt"]
        self.root.chmod(0)                                          # the Library storage cannot be read
        self.addCleanup(self.root.chmod, 0o755)
        response = self.client.post(reverse(RUN))
        self.assertEqual(response.status_code, 202, response.content)
        self.pids.append(response.json()["pid"])
        self.assertTrue(wait_until(lambda: self.finished(previous_attempt), 120), reconciliation.scan_status())
        st = reconciliation.scan_status()
        self.assertEqual(st["last_attempt"]["status"], "failed")
        self.assertEqual(st["latest"]["scan_id"], previous)
        self.assertEqual((self.directory / "latest.json").read_bytes(), pointer)
