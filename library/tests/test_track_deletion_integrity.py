import json
import tempfile
import threading
import time
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import close_old_connections, transaction
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from library.admin import TrackAdmin
from library.models import (
    Artist,
    Category,
    CategoryKind,
    DuplicateCandidate,
    Playlist,
    PlaylistItem,
    Track,
)
from library.services import waveform_cache
from library.services.track_deletion import delete_track, delete_tracks
from library.views import _delete_track_and_file


def _payload(track_id):
    return {
        "track_id": track_id,
        "analysis_duration": 2.0,
        "envelope_times": [0.5, 1.0],
        "envelope_db": [-20.0, -30.0],
        "samples_left": [1, 2],
        "samples_right": [1, 2],
    }


class _DeletionFixture:
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.media = self.root / "media"
        self.waves = self.root / "waveforms"
        self.media.mkdir()
        self.waves.mkdir()
        settings = override_settings(
            LIBRARY_ROOT=str(self.media),
            WAVEFORMS_DIR=str(self.waves),
            SECURE_SSL_REDIRECT=False,
        )
        settings.enable()
        self.addCleanup(settings.disable)
        kind, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        self.category = Category.objects.create(code="DELETE", name="Delete", kind=kind)
        self.artist = Artist.objects.create(name="Deletion Test Artist")
        self.user = User.objects.create_superuser("delete-op", "delete@example.invalid", "pw")

    def track(self, name="track.flac", *, cache=True, pk=None):
        source = self.media / name
        source.write_bytes(b"audio")
        track = Track.objects.create(
            pk=pk,
            filepath=str(source),
            filename=name,
            title=name,
            artist=self.artist,
            category=self.category,
            duration_seconds=2.0,
            format="flac",
            ready2air=True,
        )
        if cache:
            wave = self.waves / f"{track.pk}.json"
            wave.write_text(json.dumps(_payload(track.pk)), encoding="utf-8")
            Track.objects.filter(pk=track.pk).update(waveform_path=str(wave))
            track.refresh_from_db()
        return track

    def paths(self, track):
        return Path(track.filepath), self.waves / f"{track.pk}.json"


