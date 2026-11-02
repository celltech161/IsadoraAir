"""r0125 (roadmap 3.11 Phase A): /library/ bulk editing -- selection, preview, leave/set/clear, categories.

Everything runs against the disposable test database; no audio, waveform or station file exists or is
touched (Track file paths are only strings here)."""
import hashlib
import json
import tempfile
import threading
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import close_old_connections, connection, connections
from django.db.models import F
from django.db.models.manager import Manager
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from library import models as library_models
from library.models import (
    Album, Artist, Category, CategoryKind, Genre, Holiday, LogItem, PlayEvent, PlaylistLog, Track,
    TrackWriteRefused,
)
from library.services import cue_markers, track_bulk_edit

User = get_user_model()
PREVIEW = "library:api-track-bulk-edit-preview"
APPLY = "library:api-track-bulk-edit-apply"


class _FixtureMixin:
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        settings = override_settings(LIBRARY_ROOT=str(self.root), SECURE_SSL_REDIRECT=False)
        settings.enable()
        self.addCleanup(settings.disable)
        kind, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        self.rock = Category.objects.create(code="ROCK", name="Rock", kind=kind)
        self.pop = Category.objects.create(code="POP", name="Pop", kind=kind)
        self.blues = Category.objects.create(code="BLUES", name="Blues", kind=kind)
        self.xmas = Holiday.objects.create(code="xmas", name="Christmas", month=12, day=25)
        self.halloween = Holiday.objects.create(code="halloween", name="Halloween", month=10, day=31)
        self.artist = Artist.objects.create(name="Bulk Artist")
        self.admin = User.objects.create_superuser("bulk-admin", "bulk@example.invalid", "pw")
        self.client.force_login(self.admin)

    def track(self, title, *, category=None, **fields):
        category = category or self.rock
        return Track.objects.create(
            filepath=str(self.root / category.code / f"{title}.flac"), filename=f"{title}.flac",
            title=title, artist=fields.pop("artist", self.artist), category=category, format="flac",
            duration_seconds=180.0, **fields)

    def post(self, name, payload):
        response = self.client.post(reverse(name), payload, content_type="application/json")
        return response.status_code, response.json()

    def preview(self, selection, changes):
        return self.post(PREVIEW, {"selection": selection, "changes": changes})

    def apply(self, selection, changes, token=None):
        if token is None:
            status, body = self.preview(selection, changes)
            self.assertEqual(status, 200, body)
            token = body["token"]
        return self.post(APPLY, {"selection": selection, "changes": changes, "token": token})

    @staticmethod
    def ids(*tracks):
        return {"mode": "ids", "ids": [t.pk for t in tracks]}


class _Fixture(_FixtureMixin, TestCase):
    pass


