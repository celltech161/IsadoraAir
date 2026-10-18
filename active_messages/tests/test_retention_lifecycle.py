"""D3E-C: publication retirement and protection, through real executions.

Real READY aggregates (render + validation), real publications (isolated
library and waveform roots; the native-airing gate opened only by the D2B
test patch), the engine-side admission/authority services and real deck
evidence -- see d3ec_support. Nothing here touches the station's library or
media storage.
"""
import os
import unittest
from pathlib import Path
from unittest import mock

from django.test import TestCase

from active_messages import playout, reconcile, retention, services
from active_messages.models import ActiveBroadcastMessageEvent
from library.models import LogItem, PlayEvent, PlaylistItem, Playlist, Track
from production.tests.support import ffmpeg_available

from .d3ec_support import Lifecycle


def codes(report, category=None):
    return sorted(f.code for f in report.findings if category is None or f.category == category)


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class RetirementTests(Lifecycle, TestCase):
    def test_a_completed_airing_retires_and_keeps_every_record(self):
        execution, publication, item = self.completed()
        play_event = PlayEvent.objects.get(log_item_id_snapshot=item.pk)
        self.assertTrue(self.file(publication).is_file())
        self.assertTrue(self.wave(publication).is_file())
        size = self.file(publication).stat().st_size + self.wave(publication).stat().st_size

        before_history, before_disk = self.history(), self.disk()
        dry = self.retire(execution, apply=False)
        self.assertEqual((dry.action, dry.reasons, dry.bytes_reclaimed), ("eligible", (), 0))
        self.assertEqual(dry.bytes_reclaimable, self.file(publication).stat().st_size)
        self.assertEqual((self.history(), self.disk()), (before_history, before_disk))   # a dry run changes nothing

        outcome = self.retire(execution)
        self.assertEqual((outcome.action, outcome.reasons, outcome.bytes_reclaimed), ("retired", (), size))
        self.assertFalse(self.file(publication).exists())
        self.assertFalse(self.wave(publication).exists())
        self.assertFalse(self.tracks(publication).exists())
        after = self.history()
        after_events = after.pop("events")
        before_events = before_history.pop("events")
        self.assertEqual(after, before_history)                  # every record and snapshot survives
        self.assertTrue(set(before_events) <= set(after_events))  # append-only: nothing rewritten
        [added] = sorted(set(after_events) - set(before_events))
        self.assertIn('"event": "publication_retired"', added)
        [event] = ActiveBroadcastMessageEvent.objects.filter(execution=execution, event=retention.RETIRED_EVENT)
        self.assertEqual((event.actor_username, event.detail["track"], event.detail["sha256"]),
                         ("admin", publication.track_id_snapshot, publication.sha256))
        stored = self.stored(execution)
        self.assertEqual((stored.state, stored.confirmed_duration_seconds), ("completed", execution.confirmed_duration_seconds))
        self.assertEqual(PlayEvent.objects.get(pk=play_event.pk).track_title, "Urgent PA")
        self.assertIsNone(LogItem.objects.get(pk=item.pk).track_id)
        self.assertEqual(LogItem.objects.get(pk=item.pk).track_title, "Urgent PA")

    def test_retirement_is_idempotent_and_never_redispatches(self):
        execution, publication, _item = self.completed()
        self.retire(execution)
        commands = len(self.commands)
        again = self.retire(execution)
        self.assertEqual((again.action, again.bytes_reclaimed), ("already_retired", 0))
        self.assertEqual(ActiveBroadcastMessageEvent.objects.filter(event=retention.RETIRED_EVENT).count(), 1)
        with self.captureOnCommitCallbacks(execute=True):
            playout.advance()
        self.assertEqual(len(self.commands), commands)           # no engine command for a retired airing
        self.assertEqual(self.stored(execution).state, "completed")
        report = reconcile.reconcile()
        self.assertEqual(codes(report, "integrity_damage"), [])
        self.assertEqual(report.healthy["publication_retired"], 1)

    # -- protection: nothing live, unsettled or still used is ever retired ------------------------
    def test_live_executions_are_never_retired(self):
        ready, ready_publication = self.published()
        self.assertEqual(self.retire(ready).reasons, ("execution_ready",))
        self.assertTrue(self.file(ready_publication).is_file())

    def assert_protected(self, execution, publication, state):
        self.assertEqual(execution.state, state)
        outcome = self.retire(execution)
        self.assertEqual(outcome.action, "refused")
        self.assertIn(f"execution_{state}", outcome.reasons)        # (plus any open deck/air evidence)
        self.assertTrue(self.file(publication).is_file())
        self.assertTrue(self.tracks(publication).exists())
        self.assertFalse(retention.is_retired(execution.pk))

    def test_an_accepted_item_is_protected_however_long_it_waits(self):
        execution, publication, _item = self.accepted()
        self.assert_protected(execution, publication, "accepted")

    def test_a_claimed_item_is_protected(self):
        execution, publication, _item = self.claimed()
        self.assert_protected(execution, publication, "claimed")

    def test_an_airing_on_air_is_protected(self):
        execution, publication, _item = self.started()
        self.assert_protected(execution, publication, "started")

    def test_terminal_alone_is_not_enough(self):
        execution, publication, item = self.interrupted()
        playlist = Playlist.objects.create(name="Keeps the PA")
        PlaylistItem.objects.create(playlist=playlist, track_id=publication.track_id_snapshot, position=0)
        self.assertEqual(self.retire(execution).reasons, ("track_protected",))
        PlaylistItem.objects.all().delete()
        PlayEvent.objects.filter(log_item_id_snapshot=item.pk).update(duration_evidence_state="active")
        self.assertEqual(self.retire(execution).reasons, ("air_evidence_open",))
        PlayEvent.objects.filter(log_item_id_snapshot=item.pk).delete()
        self.assertEqual(self.retire(execution).reasons, ("deck_claim_unresolved",))
        self.assertTrue(self.file(publication).is_file())

    def test_a_track_that_is_not_the_publication_is_never_deleted(self):
        execution, publication, _item = self.completed()
        Track.objects.filter(pk=publication.track_id_snapshot).update(filepath="/elsewhere/am-other.flac")
        self.assertEqual(self.retire(execution).reasons, ("track_not_publication",))
        self.assertTrue(self.tracks(publication).exists())

    def test_retirement_needs_an_authorized_operator(self):
        execution, publication, _item = self.completed()
        with self.assertRaises(services.NotPermitted):
            retention.retire_publication(execution.pk, operator=self.producer, apply=True)
        with self.assertRaises(services.NotPermitted):
            retention.retire_publication(execution.pk, operator=None, apply=True)
        self.assertTrue(self.file(publication).is_file())
        self.assertFalse(retention.is_retired(execution.pk))

    # -- crash safety --------------------------------------------------------------------------------
    def test_a_failure_before_the_durable_transition_changes_nothing(self):
        execution, publication, _item = self.completed()
        before_history, before_disk = self.history(), self.disk()
        from django.db.models.deletion import Collector
        with mock.patch.object(Collector, "delete", side_effect=RuntimeError("crash")), \
                self.assertRaises(RuntimeError):
            self.retire(execution)                               # the Track deletion fails mid-transaction
        self.assertEqual((self.history(), self.disk()), (before_history, before_disk))
        self.assertFalse(retention.is_retired(execution.pk))

    def test_a_crash_after_the_transition_is_finished_by_the_sweep(self):
        execution, publication, _item = self.completed()
        with mock.patch.object(retention, "_remove_publication_bytes", side_effect=SystemExit("killed")):
            with self.assertRaises(SystemExit):
                self.retire(execution)
        self.assertTrue(retention.is_retired(execution.pk))      # durable state committed first ...
        self.assertTrue(self.file(publication).is_file())        # ... the bytes remain, harmlessly
        report = reconcile.reconcile()
        self.assertIn("retired_bytes_remaining", codes(report, "in_flight"))
        self.assertEqual(codes(report, "integrity_damage"), [])
        dry = retention.finish_retirements()
        self.assertEqual([(o.action, o.bytes_reclaimed) for o in dry.outcomes], [("eligible", 0)])
        self.assertTrue(self.file(publication).is_file())
        done = retention.finish_retirements(apply=True)
        self.assertEqual([o.action for o in done.outcomes], ["removed"])
        self.assertFalse(self.file(publication).exists())
        self.assertFalse(self.wave(publication).exists())
        self.assertEqual(retention.finish_retirements(apply=True).outcomes, [])       # idempotent
        self.assertEqual(reconcile.reconcile().healthy["publication_retired"], 1)

    def test_a_failed_waveform_removal_is_retried(self):
        execution, publication, _item = self.completed()
        real_unlink = os.unlink

        def unlink(path, *args, **kwargs):
            if str(path).endswith(".json"):
                raise PermissionError("denied")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(retention.os, "unlink", side_effect=unlink):
            outcome = self.retire(execution)
        self.assertEqual((outcome.action, outcome.reasons), ("failed", ("waveform_PermissionError",)))
        self.assertFalse(self.file(publication).exists())
        self.assertTrue(self.wave(publication).is_file())
        self.assertEqual([o.action for o in retention.finish_retirements(apply=True).outcomes], ["removed"])
        self.assertFalse(self.wave(publication).exists())

    def test_a_retired_publication_whose_track_reappears_is_refused(self):
        execution, publication, _item = self.completed()
        with mock.patch.object(retention, "_remove_publication_bytes", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                self.retire(execution)
        artist = Track.objects.first().artist if Track.objects.exists() else self.song_artist
        Track.objects.create(id=publication.track_id_snapshot, title="again", artist=artist,
                             filepath=str(self.file(publication)), filename="x.flac")
        self.assertEqual([o.reasons for o in retention.finish_retirements(apply=True).outcomes],
                         [("retired_track_present",)])
        self.assertEqual(self.retire(execution).reasons, ("retired_track_present",))
        self.assertTrue(self.file(publication).is_file())
        self.assertIn("retired_track_present", codes(reconcile.reconcile(), "integrity_damage"))

    # -- reconciliation of publications ----------------------------------------------------------------
    def test_library_pruning_is_expected_and_never_alerts(self):
        from library.views import _delete_track_and_file
        execution, publication, item = self.completed()
        self.assertEqual(_delete_track_and_file(Track.objects.get(pk=publication.track_id_snapshot)), (True, None))
        LogItem.objects.filter(pk=item.pk).delete()
        report = reconcile.reconcile()
        self.assertEqual(codes(report, "expected_pruning"), ["insertion_log_item_pruned", "publication_pruned"])
        self.assertEqual(codes(report, "integrity_damage"), [])
        with mock.patch("monitoring.models.emit_event") as emitted:
            self.assertEqual(reconcile.attention(report), 0)
        emitted.assert_not_called()

    def test_live_publication_damage_is_critical(self):
        execution, publication, _item = self.accepted()
        Track.objects.filter(pk=publication.track_id_snapshot).delete()
        [finding] = [f for f in reconcile.reconcile().findings if f.artifact == "publication"]
        self.assertEqual((finding.code, finding.severity), ("publication_track_missing", "critical"))

    def test_missing_file_wrong_digest_wrong_path_and_missing_waveform(self):
        execution, publication, _item = self.completed()
        path = self.file(publication)
        data = path.read_bytes()
        cases = {
            "publication_file_missing": lambda: path.unlink(),
            "publication_file_digest_mismatch": lambda: path.write_bytes(b"X" + data[1:]),
            "publication_waveform_missing": lambda: self.wave(publication).unlink(),
            "publication_track_path_mismatch": lambda: Track.objects.filter(pk=publication.track_id_snapshot)
            .update(filepath=str(self.library_root / "UrgentPA" / "not-ours.flac")),
        }
        for code, damage in cases.items():
            with self.subTest(code):
                wave = self.wave(publication).read_bytes() if self.wave(publication).exists() else None
                damage()
                report = reconcile.reconcile(deep=True)
                [finding] = [f for f in report.findings if f.artifact == "publication"]
                self.assertEqual((finding.code, finding.category, finding.severity),
                                 (code, "integrity_damage", "warning"))       # terminal: history only
                path.write_bytes(data)
                if wave is not None:
                    self.wave(publication).write_bytes(wave)
                Track.objects.filter(pk=publication.track_id_snapshot).update(filepath=str(path))
        self.assertEqual(codes(reconcile.reconcile(deep=True), "integrity_damage"), [])
        self.assertEqual(path.read_bytes(), data)

    def test_the_metadata_check_never_hashes_and_the_reconciler_writes_nothing(self):
        execution, publication, _item = self.completed()
        before_history, before_disk = self.history(), self.disk()
        from monitoring.models import SystemEvent
        events = SystemEvent.objects.count()
        with mock.patch.object(reconcile, "_file_sha256", side_effect=AssertionError("hashed")), \
                mock.patch("production.services.media_io.verify_integrity", side_effect=AssertionError("hashed")):
            first = reconcile.reconcile()
        second = reconcile.reconcile()
        self.assertEqual(first.signature(), second.signature())   # idempotent
        self.assertEqual((self.history(), self.disk(), SystemEvent.objects.count()),
                         (before_history, before_disk, events))
        self.assertEqual(self.stored(execution).state, "completed")

    # -- interrupted publication leftovers in the reserved namespace ----------------------------------------------
    def leftovers(self, *, age_hours):
        import time
        import uuid as _uuid
        directory = self.library_root / "UrgentPA"
        orphan = directory / f"am-{_uuid.uuid4().hex}.flac"
        orphan.write_bytes(b"orphan" * 100)
        scratch = directory / ".am-publish-abc123"
        scratch.mkdir()
        (scratch / "aggregate.flac").write_bytes(b"partial")
        lock = directory / f".{orphan.name}.publish.lock"
        lock.write_bytes(b"")
        legacy = directory / "legacy-ogremote-mix.flac"
        legacy.write_bytes(b"legacy")
        stamp = time.time() - age_hours * 3600
        for path in (orphan, scratch / "aggregate.flac", scratch, lock, legacy):
            os.utime(path, (stamp, stamp))
        return orphan, scratch, lock, legacy

    def test_interrupted_publication_leftovers_are_swept_only_when_old(self):
        execution, publication, _item = self.completed()
        published = self.file(publication).read_bytes()
        orphan, scratch, lock, legacy = self.leftovers(age_hours=0)
        young = retention.sweep_publication_namespace(apply=True)
        self.assertEqual(young.count("removed"), 0)
        self.assertTrue(orphan.exists() and scratch.exists() and lock.exists())
        from datetime import timedelta
        from django.utils import timezone
        later = timezone.now() + timedelta(hours=25)              # the grace counts from mtime AND ctime
        report = reconcile.reconcile(now=later)
        self.assertEqual(sorted(f.code for f in report.findings if f.category == "candidate_orphan"),
                         ["namespace_orphan_file", "publication_lock_leftover", "publication_scratch_leftover"])
        dry = retention.sweep_publication_namespace(now=later)
        self.assertEqual(sorted(o.artifact for o in dry.outcomes if o.action == "eligible"),
                         ["namespace_file", "publication_lock", "publication_scratch"])
        self.assertTrue(orphan.exists())                              # dry run
        done = retention.sweep_publication_namespace(apply=True, now=later)
        self.assertEqual(done.count("removed"), 3)
        self.assertFalse(orphan.exists() or scratch.exists() or lock.exists())
        self.assertEqual(legacy.read_bytes(), b"legacy")              # never ours, never touched
        self.assertEqual(self.file(publication).read_bytes(), published)   # a valid publication is never touched

    def test_a_ready_executions_placed_file_awaits_adoption(self):
        execution = self.ready_execution()
        placed = self.library_root / playout.relpath_for(execution.pk)
        placed.parent.mkdir(parents=True, exist_ok=True)
        placed.write_bytes(b"interrupted placement")
        from datetime import timedelta
        from django.utils import timezone
        later = timezone.now() + timedelta(hours=25)
        self.assertEqual(retention.sweep_publication_namespace(apply=True, now=later).outcomes[0].reasons,
                         ("in_flight_publication",))
        self.assertTrue(placed.exists())
        [finding] = [f for f in reconcile.reconcile().findings if f.artifact == "namespace_file"]
        self.assertEqual(finding.category, "in_flight")

    def test_symlinks_are_never_followed_or_removed(self):
        execution, publication, _item = self.completed()
        path = self.file(publication)
        target = self.library_root / "victim.flac"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
        report = reconcile.reconcile()
        self.assertIn("publication_file_not_regular", codes(report, "integrity_damage"))
        self.assertEqual(self.retire(execution).reasons, ("file_not_regular",))
        self.assertTrue(target.is_file() and path.is_symlink())

    def test_a_substituted_namespace_or_missing_library_refuses(self):
        execution, publication, _item = self.completed()
        directory = self.library_root / "UrgentPA"
        moved = self.library_root / "UrgentPA.real"
        directory.rename(moved)
        directory.symlink_to(moved)
        self.assertEqual(self.retire(execution).reasons, ("namespace_not_a_directory",))
        with self.assertRaises(retention.RetentionUnavailable):
            retention.sweep_publication_namespace(apply=True)
        directory.unlink()
        moved.rename(directory)
        with self.settings(LIBRARY_ROOT=str(self.library_root / "absent")):
            self.assertEqual(self.retire(execution).reasons, ("library_unavailable",))
            [finding] = [f for f in reconcile.reconcile().findings if f.artifact == "publication"]
            self.assertEqual((finding.code, finding.category), ("library_unavailable", "unknown"))
        self.assertTrue(self.file(publication).is_file())

    def test_a_permission_error_is_a_failure_never_reported_as_reclaimed(self):
        execution, publication, _item = self.completed()
        directory = self.library_root / "UrgentPA"
        directory.chmod(0o555)
        self.addCleanup(directory.chmod, 0o755)
        if os.access(directory, os.W_OK):
            self.skipTest("privileges ignore directory permissions")
        wave_bytes = self.wave(publication).stat().st_size
        outcome = self.retire(execution)
        self.assertEqual((outcome.action, outcome.reasons), ("failed", ("file_PermissionError",)))
        self.assertEqual(outcome.bytes_reclaimed, wave_bytes)          # only what really went (the waveform)
        self.assertTrue(self.file(publication).is_file())
        self.assertTrue(retention.is_retired(execution.pk))
        directory.chmod(0o755)
        self.assertEqual([o.action for o in retention.finish_retirements(apply=True).outcomes], ["removed"])

    # -- attention ------------------------------------------------------------------------------------------
    def test_attention_is_deduplicated_per_condition_code(self):
        execution, publication, _item = self.completed()
        self.wave(publication).unlink()
        report = reconcile.reconcile()
        from monitoring.models import SystemEvent
        before = SystemEvent.objects.count()
        self.assertEqual(reconcile.attention(report), 1)
        self.assertEqual(reconcile.attention(report), 1)
        rows = SystemEvent.objects.order_by("-pk")[:SystemEvent.objects.count() - before]
        self.assertEqual(len({row.title for row in rows}), 1)
        self.assertLessEqual(SystemEvent.objects.count() - before, 1)   # coalesced, not flooded


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class AggregateRetentionTests(Lifecycle, TestCase):
    def unbound(self):
        """A real aggregate whose execution moved on before binding: render it,
        then fail the execution (stale), so the rendition is bound to nothing."""
        self.real_active(self.producer, kind="urgent", seconds=1.0)
        execution = services.materialize_due_execution()
        media = __import__("active_messages.rendering", fromlist=["render"]).render(execution).media
        services.fail_execution(execution, code="render_failed")
        return media

    def test_bound_and_live_aggregates_are_protected(self):
        execution = self.ready_execution()
        mix = execution.mix_media
        self.assertIn("referenced", retention.assess_aggregate_purge(mix, grace=retention.MIN_GRACE,
                                                                     now=mix.created_at + retention.DEFAULT_GRACE).reasons)
        self.assertEqual(retention.purge_unbound_aggregates(apply=True).outcomes, [])
        self.assertTrue(type(mix).objects.get(pk=mix.pk).is_present)
        message_media = execution.composition_snapshot["components"][0]["media"]
        source = type(mix).objects.get(pk=message_media)
        self.assertEqual(retention.assess_aggregate_purge(source).reasons, ("not_active_message_aggregate",))

    def test_an_unbound_aggregate_is_purged_after_the_grace(self):
        from datetime import timedelta
        media = self.unbound()
        young = retention.purge_unbound_aggregates(apply=True)
        self.assertEqual([(o.action, o.reasons) for o in young.outcomes], [("kept", ("too_recent",))])
        later = media.created_at + timedelta(hours=25)
        dry = retention.purge_unbound_aggregates(now=later)
        self.assertEqual([(o.action, o.bytes_reclaimable, o.bytes_reclaimed) for o in dry.outcomes],
                         [("eligible", media.byte_size, 0)])
        self.assertTrue(type(media).objects.get(pk=media.pk).is_present)
        storage = Path(retention._storage_path(media))
        with self.captureOnCommitCallbacks(execute=True):
            done = retention.purge_unbound_aggregates(apply=True, now=later)
            # inside a transaction: purged, bytes not yet removed, nothing claimed as reclaimed
            self.assertEqual([(o.action, o.reasons, o.bytes_reclaimed) for o in done.outcomes],
                             [("purged", ("bytes_removed_on_commit",), 0)])
            self.assertTrue(storage.is_file())
        self.assertFalse(storage.exists())                          # removed when the purge committed
        stored = type(media).objects.get(pk=media.pk)
        self.assertEqual((stored.retention_state, stored.sha256, stored.recipe_params_digest),
                         ("purged", media.sha256, media.recipe_params_digest))   # provenance kept

    def test_no_air_provenance_is_never_laundered(self):
        from production.models import ProductionMedia
        from .support import media as make_media
        make_media(self.producer, airing_prohibited=True)
        self.published()
        prohibited = list(ProductionMedia.objects.filter(airing_prohibited=True).values_list("pk", flat=True))
        refuse = AssertionError("retention never binds")
        with mock.patch("production.services.retention.lock_for_binding", side_effect=refuse), \
                mock.patch("production.services.retention.lock_for_no_air_binding", side_effect=refuse):
            reconcile.reconcile(deep=True)
            retention.sweep_publication_namespace(apply=True)
            retention.purge_unbound_aggregates(apply=True)
            retention.finish_retirements(apply=True)
        self.assertEqual(list(ProductionMedia.objects.filter(airing_prohibited=True).values_list("pk", flat=True)),
                         prohibited)


class ClosedGateTests(TestCase):
    """With the gate closed (production), reconciliation and every cleanup run
    without opening it, queue no engine command and write no air evidence."""

    def test_nothing_airs_or_dispatches(self):
        import tempfile
        from library.models import LogItem as Item
        self.assertIs(services.NATIVE_AIRING_AVAILABLE, False)
        with tempfile.TemporaryDirectory() as root, self.settings(LIBRARY_ROOT=root, WAVEFORMS_DIR=root):
            os.mkdir(os.path.join(root, "UrgentPA"))
            counts = (Item.objects.count(), PlayEvent.objects.count())
            with mock.patch("isadoraair.engine_commands.enqueue_engine_command",
                            side_effect=AssertionError("no engine command")):
                reconcile.reconcile(deep=True)
                retention.sweep_publication_namespace(apply=True)
                retention.finish_retirements(apply=True)
                retention.purge_unbound_aggregates(apply=True)
            self.assertEqual((Item.objects.count(), PlayEvent.objects.count()), counts)
        self.assertIs(services.NATIVE_AIRING_AVAILABLE, False)
        from active_messages.models import ActiveBroadcastPolicy
        self.assertFalse(ActiveBroadcastPolicy.objects.exists())        # never even created the policy row


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class ReconcileMediaTests(Lifecycle, TestCase):
    def storage(self, media):
        return Path(retention._storage_path(media))

    def finding(self, report, artifact):
        [found] = [f for f in report.findings if f.artifact == artifact]
        return found.code, found.severity, found.category

    def test_a_live_aggregate_whose_bytes_vanished_is_critical(self):
        execution = self.ready_execution()
        self.assertEqual(codes(reconcile.reconcile(), "integrity_damage"), [])
        self.storage(execution.mix_media).unlink()
        self.assertEqual(self.finding(reconcile.reconcile(), "execution_mix"),
                         ("mix_media_bytes_inconsistent", "critical", "integrity_damage"))

    def test_deep_verification_catches_same_size_corruption(self):
        execution = self.ready_execution()
        path = self.storage(execution.mix_media)
        data = path.read_bytes()
        path.chmod(0o644)
        path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        self.assertEqual(codes(reconcile.reconcile(), "integrity_damage"), [])   # metadata: same size
        self.assertEqual(self.finding(reconcile.reconcile(deep=True), "execution_mix"),
                         ("mix_media_bytes_inconsistent", "critical", "integrity_damage"))

    def test_active_message_media_and_pending_inputs(self):
        message = self.real_active(self.producer, kind="urgent", seconds=1.0)
        execution = services.materialize_due_execution()
        self.assertEqual(execution.state, "pending_render")
        self.storage(message.message_media).unlink()
        report = reconcile.reconcile()
        self.assertEqual(self.finding(report, "message_media"),
                         ("active_message_media_unavailable", "critical", "integrity_damage"))
        self.assertEqual(self.finding(report, "pending_input"),
                         ("pending_input_unavailable", "critical", "integrity_damage"))

    def test_purged_media_still_referenced_is_reported(self):
        from production import transitions
        from django.utils import timezone
        execution = self.ready_execution()
        transitions.mark_purged(execution.mix_media_id, timezone.now())    # a binding-rule violation, simulated
        report = reconcile.reconcile()
        self.assertIn("purged_media_still_referenced", codes(report, "integrity_damage"))
        self.assertIn("mix_media_purged_but_referenced", codes(report, "integrity_damage"))
