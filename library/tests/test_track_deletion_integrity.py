import contextlib
import json
import os
import tempfile
import threading
import time
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connections, transaction
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
from library.services import source_paths, track_deletion, waveform_cache
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
        # Fixture files are written moments before they are deleted; the freshness rule is exercised
        # explicitly (RealClockFreshnessTests stops this patch).
        self.clock = mock.patch.object(track_deletion, "_now_ns", lambda: time.time_ns() + 86_400 * 10**9)
        self.clock.start()
        self.addCleanup(self.clock.stop)
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


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _waiting_backends(kind):
    """Backends currently blocked: on a transaction id (the unique index) or on an advisory lock."""
    from django.db import connection

    with connection.cursor() as cursor:
        if kind == "advisory":
            cursor.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
        else:
            cursor.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'transactionid' AND NOT granted")
        return cursor.fetchone()[0]


class SourcePathRaceTests(_DeletionFixture, TransactionTestCase):
    """L1: a Track registered at the deleted Track's path, in flight when the cleanup runs.

    The creator runs in a second connection.  Every interleaving is forced by waiting on the database's
    own lock state (pg_locks), never on sleeps."""

    def _creator(self, path, *, hold=None, fail=False):
        results = {}
        taking = threading.Event()

        def run():
            close_old_connections()
            try:
                with transaction.atomic():
                    taking.set()
                    Track.objects.create(
                        filepath=path, filename=Path(path).name, title="new owner", artist=self.artist,
                        category=self.category, duration_seconds=2.0, format="flac",
                    )
                    results["inserted"] = True
                    if hold is not None:
                        hold.wait(20)
                    if fail:
                        raise RuntimeError("ingest failed after registering")
            except Exception as exc:
                results["error"] = repr(exc)
            finally:
                connections.close_all()

        return threading.Thread(target=run), taking, results

    def _delete_while_creator_waits(self, track, creator, taking, **delete_kwargs):
        """Delete ``track``; once the creator is provably blocked behind the uncommitted delete, commit."""
        original = track_deletion._schedule_source_cleanup

        def after_row_delete(*args, **kwargs):
            creator.start()
            self.assertTrue(taking.wait(10))
            self.assertTrue(_wait_for(lambda: _waiting_backends("transactionid") >= 1),
                            "creator never blocked behind the uncommitted delete")
            return original(*args, **kwargs)

        with mock.patch.object(track_deletion, "_schedule_source_cleanup", after_row_delete):
            return delete_track(track, remove_source=True, **delete_kwargs)

    def _release_when_cleanup_waits(self, hold):
        def watch():
            try:
                _wait_for(lambda: _waiting_backends("advisory") >= 1)
                hold.set()
            finally:
                connections.close_all()          # this thread's own connection
        thread = threading.Thread(target=watch)
        thread.start()
        return thread

    def test_creator_still_uncommitted_when_the_delete_commits_keeps_its_file(self):
        old = self.track("shared.flac", cache=False)
        path = old.filepath
        hold = threading.Event()
        creator, taking, results = self._creator(path, hold=hold)
        releaser = self._release_when_cleanup_waits(hold)
        cleanup = self._delete_while_creator_waits(old, creator, taking)
        creator.join(20)
        releaser.join(20)
        self.assertEqual(results.get("error"), None)
        self.assertEqual(cleanup.status, "retained_live_owner")
        self.assertTrue(Track.objects.filter(filepath=path).exists())
        self.assertTrue(Path(path).exists())

    def test_creator_that_commits_immediately_keeps_its_file(self):
        old = self.track("shared2.flac", cache=False)
        path = old.filepath
        creator, taking, results = self._creator(path)
        cleanup = self._delete_while_creator_waits(old, creator, taking)
        creator.join(20)
        self.assertEqual(results.get("error"), None)
        self.assertEqual(cleanup.status, "retained_live_owner")
        self.assertTrue(Track.objects.filter(filepath=path).exists())
        self.assertTrue(Path(path).exists())

    def test_creator_that_fails_after_registering_leaves_the_path_free_for_the_delete(self):
        old = self.track("shared3.flac", cache=False)
        path = old.filepath
        hold = threading.Event()
        creator, taking, results = self._creator(path, hold=hold, fail=True)
        releaser = self._release_when_cleanup_waits(hold)
        cleanup = self._delete_while_creator_waits(old, creator, taking)
        creator.join(20)
        releaser.join(20)
        self.assertIn("ingest failed", results["error"])
        self.assertEqual(cleanup.status, "removed")
        self.assertFalse(Track.objects.filter(filepath=path).exists())
        self.assertFalse(Path(path).exists())

    def test_cleanup_that_cannot_get_the_path_retains_the_file_and_says_so(self):
        old = self.track("shared4.flac", cache=False)
        path = old.filepath
        hold = threading.Event()
        creator, taking, results = self._creator(path, hold=hold)
        with mock.patch.object(source_paths, "CLEANUP_LOCK_TIMEOUT_MS", 300), \
             self.assertLogs("library.services.track_deletion", level="WARNING") as logs:
            cleanup = self._delete_while_creator_waits(old, creator, taking)
        hold.set()
        creator.join(20)
        self.assertEqual(cleanup.status, "retained_busy")
        self.assertTrue(Path(path).exists())
        self.assertTrue(any("outcome=retained_busy" in line and "shared4.flac" in line for line in logs.output))

    def test_unrelated_path_is_not_blocked_by_a_registration_in_flight(self):
        other = self.track("other.flac", cache=False)
        other_path = Path(other.filepath)
        hold = threading.Event()
        creator, taking, results = self._creator(str(self.media / "busy.flac"), hold=hold)
        creator.start()
        self.assertTrue(taking.wait(10))
        self.assertTrue(_wait_for(lambda: results.get("inserted")), "the creator never registered its path")
        started = time.monotonic()
        cleanup = delete_track(other, remove_source=True)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(cleanup.status, "removed")
        self.assertFalse(other_path.exists())
        hold.set()
        creator.join(20)

    def test_ordinary_delete_still_removes_unreferenced_audio(self):
        track = self.track("plain.flac", cache=False)
        path = Path(track.filepath)
        self.assertEqual(delete_track(track, remove_source=True).status, "removed")
        self.assertFalse(path.exists())

    def test_two_bulk_creators_registering_the_same_paths_in_opposite_order_cannot_deadlock(self):
        paths = [str(self.media / "x.flac"), str(self.media / "y.flac")]
        for path in paths:
            Path(path).write_bytes(b"audio")
        errors = []
        barrier = threading.Barrier(2)

        def bulk(order):
            close_old_connections()
            try:
                barrier.wait(10)
                Track.objects.bulk_create([
                    Track(filepath=p, filename=Path(p).name, title=Path(p).name, artist=self.artist,
                          category=self.category, duration_seconds=1.0, format="flac")
                    for p in order
                ], ignore_conflicts=True)
            except Exception as exc:
                errors.append(repr(exc))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=bulk, args=(paths,)), threading.Thread(target=bulk, args=(paths[::-1],))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(Track.objects.filter(filepath__in=paths).count(), 2)


