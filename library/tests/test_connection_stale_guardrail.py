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
        # The exact live statement, not a loose substring -- this
        # fix's own explanatory comments legitimately mention
        # "setInterval(poll," in prose, which a bare substring count
        # would also (mis)count.
        self.assertEqual(content.count("setInterval(poll, 1000);"), 1)
        self.assertNotIn("WebSocket(", content.split("toggleRemoteDjConnect")[0])
        # Only ever fetches the existing status endpoint for connectivity.
        self.assertIn("const resp = await fetch('/api/engine/status/');", content)

    def test_does_not_infer_connectivity_from_levels_or_listener_endpoints(self):
        content = self._content()
        # The stale-tracking functions must not appear anywhere near
        # the levels/listener poll functions' own bodies.
        levels_fn = content[content.index("async function pollLevels()"):
                             content.index("async function pollListeners()")]
        self.assertNotIn("checkConnectionStaleness", levels_fn)
        self.assertNotIn("markStatusSuccess", levels_fn)
        self.assertNotIn("connectionStale", levels_fn)
        listeners_start = content.index("async function pollListeners()")
        listeners_fn = content[listeners_start:listeners_start + 2000]
        self.assertNotIn("checkConnectionStaleness", listeners_fn)
        self.assertNotIn("markStatusSuccess", listeners_fn)

    # -- the threshold/accumulation logic itself --

    def test_stale_threshold_and_state_tracking_present(self):
        content = self._content()
        self.assertIn("const STATUS_STALE_THRESHOLD_MS = 3000;", content)
        self.assertIn("let lastStatusSuccessAt = Date.now();", content)
        self.assertIn("let connectionStale = false;", content)
        self.assertIn("function checkConnectionStaleness()", content)
        self.assertIn("function markStatusSuccess()", content)
        self.assertIn("function renderConnectionStale(stale)", content)

    def test_stale_age_check_runs_before_awaiting_fetch_status(self):
        """Timing-hole fix: staleness must be evaluated from ELAPSED
        TIME at the start of every poll() tick, BEFORE `await
        fetchStatus()` -- not from a failure callback reached only
        after that await resolves. A blackholed/hanging request never
        resolves and so never reaches any code after the await; if the
        stale check lived there, an indefinitely-pending request could
        suppress detection forever even though setInterval(poll, 1000)
        keeps firing fresh ticks every second. Checking synchronously
        at the top of poll(), before the await, guarantees every tick
        -- pending fetch or not -- re-evaluates elapsed time."""
        content = self._content()
        poll_start = content.index("async function poll() {")
        poll_fn = content[poll_start:content.index("\n}", poll_start)]
        self.assertIn("checkConnectionStaleness();", poll_fn)
        self.assertIn("await fetchStatus();", poll_fn)
        check_pos = poll_fn.index("checkConnectionStaleness();")
        await_pos = poll_fn.index("await fetchStatus();")
        self.assertLess(check_pos, await_pos,
            "checkConnectionStaleness() must run BEFORE `await fetchStatus()`, "
            "so a hung/pending request cannot suppress stale detection")

    def test_success_reported_only_after_fetch_resolves_with_data(self):
        """markStatusSuccess() must only run once fetchStatus() has
        actually resolved with real data -- never speculatively, and
        never for a null/failed result."""
        content = self._content()
        poll_start = content.index("async function poll() {")
        poll_fn = content[poll_start:content.index("\n}", poll_start)]
        self.assertIn("if (data) markStatusSuccess();", poll_fn)
        mark_pos = poll_fn.index("if (data) markStatusSuccess();")
        await_pos = poll_fn.index("await fetchStatus();")
        return_pos = poll_fn.index("if (!data) return;")
        self.assertLess(await_pos, mark_pos)
        self.assertLess(mark_pos, return_pos)

    def test_single_transient_miss_does_not_immediately_mark_stale(self):
        """2: a bare failure alone doesn't flip the flag -- only a
        failure that's been going on for STATUS_STALE_THRESHOLD_MS.
        checkConnectionStaleness() only ever SETS stale (based on
        elapsed time); it is never told about a failure directly."""
        content = self._content()
        fn_start = content.index("function checkConnectionStaleness()")
        fn = content[fn_start:content.index("\n}", fn_start) + 2]
        self.assertIn("Date.now() - lastStatusSuccessAt >= STATUS_STALE_THRESHOLD_MS", fn)
        self.assertIn("if (connectionStale) return;", fn)

    def test_success_updates_timestamp_and_clears_stale_immediately(self):
        """5: next successful status clears stale with no extra delay,
        and refreshes the timestamp checkConnectionStaleness() reads
        on every subsequent tick."""
        content = self._content()
        fn_start = content.index("function markStatusSuccess()")
        fn = content[fn_start:content.index("\n}", fn_start) + 2]
        self.assertIn("lastStatusSuccessAt = Date.now();", fn)
        self.assertIn("connectionStale = false;", fn)
        self.assertIn("renderConnectionStale(false);", fn)

    def test_failure_leaves_last_success_timestamp_untouched(self):
        """Explicit invariant from the correction: on a failed
        response, lastStatusSuccessAt must NOT be touched -- staleness
        is purely a function of elapsed time since the last real
        success, never reset/extended by a failure itself."""
        content = self._content()
        poll_start = content.index("async function poll() {")
        poll_fn = content[poll_start:content.index("\n}", poll_start)]
        # poll() itself never assigns the timestamp directly -- the
        # only write path is inside markStatusSuccess(), reached only
        # via `if (data) markStatusSuccess();`. (The module-scope
        # `let lastStatusSuccessAt = Date.now();` seed and the
        # assignment inside markStatusSuccess() are the two legitimate
        # occurrences of this exact text elsewhere in the file.)
        self.assertNotIn("lastStatusSuccessAt = Date.now();", poll_fn)
        self.assertNotIn("lastStatusSuccessAt =", poll_fn)

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
        reconnect. The presentational stale-CSS-class toggle is the
        one exception allowed to touch DOM state directly, and it only
        ever touches the banner's `hidden` attribute and
        #consoleRoot's class list -- never any control's own
        `.disabled` property."""
        content = self._content()
        render_stale_start = content.index("function renderConnectionStale(stale)")
        check_fn_start = content.index("function checkConnectionStaleness()")
        mark_fn_start = content.index("function markStatusSuccess()")
        render_stale_fn = content[render_stale_start:content.index("\n}", render_stale_start) + 2]
        self.assertNotIn(".disabled", render_stale_fn)
        self.assertIn("classList.toggle('connection-stale', stale)", render_stale_fn)
        check_fn = content[check_fn_start:content.index("\n}", check_fn_start) + 2]
        mark_fn = content[mark_fn_start:content.index("\n}", mark_fn_start) + 2]
        self.assertNotIn(".disabled", check_fn)
        self.assertNotIn(".disabled", mark_fn)

    # -- presentational stale treatment (CSS-only, on #consoleRoot) --

    def test_console_root_has_stable_id_for_the_stale_class_toggle(self):
        content = self._content()
        self.assertIn('id="consoleRoot"', content)

    def test_render_connection_stale_toggles_class_on_console_root(self):
        content = self._content()
        fn_start = content.index("function renderConnectionStale(stale)")
        fn = content[fn_start:content.index("\n}", fn_start) + 2]
        self.assertIn("document.getElementById('consoleRoot')", fn)
        self.assertIn("classList.toggle('connection-stale', stale)", fn)

    STALE_DIMMED_SELECTORS = [
        "#consoleRoot.connection-stale .ops-btn:not(#remoteDjConnectBtn)",
        "#consoleRoot.connection-stale .playnow-btn:not(.ops-btn)",
        "#consoleRoot.connection-stale .deck-transport-btn",
        "#consoleRoot.connection-stale .qt-next-btn",
        "#consoleRoot.connection-stale .qt-drag-handle",
        "#consoleRoot.connection-stale .qir-btn",
        "#consoleRoot.connection-stale .fx-cart",
    ]

    def test_stale_css_dims_every_enumerated_control_category(self):
        """4 (visual half): manual/auto, studio mic PTT, Remote DJ
        gate, deck transport (both buttons share .deck-transport-btn),
        queue set-next/insert/reorder, FX fire, and play-now all read
        as visibly unavailable while #consoleRoot carries
        .connection-stale."""
        content = self._content()
        for selector in self.STALE_DIMMED_SELECTORS:
            with self.subTest(selector=selector):
                self.assertIn(selector, content)
        # The shared dimmed-rule body itself.
        rule_start = content.index(self.STALE_DIMMED_SELECTORS[0])
        rule = content[rule_start:content.index("}", rule_start)]
        self.assertIn("opacity:", rule)
        self.assertIn("cursor: not-allowed;", rule)
        self.assertIn("pointer-events: none;", rule)

    def test_remote_dj_connect_button_excluded_from_stale_dimming(self):
        """The connect/disconnect button must NOT visually read as
        unavailable while stale -- its disconnect action stays
        functionally live (see toggleRemoteDjConnect's own gate
        placement), so dimming it would misrepresent a control that
        still works."""
        content = self._content()
        rule_start = content.index(self.STALE_DIMMED_SELECTORS[0])
        rule = content[rule_start:content.index("{", rule_start)]
        self.assertIn(":not(#remoteDjConnectBtn)", rule)
        self.assertNotIn("#remoteDjConnectBtn,", rule)

    def test_stale_css_never_sets_the_disabled_property_anywhere(self):
        """The presentational treatment must be pure CSS/opacity, not
        a second mechanism that writes to `.disabled` -- that remains
        exclusively each control's own render*(data) function's job."""
        content = self._content()
        css_start = content.index("#consoleRoot.connection-stale")
        css_end = content.index("pointer-events: none;\n  }", css_start) + len("pointer-events: none;\n  }")
        css_block = content[css_start:css_end]
        self.assertNotIn(".disabled", css_block)
        self.assertNotIn("disabled = true", css_block)
        self.assertNotIn("disabled = false", css_block)

    def test_pure_navigation_and_search_input_not_targeted_by_stale_css(self):
        """fx-toggle-mobile/fx-more (show/hide UI) and the search input
        itself must not be swept up by the dimming rule -- only actual
        engine-mutation controls."""
        content = self._content()
        rule_start = content.index(self.STALE_DIMMED_SELECTORS[0])
        rule = content[rule_start:content.index("{", rule_start)]
        self.assertNotIn("fx-toggle-mobile", rule)
        self.assertNotIn("fx-more", rule)
        self.assertNotIn("queueInsertSearch", rule)
        self.assertNotIn("queueInsertResults", rule)

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
        self.assertIn('id="consoleRoot"', content)
        self.assertIn("const STATUS_STALE_THRESHOLD_MS = 3000;", content)
        self.assertIn("function checkConnectionStaleness()", content)
        self.assertIn("function markStatusSuccess()", content)
        # The Remote DJ gate control (visible in this mode) is gated,
        # both functionally and via the presentational CSS class.
        self.assertIn(
            "#consoleRoot.connection-stale .ops-btn:not(#remoteDjConnectBtn)", content)
        start = content.index("async function toggleRemoteDjGate() {")
        head = content[start:start + 400]
        self.assertIn("if (connectionStale) return;", head)
