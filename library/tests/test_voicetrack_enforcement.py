"""2.22B corrective -- the VoiceTrack binding and removal invariants hold on the
REAL ORM mutation surface, not just the public manager and save().

Codex's reproductions (``VoiceTrack._base_manager.update(media_id=...)`` and
``vt.save_base()`` re-binding outside bind_media; generic and admin deletion
bypassing the removal service) are pinned here, together with every other
path Django offers to write ``media`` or delete a row -- and the legitimate
paths that must keep working: unrelated edits, the binding and removal
services, Track cascade deletion and SET_NULL from user deletion.
"""
import tempfile
from pathlib import Path
from unittest import mock

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection, models, transaction
from django.forms import modelform_factory
from django.test import TestCase, TransactionTestCase, override_settings

from library import voicetrack_guard
from library.models import Track, VoiceTrack, VoiceTrackQuerySet
from library.services import voicetrack_media as vtm
from library.tests.test_voicetrack_production_media import ingest_take, make_legacy_vt, make_track
from production.services import layout, retention
from production.tests.support import IsolatedMediaRootMixin

User = get_user_model()
Refused = voicetrack_guard.UnguardedVoiceTrackBinding
RemovalRefused = voicetrack_guard.UnguardedVoiceTrackRemoval


def bound(vt_or_id):
    pk = getattr(vt_or_id, "pk", vt_or_id)
    return VoiceTrack.objects.values_list("media_id", flat=True).get(pk=pk)


