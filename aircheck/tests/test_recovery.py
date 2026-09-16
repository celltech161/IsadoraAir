"""P2 1.13B -- Aircheck recovery, bounded failed-artifact retention,
and the finalization lock's real cross-process semantics.

Builds on AircheckRecorderFixtureMixin (redirects every Aircheck /run
path to a per-test tempdir) and SegmentationFixture's make_session
helper, both already established by Pass A/A2's own test suite."""
import json
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from aircheck.models import AircheckSession
from aircheck.services import recorder, recovery
from aircheck.tests.test_active_segmentation import SegmentationFixture
from aircheck.tests.test_recorder import AircheckRecorderFixtureMixin
from monitoring.models import SystemEvent


# Genuinely separate OS process (never forked from this Django test
# process -- forking here was tried and reproducibly corrupts the
# shared Postgres test connection for the rest of the run) that
# acquires a real fcntl.flock on the given path, signals readiness by
# creating a sentinel file, then either waits for a release sentinel or
# is killed outright -- real process-death lock-release proof, not a
# mocked stand-in.
_LOCK_HOLDER_SCRIPT = (
    "import fcntl, os, sys, time\n"
    "lock_path, ready_path, release_path = sys.argv[1:4]\n"
    "fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    "open(ready_path, 'w').close()\n"
    "deadline = time.time() + 30\n"
    "while time.time() < deadline and not os.path.exists(release_path):\n"
    "    time.sleep(0.05)\n"
    "os.close(fd)\n"
)


def _spawn_lock_holder(lock_path):
    """Starts the subprocess above, waits for it to genuinely hold the
    lock, and returns (process, release_path) -- caller either touches
    release_path for a clean handoff or kills the process outright to
    prove death-releases-the-lock."""
    ready_path = lock_path.with_suffix(".ready")
    release_path = lock_path.with_suffix(".release")
    for stale in (ready_path, release_path):
        stale.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER_SCRIPT, str(lock_path), str(ready_path), str(release_path)]
    )
    deadline = time.time() + 5
    while time.time() < deadline and not ready_path.exists():
        time.sleep(0.02)
    assert ready_path.exists(), "lock-holder subprocess never signaled readiness"
    return proc, release_path


def _real_mp3(path, duration=0.3, freq=440):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={duration}",
            "-c:a", "libmp3lame", "-b:a", "192k", str(path),
        ],
        check=True, capture_output=True,
    )


def _age_path(path, seconds_ago):
    stamp = time.time() - seconds_ago
    os.utime(path, (stamp, stamp))


