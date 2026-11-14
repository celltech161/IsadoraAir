"""Console / Remote DJ log display: the Intro column and "Show played items".

Server side (no browser): the engine's queue entries carry the Track's own
``intro_until_seconds`` (None, 0 and real values all survive); the read-only
active-log endpoint the Show-played display merges with the live queue; the
station-wide ``UITheme.show_played_items`` switch (default Off, in the admin,
rendered into both Console pages); and the facts the display relies on --
Remote DJ can reach the new endpoint through the existing status prefix with
no access-map change, other restricted groups still cannot.

The browser behaviour (rows, colours, outline, reorder safety) lives in
test_console_log_display_browser.py.
"""
import json
import tempfile
import time
from datetime import date
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.utils import timezone

from library.models import (
    Artist, Category, CategoryKind, LogItem, PlaylistLog, Track, UITheme,
)
from library.tests.cue_marker_support import set_markers
from library.tests.test_engine_queue_eta import QueueEtaFixtureMixin, make_stand_in

User = get_user_model()


class IntroSerializationTests(QueueEtaFixtureMixin, TestCase):
    """The engine's queue array exposes the existing marker, untouched."""

    def _queue_for(self, *intro_values):
        stand_in = make_stand_in()
        items = []
        for index, intro in enumerate(intro_values, start=1):
            item = self.make_queued_item(next_start_seconds=200.0, position=index)
            if intro is not None:      # a new Track's marker is already unset (None)
                set_markers(item.track_id, intro_until_seconds=intro)
            items.append(LogItem.objects.select_related("track").get(pk=item.pk))
        stand_in.log_items = items
        return stand_in._compute_queue_eta_state(snapshot=stand_in.decks)

    def test_populated_null_and_zero_markers_are_carried_as_they_are(self):
        queue = self._queue_for(12.4, None, 0.0, 65.0)
        self.assertEqual([q["intro_until_seconds"] for q in queue], [12.4, None, 0.0, 65.0])
        # 0 is a configured value, never collapsed into "unset".
        self.assertIsNotNone(queue[2]["intro_until_seconds"])

    def test_existing_queue_fields_are_unchanged(self):
        queue = self._queue_for(5.0)
        self.assertEqual(
            set(queue[0]),
            {"item_id", "track_id", "title", "artist", "duration", "airtime_seconds", "category",
             "format", "fill_color", "eta_seconds", "intro_until_seconds"},
        )


