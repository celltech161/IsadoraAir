"""r0145: the NWS alerts fetch outcome record (nws_alert_fetch_status.json) and its log line.

No network and no real data directory: the NWS session is a double and every file lives in a private temporary
directory. The record is evidence for Weather Health only -- these tests also pin that it never changes the
snapshot, the coordinator run or the headline trigger."""
import json
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from weather_ingest.tests import test_alert_sources as sources
from weather_ingest.tests.test_alert_sources import feature, update_local_wx_data


class FetchStatusHarness(sources.NwsHarness):
    def setUp(self):
        super().setUp()
        self.status_path = self.tmp / "nws_alert_fetch_status.json"
        p = patch.object(update_local_wx_data, "NWS_ALERT_FETCH_STATUS_FILE", self.status_path)
        p.start()
        self.addCleanup(p.stop)

    def status(self):
        return json.loads(self.status_path.read_text())

    def run_cycle(self):
        with self.assertLogs(update_local_wx_data.log, level="DEBUG") as logs:
            update_local_wx_data.log.debug("keep-alive")     # assertLogs needs at least one record
            update_local_wx_data.update_wx_alerts()
        return logs.output


class FetchStatusTests(FetchStatusHarness):
    def test_a_successful_fetch_records_ok_and_clears_failing_since(self):
        self.cycle(feature())
        record = self.status()
        self.assertIs(record["ok"], True)
        self.assertEqual(record["status_code"], 200)
        self.assertIsNone(record["error"])
        self.assertIsNone(record["failing_since"])
        self.assertTrue(datetime.fromisoformat(record["checked_at"]).tzinfo)

    def test_an_empty_alert_list_is_still_a_success(self):
        self.cycle()
        self.assertIs(self.status()["ok"], True)

    def test_a_non_200_is_recorded_with_its_code_and_logged_with_the_code(self):
        self.cycle(feature())
        before = (self.tmp / "active_watches_warnings.json").read_bytes()
        self.feed(status_code=503)
        output = self.run_cycle()
        record = self.status()
        self.assertEqual((record["ok"], record["status_code"], record["error"]), (False, 503, "HTTP 503"))
        self.assertTrue(record["failing_since"])
        self.assertTrue(any("NWS alerts API returned HTTP 503" in line and "WARNING" in line for line in output), output)
        # evidence only: the snapshot is untouched and the coordinator still runs every cycle
        self.assertEqual((self.tmp / "active_watches_warnings.json").read_bytes(), before)
        self.assertEqual(self.coordinator_runs, 2)

    def test_failing_since_is_the_start_of_the_unbroken_run_of_failures(self):
        self.feed(status_code=503)
        update_local_wx_data.update_wx_alerts()
        first = self.status()
        update_local_wx_data.update_wx_alerts()
        second = self.status()
        self.assertEqual(second["failing_since"], first["failing_since"])
        self.assertGreaterEqual(second["checked_at"], first["checked_at"])
        # a different failure inside the same run keeps the same start
        with patch.object(update_local_wx_data.session, "get", side_effect=requests.ConnectionError("down")):
            update_local_wx_data.update_wx_alerts()
        self.assertEqual(self.status()["failing_since"], first["failing_since"])
        # recovery clears it; a later failure starts a NEW run
        self.cycle(feature())
        self.assertIsNone(self.status()["failing_since"])
        self.feed(status_code=500)
        update_local_wx_data.update_wx_alerts()
        self.assertNotEqual(self.status()["failing_since"], first["failing_since"])

    def test_a_connection_error_is_recorded_with_the_error_text(self):
        with patch.object(update_local_wx_data.session, "get", side_effect=requests.ConnectionError("no route to host")):
            update_local_wx_data.update_wx_alerts()
        record = self.status()
        self.assertIs(record["ok"], False)
        self.assertIsNone(record["status_code"])
        self.assertIn("ConnectionError", record["error"])
        self.assertIn("no route to host", record["error"])

    def test_an_unreadable_answer_is_a_failure_and_the_error_is_bounded(self):
        self.response = Mock(status_code=200)
        self.response.json.side_effect = ValueError("x" * 1000)
        update_local_wx_data.update_wx_alerts()
        record = self.status()
        self.assertIs(record["ok"], False)
        self.assertEqual(record["status_code"], 200)
        self.assertLessEqual(len(record["error"]), 200)
        self.assertFalse((self.tmp / "active_watches_warnings.json").exists())     # nothing was written

    def test_unwritable_status_file_never_breaks_the_cycle(self):
        with patch.object(update_local_wx_data, "safe_write_json", side_effect=OSError("disk full")):
            self.feed(status_code=503)
            update_local_wx_data.update_wx_alerts()
        self.assertEqual(self.coordinator_runs, 1)

    def test_a_corrupt_previous_record_is_replaced(self):
        self.status_path.write_text("{not json")
        self.feed(status_code=503)
        update_local_wx_data.update_wx_alerts()
        self.assertIs(self.status()["ok"], False)

    def test_the_recorded_success_matches_a_real_snapshot_write(self):
        """ok=True is recorded only after the snapshot was written, never for a fetch that failed afterwards."""
        self.cycle(feature())
        self.assertIs(self.status()["ok"], True)
        self.feed(status_code=503)
        update_local_wx_data.update_wx_alerts()
        self.assertIs(self.status()["ok"], False)


class StatusFileNameTests(unittest.TestCase):
    def test_the_status_filename_is_the_one_diagnostics_reads(self):
        from weather.diagnostics import NWS_ALERT_FETCH_STATUS_FILENAME
        self.assertEqual(Path(update_local_wx_data.NWS_ALERT_FETCH_STATUS_FILE).name,
                         NWS_ALERT_FETCH_STATUS_FILENAME)
        self.assertEqual(NWS_ALERT_FETCH_STATUS_FILENAME, "nws_alert_fetch_status.json")
