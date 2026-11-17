"""r0145: the NWS alerts fetch outcome record (nws_alert_fetch_status.json) and its log lines.

No network and no real data directory: the NWS session is a double and every file lives in a private temporary
directory. The record is evidence for Weather Health only -- these tests also pin that it never changes the
snapshot, the coordinator run or the headline trigger, that `ok: true` means the authoritative snapshot was really
written and renamed into place, and that a local publication failure is never described as an NWS failure."""
import builtins
import glob
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from weather_ingest.tests import test_alert_sources as sources
from weather_ingest.tests.test_alert_sources import feature, update_local_wx_data

REAL_OPEN = builtins.open
REAL_REPLACE = os.replace


def iso(dt):
    return dt.isoformat()


class FetchStatusHarness(sources.NwsHarness):
    def setUp(self):
        super().setUp()
        self.status_path = self.tmp / "nws_alert_fetch_status.json"
        self.snapshot_path = self.tmp / "active_watches_warnings.json"
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

    def staged_files(self):
        return glob.glob(str(self.tmp / "*.tmp"))

    def write_previous(self, **fields):
        now = datetime.now(timezone.utc)
        record = {"ok": False, "kind": "upstream", "checked_at": iso(now - timedelta(minutes=1)),
                  "status_code": 503, "error": "HTTP 503", "failing_since": iso(now - timedelta(minutes=6))}
        record.update(fields)
        self.status_path.write_text(json.dumps(record) if not isinstance(record, str) else record)
        return record

    def fail_upstream_once(self):
        self.feed(status_code=503)
        update_local_wx_data.update_wx_alerts()
        return self.status()


class FetchStatusTests(FetchStatusHarness):
    def test_a_successful_fetch_records_ok_and_clears_failing_since(self):
        self.cycle(feature())
        record = self.status()
        self.assertIs(record["ok"], True)
        self.assertIsNone(record["kind"])
        self.assertEqual(record["status_code"], 200)
        self.assertIsNone(record["error"])
        self.assertIsNone(record["failing_since"])
        self.assertTrue(datetime.fromisoformat(record["checked_at"]).tzinfo)

    def test_an_empty_alert_list_is_still_a_success(self):
        self.cycle()
        self.assertIs(self.status()["ok"], True)

    def test_a_non_200_is_recorded_as_upstream_with_its_code_and_logged(self):
        self.cycle(feature())
        before = self.snapshot_path.read_bytes()
        self.feed(status_code=503)
        output = self.run_cycle()
        record = self.status()
        self.assertEqual((record["ok"], record["kind"], record["status_code"], record["error"]),
                         (False, "upstream", 503, "HTTP 503"))
        self.assertTrue(record["failing_since"])
        self.assertTrue(any("NWS alerts API returned HTTP 503" in line and "WARNING" in line for line in output), output)
        # evidence only: the snapshot is untouched and the coordinator still runs every cycle
        self.assertEqual(self.snapshot_path.read_bytes(), before)
        self.assertEqual(self.coordinator_runs, 2)

    def test_a_connection_error_is_upstream_with_the_error_text(self):
        with patch.object(update_local_wx_data.session, "get", side_effect=requests.ConnectionError("no route to host")):
            update_local_wx_data.update_wx_alerts()
        record = self.status()
        self.assertEqual((record["ok"], record["kind"], record["status_code"]), (False, "upstream", None))
        self.assertIn("ConnectionError", record["error"])
        self.assertIn("no route to host", record["error"])

    def test_an_unreadable_200_answer_is_upstream_and_the_error_is_bounded(self):
        self.response = Mock(status_code=200)
        self.response.json.side_effect = ValueError("x" * 1000)
        update_local_wx_data.update_wx_alerts()
        record = self.status()
        self.assertEqual((record["ok"], record["kind"], record["status_code"]), (False, "upstream", 200))
        self.assertLessEqual(len(record["error"]), 200)
        self.assertFalse(self.snapshot_path.exists())

    def test_a_processing_error_after_a_good_answer_is_local_not_upstream(self):
        self.feed(feature())
        with patch.object(update_local_wx_data, "_dedup_similar_bodies", side_effect=RuntimeError("bug")):
            update_local_wx_data.update_wx_alerts()
        record = self.status()
        self.assertEqual((record["ok"], record["kind"], record["status_code"]), (False, "local", 200))
        self.assertIn("RuntimeError: bug", record["error"])

    def test_unwritable_status_file_never_breaks_the_cycle(self):
        with patch.object(update_local_wx_data, "safe_write_json", side_effect=OSError("disk full")):
            self.feed(status_code=503)
            update_local_wx_data.update_wx_alerts()
        self.assertEqual(self.coordinator_runs, 1)

    def test_a_corrupt_previous_record_is_replaced(self):
        self.status_path.write_text("{not json")
        self.fail_upstream_once()
        self.assertIs(self.status()["ok"], False)


