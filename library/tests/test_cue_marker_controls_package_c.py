"""r0123 Package C: Track Detail cue-marker controls -- the state/action endpoint, server-side
confirmation of protected replacements, approval history and waveform trust presentation (r0124:
Refresh Waveform Only was retired; Reanalyze Track is the one waveform action).

Server-side behaviour only: the page's JavaScript is exercised in a browser at field acceptance
(no browser is available inside the test sandbox). Real audio is generated into per-test
temporary directories; no station media, waveform, database or service is touched."""
import errno
import json
import os
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client, override_settings

from library.models import Category, Track, validate_category_storage_component
from library.services import cue_markers, waveform_cache
from library.tests.test_waveform_integrity_package_b import _audio, _Base, _row

M = cue_markers.MARKERS
REPO = Path(__file__).resolve().parents[2]


class _Controls(_Base):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.user)

    def state(self, track):
        response = self.client.get(f"/api/tracks/{track.pk}/cue-markers/")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def act(self, track, body, status=200):
        response = self.client.post(f"/api/tracks/{track.pk}/cue-markers/", data=json.dumps(body),
                                    content_type="application/json")
        self.assertEqual(response.status_code, status, response.content)
        return response.json()

    def patch_track(self, track, body):
        return self.client.patch(f"/api/tracks/{track.pk}/", data=json.dumps(body), content_type="application/json")

    def displays(self, track):
        return {name: info["display"] for name, info in self.state(track)["cue_marker_review"].items()}


# ------------------------------------------------------------------ presentation

class PresentationTests(_Controls):
    def test_the_page_renders_eight_marker_rows_and_the_controls(self):
        track = self.track()
        html = self.client.get(f"/track/{track.pk}/").content.decode()
        self.assertEqual(re.findall(r'data-marker="([a-z_]+)"', html), list(M))
        for name in M:
            self.assertIn(f'<label for="f_{name}">', html)
        for element in ('id="lockAllBtn"', 'id="unlockAllBtn"', 'id="reanalyzeBtn"', 'id="reanalyzeStatus"',
                        'id="cueNotice"', 'role="status"', "Locked and historical cue points keep their values"):
            self.assertIn(element, html)
        for retired in ("refreshWaveformBtn", "refresh-waveform", "Refresh Waveform Only"):   # r0124
            self.assertNotIn(retired, html)

    def test_every_display_state_is_reported_truthfully(self):
        track = self.track()                                          # historical values, legacy cache
        Track.objects.filter(pk=track.pk).update(waveform_path="")    # never analyzed: Cue In 0 is a default
        self.assertEqual(self.displays(track)["cue_in_seconds"], "default_zero")
        self.assertEqual(self.displays(track)["next_start_seconds"], "unset")
        track = self.track(name="b.flac")
        cue_markers.apply_calculation(track.pk, {"next_start_seconds": 2.5, "hook_in_seconds": 1.0}, basis_revision=0,
                                      source=cue_markers.SOURCE_GENERATED, media_replaced=True)
        cue_markers.operator_edit(track.pk, expected_revision=1, user=self.user, values={"intro_until_seconds": 0.8})
        cue_markers.operator_edit(track.pk, expected_revision=2, user=self.user, approve=["hook_in_seconds"])
        cue_markers.operator_edit(track.pk, expected_revision=3, user=self.user, unlock=["hook_in_seconds"])
        shown = self.displays(track)
        self.assertEqual((shown["next_start_seconds"], shown["hook_in_seconds"], shown["intro_until_seconds"],
                          shown["cue_in_seconds"]), ("auto", "unlocked_operator", "locked_unverified", "historical"))
        state = self.state(track)
        self.assertFalse(state["audio_verified"])                     # unknown audio is never shown as verified
        self.assertEqual(state["waveform"]["status"], "legacy")
        self.assertFalse(state["repick_available"])

    def test_current_other_and_replaced_audio_approvals_do_not_look_alike(self):
        track = self.track()
        self.reanalyze(track)
        state = self.act(track, {"cue_marker_revision": _row(track.pk)["cue_marker_revision"],
                                 "approve": ["cue_in_seconds"]})
        self.assertTrue(state["audio_verified"])
        self.assertEqual(state["cue_marker_review"]["cue_in_seconds"]["display"], "approved")
        self.assertEqual(state["cue_marker_review"]["cue_in_seconds"]["approved_by_name"], self.user.username)
        _audio(Path(track.filepath), freq=500)                        # replaced outside any writer...
        self.reanalyze(track)                                          # ...and re-identified by reanalysis
        info = self.state(track)["cue_marker_review"]["cue_in_seconds"]
        self.assertEqual((info["display"], info["needs_review"]), ("locked_other_audio", True))
        cue_markers.apply_calculation(track.pk, {"next_start_seconds": 2.0},
                                      basis_revision=_row(track.pk)["cue_marker_revision"], media_replaced=True,
                                      audio_revision="d" * 64)       # a writer publishing new media
        info = self.state(track)["cue_marker_review"]["cue_in_seconds"]
        self.assertEqual((info["display"], info["needs_review"]), ("review", True))
        self.assertEqual(info["previous_approval"]["approved_by_name"], self.user.username)
        self.assertTrue(Track.objects.get(pk=track.pk).ready2air)     # review never changes readiness


