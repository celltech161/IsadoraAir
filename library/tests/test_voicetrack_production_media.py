"""2.22B -- evergreen VoiceTrack adopts ProductionMedia (B8-B13, B17, B22).

* schema: additive nullable PROTECT FK, same database, no GenericForeignKey;
* the binding guard: VoiceTrack.media changes ONLY through the binding service;
* the resolver: a valid bound ProductionMedia first, else the legacy file;
* the binding service: canonical lock, optimistic revision, immutable takes,
  identity preserved, delete semantics, audit;
* real PostgreSQL sessions: binding vs purge both ways, two editors, rollback,
  database error mid-rebind, and the engine seeing a new take only after commit;
* engine equivalence: legacy and ProductionMedia-backed VoiceTracks drive the
  existing VT path identically.
"""
import io
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext

from library import voicetrack_guard
from library.models import Artist, Category, CategoryKind, Track, VoiceTrack
from library.services import voicetrack_media as vtm
from production.errors import PurgeRefused
from production.models import ProductionMedia
from production.services import intake, layout, reconcile, retention
from production.tests.support import IsolatedMediaRootMixin, fixture

User = get_user_model()


def make_track(title="VT Track", *, intro=8.0, outro=170.0):
    artist, _ = Artist.objects.get_or_create(name="VT Artist")
    kind, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
    category, _ = Category.objects.get_or_create(code="VTMUSIC", defaults={"name": "VT Music", "kind": kind})
    return Track.objects.create(
        filepath=f"/nonexistent/{title}.mp3", filename=f"{title}.mp3", title=title, artist=artist,
        category=category, intro_until_seconds=intro, outro_starts_seconds=outro,
    )


def ingest_take(user=None, name="wav16_mono.wav", parent=None):
    source = io.BytesIO(fixture(name))
    if parent is not None:
        return intake.ingest_derivative(parent, source, owner=user, policy=vtm.VOICETRACK_POLICY,
                                        recipe_key="iportal.editor", recipe_version=1,
                                        recipe_params={"ops": ["trim"]}).media
    return intake.ingest_stream(source, kind=ProductionMedia.KIND_RECORDING, owner=user,
                                policy=vtm.VOICETRACK_POLICY).media


def legacy_file(directory, data=b"RIFF....WAVEfmt "):
    path = Path(directory) / "legacy.wav"
    path.write_bytes(data)
    return str(path)


def make_legacy_vt(track, position="intro", path=None, duration=3.5):
    """A pre-2.22B row exactly as the legacy endpoints left it."""
    vt = VoiceTrack(track=track, position=position, filepath=path or "/nonexistent/legacy.wav", source="browser")
    vt.save()
    VoiceTrack.objects.filter(pk=vt.pk).update(duration_seconds=duration)
    return VoiceTrack.objects.get(pk=vt.pk)


class SchemaTests(TestCase):
    def test_media_is_an_additive_nullable_protected_foreign_key(self):
        field = VoiceTrack._meta.get_field("media")
        self.assertTrue(field.null)
        self.assertIs(field.related_model, ProductionMedia)
        self.assertEqual(field.remote_field.on_delete.__name__, "PROTECT")

    def test_evergreen_identity_is_unchanged(self):
        self.assertEqual([tuple(item) for item in VoiceTrack._meta.unique_together], [("track", "position")])

    def test_no_generic_foreign_key_anywhere_in_the_domain(self):
        from django.contrib.contenttypes.fields import GenericForeignKey
        for model in (VoiceTrack, ProductionMedia):
            self.assertFalse([f for f in model._meta.get_fields() if isinstance(f, GenericForeignKey)], model)

    def test_voicetrack_and_production_media_share_one_database(self):
        from django.db import router
        self.assertEqual(router.db_for_write(VoiceTrack), router.db_for_write(ProductionMedia))
        self.assertTrue(router.allow_relation(VoiceTrack(), ProductionMedia()) is not False)

    def test_the_media_relation_participates_in_purge_reference_discovery(self):
        labels = {(rel.related_model._meta.label, rel.field.name) for rel in retention._reverse_relations()}
        self.assertIn(("library.VoiceTrack", "media"), labels)


