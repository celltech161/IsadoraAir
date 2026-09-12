"""r0074 Remote DJ fixed-size, two-line dashboard button contracts.

These are deterministic source/markup contracts, matching the established
dashboard tests without adding a browser automation framework solely for
pixel measurement.
"""
from pathlib import Path

from django.test import SimpleTestCase


class RemoteDJDashboardQualityButtonTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.template = (
            Path(__file__).parents[1] / "templates/library/dashboard.html"
        ).read_text(encoding="utf-8")
        cls.control_row = cls.template[
            cls.template.index('<div class="playnow-bar">'):
            cls.template.index('<audio id="remoteMonitorAudio"')
        ]
        cls.renderer = cls.template[
            cls.template.index("function renderRemoteDjConnect"):
            cls.template.index("function renderRemoteDjGate")
        ]
        cls.toggle = cls.template[
            cls.template.index("async function toggleRemoteDjConnect"):
            cls.template.index("function rdjDisconnect")
        ]

    def test_standalone_r0073_quality_indicator_is_fully_removed(self):
        self.assertNotIn("remoteDjQualityBadge", self.template)
        self.assertNotIn("rdj-quality-badge", self.template)
        self.assertNotIn("function renderRemoteDjQuality", self.template)
        self.assertNotIn("Remote Link:", self.template)

    def test_existing_real_button_contains_separate_action_and_status_lines(self):
        self.assertIn('<button class="playnow-btn ops-btn" id="remoteDjConnectBtn"', self.control_row)
        self.assertIn('id="remoteDjConnectAction" class="rdj-connect-action"', self.control_row)
        self.assertIn('id="remoteDjConnectStatus" class="rdj-connect-status rdj-status-offline"', self.control_row)
        self.assertLess(
            self.control_row.index("remoteDjConnectAction"),
            self.control_row.index("remoteDjConnectStatus"),
        )

    def test_disconnected_state_is_connect_offline(self):
        self.assertIn("'Connect', 'Offline', 'offline', 'Connect Remote DJ — Offline'", self.renderer)

    def test_connecting_state_is_connecting_negotiating(self):
        self.assertIn("'Connecting…', 'Negotiating', 'negotiating'", self.template)
        self.assertIn("if (rdjConnecting)", self.renderer)

    def test_good_state_is_disconnect_good(self):
        self.assertIn("good: ['Good', 'good']", self.renderer)
        self.assertIn("const action = weAreConnected ? 'Disconnect'", self.renderer)

    def test_fair_state_is_disconnect_fair(self):
        self.assertIn("fair: ['Fair', 'fair']", self.renderer)

    def test_poor_state_is_disconnect_poor(self):
        self.assertIn("poor: ['Poor', 'poor']", self.renderer)

    def test_authoritative_reconnecting_is_disconnect_reconnecting(self):
        self.assertIn(
            "const reconnecting = !!data.remote_dj_reconnecting || (weAreConnected && rdjReconnecting);",
            self.renderer,
        )
        self.assertIn("action, 'Reconnecting…', 'reconnecting'", self.renderer)
        self.assertNotIn("reconnecting: [", self.renderer)

    def test_local_disconnect_edge_updates_reconnecting_immediately(self):
        disconnected = "if (statsPc.iceConnectionState === 'disconnected')"
        edge = self.template[
            self.template.index(disconnected):
            self.template.index("if (['failed', 'closed']", self.template.index(disconnected))
        ]
        self.assertIn("rdjReconnecting = true;", edge)
        self.assertIn("renderRemoteDjConnect(window.__rdjLastEngineState || {});", edge)

    def test_initializing_or_missing_quality_never_guesses_good(self):
        fallback = self.renderer[self.renderer.index("if (presentedQuality)"):]
        self.assertIn("'Initializing…', 'initializing'", fallback)
        self.assertNotIn("'Good', 'good'", fallback)

    def test_button_reads_only_overall_not_direction_specific_quality(self):
        self.assertIn("const overall = quality && quality.overall;", self.renderer)
        self.assertNotIn("remote_mic", self.renderer)
        self.assertNotIn("monitor_return", self.renderer)

    def test_direction_specific_quality_remains_in_engine_state(self):
        classifier = (
            Path(__file__).parents[1] / "services/remote_dj_quality.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"remote_mic"', classifier)
        self.assertIn('"monitor_return"', classifier)

    def test_semantic_status_classes_use_text_and_theme_colors(self):
        self.assertIn(".rdj-connect-status.rdj-status-good { color: #22c55e; }", self.template)
        self.assertIn(".rdj-connect-status.rdj-status-fair,", self.template)
        self.assertIn(".rdj-connect-status.rdj-status-reconnecting { color: #d97706; }", self.template)
        self.assertIn(".rdj-connect-status.rdj-status-poor { color: var(--danger); }", self.template)
        self.assertIn("rdj-status-offline", self.template)
        self.assertIn("rdj-status-negotiating", self.template)
        self.assertIn("rdj-status-initializing", self.template)

    def test_existing_click_and_disconnect_action_semantics_are_preserved(self):
        self.assertIn('onclick="toggleRemoteDjConnect()"', self.control_row)
        self.assertIn("if (rdjPc || rdjWs) { rdjDisconnect(); return; }", self.toggle)
        self.assertIn("if (connectionStale) return;", self.toggle)

    def test_outer_dimensions_are_fixed_independently_of_status_text(self):
        css = self.template[
            self.template.index("#remoteDjConnectBtn {"):
            self.template.index("#remoteDjConnectBtn.active")
        ]
        self.assertIn("box-sizing: border-box;", css)
        self.assertIn("flex: 0 0 13em;", css)
        self.assertIn("height: 2rem;", css)
        self.assertIn("overflow: hidden;", css)
        self.assertIn("white-space: nowrap;", css)

    def test_mobile_rule_keeps_same_explicit_height_and_half_row_basis(self):
        mobile = self.template[self.template.index("@media (max-width: 640px)"):]
        connect_css = mobile[
            mobile.index("#remoteDjConnectBtn {"):
            mobile.index("}", mobile.index("#remoteDjConnectBtn {"))
        ]
        self.assertIn("flex-basis: calc(50% - 0.3rem);", connect_css)
        self.assertIn("height: 2rem;", connect_css)

    def test_quality_availability_never_adds_a_sibling_or_column(self):
        self.assertNotIn("Quality", self.control_row)
        self.assertEqual(self.control_row.count('id="remoteDjConnectBtn"'), 1)
        self.assertEqual(self.control_row.count('id="remoteDjConnectStatus"'), 1)
        self.assertNotIn("createElement", self.renderer)
        self.assertNotIn("hidden =", self.renderer)

    def test_accessibility_retains_button_keyboard_and_combined_label(self):
        self.assertIn('aria-label="Connect Remote DJ — Offline"', self.control_row)
        self.assertIn('aria-live="polite"', self.control_row)
        self.assertIn("btn.setAttribute('aria-label', ariaLabel);", self.template)
        self.assertIn("'Disconnect Remote DJ'", self.renderer)
        self.assertIn("' — link quality ' + presentedQuality[0]", self.renderer)

    def test_status_typography_is_compact_but_literal(self):
        status_css = self.template[
            self.template.index(".rdj-connect-status {"):
            self.template.index(".rdj-connect-status.rdj-status-good")
        ]
        self.assertIn("font-size: 0.75em;", status_css)
        self.assertIn("line-height: 1;", status_css)
        for literal in ("Offline", "Negotiating", "Good", "Fair", "Poor", "Reconnecting…"):
            self.assertIn(literal, self.template)