# ------------------------------------------------------------------ lock, unlock, approve

class ActionTests(_Controls):
    def test_approve_an_unchanged_value_and_unlock_keep_values_and_history(self):
        track = self.track()
        before = {name: getattr(track, name) for name in M}
        state = self.act(track, {"cue_marker_revision": 0, "approve": ["cue_in_seconds"]})
        self.assertEqual((state["cue_marker_revision"], state["result"]["approved"]), (1, ["cue_in_seconds"]))
        entry = _row(track.pk)["cue_marker_state"]["cue_in_seconds"]
        self.assertEqual((entry["protection"], entry["approved_by"]), ("locked", self.user.pk))
        state = self.act(track, {"cue_marker_revision": 1, "unlock": ["cue_in_seconds"]})
        self.assertEqual(state["cue_marker_review"]["cue_in_seconds"]["display"], "unlocked_operator")
        entry = _row(track.pk)["cue_marker_state"]["cue_in_seconds"]
        self.assertEqual(entry["previous_approval"]["approved_by"], self.user.pk)     # history kept
        self.assertEqual({name: _row(track.pk)[name] for name in M}, before)            # no value moved

    def test_nothing_is_approved_or_unlocked_without_a_value(self):
        track = self.track()
        state = self.act(track, {"cue_marker_revision": 0, "approve": ["hook_out_seconds"],
                                 "unlock": ["intro_until_seconds"]})
        self.assertEqual(state["result"]["skipped"], {"hook_out_seconds": "no value to approve",
                                                     "intro_until_seconds": "no value to unlock"})
        self.assertEqual((_row(track.pk)["cue_marker_revision"], _row(track.pk)["cue_marker_state"]), (0, {}))
        self.assertIsNone(_row(track.pk)["hook_out_seconds"])

    def test_lock_all_and_unlock_all_are_one_revision_step_each_and_skip_unset_markers(self):
        track = self.track()
        Track.objects.filter(pk=track.pk).update(waveform_path="")    # Cue In 0 = never-analyzed default
        cue_markers.operator_edit(track.pk, expected_revision=0, user=self.user,
                                  values={"next_start_seconds": 2.0, "hook_in_seconds": 1.0})
        state = self.act(track, {"cue_marker_revision": 1, "action": "lock_all"})
        self.assertEqual(state["cue_marker_revision"], 1)            # both already locked: no change at all
        self.act(track, {"cue_marker_revision": 1, "action": "unlock_all"})
        row = _row(track.pk)
        self.assertEqual(row["cue_marker_revision"], 2)
        self.assertEqual({n for n, e in row["cue_marker_state"].items() if e.get("protection") == "unlocked"},
                         {"next_start_seconds", "hook_in_seconds", "cue_in_seconds"})
        state = self.act(track, {"cue_marker_revision": 2, "action": "lock_all"})
        self.assertEqual(state["cue_marker_revision"], 3)
        self.assertEqual(set(state["result"]["approved"]), {"next_start_seconds", "hook_in_seconds"})
        displays = self.displays(track)
        self.assertEqual(displays["cue_in_seconds"], "unlocked_operator")   # the unlocked default stays unlocked
        self.assertEqual((displays["hook_out_seconds"], displays["intro_until_seconds"]), ("unset", "unset"))
        self.assertFalse(state["audio_verified"])                    # no trusted cache: not approved "for this audio"
        self.assertIsNone(_row(track.pk)["cue_marker_state"]["next_start_seconds"]["audio_revision"])

    def test_a_stale_missing_or_invalid_request_changes_nothing(self):
        track = self.track()
        cue_markers.operator_edit(track.pk, expected_revision=0, user=self.user, approve=["cue_in_seconds"])
        for body, status in (({"cue_marker_revision": 0, "action": "unlock_all"}, 409),
                             ({"action": "unlock_all"}, 400),
                             ({"cue_marker_revision": 1, "action": "erase"}, 400),
                             ({"cue_marker_revision": 1, "approve": ["volume"]}, 400),
                             ({"cue_marker_revision": 1, "approve": "cue_in_seconds"}, 400)):
            with self.subTest(body=body):
                self.act(track, body, status=status)
                self.assertEqual(_row(track.pk)["cue_marker_revision"], 1)
        self.assertEqual(self.act(track, {"cue_marker_revision": 0, "unlock": ["cue_in_seconds"]}, 409)["conflict"],
                         True)

    def test_unauthorized_users_cannot_change_protection_or_reanalyze(self):
        track = self.track()
        listener = Client()
        listener.force_login(User.objects.create_user("listener-c", password="pw"))
        for url, body in ((f"/api/tracks/{track.pk}/cue-markers/", {"cue_marker_revision": 0, "action": "lock_all"}),
                          (f"/api/tracks/{track.pk}/reanalyze/", {})):
            with self.subTest(url):
                response = listener.post(url, data=json.dumps(body), content_type="application/json")
                self.assertEqual(response.status_code, 403)
        self.assertEqual(_row(track.pk)["cue_marker_revision"], 0)


