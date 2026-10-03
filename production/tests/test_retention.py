"""Purge safety: reference-guarded, crash-ordered, race-closed -- and no policy."""
import threading
import time
from datetime import timedelta
from pathlib import Path

from django.apps import apps
from django.db import connection, connections, models, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from production.errors import MediaNotValidated, MediaPurged, PurgeRefused
from production.models import ProductionMedia
from production.services import intake, layout, reconcile, retention

from .support import IsolatedMediaRootMixin, fixture

WAIT = 20


class FileLike:
    def __init__(self, data):
        self.data, self.position = data, 0

    def read(self, size=-1):
        chunk = self.data[self.position:self.position + (size if size and size > 0 else len(self.data))]
        self.position += len(chunk)
        return chunk


def media_row(name="wav16_mono.wav", validate=True, **kwargs):
    kwargs.setdefault("kind", "upload")
    return intake.ingest_stream(FileLike(fixture(name)), validate=validate, **kwargs).media


class PurgeTests(IsolatedMediaRootMixin, TestCase):
    def test_unreferenced_media_is_purged_bytes_after_commit_and_evidence_survives(self):
        media = media_row()
        path = layout.resolve_storage_path(media.storage_key)
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            purged = retention.purge_media(media)
        self.assertEqual(purged.retention_state, "purged")
        self.assertIsNotNone(purged.purged_at)
        self.assertTrue(path.exists())                 # state committed first, bytes go AFTER the commit
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertFalse(path.exists())
        row = ProductionMedia.objects.get(pk=media.pk)
        for field in ("sha256", "byte_size", "storage_key", "container", "codec", "probe",
                      "decoded_duration_seconds", "validation_state", "created_at"):
            self.assertEqual(getattr(row, field), getattr(media, field), field)

    def test_a_crash_between_the_commit_and_the_unlink_leaves_a_purged_row_with_harmless_bytes(self):
        media = media_row()
        with self.captureOnCommitCallbacks(execute=False):
            retention.purge_media(media)               # the on_commit callback never runs: the "crash"
        self.assertEqual(ProductionMedia.objects.get(pk=media.pk).retention_state, "purged")
        [leftover] = self.list_files("media")
        # No present row ever points at missing bytes; the reconciler reclaims the leftover.
        self.assertEqual(reconcile.find_inconsistent_media(), [])
        report = reconcile.sweep_orphan_media(apply=True, now=timezone.now() + timedelta(hours=25))
        self.assertEqual(report.removed, [f"media/{leftover}"])

    def test_purging_again_is_idempotent_and_repairs_leftover_bytes(self):
        media = media_row()
        with self.captureOnCommitCallbacks(execute=False):
            retention.purge_media(media)
        self.assertEqual(len(self.list_files("media")), 1)
        with self.captureOnCommitCallbacks(execute=True):
            again = retention.purge_media(media)
        self.assertEqual(again.retention_state, "purged")
        self.assertEqual(self.list_files("media"), [])

    def test_other_media_is_untouched(self):
        keep, drop = media_row(), media_row("flac.flac")
        with self.captureOnCommitCallbacks(execute=True):
            retention.purge_media(drop)
        self.assertEqual(self.list_files("media"), [keep.storage_key])
        self.assertEqual(ProductionMedia.objects.get(pk=keep.pk).retention_state, "present")

    def test_unvalidated_and_invalid_media_can_be_purged_too(self):
        stored = media_row(validate=False)
        invalid = media_row("truncated_flac.flac", retain_invalid=True)
        with self.captureOnCommitCallbacks(execute=True):
            for item in (stored, invalid):
                self.assertEqual(retention.purge_media(item).retention_state, "purged")

    def test_media_with_a_derivative_is_refused_and_nothing_changes(self):
        parent = media_row()
        intake.ingest_derivative(parent, FileLike(fixture("flac.flac")), kind="edit", validate=True)
        with self.captureOnCommitCallbacks(execute=True), self.assertRaises(PurgeRefused) as caught:
            retention.purge_media(parent)
        self.assertEqual(caught.exception.references, (("production.ProductionMedia", "derived_from", 1),))
        self.assertEqual(ProductionMedia.objects.get(pk=parent.pk).retention_state, "present")
        self.assertTrue(layout.resolve_storage_path(parent.storage_key).exists())

    def test_purge_of_an_unknown_media_raises(self):
        import uuid
        with self.assertRaises(ProductionMedia.DoesNotExist):
            retention.purge_media(uuid.uuid4())

    def test_purged_media_can_no_longer_be_opened(self):
        from production.services import media_io
        media = media_row()
        with self.captureOnCommitCallbacks(execute=True):
            retention.purge_media(media)
        with self.assertRaises(MediaPurged):
            media_io.open_media(media)


