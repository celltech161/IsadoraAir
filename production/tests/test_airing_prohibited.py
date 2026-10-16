"""r0113 / D3C: immutable NO-AIR provenance on ProductionMedia.

* intake stamps ``airing_prohibited`` when asked, and a derivative is
  prohibited whenever its parent is -- through any number of edits, and no
  caller can ask for less;
* the column is immutable like every other one, and no insert path (save,
  create, bulk_create, the base manager) can create a child that drops its
  parent's restriction;
* the canonical air-capable binding refuses prohibited media; the two closed
  exceptions (the NO-AIR binding, the derivation parent lock) do exactly
  their one job;
* reference audio (beds, stingers) refuses prohibited media.
"""
import io
import uuid

from django.core.management import call_command
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.models import NOT_PROVIDED
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from production.errors import (
    ImmutableMediaError, IntakeError, MediaAiringNotProhibited, MediaAiringProhibited, MediaPurged,
)
from production.models import ProductionMedia, ReferenceAudio
from production.services import intake, layout, reference_audio, retention
from production.services.reference_audio import ReferenceAudioError
from updatecenter.management.commands import updatecenter_probe as probe

from .support import IsolatedMediaRootMixin, fixture, make_row

RECORDING, EDIT, BED = ProductionMedia.KIND_RECORDING, ProductionMedia.KIND_EDIT, ProductionMedia.KIND_BED


def take(*, kind=RECORDING, **options):
    return intake.ingest_stream(io.BytesIO(fixture("wav16_mono.wav")), kind=kind, **options).media


def row_values(**values):
    """Columns for a direct ProductionMedia(...) (no bytes) -- for insert-path tests."""
    media_id = uuid.uuid4()
    return {"id": media_id, "kind": "upload", "storage_key": layout.storage_key_for(media_id),
            "sha256": "a" * 64, "byte_size": 10, **values}


def edit_of(parent, **options):
    return intake.ingest_derivative(parent, io.BytesIO(fixture("wav16_mono.wav")), recipe_key="iportal.editor",
                                    recipe_version=1, recipe_params={"operations": ["trim"]}, **options).media


class IntakeProvenanceTests(IsolatedMediaRootMixin, TestCase):
    def test_ordinary_intake_is_air_eligible_and_no_air_intake_is_prohibited(self):
        self.assertFalse(take().airing_prohibited)
        self.assertTrue(take(airing_prohibited=True).airing_prohibited)
        self.assertTrue(take(kind=ProductionMedia.KIND_UPLOAD, airing_prohibited=True).airing_prohibited)

    def test_only_a_real_bool_is_accepted_and_a_refusal_stores_nothing(self):
        for value in (None, 1, 0, "true", "false"):
            with self.subTest(value=value), self.assertRaises(IntakeError) as raised:
                take(airing_prohibited=value)
            self.assertEqual(raised.exception.code, "invalid_request")
        self.assertFalse(ProductionMedia.objects.exists())

    def test_a_derivative_inherits_the_restriction_and_cannot_ask_for_less(self):
        parent = take(airing_prohibited=True)
        child = edit_of(parent, airing_prohibited=False)                   # an ordinary workspace's edit
        grandchild = edit_of(child)
        great = edit_of(grandchild, airing_prohibited=False)
        self.assertEqual([m.airing_prohibited for m in (child, grandchild, great)], [True, True, True])
        self.assertEqual(great.derived_from.derived_from.derived_from_id, parent.pk)

    def test_a_no_air_edit_of_ordinary_media_is_prohibited_and_ordinary_edits_stay_ordinary(self):
        ordinary = take()
        self.assertTrue(edit_of(ordinary, airing_prohibited=True).airing_prohibited)
        self.assertFalse(edit_of(ordinary).airing_prohibited)
        self.assertFalse(ProductionMedia.objects.get(pk=ordinary.pk).airing_prohibited)    # the parent is untouched


