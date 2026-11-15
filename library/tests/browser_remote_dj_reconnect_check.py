"""Real-browser check of the Remote DJ connection, automatic fresh-session reconnect and
leave-page protection (roadmap 2.17 Package B). Not a Django test module (no ``test_``
prefix, no Django import): it runs on a host with Playwright's Chromium, OUTSIDE the
validation sandbox (where Playwright cannot launch a browser), against the pages the sandbox
rendered with library.tests.render_remote_dj_console_artifact:

    run_with_validation_service.sh <python> manage.py test library.tests.render_remote_dj_console_artifact
    # extract REMOTE_DJ_PAGE / CONSOLE_PAGE from its stdout into <dir>/remote_dj_page.html, console_page.html
    <python with playwright> -I library/tests/browser_remote_dj_reconnect_check.py <dir>

Every request is intercepted (no server, database or station). The page's real script runs in a
real Chromium; WebSocket, RTCPeerConnection, getUserMedia and AudioContext are replaced by
recording fakes so every resource the page creates or releases is counted, and a scripted
"engine" answers /api/engine/status/, /api/remote-dj/token/ and /api/remote-dj/reconnect-policy/.
Playwright's fake clock drives the page's timers, so backoff, establishment timeouts and the
sequence deadline are deterministic. Exit status 0 only if every check passes.

Codex review regressions (2.17B remediation): offer/microphone setup failures (replaceTrack,
createAnswer, setLocalDescription, setRemoteDescription, missing transceivers) for a manual Connect
and an automatic replacement, LATE failures of a superseded attempt against its successor, the
whole-sequence deadline (late policy answer, capped establishment, establishment after expiry,
simulated system sleep, late token/microphone, stale timers vs. a successor), and native
AudioContext semantics (closing it does NOT end its destination tracks). Unhandled promise
rejections are recorded and asserted absent.
"""
import hashlib
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "https://station.test"
results = []
failures = []

FAKES = r"""
(() => {
  const F = window.__fake = {ws: [], pcs: [], tracks: [], ctxs: [], gumCalls: 0, gumHold: false, gumPending: [],
                             unhandled: [], nextPcInject: null, heldRejects: []};
  // Every unhandled promise rejection the page leaves behind is recorded (a setup failure must not leave one).
  window.addEventListener('unhandledrejection', e => F.unhandled.push(String(e.reason && e.reason.message || e.reason)));
  // Failure injection for the NEXT RTCPeerConnection the page creates: {method: 'reject' | 'hold'}, methods
  // setRemoteDescription, replaceTrack, createAnswer, setLocalDescription; {transceivers: 'missing'}.
  // 'hold' leaves the call pending until F.rejectHeld() rejects it (a LATE failure).
  function injected(pc, method) {
    const mode = pc.inject[method];
    if (mode === 'reject') return Promise.reject(new Error('injected ' + method + ' failure'));
    if (mode === 'hold') return new Promise((_, reject) => F.heldRejects.push(() => reject(new Error('late ' + method + ' failure'))));
    return null;
  }
  F.rejectHeld = () => { const held = F.heldRejects; F.heldRejects = []; held.forEach(reject => reject()); };
  function track(source) { const t = {kind: 'audio', source, stopped: false, stop() { this.stopped = true; }}; F.tracks.push(t); return t; }
  function stream(source = 'mic') { const t = track(source); return {getTracks: () => [t], getAudioTracks: () => [t]}; }
  class FakeWebSocket {
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; this.closedByPage = false; F.ws.push(this); }
    send(data) { this.sent.push(JSON.parse(data)); }
    close() {
      if (this.readyState >= 2) return;
      this.readyState = 3; this.closedByPage = true;
      setTimeout(() => this.onclose && this.onclose({code: 1000}), 0);
    }
  }
  FakeWebSocket.CONNECTING = 0; FakeWebSocket.OPEN = 1; FakeWebSocket.CLOSING = 2; FakeWebSocket.CLOSED = 3;
  window.WebSocket = FakeWebSocket;
  class FakePC {
    constructor(cfg) {
      this.cfg = cfg; this.iceConnectionState = 'new'; this.closed = false; this.localDescription = null;
      this.inject = F.nextPcInject || {}; F.nextPcInject = null;
      const pc = this;
      const sender = () => ({replaceTrack: (t) => injected(pc, 'replaceTrack') || Promise.resolve(),
                             getParameters: () => ({encodings: [{}]}), setParameters: async () => {}});
      this.transceivers = [{direction: '', sender: sender()}, {direction: '', sender: sender()}];
      F.pcs.push(this);
    }
    getTransceivers() { return this.inject.transceivers === 'missing' ? this.transceivers.slice(0, 1) : this.transceivers; }
    setRemoteDescription(d) { return injected(this, 'setRemoteDescription') || Promise.resolve(this.remote = d); }
    createAnswer() {
      return injected(this, 'createAnswer') || Promise.resolve({type: 'answer', sdp: 'a=fmtp:111 minptime=10;useinbandfec=1'});
    }
    setLocalDescription(d) { return injected(this, 'setLocalDescription') || Promise.resolve(this.localDescription = d); }
    async addIceCandidate() {}
    async getStats() { return new Map(); }
    close() { this.closed = true; this.iceConnectionState = 'closed'; }
  }
  window.RTCPeerConnection = FakePC;
  navigator.mediaDevices.getUserMedia = async () => {
    F.gumCalls += 1;
    if (F.gumHold) return new Promise(resolve => F.gumPending.push(resolve));
    return stream();
  };
  F.releaseGum = () => { const pending = F.gumPending; F.gumPending = []; pending.forEach(resolve => resolve(stream())); };
  class FakeAudioContext {
    constructor() { this.closed = false; this.state = 'running'; F.ctxs.push(this); }
    createMediaStreamSource() { return {connect() {}}; }
    createGain() { return {gain: {value: 1}, connect() {}}; }
    // Native Chromium (Codex F3): closing the context does NOT end its MediaStreamDestination's
    // output tracks -- only an explicit track.stop() does. The fake behaves the same way.
    createMediaStreamDestination() { return {stream: stream('graph')}; }
    createAnalyser() { return {fftSize: 2048, getFloatTimeDomainData() {}, connect() {}}; }
    close() {
      if (this.closed) return Promise.reject(new Error('InvalidStateError: already closed'));
      this.closed = true; this.state = 'closed'; return Promise.resolve();
    }
  }
  window.AudioContext = FakeAudioContext; window.webkitAudioContext = FakeAudioContext;
  F.open = (i) => { const ws = F.ws[i]; ws.readyState = 1; ws.onopen && ws.onopen({}); };
  // Block bodies: never hand the page's async handler promise back to page.evaluate (it would wait on it).
  F.offer = (i) => { F.ws[i].onmessage({data: JSON.stringify({type: 'offer', sdp: 'v=0'})}); };
  F.serverClose = (i, code) => { const ws = F.ws[i]; ws.readyState = 3; ws.onclose && ws.onclose({code}); };
  F.error = (i) => { if (F.ws[i].onerror) F.ws[i].onerror({}); };
  F.kicked = (i) => { F.ws[i].onmessage({data: JSON.stringify({type: 'kicked'})}); };
  F.ice = (i, state) => { const pc = F.pcs[i]; pc.iceConnectionState = state; pc.oniceconnectionstatechange && pc.oniceconnectionstatechange(); };
  F.summary = () => ({
    ws: F.ws.map(ws => ({readyState: ws.readyState, closedByPage: ws.closedByPage, answer: ws.sent.some(m => m.type === 'answer')})),
    pcs: F.pcs.map(pc => ({closed: pc.closed})),
    liveTracks: F.tracks.filter(t => !t.stopped).length,
    liveMic: F.tracks.filter(t => t.source === 'mic' && !t.stopped).length,
    liveOutput: F.tracks.filter(t => t.source === 'graph' && !t.stopped).length,
    unhandled: F.unhandled.slice(),
    tracks: F.tracks.length,
    openCtxs: F.ctxs.filter(c => !c.closed).length,
    gumCalls: F.gumCalls,
  });
})();
"""