@override_settings(SECURE_SSL_REDIRECT=False)
class BindingSurfaceTests(IsolatedMediaRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.track = make_track()
        self.media = ingest_take()
        self.other = ingest_take(name="flac.flac")
        self.vt = make_legacy_vt(self.track)                      # unbound row
        self.bound_vt = vtm.bind_media(track_id=self.track.pk, position="outro", media_id=self.media.pk,
                                       user=None, expected_revision=vtm.ABSENT)

    def assert_refused(self, attempt, label):
        with self.subTest(label), self.assertRaises(Refused):
            with transaction.atomic():
                attempt()
        self.assertIsNone(bound(self.vt), label)
        self.assertEqual(bound(self.bound_vt), self.media.pk, label)

    def test_the_base_manager_is_the_guarded_one(self):
        self.assertIsInstance(VoiceTrack._base_manager.all(), VoiceTrackQuerySet)
        apps.clear_cache()
        self.assertIsInstance(VoiceTrack._base_manager.all(), VoiceTrackQuerySet)
        self.assertIsInstance(VoiceTrack.objects.using("default").all(), VoiceTrackQuerySet)
        self.assertIsInstance(self.track.voicetracks.all(), VoiceTrackQuerySet)
        self.assertIsInstance(self.media.evergreen_voicetracks.all(), VoiceTrackQuerySet)

    def test_codex_reproduction_base_manager_update(self):
        self.assert_refused(lambda: VoiceTrack._base_manager.filter(pk=self.vt.pk).update(media_id=self.media.pk),
                            "_base_manager.update(media_id=)")
        self.assert_refused(lambda: VoiceTrack._base_manager.update(media=None), "_base_manager.update(media=None)")

    def test_codex_reproduction_save_base(self):
        def save_base():
            self.vt.media_id = self.media.pk
            self.vt.save_base()

        def unbound_save_base():
            self.vt.media_id = self.media.pk
            models.Model.save_base(self.vt)

        self.assert_refused(save_base, "save_base()")
        self.vt = VoiceTrack.objects.get(pk=self.vt.pk)
        self.assert_refused(unbound_save_base, "models.Model.save_base(vt)")

    def test_every_other_orm_path_is_refused(self):
        vt_id, media = self.vt.pk, self.media

        def fresh():
            return VoiceTrack.objects.get(pk=vt_id)

        def deferred_assign():
            vt = VoiceTrack.objects.only("gain_db").get(pk=vt_id)
            vt.media_id = media.pk
            vt.save()

        def deferred_load_then_assign():
            vt = VoiceTrack.objects.defer("media").get(pk=vt_id)
            vt.media_id                                      # loads the deferred field
            vt.media = media
            vt.save()

        def unbind_by_save():
            vt = VoiceTrack.objects.get(pk=self.bound_vt.pk)
            vt.media = None
            vt.save(update_fields=["media"])

        def queryset_internal_update():
            field = VoiceTrack._meta.get_field("media")
            VoiceTrack.objects.filter(pk=vt_id)._update([(field, None, media.pk)])

        attempts = {
            "instance save()": lambda: (setattr(fresh_vt := fresh(), "media", media), fresh_vt.save()),
            "save(update_fields=[media])": lambda: (setattr(v := fresh(), "media", media),
                                                    v.save(update_fields=["media"])),
            "unbinding save()": unbind_by_save,
            "objects.update()": lambda: VoiceTrack.objects.filter(pk=vt_id).update(media=media),
            ".using().update()": lambda: VoiceTrack.objects.using("default").filter(pk=vt_id).update(
                media_id=media.pk),
            "QuerySet._update()": queryset_internal_update,
            "deferred instance assign": deferred_assign,
            "deferred instance load + assign": deferred_load_then_assign,
            "bulk_update()": lambda: VoiceTrack.objects.bulk_update(
                [setattr(v := fresh(), "media_id", media.pk) or v], ["media"]),
            "_base_manager.bulk_update()": lambda: VoiceTrack._base_manager.bulk_update(
                [setattr(v := fresh(), "media_id", media.pk) or v], ["media_id"]),
            "bulk_create()": lambda: VoiceTrack.objects.bulk_create(
                [VoiceTrack(track=make_track("other"), position="intro", filepath="", media=media)]),
            "bulk_create(update_conflicts)": lambda: VoiceTrack.objects.bulk_create(
                [VoiceTrack(track=self.track, position="intro", filepath="")],
                update_conflicts=True, unique_fields=["track", "position"], update_fields=["media"]),
            "create()": lambda: VoiceTrack.objects.create(track=make_track("c"), position="intro", filepath="",
                                                          media=media),
            "get_or_create(defaults)": lambda: VoiceTrack.objects.get_or_create(
                track=make_track("g"), position="intro", defaults={"filepath": "", "media": media}),
            "update_or_create()": lambda: VoiceTrack.objects.update_or_create(
                track=self.track, position="intro", defaults={"media": media}),
            "update_or_create(create_defaults)": lambda: VoiceTrack.objects.update_or_create(
                track=make_track("u"), position="intro", create_defaults={"filepath": "", "media": media}),
            "track.voicetracks.update()": lambda: self.track.voicetracks.update(media=media),
            "track.voicetracks.create()": lambda: make_track("r").voicetracks.create(
                position="intro", filepath="", media=media),
            "media.evergreen_voicetracks.update()": lambda: media.evergreen_voicetracks.update(media=None),
            "media.evergreen_voicetracks.clear()": lambda: media.evergreen_voicetracks.clear(),
            "media.evergreen_voicetracks.clear(bulk=False)": lambda: media.evergreen_voicetracks.clear(bulk=False),
            "media.evergreen_voicetracks.remove()": lambda: media.evergreen_voicetracks.remove(self.bound_vt),
            "media.evergreen_voicetracks.set([])": lambda: media.evergreen_voicetracks.set([]),
            "other.evergreen_voicetracks.add()": lambda: self.other.evergreen_voicetracks.add(fresh()),
            "other.evergreen_voicetracks.add(bulk=False)": lambda: self.other.evergreen_voicetracks.add(
                fresh(), bulk=False),
        }
        for label, attempt in attempts.items():
            self.assert_refused(attempt, label)

    def test_a_modelform_and_the_admin_cannot_change_the_binding(self):
        form_class = modelform_factory(VoiceTrack, fields="__all__")
        vt = VoiceTrack.objects.get(pk=self.vt.pk)
        form = form_class(data={"track": self.track.pk, "position": "intro", "filepath": vt.filepath,
                                "media": str(self.media.pk), "gain_db": "0", "source": "browser"}, instance=vt)
        self.assertTrue(form.is_valid(), form.errors)
        self.assert_refused(form.save, "ModelForm(fields='__all__').save()")

        admin = User.objects.create_superuser("root", password="x")
        self.client.force_login(admin)
        url = f"/admin/library/voicetrack/{self.vt.pk}/change/"
        response = self.client.post(url, {"track": self.track.pk, "position": "intro",
                                          "filepath": self.vt.filepath, "media": str(self.other.pk),
                                          "gain_db": "-3", "source": "browser"})
        self.assertEqual(response.status_code, 302, response.content[:2000])
        row = VoiceTrack.objects.get(pk=self.vt.pk)
        self.assertEqual((row.gain_db, row.media_id), (-3.0, None))     # the edit is saved, a binding is not

    def test_a_stale_instance_cannot_put_an_old_binding_back(self):
        stale = VoiceTrack.objects.get(pk=self.bound_vt.pk)                       # sees self.media
        vtm.bind_media(track_id=self.track.pk, position="outro", media_id=self.other.pk, user=None,
                       expected_revision=vtm.revision_of(stale))
        stale.gain_db = 2.0
        stale.save()                                         # an unrelated edit from an old instance
        row = VoiceTrack.objects.get(pk=self.bound_vt.pk)
        self.assertEqual((row.gain_db, row.media_id), (2.0, self.other.pk))

    def test_unrelated_edits_and_saves_keep_working(self):
        vt = VoiceTrack.objects.get(pk=self.bound_vt.pk)
        vt.gain_db = -1.0
        vt.save()
        vt.save_base()
        vt.save(update_fields=["gain_db"])
        VoiceTrack._base_manager.filter(pk=vt.pk).update(gain_db=1.5)
        VoiceTrack.objects.using("default").filter(pk=vt.pk).update(source="studio")
        deferred = VoiceTrack.objects.only("gain_db").get(pk=vt.pk)
        deferred.gain_db = 0.5
        deferred.save()
        VoiceTrack.objects.bulk_update([deferred], ["gain_db"])
        VoiceTrack.objects.create(track=make_track("legacy"), position="intro", filepath="/nonexistent/x.wav")
        row = VoiceTrack.objects.get(pk=vt.pk)
        self.assertEqual((row.gain_db, row.source, row.media_id), (0.5, "studio", self.media.pk))

    def test_the_binding_service_still_rebinds_the_same_row(self):
        vt = vtm.bind_media(track_id=self.track.pk, position="outro", media_id=self.other.pk, user=None,
                            expected_revision=vtm.revision_of(VoiceTrack.objects.get(pk=self.bound_vt.pk)))
        self.assertEqual((vt.pk, bound(vt)), (self.bound_vt.pk, self.other.pk))
        self.assertFalse(voicetrack_guard.binding_allowed())


@override_settings(SECURE_SSL_REDIRECT=False)
class RemovalTests(IsolatedMediaRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.track = make_track()
        self.media = ingest_take()
        self.vt = vtm.bind_media(track_id=self.track.pk, position="intro", media_id=self.media.pk, user=None,
                                 expected_revision=vtm.ABSENT)
        self.media_path = layout.resolve_storage_path(self.media.storage_key)

    def assert_media_survives(self):
        self.media.refresh_from_db()
        self.assertTrue(self.media.is_present)
        self.assertTrue(self.media_path.is_file())

    def test_generic_deletion_is_refused(self):
        attempts = {
            "instance delete()": lambda: VoiceTrack.objects.get(pk=self.vt.pk).delete(),
            "objects.filter().delete()": lambda: VoiceTrack.objects.filter(pk=self.vt.pk).delete(),
            "objects.all().delete()": lambda: VoiceTrack.objects.all().delete(),
            "_base_manager.all().delete()": lambda: VoiceTrack._base_manager.all().delete(),
            ".using().delete()": lambda: VoiceTrack.objects.using("default").filter(pk=self.vt.pk).delete(),
            "track.voicetracks.all().delete()": lambda: self.track.voicetracks.all().delete(),
            "media.evergreen_voicetracks.all().delete()": lambda: self.media.evergreen_voicetracks.all().delete(),
        }
        for label, attempt in attempts.items():
            with self.subTest(label), self.assertRaises(RemovalRefused):
                with transaction.atomic():
                    attempt()
            self.assertTrue(VoiceTrack.objects.filter(pk=self.vt.pk).exists(), label)
        self.assert_media_survives()

    def test_admin_deletion_is_refused(self):
        admin = User.objects.create_superuser("root", password="x")
        self.client.force_login(admin)
        delete_url = f"/admin/library/voicetrack/{self.vt.pk}/delete/"
        self.assertEqual(self.client.get(delete_url).status_code, 403)
        self.assertEqual(self.client.post(delete_url, {"post": "yes"}).status_code, 403)
        changelist = self.client.get("/admin/library/voicetrack/")
        self.assertNotIn("delete_selected", changelist.content.decode())
        self.client.post("/admin/library/voicetrack/", {"action": "delete_selected",
                                                         "_selected_action": [self.vt.pk], "post": "yes"})
        change = self.client.get(f"/admin/library/voicetrack/{self.vt.pk}/change/")
        self.assertEqual(change.status_code, 200)
        self.assertNotIn(delete_url, change.content.decode())
        self.assertTrue(VoiceTrack.objects.filter(pk=self.vt.pk).exists())
        self.assert_media_survives()

    def test_iportal_removal_deletes_the_row_and_keeps_the_media(self):
        with self.assertRaises(vtm.VoiceTrackConflict):
            vtm.remove_voicetrack(track_id=self.track.pk, position="intro", user=None, expected_revision="stale")
        self.assertTrue(VoiceTrack.objects.filter(pk=self.vt.pk).exists())
        with self.captureOnCommitCallbacks(execute=True):
            vtm.remove_voicetrack(track_id=self.track.pk, position="intro", user=None,
                                  expected_revision=vtm.revision_of(self.vt))
        self.assertFalse(VoiceTrack.objects.filter(pk=self.vt.pk).exists())
        self.assertFalse(voicetrack_guard.removal_allowed())
        self.assert_media_survives()
        self.assertEqual(retention.find_references(self.media), [])             # reclaimable by retention

    def test_iportal_removal_cleans_only_a_confined_legacy_file(self):
        with tempfile.TemporaryDirectory() as voicetracks, tempfile.TemporaryDirectory() as elsewhere:
            inside, outside = Path(voicetracks, "1", "intro.wav"), Path(elsewhere, "outro.wav")
            inside.parent.mkdir()
            inside.write_bytes(b"x")
            outside.write_bytes(b"x")
            track = make_track("legacy")
            a = make_legacy_vt(track, "intro", str(inside))
            b = make_legacy_vt(track, "outro", str(outside))
            with mock.patch.object(vtm, "LEGACY_VOICETRACK_DIR", voicetracks), \
                    self.captureOnCommitCallbacks(execute=True):
                vtm.remove_voicetrack(track_id=track.pk, position="intro", user=None,
                                      expected_revision=vtm.revision_of(a))
                vtm.remove_voicetrack(track_id=track.pk, position="outro", user=None,
                                      expected_revision=vtm.revision_of(b))
            self.assertFalse(inside.exists())
            self.assertTrue(outside.exists())

    def test_deleting_the_track_still_cascades_and_never_touches_media(self):
        legacy = make_legacy_vt(self.track, "outro")
        self.track.delete()
        self.assertFalse(VoiceTrack.objects.filter(pk__in=[self.vt.pk, legacy.pk]).exists())
        self.assert_media_survives()
        self.assertEqual(retention.find_references(self.media), [])

    def test_queryset_track_deletion_and_the_track_admin_still_cascade(self):
        other = make_track("other")
        vtm.bind_media(track_id=other.pk, position="intro", media_id=ingest_take().pk, user=None,
                       expected_revision=vtm.ABSENT)
        Track.objects.filter(pk=other.pk).delete()
        self.assertFalse(VoiceTrack.objects.filter(track_id=other.pk).exists())

        admin = User.objects.create_superuser("root", password="x")
        self.client.force_login(admin)
        response = self.client.post(f"/admin/library/track/{self.track.pk}/delete/", {"post": "yes"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Track.objects.filter(pk=self.track.pk).exists())
        self.assertFalse(VoiceTrack.objects.filter(pk=self.vt.pk).exists())
        self.assert_media_survives()

    def test_deleting_the_recording_user_still_nulls_recorded_by(self):
        user = User.objects.create_user("talent")
        vt = vtm.bind_media(track_id=self.track.pk, position="intro", media_id=ingest_take().pk, user=user,
                            expected_revision=vtm.revision_of(self.vt))
        user.delete()
        row = VoiceTrack.objects.get(pk=vt.pk)
        self.assertIsNone(row.recorded_by_id)
        self.assertIsNotNone(row.media_id)

    def test_only_the_removal_service_enters_the_removal_scope(self):
        root = Path(__file__).resolve().parents[2]
        users = []
        for path in root.rglob("*.py"):
            if any(part in ("venv", ".git", "node_modules") for part in path.parts) or "tests" in path.parts:
                continue
            if "removal_scope(" in path.read_text(errors="ignore"):
                users.append(str(path.relative_to(root)))
        self.assertEqual(sorted(users), ["library/services/voicetrack_media.py", "library/voicetrack_guard.py"])


class ScopeTests(IsolatedMediaRootMixin, TransactionTestCase):
    def test_both_scopes_require_a_transaction(self):
        self.assertFalse(connection.in_atomic_block)
        with self.assertRaises(Refused):
            with voicetrack_guard.binding_scope():
                pass
        with self.assertRaises(RemovalRefused):
            with voicetrack_guard.removal_scope():
                pass

    def test_a_scope_is_reset_when_its_body_raises(self):
        for scope, allowed in ((voicetrack_guard.binding_scope, voicetrack_guard.binding_allowed),
                               (voicetrack_guard.removal_scope, voicetrack_guard.removal_allowed)):
            with self.assertRaises(ZeroDivisionError):
                with transaction.atomic(), scope():
                    self.assertTrue(allowed())
                    1 / 0
            self.assertFalse(allowed())
