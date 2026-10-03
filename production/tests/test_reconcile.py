"""Sweeps and reports: only exact system-generated names, only when old enough."""
import io
import os
import uuid
from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from production.models import ProductionMedia
from production.services import intake, layout, reconcile

from .support import IsolatedMediaRootMixin, fixture


class FileLike:
    def __init__(self, data):
        self.data, self.position = data, 0

    def read(self, size=-1):
        chunk = self.data[self.position:self.position + (size if size and size > 0 else len(self.data))]
        self.position += len(chunk)
        return chunk


def far():
    return timezone.now() + timedelta(hours=25)


def stored(name="wav16_mono.wav"):
    return intake.ingest_stream(FileLike(fixture(name)), kind="upload", validate=False).media


class StalePartTests(IsolatedMediaRootMixin, TestCase):
    def make_part(self, name=None):
        layout.ensure_layout()
        path = layout.incoming_dir() / (name or f"{uuid.uuid4().hex}.part")
        path.write_bytes(b"partial")
        return path

    def test_dry_run_reports_but_removes_nothing(self):
        part = self.make_part()
        report = reconcile.sweep_stale_parts(now=far())
        self.assertTrue(report.dry_run)
        self.assertEqual(report.removed, [f"incoming/{part.name}"])
        self.assertTrue(part.exists())

    def test_old_parts_are_removed_young_ones_kept(self):
        part = self.make_part()
        self.assertEqual(reconcile.sweep_stale_parts(apply=True).kept_young, 1)
        self.assertTrue(part.exists())
        report = reconcile.sweep_stale_parts(apply=True, now=far())
        self.assertEqual(report.removed, [f"incoming/{part.name}"])
        self.assertFalse(part.exists())

    def test_only_exact_part_names_are_ever_removed(self):
        layout.ensure_layout()
        incoming = layout.incoming_dir()
        (incoming / "notes.txt").write_text("keep me")
        (incoming / f"{uuid.uuid4().hex}.part.bak").write_text("keep me")
        (incoming / f"{uuid.uuid4().hex.upper()}.part").write_text("keep me")
        (incoming / "subdir").mkdir()
        outside = self.root.parent / "target.part"
        outside.write_text("not ours")
        os.symlink(outside, incoming / f"{uuid.uuid4().hex}.part")           # symlink named like a part
        report = reconcile.sweep_stale_parts(apply=True, now=far())
        self.assertEqual(report.removed, [])
        self.assertEqual(len(report.unexpected), 5)
        self.assertTrue(outside.exists())
        self.assertEqual(len(os.listdir(incoming)), 5)

    def test_the_grace_period_has_a_one_hour_floor(self):
        for grace in (timedelta(0), timedelta(minutes=59), timedelta(seconds=-1)):
            with self.subTest(grace=grace), self.assertRaises(ValueError):
                reconcile.sweep_stale_parts(grace=grace)
        reconcile.sweep_stale_parts(grace=timedelta(hours=1))

    def test_a_missing_directory_is_not_an_error(self):
        self.assertEqual(reconcile.sweep_stale_parts(apply=True).removed, [])