class NoAutomaticRetentionPolicyTests(IsolatedMediaRootMixin, TestCase):
    def test_no_sweep_and_no_command_ever_purges_valid_present_media_however_old(self):
        from django.core.management import call_command
        import io
        media = media_row()
        ProductionMedia.objects.filter(pk=media.pk)                       # (created_at is frozen: use the clock)
        far_future = timezone.now() + timedelta(days=3650)
        for sweep in (reconcile.sweep_stale_parts, reconcile.sweep_orphan_media, reconcile.sweep_stale_work):
            sweep(apply=True, now=far_future)
        self.assertEqual(reconcile.find_inconsistent_media(deep=True), [])
        with self.settings(), unittest_patch_now(far_future):
            call_command("production_reconcile", "--apply", "--grace-hours", "1", stdout=io.StringIO())
        row = ProductionMedia.objects.get(pk=media.pk)
        self.assertEqual((row.retention_state, row.purged_at), ("present", None))
        self.assertTrue(layout.resolve_storage_path(media.storage_key).exists())

    def test_the_only_code_that_purges_is_purge_media(self):
        """AST scan for any WRITE of the retention columns (update/create keyword
        or attribute assignment) outside the model and the retention service.
        Reads such as filter(retention_state=...) are fine."""
        import ast
        root = Path(__file__).resolve().parents[1]
        columns = {"retention_state", "purged_at"}
        writers = {"update", "create", "bulk_create", "update_or_create", "get_or_create"}
        offenders = []
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            if relative.startswith(("tests/", "migrations/")) or relative in ("models.py", "services/retention.py"):
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in writers:
                    if any(keyword.arg in columns for keyword in node.keywords):
                        offenders.append(f"{relative}:{node.lineno}")
                targets = []
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                    targets = [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr in columns:
                        offenders.append(f"{relative}:{node.lineno}")
        self.assertEqual(offenders, [])

    def test_there_is_no_scheduler_or_age_based_policy_for_production_media(self):
        repo = Path(__file__).resolve().parents[2]
        # No systemd unit/timer may run, schedule or even mention production media
        # housekeeping (the backup helper deploy/stage_production_media.sh is not a scheduler).
        units = [path for pattern in ("*.service", "*.timer") for path in (repo / "deploy").glob(pattern)]
        self.assertTrue(units)                                          # the scan really covers the units
        offenders = [path.name for path in units
                     if any(marker in path.read_text() for marker in ("production_reconcile", "production-media"))]
        self.assertEqual(offenders, [])
        source = "\n".join(path.read_text() for path in (repo / "production").rglob("*.py")
                           if "tests" not in path.parts and "migrations" not in path.parts)
        for forbidden in ("365", "retention_days", "RETENTION_DAYS", "max_age", "ttl", "TTL", "expire"):
            self.assertNotIn(forbidden, source)


def unittest_patch_now(moment):
    from unittest import mock
    return mock.patch("production.services.reconcile.timezone.now", return_value=moment)


class LockForBindingTests(IsolatedMediaRootMixin, TestCase):
    def test_a_present_valid_media_is_locked_and_returned(self):
        media = media_row()
        with transaction.atomic():
            self.assertEqual(retention.lock_for_binding(media).pk, media.pk)

    def test_purged_media_cannot_be_bound(self):
        media = media_row()
        with self.captureOnCommitCallbacks(execute=True):
            retention.purge_media(media)
        with transaction.atomic(), self.assertRaises(MediaPurged):
            retention.lock_for_binding(media)

    def test_unvalidated_media_cannot_be_bound_unless_the_consumer_allows_it(self):
        media = media_row(validate=False)
        with transaction.atomic(), self.assertRaises(MediaNotValidated):
            retention.lock_for_binding(media)
        with transaction.atomic():
            self.assertEqual(retention.lock_for_binding(media, require_valid=False).pk, media.pk)

    def test_it_refuses_to_run_outside_a_transaction(self):
        from unittest import mock
        media = media_row()
        fake = mock.Mock(in_atomic_block=False)
        with mock.patch("production.services.retention.transaction.get_connection", return_value=fake), \
                self.assertRaises(RuntimeError):
            retention.lock_for_binding(media)


class DiscoveredReferenceTests(IsolatedMediaRootMixin, TransactionTestCase):
    """A FUTURE domain's foreign keys -- modelled with real throwaway tables --
    must protect media with no registration anywhere."""

    def setUp(self):
        super().setUp()
        baseline = set(apps.all_models["production"])
        self.addCleanup(self._unregister, baseline)

        class HiddenProtect(models.Model):
            media = models.ForeignKey(ProductionMedia, on_delete=models.PROTECT, related_name="+")

            class Meta:
                app_label = "production"
                db_table = "production_t_hidden_protect"

        class CascadeRef(models.Model):
            media = models.ForeignKey(ProductionMedia, on_delete=models.CASCADE, related_name="cascade_refs")

            class Meta:
                app_label = "production"
                db_table = "production_t_cascade_ref"

        class SetNullRef(models.Model):
            media = models.ForeignKey(ProductionMedia, null=True, on_delete=models.SET_NULL, related_name="null_refs")

            class Meta:
                app_label = "production"
                db_table = "production_t_set_null_ref"

        class ActiveOnly(models.Manager):
            def get_queryset(self):
                return super().get_queryset().filter(deleted=False)

        class SoftRef(models.Model):
            media = models.ForeignKey(ProductionMedia, on_delete=models.PROTECT, related_name="soft_refs")
            deleted = models.BooleanField(default=False)
            objects = ActiveOnly()

            class Meta:
                app_label = "production"
                db_table = "production_t_soft_ref"

        class ManyRef(models.Model):
            media = models.ManyToManyField(ProductionMedia, related_name="many_refs")

            class Meta:
                app_label = "production"
                db_table = "production_t_many_ref"

        self.models = dict(hidden=HiddenProtect, cascade=CascadeRef, null=SetNullRef, soft=SoftRef, many=ManyRef)
        with connection.schema_editor() as editor:
            for model in self.models.values():
                editor.create_model(model)
        apps.clear_cache()

    def _unregister(self, baseline):
        with connection.schema_editor() as editor:
            for model in self.models.values():
                editor.delete_model(model)
        for key in set(apps.all_models["production"]) - baseline:
            apps.all_models["production"].pop(key, None)
        apps.clear_cache()

    def assert_blocks(self, make_reference, label_contains):
        media = media_row()
        reference = make_reference(media)
        with self.assertRaises(PurgeRefused) as caught:
            retention.purge_media(media)
        self.assertTrue(any(label_contains in label for label, _field, _count in caught.exception.references),
                        caught.exception.references)
        self.assertEqual(ProductionMedia.objects.get(pk=media.pk).retention_state, "present")
        self.assertTrue(layout.resolve_storage_path(media.storage_key).exists())
        return media, reference

    def test_a_hidden_related_name_plus_foreign_key_still_protects(self):
        # Django's public related_objects omits these; the guard must not.
        self.assertNotIn("HiddenProtect", [r.related_model.__name__ for r in ProductionMedia._meta.related_objects])
        media, reference = self.assert_blocks(lambda m: self.models["hidden"].objects.create(media=m), "HiddenProtect")
        reference.delete()
        self.assertEqual(retention.purge_media(media).retention_state, "purged")        # reference gone: allowed
        self.assertFalse(layout.resolve_storage_path(media.storage_key).exists())       # (real commit: bytes gone)

    def test_whatever_the_on_delete_a_referrer_protects_the_bytes(self):
        self.assert_blocks(lambda m: self.models["cascade"].objects.create(media=m), "CascadeRef")
        self.assert_blocks(lambda m: self.models["null"].objects.create(media=m), "SetNullRef")

    def test_a_many_to_many_referrer_protects(self):
        def link(media):
            holder = self.models["many"].objects.create()
            holder.media.add(media)
            return holder
        self.assert_blocks(link, "ManyRef")

    def test_rows_hidden_by_a_default_manager_still_count(self):
        media = media_row()
        self.models["soft"].objects.create(media=media)
        self.models["soft"]._base_manager.filter(media=media).update(deleted=True)
        self.assertEqual(self.models["soft"].objects.filter(media=media).count(), 0)    # hidden from the default manager
        with self.assertRaises(PurgeRefused):
            retention.purge_media(media)

    def test_unreferenced_media_is_unaffected_by_other_media_being_referenced(self):
        referenced = media_row()
        self.models["hidden"].objects.create(media=referenced)
        free = media_row("flac.flac")
        self.assertEqual(retention.find_references(free), [])
        self.assertEqual(retention.purge_media(free).retention_state, "purged")
        self.assertEqual(len(retention.find_references(referenced)), 1)


class PurgeRaceTests(DiscoveredReferenceTests):
    """Real concurrent sessions: a reference and a purge can never both win."""

    def blocked_on_a_row_lock(self):
        with connection.cursor() as cursor:
            # The wait EVENT, not the query text: pg_stat_activity truncates query
            # at 1 KiB and Django's column list pushes "FOR UPDATE" off the end.
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock' AND wait_event = 'transactionid'",
            )
            return cursor.fetchone()[0]

    def wait_until_blocked(self):
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            if self.blocked_on_a_row_lock():
                return
            time.sleep(0.02)
        self.fail("the second session never blocked on the media row lock")

    def run_thread(self, target):
        box = {"error": None, "result": None}

        def runner():
            try:
                box["result"] = target()
            except BaseException as exc:                        # noqa: BLE001 -- reported to the test
                box["error"] = exc
            finally:
                connections.close_all()
        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        return thread, box

    def test_a_domain_that_skips_lock_for_binding_is_not_protected_by_the_foreign_key_alone(self):
        """Characterization, recorded deliberately: Django's FKs are DEFERRABLE
        INITIALLY DEFERRED, so an uncommitted referrer holds no lock on the
        media row and a purge does not wait for it. This is WHY consumers must
        call lock_for_binding. The substrate cannot prevent the mistake, but it
        must be able to see it."""
        media = media_row()
        inserted, release = threading.Event(), threading.Event()

        def careless_domain():
            with transaction.atomic():
                self.models["hidden"].objects.create(media=media)     # no lock_for_binding
                inserted.set()
                release.wait(WAIT)

        careless, careless_box = self.run_thread(careless_domain)
        self.assertTrue(inserted.wait(WAIT))
        purged = retention.purge_media(media)                          # NOT blocked: it completes now
        self.assertEqual(purged.retention_state, "purged")
        self.assertEqual(self.blocked_on_a_row_lock(), 0)
        release.set()
        careless.join(WAIT)
        self.assertIsNone(careless_box["error"])                       # the FK was satisfied at commit
        self.assertEqual(self.models["hidden"].objects.count(), 1)     # a reference to purged media now exists
        self.assertEqual(
            reconcile.find_purged_media_still_referenced(),
            [(str(media.pk), self.models["hidden"]._meta.label, "media", 1)],
        )

    def test_lock_for_binding_makes_a_concurrent_purge_wait_then_refuse(self):
        media = media_row()
        locked, release = threading.Event(), threading.Event()

        def domain():
            with transaction.atomic():
                retention.lock_for_binding(media)
                locked.set()
                release.wait(WAIT)
                self.models["hidden"].objects.create(media=media)

        binder, binder_box = self.run_thread(domain)
        self.assertTrue(locked.wait(WAIT))
        purger, purger_box = self.run_thread(lambda: retention.purge_media(media))
        self.wait_until_blocked()
        release.set()
        binder.join(WAIT)
        purger.join(WAIT)
        self.assertIsNone(binder_box["error"])
        self.assertIsInstance(purger_box["error"], PurgeRefused)

    def test_a_purge_in_progress_makes_a_concurrent_bind_wait_then_refuse(self):
        media = media_row()
        purging, release = threading.Event(), threading.Event()

        def purge_in_progress():
            with transaction.atomic():
                ProductionMedia.objects.select_for_update(of=("self",)).get(pk=media.pk)
                purging.set()
                release.wait(WAIT)
                ProductionMedia.objects.filter(pk=media.pk).update(
                    retention_state="purged", purged_at=timezone.now())

        def bind():
            with transaction.atomic():
                retention.lock_for_binding(media)
                self.models["hidden"].objects.create(media=media)

        purger, purger_box = self.run_thread(purge_in_progress)
        self.assertTrue(purging.wait(WAIT))
        binder, binder_box = self.run_thread(bind)
        self.wait_until_blocked()
        release.set()
        purger.join(WAIT)
        binder.join(WAIT)
        self.assertIsNone(purger_box["error"])
        self.assertIsInstance(binder_box["error"], MediaPurged)
        self.assertEqual(self.models["hidden"].objects.count(), 0)       # nothing references purged media