class PublicationFailureTests(FetchStatusHarness):
    """HTTP 200 followed by a failed local publication of the authoritative snapshot (P1)."""

    @staticmethod
    def failing_open(target):
        def opener(file, *args, **kwargs):
            if str(file).startswith(str(target) + "."):            # the staged <snapshot>.<pid>.tmp file
                raise OSError(28, "No space left on device")
            return REAL_OPEN(file, *args, **kwargs)
        return opener

    @staticmethod
    def failing_replace(target):
        def replacer(src, dst, *args, **kwargs):
            if str(dst) == str(target):
                raise OSError(18, "Invalid cross-device link")
            return REAL_REPLACE(src, dst, *args, **kwargs)
        return replacer

    def assert_local_publication_failure(self, record, needle):
        self.assertIs(record["ok"], False)
        self.assertEqual(record["kind"], "local")
        self.assertEqual(record["status_code"], 200)
        self.assertIn("snapshot publication failed", record["error"])
        self.assertIn(needle, record["error"])
        self.assertTrue(record["failing_since"])

    def test_200_then_staged_file_write_failure_is_a_local_failure_never_ok(self):
        self.cycle(feature("Tornado Warning", etn="0001"))
        before = self.snapshot_path.read_bytes()
        self.feed(feature("Blizzard Warning", phen="BZ", etn="0003", what="Heavy snow."))
        with patch("builtins.open", side_effect=self.failing_open(self.snapshot_path)):
            output = self.run_cycle()
        self.assert_local_publication_failure(self.status(), "No space left on device")
        self.assertEqual(self.snapshot_path.read_bytes(), before)          # previous authoritative snapshot intact
        self.assertEqual(self.staged_files(), [])
        self.assertTrue(any("Error writing" in line and "ERROR" in line for line in output), output)

    def test_200_then_atomic_replacement_failure_is_a_local_failure_never_ok(self):
        self.cycle(feature("Tornado Warning", etn="0001"))
        before = self.snapshot_path.read_bytes()
        self.feed(feature("Blizzard Warning", phen="BZ", etn="0003", what="Heavy snow."))
        with patch("os.replace", side_effect=self.failing_replace(self.snapshot_path)):
            update_local_wx_data.update_wx_alerts()
        self.assert_local_publication_failure(self.status(), "Invalid cross-device link")
        self.assertEqual(self.snapshot_path.read_bytes(), before)
        self.assertEqual(self.staged_files(), [], "the staged file is removed on failure")

    def test_publication_failure_without_an_existing_snapshot(self):
        self.assertFalse(self.snapshot_path.exists())
        self.feed(feature())
        with patch("os.replace", side_effect=self.failing_replace(self.snapshot_path)):
            update_local_wx_data.update_wx_alerts()
        self.assert_local_publication_failure(self.status(), "Invalid cross-device link")
        self.assertFalse(self.snapshot_path.exists())                      # nothing was published
        self.assertEqual(self.staged_files(), [])

    def test_success_is_not_inferred_from_the_destination_existing(self):
        """The old snapshot is still there after a failed publication; the record must not claim success."""
        self.cycle(feature())
        self.assertIs(self.status()["ok"], True)
        self.feed(feature("Blizzard Warning", phen="BZ", etn="0003"))
        with patch("os.replace", side_effect=self.failing_replace(self.snapshot_path)):
            update_local_wx_data.update_wx_alerts()
        self.assertTrue(self.snapshot_path.exists())
        self.assertIs(self.status()["ok"], False)

    def test_the_rest_of_the_cycle_is_unchanged_by_a_publication_failure(self):
        self.cycle(feature("Tornado Warning", etn="0001"))               # baseline fingerprint, coordinator run 1
        self.feed(feature("Blizzard Warning", phen="BZ", etn="0003", what="Heavy snow."))
        with patch("os.replace", side_effect=self.failing_replace(self.snapshot_path)):
            update_local_wx_data.update_wx_alerts()
        self.assertEqual(self.coordinator_runs, 2)                        # coordinator still runs
        self.assertEqual(self.current_temp_runs, 1)                       # headline change still triggers
        self.assertIn("Blizzard Warning", (self.tmp / "warn.txt").read_text())   # warn.txt still written

    def test_a_status_write_failure_on_top_of_a_publication_failure_stays_nonfatal(self):
        self.feed(feature())

        def opener(file, *args, **kwargs):
            if str(file).startswith(str(self.snapshot_path) + ".") or str(file).startswith(str(self.status_path) + "."):
                raise OSError("read-only filesystem")
            return REAL_OPEN(file, *args, **kwargs)
        with patch("builtins.open", side_effect=opener):
            update_local_wx_data.update_wx_alerts()
        self.assertEqual(self.coordinator_runs, 1)
        self.assertFalse(self.snapshot_path.exists())

    def test_recovery_after_a_publication_failure_records_ok_and_clears_the_failure(self):
        self.feed(feature())
        with patch("os.replace", side_effect=self.failing_replace(self.snapshot_path)):
            update_local_wx_data.update_wx_alerts()
        self.assertIs(self.status()["ok"], False)
        entries = self.cycle(feature())
        self.assertEqual(len(entries), 1)
        record = self.status()
        self.assertEqual((record["ok"], record["kind"], record["error"], record["failing_since"]),
                         (True, None, None, None))

    def test_a_local_failure_run_keeps_its_start_and_does_not_inherit_an_upstream_start(self):
        upstream = self.fail_upstream_once()
        self.feed(feature())
        with patch("os.replace", side_effect=self.failing_replace(self.snapshot_path)):
            update_local_wx_data.update_wx_alerts()
            first_local = self.status()
            update_local_wx_data.update_wx_alerts()
        self.assertEqual(first_local["kind"], "local")
        self.assertNotEqual(first_local["failing_since"], upstream["failing_since"])
        self.assertEqual(self.status()["failing_since"], first_local["failing_since"])

    def test_upstream_and_local_failures_are_distinguished(self):
        kinds = {}
        self.feed(status_code=503)
        update_local_wx_data.update_wx_alerts()
        kinds["http"] = self.status()["kind"]
        with patch.object(update_local_wx_data.session, "get", side_effect=requests.Timeout("slow")):
            update_local_wx_data.update_wx_alerts()
        kinds["network"] = self.status()["kind"]
        self.feed(feature())
        with patch("os.replace", side_effect=self.failing_replace(self.snapshot_path)):
            update_local_wx_data.update_wx_alerts()
        kinds["publication"] = self.status()["kind"]
        self.assertEqual(kinds, {"http": "upstream", "network": "upstream", "publication": "local"})


