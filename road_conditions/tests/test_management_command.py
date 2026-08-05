"""sync_road_conditions management command tests -- CLI argument
wiring, output format, exit-code behavior, and the Postgres advisory-
lock overlap guard. sync_events() itself is mocked here (its own real
behavior is covered exhaustively in test_sync.py); these tests are
about the command's own responsibilities."""
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import psycopg2
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.test import TestCase
from django.utils import timezone

from road_conditions.api import CarsApiTimeout
from road_conditions.management.commands.sync_road_conditions import ROAD_CONDITIONS_LOCK_KEY
from road_conditions.models import RoadConditionsConfiguration, RoadConditionsSyncRun, RoadEvent

_SUCCESS_RESULT = dict(
    fetched_count=10, relevant_count=3, created_count=1, updated_count=1,
    unchanged_count=1, deactivated_count=0, error_count=0, outcome="success",
    latency_ms=120, error_message="",
)


class SyncRoadConditionsDisabledByDefaultTests(TestCase):
    def setUp(self):
        RoadConditionsConfiguration.load()  # enabled=False by default

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events")
    def test_disabled_config_skips_sync_entirely(self, mock_sync):
        out = StringIO()
        call_command("sync_road_conditions", stdout=out)
        mock_sync.assert_not_called()
        self.assertIn("disabled", out.getvalue().lower())

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events", return_value=_SUCCESS_RESULT)
    def test_force_full_runs_even_when_disabled(self, mock_sync):
        out = StringIO()
        call_command("sync_road_conditions", "--force-full", stdout=out)
        mock_sync.assert_called_once()
        self.assertIn("Fetched: 10", out.getvalue())


class SyncRoadConditionsOutputFormatTests(TestCase):
    def setUp(self):
        config = RoadConditionsConfiguration.load()
        config.enabled = True
        config.save()

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events", return_value=_SUCCESS_RESULT)
    def test_summary_lines_present(self, mock_sync):
        out = StringIO()
        call_command("sync_road_conditions", stdout=out)
        output = out.getvalue()
        self.assertIn("Fetched: 10", output)
        self.assertIn("Relevant: 3", output)
        self.assertIn("Created: 1", output)
        self.assertIn("Updated: 1", output)
        self.assertIn("Unchanged: 1", output)
        self.assertIn("Deactivated: 0", output)
        self.assertIn("Errors: 0", output)

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events", return_value=_SUCCESS_RESULT)
    def test_dry_run_prefixes_output(self, mock_sync):
        out = StringIO()
        call_command("sync_road_conditions", "--dry-run", stdout=out)
        self.assertIn("[DRY RUN] Fetched: 10", out.getvalue())
        _, kwargs = mock_sync.call_args
        self.assertTrue(kwargs["dry_run"])

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events")
    def test_partial_outcome_prints_warning(self, mock_sync):
        mock_sync.return_value = dict(_SUCCESS_RESULT, outcome="partial", error_count=2)
        out = StringIO()
        call_command("sync_road_conditions", stdout=out)
        self.assertIn("Partial sync", out.getvalue())
        self.assertIn("Errors: 2", out.getvalue())


class SyncRoadConditionsArgumentWiringTests(TestCase):
    def setUp(self):
        config = RoadConditionsConfiguration.load()
        config.enabled = True
        config.save()

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events", return_value=_SUCCESS_RESULT)
    def test_event_type_forwarded(self, mock_sync):
        call_command("sync_road_conditions", "--event-type", "winterDriving", stdout=StringIO())
        _, kwargs = mock_sync.call_args
        self.assertEqual(kwargs["event_classifications_filter"], ["winterDriving"])

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events", return_value=_SUCCESS_RESULT)
    def test_event_type_repeatable(self, mock_sync):
        call_command("sync_road_conditions", "--event-type", "winterDriving", "--event-type", "roadReports", stdout=StringIO())
        _, kwargs = mock_sync.call_args
        self.assertEqual(kwargs["event_classifications_filter"], ["winterDriving", "roadReports"])

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events", return_value=_SUCCESS_RESULT)
    def test_county_forwarded(self, mock_sync):
        call_command("sync_road_conditions", "--county", "Ottawa", stdout=StringIO())
        _, kwargs = mock_sync.call_args
        self.assertEqual(kwargs["county_filter"], "Ottawa")

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events", return_value=_SUCCESS_RESULT)
    def test_limit_forwarded(self, mock_sync):
        call_command("sync_road_conditions", "--limit", "5", stdout=StringIO())
        _, kwargs = mock_sync.call_args
        self.assertEqual(kwargs["limit"], 5)

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events", return_value=_SUCCESS_RESULT)
    def test_no_overrides_pass_none(self, mock_sync):
        call_command("sync_road_conditions", stdout=StringIO())
        _, kwargs = mock_sync.call_args
        self.assertIsNone(kwargs["event_classifications_filter"])
        self.assertIsNone(kwargs["county_filter"])
        self.assertIsNone(kwargs["limit"])


