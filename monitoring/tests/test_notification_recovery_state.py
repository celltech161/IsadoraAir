"""MonitorManager persistence/restart coverage for recovery-email incidents."""
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from django.core import mail
from django.test import TransactionTestCase

from monitoring.models import MonitorCheck, NotificationConfig
from monitoring.services import monitor as monitor_module


class MonitorNotificationIncidentStateTests(TransactionTestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.state_path = Path(self.tmp_dir.name) / "monitoring_state.json"
        state_patcher = patch.object(monitor_module, "STATE_PATH", self.state_path)
        state_patcher.start()
        self.addCleanup(state_patcher.stop)

        MonitorCheck.objects.all().update(enabled=False)
        config = NotificationConfig.load()
        config.enabled = True
        config.recipients = "ops@example.com"
        config.cooldown_minutes = 30
        config.save()
        self.check = MonitorCheck.objects.create(
            name="Synthetic Recovery Check",
            kind="systemd",
            systemd_unit="synthetic.service",
            consecutive_failures_required=1,
            notify_on_warning=True,
            notify_on_critical=True,
        )

    def cycle(self, manager, status):
        with (
            patch.dict(
                monitor_module.PROBE_DISPATCH,
                {"systemd": lambda _check: (status, {"synthetic": True})},
            ),
            patch.object(monitor_module, "create_transmitter_driver", return_value=None),
            patch.object(manager, "_poll_shoutcast_listeners"),
            patch.object(monitor_module.sd_notify, "watchdog_enabled", return_value=False),
        ):
            manager._run_cycle()

    def test_synthetic_warning_to_ok_sends_alert_then_recovery(self):
        manager = monitor_module.MonitorManager()
        self.cycle(manager, "warning")
        self.cycle(manager, "ok")
        self.assertEqual([message.subject for message in mail.outbox], [
            "[IsadoraAir] Synthetic Recovery Check: WARNING",
            "[IsadoraAir] Synthetic Recovery Check: RECOVERED",
        ])
        self.assertEqual(manager._notification_incidents, {})
        self.assertEqual(manager._cooldowns, {})

    def test_live_warning_preference_change_is_seen_without_status_edge(self):
        manager = monitor_module.MonitorManager()
        self.cycle(manager, "warning")
        self.assertIn(str(self.check.id), manager._notification_incidents)

        self.check.notify_on_warning = False
        self.check.save(update_fields=["notify_on_warning"])
        self.cycle(manager, "warning")
        self.assertEqual(manager._notification_incidents, {})
        self.cycle(manager, "ok")
        self.assertEqual(len(mail.outbox), 1)

    def test_restart_preserves_legitimate_active_incident_attribution(self):
        first = monitor_module.MonitorManager()
        self.cycle(first, "critical")
        self.assertTrue(self.state_path.is_file())

        restarted = monitor_module.MonitorManager()
        restarted._load_persisted_notification_state()
        cid = str(self.check.id)
        self.assertEqual(
            restarted._notification_incidents[cid]["attempted_severity"],
            "critical",
        )
        self.cycle(restarted, "critical")
        self.assertEqual(len(mail.outbox), 1)  # cooldown: no duplicate alert
        self.cycle(restarted, "ok")
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("RECOVERED", mail.outbox[-1].subject)

    def test_restart_first_observation_ok_clears_stale_incident_silently(self):
        first = monitor_module.MonitorManager()
        self.cycle(first, "critical")

        restarted = monitor_module.MonitorManager()
        restarted._load_persisted_notification_state()
        self.cycle(restarted, "ok")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(restarted._notification_incidents, {})
        self.assertEqual(restarted._cooldowns, {})

    def test_old_state_without_incident_ledger_keeps_cooldown_but_cannot_recover(self):
        cid = str(self.check.id)
        self.state_path.write_text(json.dumps({
            "timestamp": time.time(),
            "checks": [{"id": self.check.id, "status": "critical"}],
            "_cooldowns": {cid: 123.0},
        }))
        manager = monitor_module.MonitorManager()
        manager._load_persisted_notification_state()
        self.assertEqual(manager._cooldowns, {cid: 123.0})
        self.assertEqual(manager._notification_incidents, {})

    def test_malformed_incident_records_fail_closed(self):
        cid = str(self.check.id)
        self.state_path.write_text(json.dumps({
            "_cooldowns": {cid: 123.0},
            "_notification_incidents": {
                cid: {
                    "attempted_severity": "critical",
                    "attempted_at": "yesterday",
                    "delivery_outcome": "sent",
                },
                "other": {"attempted_severity": "critical"},
            },
        }))
        manager = monitor_module.MonitorManager()
        manager._load_persisted_notification_state()
        self.assertEqual(manager._notification_incidents, {})

    def test_state_file_keeps_attempted_severity_separate_from_delivery(self):
        manager = monitor_module.MonitorManager()
        manager._notification_incidents = {
            str(self.check.id): {
                "attempted_severity": "warning",
                "attempted_at": 42.0,
                "delivery_outcome": "failed",
            }
        }
        manager._write_state([])
        persisted = json.loads(self.state_path.read_text())
        self.assertEqual(
            persisted["_notification_incidents"][str(self.check.id)],
            {
                "attempted_severity": "warning",
                "attempted_at": 42.0,
                "delivery_outcome": "failed",
            },
        )
