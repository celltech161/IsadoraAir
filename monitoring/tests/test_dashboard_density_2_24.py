"""r0123 Package D (roadmap 2.24): the /monitoring/ density and readability pass is presentation
only. The template drops the routine "for 3d" line (the duration moves onto the status pill),
uses the denser grid, and keeps every meaningful duration renderer; the status API -- including
each check's `since` -- is passed through unchanged. Rendered layout, polling and severity
behaviour were checked in a browser against a sandbox-rendered page (see the r0123 Package D
evidence); no browser runs here."""
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from monitoring.services import release_status

REPO = Path(__file__).resolve().parents[2]


@override_settings(SECURE_SSL_REDIRECT=False)
class DashboardDensityTemplateTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user("density-user", password="x", is_staff=True))
        self.body = self.client.get(reverse("monitoring:dashboard")).content.decode()

    def test_the_routine_elapsed_line_is_gone_and_moves_onto_the_pill(self):
        self.assertNotIn("mon-card-since", self.body)
        self.assertNotIn('"for " + formatSince(check.since)', self.body)
        self.assertIn('const heldFor = check.since ? ` for ${formatSince(check.since)}` : "";', self.body)
        self.assertIn('title="${check.status}${heldFor}"', self.body)
        self.assertIn('<span class="mon-sr">${heldFor}</span>', self.body)

    def test_meaningful_durations_keep_their_renderers(self):
        for fragment in ("function formatSince(since)",             # still used below
                         "`Up ${uptimeHours} hrs`",                 # systemd uptime
                         "Last heartbeat ${Math.round(heartbeatAge)}s ago",
                         '${label} for ${hours} hrs',               # audio stable/down for
                         "Connected since ${window.stationFormatString(",
                         "Down since ${window.stationFormatString(",
                         "formatSince(m.checked_at) + ' ago'",
                         "Last idle rollover: ${formatSince(rollover)} ago"):
            with self.subTest(fragment):
                self.assertIn(fragment, self.body)

    def test_denser_grid_with_overflow_protection_and_unchanged_status_styling(self):
        for fragment in ("minmax(min(198px, 100%), 1fr)", "minmax(min(168px, 100%), 1fr)",
                         "padding: 0.5rem 0.65rem;", "gap: 0.45rem;", ".mon-card-top .mon-pill { flex-shrink: 0; }",
                         "overflow-wrap: break-word;", "overflow-wrap: anywhere;",
                         ".mon-pill.critical { background: rgba(249, 115, 115, 0.18); color: var(--danger); }",
                         ".mon-pill.warning { background: rgba(245, 158, 11, 0.18); color: #f59e0b; }"):
            with self.subTest(fragment):
                self.assertIn(fragment, self.body)
        self.assertNotIn("minmax(210px, 1fr)", self.body)
        self.assertNotIn("minmax(175px, 1fr)", self.body)

    def test_grouping_visibility_and_polling_are_untouched(self):
        for fragment in ('{ label: "Services", kinds: ["systemd"] }',
                         '{ label: "Transmitter", kinds: ["transmitter_param", "transmitter_indicator"] }',
                         ".filter(c => c.show_as_card !== false)",
                         ".sort((a, b) => a.sort_order - b.sort_order)",
                         "airHost.appendChild(airCardEl);",
                         "expandedCoreCards.has(check.id)",
                         "setInterval(pollStatus, 5000);"):
            with self.subTest(fragment):
                self.assertIn(fragment, self.body)


@override_settings(SECURE_SSL_REDIRECT=False)
class StatusApiUnchangedTests(TestCase):
    """The API passes the monitoring state through: `since`, status, detail and order intact."""

    def setUp(self):
        self.client.force_login(User.objects.create_user("density-api", password="x", is_staff=True))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state_dir = Path(temp.name)
        from monitoring import views as monitoring_views
        for target, name, value in ((monitoring_views, "STATE_PATH", state_dir / "monitoring_state.json"),
                                    (release_status, "STATE_DIR", state_dir),
                                    (release_status, "ENGINE_STATE_PATH", state_dir / "engine_state.json"),
                                    (release_status, "MONITORING_STATE_PATH", state_dir / "monitoring_state.json"),
                                    (release_status, "RBDS_STATE_PATH", state_dir / "rbds_state.json")):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.state_path = state_dir / "monitoring_state.json"

    def test_since_status_detail_and_order_reach_the_browser_unchanged(self):
        since = time.time() - 3 * 86400
        checks = [
            {"id": 901, "name": "Disk /srv/isadoraair", "kind": "disk", "status": "critical", "since": since,
             "detail": {"percent": 97.5}, "sort_order": 2, "show_as_card": True},
            {"id": 902, "name": "VSWR indicator", "kind": "transmitter_indicator", "status": "warning",
             "since": since + 60, "detail": {"value": "O"}, "sort_order": 1, "show_as_card": False},
        ]
        self.state_path.write_text(json.dumps({"timestamp": time.time(), "checks": checks,
                                               "_cooldowns": {"901": 1}}), encoding="utf-8")
        body = self.client.get(reverse("monitoring:api-status")).json()
        self.assertFalse(body["stale"])
        self.assertNotIn("_cooldowns", body)
        self.assertEqual([(c["id"], c["status"], c["since"], c["detail"], c["show_as_card"]) for c in body["checks"]],
                         [(c["id"], c["status"], c["since"], c["detail"], c["show_as_card"]) for c in checks])

    def test_package_d_touches_only_the_dashboard_template(self):
        import subprocess
        changed = subprocess.run(["git", "-C", str(REPO), "diff", "--name-only", "8289b14", "--", "monitoring/"],
                                 capture_output=True, text=True)
        if changed.returncode != 0:
            self.skipTest("git history is not available here")
        self.assertEqual(sorted(set(changed.stdout.split()) - {
            "monitoring/tests/test_dashboard_density_2_24.py", "monitoring/tests/test_release_status.py",
            "monitoring/tests/render_dashboard_artifact.py"}), ["monitoring/templates/monitoring/dashboard.html"])
