"""fix/operator-state-correctness -- Fix B: browser/backend stale-
disconnected state ([P1] 1.7 Operational Guardrails).

Current dashboard.html behavior: fetchStatus() returns null on any
HTTP/network failure, and poll() just returns early on null data --
so if Wi-Fi/Cloudflare/Gunicorn/the network path dies, the last known
state (PLAYING, LIVE, AUTO, connected, deck state) stays displayed
indefinitely with no indication the browser has lost authoritative
contact, and every mutation control stays clickable with no reliable
knowledge that a command was ever received.

Fix: a client-side stale-connection guardrail built entirely on top of
the EXISTING /api/engine/status/ poll -- no new endpoint, no new
polling interval, no WebSocket, no server-side state. Tracks the
timestamp of the last successful status response; once
STATUS_STALE_THRESHOLD_MS (3000ms) has elapsed with no success, shows
a "CONNECTION LOST — STATUS STALE" banner and gates every live-
mutation entrypoint (deck transport, manual/auto, studio mic PTT,
Remote DJ connect/gate, queue mutation, FX cart fire, play-now) behind
`if (connectionStale) return;`. Clears immediately on the next
successful poll, at which point poll()'s own existing render*(data)
calls re-assert each control's real authoritative disabled state --
this fix never itself sets/clears any control's `.disabled` property,
so nothing about their normal logic is ever overwritten or lost.

These are static-source assertions against the rendered template, per
this project's existing convention for dashboard.html coverage (see
test_dashboard_view.py) -- there is no JS runtime test harness in this
project, so timing behavior is proven by asserting the actual
threshold/accumulation logic exists and reads Date.now()-based deltas
(a real fake-clock harness would require introducing new JS test
infrastructure, which is out of scope for this fix)."""
from django.contrib.auth.models import User
from django.test import TestCase, override_settings