class PreviousFailureHistoryTests(FetchStatusHarness):
    """Which previous failure records may supply `failing_since` (P2)."""

    def start_after(self, **overrides):
        previous = self.write_previous(**overrides)
        new = self.fail_upstream_once()
        return previous, new

    def assert_carried(self, previous, new):
        self.assertEqual(datetime.fromisoformat(new["failing_since"]), datetime.fromisoformat(previous["failing_since"]))

    def assert_restarted(self, previous, new):
        started = datetime.fromisoformat(new["failing_since"])
        self.assertNotEqual(new["failing_since"], previous.get("failing_since"))
        self.assertLess(abs((datetime.now(timezone.utc) - started).total_seconds()), 30)
        self.assertEqual(started, datetime.fromisoformat(new["checked_at"]))

    def test_a_valid_continuous_failure_history_is_carried_forward(self):
        self.assert_carried(*self.start_after())

    def test_a_valid_history_with_a_non_utc_offset_is_carried_forward(self):
        now = datetime.now(timezone.utc)
        tz = timezone(timedelta(hours=-6))
        previous, new = self.start_after(checked_at=iso((now - timedelta(minutes=1)).astimezone(tz)),
                                         failing_since=iso((now - timedelta(minutes=9)).astimezone(tz)))
        self.assert_carried(previous, new)

    def test_failing_since_equal_to_checked_at_is_valid(self):
        stamp = iso(datetime.now(timezone.utc) - timedelta(minutes=2))
        self.assert_carried(*self.start_after(checked_at=stamp, failing_since=stamp))

    def test_invalid_previous_histories_start_a_new_run(self):
        now = datetime.now(timezone.utc)
        naive = lambda dt: dt.replace(tzinfo=None).isoformat()
        cases = {
            "naive checked_at": dict(checked_at=naive(now - timedelta(minutes=1))),
            "naive failing_since": dict(failing_since=naive(now - timedelta(minutes=6))),
            "future checked_at": dict(checked_at=iso(now + timedelta(minutes=5))),
            "future failing_since": dict(failing_since=iso(now + timedelta(minutes=5)),
                                         checked_at=iso(now + timedelta(minutes=6))),
            "failing_since after checked_at": dict(checked_at=iso(now - timedelta(minutes=5)),
                                                   failing_since=iso(now - timedelta(minutes=1))),
            "malformed checked_at": dict(checked_at="yesterday"),
            "malformed failing_since": dict(failing_since="garbage"),
            "non-string timestamps": dict(checked_at=12345, failing_since=67890),
            "missing failing_since": dict(failing_since=None),
            "previous was a success": dict(ok=True, kind=None, failing_since=None),
            "old schema without kind": dict(kind=None),
            "unknown kind": dict(kind="mystery"),
            "a different kind of failure": dict(kind="local"),
            "ok is not a boolean": dict(ok="no"),
            "too old to be continuous": dict(checked_at=iso(now - timedelta(minutes=16)),
                                             failing_since=iso(now - timedelta(minutes=30))),
        }
        for name, overrides in cases.items():
            with self.subTest(name):
                self.status_path.unlink(missing_ok=True)
                self.assert_restarted(*self.start_after(**overrides))

    def test_non_object_previous_records_start_a_new_run(self):
        for text in ("[]", "null", '"x"', "12", "{not json", ""):
            with self.subTest(text):
                self.status_path.write_text(text)
                new = self.fail_upstream_once()
                self.assertEqual(datetime.fromisoformat(new["failing_since"]), datetime.fromisoformat(new["checked_at"]))

    def test_the_freshness_boundary_of_the_previous_record(self):
        now = datetime.now(timezone.utc)
        inside = dict(checked_at=iso(now - timedelta(seconds=update_local_wx_data.FETCH_STATUS_FRESH_SECONDS - 30)),
                      failing_since=iso(now - timedelta(minutes=30)))
        outside = dict(checked_at=iso(now - timedelta(seconds=update_local_wx_data.FETCH_STATUS_FRESH_SECONDS + 30)),
                       failing_since=iso(now - timedelta(minutes=30)))
        self.assert_carried(*self.start_after(**inside))
        self.status_path.unlink()
        self.assert_restarted(*self.start_after(**outside))


