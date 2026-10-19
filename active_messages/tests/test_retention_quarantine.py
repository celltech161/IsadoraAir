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
import stat
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


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class QuarantineOwnershipRegressionTests(Lifecycle, TestCase):
    """Independent review: a legitimate Track whose ``waveform_path`` refers to
    a quarantined payload (its ``filepath`` unrelated) must keep it."""

    pending = QuarantineLifecycleTests.pending
    interrupted_retirement = FinalSyscallSubstitutionTests.interrupted_retirement

    def payload(self, directory=None):
        root = (directory or self.library_root / "UrgentPA") / quarantine.ROOT
        (operation,) = os.listdir(root)
        return root / operation / quarantine.PAYLOAD

    def referencing_track(self, **fields):
        """Through the ordinary ORM when the guard allows it; otherwise as a
        pre-existing (legacy) row the guard never saw."""
        from django.db import models

        from library.models import Track, TrackWriteRefused
        self._unrelated = getattr(self, "_unrelated", 0) + 1
        audio = self.library_root / "Unrelated" / f"unrelated-{self._unrelated}.wav"
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"RIFF unrelated audio")
        values = dict(title="Unrelated", artist=self.song_artist, filepath=str(audio), filename=audio.name)
        values.update(fields)
        try:
            return Track.objects.create(**values), "orm"
        except TrackWriteRefused:
            (track,) = models.QuerySet(Track).bulk_create([Track(**values)])
            return track, "legacy"

    def snapshot(self, track):
        from library.models import Track
        return Track._base_manager.filter(pk=track.pk).values("pk", "filepath", "waveform_path").get()

    def test_a_waveform_path_reference_to_the_payload_keeps_it(self):
        execution, publication = self.pending()
        payload = self.payload()
        data = payload.read_bytes()
        track, _how = self.referencing_track(waveform_path=str(payload))
        row = self.snapshot(track)
        report = retention.recover_quarantine(apply=True)
        outcomes = [(o.action, o.reasons, o.bytes_reclaimed) for o in report.outcomes]
        self.assertTrue(payload.is_file(), f"the referenced payload was deleted: {outcomes}")
        self.assertEqual(payload.read_bytes(), data)
        self.assertEqual(report.bytes_reclaimed, 0)
        ((action, reasons, _bytes),) = outcomes
        self.assertEqual(action, "failed")
        self.assertIn("quarantine_owned_by_track", reasons)
        self.assertEqual(self.snapshot(track), row)                      # the Track is untouched
        self.assertTrue(retention.is_retired(execution.pk))

    # -- helpers ------------------------------------------------------------------------------------------
    def legacy_track(self, **fields):
        """A pre-existing reference the write guard never saw (Django's base QuerySet)."""
        from django.db import models

        from library.models import Track
        self._legacy = getattr(self, "_legacy", 0) + 1
        values = dict(title=f"Legacy {self._legacy}", artist=self.song_artist,
                      filepath=str(self.library_root / "Legacy" / f"legacy-{self._legacy}.wav"),
                      filename=f"legacy-{self._legacy}.wav")
        values.update(fields)
        (track,) = models.QuerySet(Track).bulk_create([Track(**values)])
        return track

    def pending_waveform(self):
        """A retirement whose WAVEFORM cleanup died right after moving it aside (the audio went)."""
        execution, publication = self.interrupted_retirement()
        seen = []

        def checkpoint(stage):
            if stage == "after_move":
                seen.append(stage)
                if len(seen) == 2:
                    raise SystemExit("killed during the waveform's quarantine")
        with mock.patch.object(quarantine, "_checkpoint", side_effect=checkpoint), self.assertRaises(SystemExit):
            retention.finish_retirements(apply=True)
        return execution, publication, self.payload(self.waveforms)

    def assert_kept(self, payload, *tracks, reason="quarantine_owned_by_track"):
        data = payload.read_bytes()
        rows = [self.snapshot(track) for track in tracks]
        for _attempt in range(2):                                        # repeated recovery is harmless
            report = retention.recover_quarantine(apply=True)
            self.assertEqual([(o.action, o.reasons, o.bytes_reclaimed) for o in report.outcomes],
                             [("failed", ("quarantine_pending", reason), 0)])
            self.assertEqual(payload.read_bytes(), data)
        self.assertEqual([self.snapshot(track) for track in tracks], rows)

    def assert_removed(self, payload):
        size = payload.stat().st_size
        report = retention.recover_quarantine(apply=True)
        self.assertEqual([(o.action, o.bytes_reclaimed) for o in report.outcomes], [("removed", size)])
        self.assertFalse(payload.exists())
        self.assertEqual(retention.recover_quarantine(apply=True).outcomes, [])

    # -- existing ownership: every reference keeps the payload ----------------------------------------
    def assert_ownership_case(self, fields):
        execution, publication = self.pending()
        payload = self.payload()
        track = self.legacy_track(**fields(payload))
        self.assertNotEqual(track.pk, publication.track_id_snapshot)       # any Track id, not the snapshot
        self.assert_kept(payload, track)
        from library.models import Track
        Track.objects.get(pk=track.pk).delete()                           # an ordinary library deletion ...
        self.assert_removed(payload)                                       # ... then it is reclaimed, once

    def test_ownership_through_filepath(self):
        self.assert_ownership_case(lambda p: dict(filepath=str(p)))

    def test_ownership_through_waveform_path_with_an_unrelated_filepath(self):
        self.assert_ownership_case(lambda p: dict(waveform_path=str(p)))

    def test_ownership_through_both_fields(self):
        self.assert_ownership_case(lambda p: dict(filepath=str(p), waveform_path=str(p)))

    def test_ownership_through_a_file_symlink_waveform_path(self):
        self.assert_ownership_case(lambda p: dict(waveform_path=str(self.link_to(p))))

    def test_ownership_through_a_path_with_dot_and_dot_dot(self):
        self.assert_ownership_case(lambda p: dict(waveform_path=f"{p.parent}/../{p.parent.name}/./{p.name}"))

    def test_ownership_through_a_symlinked_parent_directory(self):
        alias_dir = self.library_root.parent / "alias-of-urgentpa"
        alias_dir.symlink_to(self.library_root / "UrgentPA")
        self.assert_ownership_case(
            lambda p: dict(waveform_path=str(alias_dir / quarantine.ROOT / p.parent.name / p.name)))

    def link_to(self, target):
        self._links = getattr(self, "_links", 0) + 1
        link = self.library_root.parent / f"link-{self._links}.json"
        link.symlink_to(target)
        return link

    def test_a_quarantined_waveform_referenced_by_a_track_is_kept(self):
        execution, publication, payload = self.pending_waveform()
        for fields in (dict(waveform_path=str(payload)), dict(waveform_path=str(self.link_to(payload)))):
            with self.subTest(fields=list(fields)):
                track = self.legacy_track(**fields)
                self.assert_kept(payload, track)
                from library.models import Track
                Track.objects.filter(pk=track.pk).update(waveform_path="")   # the reference is cleared
        self.assert_removed(payload)

    def test_a_track_referring_to_the_former_waveform_name_gets_it_back(self):
        execution, publication, payload = self.pending_waveform()
        wave = self.wave(publication)
        data = payload.read_bytes()
        self.legacy_track(waveform_path=str(wave))
        (outcome,) = retention.recover_quarantine(apply=True).outcomes
        self.assertEqual((outcome.action, outcome.bytes_reclaimed), ("restored", 0))
        self.assertEqual(wave.read_bytes(), data)

    # -- non-ownership: no false owner by name, content or neighbourhood --------------------------------
    def test_unrelated_references_are_not_ownership(self):
        execution, publication = self.pending()
        payload = self.payload()
        same_name = self.library_root / "Elsewhere" / quarantine.PAYLOAD
        same_name.parent.mkdir()
        same_name.write_bytes(payload.read_bytes())                      # same basename AND same contents
        native_twin = self.library_root / "Elsewhere" / self.file(publication).name
        native_twin.write_bytes(payload.read_bytes())
        own_wave = self.waveforms / "999999.json"
        own_wave.write_bytes(b"{}")
        from library.models import Track
        for label, fields in {"empty waveform": dict(waveform_path=""),
                              "ordinary waveform": dict(waveform_path=str(own_wave)),
                              "same basename and contents": dict(waveform_path=str(same_name)),
                              "same native name elsewhere": dict(filepath=str(native_twin))}.items():
            self.referencing_track(**fields)                              # the ordinary ORM allows these
        self.song("An unrelated song")
        self.assert_removed(payload)
        self.assertTrue(same_name.is_file() and native_twin.is_file() and own_wave.is_file())
        self.assertGreaterEqual(Track._base_manager.count(), 5)

    # -- fail closed ----------------------------------------------------------------------------------
    def test_unresolvable_track_paths_keep_the_payload(self):
        execution, publication = self.pending()
        payload = self.payload()
        loop = self.library_root.parent / "loop.json"
        loop.symlink_to(loop)
        locked = self.library_root.parent / "locked"
        locked.mkdir()
        (locked / "x.json").write_bytes(b"{}")
        for label, value in {"symlink loop": str(loop), "permission denied": str(locked / "x.json")}.items():
            with self.subTest(label):
                track = self.legacy_track(waveform_path=value)
                locked.chmod(0o000)
                try:
                    if os.access(locked, os.R_OK | os.X_OK) and label == "permission denied":
                        self.skipTest("privileges ignore directory permissions")
                    self.assert_kept(payload, track, reason="ownership_unverifiable")
                finally:
                    locked.chmod(0o755)
                from library.models import Track
                Track._base_manager.filter(pk=track.pk).delete()
        self.assert_removed(payload)

    def test_a_database_failure_while_scanning_references_keeps_the_payload(self):
        execution, publication = self.pending()
        payload = self.payload()
        with mock.patch.object(retention, "track_references", side_effect=DatabaseError("connection lost")):
            self.assert_kept(payload, reason="ownership_unavailable")
        self.assert_removed(payload)

    def test_inconsistent_intent_metadata_never_permits_deletion(self):
        import json
        execution, publication = self.pending()
        payload = self.payload()
        manifest = payload.with_name(quarantine.MANIFEST)
        record = json.loads(manifest.read_text())
        data = payload.read_bytes()
        for label, change in {"another inode": dict(ino=record["ino"] + 1), "another name": dict(name="am-x.flac")}.items():
            with self.subTest(label):
                manifest.write_text(json.dumps({**record, **change}))
                outcomes = [(o.action, o.bytes_reclaimed) for o in retention.recover_quarantine(apply=True).outcomes]
                self.assertNotIn("removed", [action for action, _bytes in outcomes])
                restored = self.file(publication)
                if restored.exists():                                     # a mismatch is put back, never deleted
                    self.assertEqual(restored.read_bytes(), data)
                    return
                self.assertEqual(payload.read_bytes(), data)

    def test_an_unexpected_object_as_payload_is_kept(self):
        execution, publication = self.pending()
        payload = self.payload()
        payload.rename(self.library_root.parent / "moved-away")
        os.mkfifo(payload)
        (outcome,) = retention.recover_quarantine(apply=True).outcomes
        self.assertEqual((outcome.action, outcome.reasons, outcome.bytes_reclaimed),
                         ("failed", ("quarantine_recovery_required",), 0))
        self.assertTrue(stat.S_ISFIFO(os.lstat(payload).st_mode))          # kept, never moved or deleted
        self.assertFalse(self.file(publication).exists())


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class QuarantineClaimGuardTests(Lifecycle, TestCase):
    """No supported Track writer can create a new reference into a quarantine,
    by either field, on any ORM path, through any alias; claims of a
    publication's waveform name by another Track need its destination lock."""

    pending = QuarantineLifecycleTests.pending
    interrupted_retirement = FinalSyscallSubstitutionTests.interrupted_retirement
    payload = QuarantineOwnershipRegressionTests.payload

    def test_every_orm_path_refuses_a_claim_into_the_quarantine(self):
        from library.models import Track, TrackWriteRefused
        execution, publication = self.pending()
        payload = self.payload()
        alias = self.library_root.parent / "alias.json"
        alias.symlink_to(payload)
        song = self.song("Ordinary")
        for field in ("filepath", "waveform_path"):
            for value in (str(payload), str(alias), f"{payload.parent}/../{payload.parent.name}/{payload.name}"):
                values = dict(title="Claim", artist=self.song_artist, filepath=str(self.library_root / "c.wav"),
                              filename="c.wav")
                values[field] = value
                with self.subTest(field=field, value=value):
                    with self.assertRaises(TrackWriteRefused):
                        Track.objects.create(**values)
                    with self.assertRaises(TrackWriteRefused):
                        Track.objects.update_or_create(title="Claim", defaults=values)
                    with self.assertRaises(TrackWriteRefused):
                        Track.objects.bulk_create([Track(**values)])
                    with self.assertRaises(TrackWriteRefused):
                        Track.objects.filter(pk=song.pk).update(**{field: value})
                    claimed = Track.objects.get(pk=song.pk)
                    setattr(claimed, field, value)
                    with self.assertRaises(TrackWriteRefused):
                        claimed.save()
                    with self.assertRaises(TrackWriteRefused):
                        Track.objects.bulk_update([claimed], [field])
        self.assertFalse(Track._base_manager.filter(waveform_path__contains=quarantine.ROOT).exists())
        self.assertTrue(payload.is_file())

    def test_ordinary_waveforms_are_untouched(self):
        from library.models import Track
        song = self.song("Ordinary")
        own = self.waveforms / f"{song.pk}.json"
        Track.objects.filter(pk=song.pk).update(waveform_path=str(own))   # every analysis does this
        song.refresh_from_db()
        song.waveform_path = str(self.waveforms / "elsewhere.json")
        song.save()
        song.title = "Edited"
        song.save()
        self.assertEqual(Track.objects.get(pk=song.pk).title, "Edited")

    def test_claiming_a_publication_waveform_name_needs_its_lock(self):
        from library.models import Track, TrackWriteRefused
        execution, publication, _item = self.completed()
        wave = str(self.wave(publication))
        song = self.song("Another Track")
        with self.assertRaises(TrackWriteRefused):
            Track.objects.filter(pk=song.pk).update(waveform_path=wave)
        with self.assertRaises(TrackWriteRefused):
            Track.objects.create(title="x", artist=self.song_artist, filepath=str(self.library_root / "x.wav"),
                                 filename="x.wav", waveform_path=wave)
        # the publication's own Track re-analysed: its own name, untouched
        Track.objects.filter(pk=publication.track_id_snapshot).update(waveform_path=wave)
        with playout._destination_lock(self.file(publication)):
            Track.objects.filter(pk=song.pk).update(waveform_path=wave)
        self.assertEqual(Track.objects.get(pk=song.pk).waveform_path, wave)

    def test_an_existing_reference_is_not_a_new_claim(self):
        from django.db import models

        from library.models import Track
        execution, publication = self.pending()
        (track,) = models.QuerySet(Track).bulk_create([Track(
            title="Legacy", artist=self.song_artist, filepath=str(self.library_root / "legacy.wav"),
            filename="legacy.wav", waveform_path=str(self.payload()))])
        track.title = "Renamed"
        track.save()                                                       # other columns stay editable
        self.assertEqual(Track.objects.get(pk=track.pk).title, "Renamed")
