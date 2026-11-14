"""r0137: the combined Weather/AMBER spoken-alert coordinator (wx_alert.py).

Pure unit coverage, no Django, no network, no TTS/ffmpeg: rendering, publication and the engine nudge are
replaced by recording doubles; snapshots, the state record and the published file live in a private temporary
directory.  The publication double replaces the canonical file the way publish_weather_asset does (same-directory
copy, atomic rename, mtime preserved).
"""
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT.parent))
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

from isadoraair import wx_alert_state  # noqa: E402
from weather_ingest.tests.test_speech_paths import wx_alert, amber_alert  # noqa: E402,F401  (patched import)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


NOW = datetime.now(timezone.utc)
LATER = NOW + timedelta(hours=2)


def nws(key, text, *, event="Tornado Warning", expires=LATER, ends=None):
    return {"event": event, "text": text, "text_core": text, "key": key,
            "expires": iso(expires) if expires else None, "ends": iso(ends) if ends else None}


def amber(identifier, text, *, event="Child Abduction Emergency", expires=LATER):
    return {"identifier": identifier, "event": event, "event_code": "CAE", "headline": "AMBER Alert",
            "text": text, "text_core": text, "expires": iso(expires) if expires else None, "areas": ["020169"]}


class CoordinatorHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="isadoraair-wxalert-coordinator-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.library_root = self.tmp / "music"
        self.dest = wx_alert_state.audio_path(self.library_root)
        self.rendered = []          # spoken texts of every rendered candidate
        self.published = []         # spoken texts of every publication
        self.nudges = 0
        self.publish_error = None
        self.on_render = None
        patches = [
            patch.object(wx_alert, "WX_WATCHWARN_FILE", self.tmp / "active_watches_warnings.json"),
            patch.object(wx_alert, "AMBER_FILE", self.tmp / "active_amber_alerts.json"),
            patch.object(wx_alert, "STATE_FILE", self.tmp / "wx_alert_state.json"),
            patch.object(wx_alert, "LOCKFILE", self.tmp / ".lock"),
            patch.object(wx_alert, "TMP_DIR", str(self.tmp / "render")),
            patch.object(wx_alert.delivery, "LIBRARY_ROOT", self.library_root),
            patch.object(wx_alert, "render", side_effect=self._render),
            patch.object(wx_alert, "deliver", side_effect=self._deliver),
            patch.object(wx_alert, "fire_insert_urgent", side_effect=self._nudge),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    # -- doubles -----------------------------------------------------------------------------------
    def _render(self, alerts):
        texts = [a["text"] for a in alerts]
        self.rendered.append(texts)
        out = self.tmp / f"candidate-{len(self.rendered)}.flac"
        out.write_text("|".join(texts))
        if self.on_render:
            self.on_render()
        return str(out), {"name": "Max"}

    def _deliver(self, local_path, category_code, filename, **kwargs):
        if self.publish_error:
            raise self.publish_error
        self.dest.parent.mkdir(parents=True, exist_ok=True)
        staging = self.dest.parent / ".staging"
        shutil.copy2(local_path, staging)
        staging.replace(self.dest)
        self.published.append(Path(local_path).read_text().split("|"))
        return self.dest

    def _nudge(self):
        self.nudges += 1

    # -- helpers -----------------------------------------------------------------------------------
    def sources(self, nws_entries=None, amber_entries=None):
        if nws_entries is not None:
            (self.tmp / "active_watches_warnings.json").write_text(json.dumps(nws_entries))
        if amber_entries is not None:
            (self.tmp / "active_amber_alerts.json").write_text(json.dumps(amber_entries))

    def state(self):
        state, error = wx_alert_state.read_state(self.tmp / "wx_alert_state.json")
        self.assertIsNone(error)
        return state

    def run_pass(self, now=None):
        return wx_alert.coordinate(now=now)

    def baseline(self):
        self.sources([], [])
        self.assertTrue(self.run_pass())
        self.assertTrue(self.state()["baseline"])


class CombinedAnnouncementTests(CoordinatorHarness):
    def setUp(self):
        super().setUp()
        self.baseline()

    def test_nws_only_announcement(self):
        self.sources([nws("TOP.TO.W.0042", "A tornado warning is in effect.")], [])
        self.assertTrue(self.run_pass())
        self.assertEqual(self.published, [["A tornado warning is in effect."]])
        state = self.state()
        self.assertEqual((state["generation"], state["active"], self.nudges), (1, True, 1))
        self.assertEqual(state["audio"], wx_alert_state.audio_identity(self.dest))

    def test_amber_only_announcement(self):
        self.sources([], [amber("CAP-1", "An AMBER alert has been issued.")])
        self.run_pass()
        self.assertEqual(self.published, [["An AMBER alert has been issued."]])
        self.assertTrue(self.state()["active"])

    def test_combined_announcement_is_nws_then_amber_in_one_recording(self):
        self.sources([nws("TOP.TO.W.1", "Tornado text.")], [amber("CAP-1", "Amber text.")])
        self.run_pass()
        self.assertEqual(self.published, [["Tornado text.", "Amber text."]])
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()), ["wx_alert.flac"])

    def test_amber_arriving_rebuilds_one_announcement_containing_both(self):
        self.sources([nws("TOP.TO.W.1", "Tornado text.")], [])
        self.run_pass()
        self.sources(None, [amber("CAP-1", "Amber text.")])
        self.run_pass()
        self.assertEqual(self.published, [["Tornado text."], ["Tornado text.", "Amber text."]])
        self.assertEqual(self.state()["generation"], 2)

    def test_nws_text_change_with_unchanged_identity_republishes(self):
        self.sources([nws("TOP.TO.W.1", "Tornado until 4 PM.")], [])
        self.run_pass()
        self.sources([nws("TOP.TO.W.1", "Tornado until 4 PM. Take shelter in a basement now.")], None)
        self.run_pass()
        self.assertEqual(len(self.published), 2)
        self.assertEqual(self.published[-1], ["Tornado until 4 PM. Take shelter in a basement now."])

    def test_amber_text_change_with_unchanged_cap_identifier_republishes(self):
        self.sources([], [amber("CAP-1", "Suspect vehicle a blue sedan.")])
        self.run_pass()
        self.sources(None, [amber("CAP-1", "Suspect vehicle a red pickup.")])
        self.run_pass()
        self.assertEqual(self.published[-1], ["Suspect vehicle a red pickup."])

    def test_one_source_changing_keeps_the_other_sources_active_alerts(self):
        self.sources([nws("TOP.BZ.W.3", "Blizzard text.")], [amber("CAP-1", "Amber one.")])
        self.run_pass()
        self.sources(None, [amber("CAP-1", "Amber one."), amber("CAP-2", "Amber two.")])
        self.run_pass()
        self.assertEqual(self.published[-1], ["Blizzard text.", "Amber one.", "Amber two."])

    def test_unchanged_content_neither_renders_nor_publishes_again_but_re_nudges(self):
        self.sources([nws("TOP.TO.W.1", "Tornado text.")], [amber("CAP-1", "Amber text.")])
        self.run_pass()
        for _ in range(3):
            self.assertTrue(self.run_pass())
        self.assertEqual((len(self.rendered), len(self.published)), (1, 1))
        self.assertEqual(self.state()["generation"], 1)
        self.assertEqual(self.nudges, 4)          # idempotent synchronization nudges only

    def test_feed_order_does_not_change_the_fingerprint(self):
        a, b = amber("CAP-A", "Amber A."), amber("CAP-B", "Amber B.")
        self.sources([], [a, b])
        self.run_pass()
        self.sources(None, [b, a])
        self.run_pass()
        self.assertEqual(len(self.published), 1)

    def test_one_expired_alert_is_removed_from_the_replacement_audio(self):
        soon = NOW + timedelta(minutes=10)
        self.sources([nws("TOP.TO.W.1", "Tornado text.", expires=soon)], [amber("CAP-1", "Amber text.")])
        self.run_pass(now=NOW)
        self.run_pass(now=soon + timedelta(seconds=1))      # feed outage: the snapshot still lists it
        self.assertEqual(self.published, [["Tornado text.", "Amber text."], ["Amber text."]])
        state = self.state()
        self.assertEqual(state["generation"], 2)
        self.assertEqual(state["valid_until"], iso(LATER))

    def test_all_alerts_expired_or_cancelled_makes_the_announcement_inactive_without_rendering(self):
        self.sources([nws("TOP.TO.W.1", "Tornado text.")], [])
        self.run_pass()
        self.sources([], None)
        self.run_pass()
        state = self.state()
        self.assertEqual((state["active"], state["generation"], state["fingerprint"]), (False, 2, ""))
        self.assertEqual(len(self.rendered), 1)
        self.assertEqual(self.nudges, 1)

    def test_valid_until_is_the_earliest_authoritative_deadline_and_refreshes_without_speech(self):
        first = NOW + timedelta(hours=1)
        self.sources([nws("TOP.WS.W.9", "Winter storm text.", expires=first, ends=NOW + timedelta(days=2))], [])
        self.run_pass(now=NOW)
        self.assertEqual(self.state()["valid_until"], iso(first))
        extended = NOW + timedelta(hours=4)
        self.sources([nws("TOP.WS.W.9", "Winter storm text.", expires=extended, ends=NOW + timedelta(days=2))], None)
        self.run_pass(now=NOW)
        self.assertEqual(self.state()["valid_until"], iso(extended))
        self.assertEqual(len(self.published), 1)

    def test_an_alert_without_any_authoritative_expiration_is_conservatively_excluded(self):
        self.sources([], [amber("CAP-1", "Amber text.", expires=None)])
        self.run_pass()
        self.assertEqual(self.published, [])
        self.assertIn(["amber", "CAP-1", "no authoritative expiration"], self.state()["excluded"])

    def test_a_to_b_to_a_is_three_genuine_announcements(self):
        for text in ("Content A.", "Content B.", "Content A."):
            self.sources([nws("TOP.TO.W.1", text)], [])
            self.run_pass()
        self.assertEqual(self.published, [["Content A."], ["Content B."], ["Content A."]])
        self.assertEqual(self.state()["generation"], 3)

    def test_repeated_publications_never_accumulate_versioned_files(self):
        for i in range(4):
            self.sources([nws("TOP.TO.W.1", f"Revision {i}.")], [])
            self.run_pass()
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()), ["wx_alert.flac"])