# ------------------------------------------------------------------ editing with confirmation

class EditTests(_Controls):
    def test_editing_an_unprotected_marker_locks_only_that_marker(self):
        track = self.track()
        response = self.patch_track(track, {"hook_in_seconds": 1.25, "cue_marker_revision": 0})
        self.assertEqual(response.status_code, 200, response.content)
        state = _row(track.pk)["cue_marker_state"]
        self.assertEqual(set(state), {"hook_in_seconds"})
        self.assertEqual(state["hook_in_seconds"]["protection"], "locked")

    def test_replacing_a_protected_value_needs_server_side_confirmation(self):
        track = self.track()
        cue_markers.operator_edit(track.pk, expected_revision=0, user=self.user, values={"next_start_seconds": 2.0})
        for body in ({"title": "Should Not Save", "next_start_seconds": 2.2, "cue_marker_revision": 1},
                     {"title": "Should Not Save", "next_start_seconds": 2.2, "cue_marker_revision": 1,
                      "confirm_override": ["hook_in_seconds"]}):
            with self.subTest(body=body):
                response = self.patch_track(track, body)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["confirmation_required"], ["next_start_seconds"])
                row = Track.objects.filter(pk=track.pk).values("title", "next_start_seconds",
                                                                "cue_marker_revision").get()
                self.assertEqual((row["title"], row["next_start_seconds"], row["cue_marker_revision"]),
                                 ("song.flac", 2.0, 1))           # nothing at all was saved
        confirmed = self.patch_track(track, {"next_start_seconds": 2.2, "cue_marker_revision": 1,
                                             "confirm_override": ["next_start_seconds"]})
        self.assertEqual(confirmed.status_code, 200, confirmed.content)
        entry = _row(track.pk)["cue_marker_state"]["next_start_seconds"]
        self.assertEqual((_row(track.pk)["next_start_seconds"], entry["protection"]), (2.2, "locked"))
        self.assertEqual((entry["previous_approval"]["value"], entry["previous_approval"]["retired_reason"]),
                         (2.0, "replaced_by_operator"))

    def test_historical_values_need_confirmation_and_record_the_replaced_value(self):
        track = self.track()                                           # Cue In 0.0 with a cache: historical
        self.assertEqual(self.patch_track(track, {"cue_in_seconds": 0.4, "cue_marker_revision": 0}).status_code, 409)
        response = self.patch_track(track, {"cue_in_seconds": 0.4, "cue_marker_revision": 0,
                                            "confirm_override": ["cue_in_seconds"]})
        self.assertEqual(response.status_code, 200, response.content)
        entry = _row(track.pk)["cue_marker_state"]["cue_in_seconds"]
        self.assertEqual(entry["replaced_historical_value"]["value"], 0.0)

    def test_the_authority_enforces_confirmation_under_its_lock(self):
        track = self.track()
        result = cue_markers.operator_edit(track.pk, expected_revision=0, user=self.user,
                                           values={"cue_in_seconds": 0.3}, confirm_protected=[])
        self.assertEqual((result.ok, result.confirmation_required), (False, ["cue_in_seconds"]))
        self.assertEqual(_row(track.pk)["cue_marker_revision"], 0)

    def test_metadata_only_and_unchanged_full_form_saves_touch_no_marker_state(self):
        track = self.track()
        cue_markers.operator_edit(track.pk, expected_revision=0, user=self.user, approve=["cue_in_seconds"])
        before = _row(track.pk)
        unchanged = {name: before[name] for name in M}
        for body in ({"title": "Renamed"}, {"title": "Renamed Again", **unchanged, "cue_marker_revision": 1}):
            with self.subTest(body=sorted(body)):
                self.assertEqual(self.patch_track(track, body).status_code, 200)
                after = _row(track.pk)
                self.assertEqual((after["cue_marker_revision"], after["cue_marker_state"]),
                                 (before["cue_marker_revision"], before["cue_marker_state"]))

    def test_a_stale_page_cannot_overwrite_newer_values(self):
        track = self.track()
        self.act(track, {"cue_marker_revision": 0, "unlock": ["cue_in_seconds"]})
        response = self.patch_track(track, {"cue_in_seconds": 0.7, "cue_marker_revision": 0})
        self.assertEqual((response.status_code, response.json()["conflict"]), (409, True))
        self.assertEqual(_row(track.pk)["cue_in_seconds"], 0.0)


