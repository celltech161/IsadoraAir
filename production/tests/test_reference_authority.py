"""ReferenceAudio authority: retained, pinned provenance that ordinary ORM
code can read but never fabricate, rewrite or delete -- and registration that
is serialized per key across real PostgreSQL sessions.
"""
import io
import os
import unittest
from unittest import mock

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from production.errors import ReferenceAudioIntegrityError
from production.models import ProductionMedia, ReferenceAudio, ReferenceAudioQuerySet, reference_registration
from production.services import intake, layout, reference_audio, retention

from . import sessions
from .support import IsolatedMediaRootMixin, ffmpeg_available

User = get_user_model()


def bed_bytes(seconds=1.0, frequency=1500):
    from spoken.tests.support import tone_wav
    return tone_wav([(seconds, frequency, 0.5)], channels=2)


def ingest_bed(seconds=1.0, frequency=1500):
    return intake.ingest_stream(io.BytesIO(bed_bytes(seconds, frequency)), kind=ProductionMedia.KIND_BED,
                                validate=True).media


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class ReferenceAudioOrmAuthorityTests(IsolatedMediaRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.operator = User.objects.create_user("operator")
        self.bed = ingest_bed()
        self.other_bed = ingest_bed(frequency=1200)
        self.reference = reference_audio.register("bed.x", self.bed, actor=self.operator, note="initial")
        self.snapshot = self.rows()

    def rows(self):
        return list(ReferenceAudio._base_manager.order_by("registered_at", "id").values())

    def assert_refused(self, attempt):
        with self.assertRaises(ReferenceAudioIntegrityError), transaction.atomic():
            attempt()
        self.assertEqual(self.rows(), self.snapshot)

    def managers(self):
        return {"objects": ReferenceAudio.objects, "_default_manager": ReferenceAudio._default_manager,
                "_base_manager": ReferenceAudio._base_manager, "media.reference_audio": self.bed.reference_audio,
                "user.registered_reference_audio": self.operator.registered_reference_audio}

    def test_the_guarded_queryset_is_the_default_base_and_reverse_manager(self):
        for name, manager in self.managers().items():
            with self.subTest(manager=name):
                self.assertIsInstance(manager.all(), ReferenceAudioQuerySet)
        self.assertEqual(ReferenceAudio._base_manager.get(pk=self.reference.pk).key, "bed.x")      # reads work

    def test_no_generic_creation(self):
        forged = dict(key="bed.forged", sha256=self.other_bed.sha256)
        for name, manager in self.managers().items():
            kw = {} if name == "media.reference_audio" else {"media": self.other_bed}
            forged_here = dict(key="bed.forged", sha256=self.bed.sha256) if name == "media.reference_audio" else forged
            attempts = {
                "create": lambda: manager.create(**kw, **forged_here),
                "get_or_create": lambda: manager.get_or_create(**kw, key="bed.forged", defaults=forged_here),
                "update_or_create": lambda: manager.update_or_create(**kw, key="bed.forged", defaults=forged_here),
                "bulk_create": lambda: manager.bulk_create([ReferenceAudio(media=self.other_bed, **forged)]),
                "bulk_create upsert": lambda: manager.bulk_create(
                    [ReferenceAudio(pk=self.reference.pk, media=self.other_bed, **forged)], update_conflicts=True,
                    unique_fields=["id"], update_fields=["key", "sha256", "media"]),
                "acreate": lambda: async_to_sync(manager.acreate)(**kw, **forged_here),
            }
            for label, attempt in attempts.items():
                with self.subTest(manager=name, api=label):
                    self.assert_refused(attempt)
        self.assert_refused(lambda: ReferenceAudio(media=self.other_bed, **forged).save())
        self.assert_refused(lambda: ReferenceAudio(media=self.other_bed, **forged).save(force_insert=True))

    def test_the_registration_scope_authorizes_only_the_exact_row(self):
        with reference_registration(key="bed.y", media_id=self.other_bed.pk, sha256=self.other_bed.sha256):
            for label, values in {
                "other key": dict(key="bed.z", media=self.other_bed, sha256=self.other_bed.sha256),
                "other media": dict(key="bed.y", media=self.bed, sha256=self.other_bed.sha256),
                "other pin": dict(key="bed.y", media=self.other_bed, sha256=self.bed.sha256),
                "born retired": dict(key="bed.y", media=self.other_bed, sha256=self.other_bed.sha256,
                                     retired_at=timezone.now()),
            }.items():
                with self.subTest(case=label):
                    self.assert_refused(lambda: ReferenceAudio.objects.create(**values))
            self.assert_refused(lambda: ReferenceAudio.objects.bulk_create(
                [ReferenceAudio(key="bed.y", media=self.other_bed, sha256=self.other_bed.sha256)]))

    def test_identity_pin_and_registration_evidence_are_immutable(self):
        # Codex's reproduction: rewrite the key and the pinned SHA-256.
        for name, manager in self.managers().items():
            attempts = {
                "update key+sha": lambda: manager.filter(pk=self.reference.pk).update(
                    key="bed.other", sha256=self.other_bed.sha256),
                "update media": lambda: manager.all().update(media=self.other_bed),
                "update registered_at": lambda: manager.all().update(registered_at=timezone.now()),
                "update registered_by": lambda: manager.all().update(registered_by=None,
                                                                     registered_by_username="x"),
                "update retire": lambda: manager.all().update(retired_at=timezone.now()),
                "update reassign actor": lambda: manager.all().update(registered_by=self.operator),
                "update_or_create": lambda: manager.update_or_create(pk=self.reference.pk,
                                                                     defaults={"sha256": "0" * 64}),
                "bulk_update": lambda: manager.bulk_update([self.reference], ["key"]),
                "aupdate": lambda: async_to_sync(manager.all().aupdate)(key="bed.other"),
            }
            for label, attempt in attempts.items():
                with self.subTest(manager=name, api=label):
                    self.assert_refused(attempt)
        loaded = ReferenceAudio.objects.get(pk=self.reference.pk)
        loaded.key, loaded.sha256 = "bed.other", "0" * 64
        self.assert_refused(loaded.save)
        loaded = ReferenceAudio.objects.get(pk=self.reference.pk)
        loaded.retired_at = timezone.now()
        self.assert_refused(lambda: loaded.save(update_fields=["retired_at"]))   # retirement is the service's
        self.assert_refused(lambda: self.other_bed.reference_audio.add(loaded))   # reverse add() rebinds media

    def test_history_is_never_deleted(self):
        for name, manager in self.managers().items():
            for label, attempt in {"all().delete": lambda: manager.all().delete(),
                                   "filter().delete": lambda: manager.filter(pk=self.reference.pk).delete(),
                                   "adelete": lambda: async_to_sync(manager.all().adelete)()}.items():
                with self.subTest(manager=name, api=label):
                    self.assert_refused(attempt)
        self.assert_refused(ReferenceAudio.objects.get(pk=self.reference.pk).delete)
        self.assertFalse(hasattr(ReferenceAudio.objects, "delete"))

    def test_user_deletion_nulls_the_actor_and_keeps_the_snapshot(self):
        reference_audio.retire("bed.x", actor=self.operator, reason="season over")
        self.operator.delete()
        row = ReferenceAudio.objects.get(pk=self.reference.pk)
        self.assertEqual((row.registered_by_id, row.registered_by_username, row.retired_by_id,
                          row.retired_by_username, row.key, row.sha256),
                         (None, "operator", None, "operator", "bed.x", self.bed.sha256))


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class ReferenceAudioServiceTests(IsolatedMediaRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.first, self.second = ingest_bed(), ingest_bed(frequency=1200)

    def test_retirement_is_the_one_mutation_and_touches_only_its_columns(self):
        row = reference_audio.register("bed.x", self.first, note="initial")
        identity = ReferenceAudio.objects.filter(pk=row.pk).values(
            "key", "media", "sha256", "label", "note", "registered_by", "registered_by_username",
            "registered_at").get()
        retired = reference_audio.retire("bed.x", reason="off air")
        self.assertEqual((retired.pk, retired.retire_reason), (row.pk, "off air"))
        self.assertIsNotNone(retired.retired_at)
        self.assertEqual(ReferenceAudio.objects.filter(pk=row.pk).values(*identity).get(), identity)
        retired_at = retired.retired_at
        self.assertIsNone(reference_audio.retire("bed.x", reason="again"))          # idempotent, writes nothing
        self.assertEqual(ReferenceAudio.objects.get(pk=row.pk).retired_at, retired_at)
        self.assertEqual(ReferenceAudio.objects.filter(key="bed.x").count(), 1)       # history readable

    def test_identical_reregistration_is_idempotent(self):
        row = reference_audio.register("bed.x", self.first)
        again = reference_audio.register("bed.x", self.first)
        self.assertEqual(again.pk, row.pk)
        self.assertEqual(ReferenceAudio.objects.filter(key="bed.x").count(), 1)

    def test_a_failed_insert_leaves_the_former_registration_current(self):
        former = reference_audio.register("bed.x", self.first)
        with mock.patch.object(type(ReferenceAudio.objects), "create", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                reference_audio.register("bed.x", self.second)
        current = ReferenceAudio.objects.get(key="bed.x", retired_at__isnull=True)
        self.assertEqual((current.pk, current.retire_reason), (former.pk, ""))

    def test_registration_verifies_the_stored_bytes_against_the_pin(self):
        path = layout.resolve_storage_path(self.first.storage_key)
        os.chmod(path, 0o640)
        data = bytearray(path.read_bytes())
        data[-1] ^= 0xFF
        path.write_bytes(bytes(data))
        with self.assertRaises(reference_audio.ReferenceAudioError) as raised:
            reference_audio.register("bed.x", self.first)
        self.assertEqual(raised.exception.code, "reference_hash_mismatch")
        self.assertFalse(ReferenceAudio.objects.exists())

    def test_the_key_lock_id_is_stable_and_not_pythons_hash(self):
        self.assertEqual(reference_audio.key_lock_id("bed.news_intro"), reference_audio.key_lock_id("bed.news_intro"))
        self.assertNotEqual(reference_audio.key_lock_id("bed.a"), reference_audio.key_lock_id("bed.b"))
        self.assertTrue(-2 ** 31 <= reference_audio.key_lock_id("bed.x") < 2 ** 31)
        import hashlib
        expected = int.from_bytes(hashlib.sha256(b"isadoraair.production.reference_audio\0bed.x").digest()[:4],
                                  "big", signed=True)
        self.assertEqual(reference_audio.key_lock_id("bed.x"), expected)

    def test_retired_registrations_keep_their_bed_retained(self):
        reference_audio.register("bed.x", self.first)
        reference_audio.register("bed.x", self.second)
        for media in (self.first, self.second):
            with self.subTest(media=str(media.pk)), self.assertRaises(Exception):
                retention.purge_media(media)
            media.refresh_from_db()
            self.assertTrue(media.is_present)


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class ReferenceAudioRaceTests(IsolatedMediaRootMixin, TransactionTestCase):
    """Same-key registration across two real sessions. Session A is paused at
    the most adversarial point -- it has read the current registration and is
    about to INSERT (a wrapper around the registration scope, entered right
    before the insert); session B must wait for A -- on the key's advisory
    lock -- and then act on A's committed result, never on its stale view."""

    def setUp(self):
        super().setUp()
        self.beds = [ingest_bed(frequency=f) for f in (1500, 1200, 900)]

    def race(self, first, second):
        pause = sessions.Seam(reference_audio.reference_registration, "first", before=True)
        with mock.patch.object(reference_audio, "reference_registration", pause):
            a, a_box = sessions.run_thread("first", first)
            self.assertTrue(pause.reached.wait(sessions.WAIT))
            b, b_box = sessions.run_thread("second", second)
            observed = sessions.blocked_or_done(b)
            pause.release.set()
            a.join(sessions.WAIT)
            b.join(sessions.WAIT)
        return observed, a_box, b_box

    def assert_history_sound(self, key):
        rows = list(ReferenceAudio.objects.filter(key=key).order_by("registered_at"))
        self.assertLessEqual(sum(1 for row in rows if row.retired_at is None), 1)
        for row in rows:
            self.assertEqual(row.sha256, ProductionMedia.objects.get(pk=row.media_id).sha256)
        return rows

    def test_first_registration_versus_first_registration(self):
        observed, a, b = self.race(lambda: reference_audio.register("bed.k", self.beds[0]),
                                   lambda: reference_audio.register("bed.k", self.beds[1]))
        self.assertEqual((a["error"], b["error"]), (None, None))       # no IntegrityError as race behaviour
        self.assertEqual(observed, "blocked")
        rows = self.assert_history_sound("bed.k")
        self.assertEqual([(row.media_id, row.retired_at is None) for row in rows],
                         [(self.beds[0].pk, False), (self.beds[1].pk, True)])

    def test_replacement_versus_replacement(self):
        original = reference_audio.register("bed.k", self.beds[2])
        observed, a, b = self.race(lambda: reference_audio.register("bed.k", self.beds[0]),
                                   lambda: reference_audio.register("bed.k", self.beds[1]))
        self.assertEqual((a["error"], b["error"]), (None, None))
        self.assertEqual(observed, "blocked")
        rows = self.assert_history_sound("bed.k")
        self.assertEqual([row.pk for row in rows][0], original.pk)                   # nothing lost
        self.assertEqual([(row.media_id, row.retired_at is None) for row in rows],
                         [(self.beds[2].pk, False), (self.beds[0].pk, False), (self.beds[1].pk, True)])

    def test_replacement_versus_retirement(self):
        reference_audio.register("bed.k", self.beds[2])
        observed, a, b = self.race(lambda: reference_audio.register("bed.k", self.beds[0]),
                                   lambda: reference_audio.retire("bed.k", reason="off"))
        self.assertEqual((a["error"], b["error"]), (None, None))
        self.assertEqual(observed, "blocked")
        rows = self.assert_history_sound("bed.k")
        self.assertEqual(len(rows), 2)
        self.assertEqual(b["result"].media_id, self.beds[0].pk)       # retired A's committed registration
        self.assertFalse(ReferenceAudio.objects.filter(key="bed.k", retired_at__isnull=True).exists())

    def test_identical_registration_versus_itself(self):
        observed, a, b = self.race(lambda: reference_audio.register("bed.k", self.beds[0]),
                                   lambda: reference_audio.register("bed.k", self.beds[0]))
        self.assertEqual((a["error"], b["error"]), (None, None))
        self.assertEqual(observed, "blocked")
        rows = self.assert_history_sound("bed.k")
        self.assertEqual(len(rows), 1)
        self.assertEqual(a["result"].pk, b["result"].pk)

    def test_different_keys_do_not_wait_for_each_other(self):
        observed, a, b = self.race(lambda: reference_audio.register("bed.k", self.beds[0]),
                                   lambda: reference_audio.register("bed.other", self.beds[1]))
        self.assertEqual((a["error"], b["error"], observed), (None, None, "done"))

    def test_a_rolled_back_registration_releases_the_key(self):
        reference_audio.register("bed.k", self.beds[2])
        pause = sessions.Seam(reference_audio.reference_registration, "first", before=True, abort=True)
        with mock.patch.object(reference_audio, "reference_registration", pause):
            a, a_box = sessions.run_thread("first", lambda: reference_audio.register("bed.k", self.beds[0]))
            self.assertTrue(pause.reached.wait(sessions.WAIT))
            b, b_box = sessions.run_thread("second", lambda: reference_audio.register("bed.k", self.beds[1]))
            self.assertEqual(sessions.blocked_or_done(b), "blocked")
            pause.release.set()
            a.join(sessions.WAIT)
            b.join(sessions.WAIT)
        self.assertIsInstance(a_box["error"], sessions.Abort)
        self.assertIsNone(b_box["error"])
        rows = self.assert_history_sound("bed.k")
        self.assertEqual([(row.media_id, row.retired_at is None) for row in rows],
                         [(self.beds[2].pk, False), (self.beds[1].pk, True)])


class ReferenceAudioUniqueConstraintTests(IsolatedMediaRootMixin, TestCase):
    def test_the_partial_unique_constraint_still_backs_the_lock(self):
        from .support import VALID_FACTS, make_row
        bed = make_row(kind=ProductionMedia.KIND_BED, validated_at=timezone.now(), **VALID_FACTS)
        with reference_registration(key="bed.x", media_id=bed.pk, sha256=bed.sha256):
            ReferenceAudio.objects.create(key="bed.x", media=bed, sha256=bed.sha256)
            with self.assertRaises(IntegrityError), transaction.atomic():
                ReferenceAudio.objects.create(key="bed.x", media=bed, sha256=bed.sha256)
        self.assertEqual(ReferenceAudio.objects.count(), 1)