class RegistrationClaimTests(_DeletionFixture, TestCase):
    def test_only_a_creation_or_a_changed_filepath_claims_a_path(self):
        track = self.track(cache=False)
        with mock.patch.object(source_paths, "acquire") as acquire:
            Track.objects.get(pk=track.pk).save()
            Track.objects.get(pk=track.pk).save(update_fields=["title"])
            self.assertFalse(acquire.called)
            moved = Track.objects.get(pk=track.pk)
            moved.filepath = str(self.media / "moved.flac")
            moved.save()
            self.assertEqual([call.args[0] for call in acquire.call_args_list], [moved.filepath])
            acquire.reset_mock()
            Track.objects.filter(pk=track.pk).update(play_count=3)
            self.assertFalse(acquire.called)
            Track.objects.filter(pk=track.pk).update(filepath=str(self.media / "again.flac"))
            self.assertEqual([call.args[0] for call in acquire.call_args_list], [str(self.media / "again.flac")])

    def test_creation_claims_its_path_inside_the_writing_transaction(self):
        seen = []
        real = source_paths.acquire

        def spy(path, using, *, timeout_ms):
            seen.append((path, transaction.get_connection(using).in_atomic_block))
            return real(path, using, timeout_ms=timeout_ms)

        with mock.patch.object(source_paths, "acquire", spy):
            track = self.track("claimed.flac", cache=False)
        self.assertEqual(seen, [(track.filepath, True)])

    def test_lock_key_is_stable_per_path_and_distinct_between_paths(self):
        self.assertEqual(source_paths.lock_key("/a/b.flac"), source_paths.lock_key("/a//b.flac"))
        self.assertNotEqual(source_paths.lock_key("/a/b.flac"), source_paths.lock_key("/a/c.flac"))