class FailureAndConcurrencyTests(CoordinatorHarness):
    def setUp(self):
        super().setUp()
        self.baseline()

    def test_unreadable_source_snapshot_changes_nothing(self):
        self.sources([nws("TOP.TO.W.1", "Tornado text.")], [])
        self.run_pass()
        (self.tmp / "active_amber_alerts.json").write_text("{not json")
        self.sources([], None)                                  # would otherwise deactivate
        self.assertFalse(self.run_pass())
        state = self.state()
        self.assertEqual((state["generation"], state["active"]), (1, True))
        self.assertIn("source snapshot unreadable", state["last_error"])

    def test_a_missing_amber_snapshot_means_no_known_amber_alerts(self):
        (self.tmp / "active_amber_alerts.json").unlink()
        self.sources([nws("TOP.TO.W.1", "Tornado text.")], None)
        self.assertTrue(self.run_pass())
        self.assertEqual(self.state()["sources"], {"nws": "ok", "amber": "missing"})

    def test_publication_failure_keeps_the_previous_generation_and_retries_next_pass(self):
        self.sources([nws("TOP.TO.W.1", "First.")], [])
        self.run_pass()
        before = self.state()
        self.sources([nws("TOP.TO.W.1", "Second.")], None)
        self.publish_error = RuntimeError("publish_weather_asset exited 1")
        self.assertFalse(self.run_pass())
        failed = self.state()
        self.assertEqual({k: failed[k] for k in ("generation", "fingerprint", "audio", "published_at")},
                         {k: before[k] for k in ("generation", "fingerprint", "audio", "published_at")})
        self.assertIn("publication failed", failed["last_error"])
        self.publish_error = None
        self.assertTrue(self.run_pass())
        self.assertEqual(self.published, [["First."], ["Second."]])
        self.assertEqual((self.state()["generation"], self.state()["last_error"]), (2, None))
        self.assertTrue(self.run_pass())
        self.assertEqual(len(self.published), 2)                # no duplicate after the retry

    def test_a_failed_nudge_does_not_republish_and_is_retried_by_the_next_pass(self):
        self.sources([nws("TOP.TO.W.1", "Tornado text.")], [])
        with patch.object(wx_alert, "fire_insert_urgent", side_effect=RuntimeError("queue full")):
            self.assertFalse(self.run_pass())
        self.assertEqual(self.state()["generation"], 1)
        self.assertTrue(self.run_pass())
        self.assertEqual((len(self.published), self.nudges), (1, 1))

    def test_a_source_update_during_synthesis_discards_the_obsolete_candidate(self):
        self.sources([nws("TOP.TO.W.1", "Old text.")], [])
        self.on_render = lambda: self.sources([nws("TOP.TO.W.1", "Newer text.")], None)
        self.assertTrue(self.run_pass())
        self.assertEqual(self.published, [])
        self.assertTrue(self.state()["baseline"])                 # nothing advanced
        self.on_render = None
        self.run_pass()
        self.assertEqual(self.published, [["Newer text."]])

    def test_an_externally_replaced_file_is_republished_on_the_next_unchanged_pass(self):
        self.sources([nws("TOP.TO.W.1", "Tornado text.")], [])
        self.run_pass()
        time.sleep(0.01)
        self.dest.write_text("something else")                    # not the recorded generation's audio
        self.run_pass()
        self.assertEqual(len(self.published), 2)
        self.assertEqual(self.state()["audio"], wx_alert_state.audio_identity(self.dest))

    def test_a_second_coordinator_waits_for_the_lock_instead_of_being_dropped(self):
        holder = wx_alert.acquire_lock(self.tmp / ".lock")
        released = threading.Timer(0.6, holder.close)
        released.start()
        started = time.monotonic()
        second = wx_alert.acquire_lock(self.tmp / ".lock", wait_seconds=5)
        self.assertGreaterEqual(time.monotonic() - started, 0.4)
        second.close()

    def test_lock_wait_is_bounded(self):
        holder = wx_alert.acquire_lock(self.tmp / ".lock")
        self.addCleanup(holder.close)
        with self.assertRaises(wx_alert.CoordinatorLockTimeout):
            wx_alert.acquire_lock(self.tmp / ".lock", wait_seconds=0.6)