class TrackDeletionWorkflowTests(_DeletionFixture, TestCase):
    def test_track_detail_deletion_removes_source_and_waveform_after_commit(self):
        track = self.track()
        source, wave = self.paths(track)
        self.client.force_login(self.user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete(reverse("library:api-track-detail", args=[track.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.assertFalse(source.exists())
        self.assertFalse(wave.exists())

    def test_admin_single_deletion_removes_source_and_waveform(self):
        track = self.track()
        source, wave = self.paths(track)
        request = RequestFactory().post("/admin/library/track/")
        request.user = self.user
        with self.captureOnCommitCallbacks(execute=True):
            TrackAdmin(Track, AdminSite()).delete_model(request, track)
        self.assertFalse(source.exists())
        self.assertFalse(wave.exists())

    def test_admin_bulk_deletion_removes_every_source_and_waveform(self):
        tracks = [self.track("one.flac"), self.track("two.flac")]
        paths = [path for track in tracks for path in self.paths(track)]
        request = RequestFactory().post("/admin/library/track/")
        request.user = self.user
        with self.captureOnCommitCallbacks(execute=True):
            TrackAdmin(Track, AdminSite()).delete_queryset(
                request,
                Track.objects.filter(pk__in=[track.pk for track in tracks]),
            )
        self.assertTrue(all(not path.exists() for path in paths))

    def test_bulk_response_reports_cleanup_pending_when_outer_transaction_delays_callback(self):
        track = self.track("pending.flac", cache=False)
        track_id = track.pk
        source, _wave = self.paths(track)
        self.client.force_login(self.user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("library:api-track-bulk"),
                data=json.dumps({"action": "delete", "ids": [track_id]}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["cleanup"], {
            "completed": 0,
            "pending": 1,
            "retained": 0,
            "failed": 0,
        })
        self.assertEqual(body["cleanup_warnings"][0]["status"], "pending")
        self.assertFalse(source.exists())

    def test_duplicate_resolution_deletes_source_and_waveform_and_preserves_category(self):
        keep = self.track("keep.flac")
        drop = self.track("drop.flac")
        extra = Category.objects.create(code="EXTRA", name="Extra", kind=self.category.kind)
        Track.objects.filter(pk=drop.pk).update(category=extra)
        drop.refresh_from_db()
        source, wave = self.paths(drop)
        DuplicateCandidate.objects.create(
            track_a=keep,
            track_b=drop,
            confidence="exact",
            resolution="keep_a",
        )
        with self.captureOnCommitCallbacks(execute=True):
            call_command("apply_duplicate_resolutions", "--apply", stdout=StringIO(), stderr=StringIO())
        self.assertFalse(Track.objects.filter(pk=drop.pk).exists())
        self.assertTrue(Track.objects.get(pk=keep.pk).additional_categories.filter(pk=extra.pk).exists())
        self.assertFalse(source.exists())
        self.assertFalse(wave.exists())

    def test_orphaned_track_command_removes_waveform(self):
        track = self.track()
        source, wave = self.paths(track)
        source.unlink()
        with self.captureOnCommitCallbacks(execute=True):
            call_command("find_orphaned_tracks", "--apply", stdout=StringIO(), stderr=StringIO())
        self.assertFalse(Track.objects.filter(pk=track.pk).exists())
        self.assertFalse(wave.exists())

    def test_remove_track_file_command_uses_commit_safe_cleanup(self):
        track = self.track()
        track_id = track.pk
        source, wave = self.paths(track)
        with self.captureOnCommitCallbacks(execute=True):
            call_command("remove_track_file", str(source), stdout=StringIO(), stderr=StringIO())
        self.assertFalse(Track.objects.filter(pk=track_id).exists())
        self.assertFalse(source.exists())
        self.assertFalse(wave.exists())

    def test_protected_delete_preserves_database_source_and_waveform(self):
        track = self.track()
        source, wave = self.paths(track)
        playlist = Playlist.objects.create(name="Protects track")
        PlaylistItem.objects.create(playlist=playlist, position=0, track=track)
        with self.captureOnCommitCallbacks(execute=True):
            ok, reason = _delete_track_and_file(track)
        self.assertFalse(ok)
        self.assertIn("still referenced", reason)
        self.assertTrue(Track.objects.filter(pk=track.pk).exists())
        self.assertTrue(source.exists())
        self.assertTrue(wave.exists())

    def test_outer_transaction_rollback_preserves_database_source_and_waveform(self):
        track = self.track()
        track_id = track.pk
        source, wave = self.paths(track)
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    delete_track(track, remove_source=True)
                    raise RuntimeError("roll back")
            except RuntimeError:
                pass
        self.assertTrue(Track.objects.filter(pk=track_id).exists())
        self.assertTrue(source.exists())
        self.assertTrue(wave.exists())

    def test_missing_waveform_is_harmless(self):
        track = self.track(cache=False)
        source, wave = self.paths(track)
        with self.captureOnCommitCallbacks(execute=True):
            delete_track(track, remove_source=True)
        self.assertFalse(source.exists())
        self.assertFalse(wave.exists())

    def test_failing_source_unlink_is_reported_without_undoing_database_delete(self):
        track = self.track(cache=False)
        track_id = track.pk
        source, _wave = self.paths(track)
        real_unlink = Path.unlink

        def fail_source(path, *args, **kwargs):
            if path == source:
                raise PermissionError("test refusal")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", fail_source):
            with self.assertLogs("library.services.track_deletion", level="ERROR"):
                with self.captureOnCommitCallbacks(execute=True):
                    cleanup = delete_track(track, remove_source=True)

        self.assertFalse(Track.objects.filter(pk=track_id).exists())
        self.assertTrue(source.exists())
        self.assertEqual(cleanup.status, "failed")
        self.assertEqual(cleanup.error, "PermissionError")

    def test_busy_waveform_lock_is_observable_but_does_not_undo_delete(self):
        track = self.track()
        track_id = track.pk
        _source, wave = self.paths(track)
        with mock.patch.object(
            waveform_cache,
            "remove",
            side_effect=waveform_cache.WaveformBusy("test lock"),
        ):
            with self.assertLogs("library.services.track_deletion", level="ERROR") as messages:
                with self.captureOnCommitCallbacks(execute=True):
                    delete_track(track, remove_source=False)

        self.assertFalse(Track.objects.filter(pk=track_id).exists())
        self.assertTrue(wave.exists())
        self.assertIn("WaveformBusy", "\n".join(messages.output))

    def test_reused_track_id_retains_new_owners_waveform_and_clears_old_fence(self):
        track = self.track("old.flac")
        track_id = track.pk
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            delete_track(track, remove_source=False)

        replacement = self.track("replacement.flac", pk=track_id)
        _source, wave = self.paths(replacement)
        deleted_marker = self.waves / ".locks" / f"{track_id}.deleted"
        deleted_marker.parent.mkdir(exist_ok=True)
        deleted_marker.write_text("deleted\n", encoding="ascii")
        for callback in callbacks:
            callback()

        self.assertTrue(Track.objects.filter(pk=track_id).exists())
        self.assertTrue(wave.exists())
        self.assertFalse(deleted_marker.exists())

    def test_reused_source_path_is_retained_for_new_live_owner(self):
        track = self.track("reused.flac", cache=False)
        source, _wave = self.paths(track)
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            cleanup = delete_track(track, remove_source=True)

        Track.objects.create(
            filepath=str(source),
            filename=source.name,
            title="replacement",
            artist=self.artist,
            category=self.category,
            duration_seconds=2.0,
            format="flac",
            ready2air=True,
        )
        for callback in callbacks:
            callback()

        self.assertTrue(source.exists())
        self.assertEqual(cleanup.status, "retained_live_owner")

    def test_replaced_source_file_is_retained_even_without_new_database_owner(self):
        track = self.track("replaced.flac", cache=False)
        source, _wave = self.paths(track)
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            cleanup = delete_track(track, remove_source=True)

        source.unlink()
        source.write_bytes(b"replacement audio")
        for callback in callbacks:
            callback()

        self.assertEqual(source.read_bytes(), b"replacement audio")
        self.assertEqual(cleanup.status, "retained_replaced")

    def test_one_callback_failure_does_not_prevent_later_source_cleanup(self):
        first = self.track("first.flac", cache=False)
        second = self.track("second.flac", cache=False)
        first_path, _ = self.paths(first)
        second_path, _ = self.paths(second)
        real_unlink = Path.unlink

        def fail_first(path, *args, **kwargs):
            if path == first_path:
                raise PermissionError("test refusal")
            return real_unlink(path, *args, **kwargs)

        def broken_reporter(_path, _exc):
            raise RuntimeError("test reporter failure")

        with mock.patch.object(Path, "unlink", fail_first):
            with self.assertLogs("library.services.track_deletion", level="ERROR"):
                with self.captureOnCommitCallbacks(execute=True):
                    results = delete_tracks(
                        Track.objects.filter(pk__in=[first.pk, second.pk]).order_by("pk"),
                        remove_sources=True,
                        on_source_error=broken_reporter,
                    )

        self.assertEqual([result.status for result in results], ["failed", "removed"])
        self.assertTrue(first_path.exists())
        self.assertFalse(second_path.exists())

    def test_restore_file_cannot_recreate_orphan_cache(self):
        track = self.track()
        track_id = track.pk
        backup = self.root / "backup.json"
        backup.write_text(json.dumps(_payload(track_id)), encoding="utf-8")
        with self.captureOnCommitCallbacks(execute=True):
            delete_track(track, remove_source=False)

        with self.assertRaises(waveform_cache.PromotionRefused) as raised:
            waveform_cache.restore_file(track_id, backup, self.waves)

        self.assertEqual(raised.exception.code, "track_missing")
        self.assertFalse((self.waves / f"{track_id}.json").exists())

    def test_restore_file_remains_available_to_a_live_track_and_clears_stale_fence(self):
        track = self.track()
        track_id = track.pk
        backup = self.root / "backup-live.json"
        backup.write_text(json.dumps(_payload(track_id)), encoding="utf-8")
        deleted_marker = self.waves / ".locks" / f"{track_id}.deleted"
        deleted_marker.parent.mkdir(exist_ok=True)
        deleted_marker.write_text("deleted\n", encoding="ascii")

        waveform_cache.restore_file(track_id, backup, self.waves)

        self.assertEqual((self.waves / f"{track_id}.json").read_bytes(), backup.read_bytes())
        self.assertFalse(deleted_marker.exists())

    def test_deferred_field_queryset_delete_captures_waveform_before_row_is_gone(self):
        track = self.track()
        track_id = track.pk
        _source, wave = self.paths(track)
        with self.captureOnCommitCallbacks(execute=True):
            Track.objects.filter(pk=track_id).only("id").delete()

        self.assertFalse(Track.objects.filter(pk=track_id).exists())
        self.assertFalse(wave.exists())

    def test_unexpected_waveform_path_never_removes_unrelated_file(self):
        track = self.track()
        source, canonical = self.paths(track)
        unrelated = self.root / "unrelated.json"
        unrelated.write_text("keep", encoding="utf-8")
        Track.objects.filter(pk=track.pk).update(waveform_path=str(unrelated))
        track.refresh_from_db()
        with self.assertLogs("library.services.track_deletion", level="ERROR"):
            with self.captureOnCommitCallbacks(execute=True):
                delete_track(track, remove_source=True)
        self.assertFalse(source.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(canonical.exists())

    def test_active_message_publication_keeps_domain_owned_media_for_retention(self):
        """Library deletion must not bypass Active Messages quarantine."""
        from django.db import models

        from active_messages import _store
        from active_messages.models import ActiveBroadcastExecution, ActiveBroadcastPublication
        from production.models import ProductionMedia

        execution = _store.insert(
            ActiveBroadcastExecution,
            reason="scheduled",
            policy_generation=1,
            composition_digest="c" * 64,
            composition_snapshot={"components": [{"message": "test"}]},
            component_count=1,
            due_at=timezone.now(),
        )
        (media,) = models.QuerySet(ProductionMedia).bulk_create([
            ProductionMedia(
                kind=ProductionMedia.KIND_UPLOAD,
                storage_key="aa/" + "a" * 32,
                sha256="b" * 64,
                byte_size=5,
            )
        ])
        track = self.track()
        source, wave = self.paths(track)
        publication = _store.insert(
            ActiveBroadcastPublication,
            execution=execution,
            media=media,
            library_relpath=f"UrgentPA/am-{execution.pk.hex}.flac",
            sha256=media.sha256,
            track=track,
            track_id_snapshot=track.pk,
            published_at=timezone.now(),
        )

        with self.captureOnCommitCallbacks(execute=True):
            delete_track(track, remove_source=True)

        publication.refresh_from_db()
        self.assertIsNone(publication.track_id)
        self.assertTrue(source.exists())
        self.assertTrue(wave.exists())

    def test_waveform_api_requires_a_live_track_even_if_cache_exists(self):
        self.client.force_login(self.user)
        track = self.track()
        response = self.client.get(reverse("library:api-waveform", args=[track.pk]))
        self.assertEqual(response.status_code, 200)

        missing = self.track("missing-waveform.flac", cache=False)
        response = self.client.get(reverse("library:api-waveform", args=[missing.pk]))
        self.assertEqual(response.status_code, 404)

        stray_id = missing.pk + 1000000
        (self.waves / f"{stray_id}.json").write_text(json.dumps(_payload(stray_id)), encoding="utf-8")
        response = self.client.get(reverse("library:api-waveform", args=[stray_id]))
        self.assertEqual(response.status_code, 404)


class ConcurrentWaveformDeletionTests(_DeletionFixture, TransactionTestCase):
    def test_publisher_overlapping_committed_delete_cannot_leave_an_orphan(self):
        track = self.track(cache=False)
        track_id = track.pk
        staged = self.root / "staged.json"
        staged.write_text(json.dumps(_payload(track_id)), encoding="utf-8")
        publisher_at_replace = threading.Event()
        allow_replace = threading.Event()
        publisher_errors = []
        delete_errors = []
        original_replace = waveform_cache._replace

        def paused_replace(*args, **kwargs):
            publisher_at_replace.set()
            if not allow_replace.wait(5):
                raise RuntimeError("test publisher was not released")
            return original_replace(*args, **kwargs)

        def publish():
            close_old_connections()
            try:
                waveform_cache.install_file(track_id, staged, self.waves)
            except Exception as exc:  # collected for assertion in the main thread
                publisher_errors.append(exc)
            finally:
                close_old_connections()

        def delete():
            close_old_connections()
            try:
                delete_track(Track.objects.get(pk=track_id), remove_source=True)
            except Exception as exc:  # collected for assertion in the main thread
                delete_errors.append(exc)
            finally:
                close_old_connections()

        with mock.patch.object(waveform_cache, "_replace", side_effect=paused_replace):
            publisher = threading.Thread(target=publish)
            publisher.start()
            self.assertTrue(publisher_at_replace.wait(5))
            deleter = threading.Thread(target=delete)
            deleter.start()
            deadline = time.monotonic() + 5
            while Track.objects.filter(pk=track_id).exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(Track.objects.filter(pk=track_id).exists())
            allow_replace.set()
            publisher.join(5)
            deleter.join(5)

        self.assertFalse(publisher.is_alive())
        self.assertFalse(deleter.is_alive())
        self.assertEqual(publisher_errors, [])
        self.assertEqual(delete_errors, [])
        self.assertFalse((self.waves / f"{track_id}.json").exists())

    def test_publisher_started_after_delete_is_refused(self):
        track = self.track(cache=False)
        track_id = track.pk
        staged = self.root / "staged.json"
        staged.write_text(json.dumps(_payload(track_id)), encoding="utf-8")
        delete_track(track, remove_source=True)
        with self.assertRaises(waveform_cache.PromotionRefused) as raised:
            waveform_cache.install_file(track_id, staged, self.waves)
        self.assertEqual(raised.exception.code, "track_missing")


class BulkDeletionReportingTests(_DeletionFixture, TransactionTestCase):
    def test_bulk_response_distinguishes_completed_failed_and_blocked_deletes(self):
        completed = self.track("completed.flac", cache=False)
        failed = self.track("failed.flac", cache=False)
        blocked = self.track("blocked.flac", cache=False)
        blocked_id = blocked.pk
        playlist = Playlist.objects.create(name="Bulk blocker")
        PlaylistItem.objects.create(playlist=playlist, position=0, track=blocked)
        failed_path, _wave = self.paths(failed)
        real_unlink = Path.unlink

        def fail_one(path, *args, **kwargs):
            if path == failed_path:
                raise PermissionError("test refusal")
            return real_unlink(path, *args, **kwargs)

        self.client.force_login(self.user)
        with mock.patch.object(Path, "unlink", fail_one):
            with self.assertLogs("library.services.track_deletion", level="ERROR"):
                response = self.client.post(
                    reverse("library:api-track-bulk"),
                    data=json.dumps({
                        "action": "delete",
                        "ids": [completed.pk, failed.pk, blocked.pk],
                    }),
                    content_type="application/json",
                )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["deleted"], 2)
        self.assertEqual(body["cleanup"], {
            "completed": 1,
            "pending": 0,
            "retained": 0,
            "failed": 1,
        })
        self.assertEqual(len(body["cleanup_warnings"]), 1)
        self.assertEqual(body["cleanup_warnings"][0]["status"], "failed")
        self.assertIn("database deletion committed", body["cleanup_warnings"][0]["reason"])
        self.assertEqual(len(body["blocked"]), 1)
        self.assertEqual(body["blocked"][0]["id"], blocked_id)
        self.assertTrue(Track.objects.filter(pk=blocked_id).exists())
