"""Library enhancement: Bulk Edit Related Artists (/library/ Bulk Edit dialog).

``Track.related_artists`` is editable in bulk with Leave unchanged / Set to / Append / Clear, through the shared
``library.services.related_artists`` canonicalization and merge.  Everything runs against the disposable test
database; Track file paths are only strings and no audio, tag, cue or file location is touched.
"""
from unittest import mock

from django.db.models import F
from django.test import SimpleTestCase
from django.urls import reverse

from library.models import Artist, Track
from library.services import related_artists, track_bulk_edit
from library.tests.test_library_audit_r0125 import audit_events
from library.tests.test_library_bulk_edit_r0125 import _Fixture

LIMIT = Track._meta.get_field("related_artists").max_length


def _names(count, width):
    """``count`` distinct artist names of exactly ``width`` characters each."""
    return [f"N{i:03d}".ljust(width, "x") for i in range(count)]


class _RelatedFixture(_Fixture):
    def setUp(self):
        super().setUp()
        self.gilmour = Artist.objects.create(name="David Gilmour")
        self.waters = Artist.objects.create(name="Roger Waters")

    def related(self, track):
        return Track.objects.get(pk=track.pk).related_artists

    def snapshot(self, track):
        return Track.objects.filter(pk=track.pk).values().get()


class OperationTests(_RelatedFixture):
    def test_leave_and_omitted_never_change_related_artists(self):
        one = self.track("leave", related_artists="Kept One, Kept Two")
        for changes in ({"related_artists": {"op": "leave"}, "composer": {"op": "set", "value": "C"}},
                        {"composer": {"op": "set", "value": "D"}}):
            with self.subTest(changes=changes):
                status, body = self.apply(self.ids(one), changes)
                self.assertEqual((status, body["updated"]), (200, 1))
                self.assertEqual(self.related(one), "Kept One, Kept Two")

    def test_set_replaces_the_list_in_canonical_form(self):
        one = self.track("set", related_artists="Old Name")
        status, body = self.apply(self.ids(one), {"related_artists": {"op": "set",
                                                                      "value": "  Richard   Wright ,Nick Mason,,  "}})
        self.assertEqual((status, body["updated"]), (200, 1))
        self.assertEqual(self.related(one), "Richard Wright, Nick Mason")

    def test_append_keeps_existing_entries_and_their_order(self):
        one = self.track("append", related_artists="Zed, Alpha")
        status, body = self.apply(self.ids(one), {"related_artists": {"op": "append", "value": "Mid, Beta"}})
        self.assertEqual((status, body["updated"]), (200, 1))
        self.assertEqual(self.related(one), "Zed, Alpha, Mid, Beta")

    def test_append_to_an_empty_field(self):
        one = self.track("append-empty")
        self.apply(self.ids(one), {"related_artists": {"op": "append", "value": "Solo Guest"}})
        self.assertEqual(self.related(one), "Solo Guest")

    def test_clear_empties_the_field_and_an_empty_field_is_already_matching(self):
        full, empty = self.track("clear-full", related_artists="A Person"), self.track("clear-empty")
        status, preview = self.preview(self.ids(full, empty), {"related_artists": {"op": "clear"}})
        self.assertEqual((status, preview["tracks_changing"], preview["tracks_unchanged"]), (200, 1, 1))
        status, body = self.apply(self.ids(full, empty), {"related_artists": {"op": "clear"}}, token=preview["token"])
        self.assertEqual((body["updated"], body["unchanged"]), (1, 1))
        self.assertEqual((self.related(full), self.related(empty)), ("", ""))

    def test_append_of_names_already_present_is_unchanged(self):
        one = self.track("present", related_artists="Guest One, Guest Two")
        status, preview = self.preview(self.ids(one), {"related_artists": {"op": "append", "value": "guest two"}})
        self.assertEqual((preview["tracks_changing"], preview["tracks_unchanged"]), (0, 1))

    def test_a_formatting_only_difference_is_not_a_change(self):
        one = self.track("format", related_artists="Guest One,Guest Two")
        for changes in ({"related_artists": {"op": "set", "value": "Guest One, Guest Two"}},
                        {"related_artists": {"op": "append", "value": "Guest One"}}):
            with self.subTest(changes=changes):
                _status, preview = self.preview(self.ids(one), changes)
                self.assertEqual((preview["tracks_changing"], preview["tracks_unchanged"]), (0, 1))
        self.assertEqual(self.related(one), "Guest One,Guest Two")

    def test_a_spelling_change_is_a_change(self):
        one = self.track("spelling", related_artists="guest one")
        self.apply(self.ids(one), {"related_artists": {"op": "set", "value": "Guest One"}})
        self.assertEqual(self.related(one), "Guest One")