def check(scenario, label, ok, detail=""):
    line = f"{'PASS' if ok else 'FAIL'} [{scenario}] {label}{(': ' + str(detail)) if detail else ''}"
    (results if ok else failures).append(line)
    print(line, flush=True)


class Engine:
    """The scripted station side: engine status, token minting and the reconnect policy."""

    def __init__(self, viewer_is_owner=True):
        self.seq = 0
        self.connected = False
        self.owner_attempt = None
        self.connection = None
        self.slot = "available"
        self.tokens = []
        self.token_status = 200
        self.policy_calls = []
        self.policy = lambda attempts: {"decision": "retry", "reason": "ok"}
        self.viewer_is_owner = viewer_is_owner
        self.other_owner = None
        self.instance = "eng-1"

    def mint(self):
        attempt = f"harnessAttempt{len(self.tokens) + 1:010d}"
        self.tokens.append(attempt)
        return {"token": f"tok-{attempt}", "attempt_id": attempt, "control_key": "a" * 64,
                "ice_servers": [{"urls": "stun:stun.test:19302"}]}

    def admit(self, attempt):
        self.connected, self.owner_attempt, self.slot = True, attempt, "occupied"
        self.connection = {"attempt_id": attempt, "status": "connected", "end_reason": None,
                           "emergency_mute_at_end": False, "failure": None}

    def finalize(self, end_reason="signaling_closed", slot="cleaning"):
        self.connected, self.slot = False, slot
        if self.connection:
            self.connection = {**self.connection, "status": "ended", "end_reason": end_reason}
        self.owner_attempt = None

    def status(self):
        self.seq += 1
        owner = None
        attempt = self.other_owner if self.other_owner else self.owner_attempt
        if self.connected and attempt:
            owner = {"identity": "known", "display_name": "Remote Host",
                     "is_current_user": self.viewer_is_owner and not self.other_owner,
                     "browser_key": hashlib.sha256(attempt.encode()).hexdigest()[:32], "gate_generation": 1}
        return {"transport": "STOPPED", "decks": {"A": None, "B": None}, "queue": [], "fx_fires": [],
                "timestamp": 0, "state_revision": {"instance": self.instance, "seq": self.seq},
                "remote_dj_configured": True, "remote_dj_connected": self.connected or bool(self.other_owner),
                "remote_dj_live": False, "remote_dj_reconnecting": False,
                "remote_dj_quality": {"overall": "good"} if self.connected else None,
                "remote_dj_connection": self.connection, "remote_dj_slot": self.slot,
                "remote_dj_owner_supported": True, "remote_dj_owner": owner,
                "manual_mode": False, "mic_live": False}


class Page:
    def __init__(self, browser, html, engine, scenario):
        self.engine, self.scenario = engine, scenario
        self.context = browser.new_context(ignore_https_errors=True)
        self.page = self.context.new_page()
        self.errors = []
        self.held = []                # requests answered later (late responses) or aborted at close
        self.held_policy = []         # policy requests held by the "hold" answer, released by release_policy()
        self.hold_tokens = False
        self.dialogs = []
        self.navigated = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("dialog", self._dialog)
        self.dismiss_native = True
        self.page.add_init_script(FAKES)
        self.page.route("**/*", lambda route, request: self._route(route, request, html))
        # A PAUSED fake clock: page timers advance only through advance() (run_for), so the short real-time
        # waits used to let fetches settle can never fire a backoff or timeout early.
        self.page.clock.install(time=1_800_000_000_000)
        self.page.clock.pause_at(1_800_000_000_500)
        self.page.goto(f"{BASE}/remote-dj/")
        self.settle()
        self.advance(1100)                       # first status poll enables the Connect button

    def _dialog(self, dialog):
        self.dialogs.append(dialog.type)
        dialog.dismiss() if self.dismiss_native else dialog.accept()

    def _route(self, route, request, html):
        url = request.url
        path = url.replace(BASE, "").split("?")[0]
        if path in ("/remote-dj/", "/"):
            return route.fulfill(status=200, content_type="text/html", body=html)
        if path == "/api/engine/status/":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(self.engine.status()))
        if path == "/api/remote-dj/token/":
            if self.hold_tokens:
                self.held.append(route)
                return None
            if self.engine.token_status != 200:
                return route.fulfill(status=self.engine.token_status, content_type="application/json",
                                     body=json.dumps({"error": "Remote DJ access is not permitted now"}))
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(self.engine.mint()))
        if path == "/api/remote-dj/reconnect-policy/":
            attempts = re.search(r"attempts=([^&]*)", url)
            attempts = attempts.group(1).replace("%2C", ",").split(",") if attempts else []
            self.engine.policy_calls.append(attempts)
            answer = self.engine.policy(attempts)
            if answer == "error":
                return route.fulfill(status=500, body="boom")
            if answer == "hang":
                self.held.append(route)    # never answered in time: the page's own timeout must handle it
                return None
            if answer == "hold":
                self.held_policy.append(route)   # answered later, by release_policy()
                return None
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(answer))
        self.navigated.append(path) if request.is_navigation_request() else None
        if request.is_navigation_request():
            return route.fulfill(status=200, content_type="text/html", body="<html><body>LEFT</body></html>")
        return route.fulfill(status=200, content_type="application/json", body="{}")

    # -- driving ----------------------------------------------------------------
    def settle(self, rounds=4):
        for _ in range(rounds):
            self.page.wait_for_timeout(15)

    def advance(self, ms, step=250):
        remaining = ms
        while remaining > 0:
            chunk = min(step, remaining)
            self.page.clock.run_for(chunk)
            self.settle(2)
            remaining -= chunk

    def js(self, expr, arg=None):
        return self.page.evaluate(expr, arg)

    def fake(self):
        return self.js("window.__fake.summary()")

    def button(self):
        return self.js("({action: document.getElementById('remoteDjConnectAction').textContent,"
                       " status: document.getElementById('remoteDjConnectStatus').textContent,"
                       " disabled: document.getElementById('remoteDjConnectBtn').disabled})")

    def click_connect(self):
        self.page.click("#remoteDjConnectBtn")
        self.settle()

    def establish(self, index):
        """Server side of one admitted attempt: socket open, offer, ICE connected, engine owner."""
        self.js("i => window.__fake.open(i)", index)
        self.js("i => window.__fake.offer(i)", index)
        self.settle(6)
        self.js("i => window.__fake.ice(i, 'connected')", index)
        self.engine.admit(self.engine.tokens[-1])
        self.advance(2200)                       # two status polls: trusted ownership

    def lose_established(self, ws_index, code=1006):
        self.engine.finalize()
        self.js("([i, c]) => window.__fake.serverClose(i, c)", [ws_index, code])
        self.settle()

    def owns(self):
        return self.js("rdjOwnsLiveSession()")

    def guard(self):
        return self.js("rdjUnloadGuardInstalled")

    def add_test_links(self):
        self.js("""() => {
          const box = document.createElement('div'); box.id = 'testLinks';
          box.innerHTML = '<a id="navSame" href="/library/">Library</a>'
            + '<a id="navBlank" href="/library/" target="_blank">New tab</a>'
            + '<a id="navHash" href="#queueSection">Anchor</a>'
            + '<a id="navHandled" href="/library/" onclick="event.preventDefault(); window.__handled = 1">JS</a>'
            + '<form id="navForm" method="get" action="/search/"><button id="navSubmit">Go</button></form>';
          document.body.appendChild(box);
        }""")

    def release_policy(self, answer):
        held, self.held_policy = self.held_policy, []
        for route in held:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(answer))
        self.settle(6)
        return len(held)

    def now(self):
        return self.js("Date.now()")

    def wall_jump(self, ms):
        """Wall clock jumps forward WITHOUT firing timers or advancing performance.now() (system sleep)."""
        self.page.clock.set_system_time(self.now() + ms)
        self.settle()

    def release_held(self):
        held, self.held = self.held, []
        for route in held:
            try:
                if "/api/remote-dj/token/" in route.request.url:
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(self.engine.mint()))
                else:
                    route.abort()
            except Exception:          # noqa: BLE001 -- the page may already have aborted it
                pass
        self.settle(6)

    def close(self):
        self.release_held()
        for route in self.held_policy:
            try:
                route.abort()
            except Exception:          # noqa: BLE001
                pass
        self.held_policy = []
        self.context.close()