class RelocationConsistencyTests(_Controls):
    def setUp(self):
        super().setUp()
        settings = override_settings(LIBRARY_ROOT=str(self.dir))
        settings.enable()
        self.addCleanup(settings.disable)
        self.destination_category = Category.objects.create(
            code="RELOCATED", name="Relocated", kind=self.category.kind,
        )

    def destination(self, track):
        return self.dir / self.destination_category.code / Path(track.filepath).name

    def assert_rejected_without_change(self, track, category, *, escaped=None):
        source = Path(track.filepath)
        original = source.read_bytes()
        old_category = track.category_id
        response = self.patch_track(track, {"category_id": category.pk})
        self.assertIn(response.status_code, (409, 500), response.content)
        row = Track.objects.get(pk=track.pk)
        self.assertEqual((row.category_id, row.filepath), (old_category, str(source)))
        self.assertEqual(source.read_bytes(), original)
        if escaped is not None:
            self.assertFalse(Path(escaped).exists())
        return response

    def test_late_revision_conflict_occurs_before_media_relocation(self):
        track = self.track()
        source = Path(track.filepath)
        original = source.read_bytes()

        def concurrent_operator(_track):
            result = cue_markers.operator_edit(
                track.pk, expected_revision=0, user=self.user, values={"hook_in_seconds": 0.25},
            )
            self.assertTrue(result.ok)
            return None

        with patch.object(waveform_cache, "current_audio_revision", side_effect=concurrent_operator):
            response = self.patch_track(track, {
                "category_id": self.destination_category.pk,
                "hook_in_seconds": 0.75,
                "cue_marker_revision": 0,
            })

        self.assertEqual((response.status_code, response.json()["conflict"]), (409, True))
        row = Track.objects.get(pk=track.pk)
        self.assertEqual((row.category_id, row.filepath), (self.category.pk, str(source)))
        self.assertEqual((row.hook_in_seconds, row.cue_marker_revision), (0.25, 1))
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(self.destination(track).exists())

    def test_successful_combined_marker_and_category_edit_keeps_db_and_media_consistent(self):
        track = self.track()
        source = Path(track.filepath)
        original = source.read_bytes()
        destination = self.destination(track)

        response = self.patch_track(track, {
            "category_id": self.destination_category.pk,
            "hook_in_seconds": 0.75,
            "cue_marker_revision": 0,
        })

        self.assertEqual(response.status_code, 200, response.content)
        row = Track.objects.get(pk=track.pk)
        self.assertEqual((row.category_id, row.filepath), (self.destination_category.pk, str(destination)))
        self.assertEqual((row.hook_in_seconds, row.cue_marker_revision), (0.75, 1))
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_bytes(), original)

    def test_database_failure_after_move_restores_original_media_location(self):
        track = self.track()
        source = Path(track.filepath)
        original = source.read_bytes()
        destination = self.destination(track)

        with patch.object(Track, "save", autospec=True, side_effect=RuntimeError("injected save failure")):
            response = self.patch_track(track, {"category_id": self.destination_category.pk})

        self.assertEqual(response.status_code, 500)
        row = Track.objects.get(pk=track.pk)
        self.assertEqual((row.category_id, row.filepath), (self.category.pk, str(source)))
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(destination.exists())

    def test_destination_collision_is_never_overwritten(self):
        track = self.track()
        source = Path(track.filepath)
        original = source.read_bytes()
        destination = self.destination(track)
        destination.parent.mkdir()
        destination.write_bytes(b"OTHER TRACK")

        response = self.patch_track(track, {"category_id": self.destination_category.pk})

        self.assertEqual(response.status_code, 409)
        row = Track.objects.get(pk=track.pk)
        self.assertEqual((row.category_id, row.filepath), (self.category.pk, str(source)))
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(destination.read_bytes(), b"OTHER TRACK")

    def test_metadata_only_category_move_does_not_touch_marker_authority(self):
        track = self.track()
        before = _row(track.pk)
        response = self.patch_track(track, {"category_id": self.destination_category.pk})
        self.assertEqual(response.status_code, 200, response.content)
        after = _row(track.pk)
        self.assertEqual(
            (after["cue_marker_revision"], after["cue_marker_state"]),
            (before["cue_marker_revision"], before["cue_marker_state"]),
        )

    def test_category_storage_component_validation_preserves_station_conventions(self):
        for valid in ("Aircheck", "FSN Weekend", "B_GOLD", "3MinCenter", "Rock & Roll"):
            with self.subTest(valid=valid):
                self.assertEqual(validate_category_storage_component(valid), valid)
        self.destination_category.full_clean()                 # the model/application boundary accepts valid codes
        for invalid in ("", ".", "..", "../outside", "/tmp/outside", "nested/child", "bad\x00name"):
            with self.subTest(invalid=repr(invalid)), self.assertRaises(ValidationError):
                validate_category_storage_component(invalid)
        invalid_model = Category(code="../outside", name="Invalid", kind=self.category.kind)
        with self.assertRaises(ValidationError):
            invalid_model.full_clean()

    def test_unsafe_legacy_category_codes_cannot_escape_or_create_directories(self):
        cases = (
            ("../escape", self.dir.parent / "escape"),
            ("nested/child", self.dir / "nested" / "child"),
            ("", None),
        )
        for index, (code, escaped_parent) in enumerate(cases):
            with self.subTest(code=repr(code)):
                track = self.track(name=f"unsafe-{index}.flac")
                category = Category.objects.create(
                    code=code, name=f"Unsafe {index}", kind=self.category.kind,
                )
                escaped = escaped_parent / Path(track.filepath).name if escaped_parent else None
                self.assert_rejected_without_change(track, category, escaped=escaped)
                if escaped_parent is not None:
                    self.assertFalse(escaped_parent.exists())

    def test_an_absolute_legacy_category_code_is_rejected_without_touching_it(self):
        track = self.track(name="absolute.flac")
        # Category.code is varchar(20); use a short root-absolute value that a legacy row can hold.
        absolute_parent = Path(f"/r12a{os.getpid()}")
        self.assertFalse(absolute_parent.exists())
        category = Category.objects.create(
            code=str(absolute_parent), name="Absolute", kind=self.category.kind,
        )
        self.assert_rejected_without_change(
            track, category, escaped=absolute_parent / Path(track.filepath).name,
        )
        self.assertFalse(absolute_parent.exists())

    def test_destination_category_symlink_cannot_escape_the_library_root(self):
        track = self.track(name="destination-symlink.flac")
        with TemporaryDirectory(dir=self.dir.parent) as outside_name:
            outside = Path(outside_name)
            category = Category.objects.create(code="SYMLINKDEST", name="Symlink", kind=self.category.kind)
            (self.dir / category.code).symlink_to(outside, target_is_directory=True)
            escaped = outside / Path(track.filepath).name
            self.assert_rejected_without_change(track, category, escaped=escaped)
            self.assertEqual(list(outside.iterdir()), [])

    def test_source_media_symlink_is_rejected_without_modifying_its_target(self):
        track = self.track(name="source-symlink.flac")
        source = Path(track.filepath)
        target = source.with_name("source-symlink-target.flac")
        original = source.read_bytes()
        source.rename(target)
        source.symlink_to(target.name)

        response = self.patch_track(track, {"category_id": self.destination_category.pk})

        self.assertEqual(response.status_code, 500, response.content)
        row = Track.objects.get(pk=track.pk)
        self.assertEqual((row.category_id, row.filepath), (self.category.pk, str(source)))
        self.assertTrue(source.is_symlink())
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(self.destination(track).exists())

    def test_directory_substitution_between_open_and_link_fails_closed(self):
        track = self.track(name="directory-race.flac")
        source = Path(track.filepath)
        original = source.read_bytes()
        category = Category.objects.create(code="DIRRACE", name="Directory Race", kind=self.category.kind)
        destination_dir = self.dir / category.code
        parked = self.dir / "DIRRACE-parked"
        real_link = os.link

        with TemporaryDirectory(dir=self.dir.parent) as outside_name:
            outside = Path(outside_name)

            def substitute(*args, **kwargs):
                destination_dir.rename(parked)
                destination_dir.symlink_to(outside, target_is_directory=True)
                return real_link(*args, **kwargs)

            with patch("library.views.os.link", side_effect=substitute):
                response = self.patch_track(track, {"category_id": category.pk})

            self.assertEqual(response.status_code, 500, response.content)
            row = Track.objects.get(pk=track.pk)
            self.assertEqual((row.category_id, row.filepath), (self.category.pk, str(source)))
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual(list(parked.iterdir()), [])

    def test_cross_filesystem_copy_fallback_obeys_the_same_confinement_and_commits(self):
        track = self.track(name="cross-filesystem.flac")
        source = Path(track.filepath)
        original = source.read_bytes()
        destination = self.destination(track)

        with patch("library.views.os.link", side_effect=OSError(errno.EXDEV, "cross-device link")):
            response = self.patch_track(track, {"category_id": self.destination_category.pk})

        self.assertEqual(response.status_code, 200, response.content)
        row = Track.objects.get(pk=track.pk)
        self.assertEqual((row.category_id, row.filepath), (self.destination_category.pk, str(destination)))
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_bytes(), original)