class RealClockFreshnessTests(_DeletionFixture, TestCase):
    """The freshness rule with the real clock (the fixture's advanced clock is stopped)."""

    def setUp(self):
        super().setUp()
        self.clock.stop()
        self.addCleanup(self.clock.start)       # keeps the fixture's own stop() balanced

    def test_recently_changed_audio_is_kept_and_reported(self):
        track = self.track("fresh.flac", cache=False)
        track_id = track.pk
        path = Path(track.filepath)
        with self.assertLogs("library.services.track_deletion", level="WARNING") as logs, \
             self.captureOnCommitCallbacks(execute=True):
            cleanup = delete_track(track, remove_source=True)
        self.assertEqual(cleanup.status, "retained_recent")
        self.assertTrue(path.exists())
        self.assertFalse(Track.objects.filter(pk=track_id).exists())
        self.assertTrue(any("operation=source_unlink" in line and "outcome=retained_recent" in line
                            and f"track_id={track_id} " in line and "db_deleted=true" in line
                            and "fresh.flac" in line and str(self.media) not in line for line in logs.output))

    def test_a_delivery_exposed_before_its_track_is_registered_survives_the_old_tracks_delete(self):
        old = self.track("episode.flac", cache=False)
        path = Path(old.filepath)
        delivery = path.with_name("episode.flac.incoming")
        delivery.write_bytes(b"new episode")
        os.replace(delivery, path)                                       # the outside ingest exposes it
        with self.captureOnCommitCallbacks(execute=True):
            cleanup = delete_track(old, remove_source=True)
        self.assertEqual(cleanup.status, "retained_recent")
        self.assertEqual(path.read_bytes(), b"new episode")
        Track.objects.create(filepath=str(path), filename=path.name, title="new", artist=self.artist,
                             category=self.category, duration_seconds=1.0, format="flac")
        self.assertTrue(path.exists())

    def test_caller_that_owns_a_fresh_file_can_remove_it(self):
        track = self.track("mine.flac", cache=False)
        path = Path(track.filepath)
        with self.captureOnCommitCallbacks(execute=True):
            cleanup = delete_track(track, remove_source=True, source_grace=False)
        self.assertEqual(cleanup.status, "removed")
        self.assertFalse(path.exists())


class CleanupEvidenceTests(_DeletionFixture, TestCase):
    def test_failed_source_cleanup_log_identifies_the_file_without_its_directory(self):
        track = self.track("stuck.flac", cache=False)
        track_id = track.pk
        with mock.patch("pathlib.Path.unlink", side_effect=PermissionError("denied")), \
             self.assertLogs("library.services.track_deletion", level="ERROR") as logs, \
             self.captureOnCommitCallbacks(execute=True):
            cleanup = delete_track(track, remove_source=True)
        self.assertEqual(cleanup.status, "failed")
        line = next(entry for entry in logs.output if "operation=source_unlink" in entry)
        for fragment in (f"track_id={track_id} ", "db_deleted=true", "outcome=failed", "'stuck.flac'",
                         "error=PermissionError"):
            self.assertIn(fragment, line)
        self.assertNotIn(str(self.media), line)

    def test_failed_waveform_cleanup_log_names_the_cache_file(self):
        track = self.track("cache.flac")
        track_id = track.pk
        with mock.patch.object(waveform_cache, "remove", side_effect=OSError("disk gone")), \
             self.assertLogs("library.services.track_deletion", level="ERROR") as logs, \
             self.captureOnCommitCallbacks(execute=True):
            delete_track(track, remove_source=False)
        line = next(entry for entry in logs.output if "operation=waveform_remove" in entry)
        for fragment in (f"track_id={track_id} ", "db_deleted=true", "outcome=failed", f"'{track_id}.json'",
                         "error=OSError"):
            self.assertIn(fragment, line)
        self.assertNotIn(str(self.waves), line)