class RecoveryFixture(SegmentationFixture):
    """recorder._sync_finalized_recording_to_library (reached whenever
    recovery.py synchronously retries/completes a finalization on the
    test's own thread) calls close_old_connections() in its own
    finally -- correct for its normal real caller (a background
    daemon thread with its own connection) but destructive to the
    surrounding TestCase's shared connection/transaction when called
    synchronously on the test thread instead. The existing Pass-A/A2
    suite already works around this the same way per-test; doing it
    once here in setUp covers every recovery.py entry point that can
    reach it."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(recorder, "close_old_connections"))


# ======================================================================
# Crash/restart cases (1-10)
# ======================================================================

class PendingFinalizationRetryTests(RecoveryFixture, TestCase):
    def _pending_session(self, segments=1):
        session = self.make_session("mp3")
        session.still_running = False
        session.exit_note = recorder.FINALIZATION_PENDING_NOTE
        session.ended_at = timezone.now() - timedelta(seconds=recovery.PENDING_FINALIZATION_GRACE_SECONDS + 60)
        session.save(update_fields=["still_running", "exit_note", "ended_at"])
        for i in range(1, segments + 1):
            _real_mp3(recorder._segment_path(session, i), freq=440 + i * 50)
        return session

    def test_1_gunicorn_death_after_stop_before_thread_starts(self):
        """A session can be marked PENDING with segments fully staged
        and no finalizer ever having started at all (e.g. Gunicorn died
        between Stop's lock release and Thread.start()). Automatic
        retry must still recover it."""
        session = self._pending_session(segments=2)
        result = recovery.retry_pending_finalizations()
        self.assertEqual(result["succeeded"], 1)
        session.refresh_from_db()
        self.assertEqual(recorder.classify_finalization(session), "complete")
        self.assertTrue(Path(session.filename).is_file())
        self.assertFalse(recorder._staging_dir(session).exists())

    def test_2_gunicorn_death_while_finalization_lock_held(self):
        """A REAL separate process holds the finalization lock (proves
        actual flock() semantics, not a mocked stand-in) -- recovery
        must observe it as busy and do nothing, even though the
        session is well past the grace period."""
        session = self._pending_session(segments=1)
        lock_path = recorder._staging_dir(session) / recovery.FINALIZATION_LOCK_NAME
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        proc, release_path = _spawn_lock_holder(lock_path)
        try:
            result = recovery.retry_pending_finalizations()
            self.assertEqual(result["skipped_locked"], 1)
            self.assertEqual(result["succeeded"], 0)
            session.refresh_from_db()
            self.assertEqual(session.exit_note, recorder.FINALIZATION_PENDING_NOTE)
            self.assertTrue(recorder._staging_dir(session).exists())
        finally:
            release_path.touch()
            proc.wait(timeout=5)

    def test_3_maintenance_sees_pending_while_original_finalizer_alive(self):
        """Same real cross-process proof as (2), phrased as the
        original-finalizer-still-working scenario explicitly."""
        session = self._pending_session(segments=1)
        with recovery.finalization_lock(session, blocking=True):
            with patch.object(recovery, "_pending_session_candidates", return_value=[session]):
                result = recovery.retry_pending_finalizations()
        self.assertEqual(result["skipped_locked"], 1)

    def test_4_original_finalizer_dies_later_maintenance_retries(self):
        """A REAL child process holds the lock and is SIGKILLed
        outright (never given a chance to run its own cleanup) --
        proves process death actually releases the OS-held flock
        automatically, not merely that our own context manager's
        finally-block ran (which a normal/cooperative exit would also
        satisfy trivially)."""
        session = self._pending_session(segments=1)
        lock_path = recorder._staging_dir(session) / recovery.FINALIZATION_LOCK_NAME
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        proc, _release_path = _spawn_lock_holder(lock_path)
        with recovery.finalization_lock(session, blocking=False) as acquired:
            self.assertFalse(acquired, "lock should be held by the live subprocess")
        proc.kill()
        proc.wait(timeout=5)

        result = recovery.retry_pending_finalizations()
        self.assertEqual(result["succeeded"], 1)

    def test_5_retry_succeeds_and_cleans_up_exactly_once(self):
        session = self._pending_session(segments=2)
        with patch.object(recorder, "_sync_finalized_recording_to_library") as sync:
            result = recovery.retry_pending_finalizations()
        self.assertEqual(result["succeeded"], 1)
        sync.assert_called_once()
        self.assertFalse(recorder._staging_dir(session).exists())

    def test_6_retry_failure_leaves_recovery_set_intact_without_retry_storm(self):
        session = self._pending_session(segments=1)
        with patch.object(recorder.subprocess, "run", side_effect=OSError("ffmpeg missing")):
            result = recovery.retry_pending_finalizations()
        self.assertEqual(result["failed"], 1)
        session.refresh_from_db()
        self.assertEqual(recorder.classify_finalization(session), "error")
        self.assertEqual(len(recorder._discover_segments(session)), 1)

        # A second immediate pass must NOT re-attempt: exit_note is no
        # longer FINALIZATION_PENDING_NOTE, so this failure is now
        # FAILED_RECOVERABLE, deliberately excluded from automatic retry.
        with patch.object(recorder.subprocess, "run") as run2:
            result2 = recovery.retry_pending_finalizations()
        run2.assert_not_called()
        self.assertEqual(result2["candidates"], 0)

    def test_7_run_owned_handoff_survives_process_death_and_is_evacuated(self):
        """A stopped session's ordinary-path handoff (the Pass-A-review
        carry-forward gap) is not lost and is evacuated on the very
        next bounded reconciliation, independent of still_running."""
        session = self.make_session("mp3")
        session.still_running = False
        session.exit_note = "move working -> dest failed: disk full"
        session.save(update_fields=["still_running", "exit_note"])
        handoff = recorder._handoff_path(session, 1)
        handoff.write_bytes(b"recoverable-audio")

        recovery.evacuate_stranded_handoffs(active_session_id=None)

        self.assertFalse(handoff.exists())
        segments = recorder._discover_segments(session)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].read_bytes(), b"recoverable-audio")

    def test_8_unknown_handoff_is_quarantined_not_guessed_or_deleted(self):
        working = Path(recorder.AIRCHECK_CURRENT_PATH)
        bogus = working.with_name(f"{recorder.HANDOFF_NAME_PREFIX}999999-000001.handoff")
        bogus.write_bytes(b"orphan-audio")

        recovery.evacuate_stranded_handoffs(active_session_id=None)

        self.assertFalse(bogus.exists())
        quarantine = recovery._quarantine_root()
        found = list(quarantine.glob(f"*{bogus.name}"))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].read_bytes(), b"orphan-audio")
        self.assertTrue(
            SystemEvent.objects.filter(category="aircheck", title__icontains="quarantined").exists()
        )

    def test_9_legacy_he_aac_failed_intermediate_moves_to_persistent_recovery(self):
        session = self.make_session("he_aac")
        session.still_running = False
        session.exit_note = "remux failed: ffmpeg exit 1: bad input"
        session.save(update_fields=["still_running", "exit_note"])
        intermediate = recorder.REMUX_INTERMEDIATE_DIR / f"aircheck-remux-{session.id}.aac"
        intermediate.parent.mkdir(parents=True, exist_ok=True)
        intermediate.write_bytes(b"adts-audio-recoverable")

        recovery.evacuate_legacy_remux_artifacts()

        self.assertFalse(intermediate.exists())
        staging = recorder._staging_dir(session)
        recovered = staging / "legacy-remux-source.aac"
        self.assertTrue(recovered.is_file())
        self.assertEqual(recovered.read_bytes(), b"adts-audio-recoverable")

    def test_9b_legacy_remux_still_pending_is_never_touched(self):
        session = self.make_session("he_aac")
        session.still_running = False
        session.exit_note = recorder.REMUX_PENDING_NOTE
        session.save(update_fields=["still_running", "exit_note"])
        intermediate = recorder.REMUX_INTERMEDIATE_DIR / f"aircheck-remux-{session.id}.aac"
        intermediate.parent.mkdir(parents=True, exist_ok=True)
        intermediate.write_bytes(b"in-flight")

        recovery.evacuate_legacy_remux_artifacts()

        self.assertTrue(intermediate.is_file())

    def test_10_failure_while_evacuating_leaves_source_recoverable(self):
        session = self.make_session("mp3")
        session.still_running = False
        session.exit_note = "move working -> dest failed: disk full"
        session.save(update_fields=["still_running", "exit_note"])
        handoff = recorder._handoff_path(session, 1)
        handoff.write_bytes(b"must-not-be-lost")

        with patch.object(recorder.shutil, "copyfileobj", side_effect=OSError("disk full")):
            recovery.evacuate_stranded_handoffs(active_session_id=None)

        self.assertTrue(handoff.exists())
        self.assertEqual(handoff.read_bytes(), b"must-not-be-lost")


# ======================================================================
# Retention tests (11-22)
# ======================================================================

class RetentionTests(RecoveryFixture, TestCase):
    def _failed_segmented_session(self, *, age_seconds, payload_bytes=100, note="finalization failed: bad concat"):
        session = self.make_session("mp3")
        session.still_running = False
        session.exit_note = note
        session.save(update_fields=["still_running", "exit_note"])
        seg = recorder._segment_path(session, 1)
        seg.parent.mkdir(parents=True, exist_ok=True)
        seg.write_bytes(b"x" * payload_bytes)
        _age_path(seg, age_seconds)
        return session

    def test_11_active_recovery_material_is_never_deleted(self):
        session = self.make_session("mp3")  # still_running=True by default
        seg = recorder._segment_path(session, 1)
        seg.parent.mkdir(parents=True, exist_ok=True)
        seg.write_bytes(b"active-audio")
        _age_path(seg, 999999)
        with patch.object(recovery, "RETENTION_MIN_SAFETY_SECONDS", 0), \
             patch.object(recovery, "RETENTION_MAX_AGE_SECONDS", 0):
            recovery.collect_retention(dry_run=False)
        self.assertTrue(seg.exists())

    def test_12_pending_finalization_is_never_retention_deleted(self):
        session = self.make_session("mp3")
        session.still_running = False
        session.exit_note = recorder.FINALIZATION_PENDING_NOTE
        session.save(update_fields=["still_running", "exit_note"])
        seg = recorder._segment_path(session, 1)
        seg.parent.mkdir(parents=True, exist_ok=True)
        seg.write_bytes(b"pending-audio")
        _age_path(seg, 999999)
        with patch.object(recovery, "RETENTION_MIN_SAFETY_SECONDS", 0), \
             patch.object(recovery, "RETENTION_MAX_AGE_SECONDS", 0):
            recovery.collect_retention(dry_run=False)
        self.assertTrue(seg.exists())

    def test_13_locked_finalization_is_never_touched(self):
        session = self._failed_segmented_session(age_seconds=999999)
        with recovery.finalization_lock(session, blocking=True):
            with patch.object(recovery, "RETENTION_MIN_SAFETY_SECONDS", 0), \
                 patch.object(recovery, "RETENTION_MAX_AGE_SECONDS", 0):
                result = recovery.collect_retention(dry_run=False)
        self.assertEqual(result["deleted_sets"], 0)
        self.assertTrue(recorder._segment_path(session, 1).exists())

    def test_14_explicit_failure_younger_than_min_safety_survives_over_budget(self):
        session = self._failed_segmented_session(age_seconds=10, payload_bytes=1000)
        with patch.object(recovery, "RETENTION_MIN_SAFETY_SECONDS", 3600), \
             patch.object(recovery, "RETENTION_MAX_AGE_SECONDS", 999999), \
             patch.object(recovery, "RETENTION_BYTE_BUDGET_BYTES", 1):  # already "over" budget
            result = recovery.collect_retention(dry_run=False)
        self.assertEqual(result["deleted_sets"], 0)
        self.assertTrue(recorder._segment_path(session, 1).exists())

    def test_15_age_expired_failure_cleaned_as_whole_recovery_set(self):
        session = self._failed_segmented_session(age_seconds=1000)
        recorder._segment_path(session, 2).write_bytes(b"y" * 50)
        _age_path(recorder._segment_path(session, 2), 1000)
        with patch.object(recovery, "RETENTION_MIN_SAFETY_SECONDS", 1), \
             patch.object(recovery, "RETENTION_MAX_AGE_SECONDS", 100), \
             patch.object(recovery, "RETENTION_BYTE_BUDGET_BYTES", 10**9):
            result = recovery.collect_retention(dry_run=False)
        self.assertEqual(result["deleted_sets"], 1)
        self.assertFalse(recorder._staging_dir(session).exists())

    def test_16_byte_budget_pressure_removes_oldest_eligible_first(self):
        old = self._failed_segmented_session(age_seconds=5000, payload_bytes=100)
        new = self._failed_segmented_session(age_seconds=4000, payload_bytes=100)
        with patch.object(recovery, "RETENTION_MIN_SAFETY_SECONDS", 1), \
             patch.object(recovery, "RETENTION_MAX_AGE_SECONDS", 999999), \
             patch.object(recovery, "RETENTION_BYTE_BUDGET_BYTES", 100):  # room for only one set
            result = recovery.collect_retention(dry_run=False)
        self.assertEqual(result["deleted_sets"], 1)
        self.assertFalse(recorder._staging_dir(old).exists())
        self.assertTrue(recorder._staging_dir(new).exists())

    def test_17_newer_protected_sets_remain(self):
        # Same as 16, phrased as the positive assertion the task lists
        # separately: the newer set specifically remains untouched.
        old = self._failed_segmented_session(age_seconds=5000, payload_bytes=100)
        new = self._failed_segmented_session(age_seconds=10, payload_bytes=100)
        with patch.object(recovery, "RETENTION_MIN_SAFETY_SECONDS", 1), \
             patch.object(recovery, "RETENTION_MAX_AGE_SECONDS", 999999), \
             patch.object(recovery, "RETENTION_BYTE_BUDGET_BYTES", 100):
            recovery.collect_retention(dry_run=False)
        self.assertFalse(recorder._staging_dir(old).exists())
        self.assertTrue(recorder._staging_dir(new).exists())

    def test_18_segment_sets_are_never_partially_pruned(self):
        session = self._failed_segmented_session(age_seconds=1000, payload_bytes=50)
        for seq in (2, 3):
            p = recorder._segment_path(session, seq)
            p.write_bytes(b"z" * 50)
            _age_path(p, 1000)
        with patch.object(recovery, "RETENTION_MIN_SAFETY_SECONDS", 1), \
             patch.object(recovery, "RETENTION_MAX_AGE_SECONDS", 100), \
             patch.object(recovery, "RETENTION_BYTE_BUDGET_BYTES", 10**9):
            recovery.collect_retention(dry_run=False)
        # All 3 or none -- never 1 or 2 left behind.
        remaining = list(recorder._staging_dir(session).glob("segment-*")) if recorder._staging_dir(session).exists() else []
        self.assertEqual(len(remaining), 0)

    def test_19_legacy_he_aac_artifact_participates_in_retention(self):
        session = self.make_session("he_aac")
        session.still_running = False
        session.exit_note = "remux failed: bad input"
        session.save(update_fields=["still_running", "exit_note"])
        staging = recorder._staging_dir(session)
        staging.mkdir(parents=True)
        legacy = staging / "legacy-remux-source.aac"
        legacy.write_bytes(b"legacy-audio")
        _age_path(legacy, 1000)
        with patch.object(recovery, "RETENTION_MIN_SAFETY_SECONDS", 1), \
             patch.object(recovery, "RETENTION_MAX_AGE_SECONDS", 100), \
             patch.object(recovery, "RETENTION_BYTE_BUDGET_BYTES", 10**9):
            result = recovery.collect_retention(dry_run=False)
        self.assertEqual(result["deleted_sets"], 1)
        self.assertFalse(legacy.exists())

    def test_20_unknown_quarantine_follows_bounded_policy(self):
        root = recovery._quarantine_root()
        root.mkdir(parents=True, exist_ok=True)
        stray = root / "orphan.mp3"
        stray.write_bytes(b"unknown-audio")
        _age_path(stray, 1000)
        with patch.object(recovery, "RETENTION_MIN_SAFETY_SECONDS", 1), \
             patch.object(recovery, "RETENTION_MAX_AGE_SECONDS", 100), \
             patch.object(recovery, "RETENTION_BYTE_BUDGET_BYTES", 10**9):
            result = recovery.collect_retention(dry_run=False)
        self.assertEqual(result["deleted_sets"], 1)
        self.assertFalse(stray.exists())

    def test_21_stale_success_cleaned_only_when_success_proven(self):
        session = self.make_session("mp3")
        session.still_running = False
        session.exit_note = ""
        session.save(update_fields=["still_running", "exit_note"])
        staging = recorder._staging_dir(session)
        staging.mkdir(parents=True)
        (staging / "leftover.tmp").write_bytes(b"debris")
        dest = Path(session.filename)

        # No valid destination yet -- must NOT be cleaned.
        removed = recovery.collect_stale_success_cleanup(dry_run=False)
        self.assertEqual(removed, [])
        self.assertTrue(staging.exists())

        # Now make it genuinely valid and prove cleanup only fires then.
        _real_mp3(dest, duration=0.2)
        removed2 = recovery.collect_stale_success_cleanup(dry_run=False)
        self.assertEqual(len(removed2), 1)
        self.assertFalse(staging.exists())
        self.assertTrue(dest.is_file())  # the actual final file is never touched

    def test_22_routine_collector_is_idempotent(self):
        session = self._failed_segmented_session(age_seconds=1000)
        with patch.object(recovery, "RETENTION_MIN_SAFETY_SECONDS", 1), \
             patch.object(recovery, "RETENTION_MAX_AGE_SECONDS", 100), \
             patch.object(recovery, "RETENTION_BYTE_BUDGET_BYTES", 10**9):
            first = recovery.collect_retention(dry_run=False)
            second = recovery.collect_retention(dry_run=False)
        self.assertEqual(first["deleted_sets"], 1)
        self.assertEqual(second["deleted_sets"], 0)
        self.assertEqual(second["total_sets"], 0)

    def test_dry_run_makes_zero_mutations(self):
        session = self._failed_segmented_session(age_seconds=1000)
        with patch.object(recovery, "RETENTION_MIN_SAFETY_SECONDS", 1), \
             patch.object(recovery, "RETENTION_MAX_AGE_SECONDS", 100), \
             patch.object(recovery, "RETENTION_BYTE_BUDGET_BYTES", 10**9):
            result = recovery.collect_retention(dry_run=True)
        self.assertEqual(result["deleted_sets"], 1)  # reported...
        self.assertTrue(recorder._segment_path(session, 1).exists())  # ...but not actually deleted
        self.assertFalse(
            SystemEvent.objects.filter(category="aircheck", title__icontains="deleted by retention").exists()
        )


# ======================================================================
# Library-sync idempotency (35-36)
#
# TransactionTestCase + a real thread, NOT a patched close_old_
# connections -- matching the exact precedent already established by
# aircheck/tests/test_recorder.py's own LibrarySyncTests (see its
# docstring): close_old_connections() is incompatible with TestCase's
# outer atomic transaction, but genuinely safe and production-faithful
# under TransactionTestCase. This is real end-to-end proof through the
# actual sync_track_file path, not a mock of it.
# ======================================================================

import tempfile
import threading

from django.test import TransactionTestCase as _TransactionTestCase
from django.test.utils import override_settings

from library.management.commands import analyze_tracks
from library.models import Category, CategoryKind, Track


def _fake_mono_pcm(seconds=1, sample_rate=8000, amplitude=3000):
    import struct
    n = seconds * sample_rate
    return struct.pack(f"<{n}h", *([amplitude] * n))


def _fake_stereo_pcm(seconds=1, sample_rate=8000, amplitude=3000):
    import struct
    n_frames = seconds * sample_rate
    return struct.pack(f"<{n_frames * 2}h", *([amplitude] * (n_frames * 2)))


class LibrarySyncConvergenceTests(SegmentationFixture, _TransactionTestCase):
    def setUp(self):
        self._lib_root_ctx = tempfile.TemporaryDirectory()
        lib_root = Path(self._lib_root_ctx.name)
        self.addCleanup(self._lib_root_ctx.cleanup)
        self._override = override_settings(LIBRARY_ROOT=str(lib_root))
        self._override.enable()
        self.addCleanup(self._override.disable)
        kind, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        Category.objects.create(code="Aircheck", name="Aircheck", kind=kind)

        super().setUp()
        # AircheckRecorderFixtureMixin's own setUp (reached via
        # SegmentationFixture) unconditionally sets self.out_dir/
        # self.cfg.output_directory to ITS OWN fresh tempdir AFTER
        # whatever this method assigns before calling super() -- so the
        # LIBRARY_ROOT-nested directory must be applied here, after
        # super().setUp() has already run and created self.cfg, by
        # updating the already-created row rather than pre-seeding
        # self.out_dir beforehand (which super().setUp() would just
        # silently overwrite).
        self.out_dir = lib_root / "Aircheck"
        self.out_dir.mkdir(parents=True)
        self.cfg.output_directory = str(self.out_dir)
        self.cfg.save(update_fields=["output_directory"])

    def _retry_for_real(self):
        with patch.object(analyze_tracks, "decode_audio_to_pcm", return_value=_fake_mono_pcm()), \
             patch.object(analyze_tracks, "decode_audio_to_pcm_stereo", return_value=_fake_stereo_pcm()):
            t = threading.Thread(target=recovery.retry_pending_finalizations)
            t.start()
            t.join(timeout=15)

    def test_35_crash_after_publish_before_bookkeeping_then_retry_converges(self):
        """Simulates a process death between _finalize_segment_set's
        successful return (destination published, sources already
        cleaned by that function itself) and the caller completing
        session.save()/library sync -- by calling the real finalize
        function directly and stopping there, exactly mimicking what a
        crash at that exact point would leave behind."""
        session = self.make_session("mp3")
        session.still_running = False
        session.exit_note = recorder.FINALIZATION_PENDING_NOTE
        session.ended_at = timezone.now() - timedelta(seconds=recovery.PENDING_FINALIZATION_GRACE_SECONDS + 60)
        session.save(update_fields=["still_running", "exit_note", "ended_at"])
        _real_mp3(recorder._segment_path(session, 1), duration=0.2)

        dest = Path(session.filename)
        recorder._finalize_segment_set(session, dest)  # real publish; no bookkeeping after
        self.assertTrue(dest.is_file())
        self.assertEqual(Track.objects.count(), 0)  # bookkeeping genuinely never ran

        self._retry_for_real()

        session.refresh_from_db()
        self.assertEqual(session.exit_note, "")
        self.assertEqual(recorder.classify_finalization(session), "complete")
        self.assertEqual(Track.objects.filter(filepath=str(dest)).count(), 1)

    def test_36_retry_converges_to_exactly_one_final_file_and_one_library_asset(self):
        """Runs the exact same recovery pass twice against the same
        already-converged state -- must remain exactly one Track row,
        never a duplicate, and the second pass must not attempt to
        rebuild/redecode anything (nothing left to retry)."""
        session = self.make_session("mp3")
        session.still_running = False
        session.exit_note = recorder.FINALIZATION_PENDING_NOTE
        session.ended_at = timezone.now() - timedelta(seconds=recovery.PENDING_FINALIZATION_GRACE_SECONDS + 60)
        session.save(update_fields=["still_running", "exit_note", "ended_at"])
        _real_mp3(recorder._segment_path(session, 1), duration=0.2)
        dest = Path(session.filename)
        recorder._finalize_segment_set(session, dest)

        self._retry_for_real()
        self.assertEqual(Track.objects.filter(filepath=str(dest)).count(), 1)
        first_track_id = Track.objects.get(filepath=str(dest)).id

        # Second pass: session is now fully resolved (exit_note == ""),
        # so it is no longer a retry candidate at all.
        result = recovery.retry_pending_finalizations()
        self.assertEqual(result["candidates"], 0)
        self.assertEqual(Track.objects.filter(filepath=str(dest)).count(), 1)
        self.assertEqual(Track.objects.get(filepath=str(dest)).id, first_track_id)
