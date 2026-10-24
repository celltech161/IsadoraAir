"""Roadmap 2.23 Phase 1 -- Console presentation: server-side contracts.

Covers what the browser matrix (test_console_browser_2_23) cannot prove
by itself:

* the capability flags the Console receives (``console_caps``) for each
  kind of user, evaluated by the real ``authorize()`` with the seeded
  Roles -- and that they are presentation only: hiding a control never
  replaces the endpoint's own enforcement (direct API requests are still
  refused, and no engine command is queued);
* deterministic template contracts for the Phase 1 markup (ops-row order,
  disabled Line In, the single icon boundary, the Start Next slot
  (activated in Phase 3), the single waveform renderer, the honest Eject
  label).

``enqueue_engine_command`` is mocked wherever an endpoint is called, so
nothing here can reach /run/isadoraair/engine_cmd.d.
"""
import json
import re
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from authz.models import Capability, GroupRole, Role, RoleCapability, ScheduleAccessConfig
from library.models import GroupAccess

User = get_user_model()

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "templates" / "library" / "dashboard.html"
ICONS_PATH = Path(__file__).resolve().parents[1] / "templates" / "library" / "_console_icons.html"


def _console_caps(response):
    match = re.search(
        r'<script id="console-caps" type="application/json">(.*?)</script>',
        response.content.decode("utf-8"),
        re.S,
    )
    assert match, "console-caps json_script missing"
    return json.loads(match.group(1))