class BindingGuardTests(IsolatedMediaRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.track = make_track()
        self.media = ingest_take()
        self.vt = make_legacy_vt(self.track)

    def test_every_naked_binding_path_is_refused(self):
        attempts = {
            "instance save": lambda: (setattr(self.vt, "media", self.media), self.vt.save()),
            "create": lambda: VoiceTrack.objects.create(track=self.track, position="outro", filepath="",
                                                       media=self.media),
            "queryset update": lambda: VoiceTrack.objects.filter(pk=self.vt.pk).update(media=self.media),
            "queryset update by id": lambda: VoiceTrack.objects.filter(pk=self.vt.pk).update(media_id=self.media.pk),
            "bulk_update": lambda: VoiceTrack.objects.bulk_update(
                [setattr(self.vt, "media_id", self.media.pk) or self.vt], ["media"]),
            "bulk_create": lambda: VoiceTrack.objects.bulk_create(
                [VoiceTrack(track=self.track, position="outro", filepath="", media=self.media)]),
            "update_or_create": lambda: VoiceTrack.objects.update_or_create(
                track=self.track, position="intro", defaults={"media": self.media}),
            "reverse manager update": lambda: self.media.evergreen_voicetracks.update(media=None),
        }
        for label, attempt in attempts.items():
            with self.subTest(label), self.assertRaises(voicetrack_guard.UnguardedVoiceTrackBinding):
                with transaction.atomic():
                    attempt()
        self.vt.refresh_from_db()
        self.assertIsNone(self.vt.media_id)

    def test_unrelated_edits_still_work(self):
        self.vt.gain_db = -2.0
        self.vt.save()
        VoiceTrack.objects.filter(pk=self.vt.pk).update(gain_db=1.5)
        self.vt.refresh_from_db()
        self.assertEqual(self.vt.gain_db, 1.5)

    def test_only_the_binding_service_enters_the_scope(self):
        root = Path(__file__).resolve().parents[2]
        users = []
        for path in root.rglob("*.py"):
            if any(part in ("venv", ".git", "node_modules") for part in path.parts) or "tests" in path.parts:
                continue
            if "binding_scope(" in path.read_text(errors="ignore"):
                users.append(str(path.relative_to(root)))
        self.assertEqual(sorted(users), ["library/services/voicetrack_media.py", "library/voicetrack_guard.py"])

    def test_admin_cannot_edit_the_binding(self):
        from django.contrib import admin
        self.assertIn("media", admin.site._registry[VoiceTrack].get_readonly_fields(None))


class ResolverTests(IsolatedMediaRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.track = make_track()

    def test_a_legacy_file_resolves_exactly_as_before(self):
        path = legacy_file(self.tmp.name)
        vt = make_legacy_vt(self.track, path=path)
        audio = vt.playable_audio()
        self.assertEqual((audio.path, audio.duration_seconds, audio.origin), (path, 3.5, "legacy"))
        self.assertTrue(vt.file_exists)

    def test_a_missing_legacy_file_resolves_to_nothing(self):
        vt = make_legacy_vt(self.track)
        self.assertIsNone(vt.playable_audio())
        self.assertFalse(vt.file_exists)

    def test_a_bound_take_resolves_to_its_immutable_file_and_validated_duration(self):
        media = ingest_take()
        vt = vtm.bind_media(track_id=self.track.pk, position="intro", media_id=media.pk, user=None,
                            expected_revision=vtm.ABSENT)
        audio = VoiceTrack.objects.get(pk=vt.pk).playable_audio()
        self.assertEqual(audio.origin, "production_media")
        self.assertEqual(audio.path, str(layout.resolve_storage_path(media.storage_key)))
        self.assertAlmostEqual(audio.duration_seconds, float(media.decoded_duration_seconds), places=3)

    def test_a_bound_take_wins_over_the_legacy_file_and_degrades_to_it(self):
        path = legacy_file(self.tmp.name)
        vt = make_legacy_vt(self.track, path=path)
        media = ingest_take()
        vtm.bind_media(track_id=self.track.pk, position="intro", media_id=media.pk, user=None,
                       expected_revision=vtm.revision_of(vt))
        vt = VoiceTrack.objects.get(pk=vt.pk)
        self.assertEqual(vt.playable_audio().origin, "production_media")
        # The store becomes unreachable (misconfiguration): never raises, legacy still airs.
        with self.settings(PRODUCTION_MEDIA_ROOT="/"):
            self.assertEqual(VoiceTrack.objects.get(pk=vt.pk).playable_audio().origin, "legacy")


class BindingServiceTests(IsolatedMediaRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user("host", password="x")
        self.track = make_track()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def bind(self, media, revision, position="intro"):
        return vtm.bind_media(track_id=self.track.pk, position=position, media_id=media.pk, user=self.user,
                              expected_revision=revision)

    def test_record_validate_save_bind_creates_one_identity(self):
        media = ingest_take(self.user)
        vt = self.bind(media, vtm.ABSENT)
        self.assertEqual((vt.track_id, vt.position, vt.media_id, vt.filepath), (self.track.pk, "intro", media.pk, ""))
        self.assertAlmostEqual(vt.duration_seconds, float(media.decoded_duration_seconds), places=3)
        self.assertEqual(VoiceTrack.objects.filter(track=self.track).count(), 1)

    def test_rerecord_keeps_the_row_and_leaves_the_old_take_immutable(self):
        first = ingest_take(self.user)
        vt = self.bind(first, vtm.ABSENT)
        first_bytes = layout.resolve_storage_path(first.storage_key).read_bytes()
        second = ingest_take(self.user, name="flac.flac")
        vt2 = self.bind(second, vtm.revision_of(vt))
        self.assertEqual(vt2.pk, vt.pk)                                   # identity preserved
        self.assertNotEqual(first.pk, second.pk)                          # a distinct immutable take
        first.refresh_from_db()
        self.assertTrue(first.is_present)                                 # old take not removed
        self.assertEqual(layout.resolve_storage_path(first.storage_key).read_bytes(), first_bytes)
        self.assertEqual(retention.find_references(first), [])           # ...just unreferenced
        self.assertEqual(VoiceTrack.objects.filter(track=self.track).count(), 1)

    def test_an_edit_is_a_derivative_with_provenance_and_the_original_is_unchanged(self):
        original = ingest_take(self.user)
        vt = self.bind(original, vtm.ABSENT)
        edit = ingest_take(self.user, parent=original)
        self.assertEqual((edit.kind, edit.derived_from_id), (ProductionMedia.KIND_EDIT, original.pk))
        vt = self.bind(edit, vtm.revision_of(vt))
        self.assertEqual(vt.media_id, edit.pk)
        original.refresh_from_db()
        self.assertTrue(original.is_present and original.is_valid)

    def test_a_legacy_voicetrack_converts_without_changing_identity(self):
        path = legacy_file(self.tmp.name)
        vt = make_legacy_vt(self.track, path=path)
        media = ingest_take(self.user)
        converted = self.bind(media, vtm.revision_of(vt))
        self.assertEqual((converted.pk, converted.track_id, converted.position), (vt.pk, self.track.pk, "intro"))
        self.assertEqual(converted.media_id, media.pk)
        self.assertEqual(converted.filepath, path)            # kept as evidence ...
        self.assertTrue(Path(path).is_file())                 # ... and never deleted by a rebind
        self.assertEqual(converted.playable_audio().origin, "production_media")

    def test_a_stale_revision_is_refused_and_the_newer_binding_preserved(self):
        first = ingest_take(self.user)
        vt = self.bind(first, vtm.ABSENT)
        stale = vtm.revision_of(vt)
        newer = ingest_take(self.user, name="flac.flac")
        self.bind(newer, stale)
        late = ingest_take(self.user, name="opus.ogg")
        with self.assertRaises(vtm.VoiceTrackConflict) as ctx:
            self.bind(late, stale)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(VoiceTrack.objects.get(pk=vt.pk).media_id, newer.pk)
        late.refresh_from_db()
        self.assertTrue(late.is_present)                      # the late take survives, unreferenced/reclaimable
        self.assertEqual(retention.find_references(late), [])

    def test_ineligible_tracks_and_unbindable_media_are_refused(self):
        bare = make_track("No markers", intro=None, outro=None)
        media = ingest_take(self.user)
        with self.assertRaises(vtm.VoiceTrackIneligible):
            vtm.bind_media(track_id=bare.pk, position="intro", media_id=media.pk, user=self.user,
                           expected_revision=vtm.ABSENT)
        for bad in ("not-a-uuid", "00000000-0000-0000-0000-000000000000"):
            with self.subTest(media=bad), self.assertRaises(vtm.VoiceTrackMediaRefused):
                vtm.bind_media(track_id=self.track.pk, position="intro", media_id=bad, user=self.user,
                               expected_revision=vtm.ABSENT)
        self.assertFalse(VoiceTrack.objects.exists())

    def test_failed_or_pending_validation_never_changes_the_airable_binding(self):
        current_media = ingest_take(self.user)
        vt = self.bind(current_media, vtm.ABSENT)
        unvalidated = intake.ingest_stream(io.BytesIO(fixture("wav16_mono.wav")), kind=ProductionMedia.KIND_RECORDING,
                                           owner=self.user, validate=False).media
        with self.assertRaises(vtm.VoiceTrackMediaRefused):
            self.bind(unvalidated, vtm.revision_of(vt))
        self.assertEqual(VoiceTrack.objects.get(pk=vt.pk).media_id, current_media.pk)

    def test_a_take_outside_the_voicetrack_policy_is_refused(self):
        media = ingest_take(self.user)
        with mock.patch.object(vtm, "policy_error", return_value="too_long"):
            with self.assertRaises(vtm.VoiceTrackMediaRefused):
                self.bind(media, vtm.ABSENT)
        self.assertFalse(VoiceTrack.objects.exists())

    def test_delete_removes_the_row_but_never_the_shared_media_bytes(self):
        media = ingest_take(self.user)
        vt = self.bind(media, vtm.ABSENT)
        path = layout.resolve_storage_path(media.storage_key)
        with mock.patch("os.unlink") as unlink, self.captureOnCommitCallbacks(execute=True):
            vtm.remove_voicetrack(track_id=self.track.pk, position="intro", user=self.user,
                                  expected_revision=vtm.revision_of(vt))
        self.assertFalse(VoiceTrack.objects.filter(pk=vt.pk).exists())
        self.assertFalse(any(str(path) in str(call) for call in unlink.call_args_list))
        self.assertTrue(path.is_file())
        media.refresh_from_db()
        self.assertTrue(media.is_present)
        self.assertEqual(retention.find_references(media), [])          # now reclaimable by retention

    def test_delete_of_a_legacy_voicetrack_keeps_the_legacy_semantics(self):
        legacy_dir = Path(self.tmp.name) / "voicetracks" / str(self.track.pk)
        legacy_dir.mkdir(parents=True)
        path = legacy_dir / "intro.wav"
        path.write_bytes(b"legacy")
        vt = make_legacy_vt(self.track, path=str(path))
        with mock.patch.object(vtm, "LEGACY_VOICETRACK_DIR", Path(self.tmp.name) / "voicetracks"), \
                self.captureOnCommitCallbacks(execute=True):
            vtm.remove_voicetrack(track_id=self.track.pk, position="intro", user=self.user,
                                  expected_revision=vtm.revision_of(vt))
        self.assertFalse(path.exists())

    def test_a_legacy_path_outside_the_voicetrack_directory_is_never_unlinked(self):
        outside = legacy_file(self.tmp.name)
        vt = make_legacy_vt(self.track, path=outside)
        with self.captureOnCommitCallbacks(execute=True):
            vtm.remove_voicetrack(track_id=self.track.pk, position="intro", user=self.user,
                                  expected_revision=vtm.revision_of(vt))
        self.assertTrue(Path(outside).exists())

    def test_a_stale_delete_is_refused(self):
        media = ingest_take(self.user)
        vt = self.bind(media, vtm.ABSENT)
        with self.assertRaises(vtm.VoiceTrackConflict):
            vtm.remove_voicetrack(track_id=self.track.pk, position="intro", user=self.user,
                                  expected_revision=vtm.ABSENT)
        self.assertTrue(VoiceTrack.objects.filter(pk=vt.pk).exists())

    def test_material_actions_are_audited_once_on_commit(self):
        from monitoring.models import SystemEvent
        media = ingest_take(self.user)
        with self.captureOnCommitCallbacks(execute=True):
            vt = self.bind(media, vtm.ABSENT)
        with self.captureOnCommitCallbacks(execute=True):
            vtm.remove_voicetrack(track_id=self.track.pk, position="intro", user=self.user,
                                  expected_revision=vtm.revision_of(vt))
        events = list(SystemEvent.objects.filter(category="voicetrack").order_by("created_at"))
        self.assertEqual([event.detail["action"] for event in events], ["recorded", "deleted"])
        self.assertEqual(events[0].detail["media_id"], str(media.pk))


class BoundedBindingTransactionTests(IsolatedMediaRootMixin, TestCase):
    """B9: the locked transaction does row work only -- nothing expensive."""

    def setUp(self):
        super().setUp()
        self.track = make_track()
        self.media = ingest_take()

    def test_no_expensive_work_happens_inside_the_binding(self):
        forbidden = AssertionError("expensive work inside the binding transaction")
        with mock.patch("production.services.validation._analyze_path", side_effect=forbidden), \
                mock.patch("production.services.intake.ingest_stream", side_effect=forbidden), \
                mock.patch("production.services.confinement.run_confined", side_effect=forbidden), \
                mock.patch("subprocess.Popen", side_effect=forbidden), \
                CaptureQueriesContext(connection) as queries:
            vtm.bind_media(track_id=self.track.pk, position="intro", media_id=self.media.pk, user=None,
                           expected_revision=vtm.ABSENT)
        self.assertLessEqual(len(queries), 12, [q["sql"] for q in queries])

    def test_the_canonical_lock_is_taken_first_inside_the_writing_transaction(self):
        with CaptureQueriesContext(connection) as queries:
            vtm.bind_media(track_id=self.track.pk, position="intro", media_id=self.media.pk, user=None,
                           expected_revision=vtm.ABSENT)
        sql = [q["sql"] for q in queries]
        lock = next(i for i, s in enumerate(sql) if "production_productionmedia" in s and "FOR UPDATE" in s)
        write = next(i for i, s in enumerate(sql) if s.startswith("INSERT INTO \"library_voicetrack\""))
        self.assertLess(lock, write)
        self.assertFalse(any("library_voicetrack" in s and ("INSERT" in s or "UPDATE" in s) for s in sql[:lock]))


# ---------------------------------------------------------------------------
# Real, isolated PostgreSQL sessions.
# ---------------------------------------------------------------------------

class _Pause:
    """Make one call block inside its transaction until released."""

    def __init__(self):
        self.reached = threading.Event()
        self.release = threading.Event()

    def wrap(self, real):
        def wrapper(*args, **kwargs):
            result = real(*args, **kwargs)
            self.reached.set()
            assert self.release.wait(20), "test never released the paused transaction"
            return result
        return wrapper


def _in_thread(target, results, key):
    def run():
        try:
            results[key] = target()
        except Exception as exc:  # noqa: BLE001 -- reported to the test
            results[key] = exc
        finally:
            connections.close_all()
    thread = threading.Thread(target=run)
    thread.start()
    return thread


class RealSessionRaceTests(IsolatedMediaRootMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.track = make_track()

    def assert_invariants(self):
        for vt in VoiceTrack.objects.exclude(media=None):
            media = ProductionMedia.objects.get(pk=vt.media_id)            # no dangling FK
            self.assertTrue(media.is_present, "a purged media is bound")
        self.assertEqual(reconcile.find_purged_media_still_referenced(), [])
        self.assertLessEqual(VoiceTrack.objects.filter(track=self.track, position="intro").count(), 1)

    def test_the_binding_scope_requires_a_transaction(self):
        with self.assertRaises(voicetrack_guard.UnguardedVoiceTrackBinding):
            with voicetrack_guard.binding_scope():
                pass

    def test_binder_wins_purge_waits_then_refuses(self):
        media = ingest_take()
        pause, results = _Pause(), {}
        with mock.patch.object(vtm, "revision_of", side_effect=pause.wrap(vtm.revision_of)):
            binder = _in_thread(lambda: vtm.bind_media(track_id=self.track.pk, position="intro",
                                                       media_id=media.pk, user=None,
                                                       expected_revision=vtm.ABSENT), results, "bind")
            self.assertTrue(pause.reached.wait(20))
            purger = _in_thread(lambda: retention.purge_media(media.pk), results, "purge")
            time.sleep(0.5)
            self.assertTrue(purger.is_alive(), "purge must wait for the binding lock")
            pause.release.set()
            binder.join(20)
            purger.join(20)
        self.assertIsInstance(results["bind"], VoiceTrack)
        self.assertIsInstance(results["purge"], PurgeRefused)
        self.assert_invariants()

    def test_purge_wins_binding_waits_then_refuses(self):
        media = ingest_take()
        pause, results = _Pause(), {}
        with mock.patch.object(retention, "find_references", side_effect=pause.wrap(retention.find_references)):
            purger = _in_thread(lambda: retention.purge_media(media.pk), results, "purge")
            self.assertTrue(pause.reached.wait(20))
            binder = _in_thread(lambda: vtm.bind_media(track_id=self.track.pk, position="intro",
                                                       media_id=media.pk, user=None,
                                                       expected_revision=vtm.ABSENT), results, "bind")
            time.sleep(0.5)
            self.assertTrue(binder.is_alive(), "binding must wait for the purge's lock")
            pause.release.set()
            purger.join(20)
            binder.join(20)
        self.assertIsInstance(results["bind"], vtm.VoiceTrackMediaRefused)
        self.assertEqual(results["bind"].code, "media_purged")
        self.assertFalse(VoiceTrack.objects.exists())
        self.assert_invariants()

    def test_two_editors_one_wins_the_other_gets_a_conflict(self):
        a, b = ingest_take(), ingest_take(name="flac.flac")
        pause, results = _Pause(), {}
        real_revision = vtm.revision_of
        calls = {"n": 0}

        def first_pauses(vt):
            calls["n"] += 1
            if calls["n"] == 1:
                return pause.wrap(real_revision)(vt)
            return real_revision(vt)

        with mock.patch.object(vtm, "revision_of", side_effect=first_pauses):
            first = _in_thread(lambda: vtm.bind_media(track_id=self.track.pk, position="intro", media_id=a.pk,
                                                      user=None, expected_revision=vtm.ABSENT), results, "a")
            self.assertTrue(pause.reached.wait(20))
            second = _in_thread(lambda: vtm.bind_media(track_id=self.track.pk, position="intro", media_id=b.pk,
                                                       user=None, expected_revision=vtm.ABSENT), results, "b")
            time.sleep(0.5)
            self.assertTrue(second.is_alive(), "the second editor must wait on the track/row lock")
            pause.release.set()
            first.join(20)
            second.join(20)
        self.assertIsInstance(results["a"], VoiceTrack)
        self.assertIsInstance(results["b"], vtm.VoiceTrackConflict)
        self.assertEqual(VoiceTrack.objects.get(track=self.track, position="intro").media_id, a.pk)
        self.assertEqual(retention.find_references(b), [])                  # loser's take: reclaimable
        self.assert_invariants()

    def test_rollback_after_media_created_but_before_binding(self):
        media = ingest_take()
        with mock.patch.object(VoiceTrack, "save", side_effect=RuntimeError("crash after the lock")):
            with self.assertRaises(RuntimeError):
                vtm.bind_media(track_id=self.track.pk, position="intro", media_id=media.pk, user=None,
                               expected_revision=vtm.ABSENT)
        self.assertFalse(VoiceTrack.objects.exists())
        self.assertFalse(voicetrack_guard.binding_allowed())
        retention.purge_media(media.pk)                     # unreferenced: safely reclaimable
        self.assert_invariants()

    def test_a_database_error_during_rebind_keeps_the_old_binding(self):
        old = ingest_take()
        vt = vtm.bind_media(track_id=self.track.pk, position="intro", media_id=old.pk, user=None,
                            expected_revision=vtm.ABSENT)
        new = ingest_take(name="flac.flac")
        with mock.patch.object(VoiceTrack, "save", side_effect=DatabaseError("connection reset mid-rebind")):
            with self.assertRaises(DatabaseError):
                vtm.bind_media(track_id=self.track.pk, position="intro", media_id=new.pk, user=None,
                               expected_revision=vtm.revision_of(vt))
        self.assertEqual(VoiceTrack.objects.get(pk=vt.pk).media_id, old.pk)
        self.assertFalse(voicetrack_guard.binding_allowed())
        self.assert_invariants()

    def test_the_engine_sees_a_new_take_only_after_commit(self):
        old = ingest_take()
        vt = vtm.bind_media(track_id=self.track.pk, position="intro", media_id=old.pk, user=None,
                            expected_revision=vtm.ABSENT)
        old_path = str(layout.resolve_storage_path(old.storage_key))
        new = ingest_take(name="flac.flac")
        new_path = str(layout.resolve_storage_path(new.storage_key))
        pause, results = _Pause(), {}

        def engine_lookup():
            row = VoiceTrack.objects.filter(track=self.track, position="intro").select_related("media").first()
            return row.playable_audio().path

        real_audit = vtm._audit
        with mock.patch.object(vtm, "_audit", side_effect=lambda *a, **k: real_audit(*a, **k)), \
                mock.patch.object(VoiceTrack, "save", side_effect=pause.wrap(VoiceTrack.save), autospec=True):
            binder = _in_thread(lambda: vtm.bind_media(track_id=self.track.pk, position="intro", media_id=new.pk,
                                                       user=None, expected_revision=vtm.revision_of(vt)),
                                results, "bind")
            self.assertTrue(pause.reached.wait(20))
            during = engine_lookup()                         # written but NOT committed
            pause.release.set()
            binder.join(20)
        self.assertEqual(during, old_path)
        self.assertEqual(engine_lookup(), new_path)


# ---------------------------------------------------------------------------
# Engine equivalence (B12): only media resolution changed.
# ---------------------------------------------------------------------------

class EngineEquivalenceTests(IsolatedMediaRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        # The engine refreshes its DB connection per transition; inside a test
        # transaction that would close the test's own connection.
        patcher = mock.patch("library.services.engine.close_old_connections", lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.outgoing = make_track("Outgoing")
        self.incoming = make_track("Incoming", intro=1.0)

    def _engine(self):
        from library.services import engine as eng
        stand = object.__new__(eng.PlaybackEngine)
        stand._vt_lock = threading.Lock()
        stand._vt = {}
        stand._next_triggered = False
        stand.fired = []
        stand._vt_fire_file = lambda path, gain, kind: stand.fired.append((path, gain, kind)) or 1
        stand._vt_ramp_duck_to_db = lambda *a: None
        item = mock.Mock(track=self.incoming, id=999999)
        stand._peek_playable_at_cursor = lambda: item
        return stand

    def _enter(self):
        stand = self._engine()
        entered = stand._vt_maybe_enter(mock.Mock(track=self.outgoing))
        return stand, entered

    def _intro_delay(self, stand):
        from library.services import engine as eng
        delays = []
        with mock.patch.object(eng.GLib, "timeout_add", side_effect=lambda ms, fn: delays.append(ms)):
            stand._vt_start_intro_phase()
        return delays[-1]

    def _shape(self, stand):
        return {key: (value if not isinstance(value, str) or "/" not in value else "PATH")
                for key, value in stand._vt.items() if key != "incoming_track"}

    def test_legacy_and_production_media_voicetracks_drive_the_vt_path_identically(self):
        legacy_dir = Path(self.tmp.name)
        out_path = legacy_dir / "out.wav"
        in_path = legacy_dir / "in.wav"
        for path in (out_path, in_path):
            path.write_bytes(fixture("wav16_mono.wav"))
        out_legacy = make_legacy_vt(self.outgoing, "outro", str(out_path), duration=4.0)
        in_legacy = make_legacy_vt(self.incoming, "intro", str(in_path), duration=4.0)
        VoiceTrack.objects.filter(pk__in=[out_legacy.pk, in_legacy.pk]).update(gain_db=-1.5)
        legacy_stand, legacy_entered = self._enter()
        legacy_shape = self._shape(legacy_stand)
        legacy_fired = list(legacy_stand.fired)
        legacy_delay = self._intro_delay(legacy_stand)

        # Same rows, same gains -- now bound to validated ProductionMedia takes.
        for vt in (out_legacy, in_legacy):
            take = ingest_take()
            vtm.bind_media(track_id=vt.track_id, position=vt.position, media_id=take.pk, user=None,
                           expected_revision=vtm.revision_of(VoiceTrack.objects.get(pk=vt.pk)))
        pm_stand, pm_entered = self._enter()
        pm_shape = self._shape(pm_stand)
        pm_fired = list(pm_stand.fired)
        pm_delay = self._intro_delay(pm_stand)

        self.assertTrue(legacy_entered and pm_entered)
        self.assertAlmostEqual(pm_shape.pop("incoming_vt_duration"), legacy_shape.pop("incoming_vt_duration"),
                               places=2)                                    # validated decode vs mutagen
        self.assertEqual(pm_shape, legacy_shape)                            # same state machine values
        self.assertEqual([(g, k) for _p, g, k in pm_fired], [(g, k) for _p, g, k in legacy_fired])
        self.assertEqual(pm_delay, legacy_delay)                           # same intro_until fit (4.0 s > 1.0 s)
        bound = VoiceTrack.objects.get(pk=out_legacy.pk).media
        self.assertEqual(pm_fired[0][0], str(layout.resolve_storage_path(bound.storage_key)))
        self.assertEqual(legacy_fired[0][0], str(out_path))

    def test_no_voicetrack_still_falls_through_to_the_normal_crossfade(self):
        stand, entered = self._enter()
        self.assertFalse(entered)
        self.assertEqual(stand.fired, [])

    def test_a_voicetrack_with_nothing_playable_behaves_as_before(self):
        make_legacy_vt(self.outgoing, "outro")             # legacy row whose file is gone
        stand, entered = self._enter()
        self.assertTrue(entered)
        self.assertEqual(stand.fired, [("/nonexistent/legacy.wav", 0.0, "outro")])   # fire skipped downstream