class FirstRunTests(CoordinatorHarness):
    def test_first_run_records_current_alerts_as_an_unannounced_baseline_then_announces_changes(self):
        self.sources([nws("TOP.TO.W.1", "Already active at install.")], [])
        self.assertTrue(self.run_pass())
        state = self.state()
        self.assertEqual((state["baseline"], state["active"], state["generation"]), (True, False, 0))
        self.run_pass()
        self.assertEqual((self.published, self.nudges), ([], 0))
        self.sources([nws("TOP.TO.W.1", "Already active at install."), nws("TOP.SV.W.2", "New storm.")], None)
        self.run_pass()
        self.assertEqual(self.published, [["Already active at install.", "New storm."]])
        self.assertEqual((self.state()["active"], self.state()["generation"]), (True, 1))

    def test_a_pre_upgrade_nws_snapshot_holds_the_first_run_until_the_nws_poll_rewrites_it(self):
        legacy = [{"event": "Tornado Warning", "text": "Active at install.", "text_core": "Active at install."}]
        self.sources(legacy, [amber("CAP-1", "Amber at install.")])
        self.assertTrue(self.run_pass())                     # the AMBER poll ran first after installation
        self.assertFalse((self.tmp / "wx_alert_state.json").exists())
        self.sources([nws("TOP.TO.W.1", "Active at install.")], None)
        self.run_pass()
        self.assertTrue(self.state()["baseline"])
        self.run_pass()
        self.assertEqual((self.published, self.nudges), ([], 0))   # alerts present at install stay silent

    def test_a_malformed_state_record_is_rebuilt_and_announces_current_content(self):
        (self.tmp / "wx_alert_state.json").write_text("{broken")
        self.sources([nws("TOP.TO.W.1", "Tornado text.")], [])
        self.run_pass()
        self.assertEqual(self.published, [["Tornado text."]])


if __name__ == "__main__":
    unittest.main()
