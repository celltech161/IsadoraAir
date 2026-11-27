"""1.24-B: feed notifications, the empty-dataset guard, the sync's request
for a generation check, and the anchored retrieval slots.

Mail goes to Django's in-memory outbox (asserted below); nothing is ever
sent. The sync runs for real against a fake CARS client."""
import json
import smtplib
import socket
import tempfile
from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import psycopg2
from django.conf import settings
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone as dj_timezone

from monitoring.models import NotificationConfig, SystemEvent
from road_conditions import alerts, trigger
from road_conditions.api import CarsApiTimeout
from road_conditions.management.commands import sync_road_conditions as sync_command
from road_conditions.models import RoadConditionsConfiguration, RoadConditionsSyncRun, RoadEvent
from road_conditions.synthesis import road_report_path, road_report_text_path
from road_conditions.tests.runtime_paths import patch_runtime_paths
from road_conditions.tests.test_generate_road_condition_audio import make_fresh_config
from road_conditions.tests.test_kandrive_synthesis import KanDriveSynthesisFixtureMixin
from road_conditions.tests.test_sync import FakeCarsClient, make_event

SERVICES = "road_conditions.services"
SUBJECT = "[IsadoraAir] Road Conditions Data: "


class RuntimeScratchMixin:
    def setUp(self):
        super().setUp()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.runtime = Path(scratch.name)
        for patcher in patch_runtime_paths(self.runtime):
            self.addCleanup(patcher.stop)

    def configure(self, **fields):
        config = RoadConditionsConfiguration.load()
        values = dict(enabled=True, last_error="", poll_cadence_minutes=30,
                      stale_data_threshold_minutes=120, counties="Ottawa",
                      additional_route_coverage="", last_fetch_attempted_at=dj_timezone.now(),
                      last_fetch_succeeded_at=dj_timezone.now())
        values.update(fields)
        for name, value in values.items():
            setattr(config, name, value)
        config.save()
        notify = NotificationConfig.load()
        notify.enabled = True
        notify.recipients = "engineer@example.test"
        notify.cooldown_minutes = 60
        notify.save()

    def sync(self, events=None, error=None, args=("--force-full",)):
        client = FakeCarsClient(events=events if events is not None else [make_event()])
        if error is not None:
            def fail(**kwargs):
                raise error
            client.get_events = fail
        out = StringIO()
        with patch(f"{SERVICES}.build_client", return_value=client):
            call_command("sync_road_conditions", *args, stdout=out)
        return out.getvalue()

    def failing_sync(self, error=None, events=None, args=("--force-full",)):
        with self.assertRaises(CommandError):
            self.sync(events=events, error=error or CarsApiTimeout("simulated timeout"), args=args) \
                if events is None else self.sync(events=events, args=args)

    def subjects(self):
        return [message.subject for message in mail.outbox]

    def stamp(self):
        try:
            return trigger.TRIGGER_PATH.read_text()
        except FileNotFoundError:
            return None

    def age_cooldown(self, minutes=61):
        """As if the last notification attempt had been `minutes` ago."""
        state = json.loads(alerts.STATE_PATH.read_text())
        state["cooldowns"] = {key: value - minutes * 60 for key, value in state["cooldowns"].items()}
        alerts.STATE_PATH.write_text(json.dumps(state))

    def tick(self, count=1):
        """An ordinary one-minute invocation that is not due."""
        for _ in range(count):
            out = StringIO()
            with patch.object(sync_command, "_due", return_value=(False, dj_timezone.now())):
                call_command("sync_road_conditions", stdout=out)
        return out.getvalue()