@override_settings(SECURE_SSL_REDIRECT=False)
class ConsoleCapabilityFlagTests(TestCase):
    """Who is shown deck Eject / Play-Pause / click-seek (D10)."""

    def setUp(self):
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = False
        cfg.save()

        self.superuser = User.objects.create_superuser("c223_su", "su@example.invalid", "pw")
        self.staff = User.objects.create_user("c223_staff", password="pw", is_staff=True)

        # The seeded Remote Host Role (authz 0002/0006), bound to remote_dj.
        self.remote_host = User.objects.create_user("c223_remote_host", password="pw")
        self.remote_host.groups.add(Group.objects.get(name="remote_dj"))

        # A non-staff on-air operator holding the seeded Station
        # Administrator Role through a group -- the real capability path,
        # not the staff/superuser bypass.
        self.operator = User.objects.create_user("c223_operator", password="pw")
        operators = Group.objects.create(name="c223_operators")
        GroupRole.objects.create(group=operators, role=Role.objects.get(name="Station Administrator"))
        GroupAccess.objects.create(
            group=operators, allowed_exact="/\n/remote-dj/", allowed_prefixes="/api/engine/", landing_url="/",
        )
        self.operator.groups.add(operators)

    def test_seeded_roles_match_the_assumed_capability_split(self):
        remote_host = set(
            RoleCapability.objects.filter(role__name="Remote Host").values_list("capability__slug", flat=True)
        )
        self.assertNotIn("playout.control", remote_host)
        self.assertIn("playout.queue_manage", remote_host)
        admin = set(
            RoleCapability.objects.filter(role__name="Station Administrator").values_list(
                "capability__slug", flat=True)
        )
        self.assertIn("playout.control", admin)

    def test_superuser_full_console_gets_deck_transport(self):
        self.client.force_login(self.superuser)
        self.assertEqual(_console_caps(self.client.get("/")), {"playout_control": True, "start_next": True})

    def test_staff_gets_deck_transport_on_both_pages(self):
        self.client.force_login(self.staff)
        self.assertTrue(_console_caps(self.client.get("/"))["playout_control"])
        self.assertTrue(_console_caps(self.client.get("/remote-dj/"))["playout_control"])

    def test_operator_role_holder_gets_deck_transport(self):
        self.client.force_login(self.operator)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(_console_caps(response)["playout_control"])

    def test_remote_host_is_not_shown_deck_transport(self):
        self.client.force_login(self.remote_host)
        response = self.client.get("/remote-dj/")
        self.assertEqual(response.status_code, 200)
        # Phase 3: Start Next (playout.start_next) without deck transport.
        self.assertEqual(_console_caps(response), {"playout_control": False, "start_next": True})

    def test_flag_object_carries_only_the_known_presentation_keys(self):
        self.client.force_login(self.superuser)
        self.assertEqual(set(_console_caps(self.client.get("/"))), {"playout_control", "start_next"})

    @patch("library.views.enqueue_engine_command")
    def test_hidden_controls_are_still_refused_by_the_server(self, mock_enqueue):
        """Presentation is not the boundary: a Remote Host's hand-built
        Eject / Pause / Seek / Studio PTT requests are refused and nothing
        is queued for the engine."""
        self.client.force_login(self.remote_host)
        for url, body in (
            (reverse("library:api-engine-deck-command", args=["A"]), {"action": "eject"}),
            (reverse("library:api-engine-deck-command", args=["B"]), {"action": "pause"}),
            (reverse("library:api-engine-seek"), {"position": 10, "slot": "A"}),
            (reverse("library:api-engine-mic-ptt"), {"active": True}),
        ):
            with self.subTest(url=url):
                response = self.client.post(url, data=body, content_type="application/json")
                self.assertEqual(response.status_code, 403)
        mock_enqueue.assert_not_called()

    @patch("library.views.enqueue_engine_command")
    def test_outside_schedule_window_is_shown_but_refused(self, mock_enqueue):
        """Capability possession is evaluated with SCHEDULE_POLICY_IGNORE for
        display (the remote_dj_page convention); the endpoint applies the real
        schedule policy and refuses a holder with no active assignment."""
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = True
        cfg.save()
        self.client.force_login(self.operator)
        self.assertTrue(_console_caps(self.client.get("/"))["playout_control"])
        response = self.client.post(
            reverse("library:api-engine-deck-command", args=["A"]),
            data={"action": "eject"}, content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("assignment", response.json()["error"].lower())
        mock_enqueue.assert_not_called()

    def test_anonymous_never_reaches_the_console(self):
        for path in ("/", "/remote-dj/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("login", response["Location"])

    def test_contributor_is_not_given_the_console(self):
        contributor = User.objects.create_user("c223_contributor", password="pw")
        contributor.groups.add(Group.objects.get_or_create(name="Contributor")[0])
        self.client.force_login(contributor)
        response = self.client.get("/")
        self.assertNotEqual(response.status_code, 200)
        response = self.client.get("/remote-dj/")
        self.assertNotIn(b'id="console-caps"', response.content)

    def test_missing_capability_row_fails_closed(self):
        Capability.objects.filter(slug="playout.control").delete()
        self.client.force_login(self.operator)
        self.assertFalse(_console_caps(self.client.get("/"))["playout_control"])


@override_settings(SECURE_SSL_REDIRECT=False)
class ConsoleRenderedMarkupTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser("c223_markup_su", "m@example.invalid", "pw")
        self.remote_host = User.objects.create_user("c223_markup_host", password="pw")
        self.remote_host.groups.add(Group.objects.get(name="remote_dj"))

    @staticmethod
    def _ops_row(content):
        return content[content.index('<div class="playnow-bar">'):content.index('<audio id="remoteMonitorAudio"')]

    def test_full_console_ops_row_order(self):
        self.client.force_login(self.superuser)
        row = self._ops_row(self.client.get("/").content.decode("utf-8"))
        ids = re.findall(r'<button[^>]*\bid="([A-Za-z]+Btn)"', row)
        self.assertEqual(
            ids, ["lineInBtn", "micPttBtn", "manualModeBtn", "remoteDjGateBtn", "remoteDjConnectBtn"]
        )

    def test_remote_dj_ops_row_omits_studio_only_controls(self):
        self.client.force_login(self.remote_host)
        row = self._ops_row(self.client.get("/remote-dj/").content.decode("utf-8"))
        ids = re.findall(r'<button[^>]*\bid="([A-Za-z]+Btn)"', row)
        self.assertEqual(ids, ["manualModeBtn", "remoteDjGateBtn", "remoteDjConnectBtn"])

    def test_icon_definitions_render_once_on_both_pages(self):
        for user, path in ((self.superuser, "/"), (self.remote_host, "/remote-dj/")):
            with self.subTest(path=path):
                self.client.force_login(user)
                content = self.client.get(path).content.decode("utf-8")
                self.assertEqual(content.count('class="console-icon-defs"'), 1)
                self.assertEqual(content.count('<symbol id="icon-mic-remote"'), 1)


class ConsoleTemplateContractTests(SimpleTestCase):
    """Deterministic source contracts for the Phase 1 markup."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.template = TEMPLATE_PATH.read_text(encoding="utf-8")
        cls.icons = ICONS_PATH.read_text(encoding="utf-8")

    def _function(self, name):
        # From the declaration to its own closing brace (top-level functions
        # in this template close with "}" at column 0).
        start = self.template.index(f"function {name}(")
        end = self.template.index("\n}", start) + 2
        return self.template[start:end]

    def test_line_in_is_disabled_and_inert(self):
        tag = self.template[self.template.index('id="lineInBtn"') - 80:]
        tag = tag[:tag.index("</button>")]
        self.assertIn(" disabled", tag)
        self.assertIn('aria-disabled="true"', tag)
        self.assertIn("unavailable", tag)
        self.assertNotIn("onclick", tag)
        # Nothing in the page wires it up or sends anything for it.
        self.assertNotIn("getElementById('lineInBtn')", self.template)
        self.assertNotIn('querySelector("#lineInBtn")', self.template)
        self.assertNotIn("line-in/", self.template)

    def test_line_in_is_studio_only(self):
        block = self.template[
            self.template.index('<div class="playnow-bar">'):self.template.index('id="manualModeBtn"')
        ]
        self.assertIn("{% if mode != 'remote_dj' %}", block)
        self.assertLess(block.index("{% if mode != 'remote_dj' %}"), block.index('id="lineInBtn"'))

    def test_icon_boundary_is_one_replaceable_file(self):
        for symbol in ("icon-mic-studio", "icon-mic-remote", "icon-line-in"):
            with self.subTest(symbol=symbol):
                self.assertEqual(self.icons.count(f'<symbol id="{symbol}"'), 1)
                self.assertIn(f'<use href="#{symbol}">', self.template)
                # Artwork lives only in the include, never inline in the page.
                self.assertNotIn(f'<symbol id="{symbol}"', self.template)
        self.assertIn('{% include "library/_console_icons.html" %}', self.template)
        self.assertIn('stroke="currentColor"', self.icons)

    def test_mic_buttons_no_longer_use_a_text_prefix(self):
        row = self.template[
            self.template.index('<div class="playnow-bar">'):self.template.index('<audio id="remoteMonitorAudio"')
        ]
        self.assertNotIn(">Studio Mic:", row)
        self.assertNotIn(">Remote Mic:", row)
        self.assertNotIn("btn.textContent = 'Remote Mic:", self.template)
        self.assertNotIn("btn.textContent = 'Studio Mic:", self.template)
        for fn in ("renderMicPtt", "renderRemoteDjGate"):
            with self.subTest(fn=fn):
                self.assertIn("setOpsMicButton(", self._function(fn))

    def test_mic_states_are_not_colour_only(self):
        helper = self._function("setOpsMicButton")
        self.assertIn("btn.dataset.micState = state;", helper)
        self.assertIn("label.textContent = word;", helper)
        self.assertIn("btn.setAttribute('aria-label', ariaLabel);", helper)
        for state in ("on", "off", "error", "checking", "calibrating", "unavailable", "denied"):
            with self.subTest(state=state):
                self.assertIn(f'[data-mic-state="{state}"]', self.template)

    def test_command_feedback_never_replaces_the_engine_state(self):
        # A refused/failed OFF request while the mic is LIVE must still read
        # LIVE: the feedback layer never touches engine-owned presentation.
        for fn in ("finishControlCommand", "setControlWarning", "clearControlWarning",
                   "renderControlFeedback", "reconcileControlWarning", "postEngineCommand"):
            with self.subTest(fn=fn):
                body = self._function(fn)
                self.assertNotIn("setOpsMicButton", body)
                self.assertNotIn("micState", body)
                self.assertNotIn("aria-label", body)
                self.assertNotIn("'active'", body)
        self.assertIn("btn.classList.toggle('refused', !!fb.warning);", self._function("renderControlFeedback"))
        self.assertIn('<div class="ops-msg" id="opsMsg" role="status" aria-live="polite"></div>', self.template)

    def test_every_engine_command_control_uses_the_guarded_post(self):
        # Codex Phase 1 P2: no unguarded fetch() for these controls.
        for url in ("/api/engine/mic-ptt/", "/api/engine/remote-dj-gate/", "/api/engine/manual-mode/",
                    "/api/engine/seek/", "/api/engine/deck/"):
            with self.subTest(url=url):
                self.assertNotIn(f"fetch('{url}", self.template)
                self.assertNotIn(f"fetch(`{url}", self.template)
        post = self._function("postEngineCommand")
        self.assertIn("} catch (e) {\n    return { kind: 'unconfirmed', text: 'Connection error — command outcome unconfirmed' };", post)
        self.assertNotIn("not sent", self.template)
        for fn, key in (("toggleMicPtt", "'micPttBtn'"), ("toggleRemoteDjGate", "'remoteDjGateBtn'"),
                        ("toggleManualMode", "'manualModeBtn'"), ("deckCommand", "key"), ("seekTo", "key")):
            with self.subTest(fn=fn):
                body = self._function(fn)
                self.assertIn(f"const id = beginControlCommand({key});", body)
                self.assertIn("finishControlCommand(", body)

    def test_only_the_newest_request_may_change_a_control(self):
        finish = self._function("finishControlCommand")
        self.assertIn("if (id !== fb.seq) return false;", finish)
        warn = self._function("setControlWarning")
        self.assertIn("if (fb.warning === warning) clearControlWarning(key);", warn)
        # Status renders keep an in-flight request's busy indication.
        for fn, key in (("renderMicPtt", "micPttBtn"), ("renderRemoteDjGate", "remoteDjGateBtn"),
                        ("renderManualMode", "manualModeBtn")):
            with self.subTest(fn=fn):
                body = self._function(fn)
                self.assertIn(f"if (!isControlInFlight('{key}')) btn.classList.remove('pending');", body)
                self.assertIn(f"reconcileControlWarning('{key}',", body)

    def test_remote_mic_keeps_vu_fill_and_dual_purpose(self):
        gate = self._function("renderRemoteDjGate")
        self.assertIn("if (data.remote_dj_connected) {", gate)
        self.assertIn("'calibrate'", gate)
        self.assertIn("#remoteDjGateBtn::before", self.template)
        self.assertIn("width: var(--remote-dj-vu-pct, 0%);", self.template)

    def test_eject_is_no_longer_labelled_start_next(self):
        self.assertNotIn('title="Start Next"', self.template)
        builder = self._function("buildDeckDOM")
        self.assertIn('aria-label="Eject Deck ${slot}: ${EJECT_DESCRIPTION}"', builder)
        self.assertIn("(no fade)", self.template)

    def test_deck_transport_follows_capability_not_mode(self):
        builder = self._function("buildDeckDOM")
        self.assertIn("const transportButtons = CAN_DECK_TRANSPORT ?", builder)
        self.assertIn("if (CAN_WAVEFORM_SEEK) {", builder)
        self.assertNotIn("if (!IS_REMOTE_DJ) {", builder)
        self.assertIn("const CAN_DECK_TRANSPORT = CONSOLE_CAPS.playout_control === true;", self.template)
        self.assertIn("const CAN_WAVEFORM_SEEK = CAN_DECK_TRANSPORT && !IS_REMOTE_DJ;", self.template)

    def test_deck_command_reports_refusals(self):
        command = self._function("deckCommand")
        self.assertIn("if (connectionStale) return;", command.splitlines()[1])
        self.assertIn("finishControlCommand(key, id, deckCommandOutcome(action, outcome));", command)
        self.assertIn("refused: permission denied (role or schedule)", self._function("postEngineCommand"))
        self.assertIn("renderControlFeedback(`deck-${slot}`);", self._function("buildDeckDOM"))

    def test_single_waveform_renderer_and_no_overlay_marker(self):
        self.assertNotIn("position-line", self.template)
        self.assertNotIn("posLine", self.template)
        canvas_rule = self.template[self.template.index("  .np-waveform canvas {"):]
        canvas_rule = canvas_rule[:canvas_rule.index("}")]
        self.assertIn("height: 62px;", canvas_rule)
        self.assertNotIn("96px", canvas_rule)
        draw = self._function("drawWaveform")
        self.assertIn("ctx.drawImage(layer.canvas, 0, 0);", draw)
        self.assertIn("ctx.strokeStyle = '#ffffff';", draw)
        self.assertEqual(draw.count("ctx.closePath();"), 2)  # top + bottom triangles
        self.assertIn("drawWaveform(slot);", self._function("updatePositionIndicators"))

    def test_one_time_scale_for_markers_playhead_and_seek(self):
        self.assertIn(
            "return waveformState[slot].analysisDuration || deckState[slot].localDuration || 1;",
            self._function("waveformTimeScale"),
        )
        self.assertIn("const pos = pct * waveformTimeScale(slot);", self._function("buildDeckDOM"))
        self.assertIn("const x = (seconds / scale) * w;", self._function("renderWaveformLayer"))
        self.assertIn("let x = (t / scale) * cssW;", self._function("drawWaveform"))

    def test_waveform_redraws_on_resize_and_dpr_change(self):
        self.assertIn("new ResizeObserver(", self.template)
        self.assertIn("waveformResizeObserver.observe(canvas);", self._function("buildDeckDOM"))
        for fn in ("buildDeckDOM", "renderDeckOffline", "renderDeckEmpty"):
            with self.subTest(fn=fn):
                self.assertTrue(self._function(fn).split("\n")[1].strip() == "releaseDeckCanvas(slot);")
        self.assertIn("(resolution: ${window.devicePixelRatio || 1}dppx)", self.template)

    def test_start_next_sits_between_the_decks_for_holders_and_stays_hidden_otherwise(self):
        row = self.template[self.template.index('<div class="decks-row'):]
        row = row[:row.index('<div class="queue-panel')]
        self.assertIn('{% if console_caps.start_next %} has-start-next{% endif %}', row)
        holder, _, other = row.partition("{% else %}")
        # Both branches render the slot between Deck A and Deck B.
        self.assertLess(row.index('id="deck-A"'), row.index('id="startNextSlot"'))
        self.assertLess(row.rindex('id="startNextSlot"'), row.index('id="deck-B"'))
        self.assertIn('id="startNextBtn"', holder)
        self.assertIn('aria-describedby="startNextStatus"', holder)
        self.assertIn('role="status" aria-live="polite"', holder)
        self.assertIn('<div class="start-next-slot" id="startNextSlot" hidden></div>', other)
        self.assertIn(".console-remote_dj .start-next-slot { order: 2; }", self.template)
        self.assertIn(".console-remote_dj .deck-panel.deck-mode-preview { order: 3; }", self.template)

    def test_start_next_is_engine_authoritative_with_no_browser_transport_timing(self):
        self.assertIn("const CAN_START_NEXT = CONSOLE_CAPS.start_next === true;", self.template)
        # One press posts once, plus exactly one retry of the SAME body when the
        # first response was lost (r0117 SN-AUTH-1: the server derives the same
        # request id from the press's client_request_id).
        start_next = self._function("startNext")
        self.assertEqual(self.template.count("postEngineCommand('/api/engine/start-next/'"), 2)
        self.assertEqual(start_next.count("postEngineCommand('/api/engine/start-next/', body)"), 2)
        self.assertIn("if (outcome.kind === 'unconfirmed') {", start_next)
        self.assertIn("const binding = startNextBinding(data);", start_next)
        self.assertIn("if (!binding) return;", start_next)
        self.assertIn("const body = { client_request_id: startNextClientId(), ...binding };", start_next)
        self.assertIn("if (!instance) return null;", self._function("startNextBinding"))
        for fn in ("startNext", "renderStartNext", "startNextResolvePending", "startNextRoute"):
            with self.subTest(fn=fn):
                body = self._function(fn)
                self.assertNotIn("setInterval", body)
                self.assertNotIn("setTimeout", body)
                self.assertNotIn("volume", body)
        # 202 only records the request id; completion comes from the engine.
        self.assertIn("pending.requestId = outcome.payload.request_id;", self._function("startNext"))
        resolve = self._function("startNextResolvePending")
        self.assertIn("block.recent.find(r => r.request_id === p.requestId)", resolve)
        self.assertIn("instance !== p.instance", resolve)

    def test_eject_is_bound_to_the_occurrence_the_deck_shows(self):
        command = self._function("deckCommand")
        self.assertIn("body.expected_log_item_id = deckState[slot].logItemId;", command)
        self.assertIn("if (action === 'eject' && deckState[slot].logItemId) {", command)

    def test_no_new_polling_or_transport_timers(self):
        self.assertEqual(self.template.count("setInterval(poll, 1000);"), 1)
        self.assertEqual(self.template.count("setInterval(updatePositionIndicators, 100);"), 1)
        for fn in ("drawWaveform", "renderWaveformLayer", "updatePositionIndicators", "buildDeckDOM",
                   "setOpsMicButton", "finishControlCommand", "setControlWarning",
                   "renderControlFeedback", "deckCommandOutcome"):
            with self.subTest(fn=fn):
                body = self._function(fn)
                self.assertNotIn("setInterval(", body)
                self.assertNotIn("fetch(", body)


class RemoteDjIdentityTemplateContractTests(SimpleTestCase):
    """Roadmap 2.23 Phase 1B -- deterministic source contracts."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.template = TEMPLATE_PATH.read_text(encoding="utf-8")

    def _function(self, name):
        start = self.template.index(f"function {name}(")
        return self.template[start:self.template.index("\n}", start) + 2]

    def test_ownership_is_this_browsers_attempt_key_never_user_identity(self):
        link = self._function("rdjLocalLink")
        self.assertIn("if (!rdjPc) return 'none';", link)
        self.assertIn("return owner.browser_key === rdjBrowserKey ? 'owner' : 'stale';", link)
        self.assertNotIn("is_current_user", link)
        # Ownership is never inferred from a local peer alone (second-review
        # P2): every owner-only control path goes through rdjLocalLink().
        connect = self._function("renderRemoteDjConnect")
        self.assertIn("const link = rdjLocalLink(data);", connect)
        self.assertNotIn("const weAreConnected = !!rdjPc;", connect)
        gate = self._function("renderRemoteDjGate")
        # Roadmap 2.17A: the gate needs the verified owner AND the complete
        # session binding (rdjGateBinding starts from rdjMayGate).
        self.assertIn("const mayGate = rdjGateBinding(data) !== null;", gate)
        self.assertIn("btn.disabled = !mayGate;", gate)
        self.assertNotIn("!!rdjPc", gate)
        toggle = self._function("toggleRemoteDjGate")
        self.assertIn("const binding = rdjGateBinding(state);\n    if (!binding) return;", toggle)
        self.assertIn("if (!rdjMayGate(data) || !rdjAttemptId || !rdjControlKey) return null;",
                      self._function("rdjGateBinding"))
        may = self._function("rdjMayGate")
        self.assertIn("return rdjLocalLink(data) === 'owner';", may)
        self.assertIn("const thisBrowser = link === 'owner';", self._function("rdjOwnerPresentation"))

    def test_no_raw_identifier_is_read_from_the_owner_block(self):
        for raw in ("remote_dj_owner.user_id", "remote_dj_owner.attempt_id", "owner.user_id", "owner.attempt_id"):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, self.template)

    def test_remote_mic_requests_carry_the_2_17a_session_binding(self):
        toggle = self._function("toggleRemoteDjGate")
        self.assertIn("const body = { active: desiredActive, sequence: rdjGateSeq, ...binding };", toggle)
        # Exactly one same-body retry, only for a lost response.
        self.assertEqual(toggle.count("postEngineCommand('/api/engine/remote-dj-gate/', body)"), 2)
        self.assertIn("if (outcome.kind === 'unconfirmed' && controlFb('remoteDjGateBtn').seq === id\n"
                      "        && rdjGateStillBound(binding)) {", toggle)
        self.assertNotIn("{ active: desiredActive })", toggle)
        binding = self._function("rdjGateBinding")
        self.assertIn("return { attempt_id: rdjAttemptId, control_key: rdjControlKey, engine_instance: instance };",
                      binding)

    def test_control_key_lives_only_in_page_memory_for_one_attempt(self):
        self.assertIn("rdjControlKey = null;", self._function("rdjDisconnect"))
        connect = self._function("toggleRemoteDjConnect")
        self.assertIn("/^[0-9a-f]{64}$/.test(tokenResponse.control_key)", connect)
        self.assertIn("if (!token || !rdjAttemptId || !rdjControlKey ||", connect)
        self.assertLess(connect.index("rdjControlKey = null;"), connect.index("fetch('/api/remote-dj/token/'"))
        uses = [line.strip() for line in self.template.splitlines() if "rdjControlKey" in line]
        for line in uses:
            with self.subTest(line=line):
                for sink in ("localStorage", "sessionStorage", "title", "textContent", "innerHTML",
                             "console.", "setAttribute", "rdjWs.send", "dataset"):
                    self.assertNotIn(sink, line)

    def test_browser_key_is_derived_locally_and_dropped_on_disconnect(self):
        self.assertIn("window.crypto.subtle.digest('SHA-256'", self._function("rdjDeriveBrowserKey"))
        self.assertIn("rdjBrowserKey = null;", self._function("rdjDisconnect"))

    def test_status_polling_is_single_flight_and_bounded(self):
        # Fourth review: at most one outstanding status request, bounded by a
        # timeout derived from the stale threshold; each outcome applied once.
        self.assertIn("const STATUS_FETCH_TIMEOUT_MS = 2 * STATUS_STALE_THRESHOLD_MS;", self.template)
        poll = self._function("poll")
        self.assertLess(poll.index("checkConnectionStaleness();"),
                        poll.index("if (statusInFlightSeq !== 0) return;"))
        self.assertLess(poll.index("if (statusInFlightSeq !== 0) return;"), poll.index("const seq = ++statusPollSeq;"))
        timeout = poll[poll.index("const timer = setTimeout("):poll.index("STATUS_FETCH_TIMEOUT_MS);")]
        self.assertIn("statusAbortController.abort();", timeout)
        self.assertIn("if (!claimStatusOutcome(seq)) return;", timeout)
        self.assertLess(poll.index("await fetchStatus();"), poll.index("if (!claimStatusOutcome(seq)) return;", poll.index("await fetchStatus();")))
        claim = self._function("claimStatusOutcome")
        self.assertIn("if (seq <= latestStatusOutcomeSeq) return false;", claim)
        self.assertIn("statusInFlightSeq = 0;", claim)
        self.assertIn("signal: statusAbortSignal()", self._function("fetchStatus"))

    def test_only_a_provably_newer_engine_snapshot_is_trusted(self):
        poll = self._function("poll")
        after_claim = poll[poll.index("await fetchStatus();"):]
        self.assertLess(after_claim.index("const verdict = data ? rdjAdmitSnapshot(data) : 'failed';"),
                        after_claim.index("if (data) markStatusSuccess();"))
        self.assertIn("latestSnapshotTrusted = verdict === 'current';", poll)
        self.assertLess(poll.index("if (verdict === 'older') return;"), poll.index("if (data) markStatusSuccess();"))
        admit = self._function("rdjAdmitSnapshot")
        self.assertIn("if (!rdjValidRevision(rev)) return 'unversioned';", admit)
        self.assertIn("if (retiredEngineInstances.has(rev.instance)) return 'older';", admit)
        self.assertIn("if (rev.seq > acceptedRevision.seq) {", admit)
        self.assertIn("return 'provisional';", admit)
        # Nothing about HTTP request order feeds the snapshot decision.
        self.assertNotIn("seq ===", admit.replace("rev.seq === ", ""))
        self.assertNotIn("statusPollSeq", admit)
        self.assertNotIn("latestStatusOutcomeSeq", admit)

    def test_ownership_needs_a_trusted_snapshot_and_legacy_never_gates(self):
        link = self._function("rdjLocalLink")
        self.assertIn("if (connectionStale || !latestStatusOk) return 'unverified';", link)
        self.assertIn("if (!latestSnapshotTrusted) return 'unverified';", link)
        self.assertLess(link.index("!latestStatusOk"), link.index("remote_dj_owner_supported === false"))
        self.assertLess(link.index("if (!latestSnapshotTrusted)"), link.index("owner.browser_key === rdjBrowserKey"))
        self.assertIn("return rdjLocalLink(data) === 'owner';", self._function("rdjMayGate"))

    def test_identity_is_withheld_while_status_is_stale(self):
        self.assertIn("renderRemoteDjConnect(window.__rdjLastEngineState || {});",
                      self._function("renderConnectionStale"))
        render = self._function("renderRemoteDjConnect")
        branch = "if ((connectionStale || !latestStatusOk) && (weAreConnected || someoneElseConnected)) {"
        stale = render[render.index(branch):]
        self.assertIn("'Status unavailable'", stale[:stale.index("return;")])
        self.assertLess(render.index(branch), render.index("rdjOwnerPresentation(data"))

    def test_no_new_polling_loop_or_endpoint(self):
        self.assertEqual(self.template.count("setInterval(poll, 1000);"), 1)
        self.assertNotIn("/api/remote-dj/owner", self.template)
        self.assertNotIn("/api/engine/remote-dj-owner", self.template)