# ---------------------------------------------------------------------------
def scenario_manual_connect_and_leave_page(browser, html):
    name = "manual+navigation"
    engine = Engine()
    p = Page(browser, html, engine, name)
    check(name, "no beforeunload guard before connecting", not p.guard())
    p.add_test_links()
    p.page.click("#navSame")
    p.settle()
    check(name, "an unconnected page navigates without a prompt", p.page.url.endswith("/library/"), p.page.url)
    p.close()

    p = Page(browser, html, engine, name)
    p.click_connect()
    check(name, "one token request", len(engine.tokens) == 1)
    check(name, "guard installed while establishing", p.guard() and p.owns())
    p.establish(0)
    check(name, "connected presentation after authoritative ownership", p.button()["action"] == "Disconnect",
          p.button())
    p.add_test_links()
    p.page.click("#navSame")
    p.settle()
    check(name, "same-tab link opens the leave dialog", p.js("document.getElementById('rdjLeaveDialog').open"))
    check(name, "dialog wording", "live Remote DJ audio session" in p.js(
        "document.getElementById('rdjLeaveText').textContent"))
    check(name, "Stay is focused", p.js("document.activeElement && document.activeElement.id") == "rdjLeaveStay")
    p.page.click("#rdjLeaveStay")
    p.settle()
    f = p.fake()
    check(name, "Stay keeps socket, peer and microphone", f["ws"][0]["readyState"] == 1 and not f["pcs"][0]["closed"]
          and f["liveTracks"] >= 1, f)
    check(name, "Stay keeps the same attempt", p.js("rdjAttemptId") == engine.tokens[0])
    check(name, "Stay did not navigate", p.page.url.endswith("/remote-dj/"))
    p.page.keyboard.press("Escape")
    p.page.click("#navSame")
    p.settle()
    p.page.keyboard.press("Escape")             # Esc = Stay
    p.settle()
    check(name, "Escape keeps the session", p.owns() and not p.js("document.getElementById('rdjLeaveDialog').open"))
    for target in ("#navBlank", "#navHash", "#navHandled"):
        p.page.click(target, modifiers=[]) if target != "#navBlank" else p.page.evaluate(
            "document.getElementById('navBlank').dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}))")
        p.settle()
        check(name, f"{target} does not prompt", not p.js("document.getElementById('rdjLeaveDialog').open"))
    check(name, "session intact after non-navigating clicks", p.owns() and "/remote-dj/" in p.page.url
          and p.fake()["ws"][0]["readyState"] == 1, p.page.url)
    p.page.click("#navSubmit")
    p.settle()
    check(name, "same-tab form submission prompts", p.js("document.getElementById('rdjLeaveDialog').open"))
    p.page.click("#rdjLeaveStay")
    p.page.click("#navSame")
    p.settle()
    policy_before = len(engine.policy_calls)
    p.page.click("#rdjLeaveGo")
    p.page.wait_for_url("**/library/")
    check(name, "Leave navigates", p.page.url.endswith("/library/"))
    check(name, "no native prompt after confirming", p.dialogs == [], p.dialogs)
    check(name, "leaving never starts a reconnect", len(engine.policy_calls) == policy_before)
    check(name, "no page errors", not p.errors, p.errors)
    p.close()


def scenario_native_prompt_and_disconnect(browser, html):
    name = "native+disconnect"
    engine = Engine()
    p = Page(browser, html, engine, name)
    p.click_connect()
    p.establish(0)
    check(name, "guard installed while connected", p.guard())
    p.dismiss_native = True
    try:
        p.js("location.href = '/elsewhere/'")
    except Exception:
        pass
    p.settle(8)
    check(name, "script navigation gets the native beforeunload prompt", "beforeunload" in p.dialogs, p.dialogs)
    check(name, "dismissing the native prompt keeps the session", p.page.url.endswith("/remote-dj/") and p.owns())
    p.click_connect()                        # explicit Disconnect
    engine.finalize("signaling_closed", slot="available")   # the server sees the socket close
    p.advance(500)
    f = p.fake()
    check(name, "Disconnect closes socket and peer", f["ws"][0]["closedByPage"] and f["pcs"][0]["closed"], f)
    check(name, "Disconnect stops the microphone and closes the AudioContext", f["liveTracks"] == 0
          and f["openCtxs"] == 0, f)
    check(name, "explicit cleanup ends BOTH the microphone and the Web Audio destination tracks",
          f["liveMic"] == 0 and f["liveOutput"] == 0 and f["tracks"] >= 2, f)
    check(name, "guard removed after Disconnect", not p.guard())
    check(name, "Disconnect never reconnects", engine.policy_calls == [] and len(engine.tokens) == 1)
    dialogs = len(p.dialogs)
    p.page.reload()
    p.settle()
    check(name, "no prompt once disconnected", len(p.dialogs) == dialogs, p.dialogs)
    p.close()

    p = Page(browser, html, engine, name)
    p.click_connect()
    p.establish(len(engine.tokens) - 1 - (len(engine.tokens) - 1))
    p.page.close(run_before_unload=True)
    p.page.wait_for_timeout(200)
    check(name, "closing the tab gets the native prompt", "beforeunload" in p.dialogs, p.dialogs)
    p.context.close()