class _PausedAcquire:
    """Patch ``source_paths.acquire`` so the named thread stops right after obtaining a path lock.

    ``held`` is set once that thread owns the lock; it keeps it (inside its transaction) until ``release``."""

    def __init__(self, thread_name):
        self.thread_name = thread_name
        self.held = threading.Event()
        self.release = threading.Event()
        self._real = source_paths.acquire

    def __call__(self, path, using, *, timeout_ms):
        self._real(path, using, timeout_ms=timeout_ms)
        if threading.current_thread().name == self.thread_name and not self.held.is_set():
            self.held.set()
            if not self.release.wait(20):
                raise RuntimeError("test never released the paused lock holder")

    @contextlib.contextmanager
    def patch(self):
        """Active for the block; always releases the holder on exit, so a failed assertion cannot strand it."""
        try:
            with mock.patch.object(source_paths, "acquire", self):
                yield self
        finally:
            self.release.set()


def _in_thread(name, target, results):
    def run():
        close_old_connections()
        try:
            results["value"] = target()
        except Exception as exc:
            results["error"] = exc
        finally:
            connections.close_all()
    return threading.Thread(target=run, name=name)


class RecallRaceTests(_DeletionFixture, TransactionTestCase):
    """remove_track_file (an explicit recall) against a concurrent first registration of the same file.

    Every interleaving is forced on the database's own lock state (pg_locks), never on sleeps."""

    def delivered(self, name):
        folder = self.media / self.category.code
        folder.mkdir(exist_ok=True)
        path = folder / name
        path.write_bytes(b"delivered audio")
        return path

    def recall(self, path):
        out = StringIO()
        call_command("remove_track_file", str(path), stdout=out, stderr=StringIO())
        return out.getvalue()

    def sync(self, path):
        from library.management.commands import analyze_tracks, sync_track_file

        # The registration is what races; the analysis that follows it is not under test.
        with mock.patch.object(sync_track_file, "analyze_one_track",
                               return_value=analyze_tracks.AnalysisOutcome(True)):
            return sync_track_file.sync_track_file(str(path))

    def test_review_probe_recall_while_an_uncommitted_creator_holds_the_path_keeps_its_audio(self):
        # The independent review's reproduction: before 3.11's correction this recall found no visible
        # Track, unlinked the file, and the creator then committed a Track pointing at nothing.
        path = self.delivered("probe.flac")
        paused, results = _PausedAcquire("creator"), {}
        creator = _in_thread("creator", lambda: Track.objects.create(
            filepath=str(path), filename=path.name, title="new delivery", artist=self.artist,
            category=self.category, duration_seconds=1.0, format="flac"), results)
        with paused.patch(), mock.patch.object(source_paths, "CLEANUP_LOCK_TIMEOUT_MS", 300):
            creator.start()
            self.assertTrue(paused.held.wait(10))
            with self.assertRaisesRegex(CommandError, "being registered by another process.*nothing was removed"):
                self.recall(path)
            self.assertTrue(path.exists(), "the recall removed audio a registration in flight owns")
            paused.release.set()
            creator.join(20)
        self.assertNotIn("error", results)
        self.assertTrue(Track.objects.filter(filepath=str(path)).exists())
        self.assertTrue(path.exists())

    def test_creator_first_recall_waits_for_the_commit_then_removes_the_registered_track(self):
        path = self.delivered("creator-first.flac")
        paused, created, recalled = _PausedAcquire("creator"), {}, {}
        creator = _in_thread("creator", lambda: self.sync(path), created)
        recaller = _in_thread("recall", lambda: self.recall(path), recalled)
        with paused.patch():
            creator.start()
            self.assertTrue(paused.held.wait(10))
            recaller.start()
            self.assertTrue(_wait_for(lambda: _waiting_backends("advisory") >= 1),
                            "the recall did not wait for the registration's path lock")
            self.assertTrue(path.exists())
            paused.release.set()
            creator.join(20)
            recaller.join(20)
        self.assertNotIn("error", created)
        track, was_created = created["value"]
        self.assertTrue(was_created)
        self.assertNotIn("error", recalled)
        self.assertEqual(recalled["value"].strip(), f"Removed {path} and Track {track.pk}")
        self.assertFalse(Track.objects.filter(pk=track.pk).exists())
        self.assertFalse(path.exists())

    def test_recall_first_creator_fails_visibly_and_registers_nothing(self):
        path = self.delivered("recall-first.flac")
        paused, created, recalled = _PausedAcquire("recall"), {}, {}
        recaller = _in_thread("recall", lambda: self.recall(path), recalled)
        creator = _in_thread("creator", lambda: self.sync(path), created)
        with paused.patch():
            recaller.start()
            self.assertTrue(paused.held.wait(10))
            creator.start()
            self.assertTrue(_wait_for(lambda: _waiting_backends("advisory") >= 1),
                            "the creator did not wait for the recall's path lock")
            paused.release.set()
            recaller.join(20)
            creator.join(20)
        self.assertNotIn("error", recalled)
        self.assertEqual(recalled["value"].strip(), f"Removed {path} (no Track row existed)")
        self.assertIsInstance(created.get("error"), CommandError)
        self.assertIn("disappeared before its Track could be registered", str(created["error"]))
        self.assertFalse(Track.objects.filter(filepath=str(path)).exists())
        self.assertFalse(path.exists())

    def test_file_replaced_after_its_metadata_was_read_is_not_registered(self):
        from library.management.commands import import_songs, sync_track_file

        path = self.delivered("replaced.flac")
        real_parse = import_songs.parse_tags

        def parse_then_replace(filepath):
            parsed = real_parse(filepath)
            staged = path.with_name("replacement.part")
            staged.write_bytes(b"a different delivery")
            os.replace(staged, path)
            return parsed

        with mock.patch.object(sync_track_file, "parse_tags", parse_then_replace), \
             self.assertRaisesRegex(CommandError, "changed while its metadata was being read"):
            self.sync(path)
        self.assertFalse(Track.objects.filter(filepath=str(path)).exists())
        self.assertTrue(path.exists())

    def test_registration_lock_timeout_is_bounded_and_reported(self):
        path = self.delivered("busy-registration.flac")
        paused, recalled = _PausedAcquire("recall"), {}
        recaller = _in_thread("recall", lambda: self.recall(path), recalled)
        with paused.patch(), mock.patch.object(source_paths, "REGISTRATION_LOCK_TIMEOUT_MS", 300):
            recaller.start()
            self.assertTrue(paused.held.wait(10))
            with self.assertRaisesRegex(CommandError, "locked by a concurrent recall or cleanup.*no Track was registered"):
                self.sync(path)
            paused.release.set()
            recaller.join(20)
        self.assertFalse(Track.objects.filter(filepath=str(path)).exists())

    def test_existing_track_recall_removes_track_source_and_waveform_after_commit(self):
        track = self.track("recalled.flac")
        source, wave = self.paths(track)
        self.assertEqual(self.recall(source).strip(), f"Removed {source} and Track {track.pk}")
        self.assertFalse(Track.objects.filter(pk=track.pk).exists())
        self.assertFalse(source.exists())
        self.assertFalse(wave.exists())

    def test_rolled_back_recall_preserves_the_track_and_its_media(self):
        track = self.track("kept.flac")
        source, wave = self.paths(track)
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.recall(source)
                raise RuntimeError("caller rolled back")
        self.assertTrue(Track.objects.filter(pk=track.pk).exists())
        self.assertTrue(source.exists())
        self.assertTrue(wave.exists())

    def test_recall_of_an_unregistered_file_removes_it_and_of_a_missing_one_says_so(self):
        path = self.delivered("loose.flac")
        self.assertEqual(self.recall(path).strip(), f"Removed {path} (no Track row existed)")
        self.assertFalse(path.exists())
        self.assertEqual(self.recall(path).strip(),
                         f"{path} was already absent and no Track row existed; nothing to remove")

    def test_recall_of_a_registered_track_whose_file_is_already_gone_reports_it(self):
        track = self.track("gone.flac", cache=False)
        source = Path(track.filepath)
        source.unlink()
        self.assertEqual(self.recall(source).strip(),
                         f"Removed Track {track.pk}; its source file {source} was already absent")
        self.assertFalse(Track.objects.filter(pk=track.pk).exists())

    def test_recall_is_not_blocked_by_a_registration_of_an_unrelated_path(self):
        busy, other = self.delivered("busy.flac"), self.delivered("unrelated.flac")
        paused, results = _PausedAcquire("creator"), {}
        creator = _in_thread("creator", lambda: Track.objects.create(
            filepath=str(busy), filename=busy.name, title="busy", artist=self.artist,
            category=self.category, duration_seconds=1.0, format="flac"), results)
        with paused.patch():
            creator.start()
            self.assertTrue(paused.held.wait(10))
            started = time.monotonic()
            self.assertEqual(self.recall(other).strip(), f"Removed {other} (no Track row existed)")
            self.assertLess(time.monotonic() - started, 3)
            paused.release.set()
            creator.join(20)
        self.assertNotIn("error", results)
        self.assertTrue(busy.exists())