class DuplicateAndPrimaryArtistTests(_RelatedFixture):
    def test_duplicates_are_removed_case_insensitively_first_spelling_wins(self):
        one = self.track("dupes")
        self.apply(self.ids(one), {"related_artists": {"op": "set", "value": "Ann Lee, ANN LEE, Bo, ann  lee, bo"}})
        self.assertEqual(self.related(one), "Ann Lee, Bo")

    def test_append_does_not_duplicate_an_existing_entry_in_another_case(self):
        one = self.track("append-dupe", related_artists="Ann Lee")
        self.apply(self.ids(one), {"related_artists": {"op": "append", "value": "ANN LEE, New Guest"}})
        self.assertEqual(self.related(one), "Ann Lee, New Guest")

    def test_each_tracks_own_primary_artist_is_excluded(self):
        by_gilmour = self.track("g", artist=self.gilmour)
        by_waters = self.track("w", artist=self.waters)
        changes = {"related_artists": {"op": "set", "value": "david gilmour, Roger Waters, Nick Mason"}}
        status, body = self.apply(self.ids(by_gilmour, by_waters), changes)
        self.assertEqual((status, body["updated"]), (200, 2))
        self.assertEqual(self.related(by_gilmour), "Roger Waters, Nick Mason")
        self.assertEqual(self.related(by_waters), "david gilmour, Nick Mason")

    def test_append_excludes_the_primary_artist(self):
        one = self.track("append-primary", artist=self.gilmour, related_artists="Nick Mason")
        self.apply(self.ids(one), {"related_artists": {"op": "append", "value": "DAVID GILMOUR, Rick Wright"}})
        self.assertEqual(self.related(one), "Nick Mason, Rick Wright")

    def test_a_list_of_only_the_primary_artist_sets_an_empty_field(self):
        one = self.track("only-primary", artist=self.gilmour, related_artists="Nick Mason")
        self.apply(self.ids(one), {"related_artists": {"op": "set", "value": "David Gilmour"}})
        self.assertEqual(self.related(one), "")


class SimultaneousArtistChangeTests(_RelatedFixture):
    def test_set_is_normalized_against_the_new_primary_artist(self):
        one = self.track("both-set", artist=self.gilmour)
        changes = {"artist": {"op": "set", "value": "roger waters"},
                   "related_artists": {"op": "set", "value": "David Gilmour, Roger Waters, Nick Mason"}}
        status, preview = self.preview(self.ids(one), changes)
        self.assertEqual(status, 200, preview)
        after = {c["field"]: c["after"] for c in preview["samples"][0]["changes"]}
        self.assertEqual(after["related_artists"], "David Gilmour, Nick Mason")
        status, body = self.apply(self.ids(one), changes, token=preview["token"])
        self.assertEqual((status, body["updated"]), (200, 1))
        track = Track.objects.select_related("artist").get(pk=one.pk)
        self.assertEqual((track.artist.name, track.related_artists), ("Roger Waters", "David Gilmour, Nick Mason"))

    def test_append_drops_an_existing_entry_that_becomes_the_primary_artist(self):
        one = self.track("both-append", artist=self.gilmour, related_artists="Roger Waters, Nick Mason")
        changes = {"artist": {"op": "set", "value": "Roger Waters"},
                   "related_artists": {"op": "append", "value": "David Gilmour"}}
        self.apply(self.ids(one), changes)
        self.assertEqual(self.related(one), "Nick Mason, David Gilmour")

    def test_a_brand_new_primary_artist_is_excluded_in_preview_and_apply(self):
        one = self.track("both-new", artist=self.gilmour)
        changes = {"artist": {"op": "set", "value": "Brand New Act"},
                   "related_artists": {"op": "set", "value": "brand new act, Guest"}}
        _status, preview = self.preview(self.ids(one), changes)
        after = {c["field"]: c["after"] for c in preview["samples"][0]["changes"]}
        self.assertEqual((after["artist"], after["related_artists"]), ("Brand New Act (new)", "Guest"))
        self.apply(self.ids(one), changes, token=preview["token"])
        self.assertEqual(self.related(one), "Guest")

    def test_artist_change_alone_leaves_related_artists_as_stored(self):
        # Matches the single-Track editor: Related Artists are re-normalized only when they are edited too.
        one = self.track("artist-only", artist=self.gilmour, related_artists="Roger Waters")
        self.apply(self.ids(one), {"artist": {"op": "set", "value": "Roger Waters"}})
        self.assertEqual(self.related(one), "Roger Waters")