class SyncRoadConditionsNarrowedRunWarningTests(TestCase):
    """Item 2: --limit (and --county/--event-type) must print a clear
    warning that the source set this run sees is intentionally
    incomplete and that deactivation/re-scoping are disabled -- not
    just silently behave differently with no operator-visible signal."""

    def setUp(self):
        config = RoadConditionsConfiguration.load()
        config.enabled = True
        config.save()

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events", return_value=_SUCCESS_RESULT)
    def test_limit_prints_narrowed_warning(self, mock_sync):
        out = StringIO()
        call_command("sync_road_conditions", "--limit", "5", stdout=out)
        output = out.getvalue()
        self.assertIn("Narrowed run", output)
        self.assertIn("Deactivation", output)

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events", return_value=_SUCCESS_RESULT)
    def test_county_prints_narrowed_warning(self, mock_sync):
        out = StringIO()
        call_command("sync_road_conditions", "--county", "Ottawa", stdout=out)
        self.assertIn("Narrowed run", out.getvalue())

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events", return_value=_SUCCESS_RESULT)
    def test_event_type_prints_narrowed_warning(self, mock_sync):
        out = StringIO()
        call_command("sync_road_conditions", "--event-type", "winterDriving", stdout=out)
        self.assertIn("Narrowed run", out.getvalue())

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events", return_value=_SUCCESS_RESULT)
    def test_plain_run_prints_no_narrowed_warning(self, mock_sync):
        out = StringIO()
        call_command("sync_road_conditions", stdout=out)
        self.assertNotIn("Narrowed run", out.getvalue())


class SyncRoadConditionsExitCodeTests(TestCase):
    def setUp(self):
        config = RoadConditionsConfiguration.load()
        config.enabled = True
        config.save()

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events")
    def test_total_api_failure_raises_command_error(self, mock_sync):
        # call_command() (used in-process by tests) surfaces CommandError
        # as a raised exception rather than a process exit -- real CLI
        # invocation via manage.py turns any CommandError into a
        # nonzero process exit status via Django's own run_from_argv(),
        # which is standard framework behavior, not something this
        # project needs to separately test.
        mock_sync.side_effect = CarsApiTimeout("simulated total failure")
        with self.assertRaises(CommandError) as ctx:
            call_command("sync_road_conditions", stdout=StringIO())
        self.assertIn("simulated total failure", str(ctx.exception))

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events", return_value=_SUCCESS_RESULT)
    def test_success_does_not_raise(self, mock_sync):
        call_command("sync_road_conditions", stdout=StringIO())  # no exception == pass


class SyncRoadConditionsLockContentionTests(TestCase):
    """Real cross-connection test of the Postgres advisory-lock overlap
    guard -- opens a genuinely separate DB connection (advisory locks
    are session-scoped, so a second connection on the SAME session as
    the test transaction would not exercise real contention) and holds
    the lock from it while invoking the command."""

    def setUp(self):
        config = RoadConditionsConfiguration.load()
        config.enabled = True
        config.save()

    def test_command_exits_quietly_when_another_instance_holds_the_lock(self):
        params = connections["default"].get_connection_params()
        blocking_conn = psycopg2.connect(**params)
        try:
            with blocking_conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s, %s)", ROAD_CONDITIONS_LOCK_KEY)
                acquired = cur.fetchone()[0]
            self.assertTrue(acquired, "test setup failed to acquire its own lock")

            with patch("road_conditions.management.commands.sync_road_conditions.sync_events") as mock_sync:
                out = StringIO()
                call_command("sync_road_conditions", stdout=out)
                mock_sync.assert_not_called()
                self.assertIn("already running", out.getvalue())
            self.assertEqual(RoadEvent.objects.count(), 0)
        finally:
            with blocking_conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s, %s)", ROAD_CONDITIONS_LOCK_KEY)
            blocking_conn.close()

    def test_lock_is_released_after_a_normal_run_so_a_second_run_succeeds(self):
        with patch("road_conditions.management.commands.sync_road_conditions.sync_events", return_value=_SUCCESS_RESULT) as mock_sync:
            call_command("sync_road_conditions", stdout=StringIO())
            call_command("sync_road_conditions", stdout=StringIO())
            self.assertEqual(mock_sync.call_count, 2)

    def test_lock_is_released_even_when_sync_events_raises(self):
        with patch("road_conditions.management.commands.sync_road_conditions.sync_events") as mock_sync:
            mock_sync.side_effect = CarsApiTimeout("boom")
            with self.assertRaises(CommandError):
                call_command("sync_road_conditions", stdout=StringIO())

            mock_sync.side_effect = None
            mock_sync.return_value = _SUCCESS_RESULT
            call_command("sync_road_conditions", stdout=StringIO())  # would report "already running" if the lock leaked
            self.assertEqual(mock_sync.call_count, 2)