class TrackLockOrderTests(_DeletionFixture, TransactionTestCase):
    """Path lock before Track row lock, for every path-registering write (P2)."""

    def test_review_probe_update_or_create_against_queryset_update_does_not_deadlock(self):
        # The independent review's reproduction, unchanged: "row-first" (update_or_create) is made to enter
        # its first path claim only after "path-first" (QuerySet.update) holds the path.  Django's
        # update_or_create used to lock the row before Track.save claimed the path, so the two waited on
        # each other and PostgreSQL aborted one as a deadlock.
        track = self.track("old.flac", cache=False)
        target = str(self.media / "new.flac")
        row_locked, path_locked = threading.Event(), threading.Event()
        real_acquire = source_paths.acquire

        def wrapped(path, using, *, timeout_ms):
            name = threading.current_thread().name
            if name == "row-first":
                row_locked.set()
                if not path_locked.wait(10):
                    raise RuntimeError("path-first thread did not obtain advisory lock")
                return real_acquire(path, using, timeout_ms=timeout_ms)
            if name == "path-first":
                if not row_locked.wait(10):
                    raise RuntimeError("row-first thread did not reach save")
                real_acquire(path, using, timeout_ms=timeout_ms)
                path_locked.set()
                return None
            return real_acquire(path, using, timeout_ms=timeout_ms)

        def row_first():
            with transaction.atomic():
                Track.objects.update_or_create(pk=track.pk, defaults={"filepath": target})

        def path_first():
            with transaction.atomic():
                Track.objects.filter(pk=track.pk).update(filepath=target)

        results = {"row-first": {}, "path-first": {}}
        with mock.patch.object(source_paths, "acquire", wrapped):
            threads = [_in_thread(name, fn, results[name])
                       for name, fn in (("row-first", row_first), ("path-first", path_first))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual({name: repr(r.get("error")) for name, r in results.items() if "error" in r}, {})
        self.assertEqual(Track.objects.get(pk=track.pk).filepath, target)

    def test_update_or_create_waits_for_the_path_before_taking_the_row(self):
        track = self.track("row.flac", cache=False)
        target = str(self.media / "claimed.flac")
        paused, updater, upserter = _PausedAcquire("path-first"), {}, {}
        first = _in_thread("path-first", lambda: Track.objects.filter(pk=track.pk).update(filepath=target), updater)
        second = _in_thread("row-first", lambda: Track.objects.update_or_create(
            pk=track.pk, defaults={"filepath": target, "title": "upserted"}), upserter)
        with paused.patch():
            first.start()
            self.assertTrue(paused.held.wait(10))
            second.start()
            self.assertTrue(_wait_for(lambda: _waiting_backends("advisory") >= 1),
                            "update_or_create did not wait on the path lock")
            # Waiting on the path, update_or_create holds no row lock: nothing waits on a row.
            self.assertEqual(_waiting_backends("transactionid"), 0)
            paused.release.set()
            first.join(20)
            second.join(20)
        self.assertNotIn("error", updater)
        self.assertNotIn("error", upserter)
        self.assertEqual(updater["value"], 1)
        obj, created = upserter["value"]
        self.assertFalse(created)
        final = Track.objects.get(pk=track.pk)
        self.assertEqual((final.filepath, final.title), (target, "upserted"))
        self.assertTrue(Path(track.filepath).exists())   # no media was touched by either write

    def test_bulk_filepath_updates_in_opposite_object_order_cannot_deadlock(self):
        one, two = self.track("one.flac", cache=False), self.track("two.flac", cache=False)
        targets = {one.pk: str(self.media / "a.flac"), two.pk: str(self.media / "b.flac")}
        barrier, errors = threading.Barrier(2), []

        def bulk(order):
            close_old_connections()
            try:
                objs = list(Track.objects.filter(pk__in=order))
                objs.sort(key=lambda obj: order.index(obj.pk))
                for obj in objs:
                    obj.filepath = targets[obj.pk]
                barrier.wait(10)
                Track.objects.bulk_update(objs, ["filepath"])
            except Exception as exc:
                errors.append(repr(exc))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=bulk, args=([one.pk, two.pk],)),
                   threading.Thread(target=bulk, args=([two.pk, one.pk],))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(dict(Track.objects.filter(pk__in=targets).values_list("pk", "filepath")), targets)