class SelectionTests(_Fixture):
    def test_ticked_rows_from_different_pages_are_one_explicit_selection(self):
        tracks = [self.track(f"t{i:02d}") for i in range(60)]          # the list shows 50 per page
        chosen = [tracks[3], tracks[55]]
        status, body = self.preview(self.ids(*chosen), {"energy": {"op": "set", "value": "upbeat"}})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["selection"]["mode"], body["selection"]["count"]), ("ids", 2))
        status, body = self.apply(self.ids(*chosen), {"energy": {"op": "set", "value": "upbeat"}})
        self.assertEqual((status, body["updated"]), (200, 2))
        self.assertEqual(Track.objects.filter(energy="upbeat").count(), 2)

    def test_filter_selection_is_resolved_on_the_server_with_exclusions(self):
        keep = [self.track("Moon One"), self.track("Moon Two"), self.track("Moon Three")]
        self.track("Sun", category=self.pop)
        selection = {"mode": "filter", "filter": {"q": "moon", "category": str(self.rock.pk), "ready2air": ""},
                     "exclude_ids": [keep[1].pk]}
        status, body = self.preview(selection, {"vocal_type": {"op": "set", "value": "group"}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["selection"]["mode"], "filter")
        self.assertEqual(body["selection"]["count"], 2)
        self.assertIn("unticked", body["selection"]["description"])
        status, body = self.apply(selection, {"vocal_type": {"op": "set", "value": "group"}})
        self.assertEqual(body["updated"], 2)
        self.assertEqual(set(Track.objects.filter(vocal_type="group").values_list("pk", flat=True)),
                         {keep[0].pk, keep[2].pk})

    def test_empty_and_unmatched_selections_are_refused(self):
        status, body = self.preview({"mode": "ids", "ids": []}, {"energy": {"op": "set", "value": "slow"}})
        self.assertEqual((status, body["error"]), (400, "No Tracks are selected"))
        status, body = self.preview({"mode": "filter", "filter": {"q": "nothing like this"}},
                                    {"energy": {"op": "set", "value": "slow"}})
        self.assertEqual((status, body["error"]), (400, "No Tracks match this filter"))
        status, _ = self.preview({"mode": "everything"}, {"energy": {"op": "set", "value": "slow"}})
        self.assertEqual(status, 400)

    def test_selections_over_the_limit_are_refused_with_the_count(self):
        tracks = [self.track(f"cap{i}") for i in range(4)]
        with mock.patch.object(track_bulk_edit, "MAX_TRACKS", 3):
            status, body = self.preview(self.ids(*tracks), {"energy": {"op": "set", "value": "slow"}})
            self.assertEqual(status, 400)
            self.assertIn("limited to 3", body["error"])
            status, body = self.preview({"mode": "filter", "filter": {"q": "cap"}},
                                        {"energy": {"op": "set", "value": "slow"}})
            self.assertEqual((status, body["count"]), (400, 4))
        self.assertFalse(Track.objects.filter(energy="slow").exists())

    def test_tracks_deleted_since_the_list_loaded_are_reported_missing(self):
        one, two = self.track("present"), self.track("gone")
        gone_id = two.pk
        two.delete()
        status, body = self.preview({"mode": "ids", "ids": [one.pk, gone_id]},
                                    {"energy": {"op": "set", "value": "slow"}})
        self.assertEqual((status, body["selection"]["count"], body["selection"]["missing"]), (200, 1, [gone_id]))
        status, body = self.apply({"mode": "ids", "ids": [one.pk, gone_id]}, {"energy": {"op": "set", "value": "slow"}})
        self.assertEqual((body["updated"], body["missing"], body["requested"]), (1, [gone_id], 2))

    def test_apply_refuses_a_selection_that_changed_after_the_preview(self):
        one, two = self.track("stale-a"), self.track("stale-b")
        changes = {"energy": {"op": "set", "value": "slow"}}
        selection = self.ids(one, two)
        _status, preview = self.preview(selection, changes)
        two.delete()
        status, body = self.post(APPLY, {"selection": selection, "changes": changes, "token": preview["token"]})
        self.assertEqual(status, 409, body)
        self.assertIn("Preview again", body["error"])
        self.assertEqual(Track.objects.get(pk=one.pk).energy, "")
        # A filter selection that gained a match is just as stale.
        selection = {"mode": "filter", "filter": {"q": "stale"}}
        _status, preview = self.preview(selection, changes)
        self.track("stale-c")
        status, _ = self.post(APPLY, {"selection": selection, "changes": changes, "token": preview["token"]})
        self.assertEqual(status, 409)
        self.assertFalse(Track.objects.filter(energy="slow").exists())

    def test_apply_refuses_changes_that_differ_from_the_preview_and_needs_a_token(self):
        one = self.track("token")
        _status, preview = self.preview(self.ids(one), {"energy": {"op": "set", "value": "slow"}})
        status, _ = self.post(APPLY, {"selection": self.ids(one), "changes": {"energy": {"op": "set", "value": "upbeat"}},
                                      "token": preview["token"]})
        self.assertEqual(status, 409)
        status, body = self.post(APPLY, {"selection": self.ids(one), "changes": {"energy": {"op": "set", "value": "slow"}}})
        self.assertEqual(status, 400)
        self.assertIn("Preview the edit first", body["error"])
        self.assertEqual(Track.objects.get(pk=one.pk).energy, "")


class LeaveSetClearTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.album = Album.objects.create(title="Old Album", album_artist="Various")
        self.genre = Genre.objects.create(name="Rock")
        self.full = self.track("full", album=self.album, genre=self.genre, year=1999, composer="C. Writer",
                               publisher="Pub", record_label="Label", energy="slow", vocal_type="male",
                               end_type="fade", rotation_weight=4, ready2air=True, comments="keep me",
                               isrc="USRC17607839")

    def snapshot(self, track):
        return Track.objects.filter(pk=track.pk).values().get()

    def test_omitted_and_leave_fields_never_change(self):
        before = self.snapshot(self.full)
        status, body = self.apply(self.ids(self.full), {"composer": {"op": "set", "value": "New Writer"},
                                                        "album": {"op": "leave"}, "year": {"op": "leave"}})
        self.assertEqual((status, body["updated"]), (200, 1))
        after = self.snapshot(self.full)
        changed = {k for k in before if before[k] != after[k]}
        self.assertEqual(changed, {"composer", "updated_at"})

    def test_set_resolves_lookups_like_single_track_editing(self):
        Artist.objects.create(name="Existing Case")
        changes = {
            "artist": {"op": "set", "value": "existing CASE"},          # case-insensitive reuse
            "album": {"op": "set", "value": "New Album"},               # within the Track's album artist
            "genre": {"op": "set", "value": "Brand New Genre"},
            "year": {"op": "set", "value": "2001"},
            "energy": {"op": "set", "value": "upbeat"},
            "vocal_type": {"op": "set", "value": "female"},
            "end_type": {"op": "set", "value": "cold"},
            "rotation_weight": {"op": "set", "value": 1},
            "ready2air": {"op": "set", "value": False},
            "publisher": {"op": "set", "value": "  Trimmed Pub  "},
        }
        status, body = self.apply(self.ids(self.full), changes)
        self.assertEqual((status, body["updated"], body["failed"]), (200, 1, []))
        t = Track.objects.select_related("artist", "album", "genre").get(pk=self.full.pk)
        self.assertEqual(t.artist.name, "Existing Case")
        self.assertEqual(Artist.objects.filter(name__iexact="existing case").count(), 1)
        self.assertEqual((t.album.title, t.album.album_artist), ("New Album", "Various"))
        self.assertEqual(t.genre.name, "Brand New Genre")
        self.assertEqual((t.year, t.energy, t.vocal_type, t.end_type, t.rotation_weight, t.ready2air, t.publisher),
                         (2001, "upbeat", "female", "cold", 1, False, "Trimmed Pub"))

    def test_clear_empties_only_clearable_fields(self):
        changes = {name: {"op": "clear"} for name in
                   ("album", "genre", "year", "composer", "publisher", "record_label", "energy", "vocal_type")}
        status, body = self.apply(self.ids(self.full), changes)
        self.assertEqual((status, body["updated"]), (200, 1))
        t = Track.objects.get(pk=self.full.pk)
        self.assertEqual((t.album_id, t.genre_id, t.year, t.composer, t.publisher, t.record_label, t.energy,
                          t.vocal_type), (None, None, None, "", "", "", "", ""))
        self.assertEqual((t.end_type, t.rotation_weight, t.ready2air, t.comments, t.isrc),
                         ("fade", 4, True, "keep me", "USRC17607839"))
        for name in ("artist", "end_type", "rotation_weight", "ready2air"):
            status, body = self.preview(self.ids(self.full), {name: {"op": "clear"}})
            self.assertEqual(status, 400, name)
            self.assertIn("cannot be cleared", body["error"])

    def test_invalid_values_are_refused_before_anything_is_written(self):
        before = self.snapshot(self.full)
        cases = [
            ({"composer": {"op": "set", "value": "   "}}, "choose Clear"),
            ({"energy": {"op": "set", "value": "frantic"}}, "must be one of"),
            ({"year": {"op": "set", "value": 99}}, "between 1000 and 9999"),
            ({"year": {"op": "set", "value": "nineteen"}}, "whole number"),
            ({"rotation_weight": {"op": "set", "value": 9}}, "between 0 and 5"),
            ({"ready2air": {"op": "set", "value": "yes"}}, "true or false"),
            ({"composer": {"op": "set", "value": "x" * 256}}, "longer than 255"),
            ({"title": {"op": "set", "value": "Bulk Title"}}, "Not editable in bulk"),
            ({"cue_in_seconds": {"op": "set", "value": 1.0}}, "Not editable in bulk"),
            ({"composer": {"op": "set"}}, "needs a value"),
            ({"composer": "New"}, "expected"),
            ({}, "Nothing to change"),
            ({"energy": {"op": "leave"}}, "Nothing to change"),
        ]
        for changes, message in cases:
            with self.subTest(changes=changes):
                status, body = self.preview(self.ids(self.full), changes)
                self.assertEqual(status, 400, body)
                self.assertIn(message, body["error"])
                status, _ = self.post(APPLY, {"selection": self.ids(self.full), "changes": changes, "token": "x"})
                self.assertEqual(status, 400)
        status, body = self.preview(self.ids(self.full), {"category": {"op": "set", "value": 999999}})
        self.assertEqual(status, 404)
        self.assertEqual(self.snapshot(self.full), before)

    def test_preview_writes_nothing_and_reports_what_would_change(self):
        other = self.track("other", energy="upbeat")
        before = [self.snapshot(self.full), self.snapshot(other)]
        artists = Artist.objects.count()
        status, body = self.preview(self.ids(self.full, other), {
            "energy": {"op": "set", "value": "upbeat"}, "artist": {"op": "set", "value": "Not Yet Created"}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["summary"]["energy"], {"label": "Energy", "change": 1, "unchanged": 1})
        self.assertEqual(body["tracks_changing"], 2)
        self.assertIn('artist "Not Yet Created"', " ".join(body["warnings"]))
        sample = {c["field"]: c for s in body["samples"] if s["id"] == self.full.pk for c in s["changes"]}
        self.assertEqual((sample["energy"]["before"], sample["energy"]["after"]), ("slow", "upbeat"))
        self.assertEqual(sample["artist"]["after"], "Not Yet Created (new)")
        self.assertEqual(Artist.objects.count(), artists)
        self.assertEqual([self.snapshot(self.full), self.snapshot(other)], before)

    def test_tracks_already_matching_are_counted_unchanged_not_rewritten(self):
        same = self.track("same", energy="slow")
        stamp = Track.objects.get(pk=same.pk).updated_at
        status, body = self.apply(self.ids(same, self.full), {"energy": {"op": "set", "value": "slow"}})
        self.assertEqual((status, body["updated"], body["unchanged"]), (200, 0, 2))
        self.assertEqual(Track.objects.get(pk=same.pk).updated_at, stamp)


class CategoryTests(_Fixture):
    def test_additional_categories_are_added_and_removed_without_replacing_others(self):
        a = self.track("a")
        b = self.track("b", category=self.pop)
        a.additional_categories.add(self.blues)
        b.additional_categories.add(self.blues)
        changes = {"additional_categories": {"add": [self.pop.pk], "remove": []}}
        status, body = self.apply(self.ids(a, b), changes)
        self.assertEqual((status, body["updated"], body["unchanged"]), (200, 1, 1))
        self.assertEqual(set(a.additional_categories.values_list("code", flat=True)), {"BLUES", "POP"})
        # POP is b's primary category, so it is never also an additional one.
        self.assertEqual(set(b.additional_categories.values_list("code", flat=True)), {"BLUES"})
        status, body = self.apply(self.ids(a, b), {"additional_categories": {"remove": [self.pop.pk]}})
        self.assertEqual(body["updated"], 1)
        self.assertEqual(set(a.additional_categories.values_list("code", flat=True)), {"BLUES"})
        self.assertEqual(set(b.additional_categories.values_list("code", flat=True)), {"BLUES"})

    def test_primary_category_change_is_database_only_and_reports_the_folder(self):
        a = self.track("moved")
        a.additional_categories.add(self.pop, self.blues)
        path = a.filepath
        status, body = self.preview(self.ids(a), {"category": {"op": "set", "value": self.pop.pk}})
        self.assertIn("stay in their current category folder", " ".join(body["warnings"]))
        status, body = self.apply(self.ids(a), {"category": {"op": "set", "value": self.pop.pk}})
        self.assertEqual((status, body["updated"], body["files_left_in_previous_category_folder"]), (200, 1, 1))
        t = Track.objects.get(pk=a.pk)
        self.assertEqual((t.category_id, t.filepath), (self.pop.pk, path))
        # The new primary category left the additional list; BLUES was not touched.
        self.assertEqual(set(t.additional_categories.values_list("code", flat=True)), {"BLUES"})
        status, body = self.apply(self.ids(a), {"category": {"op": "clear"}})
        self.assertIsNone(Track.objects.get(pk=a.pk).category_id)

    def test_holidays_are_added_and_removed_by_name(self):
        a, b = self.track("h1"), self.track("h2")
        a.holidays.add(self.halloween)
        status, body = self.apply(self.ids(a, b), {"holidays": {"add": ["xmas"], "remove": ["halloween"]}})
        self.assertEqual((status, body["updated"]), (200, 2))
        self.assertEqual(set(a.holidays.values_list("code", flat=True)), {"xmas"})
        self.assertEqual(set(b.holidays.values_list("code", flat=True)), {"xmas"})
        status, body = self.preview(self.ids(a), {"holidays": {"add": ["xmas"], "remove": ["xmas"]}})
        self.assertEqual(status, 400)
        status, body = self.preview(self.ids(a), {"holidays": {"add": ["nope"]}})
        self.assertEqual(status, 404)


class MixedOutcomeTests(_Fixture):
    def test_a_refused_track_is_predicted_reported_and_does_not_block_the_others(self):
        ok, refused = self.track("allowed"), self.track("projected")

        def guard(track, update_fields):
            if track.pk == refused.pk and (update_fields is None or "artist" in update_fields):
                raise TrackWriteRefused("Track ['artist'] of a projected spoken Track change only through the projection")

        guards = library_models.TRACK_WRITE_GUARDS + [(guard, lambda qs, cols: None, lambda objs, f: None)]
        changes = {"artist": {"op": "set", "value": "Renamed"}, "energy": {"op": "set", "value": "upbeat"}}
        with mock.patch.object(library_models, "TRACK_WRITE_GUARDS", guards):
            status, preview = self.preview(self.ids(ok, refused), changes)
            self.assertEqual([r["id"] for r in preview["refusals"]], [refused.pk])
            self.assertEqual(preview["tracks_changing"], 1)
            status, body = self.apply(self.ids(ok, refused), changes, token=preview["token"])
        self.assertEqual((status, body["updated"]), (200, 1))
        self.assertEqual([(f["id"], "projected spoken" in f["reason"]) for f in body["refused"]], [(refused.pk, True)])
        self.assertEqual((body["failed"], body["stale"]), ([], []))
        refused.refresh_from_db()
        self.assertEqual((refused.artist_id, refused.energy), (self.artist.pk, ""))     # the whole Track rolled back
        self.assertEqual(Track.objects.get(pk=ok.pk).artist.name, "Renamed")

    def test_an_unexpected_failure_rolls_back_only_that_track(self):
        a, b, c = self.track("one"), self.track("two"), self.track("three")
        real = Manager.bulk_create
        holiday_through = Track.holidays.through

        def failing(manager, objs, *args, **kwargs):
            objs = list(objs)
            if manager.model is holiday_through and any(o.track_id == b.pk for o in objs):
                raise RuntimeError("disk full")
            return real(manager, objs, *args, **kwargs)

        changes = {"composer": {"op": "set", "value": "Written"}, "holidays": {"add": ["xmas"]}}
        with mock.patch.object(Manager, "bulk_create", failing), \
             self.assertLogs("library.services.track_bulk_edit", level="ERROR"):
            status, body = self.apply(self.ids(a, b, c), changes)
        self.assertEqual((status, body["updated"]), (200, 2))
        self.assertEqual(body["failed"][0]["id"], b.pk)
        self.assertIn("this Track was not changed", body["failed"][0]["reason"])
        b.refresh_from_db()
        self.assertEqual((b.composer, list(b.holidays.all())), ("", []))           # column change rolled back too
        self.assertEqual(Track.objects.get(pk=c.pk).composer, "Written")


class ImmutabilityTests(_Fixture):
    def test_logs_play_events_cue_markers_and_media_fields_are_untouched(self):
        track = self.track("aired", ready2air=True, waveform_path="/srv/waves/1.json")
        cue_markers.operator_edit(track.pk, expected_revision=0, user=self.admin,
                                  values={"cue_in_seconds": 1.5, "next_start_seconds": 170.0})
        log = PlaylistLog.objects.create(date=timezone.localdate(), hour=5, status="approved")
        item = LogItem.objects.create(playlist_log=log, position=0, scheduled_time=timezone.now(), track=track,
                                      track_title=track.title, category=self.rock)
        event = PlayEvent.objects.create(track=track, track_title=track.title, track_artist="Bulk Artist",
                                         started_at=timezone.now())
        frozen_track = ("cue_in_seconds", "next_start_seconds", "cue_marker_state", "cue_marker_revision",
                        "waveform_path", "filepath", "analysis_requested_at", "play_count", "last_played_at")
        before = (Track.objects.filter(pk=track.pk).values(*frozen_track).get(),
                  PlaylistLog.objects.filter(pk=log.pk).values().get(),
                  LogItem.objects.filter(pk=item.pk).values().get(),
                  PlayEvent.objects.filter(pk=event.pk).values().get())
        status, body = self.apply(self.ids(track), {
            "artist": {"op": "set", "value": "New Name"}, "category": {"op": "set", "value": self.pop.pk},
            "energy": {"op": "set", "value": "upbeat"}, "ready2air": {"op": "set", "value": False}})
        self.assertEqual((status, body["updated"]), (200, 1))
        after = (Track.objects.filter(pk=track.pk).values(*frozen_track).get(),
                 PlaylistLog.objects.filter(pk=log.pk).values().get(),
                 LogItem.objects.filter(pk=item.pk).values().get(),
                 PlayEvent.objects.filter(pk=event.pk).values().get())
        self.assertEqual(after, before)
        self.assertEqual(Track.objects.get(pk=track.pk).cue_marker_state["cue_in_seconds"]["protection"],
                         cue_markers.LOCKED)

    def test_no_audio_tags_reanalysis_or_files_are_touched(self):
        track = self.track("files")
        with mock.patch("library.views._write_file_tags") as tags, \
             mock.patch.object(cue_markers, "request_analysis") as analysis, \
             mock.patch("pathlib.Path.unlink") as unlink, \
             mock.patch("shutil.move") as move:
            status, body = self.apply(self.ids(track), {"genre": {"op": "set", "value": "Jazz"},
                                                        "category": {"op": "set", "value": self.blues.pk}})
        self.assertEqual((status, body["updated"]), (200, 1))
        for spy in (tags, analysis, unlink, move):
            spy.assert_not_called()


@override_settings(SECURE_SSL_REDIRECT=False)
class PermissionTests(_Fixture):
    def user_in(self, group_name):
        user = User.objects.create_user(f"u-{group_name}", f"{group_name}@example.invalid", "pw")
        group, _ = Group.objects.get_or_create(name=group_name)
        user.groups.add(group)
        return user

    def test_only_track_managers_may_preview_or_apply(self):
        track = self.track("guarded")
        changes = {"energy": {"op": "set", "value": "slow"}}
        _status, preview = self.preview(self.ids(track), changes)
        for group in ("Contributor", "remote_dj", "No Capability Group"):
            with self.subTest(group=group):
                self.client.force_login(self.user_in(group))
                for name, payload in ((PREVIEW, {"selection": self.ids(track), "changes": changes}),
                                      (APPLY, {"selection": self.ids(track), "changes": changes,
                                               "token": preview["token"]})):
                    response = self.client.post(reverse(name), payload, content_type="application/json")
                    self.assertEqual(response.status_code, 403, (group, name))
        self.assertEqual(Track.objects.get(pk=track.pk).energy, "")

    def test_the_library_page_offers_the_editor_only_to_track_managers(self):
        response = self.client.get(reverse("library:library"))
        self.assertContains(response, 'id="bulkEditor"')
        self.assertContains(response, "const BULK_UI = true;")
        self.client.force_login(self.user_in("Contributor"))
        response = self.client.get(reverse("library:library"))
        self.assertNotContains(response, 'id="bulkEditor"')
        self.assertContains(response, "const BULK_UI = false;")


class PageContractTests(_Fixture):
    def page(self):
        return self.client.get(reverse("library:library")).content.decode()

    def test_page_checkbox_selects_only_the_page_and_matching_results_need_an_explicit_choice(self):
        html = self.page()
        handler = html[html.index("// Page checkbox"):html.index("// Sort on header click")]
        self.assertIn("pageTrackIds.forEach(id => selectedIds.add(id))", handler)
        self.assertNotIn("filterSelection = {", handler)
        self.assertIn("function selectAllMatching()", html)
        self.assertIn("Select all ${lastTotal.toLocaleString()} Tracks matching this filter", html)
        self.assertIn("lastTotal > BULK_MAX", html)

    def test_every_field_starts_as_leave_unchanged_and_edits_go_through_preview(self):
        html = self.page()
        self.assertEqual(html.count('class="be-op"'), len(track_bulk_edit.FIELDS))
        for name, spec in track_bulk_edit.FIELDS.items():
            row = html[html.index(f'id="be-{name}-op"'):]
            row = row[:row.index("</select>")]
            self.assertTrue(row.split(">", 1)[1].startswith('<option value="leave">Leave unchanged'), name)
            self.assertEqual('value="clear"' in row, spec.clearable, name)
        # Every edit path previews first; the old immediate per-action requests are gone.
        self.assertNotIn("bulkAction(", html)
        self.assertNotIn("action: 'set_category'", html)
        self.assertIn("/api/tracks/bulk/preview/", html)
        self.assertIn("token", html[html.index("async function applyBulkEdit"):])


class TokenIntegrityTests(_Fixture):
    """The preview token is server-issued, operator- and session-bound, single-use and expiring, and apply
    leaves every Track that changed since the preview alone."""

    changes = {"energy": {"op": "set", "value": "upbeat"}}

    def test_a_forged_or_tampered_token_is_refused_and_nothing_is_written(self):
        one = self.track("forged")
        # Phase A's token was this deterministic hash, computable without any preview.
        payload = json.dumps({"v": 1, "ids": [one.pk], "ops": [["energy", "set", "upbeat"]]},
                             sort_keys=True, separators=(",", ":"))
        for token in (hashlib.sha256(payload.encode()).hexdigest(), "", None):
            status, body = self.post(APPLY, {"selection": self.ids(one), "changes": self.changes, "token": token})
            self.assertEqual(status, 400, body)
        _status, preview = self.preview(self.ids(one), self.changes)
        tampered = preview["token"][:-2] + ("AA" if not preview["token"].endswith("AA") else "BB")
        status, body = self.post(APPLY, {"selection": self.ids(one), "changes": self.changes, "token": tampered})
        self.assertEqual(status, 400)
        self.assertIn("not a valid preview token", body["error"])
        self.assertEqual(Track.objects.get(pk=one.pk).energy, "")

    def test_a_token_is_good_for_one_apply_only(self):
        one = self.track("once")
        _status, preview = self.preview(self.ids(one), self.changes)
        status, body = self.apply(self.ids(one), self.changes, token=preview["token"])
        self.assertEqual((status, body["updated"]), (200, 1))
        Track.objects.filter(pk=one.pk).update(energy="")
        status, body = self.apply(self.ids(one), self.changes, token=preview["token"])
        self.assertEqual(status, 409)
        self.assertIn("already applied", body["error"])
        self.assertEqual(Track.objects.get(pk=one.pk).energy, "")

    def test_an_expired_token_is_refused(self):
        one = self.track("expired")
        _status, preview = self.preview(self.ids(one), self.changes)
        with mock.patch.object(track_bulk_edit, "TOKEN_MAX_AGE_SECONDS", -1):
            status, body = self.apply(self.ids(one), self.changes, token=preview["token"])
        self.assertEqual(status, 409)
        self.assertIn("expired", body["error"])
        self.assertEqual(Track.objects.get(pk=one.pk).energy, "")

    def test_a_token_is_bound_to_its_operator_and_session(self):
        one = self.track("cross")
        _status, preview = self.preview(self.ids(one), self.changes)
        other = User.objects.create_superuser("other-admin", "other@example.invalid", "pw")
        second = Client()
        second.force_login(other)
        response = second.post(reverse(APPLY), {"selection": self.ids(one), "changes": self.changes,
                                                "token": preview["token"]}, content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertIn("another operator", response.json()["error"])
        # The same operator in a different session (another browser) does not hold the preview either.
        elsewhere = Client()
        elsewhere.force_login(self.admin)
        response = elsewhere.post(reverse(APPLY), {"selection": self.ids(one), "changes": self.changes,
                                                   "token": preview["token"]}, content_type="application/json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Track.objects.get(pk=one.pk).energy, "")
        # The issuing session can still use it.
        status, body = self.apply(self.ids(one), self.changes, token=preview["token"])
        self.assertEqual((status, body["updated"]), (200, 1))

    def test_metadata_changed_since_the_preview_makes_that_track_stale_not_overwritten(self):
        changed, steady = self.track("changed"), self.track("steady")
        _status, preview = self.preview(self.ids(changed, steady), self.changes)
        Track.objects.filter(pk=changed.pk).update(composer="Someone else's edit")
        status, body = self.apply(self.ids(changed, steady), self.changes, token=preview["token"])
        self.assertEqual((status, body["updated"], body["unchanged"]), (200, 1, 0))
        self.assertEqual([t["id"] for t in body["stale"]], [changed.pk])
        self.assertIn("changed since the preview", body["stale"][0]["reason"])
        changed.refresh_from_db()
        self.assertEqual((changed.energy, changed.composer), ("", "Someone else's edit"))
        self.assertEqual(Track.objects.get(pk=steady.pk).energy, "upbeat")

    def test_a_track_previewed_as_already_matching_that_changed_is_stale_too(self):
        one = self.track("matched", energy="upbeat")
        _status, preview = self.preview(self.ids(one), self.changes)
        self.assertEqual(preview["tracks_unchanged"], 1)
        Track.objects.filter(pk=one.pk).update(energy="slow")
        status, body = self.apply(self.ids(one), self.changes, token=preview["token"])
        self.assertEqual(([t["id"] for t in body["stale"]], body["updated"]), ([one.pk], 0))
        self.assertEqual(Track.objects.get(pk=one.pk).energy, "slow")

    def test_membership_changed_since_the_preview_makes_that_track_stale(self):
        one, two = self.track("member-a"), self.track("member-b")
        _status, preview = self.preview(self.ids(one, two), {"holidays": {"add": ["xmas"]}})
        one.additional_categories.add(self.blues)
        status, body = self.apply(self.ids(one, two), {"holidays": {"add": ["xmas"]}}, token=preview["token"])
        self.assertEqual(([t["id"] for t in body["stale"]], body["updated"]), ([one.pk], 1))
        self.assertEqual(list(one.holidays.values_list("code", flat=True)), [])
        self.assertEqual(list(two.holidays.values_list("code", flat=True)), ["xmas"])

    def test_unrelated_changes_do_not_make_a_track_stale(self):
        one = self.track("aired")
        _status, preview = self.preview(self.ids(one), self.changes)
        Track.objects.filter(pk=one.pk).update(play_count=F("play_count") + 1, last_played_at=timezone.now(),
                                               updated_at=timezone.now())
        cue_markers.operator_edit(one.pk, expected_revision=0, user=self.admin, values={"cue_in_seconds": 2.0})
        PlayEvent.objects.create(track=one, track_title=one.title, started_at=timezone.now())
        log = PlaylistLog.objects.create(date=timezone.localdate(), hour=7, status="approved")
        LogItem.objects.create(playlist_log=log, position=0, scheduled_time=timezone.now(), track=one,
                               track_title=one.title, category=self.rock)
        status, body = self.apply(self.ids(one), self.changes, token=preview["token"])
        self.assertEqual((status, body["updated"], body["stale"]), (200, 1, []))

    def test_a_track_refused_at_preview_is_not_written_even_if_the_guard_is_gone(self):
        one, other = self.track("was-refused"), self.track("fine")

        def guard(track, update_fields):
            if track.pk == one.pk:
                raise TrackWriteRefused("projected spoken Track")

        guards = library_models.TRACK_WRITE_GUARDS + [(guard, lambda qs, cols: None, lambda objs, f: None)]
        with mock.patch.object(library_models, "TRACK_WRITE_GUARDS", guards):
            _status, preview = self.preview(self.ids(one, other), self.changes)
        status, body = self.apply(self.ids(one, other), self.changes, token=preview["token"])
        self.assertEqual(([t["id"] for t in body["refused"]], body["updated"]), ([one.pk], 1))
        self.assertEqual(Track.objects.get(pk=one.pk).energy, "")


class LegacyBulkEndpointTests(_Fixture):
    def test_direct_edit_actions_are_retired_and_write_nothing(self):
        one = self.track("legacy")
        cases = [
            {"action": "ready2air_on"}, {"action": "ready2air_off"},
            {"action": "set_category", "category_id": self.pop.pk},
            {"action": "add_additional_category", "category_id": self.pop.pk},
            {"action": "add_holiday", "holiday_code": "xmas"},
        ]
        before = Track.objects.filter(pk=one.pk).values().get()
        for payload in cases:
            with self.subTest(action=payload["action"]):
                status, body = self.post("library:api-track-bulk", {**payload, "ids": [one.pk]})
                self.assertEqual(status, 410)
                self.assertTrue(body["retired"])
                self.assertIn("/api/tracks/bulk/preview/", body["error"])
        self.assertEqual(Track.objects.filter(pk=one.pk).values().get(), before)
        self.assertFalse(one.additional_categories.exists() or one.holidays.exists())

    def test_bulk_delete_keeps_its_path_and_authorization(self):
        one = self.track("delete-me")
        with self.captureOnCommitCallbacks(execute=True):
            status, body = self.post("library:api-track-bulk", {"action": "delete", "ids": [one.pk]})
        self.assertEqual((status, body["deleted"]), (200, 1))
        self.assertFalse(Track.objects.filter(pk=one.pk).exists())
        status, _ = self.post("library:api-track-bulk", {"action": "nonsense", "ids": [1]})
        self.assertEqual(status, 400)


def _row_waiters():
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'transactionid' AND NOT granted")
        return cursor.fetchone()[0]


class ConcurrentModificationTests(_FixtureMixin, TransactionTestCase):
    """A Track modified by another transaction while apply runs is checked under its row lock."""

    def test_a_track_changed_while_apply_waits_for_its_row_is_stale(self):
        first, contended, last = self.track("c1"), self.track("c2"), self.track("c3")
        user = self.admin
        store = {}
        changes = track_bulk_edit.parse_changes({"energy": {"op": "set", "value": "upbeat"}})
        selection = track_bulk_edit.resolve_selection(self.ids(first, contended, last))
        token = track_bulk_edit.preview(selection, changes, user=user, store=store)["token"]
        locked, release, results = threading.Event(), threading.Event(), {}

        def editor():                      # another operator's single-Track edit, holding the row
            close_old_connections()
            try:
                from django.db import transaction
                with transaction.atomic():
                    row = Track.objects.select_for_update().get(pk=contended.pk)
                    locked.set()
                    release.wait(20)
                    row.composer = "Edited during the bulk apply"
                    row.save(update_fields=["composer", "updated_at"])
            finally:
                connections.close_all()

        def applier():
            close_old_connections()
            try:
                results["value"] = track_bulk_edit.apply(selection, changes, token, user=user, store=store)
            except Exception as exc:
                results["error"] = exc
            finally:
                connections.close_all()

        edit = threading.Thread(target=editor)
        edit.start()
        self.assertTrue(locked.wait(10))
        run = threading.Thread(target=applier)
        run.start()
        deadline = timezone.now().timestamp() + 10
        while _row_waiters() < 1 and timezone.now().timestamp() < deadline:
            threading.Event().wait(0.02)
        self.assertGreaterEqual(_row_waiters(), 1, "apply never waited for the contended Track's row")
        release.set()
        edit.join(20)
        run.join(30)
        self.assertNotIn("error", results)
        result = results["value"]
        self.assertEqual(([t["id"] for t in result["stale"]], result["updated"]), ([contended.pk], 2))
        contended.refresh_from_db()
        self.assertEqual((contended.energy, contended.composer), ("", "Edited during the bulk apply"))
        self.assertEqual(set(Track.objects.filter(energy="upbeat").values_list("pk", flat=True)), {first.pk, last.pk})