def scenario_auto_reconnect_success(browser, html):
    name = "auto-reconnect"
    engine = Engine()
    calls = iter([{"decision": "wait", "reason": "previous_session_cleanup"}] * 2
                 + [{"decision": "retry", "reason": "ok"}] * 5)
    engine.policy = lambda attempts: next(calls)
    p = Page(browser, html, engine, name)
    p.click_connect()
    p.establish(0)
    lost = engine.tokens[0]
    p.lose_established(0)
    f = p.fake()
    check(name, "old peer, microphone and AudioContext released at once", f["pcs"][0]["closed"]
          and f["liveTracks"] == 0 and f["openCtxs"] == 0, f)
    check(name, "Cancel reconnect presentation", p.button()["action"] == "Cancel reconnect", p.button())
    check(name, "Remote Mic gate unavailable during retry", not p.js("rdjMayGate(window.__rdjLastEngineState || {})"))
    check(name, "guard stays while reconnecting", p.guard())
    p.advance(900)
    check(name, "no policy request before the first backoff (1 s)", engine.policy_calls == [])
    p.advance(200)
    check(name, "policy asked with the lost attempt", engine.policy_calls and engine.policy_calls[0] == [lost],
          engine.policy_calls)
    check(name, "waiting never spends an attempt", len(engine.tokens) == 1)
    check(name, "waiting presentation", "Waiting" in p.button()["status"], p.button())
    engine.slot = "available"
    p.advance(2100)
    check(name, "a fresh token for the replacement", len(engine.tokens) == 2 and engine.tokens[1] != lost,
          engine.tokens)
    check(name, "retrying presentation", p.button()["status"].startswith("Retrying 1 of 3"), p.button())
    check(name, "a new socket and a new peer connection", len(p.fake()["ws"]) == 2)
    p.establish(1)
    f = p.fake()
    check(name, "a new PeerConnection (old one stays closed)", len(f["pcs"]) == 2 and f["pcs"][0]["closed"]
          and not f["pcs"][1]["closed"], f)
    check(name, "exactly one live microphone capture (the replacement's)", f["liveMic"] == 1
          and f["openCtxs"] == 1, f)
    check(name, "exactly one live destination track (the replacement's)", f["liveOutput"] == 1, f)
    check(name, "normal presentation only after ownership", p.button()["action"] == "Disconnect", p.button())
    check(name, "retry sequence finished", p.js("rdjRetry") is None)
    check(name, "no page errors", not p.errors, p.errors)
    p.close()


def scenario_policy_stops(browser, html):
    for reason, expected in (("auto_reconnect_disabled", "Disconnected"), ("kicked", "Disconnected by the station"),
                             ("emergency_mute", "Muted by the station"), ("remote_dj_unavailable",
                                                                           "Remote DJ unavailable"),
                             ("not_authorized", "Reconnect not permitted")):
        name = f"policy-stop:{reason}"
        engine = Engine()
        engine.policy = lambda attempts, r=reason: {"decision": "stop", "reason": r}
        p = Page(browser, html, engine, name)
        p.click_connect()
        p.establish(0)
        p.lose_established(0)
        p.advance(1500)
        check(name, "no replacement token", len(engine.tokens) == 1, engine.tokens)
        check(name, "stop notice", p.button()["status"] == expected, p.button())
        check(name, "guard removed", not p.guard())
        p.advance(5000)
        check(name, "stays stopped", len(engine.policy_calls) == 1 and len(engine.tokens) == 1)
        p.close()


def scenario_cancel(browser, html):
    name = "cancel-while-waiting"
    engine = Engine()
    engine.policy = lambda attempts: {"decision": "wait", "reason": "previous_session_cleanup"}
    p = Page(browser, html, engine, name)
    p.click_connect()
    p.establish(0)
    p.lose_established(0)
    p.advance(2100)
    calls = len(engine.policy_calls)
    p.click_connect()                         # Cancel reconnect
    p.advance(6000)
    check(name, "no policy request after Cancel", len(engine.policy_calls) == calls, engine.policy_calls)
    check(name, "cancelled notice", p.button()["status"] == "Reconnect cancelled", p.button())
    check(name, "guard removed after Cancel", not p.guard())
    p.click_connect()                         # a normal manual Connect afterwards
    check(name, "manual Connect still possible", len(engine.tokens) == 2)
    p.close()

    name = "cancel-mid-attempt"
    engine = Engine()
    engine.policy = lambda attempts: {"decision": "retry", "reason": "ok"}
    p = Page(browser, html, engine, name)
    p.click_connect()
    p.establish(0)
    p.lose_established(0)
    p.advance(1100)
    check(name, "replacement attempt started", len(engine.tokens) == 2)
    p.js("i => window.__fake.open(i)", 1)
    p.js("i => window.__fake.offer(i)", 1)
    p.settle(6)
    p.click_connect()                         # Cancel during establishment
    p.settle()
    f = p.fake()
    check(name, "cancel releases the replacement's socket, peer and mic", f["ws"][1]["closedByPage"]
          and f["pcs"][1]["closed"] and f["liveTracks"] == 0 and f["openCtxs"] == 0, f)
    p.js("i => window.__fake.ice(i, 'connected')", 1)      # a late event from the abandoned attempt
    p.advance(8000)
    check(name, "late events of the cancelled attempt are inert", len(engine.tokens) == 2
          and p.js("rdjPc") is None and not p.owns(), p.fake())
    p.close()


def scenario_bounded_failures(browser, html):
    name = "three-failures"
    engine = Engine()
    engine.policy = lambda attempts: {"decision": "retry", "reason": "ok"}
    p = Page(browser, html, engine, name)
    p.click_connect()
    p.establish(0)
    p.lose_established(0)
    timeline = []
    for attempt, backoff in ((1, 1000), (2, 2000), (3, 4000)):
        p.advance(backoff - 100)
        timeline.append((attempt, "before", len(engine.tokens)))
        p.advance(200)
        timeline.append((attempt, "after", len(engine.tokens)))
        p.js("i => window.__fake.serverClose(i, 1006)", len(p.fake()["ws"]) - 1)   # refused before establishment
        p.settle()
    check(name, "backoff 1 s, 2 s, 4 s before attempts", timeline == [
        (1, "before", 1), (1, "after", 2), (2, "before", 2), (2, "after", 3), (3, "before", 3), (3, "after", 4)],
        timeline)
    p.advance(10000)
    check(name, "exactly three replacement attempts", len(engine.tokens) == 4, engine.tokens)
    check(name, "Reconnect failed", p.button()["status"] == "Reconnect failed", p.button())
    f = p.fake()
    check(name, "no resources left behind", f["liveTracks"] == 0 and f["openCtxs"] == 0
          and all(pc["closed"] for pc in f["pcs"]), f)
    check(name, "attempt ids sent to the policy", engine.policy_calls[-1][0] == engine.tokens[0]
          and len(engine.policy_calls[-1]) == 3, engine.policy_calls[-1])
    p.close()


