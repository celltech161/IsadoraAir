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
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.models import NOT_PROVIDED
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from production.errors import (
    ImmutableMediaError, IntakeError, MediaAiringNotProhibited, MediaAiringProhibited, MediaInconsistent,
    MediaPurged,
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


class ParentMustExistTests(IsolatedMediaRootMixin, TestCase):
    """r0113 re-review blocker A: a derived row's parent must already EXIST when
    the child is inserted. Foreign keys are deferred, so otherwise a prohibited
    parent and an unrestricted child could be inserted together -- in one
    bulk_create (either order, either guarded manager, any batch size or
    conflict mode) or child-first inside one transaction -- and the child would
    be air-eligible. Every supported path (intake, the rendition authority)
    derives from a parent it has already locked."""

    MANAGERS = (("objects", ProductionMedia.objects), ("_base_manager", ProductionMedia._base_manager))

    def chain(self, *flags):
        """Unsaved rows, each derived from the previous one."""
        rows, parent = [], None
        for flag in flags:
            values = row_values(airing_prohibited=flag)
            if parent is not None:
                values.update(kind=EDIT, derived_from=parent)
            parent = ProductionMedia(**values)
            rows.append(parent)
        return rows

    def assert_refused(self, attempt, rows, code="parent_not_persisted"):
        with self.assertRaises(ImmutableMediaError) as raised:
            attempt()
        self.assertEqual(raised.exception.code, code)
        ids = [row.pk for row in rows]
        self.assertFalse(ProductionMedia.objects.filter(pk__in=ids).exists())          # nothing, not even a prefix
        for pk in ids:
            with self.subTest(unbindable=pk), self.assertRaises(MediaInconsistent), transaction.atomic():
                retention.lock_for_binding(pk)

    def test_the_verified_reproduction_is_refused(self):
        parent, child = self.chain(True, False)
        self.assert_refused(lambda: ProductionMedia.objects.bulk_create([parent, child]), [parent, child])

    def test_either_order_either_manager_any_batch_size_or_conflict_mode(self):
        for label, manager in self.MANAGERS:
            for order in ("parent first", "child first"):
                for options in ({}, {"batch_size": 1}, {"ignore_conflicts": True}):
                    with self.subTest(manager=label, order=order, options=options):
                        parent, child = self.chain(True, False)
                        batch = [parent, child] if order == "parent first" else [child, parent]
                        self.assert_refused(lambda: manager.bulk_create(batch, **options), [parent, child])

    def test_multi_hop_batches_cannot_drop_the_restriction(self):
        for flags in ((True, True, False), (True, False, True), (True, False, False)):
            for reverse in (False, True):
                with self.subTest(flags=flags, reverse=reverse):
                    rows = self.chain(*flags)
                    batch = list(reversed(rows)) if reverse else rows
                    self.assert_refused(lambda: ProductionMedia.objects.bulk_create(batch), rows)

    def test_a_same_batch_derivation_is_unsupported_even_when_honest(self):
        for flags in ((True, True), (False, False)):
            with self.subTest(flags=flags):
                rows = self.chain(*flags)
                self.assert_refused(lambda: ProductionMedia.objects.bulk_create(rows), rows)

    def test_a_child_inserted_before_its_parent_in_one_transaction_is_refused(self):
        parent, child = self.chain(True, False)
        self.assert_refused(child.save, [child])                      # save() / create() path
        parent, child = self.chain(True, False)
        self.assert_refused(lambda: ProductionMedia.objects.bulk_create([child]), [child])
        self.assertFalse(ProductionMedia.objects.filter(pk=parent.pk).exists())

    def test_persisted_parents_still_derive_as_before(self):
        prohibited, ordinary = make_row(airing_prohibited=True), make_row()
        self.assertTrue(make_row(kind=EDIT, derived_from=prohibited, airing_prohibited=True).airing_prohibited)
        with self.assertRaises(ImmutableMediaError) as raised:
            make_row(kind=EDIT, derived_from=prohibited)
        self.assertEqual(raised.exception.code, "airing_prohibited")
        made = ProductionMedia.objects.bulk_create([
            ProductionMedia(**row_values(kind=EDIT, derived_from=ordinary)),
            ProductionMedia(**row_values(kind=EDIT, derived_from=prohibited, airing_prohibited=True)),
            ProductionMedia(**row_values()),
        ])
        self.assertEqual([m.airing_prohibited for m in ProductionMedia.objects.filter(pk__in=[m.pk for m in made])
                          .order_by("airing_prohibited")], [False, False, True])
        with transaction.atomic():
            self.assertEqual(retention.lock_for_binding(made[0], require_valid=False).pk, made[0].pk)  # bindable


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
    """production.0003 is exactly one AddField: a non-null boolean with BOTH a
    Python default and a DATABASE default (False), so a still-running r0112
    writer -- which inserts rows without the column, between the updater's
    migration and its restart -- keeps working. The REAL classifier judges a
    database default manual (reviewed); that is its correct classification,
    and the r0113 migration authorization must name it."""

    REF = "production.0003_media_airing_prohibited"

    def test_one_add_field_with_a_database_default_classified_manual(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        migration = loader.disk_migrations[("production", "0003_media_airing_prohibited")]
        self.assertEqual(migration.dependencies, [("production", "0002_referenceaudio")])
        self.assertTrue(migration.atomic)
        (operation,) = migration.operations
        self.assertEqual((type(operation).__name__, operation.model_name, operation.name),
                         ("AddField", "productionmedia", "airing_prohibited"))
        field = operation.field
        self.assertEqual((type(field).__name__, field.null, field.default, field.db_default, field.unique,
                          field.db_index), ("BooleanField", False, False, False, False, False))
        (plan,) = probe._serialize_exact_plan(loader, [self.REF])
        self.assertEqual([(op["operation"], op["classification"], op["detail"]) for op in plan["operations"]],
                         [("AddField", "manual", "non-null AddField uses database default")])
        self.assertEqual([(m["ref"], m["operation_index"], m["operation"])
                          for m in probe.extract_manual_operations([plan])], [(self.REF, 0, "AddField")])

    def test_the_model_keeps_the_database_default(self):
        field = ProductionMedia._meta.get_field("airing_prohibited")
        self.assertEqual((field.null, field.default, field.db_default), (False, False, False))


class OldWriterCompatibilityTests(IsolatedMediaRootMixin, TransactionTestCase):
    """The deployment boundary, on real PostgreSQL: the updater applies
    migrations, then advances the source, then restarts -- so r0112 processes
    (whose model has no airing_prohibited) keep inserting ProductionMedia after
    production.0003 is applied. serialized_rollback: its teardown must not
    re-emit post_migrate into a flushed database a later serialized-rollback
    class then reloads into."""

    serialized_rollback = True
    BEFORE = ("production", "0002_referenceaudio")
    AFTER = ("production", "0003_media_airing_prohibited")

    def old_model(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.BEFORE])
        executor.loader.build_graph()
        return executor.loader.project_state(self.BEFORE).apps.get_model("production", "ProductionMedia")

    def apply(self):
        MigrationExecutor(connection).migrate([self.AFTER])

    def column(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT column_default, is_nullable, data_type FROM information_schema.columns "
                           "WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s",
                           [ProductionMedia._meta.db_table, "airing_prohibited"])
            return cursor.fetchone()

    def test_existing_rows_become_air_eligible(self):
        old = self.old_model()
        try:
            row = old.objects.create(**row_values())
            self.assertIsNone(self.column())                            # the column does not exist yet
            self.apply()
            self.assertIs(ProductionMedia.objects.get(pk=row.pk).airing_prohibited, False)
        finally:
            call_command("migrate", verbosity=0)

    def test_an_old_writer_still_inserts_after_the_migration_and_gets_false(self):
        old = self.old_model()
        try:
            self.apply()                                                # migrated; r0112 code still running
            row = old.objects.create(**row_values())                     # its INSERT never names the column
            self.assertIs(ProductionMedia.objects.get(pk=row.pk).airing_prohibited, False)
            self.assertEqual(self.column(), ("false", "NO", "boolean"))  # the DATABASE default survives
            explicit = make_row(airing_prohibited=True)                  # the default never overrides an explicit value
            self.assertIs(ProductionMedia.objects.get(pk=explicit.pk).airing_prohibited, True)
            self.assertIs(ProductionMedia.objects.get(pk=make_row().pk).airing_prohibited, False)
            with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(f"UPDATE {ProductionMedia._meta.db_table} SET airing_prohibited = NULL "
                               "WHERE id = %s", [row.pk])                # still NOT NULL
        finally:
            call_command("migrate", verbosity=0)

    def test_the_migration_sql_keeps_the_default(self):
        out = io.StringIO()
        call_command("sqlmigrate", "production", "0003_media_airing_prohibited", stdout=out)
        sql = out.getvalue()
        self.assertIn('ADD COLUMN "airing_prohibited" boolean DEFAULT false NOT NULL', sql)
        self.assertNotIn("DROP DEFAULT", sql)
