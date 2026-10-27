"""Roadmap 2.27 Phase D: evidence-only screenshots of the Hour Refill Policy
admin page in headless Chromium, against the live test server and the
development TEST database (never a station). Skipped unless
HOUR_REFILL_PHASE_D_SCREENSHOTS=<dir> is set."""
import json
import os
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connections
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from library.models import HourRefillInstall, HourRefillPolicy, PlayEvent
from library.services import hour_refill_status as status_mod

SHOTS = os.environ.get("HOUR_REFILL_PHASE_D_SCREENSHOTS")


@unittest.skipUnless(SHOTS, "evidence-only browser screenshots")
@override_settings(SECURE_SSL_REDIRECT=False)
class HourRefillAdminScreenshots(StaticLiveServerTestCase):
    def setUp(self):
        super().setUp()
        env_patch = mock.patch.dict(os.environ, {"DJANGO_ALLOW_ASYNC_UNSAFE": "true"})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.shots = Path(SHOTS)
        self.shots.mkdir(parents=True, exist_ok=True)
        self.status_file = self.shots / "runner-status-used-for-screenshots.json"
        patcher = mock.patch.object(status_mod, "STATUS_PATH", self.status_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.force_login(User.objects.create_superuser("shots", "shots@example.invalid", "pw"))
        self.session = self.client.cookies["sessionid"].value

    def _status(self, **outcome):
        now = timezone.now()
        self.status_file.write_text(json.dumps({
            "schema": 1, "written_at": now.isoformat(),
            "runner": {"running": True, "last_check_at": now.isoformat(), "check_interval_seconds": 15.0},
            "policy": {"mode": outcome.pop("mode"), "host_armed": outcome.pop("armed", False)},
            "psa_residual": "off", "last_outcome": outcome,
            "counters": {"installed": outcome.get("status") == "installed" and 1 or 0, "advisory_proposals": 3,
                         "skipped_policy_off": 0, "live_fill_conflicts": 1, "errors": 0},
            "abstentions": {"below_threshold": 2, "blocked_transport": 1},
            "evidence_counters": {"recorded": 4, "dropped_full": 0}, "live_fill": {"legacy_discards": 0},
        }))

    def test_screens(self):
        from playwright.sync_api import sync_playwright

        url = self.live_server_url + reverse("admin:library_hourrefillpolicy_change", args=[1])
        HourRefillPolicy().save()
        pw = sync_playwright().start()
        self.addCleanup(pw.stop)
        self.addCleanup(connections.close_all)
        browser = pw.chromium.launch()
        self.addCleanup(browser.close)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        context.add_cookies([{"name": "sessionid", "value": self.session, "url": self.live_server_url}])
        page = context.new_page()

        page.goto(url)
        page.screenshot(path=str(self.shots / "01-off-no-runner-status.png"), full_page=True)

        HourRefillPolicy(enabled=True, capture_request_substitution=True, capture_operator_eject=True).save()
        self._status(mode="observation", at=timezone.localtime().isoformat(), kind="advisory", status="proposed",
                     reasons=["best_fit_selected"], log_id=812, log_date="2027-06-01", log_hour=14,
                     cause_types={"operator_eject": 1}, cause_ids=[41], net_shortage_seconds=389.25,
                     threshold_seconds=30.0, safety_budget_seconds=7.0, residual_after_seconds=8.75,
                     placement_mode="tail_append", track_ids=[5101, 5102], category_ids=[7, 7])
        page.goto(url)
        page.screenshot(path=str(self.shots / "02-observation-advisory.png"), full_page=True)

        HourRefillPolicy(enabled=True, dry_run_only=False, capture_request_substitution=True,
                         capture_operator_eject=True).save()
        page.goto(url)
        page.screenshot(path=str(self.shots / "03-install-requested-host-not-armed.png"), full_page=True)

        install = HourRefillInstall.objects.create(
            install_key="refill:log812:causes:screenshot", playlist_log_id_snapshot=812, log_date="2027-06-01",
            log_hour=14, cause_ids=[41], placement_mode="tail_append", installed_log_item_ids=[90001, 90002],
            track_ids=[5101, 5102], net_before_seconds=389.25, residual_after_seconds=1.75,
            safety_budget_seconds=7.0, snapshot_digest="0" * 64)
        PlayEvent.objects.create(log_item_id_snapshot=90001, started_at=install.committed_at + timedelta(minutes=4))
        self._status(mode="install", armed=True, at=timezone.localtime().isoformat(), kind="committed",
                     status="installed", reasons=["committed"], log_id=812, log_date="2027-06-01", log_hour=14,
                     cause_types={"operator_eject": 1}, cause_ids=[41], net_shortage_seconds=389.25,
                     threshold_seconds=30.0, safety_budget_seconds=7.0, residual_beyond_budget_seconds=1.75,
                     placement_mode="tail_append", track_ids=[5101, 5102], category_ids=[7, 7],
                     installed_log_item_ids=[90001, 90002])
        with override_settings(HOUR_REFILL_INSTALL_ARMED=True):
            page.goto(url)
            page.screenshot(path=str(self.shots / "04-install-permitted-committed-vs-aired.png"), full_page=True)
            text = page.inner_text("body")
        self.assertIn("Install permitted", text)
        self.assertIn("no play record", text)
        self.assertIn("aired", text)