def scenario_establish_timeout(browser, html):
    name = "slow-connecting"
    engine = Engine()
    engine.policy = lambda attempts: {"decision": "retry", "reason": "ok"}
    p = Page(browser, html, engine, name)
    p.click_connect()
    p.establish(0)
    p.lose_established(0)
    p.advance(1100)
    check(name, "attempt 1 started", len(engine.tokens) == 2)
    p.advance(28000)                           # WebSocket stays CONNECTING (slow cellular)
    check(name, "no premature failure while CONNECTING", len(engine.tokens) == 2 and p.fake()["ws"][1]["readyState"] == 0)
    p.advance(2500)                            # 30 s establishment bound reached
    f = p.fake()
    check(name, "establishment timeout releases the attempt", f["ws"][1]["closedByPage"], f)
    p.advance(2100)
    check(name, "next attempt after its backoff", len(engine.tokens) == 3, engine.tokens)
    p.close()


def scenario_fail_closed_policy(browser, html):
    for mode in ("error", "hang"):
        name = f"policy-{mode}"
        engine = Engine()
        engine.policy = lambda attempts, m=mode: m
        p = Page(browser, html, engine, name)
        p.click_connect()
        p.establish(0)
        p.lose_established(0)
        p.advance(30000, step=500)
        check(name, "never an attempt without a retry decision", len(engine.tokens) == 1, engine.tokens)
        check(name, "keeps waiting (bounded)", p.js("rdjRetry && rdjRetry.phase") in ("waiting", "evaluating"))
        p.advance(125000, step=1000)
        check(name, "sequence deadline ends it", p.js("rdjRetry") is None and p.button()["status"] == "Reconnect failed",
              p.button())
        check(name, "still no attempt", len(engine.tokens) == 1)
        p.close()


def scenario_terminal_events(browser, html):
    name = "kick"
    engine = Engine()
    p = Page(browser, html, engine, name)
    p.click_connect()
    p.establish(0)
    engine.finalize("kicked", slot="available")
    p.js("i => window.__fake.kicked(i)", 0)
    p.js("([i, c]) => window.__fake.serverClose(i, c)", [0, 1000])
    p.advance(500)
    check(name, "Kick notice (the existing 3 s message)", p.button()["status"] == "Disconnected by the station",
          p.button())
    p.advance(5000)
    check(name, "Kick never reconnects", engine.policy_calls == [] and len(engine.tokens) == 1)
    p.close()

    name = "first-connect-failure"
    engine = Engine()
    p = Page(browser, html, engine, name)
    p.click_connect()
    p.js("([i, c]) => window.__fake.serverClose(i, c)", [0, 4002])     # refused before any session
    p.advance(5000)
    check(name, "a failed first Connect never reconnects", engine.policy_calls == [] and len(engine.tokens) == 1)
    p.close()

    name = "duplicate-socket-events"
    engine = Engine()
    engine.policy = lambda attempts: {"decision": "wait", "reason": "previous_session_cleanup"}
    p = Page(browser, html, engine, name)
    p.click_connect()
    p.establish(0)
    engine.finalize()
    p.js("i => window.__fake.error(i)", 0)
    p.js("([i, c]) => window.__fake.serverClose(i, c)", [0, 1006])
    p.js("i => window.__fake.ice(i, 'failed')", 0)
    p.advance(1100)
    check(name, "one sequence from duplicate terminal events", len(engine.policy_calls) == 1
          and p.js("rdjRetryGen") == 1, (engine.policy_calls, p.js("rdjRetryGen")))
    p.close()

    name = "transient-disconnect"
    engine = Engine()
    p = Page(browser, html, engine, name)
    p.click_connect()
    p.establish(0)
    p.js("i => window.__fake.ice(i, 'disconnected')", 0)            # B1: same-session recovery
    p.advance(1200)
    check(name, "transient DISCONNECTED keeps the session", p.owns() and not p.fake()["pcs"][0]["closed"]
          and engine.policy_calls == [])
    p.js("i => window.__fake.ice(i, 'connected')", 0)
    p.advance(1200)
    check(name, "recovers without a new session", len(engine.tokens) == 1 and p.button()["action"] == "Disconnect")
    p.close()


def scenario_late_media_and_tokens(browser, html):
    name = "late-getUserMedia"
    engine = Engine()
    p = Page(browser, html, engine, name)
    p.js("window.__fake.gumHold = true")
    p.click_connect()
    p.js("i => window.__fake.open(i)", 0)
    p.js("i => window.__fake.offer(i)", 0)
    p.settle(6)
    check(name, "permission prompt pending", p.fake()["gumCalls"] == 1)
    p.click_connect()                                                 # cancel while the prompt is open
    p.js("window.__fake.releaseGum()")
    p.settle(6)
    f = p.fake()
    check(name, "a late stream is stopped at once", f["liveTracks"] == 0 and f["openCtxs"] == 0, f)
    check(name, "no answer from the abandoned attempt", not f["ws"][0]["answer"], f)
    p.close()

    name = "late-token"
    engine = Engine()
    p = Page(browser, html, engine, name)
    p.hold_tokens = True
    p.click_connect()
    p.click_connect()                                                 # cancel while the token is in flight
    check(name, "cancel while the token is in flight", not p.owns() and p.fake()["ws"] == [])
    p.release_held()                                                  # the token response arrives late
    p.advance(2000)
    check(name, "a late token response opens nothing", p.fake()["ws"] == [] and not p.owns()
          and p.js("rdjAttemptId") is None, p.fake())
    p.close()


def scenario_unstable_replacement(browser, html):
    name = "unstable-replacement"
    engine = Engine()
    engine.policy = lambda attempts: {"decision": "retry", "reason": "ok"}
    p = Page(browser, html, engine, name)
    p.click_connect()
    p.establish(0)
    p.lose_established(0)
    p.advance(1100)
    p.establish(1)                                  # replacement #1 established ...
    p.advance(5000)
    p.lose_established(1)                           # ... and lost again within 60 s
    check(name, "the new sequence continues the same budget", p.js("rdjRetry && rdjRetry.used") == 1,
          p.js("rdjRetry && rdjRetry.used"))
    p.advance(2100)
    p.establish(2)
    p.advance(70000, step=1000)                     # stable for longer than 60 s
    p.lose_established(2)
    check(name, "a stable replacement earns a fresh budget", p.js("rdjRetry && rdjRetry.used") == 0,
          p.js("rdjRetry && rdjRetry.used"))
    p.close()