@override_settings(SECURE_SSL_REDIRECT=False)
class ActiveLogEndpointTests(QueueEtaFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.kind.fill_color = "rgba(10, 120, 200, 0.4)"
        self.kind.save()
        self.state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.state_dir.cleanup)
        self.state_path = Path(self.state_dir.name) / "engine_state.json"
        patch = mock.patch("library.views.ENGINE_STATE_PATH", self.state_path)
        patch.start()
        self.addCleanup(patch.stop)
        self.superuser = User.objects.create_superuser("log_display_su", "su@example.invalid", "pw")
        self.client.force_login(self.superuser)

    def write_state(self, log_id, age=0.0):
        self.state_path.write_text(json.dumps({"log_id": log_id, "timestamp": time.time() - age}))

    def url(self, pk=None):
        return f"/api/engine/status/log/{pk if pk is not None else self.log.id}/"

    def test_serves_the_active_log_in_position_order_with_played_evidence(self):
        aired = timezone.now()
        first = self.make_queued_item(next_start_seconds=180.0, position=1)
        second = self.make_queued_item(next_start_seconds=180.0, position=2)
        LogItem.objects.filter(pk=first.pk).update(played_at=aired)
        set_markers(first.track_id, intro_until_seconds=0.0)
        self.write_state(self.log.id)

        body = self.client.get(self.url()).json()

        self.assertEqual(body["log_id"], self.log.id)
        self.assertEqual([row["item_id"] for row in body["items"]], [first.id, second.id])
        played, unplayed = body["items"]
        self.assertAlmostEqual(played["played_at"], aired.timestamp(), places=3)
        self.assertIsNone(unplayed["played_at"])
        self.assertEqual(played["intro_until_seconds"], 0.0)
        self.assertIsNone(unplayed["intro_until_seconds"])
        self.assertEqual(played["fill_color"], "rgba(10, 120, 200, 0.4)")
        self.assertEqual(played["category"], "QUEUEETA")
        self.assertEqual(played["artist"], "Queue ETA Artist")

    def test_a_deleted_track_still_reads_from_the_logitem_snapshot(self):
        item = self.make_queued_item(next_start_seconds=100.0, position=1)
        LogItem.objects.filter(pk=item.pk).update(
            track=None, track_title="Snapshot Title", track_artist="Snapshot Artist",
            played_at=timezone.now())
        self.write_state(self.log.id)
        row = self.client.get(self.url()).json()["items"][0]
        self.assertEqual((row["title"], row["artist"], row["track_id"]), ("Snapshot Title", "Snapshot Artist", None))
        self.assertIsNone(row["intro_until_seconds"])
        self.assertEqual(row["category"], "QUEUEETA")

    def test_only_the_engines_active_log_is_served(self):
        other = PlaylistLog.objects.create(date=date(2027, 6, 1), hour=11, status="approved")
        self.write_state(self.log.id)
        self.assertEqual(self.client.get(self.url(other.id)).status_code, 404)
        self.assertEqual(self.client.get(self.url()).status_code, 200)

    def test_absent_stale_or_malformed_engine_state_is_a_404_not_an_error(self):
        self.assertEqual(self.client.get(self.url()).status_code, 404)   # no state file
        self.write_state(self.log.id, age=60)
        self.assertEqual(self.client.get(self.url()).status_code, 404)   # stale
        self.state_path.write_text("{not json")
        self.assertEqual(self.client.get(self.url()).status_code, 404)   # unreadable

    def test_it_is_read_only(self):
        self.write_state(self.log.id)
        for method in ("post", "put", "patch", "delete"):
            self.assertEqual(getattr(self.client, method)(self.url()).status_code, 405, method)

    def test_signed_out_requests_are_turned_away(self):
        self.client.logout()
        self.write_state(self.log.id)
        self.assertNotEqual(self.client.get(self.url()).status_code, 200)

    def test_remote_dj_group_reaches_it_through_the_existing_status_prefix(self):
        dj = User.objects.create_user("log_display_dj", password="pw")
        dj.groups.add(Group.objects.get(name="remote_dj"))
        contributor = User.objects.create_user("log_display_contrib", password="pw")
        contributor.groups.add(Group.objects.get(name="Contributor"))
        self.write_state(self.log.id)
        self.client.force_login(dj)
        self.assertEqual(self.client.get(self.url()).status_code, 200)
        self.client.force_login(contributor)
        self.assertEqual(self.client.get(self.url()).status_code, 403)

    def test_does_not_change_the_engine_status_feed(self):
        """/api/engine/status/ itself is not touched: same file, same keys."""
        self.state_path.write_text(json.dumps(
            {"transport": "PLAYING", "decks": {"A": None, "B": None}, "queue": [], "log_id": 3,
             "timestamp": time.time()}))
        body = self.client.get("/api/engine/status/").json()
        self.assertEqual(body["queue"], [])
        self.assertNotIn("items", body)


@override_settings(SECURE_SSL_REDIRECT=False)
class ShowPlayedSettingTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser("log_display_admin", "a@example.invalid", "pw")
        self.client.force_login(self.superuser)

    def set_flag(self, value):
        theme = UITheme.load()
        theme.show_played_items = value
        theme.save()

    def test_default_is_off(self):
        self.assertIs(UITheme.load().show_played_items, False)
        self.assertIs(UITheme._meta.get_field("show_played_items").default, False)

    def test_both_console_pages_render_the_flag(self):
        for path in ("/", "/remote-dj/"):
            with self.subTest(path=path, flag="off"):
                self.set_flag(False)
                self.assertIn("const SHOW_PLAYED_ITEMS = false;", self.client.get(path).content.decode())
            with self.subTest(path=path, flag="on"):
                self.set_flag(True)
                self.assertIn("const SHOW_PLAYED_ITEMS = true;", self.client.get(path).content.decode())

    def test_both_pages_have_the_intro_column(self):
        for path in ("/", "/remote-dj/"):
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                self.assertIn('<th class="qt-th-intro">Intro</th>', html)
                self.assertNotIn('colspan="8"', html)

    def test_admin_offers_it_in_its_own_fieldset(self):
        response = self.client.get("/admin/library/uitheme/%d/change/" % UITheme.load().pk)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Console / Remote DJ", html)
        self.assertIn('name="show_played_items"', html)
        self.assertIn("Show played items", html)