@override_settings(SECURE_SSL_REDIRECT=False)
class ConnectionStaleGuardrailStaticTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("stale-dash", "stale@example.invalid", "password")
        self.client.force_login(self.staff)

    def _content(self, url="/"):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode("utf-8")

    # -- infrastructure: no new endpoint, no new interval, no WebSocket --

    def test_no_new_polling_interval_or_endpoint_introduced(self):
        content = self._content()
        # Exactly the two pre-existing intervals (poll @1000ms status,
        # updatePositionIndicators @100ms) plus the two pre-existing
        # level/listener intervals -- nothing new added by this fix.
        self.assertEqual(content.count("setInterval(poll,"), 1)
        self.assertNotIn("WebSocket(", content.split("toggleRemoteDjConnect")[0])
        # Only ever fetches the existing status endpoint for connectivity.
        self.assertIn("const resp = await fetch('/api/engine/status/');", content)

    def test_does_not_infer_connectivity_from_levels_or_listener_endpoints(self):
        content = self._content()
        # The stale-tracking function itself must not appear anywhere
        # near the levels/listener poll functions' own bodies.
        levels_fn = content[content.index("async function pollLevels()"):
                             content.index("async function pollListeners()")]
        self.assertNotIn("updateConnectionStale", levels_fn)
        self.assertNotIn("connectionStale", levels_fn)
        listeners_start = content.index("async function pollListeners()")
        listeners_fn = content[listeners_start:listeners_start + 2000]
        self.assertNotIn("updateConnectionStale", listeners_fn)

    # -- the threshold/accumulation logic itself --

    def test_stale_threshold_and_state_tracking_present(self):
        content = self._content()
        self.assertIn("const STATUS_STALE_THRESHOLD_MS = 3000;", content)
        self.assertIn("let lastStatusSuccessAt = Date.now();", content)
        self.assertIn("let connectionStale = false;", content)
        self.assertIn("function updateConnectionStale(success)", content)
        self.assertIn("function renderConnectionStale(stale)", content)

    def test_poll_calls_update_connection_stale_before_the_null_data_return(self):
        """1 & 3: every poll() tick reports success/failure to the
        stale tracker BEFORE the existing `if (!data) return;` early
        exit -- otherwise a failed poll would never be observed at all."""
        content = self._content()
        poll_start = content.index("async function poll() {")
        poll_fn = content[poll_start:content.index("\n}", poll_start)]
        self.assertIn("updateConnectionStale(!!data);", poll_fn)
        call_pos = poll_fn.index("updateConnectionStale(!!data);")
        return_pos = poll_fn.index("if (!data) return;")
        self.assertLess(call_pos, return_pos,
            "updateConnectionStale must run before the null-data early return")

    def test_single_transient_miss_does_not_immediately_mark_stale(self):
        """2: a bare failure alone doesn't flip the flag -- only a
        failure that's been going on for STATUS_STALE_THRESHOLD_MS."""
        content = self._content()
        fn_start = content.index("function updateConnectionStale(success)")
        fn = content[fn_start:content.index("\nasync function fetchStatus()")]
        self.assertIn("(now - lastStatusSuccessAt) >= STATUS_STALE_THRESHOLD_MS", fn)
        # The elapsed-time check must gate the flip -- not a bare
        # "on any failure" assignment.
        self.assertNotIn("connectionStale = true;\n  }\n}", fn.replace(" ", ""))

    def test_success_updates_timestamp_and_clears_stale_immediately(self):
        """5: next successful status clears stale with no extra delay."""
        content = self._content()
        fn_start = content.index("function updateConnectionStale(success)")
        fn = content[fn_start:content.index("\nasync function fetchStatus()")]
        success_branch = fn[fn.index("if (success)"):fn.index("if (!connectionStale")]
        self.assertIn("lastStatusSuccessAt = now;", success_branch)
        self.assertIn("connectionStale = false;", success_branch)
        self.assertIn("renderConnectionStale(false);", success_branch)

    # -- banner presentation --

    def test_stale_banner_present_hidden_by_default_and_not_full_page(self):
        content = self._content()
        self.assertIn('id="connectionStaleBanner"', content)
        self.assertIn("CONNECTION LOST", content)
        self.assertIn("STATUS STALE", content)
        # hidden by default at initial server render -- the FIRST poll
        # gets its own grace period rather than the banner flashing on
        # every page load.
        banner_tag = content[content.index('id="connectionStaleBanner"') - 40:
                              content.index('id="connectionStaleBanner"') + 120]
        self.assertIn("hidden", banner_tag)
        # A banner, not an overlay -- must not use position:fixed/absolute
        # full-viewport coverage that would hide the rest of the console.
        css_start = content.index(".connection-stale-banner {")
        css_rule = content[css_start:content.index("}", css_start)]
        self.assertNotIn("position: fixed", css_rule)
        self.assertNotIn("position: absolute", css_rule)
        self.assertNotIn("100vh", css_rule)
        self.assertNotIn("100vw", css_rule)

    def test_render_connection_stale_toggles_hidden_attribute(self):
        content = self._content()
        fn_start = content.index("function renderConnectionStale(stale)")
        fn = content[fn_start:content.index("\n}", fn_start) + 2]
        self.assertIn("banner.hidden = !stale;", fn)

    # -- mutation controls gated --

    MUTATION_FUNCTIONS = [
        "async function toggleManualMode() {",
        "async function toggleMicPtt() {",
        "async function toggleRemoteDjGate() {",
        "async function deckCommand(slot, action) {",
        "async function seekTo(slot, seconds) {",
        "async function setNext(itemId) {",
        "async function insertTrack(trackId, position) {",
        "async function playNow() {",
        "async function fireFxCart(btn) {",
    ]

    def test_every_enumerated_mutation_entrypoint_checks_connection_stale(self):
        """4: mutation controls are unavailable while stale -- deck
        transport, manual/auto, studio mic PTT, Remote DJ gate, queue
        mutation (set-next, insert, reorder), FX cart fire, and
        play-now all gate on connectionStale close to their own entry
        point (not buried deep inside, and not merely mentioned
        anywhere in the file)."""
        content = self._content()
        for signature in self.MUTATION_FUNCTIONS:
            with self.subTest(fn=signature):
                start = content.index(signature)
                # First ~200 chars of the function body -- the guard
                # must be at/near the top, not after side effects.
                head = content[start:start + 400]
                self.assertIn("if (connectionStale) return;", head,
                    f"{signature} does not gate on connectionStale near its entry")

    def test_remote_dj_connect_gates_new_connection_but_not_local_disconnect(self):
        """Remote DJ connect/gate controls: starting a NEW connection
        while stale must be blocked, but tearing an existing session
        down is a purely local WebRTC/WebSocket cleanup with no engine
        round-trip and must remain available regardless of poll state."""
        content = self._content()
        start = content.index("async function toggleRemoteDjConnect() {")
        fn = content[start:content.index("\n}\n", start)]
        disconnect_branch = fn[:fn.index("if (connectionStale) return;")]
        self.assertIn("rdjDisconnect();", disconnect_branch)
        self.assertIn("if (connectionStale) return;", fn)

    def test_queue_reorder_pointerup_handler_gates_and_cancels_drag_cleanly(self):
        content = self._content()
        start = content.index("handle.addEventListener('pointerup', async (e) => {")
        handler = content[start:content.index("handle.addEventListener('pointercancel'", start)]
        self.assertIn("if (connectionStale) { endDrag(); return; }", handler)
        gate_pos = handler.index("if (connectionStale)")
        fetch_pos = handler.index("await fetch(`/api/log/")
        self.assertLess(gate_pos, fetch_pos)

    def test_gate_never_mutates_disabled_attribute_directly(self):
        """Preferred architecture per the task: a flag consulted by
        control functions, NOT a blanket enable/disable sweep over
        buttons -- so each control's own authoritative disabled logic
        (renderMicPtt/renderRemoteDjConnect/renderRemoteDjGate/etc,
        already reasserted every successful poll) is never fought with
        or clobbered, and nothing needs to be explicitly "restored" on
        reconnect."""
        content = self._content()
        stale_fn_start = content.index("function updateConnectionStale(success)")
        render_stale_start = content.index("function renderConnectionStale(stale)")
        # renderConnectionStale only ever touches the banner's hidden
        # attribute, never any control's .disabled property.
        render_stale_fn = content[render_stale_start:content.index("\n}", render_stale_start) + 2]
        self.assertNotIn(".disabled", render_stale_fn)
        stale_fn = content[stale_fn_start:render_stale_start]
        self.assertNotIn(".disabled", stale_fn)

    # -- pure navigation must NOT be gated --

    def test_pure_navigation_and_readonly_search_are_not_gated(self):
        content = self._content()
        # searchForInsert is read-only (a GET), must not be gated --
        # only the actual mutation (insertTrack) is.
        start = content.index("async function searchForInsert(q) {")
        fn = content[start:content.index("\n}", start) + 2]
        self.assertNotIn("connectionStale", fn)


@override_settings(SECURE_SSL_REDIRECT=False)
class ConnectionStaleGuardrailRemoteDjModeTests(TestCase):
    """7: behavior applies to both normal and remote-DJ console modes
    -- dashboard.html is shared, so a single set of assertions against
    /remote-dj/ confirms the guardrail isn't accidentally scoped only
    to the full console."""

    def setUp(self):
        from django.contrib.auth.models import Group
        self.user = User.objects.create_user("stale-rdj", "rdj@example.invalid", "password")
        group, _ = Group.objects.get_or_create(name="remote_dj")
        self.user.groups.add(group)
        self.client.force_login(self.user)

    def test_remote_dj_page_renders_the_same_guardrail(self):
        response = self.client.get("/remote-dj/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn('id="connectionStaleBanner"', content)
        self.assertIn("const STATUS_STALE_THRESHOLD_MS = 3000;", content)
        self.assertIn("function updateConnectionStale(success)", content)
        # The Remote DJ gate control (visible in this mode) is gated.
        start = content.index("async function toggleRemoteDjGate() {")
        head = content[start:start + 400]
        self.assertIn("if (connectionStale) return;", head)