class OrphanMediaTests(IsolatedMediaRootMixin, TestCase):
    def orphan(self):
        media = stored()
        path = layout.resolve_storage_path(media.storage_key)
        # Drop the row's protection by pointing at a file no row owns.
        key = layout.storage_key_for(uuid.uuid4())
        target = layout.resolve_storage_path(key)
        layout.ensure_dir(target.parent)
        target.write_bytes(b"orphan bytes")
        return media, key

    def test_a_file_owned_by_a_present_row_is_never_removed_however_old(self):
        media = stored()
        report = reconcile.sweep_orphan_media(apply=True, now=timezone.now() + timedelta(days=3650))
        self.assertEqual(report.removed, [])
        self.assertTrue(layout.resolve_storage_path(media.storage_key).exists())

    def test_orphans_are_removed_after_the_grace_period_only(self):
        _media, key = self.orphan()
        self.assertEqual(reconcile.sweep_orphan_media(apply=True).removed, [])
        self.assertEqual(reconcile.sweep_orphan_media(apply=True).kept_young, 1)
        self.assertEqual(reconcile.sweep_orphan_media(apply=True, now=far()).removed, [f"media/{key}"])

    def test_dry_run_is_the_default(self):
        _media, key = self.orphan()
        report = reconcile.sweep_orphan_media(now=far())
        self.assertEqual(report.removed, [f"media/{key}"])
        self.assertTrue(layout.resolve_storage_path(key).exists())

    def test_leftover_bytes_of_a_purged_row_are_swept(self):
        media = stored()
        ProductionMedia.objects.filter(pk=media.pk).update(retention_state="purged", purged_at=timezone.now())
        report = reconcile.sweep_orphan_media(apply=True, now=far())
        self.assertEqual(report.removed, [f"media/{media.storage_key}"])
        self.assertEqual(ProductionMedia.objects.get(pk=media.pk).retention_state, "purged")    # row survives

    def test_unexpected_entries_are_reported_and_left_alone(self):
        layout.ensure_layout()
        media_dir = layout.media_dir()
        (media_dir / "stray.txt").write_text("x")
        (media_dir / "zz").mkdir()                                  # not hex
        (media_dir / "ab").mkdir()
        (media_dir / "ab" / "short").write_text("x")
        (media_dir / "ab" / ("cd" + "0" * 30)).write_text("x")      # name does not start with its shard
        (media_dir / "ab" / ("ab" + "0" * 30)).mkdir()              # directory with a valid-looking name
        outside = self.root.parent / "outside"
        outside.write_text("not ours")
        os.symlink(outside, media_dir / "ab" / ("ab" + "1" * 30))
        report = reconcile.sweep_orphan_media(apply=True, now=far())
        self.assertEqual(report.removed, [])
        self.assertEqual(len(report.unexpected), 6)
        self.assertTrue(outside.exists())

    def test_many_files_are_handled_in_batches(self):
        keep = [stored("flac.flac") for _ in range(3)]
        orphans = []
        for _ in range(reconcile._BATCH // 100 + 3):
            key = layout.storage_key_for(uuid.uuid4())
            path = layout.resolve_storage_path(key)
            layout.ensure_dir(path.parent)
            path.write_bytes(b"o")
            orphans.append(key)
        with mock.patch.object(reconcile, "_BATCH", 4):
            report = reconcile.sweep_orphan_media(apply=True, now=far())
        self.assertEqual(sorted(report.removed), sorted(f"media/{key}" for key in orphans))
        for media in keep:
            self.assertTrue(layout.resolve_storage_path(media.storage_key).exists())


class StaleWorkTests(IsolatedMediaRootMixin, TestCase):
    def test_stale_work_directories_are_removed_with_their_contents(self):
        work = layout.create_work_dir()
        (work / "nested").mkdir()
        (work / "nested" / "scratch.wav").write_bytes(b"x")
        outside = self.root.parent / "outside"
        outside.mkdir()
        (outside / "precious").write_text("do not delete")
        os.symlink(outside, work / "link-out")                       # rmtree must not follow it
        report = reconcile.sweep_stale_work(apply=True, now=far())
        self.assertEqual(report.removed, [f"work/{work.name}"])
        self.assertFalse(work.exists())
        self.assertEqual((outside / "precious").read_text(), "do not delete")

    def test_a_workspace_with_recent_activity_inside_is_kept(self):
        work = layout.create_work_dir()
        (work / "output.wav").write_bytes(b"still being written")
        report = reconcile.sweep_stale_work(apply=True)
        self.assertEqual((report.removed, report.kept_young), ([], 1))
        self.assertTrue(work.exists())

    def test_only_system_named_directories_are_touched(self):
        layout.ensure_layout()
        (layout.work_root() / "mine").mkdir()
        (layout.work_root() / "file.txt").write_text("x")
        (layout.work_root() / uuid.uuid4().hex).write_text("a FILE named like a workspace")
        report = reconcile.sweep_stale_work(apply=True, now=far())
        self.assertEqual(report.removed, [])
        self.assertEqual(len(report.unexpected), 3)

    def test_dry_run_is_the_default(self):
        work = layout.create_work_dir()
        report = reconcile.sweep_stale_work(now=far())
        self.assertEqual(report.removed, [f"work/{work.name}"])
        self.assertTrue(work.exists())


class InconsistentMediaTests(IsolatedMediaRootMixin, TestCase):
    def test_a_healthy_store_reports_nothing(self):
        stored()
        self.assertEqual(reconcile.find_inconsistent_media(deep=True), [])

    def test_missing_wrong_sized_and_non_regular_bytes_are_reported_never_repaired(self):
        missing, wrong, irregular = stored(), stored("flac.flac"), stored("mp3.mp3")
        layout.resolve_storage_path(missing.storage_key).unlink()
        wrong_path = layout.resolve_storage_path(wrong.storage_key)
        wrong_path.chmod(0o640)
        wrong_path.write_bytes(b"short")
        irregular_path = layout.resolve_storage_path(irregular.storage_key)
        irregular_path.unlink()
        irregular_path.mkdir()
        found = {item.media_id: item.problem for item in reconcile.find_inconsistent_media()}
        self.assertEqual(found, {
            str(missing.pk): "missing_bytes", str(wrong.pk): "size_mismatch", str(irregular.pk): "not_a_regular_file",
        })
        self.assertEqual(ProductionMedia.objects.filter(retention_state="present").count(), 3)    # nothing "fixed"

    def test_a_same_size_corruption_needs_the_deep_check(self):
        media = stored()
        path = layout.resolve_storage_path(media.storage_key)
        data = bytearray(path.read_bytes())
        data[50] ^= 0xFF
        path.chmod(0o640)
        path.write_bytes(bytes(data))
        self.assertEqual(reconcile.find_inconsistent_media(), [])
        self.assertEqual([i.problem for i in reconcile.find_inconsistent_media(deep=True)], ["sha_mismatch"])

    def test_purged_rows_and_the_limit(self):
        gone = stored()
        ProductionMedia.objects.filter(pk=gone.pk).update(retention_state="purged", purged_at=timezone.now())
        layout.resolve_storage_path(gone.storage_key).unlink()
        self.assertEqual(reconcile.find_inconsistent_media(), [])
        for _ in range(3):
            layout.resolve_storage_path(stored().storage_key).unlink()
        self.assertEqual(len(reconcile.find_inconsistent_media(limit=2)), 2)


class ManagementCommandTests(IsolatedMediaRootMixin, TestCase):
    def run_command(self, *args):
        out = io.StringIO()
        with mock.patch("production.services.reconcile.timezone.now", return_value=far()):
            call_command("production_reconcile", *args, stdout=out, stderr=io.StringIO())
        return out.getvalue()

    def test_dry_run_by_default_then_apply(self):
        layout.ensure_layout()
        part = layout.incoming_dir() / f"{uuid.uuid4().hex}.part"
        part.write_bytes(b"partial")
        output = self.run_command("--grace-hours", "1")
        self.assertIn("DRY RUN", output)
        self.assertIn("would remove 1", output)
        self.assertTrue(part.exists())
        output = self.run_command("--grace-hours", "1", "--apply")
        self.assertNotIn("DRY RUN", output)
        self.assertIn("removed 1", output)
        self.assertFalse(part.exists())

    def test_the_grace_floor_is_enforced(self):
        with self.assertRaises(CommandError):
            call_command("production_reconcile", "--grace-hours", "0.5", stdout=io.StringIO())

    def test_it_reports_inconsistent_and_dangling_media(self):
        media = stored()
        layout.resolve_storage_path(media.storage_key).unlink()
        output = self.run_command("--grace-hours", "24")
        self.assertIn("present media with inconsistent bytes: 1", output)
        self.assertIn(f"{media.pk}: missing_bytes", output)
        self.assertIn("purged media still referenced by a domain row: 0", output)