class UpdateOrCreateSemanticsTests(_DeletionFixture, TestCase):
    def test_claims_only_a_literal_filepath_default_before_django_runs(self):
        track = self.track("semantics.flac", cache=False)
        target = str(self.media / "renamed.flac")
        with mock.patch.object(source_paths, "acquire") as acquire:
            obj, created = Track.objects.update_or_create(pk=track.pk, defaults={"title": "retitled"})
            self.assertEqual((obj.pk, created, obj.title), (track.pk, False, "retitled"))
            self.assertFalse(acquire.called)       # no filepath change, no path lock
            obj, created = Track.objects.using("default").update_or_create(
                filepath=track.filepath, defaults={"filepath": target})
            self.assertEqual((created, obj.filepath), (False, target))
            # The early claim, then Track.save's own re-entrant claim of the same path.
            self.assertEqual([(c.args[0], c.args[1]) for c in acquire.call_args_list],
                             [(target, "default"), (target, "default")])

    def test_create_branch_create_defaults_and_callable_defaults_keep_djangos_semantics(self):
        fresh = str(self.media / "fresh.flac")
        calls = []

        def title():
            calls.append("title")
            return "made"

        obj, created = Track.objects.update_or_create(
            filepath=fresh,
            defaults={"title": "updated"},
            create_defaults={"title": title, "filename": "fresh.flac", "artist": self.artist,
                             "category": self.category, "duration_seconds": 1.0, "format": "flac"},
        )
        self.assertTrue(created)
        self.assertEqual((obj.title, calls), ("made", ["title"]))
        obj, created = Track.objects.update_or_create(filepath=fresh, defaults={"title": title})
        self.assertFalse(created)
        self.assertEqual((obj.title, calls), ("made", ["title", "title"]))