class LengthTests(_RelatedFixture):
    def test_a_change_set_longer_than_the_field_is_refused_whole(self):
        one = self.track("too-long", related_artists="Keep")
        value = ", ".join(_names(60, 9))                        # 60 * 9 + 59 * 2 = 658 characters
        status, body = self.preview(self.ids(one), {"related_artists": {"op": "set", "value": value}})
        self.assertEqual(status, 400)
        self.assertIn(f"the limit is {LIMIT}", body["error"])
        self.assertIn("658 characters", body["error"])
        status, body = self.preview(self.ids(one), {"related_artists": {"op": "append", "value": value}})
        self.assertEqual(status, 400)
        self.assertEqual(self.related(one), "Keep")

    def test_exactly_the_limit_is_accepted(self):
        one = self.track("at-limit")
        names = _names(50, 8)                                    # 50 * 8 + 49 * 2 = 498
        value = ", ".join(names[:-1] + [names[-1] + "yy"])        # 500
        self.assertEqual(len(value), LIMIT)
        status, body = self.apply(self.ids(one), {"related_artists": {"op": "set", "value": value}})
        self.assertEqual((status, body["updated"]), (200, 1))
        self.assertEqual(self.related(one), value)

    def test_raw_input_over_the_limit_that_cleans_up_under_it_is_accepted(self):
        one = self.track("dupe-heavy")
        value = ", ".join(["Repeated Guest"] * 60)              # 958 raw characters, 14 after cleanup
        status, body = self.apply(self.ids(one), {"related_artists": {"op": "set", "value": value}})
        self.assertEqual((status, body["updated"]), (200, 1))
        self.assertEqual(self.related(one), "Repeated Guest")

    def test_append_that_would_overflow_refuses_only_that_track_and_truncates_nothing(self):
        existing = ", ".join(_names(45, 9))                     # 45 * 9 + 44 * 2 = 493 characters
        full = self.track("nearly-full", related_artists=existing)
        roomy = self.track("roomy", related_artists="First")
        changes = {"related_artists": {"op": "append", "value": "Late Guest One, Late Guest Two"}}
        status, preview = self.preview(self.ids(full, roomy), changes)
        self.assertEqual((status, preview["tracks_changing"]), (200, 1))
        # The refused Track is neither "will change" nor "already match".
        self.assertEqual(preview["summary"]["related_artists"],
                         {"label": "Related Artists", "change": 1, "unchanged": 0})
        refusal, = preview["refusals"]
        self.assertEqual(refusal["id"], full.pk)
        self.assertIn(f"exceed {LIMIT} characters", refusal["reason"])
        self.assertIn("Late Guest One, Late Guest Two", refusal["reason"])
        status, body = self.apply(self.ids(full, roomy), changes, token=preview["token"])
        self.assertEqual((status, body["updated"], [r["id"] for r in body["refused"]]), (200, 1, [full.pk]))
        self.assertEqual(self.related(full), existing)
        self.assertEqual(self.related(roomy), "First, Late Guest One, Late Guest Two")

    def test_append_where_only_some_names_fit_is_refused_not_partially_applied(self):
        existing = ", ".join(_names(40, 10))                    # 478: "Fits" (6 more) fits, the next one does not
        one = self.track("partial", related_artists=existing)
        changes = {"related_artists": {"op": "append", "value": "Fits, Does Not Fit Any More"}}
        _status, preview = self.preview(self.ids(one), changes)
        refusal, = preview["refusals"]
        self.assertIn("not appended: Does Not Fit Any More", refusal["reason"])
        _status, body = self.apply(self.ids(one), changes, token=preview["token"])
        self.assertEqual(([r["id"] for r in body["refused"]], body["updated"]), ([one.pk], 0))
        self.assertEqual(self.related(one), existing)

    def test_overflow_refusal_is_rechecked_under_the_row_lock(self):
        # A preview that saw room, applied to a Track that (by a path outside the fingerprint) has none, is
        # refused rather than written partially.
        one = self.track("lock-recheck", related_artists="Short")
        changes = {"related_artists": {"op": "append", "value": "Guest"}}
        _status, preview = self.preview(self.ids(one), changes)
        real_merge = related_artists.merge_related_artists_detailed

        def overflowing(existing, names, **kwargs):
            value, _appended, _skipped = real_merge(existing, [], **kwargs)
            return value, [], list(names)

        with mock.patch.object(related_artists, "merge_related_artists_detailed", side_effect=overflowing):
            status, body = self.apply(self.ids(one), changes, token=preview["token"])
        self.assertEqual(([r["id"] for r in body["refused"]], body["updated"]), ([one.pk], 0))
        self.assertEqual(self.related(one), "Short")