class SyncRoadConditionsDueCheckTests(TestCase):
    """Item 2: the timer fires far more often than the real cadence on
    purpose (see deploy/isadoraair-sync-road-conditions.timer) --
    sync_road_conditions itself must check
    RoadConditionsConfiguration.poll_cadence_minutes against
    last_fetch_attempted_at and skip cleanly when not due, so the
    timer's own frequent firing doesn't defeat the whole point of a
    configurable cadence (or blow through the CARS API's own bandwidth
    cost -- no pagination/caching, ~8MB per real fetch)."""

    def setUp(self):
        self.config = RoadConditionsConfiguration.load()
        self.config.enabled = True
        self.config.poll_cadence_minutes = 15
        self.config.save()

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events", return_value=_SUCCESS_RESULT)
    def test_skips_when_not_due(self, mock_sync):
        self.config.last_fetch_attempted_at = timezone.now() - timedelta(minutes=5)
        self.config.save()
        out = StringIO()
        call_command("sync_road_conditions", stdout=out)
        mock_sync.assert_not_called()
        self.assertIn("not due", out.getvalue().lower())

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events", return_value=_SUCCESS_RESULT)
    def test_runs_when_due(self, mock_sync):
        self.config.last_fetch_attempted_at = timezone.now() - timedelta(minutes=20)
        self.config.save()
        call_command("sync_road_conditions", stdout=StringIO())
        mock_sync.assert_called_once()

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events", return_value=_SUCCESS_RESULT)
    def test_runs_when_never_attempted(self, mock_sync):
        self.assertIsNone(self.config.last_fetch_attempted_at)
        call_command("sync_road_conditions", stdout=StringIO())
        mock_sync.assert_called_once()

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events", return_value=_SUCCESS_RESULT)
    def test_exactly_at_cadence_boundary_is_due(self, mock_sync):
        self.config.last_fetch_attempted_at = timezone.now() - timedelta(minutes=15, seconds=1)
        self.config.save()
        call_command("sync_road_conditions", stdout=StringIO())
        mock_sync.assert_called_once()

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events", return_value=_SUCCESS_RESULT)
    def test_force_full_bypasses_due_check(self, mock_sync):
        self.config.last_fetch_attempted_at = timezone.now() - timedelta(minutes=1)
        self.config.save()
        call_command("sync_road_conditions", "--force-full", stdout=StringIO())
        mock_sync.assert_called_once()

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events", return_value=_SUCCESS_RESULT)
    def test_not_due_skip_creates_no_sync_run_row(self, mock_sync):
        self.config.last_fetch_attempted_at = timezone.now() - timedelta(minutes=5)
        self.config.save()
        call_command("sync_road_conditions", stdout=StringIO())
        self.assertEqual(RoadConditionsSyncRun.objects.count(), 0)

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events", return_value=_SUCCESS_RESULT)
    def test_not_due_skip_does_not_touch_config_last_fetch_fields(self, mock_sync):
        attempted_at = timezone.now() - timedelta(minutes=5)
        self.config.last_fetch_attempted_at = attempted_at
        self.config.save()
        call_command("sync_road_conditions", stdout=StringIO())
        self.config.refresh_from_db()
        self.assertEqual(self.config.last_fetch_attempted_at, attempted_at)

    def test_not_due_skip_releases_lock_for_the_next_invocation(self):
        # Real lock-acquire/release path (no mocked sync_events needed,
        # since it's never called on the not-due path) -- proves a
        # "not due" quick-exit doesn't leak the advisory lock the way
        # a bug in the try/finally could.
        self.config.last_fetch_attempted_at = timezone.now() - timedelta(minutes=1)
        self.config.save()
        out1 = StringIO()
        call_command("sync_road_conditions", stdout=out1)
        self.assertIn("not due", out1.getvalue().lower())

        out2 = StringIO()
        call_command("sync_road_conditions", stdout=out2)
        self.assertNotIn("already running", out2.getvalue().lower())
        self.assertIn("not due", out2.getvalue().lower())

    @patch("road_conditions.management.commands.sync_road_conditions.sync_events", return_value=_SUCCESS_RESULT)
    def test_shorter_cadence_makes_a_recent_attempt_due_sooner(self, mock_sync):
        self.config.poll_cadence_minutes = 2
        self.config.last_fetch_attempted_at = timezone.now() - timedelta(minutes=5)
        self.config.save()
        call_command("sync_road_conditions", stdout=StringIO())
        mock_sync.assert_called_once()

    def test_disabled_check_happens_before_due_check(self):
        # A disabled config should report "disabled", not "not due" --
        # confirms the ordering (disabled gate first) even when a
        # last_fetch_attempted_at is recent enough to also be "not due".
        self.config.enabled = False
        self.config.last_fetch_attempted_at = timezone.now() - timedelta(minutes=1)
        self.config.save()
        out = StringIO()
        call_command("sync_road_conditions", stdout=out)
        self.assertIn("disabled", out.getvalue().lower())