# ------------------------------------------------------------------ waveform trust and reanalysis

class TrustAndReanalyzeTests(_Controls):
    def test_trust_states_and_repick_availability_follow_the_backend(self):
        track = self.track()
        self.assertEqual((self.state(track)["waveform"]["status"], self.state(track)["repick_available"]),
                         ("legacy", False))
        self.assertIn("Reanalyze Track", self.state(track)["waveform"]["explanation"])
        self.reanalyze(track)
        self.assertEqual((self.state(track)["waveform"]["status"], self.state(track)["repick_available"]),
                         ("trusted", True))
        self.act(track, {"cue_marker_revision": _row(track.pk)["cue_marker_revision"], "unlock": ["cue_in_seconds"]})
        self.assertEqual((self.state(track)["waveform"]["status"], self.state(track)["repick_available"]),
                         ("trusted_stale_markers", True))
        moved = Path(track.filepath).with_name("moved.flac")
        os.rename(track.filepath, moved)
        Track.objects.filter(pk=track.pk).update(filepath=str(moved), filename=moved.name)
        state = self.state(track)
        self.assertEqual((state["waveform"]["status"], state["repick_available"]), ("unverified_audio", False))
        self.assertIn("changed or moved", state["waveform"]["explanation"])
        self.cache(track).write_text("{\"track_id\"")
        state = self.state(track)
        self.assertEqual((state["waveform"]["status"], state["repick_available"]), ("malformed", False))
        self.assertIn("Reanalyze Track", state["waveform"]["explanation"])

    def test_the_refresh_waveform_workflow_is_retired(self):
        track = self.track()
        for url in (f"/api/tracks/{track.pk}/refresh-waveform/", f"/api/tracks/{track.pk}/refresh-waveform/status/"):
            with self.subTest(url):
                self.assertEqual(self.client.post(url).status_code, 404)
        source = (REPO / "library/templates/library/track_detail.html").read_text()
        for retired in ("refreshWaveformOnly", "pollRefresh", "REFRESH_KEY", "/refresh-waveform/", "sessionStorage"):
            self.assertNotIn(retired, source)
        reanalyze = source[source.index("async function reanalyzeTrack"):source.index("// --- Fast cue-point repick")]
        self.assertIn("data.outcome", reanalyze)                         # reports what actually happened
        self.assertIn("data.waveform.regenerated", reanalyze)            # reloads only an installed waveform
        click = source[source.index("function onWaveformClick"):source.index("async function loadWaveform")]
        self.assertNotIn("fetch(", click)                              # a click seeks/places; it never approves