class PreviewTests(_RelatedFixture):
    def test_preview_reports_accurate_before_and_after_values_and_writes_nothing(self):
        one = self.track("pv-one", related_artists="Existing Guest")
        two = self.track("pv-two")
        changes = {"related_artists": {"op": "append", "value": "New Guest, existing guest"}}
        before = (self.snapshot(one), self.snapshot(two))
        status, preview = self.preview(self.ids(one, two), changes)
        self.assertEqual(status, 200, preview)
        self.assertEqual((self.snapshot(one), self.snapshot(two)), before)
        self.assertEqual(preview["operations"], [{"field": "related_artists", "label": "Related Artists",
                                                  "op": "append", "value": "New Guest, existing guest"}])
        self.assertEqual(preview["summary"]["related_artists"]["change"], 2)
        rows = {s["id"]: s["changes"] for s in preview["samples"]}
        self.assertEqual(rows[one.pk], [{"field": "related_artists", "label": "Related Artists",
                                         "before": "Existing Guest", "after": "Existing Guest, New Guest"}])
        self.assertEqual(rows[two.pk], [{"field": "related_artists", "label": "Related Artists",
                                         "before": "(empty)", "after": "New Guest, existing guest"}])
        _status, cleared = self.preview(self.ids(one), {"related_artists": {"op": "clear"}})
        self.assertEqual(cleared["samples"][0]["changes"][0]["after"], "(empty)")

    def test_invalid_values_are_refused_before_anything_is_written(self):
        one = self.track("invalid", related_artists="Keep")
        cases = (
            ({"related_artists": {"op": "set", "value": ""}}, "choose Clear"),
            ({"related_artists": {"op": "append", "value": " , ,, "}}, "choose Clear"),
            ({"related_artists": {"op": "set", "value": ["A", "B"]}}, "comma-separated"),
            ({"related_artists": {"op": "set"}}, "needs a value"),
            ({"related_artists": {"op": "add", "value": "A"}}, "expected"),
            ({"artist": {"op": "append", "value": "A"}}, "expected"),
            ({"composer": {"op": "append", "value": "A"}}, "expected"),
        )
        for changes, message in cases:
            with self.subTest(changes=changes):
                status, body = self.preview(self.ids(one), changes)
                self.assertEqual(status, 400, body)
                self.assertIn(message, body["error"])
        self.assertEqual(self.related(one), "Keep")

    def test_the_operation_is_bound_to_the_token(self):
        one = self.track("bound")
        _status, preview = self.preview(self.ids(one), {"related_artists": {"op": "set", "value": "Guest"}})
        status, body = self.apply(self.ids(one), {"related_artists": {"op": "append", "value": "Guest"}},
                                  token=preview["token"])
        self.assertEqual(status, 409)
        self.assertIn("changes differ from the preview", body["error"])
        self.assertEqual(self.related(one), "")


