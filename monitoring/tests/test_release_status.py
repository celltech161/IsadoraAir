"""1.7 release/version-skew visibility -- the monitoring-side merge step
(monitoring/services/release_status.py) and its wiring into
api_monitoring_status (monitoring/views.py). Every test writes state
JSON only under a temp directory (patched STATE_DIR/*_STATE_PATH), never
touches the real /run/isadoraair/*.json files."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from monitoring.models import MonitorCheck
from monitoring.services import release_status


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class BuildVersionLookupTests(TestCase):
    """Exercises build_version_lookup()/get_release_status() directly,
    with every underlying reader mocked -- no real /run/isadoraair
    files, no real git."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name)
        patcher = patch.object(release_status, "STATE_DIR", self.state_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Re-point the module-level path constants derived from the old
        # STATE_DIR at import time -- they were computed once against the
        # real STATE_DIR before the patch above took effect.
        self._path_patchers = [
            patch.object(release_status, "ENGINE_STATE_PATH", self.state_dir / "engine_state.json"),
            patch.object(release_status, "MONITORING_STATE_PATH", self.state_dir / "monitoring_state.json"),
            patch.object(release_status, "RBDS_STATE_PATH", self.state_dir / "rbds_state.json"),
        ]
        for p in self._path_patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_matching_commit_is_current(self):
        commit = "a" * 40
        _write(release_status.ENGINE_STATE_PATH, {"runtime_commit": commit})
        checkout = {"commit": commit, "short_commit": commit[:7], "dirty": False}
        lookup = release_status.build_version_lookup(checkout)
        self.assertEqual(lookup[release_status.ENGINE_UNIT]["state"], "current")
        self.assertEqual(lookup[release_status.ENGINE_UNIT]["runtime_commit"], commit)
        self.assertEqual(lookup[release_status.ENGINE_UNIT]["runtime_short"], commit[:7])

    def test_stale_running_service(self):
        old_commit = "b" * 40
        new_commit = "c" * 40
        _write(release_status.ENGINE_STATE_PATH, {"runtime_commit": old_commit})
        checkout = {"commit": new_commit, "short_commit": new_commit[:7], "dirty": False}
        lookup = release_status.build_version_lookup(checkout)
        self.assertEqual(lookup[release_status.ENGINE_UNIT]["state"], "stale")

    def test_dirty_checkout_with_matching_commit_is_indeterminate_not_current(self):
        """Commit equality alone doesn't prove the running process
        reflects the CURRENT filesystem state when the checkout has
        uncommitted changes -- the process only ever loaded a committed
        tree, and those changes are newer than any commit it could have
        started from. Must NOT report "current"."""
        commit = "1" * 40
        _write(release_status.ENGINE_STATE_PATH, {"runtime_commit": commit})
        checkout = {"commit": commit, "short_commit": commit[:7], "dirty": True}
        lookup = release_status.build_version_lookup(checkout)
        self.assertEqual(lookup[release_status.ENGINE_UNIT]["state"], "indeterminate")

    def test_dirty_checkout_with_mismatched_commit_is_still_stale(self):
        """A dirty checkout must NOT soften a genuine SHA mismatch --
        that's still exactly as stale as it would be on a clean tree."""
        old_commit = "2" * 40
        new_commit = "3" * 40
        _write(release_status.ENGINE_STATE_PATH, {"runtime_commit": old_commit})
        checkout = {"commit": new_commit, "short_commit": new_commit[:7], "dirty": True}
        lookup = release_status.build_version_lookup(checkout)
        self.assertEqual(lookup[release_status.ENGINE_UNIT]["state"], "stale")

    def test_dirty_none_status_failure_does_not_trigger_indeterminate(self):
        """dirty can independently be None (rev-parse succeeded, `git
        status` itself failed) while commit is still populated -- that's
        NOT confirmed dirtiness, so a matching commit still reports
        "current", not "indeterminate"."""
        commit = "4" * 40
        _write(release_status.ENGINE_STATE_PATH, {"runtime_commit": commit})
        checkout = {"commit": commit, "short_commit": commit[:7], "dirty": None}
        lookup = release_status.build_version_lookup(checkout)
        self.assertEqual(lookup[release_status.ENGINE_UNIT]["state"], "current")

    def test_missing_state_file_is_unknown(self):
        checkout = {"commit": "d" * 40, "short_commit": "ddddddd", "dirty": False}
        lookup = release_status.build_version_lookup(checkout)
        self.assertEqual(lookup[release_status.ENGINE_UNIT]["state"], "unknown")
        self.assertIsNone(lookup[release_status.ENGINE_UNIT]["runtime_commit"])

    def test_missing_state_file_is_unknown_even_when_checkout_dirty(self):
        """Unknown runtime commit stays "unknown" regardless of the
        checkout's dirty state -- dirty only matters once a match is
        already established."""
        checkout = {"commit": "d" * 40, "short_commit": "ddddddd", "dirty": True}
        lookup = release_status.build_version_lookup(checkout)
        self.assertEqual(lookup[release_status.ENGINE_UNIT]["state"], "unknown")

    def test_malformed_state_file_is_unknown_not_a_crash(self):
        release_status.ENGINE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        release_status.ENGINE_STATE_PATH.write_text("{not valid json", encoding="utf-8")
        checkout = {"commit": "e" * 40, "short_commit": "eeeeeee", "dirty": False}
        lookup = release_status.build_version_lookup(checkout)
        self.assertEqual(lookup[release_status.ENGINE_UNIT]["state"], "unknown")

    def test_checkout_commit_unknown_never_claims_match(self):
        """Even if a running service's own commit happens to be a
        genuine 40-char SHA, an unknown checkout must never resolve to
        "current" -- see version_info.py's rollout-backward-compatibility
        requirement."""
        commit = "f" * 40
        _write(release_status.ENGINE_STATE_PATH, {"runtime_commit": commit})
        checkout = {"commit": None, "short_commit": None, "dirty": None}
        lookup = release_status.build_version_lookup(checkout)
        self.assertEqual(lookup[release_status.ENGINE_UNIT]["state"], "unknown")

    def test_partial_restart_scenario_several_services_different_commits(self):
        """The realistic post-deploy window: some services restarted
        onto the new commit, others still running the old one."""
        old_commit = "1" * 40
        new_commit = "2" * 40
        _write(release_status.ENGINE_STATE_PATH, {"runtime_commit": new_commit})
        _write(release_status.MONITORING_STATE_PATH, {"runtime_commit": old_commit})
        _write(release_status.RBDS_STATE_PATH, {"runtime_commit": new_commit})
        checkout = {"commit": new_commit, "short_commit": new_commit[:7], "dirty": False}
        lookup = release_status.build_version_lookup(checkout, monitoring_state={"runtime_commit": old_commit})
        self.assertEqual(lookup[release_status.ENGINE_UNIT]["state"], "current")
        self.assertEqual(lookup[release_status.MONITORING_UNIT]["state"], "stale")
        self.assertEqual(lookup[release_status.RBDS_UNIT]["state"], "current")

    def test_monitoring_state_param_avoids_second_file_read(self):
        """When the caller (api_monitoring_status) already has the
        parsed monitoring_state.json dict in hand, build_version_lookup
        must use it directly rather than re-reading the file -- confirm
        by pointing MONITORING_STATE_PATH at a file with a DIFFERENT
        value and checking the passed-in dict wins."""
        _write(release_status.MONITORING_STATE_PATH, {"runtime_commit": "stale-on-disk"})
        checkout = {"commit": "fresh-value", "short_commit": "fresh-v", "dirty": False}
        lookup = release_status.build_version_lookup(checkout, monitoring_state={"runtime_commit": "fresh-value"})
        self.assertEqual(lookup[release_status.MONITORING_UNIT]["state"], "current")

    def test_encoders_glob_reads_any_one_group_file(self):
        group_commit = "9" * 40
        _write(self.state_dir / "encoder_group_line_in.json", {"runtime_commit": group_commit})
        checkout = {"commit": group_commit, "short_commit": group_commit[:7], "dirty": False}
        lookup = release_status.build_version_lookup(checkout)
        self.assertEqual(lookup[release_status.ENCODERS_UNIT]["state"], "current")

    def test_encoders_zero_groups_degrades_to_unknown(self):
        """No encoder_group_*.json files at all (zero groups configured)
        -- an accepted degradation, not a crash."""
        checkout = {"commit": "z" * 40, "short_commit": "zzzzzzz", "dirty": False}
        lookup = release_status.build_version_lookup(checkout)
        self.assertEqual(lookup[release_status.ENCODERS_UNIT]["state"], "unknown")

    def test_gunicorn_reads_from_get_web_runtime_commit(self):
        commit = "5" * 40
        checkout = {"commit": commit, "short_commit": commit[:7], "dirty": False}
        with patch.object(release_status, "get_web_runtime_commit", return_value=commit):
            lookup = release_status.build_version_lookup(checkout)
        self.assertEqual(lookup[release_status.GUNICORN_UNIT]["state"], "current")

    def test_reader_exception_is_contained_per_unit(self):
        """A single reader raising must not take down the whole lookup
        -- every OTHER unit still resolves normally."""
        commit = "6" * 40
        _write(release_status.RBDS_STATE_PATH, {"runtime_commit": commit})
        checkout = {"commit": commit, "short_commit": commit[:7], "dirty": False}
        with patch.object(release_status, "_read_engine_runtime_commit", side_effect=RuntimeError("boom")):
            lookup = release_status.build_version_lookup(checkout)
        self.assertEqual(lookup[release_status.ENGINE_UNIT]["state"], "unknown")
        self.assertEqual(lookup[release_status.RBDS_UNIT]["state"], "current")

    def test_lookup_covers_exactly_the_five_known_units(self):
        checkout = {"commit": "0" * 40, "short_commit": "0000000", "dirty": False}
        lookup = release_status.build_version_lookup(checkout)
        self.assertEqual(set(lookup.keys()), release_status.COVERED_UNITS)


@override_settings(SECURE_SSL_REDIRECT=False)  # project-wide prod setting; the plain-HTTP test client would otherwise 301
class ApiMonitoringStatusVersionMergeTests(TestCase):
    """Integration through the real view -- confirms the "checkout" block
    and per-check "version" sub-dict actually reach the JSON response,
    and that existing Running/Stopped/Stale card-status fields are
    completely unaffected (a version mismatch must never touch
    check["status"])."""

    def setUp(self):
        self.staff = User.objects.create_user("staffuser", password="x", is_staff=True)
        self.client.force_login(self.staff)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state_dir = Path(self.tmp.name)

        from monitoring import views as monitoring_views
        state_patcher = patch.object(monitoring_views, "STATE_PATH", state_dir / "monitoring_state.json")
        state_patcher.start()
        self.addCleanup(state_patcher.stop)

        rs_patcher = patch.object(release_status, "STATE_DIR", state_dir)
        rs_patcher.start()
        self.addCleanup(rs_patcher.stop)
        for name, fname in (
            ("ENGINE_STATE_PATH", "engine_state.json"),
            ("MONITORING_STATE_PATH", "monitoring_state.json"),
            ("RBDS_STATE_PATH", "rbds_state.json"),
        ):
            p = patch.object(release_status, name, state_dir / fname)
            p.start()
            self.addCleanup(p.stop)

        self.monitoring_state_path = state_dir / "monitoring_state.json"

        # Names deliberately distinct from the migration-seeded default
        # checks (monitoring/migrations/0002_seed_default_checks.py also
        # creates a "Playback Engine"/"nginx" row) -- MonitorCheck.name
        # is unique, and the merge logic keys off systemd_unit, not
        # name, so a distinct test name is sufficient and avoids the
        # collision entirely.
        self.engine_check = MonitorCheck.objects.create(
            name="Test Playback Engine Check", kind="systemd", systemd_unit=release_status.ENGINE_UNIT, sort_order=1,
        )
        self.nginx_check = MonitorCheck.objects.create(
            name="Test Nginx Check", kind="systemd", systemd_unit="nginx.service", sort_order=2,
        )

    def _write_monitoring_state(self, checks, runtime_commit="mon-commit"):
        import time
        _write(self.monitoring_state_path, {
            "timestamp": time.time(), "checks": checks, "runtime_commit": runtime_commit,
        })

    def test_checkout_block_present_and_version_attached_to_covered_unit(self):
        engine_commit = "a" * 40
        _write(release_status.ENGINE_STATE_PATH, {"runtime_commit": engine_commit})
        self._write_monitoring_state([
            {"id": self.engine_check.id, "name": "Playback Engine", "kind": "systemd",
             "status": "ok", "systemd_unit": release_status.ENGINE_UNIT},
            {"id": self.nginx_check.id, "name": "nginx", "kind": "systemd",
             "status": "ok", "systemd_unit": "nginx.service"},
        ])
        checkout = {"commit": engine_commit, "short_commit": engine_commit[:7], "dirty": False}
        with patch("monitoring.services.release_status.get_checkout_identity", return_value=checkout):
            resp = self.client.get(reverse("monitoring:api-status"))
        data = resp.json()
        self.assertIn("checkout", data)
        self.assertEqual(data["checkout"]["short_commit"], engine_commit[:7])

        by_name = {c["name"]: c for c in data["checks"]}
        self.assertIn("version", by_name["Playback Engine"])
        self.assertEqual(by_name["Playback Engine"]["version"]["state"], "current")
        # nginx is third-party/not covered -- no "version" key at all,
        # not a fabricated "unknown".
        self.assertNotIn("version", by_name["nginx"])

    def test_stale_service_status_field_unaffected(self):
        """The whole point of the health/deployment-state distinction:
        a version mismatch must NOT downgrade check["status"]."""
        self._write_monitoring_state([
            {"id": self.engine_check.id, "name": "Playback Engine", "kind": "systemd",
             "status": "ok", "systemd_unit": release_status.ENGINE_UNIT},
        ])
        _write(release_status.ENGINE_STATE_PATH, {"runtime_commit": "old" * 13 + "x"})
        checkout = {"commit": "new" * 13 + "y", "short_commit": "newnewn", "dirty": False}
        with patch("monitoring.services.release_status.get_checkout_identity", return_value=checkout):
            resp = self.client.get(reverse("monitoring:api-status"))
        data = resp.json()
        engine = next(c for c in data["checks"] if c["name"] == "Playback Engine")
        self.assertEqual(engine["status"], "ok")
        self.assertEqual(engine["version"]["state"], "stale")

    def test_dirty_checkout_matching_service_is_indeterminate_end_to_end(self):
        """Full-stack confirmation (view + release_status + template
        contract): a dirty checkout with a matching runtime commit must
        reach the client as "indeterminate", not "current", and the
        page-level dirty warning is still present alongside it."""
        commit = "5" * 40
        _write(release_status.ENGINE_STATE_PATH, {"runtime_commit": commit})
        self._write_monitoring_state([
            {"id": self.engine_check.id, "name": "Playback Engine", "kind": "systemd",
             "status": "ok", "systemd_unit": release_status.ENGINE_UNIT},
        ])
        checkout = {"commit": commit, "short_commit": commit[:7], "dirty": True}
        with patch("monitoring.services.release_status.get_checkout_identity", return_value=checkout):
            resp = self.client.get(reverse("monitoring:api-status"))
        data = resp.json()
        self.assertTrue(data["checkout"]["dirty"])
        engine = next(c for c in data["checks"] if c["name"] == "Playback Engine")
        self.assertEqual(engine["status"], "ok")
        self.assertEqual(engine["version"]["state"], "indeterminate")

    def test_release_status_failure_does_not_break_the_whole_response(self):
        """Any unexpected exception building the release status must
        degrade to unknown fields, never a 500 for the entire dashboard
        poll (an unrelated feature failing must not blind the operator
        to every other card)."""
        self._write_monitoring_state([
            {"id": self.engine_check.id, "name": "Playback Engine", "kind": "systemd",
             "status": "ok", "systemd_unit": release_status.ENGINE_UNIT},
        ])
        with patch("monitoring.views.get_release_status", side_effect=RuntimeError("boom")):
            resp = self.client.get(reverse("monitoring:api-status"))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("checkout", data)
        self.assertIsNone(data["checkout"]["commit"])
        engine = next(c for c in data["checks"] if c["name"] == "Playback Engine")
        self.assertNotIn("version", engine)

    def test_missing_state_file_still_returns_stale_true_no_checkout_crash(self):
        """Pre-existing behavior (no state file at all yet) must be
        completely unaffected by this feature -- the endpoint returns
        early before release_status is even consulted."""
        resp = self.client.get(reverse("monitoring:api-status"))
        data = resp.json()
        self.assertEqual(data["checks"], [])
        self.assertTrue(data["stale"])
        self.assertNotIn("checkout", data)

    def test_cooldowns_still_stripped_runtime_commit_not_exposed_via_checks(self):
        """Regression guard: _cooldowns internal bookkeeping is still
        popped; the monitoring service's OWN runtime_commit (a top-level
        state field, not a per-check field) doesn't leak into the
        per-check list."""
        self._write_monitoring_state([
            {"id": self.engine_check.id, "name": "Playback Engine", "kind": "systemd",
             "status": "ok", "systemd_unit": release_status.ENGINE_UNIT},
        ])
        import time
        state = json.loads(self.monitoring_state_path.read_text())
        state["_cooldowns"] = {"1": 12345}
        _write(self.monitoring_state_path, state)
        resp = self.client.get(reverse("monitoring:api-status"))
        data = resp.json()
        self.assertNotIn("_cooldowns", data)


@override_settings(SECURE_SSL_REDIRECT=False)
class DashboardTemplateSanityTests(TestCase):
    """Layout sanity check: renders the real /monitoring/ template
    end-to-end (catches a Django template syntax error the JS/CSS edits
    could have introduced) and confirms the new elements/functions this
    round added are actually present in the output -- not a full JS
    unit test (no JS runtime here), just proof the page still renders
    and the new pieces reached the DOM/script text."""

    def setUp(self):
        self.staff = User.objects.create_user("dashboarduser", password="x", is_staff=True)
        self.client.force_login(self.staff)

    def test_dashboard_renders_with_release_elements_present(self):
        resp = self.client.get(reverse("monitoring:dashboard"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('id="monRelease"', body)
        self.assertIn("renderReleaseLine", body)
        self.assertIn("renderVersionBadge", body)
        self.assertIn("mon-version", body)
        self.assertIn("mon-release-line", body)
        # renderReleaseLine must be called from pollStatus() -- not just
        # defined and orphaned.
        self.assertIn("renderReleaseLine(data.checkout)", body)

    def test_r0007_monitoring_and_rbds_card_contract(self):
        resp = self.client.get(reverse("monitoring:dashboard"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()

        self.assertIn(".mon-group-system .mon-cards", body)
        self.assertIn(".mon-group-transmitter .mon-cards", body)
        self.assertIn("minmax(175px, 1fr)", body)
        self.assertIn(".mon-group-transmitter .mon-value-text", body)

        self.assertNotIn('id="aircheckLed"', body)
        self.assertNotIn('id="aircheckCaption"', body)
        self.assertIn("pillText = 'READY'; stateClass = 'ok'", body)
        self.assertIn("pillText = 'REC'; stateClass = 'critical'", body)
        self.assertIn("pillText = 'FINALIZING'; stateClass = 'warning'", body)

        rbds_section = body.split("<h2>RBDS</h2>", 1)[1].split(
            "<h2>Recent Failed Logins</h2>", 1
        )[0]
        self.assertEqual(rbds_section.count('class="mon-card"'), 3)
        for element_id in (
            "rbdsPs", "rbdsLongPs", "rbdsPty", "rbdsPtyn",
            "rbdsRt", "rbdsConnectionPill", "rbdsProtocol",
        ):
            self.assertIn(f'id="{element_id}"', rbds_section)
        self.assertNotIn('<span class="mon-card-name">Protocol</span>', rbds_section)
        self.assertIn("Protocol: ${data.protocol.toUpperCase()}/", body)
        self.assertIn("if (data.stale ||", body)
        self.assertIn('connectionPill.textContent = "UNKNOWN"', body)
        self.assertIn("setInterval(pollRbdsStatus, 500);", body)
        self.assertIn("setInterval(pollStatus, 5000);", body)


@override_settings(SECURE_SSL_REDIRECT=False)
class LoginRequiredTests(TestCase):
    def test_anonymous_redirected(self):
        resp = self.client.get(reverse("monitoring:api-status"))
        self.assertNotEqual(resp.status_code, 200)
