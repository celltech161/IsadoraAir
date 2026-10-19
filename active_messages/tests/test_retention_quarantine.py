"""D3E-C final correction: the last pathname operation of a cleanup can never
remove a file other than the one it verified.

The vulnerable sequence verified the publication's file through a descriptor,
re-checked that the PATHNAME still named that inode (``os.stat``), and then
removed the PATHNAME (``os.unlink``). Anything renamed into place between
those two system calls was deleted -- and reported as reclaimed -- while the
verified original survived elsewhere. The substitution here is injected at
exactly that point: after the last identity check, immediately before the
first destructive operation on the publication pathname (whatever that
operation is), so the test fails on the vulnerable candidate.
"""
import os
import unittest
from unittest import mock

from django.db import DatabaseError
from django.test import TestCase

from active_messages import playout, reconcile, retention
from active_messages.models import ActiveBroadcastMessageEvent
from production.tests.support import ffmpeg_available

from .d3ec_support import Lifecycle

try:                                                    # absent from the vulnerable candidate
    from active_messages import quarantine
except ImportError:                                     # pragma: no cover
    quarantine = None

REPLACEMENT = b"unrelated 20 bytes!!"                    # 20 bytes, as the independent reproduction


def substitute_before_destruction(test, path, *, payload=REPLACEMENT):
    """Patch every destructive pathname operation the cleanup could use so
    that, the first time one is about to act on ``path``'s name, the verified
    original is renamed aside and ``payload`` takes its pathname. Returns the
    list recording where the substitution fired."""
    fired = []
    aside = path.with_name("verified-original.aside")

    def substitute(operation):
        if not fired:
            fired.append(operation)
            os.rename(path, aside)
            path.write_bytes(payload)

    real_unlink = os.unlink

    def unlink(target, *args, **kwargs):
        if os.path.basename(os.fsdecode(target)) == path.name:
            substitute("unlink")
        return real_unlink(target, *args, **kwargs)

    patchers = [mock.patch("os.unlink", side_effect=unlink)]
    if quarantine is not None:
        real_move = quarantine._rename_noreplace

        def move(src_dirfd, src, dst_dirfd, dst):
            if src == path.name:
                substitute("rename")
            return real_move(src_dirfd, src, dst_dirfd, dst)

        patchers.append(mock.patch.object(quarantine, "_rename_noreplace", side_effect=move))
    for patcher in patchers:
        patcher.start()
        test.addCleanup(patcher.stop)
    return fired, aside


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class FinalSyscallSubstitutionTests(Lifecycle, TestCase):
    def interrupted_retirement(self):
        execution, publication, _item = self.completed()
        with mock.patch.object(retention, "_remove_publication_bytes", side_effect=SystemExit("killed")):
            with self.assertRaises(SystemExit):
                self.retire(execution)
        return execution, publication

    def test_a_replacement_substituted_after_the_last_identity_check_survives(self):
        execution, publication = self.interrupted_retirement()
        path = self.file(publication)
        original = path.read_bytes()
        wave_size = self.wave(publication).stat().st_size
        events = list(ActiveBroadcastMessageEvent.objects.order_by("id").values_list("id", "event"))
        fired, aside = substitute_before_destruction(self, path)
        report = retention.finish_retirements(apply=True)
        self.assertTrue(fired, "the cleanup performed no destructive operation on the pathname")
        (outcome,) = report.outcomes
        # the replacement was neither deleted nor overwritten: it is back at its pathname
        self.assertTrue(path.is_file(), f"the replacement was deleted ({fired[0]}); outcome {outcome}")
        self.assertEqual(path.read_bytes(), REPLACEMENT)
        # none of the audio was reported as reclaimed (only the unowned waveform); the operator is told why
        self.assertNotEqual(outcome.action, "removed")
        self.assertFalse(self.wave(publication).exists())
        self.assertEqual(report.bytes_reclaimed, wave_size)
        self.assertIn("quarantine_identity_mismatch", outcome.reasons)
        # the verified original is untouched where it was moved; history unchanged
        self.assertEqual(aside.read_bytes(), original)
        self.assertEqual(list(ActiveBroadcastMessageEvent.objects.order_by("id").values_list("id", "event")), events)
        self.assertTrue(retention.is_retired(execution.pk))