class StalePreviewTests(_RelatedFixture):
    changes = {"related_artists": {"op": "append", "value": "Guest"}}

    def test_related_artists_changed_since_the_preview_makes_the_track_stale(self):
        changed, steady = self.track("stale-ra", related_artists="Was"), self.track("steady")
        _status, preview = self.preview(self.ids(changed, steady), self.changes)
        Track.objects.filter(pk=changed.pk).update(related_artists="Someone Else's Edit")
        status, body = self.apply(self.ids(changed, steady), self.changes, token=preview["token"])
        self.assertEqual(([t["id"] for t in body["stale"]], body["updated"]), ([changed.pk], 1))
        self.assertEqual(self.related(changed), "Someone Else's Edit")
        self.assertEqual(self.related(steady), "Guest")

    def test_a_different_primary_artist_since_the_preview_makes_the_track_stale(self):
        one = self.track("stale-artist", artist=self.gilmour)
        _status, preview = self.preview(self.ids(one), self.changes)
        Track.objects.filter(pk=one.pk).update(artist=self.waters)
        _status, body = self.apply(self.ids(one), self.changes, token=preview["token"])
        self.assertEqual([t["id"] for t in body["stale"]], [one.pk])

    def test_a_rename_of_the_primary_artist_since_the_preview_makes_the_track_stale(self):
        guest = Artist.objects.create(name="Guest Later Primary")
        one = self.track("stale-rename", artist=guest)
        _status, preview = self.preview(self.ids(one), self.changes)
        Artist.objects.filter(pk=guest.pk).update(name="Guest")
        _status, body = self.apply(self.ids(one), self.changes, token=preview["token"])
        self.assertEqual(([t["id"] for t in body["stale"]], body["updated"]), ([one.pk], 0))
        self.assertEqual(self.related(one), "")

    def test_a_track_previewed_as_matching_that_changed_is_stale(self):
        one = self.track("stale-matched", related_artists="Guest")
        _status, preview = self.preview(self.ids(one), self.changes)
        self.assertEqual(preview["tracks_unchanged"], 1)
        Track.objects.filter(pk=one.pk).update(related_artists="")
        _status, body = self.apply(self.ids(one), self.changes, token=preview["token"])
        self.assertEqual(([t["id"] for t in body["stale"]], self.related(one)), ([one.pk], ""))

    def test_unrelated_activity_does_not_make_a_track_stale(self):
        one = self.track("not-stale")
        _status, preview = self.preview(self.ids(one), self.changes)
        Track.objects.filter(pk=one.pk).update(play_count=F("play_count") + 1)
        _status, body = self.apply(self.ids(one), self.changes, token=preview["token"])
        self.assertEqual((body["updated"], body["stale"]), (1, []))