class ContractPinTests(unittest.TestCase):
    def test_constants_match_the_diagnostics_contract(self):
        from weather import diagnostics
        from weather.freshness import CURRENT_DERIVED_FRESHNESS_SECONDS
        self.assertEqual(Path(update_local_wx_data.NWS_ALERT_FETCH_STATUS_FILE).name,
                         diagnostics.NWS_ALERT_FETCH_STATUS_FILENAME)
        self.assertEqual(diagnostics.NWS_ALERT_FETCH_STATUS_FILENAME, "nws_alert_fetch_status.json")
        self.assertEqual(update_local_wx_data.FETCH_STATUS_FRESH_SECONDS, CURRENT_DERIVED_FRESHNESS_SECONDS)
        self.assertEqual({update_local_wx_data.FETCH_FAILURE_UPSTREAM, update_local_wx_data.FETCH_FAILURE_LOCAL},
                         set(diagnostics._FETCH_FAILURE_KINDS))


class AtomicWriterContractTests(unittest.TestCase):
    """write_json_atomic raises; safe_write_json keeps every other caller's log-and-continue contract."""

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="isadoraair-atomic-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.target = self.tmp / "data.json"

    def test_success_replaces_the_content_and_leaves_no_staged_file(self):
        self.target.write_text('{"old":1}')
        update_local_wx_data.write_json_atomic(self.target, {"new": 2})
        self.assertEqual(json.loads(self.target.read_text()), {"new": 2})
        self.assertEqual(glob.glob(str(self.tmp / "*.tmp")), [])
        self.assertIs(update_local_wx_data.safe_write_json(self.target, {"newer": 3}), True)

    def test_failure_raises_preserves_the_previous_content_and_cleans_up(self):
        self.target.write_text('{"old":1}')
        with patch("os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                update_local_wx_data.write_json_atomic(self.target, {"new": 2})
        self.assertEqual(json.loads(self.target.read_text()), {"old": 1})
        self.assertEqual(glob.glob(str(self.tmp / "*.tmp")), [])

    def test_an_unserialisable_object_raises_and_cleans_up(self):
        with self.assertRaises(TypeError):
            update_local_wx_data.write_json_atomic(self.target, {"x": object()})
        self.assertFalse(self.target.exists())
        self.assertEqual(glob.glob(str(self.tmp / "*.tmp")), [])

    def test_safe_write_json_logs_swallows_and_reports_false(self):
        self.target.write_text('{"old":1}')
        with patch("os.replace", side_effect=OSError("boom")), \
                self.assertLogs(update_local_wx_data.log, level="ERROR") as logs:
            result = update_local_wx_data.safe_write_json(self.target, {"new": 2})
        self.assertIs(result, False)
        self.assertTrue(any(f"Error writing {self.target}: boom" in line for line in logs.output), logs.output)
        self.assertEqual(json.loads(self.target.read_text()), {"old": 1})
        self.assertEqual(glob.glob(str(self.tmp / "*.tmp")), [])