class FeedNotificationTests(RuntimeScratchMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.configure()

    def test_tests_can_never_send_real_mail(self):
        self.assertEqual(settings.EMAIL_BACKEND, "django.core.mail.backends.locmem.EmailBackend")

    def test_failed_retrieval_notifies_once_through_monitoring_recipients(self):
        self.failing_sync()
        self.assertEqual(self.subjects(), [SUBJECT + "WARNING"])
        message = mail.outbox[0]
        self.assertEqual(message.to, ["engineer@example.test"])
        self.assertIn("simulated timeout", message.body)
        self.assertIn("Ready-to-Air was not modified", message.body)
        self.assertTrue(SystemEvent.objects.filter(title="CARS API fetch failed").exists())
        self.assertTrue(SystemEvent.objects.filter(
            title="Road Conditions Data: ok → warning", level="warning").exists())

    def test_repeats_and_ticks_send_nothing_more(self):
        self.failing_sync()
        events = SystemEvent.objects.filter(title__startswith="Road Conditions Data").count()
        self.tick(30)
        self.failing_sync()
        self.age_cooldown(600)          # even long after the cooldown, same incident and severity
        self.tick(30)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(SystemEvent.objects.filter(title__startswith="Road Conditions Data").count(), events)

    def test_stale_data_is_noticed_by_an_ordinary_tick_and_escalates(self):
        self.failing_sync()
        self.age_cooldown()
        set_to = dj_timezone.now() - timedelta(minutes=121)
        RoadConditionsConfiguration.objects.update(last_fetch_succeeded_at=set_to)
        self.assertIn("Not due yet", self.tick())
        self.assertEqual(self.subjects(), [SUBJECT + "WARNING", SUBJECT + "CRITICAL"])
        self.assertIn("older than the freshness limit", mail.outbox[1].body)
        self.assertIn("120", mail.outbox[1].body)
        self.tick(10)
        self.assertEqual(len(mail.outbox), 2)

    def test_escalation_held_by_the_cooldown_is_delivered_when_it_ends(self):
        self.failing_sync()
        RoadConditionsConfiguration.objects.update(
            last_fetch_succeeded_at=dj_timezone.now() - timedelta(minutes=121))
        self.tick(5)
        self.assertEqual(self.subjects(), [SUBJECT + "WARNING"])
        self.age_cooldown()
        self.tick(5)
        self.assertEqual(self.subjects(), [SUBJECT + "WARNING", SUBJECT + "CRITICAL"])

    def test_stale_without_a_failed_fetch_notifies(self):
        # Retrievals simply stopped happening.
        RoadConditionsConfiguration.objects.update(
            last_fetch_succeeded_at=dj_timezone.now() - timedelta(hours=3))
        self.tick()
        self.assertEqual(self.subjects(), [SUBJECT + "CRITICAL"])

    def test_within_the_limit_is_not_stale(self):
        RoadConditionsConfiguration.objects.update(
            last_fetch_succeeded_at=dj_timezone.now() - timedelta(minutes=119))
        self.tick()
        self.assertEqual(mail.outbox, [])

    def test_narrowed_empty_sync_does_not_recover_or_consume_full_slot(self):
        self.failing_sync()
        first = RoadConditionsConfiguration.load()
        # Put the full attempt in the previous slot so the diagnostic
        # request will be the only invocation in the current one.
        prior_slot = dj_timezone.now() - timedelta(hours=1)
        RoadConditionsConfiguration.objects.update(last_fetch_attempted_at=prior_slot)
        self.sync(events=[], args=("--force-full", "--limit", "1"))
        second = RoadConditionsConfiguration.load()
        self.assertEqual(second.last_fetch_attempted_at, prior_slot)
        self.assertEqual(second.last_fetch_succeeded_at, first.last_fetch_succeeded_at)
        self.assertEqual(second.last_error, first.last_error)
        self.assertEqual(self.subjects(), [SUBJECT + "WARNING"])
        self.assertIsNone(self.stamp())
        self.assertIn("Fetched: 1", self.sync(args=()))
        self.assertEqual(self.subjects(), [SUBJECT + "WARNING", SUBJECT + "RECOVERED"])

    def test_recovery_is_reported_once(self):
        self.failing_sync()
        self.sync()
        self.tick(5)
        self.sync()
        self.assertEqual(self.subjects(), [SUBJECT + "WARNING", SUBJECT + "RECOVERED"])
        self.assertTrue(SystemEvent.objects.filter(title="Road Conditions Data: warning → ok").exists())

    def test_a_new_incident_after_recovery_notifies_again(self):
        self.failing_sync()
        self.sync()
        self.failing_sync()
        self.assertEqual(self.subjects(), [SUBJECT + "WARNING", SUBJECT + "RECOVERED", SUBJECT + "WARNING"])

    def test_no_recovery_message_when_nothing_could_be_delivered(self):
        NotificationConfig.objects.update(recipients="")
        self.failing_sync()
        NotificationConfig.objects.update(recipients="engineer@example.test")
        self.sync()
        self.assertEqual(mail.outbox, [])

    def test_recipients_added_during_an_incident_are_told(self):
        NotificationConfig.objects.update(recipients="")
        self.failing_sync()
        NotificationConfig.objects.update(recipients="engineer@example.test")
        self.tick()
        self.assertEqual(self.subjects(), [SUBJECT + "WARNING"])

    def test_disabled_notifications_send_nothing_but_still_record_events(self):
        NotificationConfig.objects.update(enabled=False)
        self.failing_sync()
        self.sync()
        self.assertEqual(mail.outbox, [])
        self.assertTrue(SystemEvent.objects.filter(title="Road Conditions Data: ok → warning").exists())

    def test_disabling_the_feature_closes_the_incident_silently(self):
        self.failing_sync()
        RoadConditionsConfiguration.objects.update(enabled=False)
        call_command("sync_road_conditions", stdout=StringIO())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(json.loads(alerts.STATE_PATH.read_text()),
                         {"status": "ok", "cooldowns": {}, "incidents": {}})

    def test_unwritable_state_sends_nothing_rather_than_every_minute(self):
        with patch.object(alerts, "STATE_PATH", self.runtime / "missing" / "state.json"):
            self.failing_sync()
            self.tick(10)
        self.assertEqual(mail.outbox, [])

    def test_broken_mail_server_does_not_fail_the_sync(self):
        RoadConditionsConfiguration.objects.update(
            last_fetch_succeeded_at=dj_timezone.now() - timedelta(hours=3))
        with patch("django.core.mail.send_mail", side_effect=smtplib.SMTPException("down")):
            self.tick(3)
        self.assertTrue(SystemEvent.objects.filter(title="Notification email delivery failed").exists())

    def test_sending_is_time_bounded_and_the_default_restored(self):
        seen = []
        before = socket.getdefaulttimeout()
        with patch("django.core.mail.send_mail", side_effect=lambda *a, **k: seen.append(socket.getdefaulttimeout())):
            self.failing_sync()
        self.assertEqual(seen, [alerts.SEND_TIMEOUT_SECONDS])
        self.assertEqual(socket.getdefaulttimeout(), before)

    def test_an_alerting_bug_never_fails_the_sync(self):
        with patch.object(alerts, "_evaluate", side_effect=RuntimeError("bug")):
            self.assertIn("Requested a road audio generation check", self.sync())

    def test_dry_run_neither_notifies_nor_records(self):
        RoadConditionsConfiguration.objects.update(
            last_fetch_succeeded_at=dj_timezone.now() - timedelta(hours=3))
        self.sync(args=("--force-full", "--dry-run"))
        self.assertEqual((mail.outbox, alerts.STATE_PATH.exists()), ([], False))


class GenerationRequestTests(RuntimeScratchMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.configure(last_fetch_attempted_at=None, last_fetch_succeeded_at=None)

    def test_successful_retrieval_requests_one_check(self):
        with patch.object(sync_command, "request_generation", wraps=trigger.request_generation) as request:
            out = self.sync(args=())
        self.assertEqual(request.call_count, 1)
        self.assertIn("Requested a road audio generation check", out)
        self.assertIsNotNone(self.stamp())

    def test_unchanged_road_events_still_request_a_check(self):
        self.sync()
        first = self.stamp()
        out = self.sync()
        self.assertIn("Unchanged: 1", out)
        self.assertNotEqual(self.stamp(), first)

    def test_not_due_failed_dry_narrowed_and_disabled_runs_request_nothing(self):
        self.tick(3)
        self.failing_sync()
        self.failing_sync(events=[])
        self.sync(args=("--force-full", "--dry-run"))
        for narrowing in (("--county", "Ottawa"), ("--limit", "1"), ("--event-type", "winterDriving")):
            self.sync(args=("--force-full", *narrowing))
        RoadConditionsConfiguration.objects.update(enabled=False)
        call_command("sync_road_conditions", stdout=StringIO())
        self.assertIsNone(self.stamp())

    def test_partial_retrieval_is_still_a_successful_one(self):
        self.sync(events=[make_event(), {"no": "event-id"}])
        self.assertIsNotNone(self.stamp())

    def test_unwritable_request_does_not_fail_the_sync(self):
        with patch.object(trigger, "TRIGGER_PATH", self.runtime / "missing" / "trigger"):
            out = self.sync()
        self.assertIn("could not be requested", out)
        config = RoadConditionsConfiguration.load()
        self.assertEqual(config.last_error, "")
        self.assertIsNotNone(config.last_fetch_succeeded_at)
        self.assertTrue(SystemEvent.objects.filter(title="Road audio generation could not be requested").exists())


class CommittedDataTests(RuntimeScratchMixin, TransactionTestCase):
    def test_road_data_is_committed_before_the_check_is_requested(self):
        self.configure(last_fetch_attempted_at=None)
        seen = []

        def another_process_looks():
            conn = psycopg2.connect(**connections["default"].get_connection_params())
            try:
                with conn, conn.cursor() as cur:
                    cur.execute(f"SELECT count(*) FROM {RoadEvent._meta.db_table}")
                    events = cur.fetchone()[0]
                    cur.execute(f"SELECT last_error, last_fetch_succeeded_at IS NOT NULL "
                                f"FROM {RoadConditionsConfiguration._meta.db_table}")
                    seen.append((events, *cur.fetchone()))
            finally:
                conn.close()
            return True

        with patch.object(sync_command, "request_generation", side_effect=another_process_looks):
            self.sync()
        self.assertEqual(seen, [(1, "", True)])


class EmptyDatasetAndTriggeredGenerationTests(RuntimeScratchMixin, KanDriveSynthesisFixtureMixin,
                                              TransactionTestCase):
    def setUp(self):
        super().setUp()
        make_fresh_config()
        self.configure(last_fetch_attempted_at=None)
        self.sync(events=[make_event(description="Road closed.")])
        call_command("generate_road_condition_audio", stdout=StringIO())
        self.assertEqual(self._kokoro_call_count, 1)

    def generate(self):
        out = StringIO()
        call_command("generate_road_condition_audio", stdout=out)
        return out.getvalue()

    def published(self):
        from library.models import Track
        return (road_report_path().read_bytes(), road_report_text_path().read_text(),
                Track.objects.filter(filepath=str(road_report_path())).values().get())

    def test_empty_complete_dataset_is_a_failed_retrieval_and_never_an_all_clear(self):
        before = self.published()
        succeeded = RoadConditionsConfiguration.load().last_fetch_succeeded_at
        stamp = self.stamp()
        self.failing_sync(events=[])
        config = RoadConditionsConfiguration.load()
        self.assertEqual(config.last_fetch_succeeded_at, succeeded)
        self.assertIn("empty event list", config.last_error)
        self.assertEqual(RoadEvent.objects.filter(source_active=True).count(), 1)
        self.assertEqual(RoadConditionsSyncRun.objects.order_by("-id").first().outcome, "failed")
        self.assertEqual(self.stamp(), stamp)
        self.assertEqual(self.subjects(), [SUBJECT + "WARNING"])
        self.assertIn("leaving", self.generate().lower().replace("is left", "leaving"))
        self.assertEqual((self.published(), self._kokoro_call_count), (before, 1))
        # The next valid retrieval recovers, with nothing to re-render.
        self.sync(events=[make_event(description="Road closed.")])
        self.assertIn("unchanged since last successful generation", self.generate())
        self.assertEqual(self.subjects()[-1], SUBJECT + "RECOVERED")
        self.assertEqual((self.published(), self._kokoro_call_count), (before, 1))

    def test_nonempty_dataset_with_nothing_in_coverage_publishes_the_clear_report(self):
        self.sync(events=[make_event("CARS5-ELSEWHERE", county="Sedgwick")])
        self.assertIsNotNone(self.stamp())
        self.generate()
        self.assertIn("not reporting any significant road conditions", road_report_text_path().read_text())

    def test_generator_failure_leaves_the_retrieval_successful_and_the_next_one_recovers(self):
        self.sync(events=[make_event(description="Both lanes are closed.")])
        run = RoadConditionsSyncRun.objects.order_by("-id").values().first()
        self._kokoro_should_fail = True
        with self.assertRaises(CommandError):
            self.generate()
        self.assertEqual(RoadConditionsSyncRun.objects.order_by("-id").values().first(), run)
        self.assertEqual(RoadConditionsConfiguration.load().last_error, "")
        self.assertEqual(mail.outbox, [])
        self._kokoro_should_fail = False
        first = self.stamp()
        self.sync(events=[make_event(description="Both lanes are closed.")])   # same data again
        self.assertNotEqual(self.stamp(), first)
        self.assertIn("render needed: reason=content_changed", self.generate())


class RetrievalSlotTests(TestCase):
    UTC = dt_timezone.utc

    def due(self, now, last, cadence):
        config = RoadConditionsConfiguration(poll_cadence_minutes=cadence, last_fetch_attempted_at=last)
        return sync_command._due(config, now)[0]

    def simulate(self, cadence, hours=24, tick_seconds=(62, 65, 70, 61), start=None):
        """One-minute timer ticks with their observed jitter."""
        now = start or datetime(2026, 11, 30, 0, 0, 13, tzinfo=self.UTC)
        end, last, fetches, index = now + timedelta(hours=hours), now - timedelta(minutes=1), [], 0
        while now < end:
            if self.due(now, last, cadence):
                fetches.append(now)
                last = now
            now += timedelta(seconds=tick_seconds[index % len(tick_seconds)])
            index += 1
        return fetches

    def test_thirty_minute_cadence_fetches_at_seven_and_thirty_seven_without_drift(self):
        fetches = self.simulate(30)
        self.assertEqual(len(fetches), 48)
        for moment in fetches:
            self.assertIn(moment.minute, (7, 8, 37, 38))
            slot = moment.replace(minute=7 if moment.minute < 30 else 37, second=0, microsecond=0)
            self.assertLessEqual((moment - slot).total_seconds(), 70)

    def test_other_cadences_stay_configurable(self):
        for cadence, minutes in ((15, {7, 22, 37, 52}), (60, {7}), (20, {7, 27, 47}), (120, {7})):
            with self.subTest(cadence=cadence):
                fetches = self.simulate(cadence)
                self.assertEqual(len(fetches), 24 * 60 // cadence)
                allowed = minutes | {m + 1 for m in minutes}
                self.assertTrue({f.minute for f in fetches} <= allowed)

    def test_anchored_minutes_avoid_quarter_hours_for_common_cadences(self):
        for cadence in (5, 10, 15, 20, 30, 45, 60, 90, 120):
            with self.subTest(cadence=cadence):
                starts = {sync_command._slot_start(
                    datetime(2026, 11, 30, tzinfo=self.UTC) + timedelta(minutes=m), cadence).minute
                    for m in range(0, 24 * 60)}
                self.assertFalse(starts & {0, 15, 30, 45}, starts)

    def test_local_minutes_are_unchanged_across_the_dst_change(self):
        zone = ZoneInfo("America/Chicago")
        for start in (datetime(2026, 11, 1, 4, 0, tzinfo=self.UTC), datetime(2027, 3, 14, 5, 0, tzinfo=self.UTC)):
            minutes = {f.astimezone(zone).minute for f in self.simulate(30, hours=6, start=start)}
            self.assertTrue(minutes <= {7, 8, 37, 38}, minutes)

    def test_after_downtime_or_first_start_it_fetches_at_once(self):
        now = datetime(2026, 11, 30, 12, 20, tzinfo=self.UTC)
        self.assertTrue(self.due(now, None, 30))
        self.assertTrue(self.due(now, now - timedelta(hours=5), 30))
        self.assertFalse(self.due(now, datetime(2026, 11, 30, 12, 7, 30, tzinfo=self.UTC), 30))
        self.assertTrue(self.due(datetime(2026, 11, 30, 12, 37, 1, tzinfo=self.UTC), now, 30))

    def test_a_cadence_edit_takes_effect_on_the_next_tick(self):
        last = datetime(2026, 11, 30, 12, 7, 30, tzinfo=self.UTC)
        now = datetime(2026, 11, 30, 12, 23, tzinfo=self.UTC)
        self.assertFalse(self.due(now, last, 30))
        self.assertTrue(self.due(now, last, 15))

    def test_zero_cadence_does_not_divide_by_zero(self):
        self.assertTrue(self.due(datetime(2026, 11, 30, 12, 23, tzinfo=self.UTC), None, 0))