class SelectionAndBoundaryTests(_RelatedFixture):
    def test_filtered_selection_edits_exactly_the_matching_tracks(self):
        match_a = self.track("Sunrise", related_artists="Old")
        match_b = self.track("Sunset")
        other = self.track("Moonlight", related_artists="Untouched")
        excluded = self.track("Sunburst")
        selection = {"mode": "filter", "filter": {"q": "Sun"}, "exclude_ids": [excluded.pk]}
        status, body = self.apply(selection, {"related_artists": {"op": "append", "value": "Guest"}})
        self.assertEqual((status, body["updated"]), (200, 2))
        self.assertEqual((self.related(match_a), self.related(match_b)), ("Old, Guest", "Guest"))
        self.assertEqual((self.related(other), self.related(excluded)), ("Untouched", ""))

    def test_selection_limit_still_applies(self):
        status, body = self.preview({"mode": "ids", "ids": list(range(1, track_bulk_edit.MAX_TRACKS + 2))},
                                    {"related_artists": {"op": "set", "value": "Guest"}})
        self.assertEqual(status, 400)
        self.assertIn(f"limited to {track_bulk_edit.MAX_TRACKS}", body["error"])

    def test_database_metadata_only(self):
        one = self.track("db-only", related_artists="Was")
        before = self.snapshot(one)
        with mock.patch("library.services.media_relocation.execute") as move, \
                mock.patch("library.services.media_relocation.check_source") as check:
            status, body = self.apply(self.ids(one), {"related_artists": {"op": "set", "value": "Now"}})
        self.assertEqual((status, body["updated"]), (200, 1))
        move.assert_not_called()
        check.assert_not_called()
        after = self.snapshot(one)
        self.assertEqual({k for k in before if before[k] != after[k]}, {"related_artists", "updated_at"})
        self.assertEqual(list(self.root.rglob("*")), [])

    def test_combined_with_other_fields_and_memberships(self):
        one = self.track("combined", related_artists="Was")
        changes = {"related_artists": {"op": "append", "value": "Guest"}, "year": {"op": "set", "value": 1977},
                   "holidays": {"add": ["xmas"]}}
        status, body = self.apply(self.ids(one), changes)
        self.assertEqual((status, body["updated"]), (200, 1))
        track = Track.objects.get(pk=one.pk)
        self.assertEqual((track.related_artists, track.year), ("Was, Guest", 1977))
        self.assertEqual(list(track.holidays.values_list("code", flat=True)), ["xmas"])

    def test_permission_is_unchanged(self):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Group
        one = self.track("perm")
        contributor = get_user_model().objects.create_user("ra-contrib", "c@example.invalid", "pw")
        contributor.groups.add(Group.objects.get_or_create(name="Contributor")[0])
        self.client.force_login(contributor)
        response = self.client.post(reverse("library:api-track-bulk-edit-preview"),
                                    {"selection": self.ids(one),
                                     "changes": {"related_artists": {"op": "set", "value": "Guest"}}},
                                    content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.related(one), "")


class AuditTests(_RelatedFixture):
    def test_apply_is_audited_with_the_operation(self):
        one = self.track("audited")
        changes = {"related_artists": {"op": "append", "value": "Guest"}}
        _status, preview = self.preview(self.ids(one), changes)
        with self.captureOnCommitCallbacks(execute=True):
            status, body = self.apply(self.ids(one), changes, token=preview["token"])
        self.assertEqual((status, body["outcome"]), (200, "completed"))
        event, = audit_events("bulk_edit.apply")
        self.assertEqual(event.detail["operations"], ["related_artists:append"])
        self.assertNotIn(preview["token"], repr(event.detail))


class PageContractTests(_RelatedFixture):
    def page(self):
        return self.client.get(reverse("library:library")).content.decode()

    def test_related_artists_row_sits_immediately_below_artist(self):
        html = self.page()
        grid = html[html.index('id="beGrid"'):]
        labels = [part.split('"', 1)[0] for part in grid.split('<label for="be-')[1:4]]
        self.assertEqual(labels, ["artist-op", "related_artists-op", "album-op"])

    def test_row_offers_leave_set_append_clear_and_never_truncates_input(self):
        html = self.page()
        row = html[html.index('id="be-related_artists-op"'):]
        row = row[:row.index("</select>")]
        self.assertEqual([part.split('"', 1)[0] for part in row.split('<option value="')[1:]],
                         ["leave", "set", "append", "clear"])
        field = html[html.index('<input type="text" id="be-related_artists"'):]
        field = field[:field.index(">")]
        self.assertNotIn("maxlength", field)          # the browser would cut a pasted list off silently
        self.assertIn("disabled", field)

    def test_script_sends_append_and_enables_the_input_for_it(self):
        html = self.page()
        self.assertIn("input.disabled = select.value !== 'set' && select.value !== 'append';", html)
        self.assertIn("changes[field] = { op: sel.value === 'append' ? 'append' : 'set', value };", html)
        self.assertIn("append: 'append'", html)


class ServiceReuseTests(SimpleTestCase):
    def test_the_shared_related_artists_rules_are_used(self):
        import inspect
        source = inspect.getsource(track_bulk_edit)
        for name in ("canonicalize_related_artists", "merge_related_artists_detailed", "format_related_artists",
                     "_related_artists_max_length"):
            self.assertIn(name, source)
        self.assertEqual(track_bulk_edit.FIELDS["related_artists"].label, "Related Artists")
        self.assertEqual(list(track_bulk_edit.FIELDS)[:2], ["artist", "related_artists"])