def scenario_observer_and_tabs(browser, html):
    name = "observer"
    engine = Engine(viewer_is_owner=False)
    engine.connected, engine.other_owner, engine.slot = True, "otherBrowserAttempt000001", "occupied"
    p = Page(browser, html, engine, name)
    p.advance(2200)
    p.add_test_links()
    check(name, "observer page has no guard", not p.guard() and not p.owns())
    p.page.click("#navSame")
    p.page.wait_for_url("**/library/")
    check(name, "observer navigates freely", p.page.url.endswith("/library/") and p.dialogs == [])
    p.close()

    name = "two-tabs"
    engine = Engine()
    a = Page(browser, html, engine, name + "-A")
    b = Page(browser, html, engine, name + "-B")
    a.click_connect()
    a.establish(0)
    b.advance(2200)
    check(name, "owner tab guarded, other tab not", a.guard() and not b.guard())
    a.close()
    b.close()


def scenario_lifecycle(browser, html):
    name = "background-foreground"
    engine = Engine()
    p = Page(browser, html, engine, name)
    p.click_connect()
    p.establish(0)
    p.js("""() => { Object.defineProperty(document, 'visibilityState', {value: 'hidden', configurable: true});
                    document.dispatchEvent(new Event('visibilitychange')); }""")
    p.advance(3000)
    p.js("""() => { Object.defineProperty(document, 'visibilityState', {value: 'visible', configurable: true});
                    document.dispatchEvent(new Event('visibilitychange')); }""")
    p.advance(1200)
    check(name, "backgrounding never disconnects", p.owns() and not p.fake()["pcs"][0]["closed"])
    p.js("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: false}))")
    p.advance(3000)
    f = p.fake()
    check(name, "pagehide releases and never reconnects", f["ws"][0]["closedByPage"] and f["liveTracks"] == 0
          and engine.policy_calls == [], f)
    p.close()

    name = "leave-during-retry"
    engine = Engine()
    engine.policy = lambda attempts: {"decision": "wait", "reason": "previous_session_cleanup"}
    p = Page(browser, html, engine, name)
    p.click_connect()
    p.establish(0)
    p.lose_established(0)
    p.advance(1100)
    p.add_test_links()
    p.page.click("#navSame")
    p.settle()
    check(name, "a pending reconnect is protected too", p.js("document.getElementById('rdjLeaveDialog').open"))
    calls = len(engine.policy_calls)
    p.page.click("#rdjLeaveGo")
    p.page.wait_for_url("**/library/")
    check(name, "leaving invalidates the reconnect", len(engine.policy_calls) == calls and engine.tokens == [engine.tokens[0]])
    p.close()


def scenario_calibration(browser, html):
    name = "calibration"
    engine = Engine()
    p = Page(browser, html, engine, name)
    try:
        p.js("rdjRunCalibration()")
    except Exception as exc:          # noqa: BLE001
        check(name, "calibration starts", False, exc)
        p.close()
        return
    p.advance(15000, step=500)
    f = p.fake()
    check(name, "calibration releases its own microphone and AudioContext", f["gumCalls"] >= 1
          and f["liveTracks"] == 0 and f["openCtxs"] == 0, f)
    check(name, "calibration never starts a Remote DJ connection", f["ws"] == [] and engine.tokens == [])
    check(name, "no page errors", not p.errors, p.errors)
    p.close()


# ---- 2.17B remediation (Codex review): setup failures, deadline, destination tracks ---------------
SETUP_FAILURES = (("replaceTrack", "reject"), ("createAnswer", "reject"), ("setLocalDescription", "reject"),
                  ("setRemoteDescription", "reject"), ("transceivers", "missing"))


def guarded(label, fn, *args):
    """Run one variant; an exception fails that variant only, never the remaining ones."""
    try:
        fn(*args)
    except Exception as exc:      # noqa: BLE001
        check(label, "raised", False, f"{type(exc).__name__}: {exc}")


def released(f, ws_index, pc_index):
    """The attempt's socket and peer are closed by the page; no microphone, destination track or context lives."""
    return (f["ws"][ws_index]["closedByPage"] and f["pcs"][pc_index]["closed"] and f["liveMic"] == 0
            and f["liveOutput"] == 0 and f["openCtxs"] == 0)


def offer_with(p, index, inject):
    p.js("i => window.__fake.open(i)", index)
    p.js("inject => { window.__fake.nextPcInject = inject; }", inject)
    p.js("i => window.__fake.offer(i)", index)
    p.settle(6)


def scenario_setup_failures(browser, html):
    for method, mode in SETUP_FAILURES:
        guarded(f"setup-failure:manual:{method}", scenario_setup_failures_manual, browser, html, method, mode)
        guarded(f"setup-failure:replacement:{method}", scenario_setup_failures_replacement, browser, html, method, mode)


def scenario_setup_failures_manual(browser, html, method, mode):
    name = f"setup-failure:manual:{method}"
    engine = Engine()
    p = Page(browser, html, engine, name)
    p.click_connect()
    offer_with(p, 0, {method: mode})
    f = p.fake()
    check(name, "mic + destination tracks stopped; AudioContext, peer and socket closed", released(f, 0, 0), f)
    check(name, "UI leaves Connecting/Negotiating", p.button()["action"] == "Connect"
          and p.button()["status"] == "Setup failed", p.button())
    check(name, "navigation protection not armed", not p.guard() and not p.owns())
    check(name, "no unhandled rejection or page error", not f["unhandled"] and not p.errors,
          (f["unhandled"], p.errors))
    check(name, "a manual Connect that never established never reconnects", engine.policy_calls == []
          and len(engine.tokens) == 1)
    engine.finalize("signaling_closed", slot="available")      # the server cleaned up the abandoned attempt
    p.advance(3500)
    p.click_connect()
    check(name, "a later Connect proceeds", len(engine.tokens) == 2, engine.tokens)
    p.establish(1)
    f = p.fake()
    check(name, "the later session is live", p.button()["action"] == "Disconnect" and f["liveMic"] == 1
          and f["liveOutput"] == 1 and f["openCtxs"] == 1 and not f["pcs"][1]["closed"], (p.button(), f))
    p.close()


def scenario_setup_failures_replacement(browser, html, method, mode):
    name = f"setup-failure:replacement:{method}"
    engine = Engine()
    engine.policy = lambda attempts: {"decision": "retry", "reason": "ok"}
    p = Page(browser, html, engine, name)
    p.click_connect()
    p.establish(0)
    p.lose_established(0)
    p.advance(1100)
    check(name, "replacement attempt 1 started", len(engine.tokens) == 2, engine.tokens)
    offer_with(p, 1, {method: mode})
    f = p.fake()
    check(name, "the replacement's resources are released", released(f, 1, 1), f)
    check(name, "the failure spends one attempt of the SAME sequence", p.js(
        "rdjRetry && [rdjRetry.gen, rdjRetry.used, rdjRetry.phase]") == [1, 1, "backoff"]
        and p.js("rdjRetryGen") == 1, p.js("rdjRetry && [rdjRetry.gen, rdjRetry.used, rdjRetry.phase]"))
    check(name, "still protected while the reconnect is pending", p.guard())
    check(name, "no unhandled rejection or page error", not f["unhandled"] and not p.errors,
          (f["unhandled"], p.errors))
    p.advance(2100)
    check(name, "next attempt after its backoff", len(engine.tokens) == 3, engine.tokens)
    p.establish(2)
    f = p.fake()
    check(name, "the next replacement is adopted", p.button()["action"] == "Disconnect"
          and p.js("rdjRetry") is None and p.js("rdjRetryGen") == 1 and f["liveMic"] == 1
          and f["liveOutput"] == 1 and f["openCtxs"] == 1, (p.button(), f))
    p.close()