class ImmutabilityTests(IsolatedMediaRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.no_air = make_row(airing_prohibited=True)
        self.ordinary = make_row()

    def test_no_ordinary_orm_path_can_change_the_flag(self):
        for row, value in ((self.no_air, False), (self.ordinary, True)):
            with self.subTest(prohibited=row.airing_prohibited):
                fresh = ProductionMedia.objects.get(pk=row.pk)
                fresh.airing_prohibited = value
                with self.assertRaises(ImmutableMediaError):
                    fresh.save()
                with self.assertRaises(ImmutableMediaError):
                    fresh.save(update_fields=["airing_prohibited"])
                for manager in (ProductionMedia.objects, ProductionMedia._base_manager):
                    with self.assertRaises(ImmutableMediaError):
                        manager.filter(pk=row.pk).update(airing_prohibited=value)
                with self.assertRaises(ImmutableMediaError):
                    ProductionMedia.objects.bulk_update([fresh], ["airing_prohibited"])
                self.assertEqual(ProductionMedia.objects.get(pk=row.pk).airing_prohibited, not value)

    def test_no_insert_path_can_create_a_child_that_drops_the_restriction(self):
        attempts = {
            "create": lambda: make_row(kind=EDIT, derived_from=self.no_air),
            "create_false": lambda: make_row(kind=EDIT, derived_from=self.no_air, airing_prohibited=False),
        }
        for label, attempt in attempts.items():
            with self.subTest(path=label), self.assertRaises(ImmutableMediaError) as raised:
                attempt()
            self.assertEqual(raised.exception.code, "airing_prohibited")
        for manager in (ProductionMedia.objects, ProductionMedia._base_manager):
            with self.subTest(path=f"bulk_create via {manager}"), self.assertRaises(ImmutableMediaError):
                row = ProductionMedia(**row_values(kind=EDIT, derived_from=self.no_air))
                manager.bulk_create([row])
        self.assertFalse(ProductionMedia.objects.filter(derived_from=self.no_air).exists())
        # the honest rows are accepted
        self.assertTrue(make_row(kind=EDIT, derived_from=self.no_air, airing_prohibited=True).airing_prohibited)
        self.assertFalse(make_row(kind=EDIT, derived_from=self.ordinary).airing_prohibited)


class BindingLockTests(IsolatedMediaRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.no_air = take(airing_prohibited=True)
        self.ordinary = take()

    def test_the_canonical_binding_refuses_no_air_media_and_takes_ordinary_media(self):
        with self.assertRaises(MediaAiringProhibited) as raised, transaction.atomic():
            retention.lock_for_binding(self.no_air)
        self.assertEqual(raised.exception.code, "airing_prohibited")
        with self.assertRaises(MediaAiringProhibited), transaction.atomic():
            retention.lock_for_binding(self.no_air.pk, require_valid=False)          # no opt-out
        with transaction.atomic():
            self.assertEqual(retention.lock_for_binding(self.ordinary).pk, self.ordinary.pk)

    def test_the_no_air_binding_requires_no_air_media(self):
        with transaction.atomic():
            self.assertEqual(retention.lock_for_no_air_binding(self.no_air).pk, self.no_air.pk)
        with self.assertRaises(MediaAiringNotProhibited) as raised, transaction.atomic():
            retention.lock_for_no_air_binding(self.ordinary)
        self.assertEqual(raised.exception.code, "airing_not_prohibited")

    def test_the_derivation_lock_takes_either_and_still_proves_presence(self):
        with transaction.atomic():
            self.assertEqual(retention.lock_parent_for_derivation(self.no_air).pk, self.no_air.pk)
            self.assertEqual(retention.lock_parent_for_derivation(self.ordinary).pk, self.ordinary.pk)
        retention.purge_media(self.no_air)
        with self.assertRaises(MediaPurged), transaction.atomic():
            retention.lock_parent_for_derivation(self.no_air)
        with self.assertRaises(IntakeError):
            edit_of(self.no_air)

    def test_every_lock_still_requires_a_transaction(self):
        from unittest import mock
        outside = mock.Mock(in_atomic_block=False)
        for lock in (retention.lock_for_binding, retention.lock_for_no_air_binding,
                     retention.lock_parent_for_derivation):
            with self.subTest(lock=lock.__name__), self.assertRaises(RuntimeError), \
                    mock.patch("production.services.retention.transaction.get_connection", return_value=outside):
                lock(self.ordinary)


class ReferenceAudioTests(IsolatedMediaRootMixin, TestCase):
    def test_no_air_media_never_becomes_reference_audio(self):
        bed = take(kind=BED, airing_prohibited=True)
        with self.assertRaises(ReferenceAudioError) as raised:
            reference_audio.register("bed.no_air", bed)
        self.assertEqual(raised.exception.code, "reference_airing_prohibited")
        self.assertFalse(ReferenceAudio.objects.filter(key="bed.no_air").exists())
        ordinary = take(kind=BED)
        self.assertEqual(reference_audio.register("bed.no_air", ordinary).media_id, ordinary.pk)


class AiringProhibitedMigrationTests(IsolatedMediaRootMixin, SimpleTestCase):
    """production.0003 is exactly one AddField the REAL classifier judges
    additive (non-null, explicit simple literal default, no database default,
    not unique, not indexed, not relational) -- no manual operation."""

    REF = "production.0003_media_airing_prohibited"

    def test_one_additive_add_field_and_nothing_else(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        migration = loader.disk_migrations[("production", "0003_media_airing_prohibited")]
        self.assertEqual(migration.dependencies, [("production", "0002_referenceaudio")])
        self.assertTrue(migration.atomic)
        (operation,) = migration.operations
        self.assertEqual((type(operation).__name__, operation.model_name, operation.name),
                         ("AddField", "productionmedia", "airing_prohibited"))
        field = operation.field
        self.assertEqual((type(field).__name__, field.null, field.default, field.unique, field.db_index),
                         ("BooleanField", False, False, False, False))
        self.assertIs(field.db_default, NOT_PROVIDED)
        (plan,) = probe._serialize_exact_plan(loader, [self.REF])
        self.assertEqual([(op["operation"], op["classification"]) for op in plan["operations"]],
                         [("AddField", "additive")])
        self.assertEqual(probe.extract_manual_operations([plan]), [])


class ExistingRowsMigrationTests(IsolatedMediaRootMixin, TransactionTestCase):
    """Rows that predate the column keep their historical, air-eligible meaning.
    serialized_rollback: its teardown must not re-emit post_migrate into a
    flushed database a later serialized-rollback class then reloads into."""

    serialized_rollback = True

    def test_existing_rows_become_air_eligible(self):
        executor = MigrationExecutor(connection)
        before, after = [("production", "0002_referenceaudio")], [("production", "0003_media_airing_prohibited")]
        executor.migrate(before)
        try:
            executor.loader.build_graph()
            historical = executor.loader.project_state(before[0]).apps.get_model("production", "ProductionMedia")
            row = historical.objects.create(**row_values())
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            self.assertIs(ProductionMedia.objects.get(pk=row.pk).airing_prohibited, False)
        finally:
            call_command("migrate", verbosity=0)