def crash_at(stage):
    """Make the cleanup die at ``stage`` (SystemExit escapes like a kill)."""
    def checkpoint(current):
        if current == stage:
            raise SystemExit(f"killed at {stage}")
    return mock.patch.object(quarantine, "_checkpoint", side_effect=checkpoint)


def run(*args):
    import io

    from django.core.management import call_command
    out = io.StringIO()
    call_command("active_message_retention", *args, stdout=out, stderr=io.StringIO())
    return out.getvalue()


def writes(queries):
    return [q["sql"] for q in queries if q["sql"].lstrip().split(" ", 1)[0].upper() in ("INSERT", "UPDATE", "DELETE")]


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class QuarantineLifecycleTests(Lifecycle, TestCase):
    """The quarantine through the real retention paths (real publications)."""

    def interrupted_retirement(self):
        return FinalSyscallSubstitutionTests.interrupted_retirement(self)

    def history(self):
        return list(ActiveBroadcastMessageEvent.objects.order_by("id").values_list("id", "event"))

    def pending(self):
        """A retirement whose cleanup died right after moving the verified file aside."""
        execution, publication = self.interrupted_retirement()
        with crash_at("after_move"), self.assertRaises(SystemExit):
            retention.finish_retirements(apply=True)
        self.assertFalse(self.file(publication).exists())
        return execution, publication

    def test_an_interrupted_cleanup_is_visible_read_only_and_finished_by_the_sweep(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        execution, publication = self.pending()
        size = publication.media.byte_size
        events = self.history()
        before = self.disk()
        with CaptureQueriesContext(connection) as queries:
            report = reconcile.reconcile()
            output = run()
            dry = run("--sweep")
        self.assertEqual(writes(queries), [])                        # diagnostics and dry runs write nothing
        self.assertEqual(self.disk(), before)
        codes = [f.code for f in report.findings]
        self.assertIn("quarantine_pending", codes)
        self.assertIn("retired_bytes_remaining", codes)               # the retirement is visibly incomplete
        self.assertEqual(report.bytes["quarantine_reclaimable"], size)
        self.assertIn("quarantine_reclaimable", output)
        self.assertIn("reclaimable (not reclaimed)", dry)
        wave_size = self.wave_size(publication)                      # not reached by the crashed run
        self.assertGreater(wave_size, 0)
        lines = {line.split(":", 1)[0]: line for line in run("--sweep", "--apply").splitlines() if ":" in line}
        self.assertIn(f"removed 1; reclaimed {size} bytes", lines["interrupted cleanups (quarantine)"])
        self.assertIn(f"removed 1; reclaimed {wave_size} bytes", lines["retired publication bytes"])
        self.assertEqual(self.history(), events)                     # no event added, none rewritten
        self.assertNotIn("quarantine_pending", [f.code for f in reconcile.reconcile().findings])
        self.assertEqual(retention.recover_quarantine(apply=True).outcomes, [])
        self.assertEqual(retention.finish_retirements(apply=True).outcomes, [])   # nothing counted twice

    def wave_size(self, publication):
        return self.wave(publication).stat().st_size if self.wave(publication).exists() else 0

    def test_finish_retirements_settles_a_pending_operation_once(self):
        execution, publication = self.pending()
        size = publication.media.byte_size + self.wave_size(publication)
        report = retention.finish_retirements(apply=True)
        self.assertEqual([(o.action, o.bytes_reclaimed) for o in report.outcomes], [("removed", size)])
        self.assertEqual(retention.finish_retirements(apply=True).outcomes, [])

    def test_a_track_registered_while_the_file_is_held_gets_its_file_back(self):
        from library.models import Track
        execution, publication = self.pending()
        path = self.file(publication)
        with playout._destination_lock(path):
            track = Track.objects.create(title="Adopted", artist=self.song_artist, filepath=str(path),
                                         filename=path.name)
        outcomes = retention.recover_quarantine(apply=True).outcomes
        self.assertEqual([(o.action, o.reasons, o.bytes_reclaimed) for o in outcomes],
                         [("restored", ("path_owned_by_track", "path_owned_by_track"), 0)])
        self.assertTrue(path.is_file())                                # its audio is where its Track says
        self.assertTrue(Track.objects.filter(pk=track.pk).exists())

    def test_a_database_failure_during_recovery_keeps_the_file_pending(self):
        execution, publication = self.pending()
        with mock.patch.object(retention, "_path_owners", side_effect=DatabaseError("connection lost")):
            outcomes = retention.recover_quarantine(apply=True).outcomes
        self.assertEqual([(o.action, o.reasons) for o in outcomes],
                         [("failed", ("quarantine_pending", "ownership_unavailable"))])
        self.assertEqual(retention.recover_quarantine(apply=True).count("removed"), 1)

    def test_quarantined_bytes_are_never_imported_by_the_library(self):
        from django.core.management import call_command

        from library.models import Track
        self.pending()
        tracks = Track._base_manager.count()
        import io
        call_command("import_songs", str(self.library_root), stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(Track._base_manager.filter(filepath__contains=quarantine.ROOT).count(), 0)
        self.assertEqual(Track._base_manager.count(), tracks)

    def test_a_hard_linked_publication_file_is_never_counted(self):
        execution, publication = self.interrupted_retirement()
        os.link(self.file(publication), self.library_root / "elsewhere.flac")
        (outcome,) = retention.finish_retirements(apply=True).outcomes
        self.assertEqual((outcome.action, outcome.reasons), ("failed", ("file_hard_linked",)))
        self.assertTrue(self.file(publication).is_file())

    def test_a_waveform_replacement_survives(self):
        execution, publication = self.interrupted_retirement()
        wave = self.wave(publication)
        original = wave.read_bytes()
        aside = wave.with_name("verified-waveform.aside")
        real = quarantine._rename_noreplace

        def move(src_dirfd, src, dst_dirfd, dst):
            if src == wave.name:
                os.rename(wave, aside)
                wave.write_bytes(REPLACEMENT)
            return real(src_dirfd, src, dst_dirfd, dst)

        with mock.patch.object(quarantine, "_rename_noreplace", side_effect=move):
            (outcome,) = retention.finish_retirements(apply=True).outcomes
        self.assertEqual(outcome.reasons, ("waveform_quarantine_identity_mismatch",))
        self.assertEqual(outcome.bytes_reclaimed, publication.media.byte_size)   # only the audio's proven bytes
        self.assertEqual((wave.read_bytes(), aside.read_bytes()), (REPLACEMENT, original))

    def test_an_orphan_replacement_survives(self):
        from datetime import timedelta

        from django.utils import timezone

        from active_messages import playout as playout_module
        from active_messages import services
        from production.services import media_io
        execution = self.ready_execution()
        message = execution.composition_snapshot["components"][0]["message"]
        placed = self.library_root / playout_module.relpath_for(execution.pk)
        placed.parent.mkdir(parents=True, exist_ok=True)
        opened = media_io.open_media(execution.mix_media)
        try:
            placed.write_bytes(opened.file.read())
        finally:
            opened.close()
        services.clear_message(message, actor=self.admin)
        fired, aside = substitute_before_destruction(self, placed)
        done = retention.sweep_publication_namespace(apply=True, now=timezone.now() + timedelta(hours=25))
        (outcome,) = [o for o in done.outcomes if o.artifact == "namespace_file"]
        self.assertEqual((outcome.action, outcome.reasons, outcome.bytes_reclaimed),
                         ("refused", ("quarantine_identity_mismatch",), 0))
        self.assertEqual(placed.read_bytes(), REPLACEMENT)
        self.assertTrue(aside.is_file())

    def test_a_track_pointing_into_the_quarantine_keeps_the_payload(self):
        from django.db import models

        from library.models import Track
        execution, publication = self.pending()
        (operation,) = os.listdir(self.library_root / "UrgentPA" / quarantine.ROOT)
        payload = self.library_root / "UrgentPA" / quarantine.ROOT / operation / quarantine.PAYLOAD
        models.QuerySet(Track).bulk_create([Track(title="Explicit", artist=self.song_artist,
                                                  filepath=str(payload), filename="payload")])
        outcomes = retention.recover_quarantine(apply=True).outcomes
        self.assertEqual([(o.action, o.reasons) for o in outcomes],
                         [("failed", ("quarantine_pending", "quarantine_owned_by_track"))])
        self.assertTrue(payload.is_file())