def scenario_stale_setup_failure(browser, html):
    for method in ("replaceTrack", "createAnswer", "setLocalDescription"):
        guarded(f"stale-failure:manual:{method}", scenario_stale_setup_failure_manual, browser, html, method)
        guarded(f"stale-failure:replacement:{method}", scenario_stale_setup_failure_replacement, browser, html, method)


def scenario_stale_setup_failure_manual(browser, html, method):
    name = f"stale-failure:manual:{method}"
    engine = Engine()
    p = Page(browser, html, engine, name)
    p.click_connect()
    offer_with(p, 0, {method: "hold"})              # the step is still pending ...
    p.click_connect()                               # ... when the operator cancels
    f = p.fake()
    check(name, "cancel releases the pending attempt (incl. its destination track)", released(f, 0, 0), f)
    p.advance(500)
    p.click_connect()                               # a new Connect
    p.establish(1)
    p.js("window.__fake.rejectHeld()")              # the abandoned step now fails, late
    p.settle(6)
    p.advance(1200)
    f = p.fake()
    check(name, "a late failure of the cancelled attempt leaves the successor intact",
          f["ws"][1]["readyState"] == 1 and not f["ws"][1]["closedByPage"] and not f["pcs"][1]["closed"]
          and f["liveMic"] == 1 and f["liveOutput"] == 1 and f["openCtxs"] == 1
          and p.button()["action"] == "Disconnect" and p.js("rdjAttemptId") == engine.tokens[1], (p.button(), f))
    check(name, "no unhandled rejection or page error", not f["unhandled"] and not p.errors,
          (f["unhandled"], p.errors))
    p.close()


def scenario_stale_setup_failure_replacement(browser, html, method):
    name = f"stale-failure:replacement:{method}"
    engine = Engine()
    engine.policy = lambda attempts: {"decision": "retry", "reason": "ok"}
    p = Page(browser, html, engine, name)
    p.click_connect()
    p.establish(0)
    p.lose_established(0)
    p.advance(1100)
    offer_with(p, 1, {method: "hold"})
    p.js("([i, c]) => window.__fake.serverClose(i, c)", [1, 1006])     # attempt 1 refused while pending
    p.settle()
    p.advance(2100)
    check(name, "attempt 2 started", len(engine.tokens) == 3, engine.tokens)
    p.establish(2)
    p.js("window.__fake.rejectHeld()")
    p.settle(6)
    p.advance(1200)
    f = p.fake()
    check(name, "a late failure of attempt 1 leaves the adopted replacement intact",
          f["ws"][2]["readyState"] == 1 and not f["pcs"][2]["closed"] and f["liveMic"] == 1
          and f["liveOutput"] == 1 and f["openCtxs"] == 1 and p.button()["action"] == "Disconnect"
          and p.js("rdjAttemptId") == engine.tokens[2] and p.js("rdjRetry") is None, (p.button(), f))
    check(name, "no unhandled rejection or page error", not f["unhandled"] and not p.errors,
          (f["unhandled"], p.errors))
    p.close()



WAIT = {"decision": "wait", "reason": "previous_session_cleanup"}
RETRY = {"decision": "retry", "reason": "ok"}


def lost_and_waiting(browser, html, name, until_ms):
    """An established session lost at T; the policy answers "wait" (1 s re-asks) until T + until_ms."""
    engine = Engine()
    mode = {"answer": WAIT}
    engine.policy = lambda attempts: mode["answer"]
    p = Page(browser, html, engine, name)
    p.click_connect()
    p.establish(0)
    p.lose_established(0)
    p.advance(until_ms, step=1000)
    return engine, mode, p


def scenario_deadline(browser, html):
    guarded("deadline:late-retry-answer", deadline_late_retry_answer, browser, html)
    guarded("deadline:establishment-capped", deadline_establishment_capped, browser, html)
    guarded("deadline:established-after-expiry", deadline_established_after_expiry, browser, html)
    guarded("deadline:sleep-while-waiting", deadline_sleep_while_waiting, browser, html)
    guarded("deadline:late-token", deadline_late_token, browser, html)
    guarded("deadline:late-microphone", deadline_late_microphone, browser, html)
    guarded("deadline:successor-after-cancel", deadline_successor_after_cancel, browser, html)
    guarded("deadline:engine-turnover", deadline_engine_turnover, browser, html)


def deadline_late_retry_answer(browser, html):
    name = "deadline:late-retry-answer"
    engine, mode, p = lost_and_waiting(browser, html, name, 148500)
    mode["answer"] = "hold"
    p.advance(1000)                                     # the T+149 s request is held
    check(name, "one policy request held across the deadline", len(p.held_policy) == 1)
    p.advance(1500)                                     # T+151 s: deadline passed, request timeout (4 s) not yet
    remaining = p.js("rdjRetry ? rdjRetry.deadline - performance.now() : null")
    check(name, "deadline passed while the answer was in flight", remaining is not None and remaining < 0, remaining)
    p.release_policy(RETRY)
    p.advance(1000)
    check(name, "a valid retry answer after the deadline mints no token", len(engine.tokens) == 1, engine.tokens)
    check(name, "sequence ended, guard removed", p.js("rdjRetry") is None
          and p.button()["status"] == "Reconnect failed" and not p.guard(), p.button())
    check(name, "no unhandled rejection", not p.fake()["unhandled"] and not p.errors)
    p.close()


def deadline_establishment_capped(browser, html):
    name = "deadline:establishment-capped"
    engine, mode, p = lost_and_waiting(browser, html, name, 144500)
    mode["answer"] = RETRY
    p.advance(600)                                      # T+145 s: retry with 5 s of the sequence left
    check(name, "attempt started 5 s before the deadline", len(engine.tokens) == 2, engine.tokens)
    p.js("i => window.__fake.open(i)", 1)
    p.js("i => window.__fake.offer(i)", 1)
    p.settle(6)
    p.advance(4700)
    check(name, "still establishing inside the remaining time", not p.fake()["ws"][1]["closedByPage"]
          and p.js("rdjRetry && rdjRetry.phase") == "connecting")
    p.advance(400)
    f = p.fake()
    check(name, "the attempt ends at the sequence deadline, not 30 s later", released(f, 1, 1)
          and p.js("rdjRetry") is None, f)
    check(name, "terminal presentation, guard removed", p.button()["status"] == "Reconnect failed"
          and not p.guard(), p.button())
    calls = len(engine.policy_calls)
    p.advance(40000, step=1000)
    check(name, "nothing further", len(engine.tokens) == 2 and len(engine.policy_calls) == calls)
    p.close()


def deadline_established_after_expiry(browser, html):
    name = "deadline:established-after-expiry"
    engine = Engine()
    engine.policy = lambda attempts: RETRY
    p = Page(browser, html, engine, name)
    p.click_connect()
    p.establish(0)
    p.lose_established(0)
    p.advance(1100)
    p.js("i => window.__fake.open(i)", 1)
    p.js("i => window.__fake.offer(i)", 1)
    p.settle(6)
    perf = p.js("performance.now()")
    p.wall_jump(200000)                                 # system sleep: wall clock past the deadline, no timer fired
    check(name, "simulated sleep left performance.now() and the attempt untouched",
          abs(p.js("performance.now()") - perf) < 50 and p.js("rdjRetry && rdjRetry.phase") == "connecting")
    p.js("i => window.__fake.ice(i, 'connected')", 1)
    engine.admit(engine.tokens[-1])
    p.advance(2200)
    f = p.fake()
    check(name, "an establishment after the deadline is never adopted", p.js("rdjRetry") is None
          and p.js("rdjLastSequence") is None and p.button()["action"] != "Disconnect", p.button())
    check(name, "the late replacement is released", released(f, 1, 1), f)
    check(name, "no unhandled rejection", not f["unhandled"] and not p.errors, (f["unhandled"], p.errors))
    p.close()


def deadline_sleep_while_waiting(browser, html):
    name = "deadline:sleep-while-waiting"
    engine, mode, p = lost_and_waiting(browser, html, name, 1100)
    mode["answer"] = RETRY
    p.wall_jump(200000)
    p.advance(1100)
    check(name, "a delayed re-ask after sleep never mints a token", len(engine.tokens) == 1
          and p.js("rdjRetry") is None and p.button()["status"] == "Reconnect failed", p.button())
    p.close()


def deadline_late_token(browser, html):
    name = "deadline:late-token"
    engine, mode, p = lost_and_waiting(browser, html, name, 144500)
    mode["answer"] = RETRY
    p.hold_tokens = True
    p.advance(600)
    check(name, "replacement token request in flight", len(p.held) == 1)
    p.advance(5200)
    check(name, "deadline ends the sequence while the token is pending", p.js("rdjRetry") is None)
    p.hold_tokens = False
    p.release_held()                                    # the token arrives late
    p.advance(2000)
    check(name, "a late token response opens nothing", len(p.fake()["ws"]) == 1 and not p.owns(), p.fake())
    p.close()


def deadline_late_microphone(browser, html):
    name = "deadline:late-microphone"
    engine, mode, p = lost_and_waiting(browser, html, name, 144500)
    mode["answer"] = RETRY
    p.advance(600)
    p.js("window.__fake.gumHold = true")
    p.js("i => window.__fake.open(i)", 1)
    p.js("i => window.__fake.offer(i)", 1)
    p.settle(6)
    p.advance(5200)
    check(name, "deadline ends the sequence while the microphone prompt is pending", p.js("rdjRetry") is None)
    p.js("window.__fake.releaseGum()")
    p.settle(6)
    f = p.fake()
    check(name, "the late microphone stream is stopped, no answer, nothing revived", f["liveMic"] == 0
          and f["liveOutput"] == 0 and f["openCtxs"] == 0 and not f["ws"][1]["answer"] and not p.owns(), f)
    p.close()


def deadline_successor_after_cancel(browser, html):
    name = "deadline:successor-after-cancel"
    engine, mode, p = lost_and_waiting(browser, html, name, 144500)
    mode["answer"] = RETRY
    p.advance(600)                                      # attempt timer capped at T+150 s
    p.js("i => window.__fake.open(i)", 1)
    p.js("i => window.__fake.offer(i)", 1)
    p.settle(6)
    p.advance(1000)
    p.click_connect()                                   # Cancel reconnect
    p.advance(500)
    p.click_connect()                                   # an operator Connect
    p.establish(2)
    p.advance(12000, step=1000)                         # past the old attempt timer and the old deadline
    f = p.fake()
    check(name, "stale sequence timers never touch the successor", p.button()["action"] == "Disconnect"
          and f["ws"][2]["readyState"] == 1 and not f["pcs"][2]["closed"] and f["liveMic"] == 1
          and f["liveOutput"] == 1 and p.js("rdjAttemptId") == engine.tokens[-1], (p.button(), f))
    p.close()


def deadline_engine_turnover(browser, html):
    name = "deadline:engine-turnover"
    engine, mode, p = lost_and_waiting(browser, html, name, 1100)
    engine.instance = "eng-2"                           # the engine restarted meanwhile
    mode["answer"] = {"decision": "stop", "reason": "session_not_recognized"}
    p.advance(1100)
    check(name, "an engine turnover stops the sequence", p.js("rdjRetry") is None and len(engine.tokens) == 1)
    p.advance(3500)
    p.click_connect()
    p.establish(1)
    p.advance(160000, step=2000)                        # well past every timer of the old sequence
    f = p.fake()
    check(name, "the successor survives every stale timer", p.button()["action"] == "Disconnect"
          and f["ws"][1]["readyState"] == 1 and not f["pcs"][1]["closed"] and f["liveMic"] == 1, (p.button(), f))
    p.close()


def main():
    """argv: <render dir> [scenario-name filter] [page filter: remote_dj|console]"""
    import time as _time
    render = Path(sys.argv[1])
    only = sys.argv[2] if len(sys.argv) > 2 else ""
    only_page = sys.argv[3] if len(sys.argv) > 3 else ""
    pages = {"remote_dj": (render / "remote_dj_page.html").read_text(),
             "console": (render / "console_page.html").read_text()}
    scenarios = [scenario_manual_connect_and_leave_page, scenario_native_prompt_and_disconnect,
                 scenario_auto_reconnect_success, scenario_policy_stops, scenario_cancel,
                 scenario_bounded_failures, scenario_establish_timeout, scenario_fail_closed_policy,
                 scenario_terminal_events, scenario_late_media_and_tokens, scenario_unstable_replacement,
                 scenario_observer_and_tabs, scenario_lifecycle, scenario_calibration,
                 scenario_setup_failures, scenario_stale_setup_failure, scenario_deadline]
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for page_name, html in pages.items():
            if only_page and page_name != only_page:
                continue
            for scenario in scenarios:
                if only and only not in scenario.__name__:
                    continue
                started = _time.monotonic()
                try:
                    scenario(browser, html)
                except Exception as exc:      # noqa: BLE001
                    line = f"FAIL [{page_name}:{scenario.__name__}] raised {type(exc).__name__}: {exc}"
                    failures.append(line)
                    print(line, flush=True)
                print(f"---- {page_name}:{scenario.__name__} {(_time.monotonic() - started):.1f}s", flush=True)
        browser.close()
    print(f"\n{len(results)} passed, {len(failures)} failed", flush=True)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
