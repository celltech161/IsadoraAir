"""D3E-C review 3: bytes are never removed while ANY Track owns the
publication's path (not only the original Track id); the file must be exactly
the publication's; Track ownership of a reserved path is coordinated with the
destination lock (the Track write guard). Cross-session races:
test_retention_concurrency."""
import io
import unittest
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection, models, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from active_messages import playout, reconcile, retention
from active_messages.models import ActiveBroadcastMessageEvent, ActorKind
from library.models import Track, TrackWriteRefused
from production.tests.support import ffmpeg_available

from .d3ec_support import Lifecycle


def run(*args):
    out = io.StringIO()
    call_command("active_message_retention", *args, stdout=out, stderr=io.StringIO())
    return out.getvalue()


def writes(queries):
    return [q["sql"] for q in queries if q["sql"].lstrip().split(" ", 1)[0].upper() in ("INSERT", "UPDATE", "DELETE")]


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class TrackOwnershipTests(Lifecycle, TestCase):
    """Correction 3: another Track's audio at the publication path is never removed."""

    def interrupted_retirement(self):
        execution, publication, _item = self.completed()
        with mock.patch.object(retention, "_remove_publication_bytes", side_effect=SystemExit("killed")):
            with self.assertRaises(SystemExit):
                self.retire(execution)
        self.assertTrue(retention.is_retired(execution.pk))
        self.assertFalse(self.tracks(publication).exists())
        return execution, publication

    def adopt(self, publication, *, bypass_guard=False):
        """A NEW Track (another id) registered at the publication's exact path."""
        path = str(playout._library_root() / publication.library_relpath)
        values = dict(title="Adopted", artist=self.song_artist, filepath=path, filename=path.rsplit("/", 1)[-1])
        if bypass_guard:                                         # out of contract: proves the cleanup's own check
            (track,) = models.QuerySet(Track).bulk_create([Track(**values)])
            return track
        with playout._destination_lock(playout._library_root() / publication.library_relpath):
            return Track.objects.create(**values)

    def test_a_different_track_at_the_retired_path_keeps_its_audio(self):
        execution, publication = self.interrupted_retirement()
        data = self.file(publication).read_bytes()
        adopted = self.adopt(publication)
        self.assertNotEqual(adopted.pk, publication.track_id_snapshot)
        outcomes = retention.finish_retirements(apply=True).outcomes
        self.assertEqual([(o.action, o.reasons) for o in outcomes], [("refused", ("path_owned_by_track",))])
        self.assertEqual(self.file(publication).read_bytes(), data)          # its audio is untouched
        self.assertTrue(Track.objects.filter(pk=adopted.pk).exists())
        self.assertEqual(self.retire(execution).reasons, ("path_owned_by_track",))
        self.assertIn("retired_path_owned_by_track", [f.code for f in reconcile.reconcile().findings])

    def test_the_cleanups_own_check_holds_even_if_the_guard_were_bypassed(self):
        execution, publication = self.interrupted_retirement()
        self.adopt(publication, bypass_guard=True)
        self.assertEqual([o.reasons for o in retention.finish_retirements(apply=True).outcomes],
                         [("path_owned_by_track",)])
        self.assertTrue(self.file(publication).is_file())

    def test_an_alias_path_to_the_same_file_is_ownership_too(self):
        execution, publication = self.interrupted_retirement()
        alias = self.library_root.parent / "music-alias"
        alias.symlink_to(self.library_root)
        path = str(alias / publication.library_relpath)
        models.QuerySet(Track).bulk_create([Track(title="Alias", artist=self.song_artist, filepath=path,
                                                  filename="alias.flac")])
        self.assertEqual([o.reasons for o in retention.finish_retirements(apply=True).outcomes],
                         [("path_owned_by_track",)])
        self.assertTrue(self.file(publication).is_file())

    def test_no_track_at_the_path_is_genuinely_eligible(self):
        execution, publication = self.interrupted_retirement()
        self.assertEqual([o.action for o in retention.finish_retirements(apply=True).outcomes], ["removed"])
        self.assertFalse(self.file(publication).exists())

    def test_reserved_path_ownership_needs_the_destination_lock(self):
        execution, publication = self.interrupted_retirement()
        path = str(playout._library_root() / publication.library_relpath)
        values = dict(title="Scanner", artist=self.song_artist, filepath=path, filename="x.flac")
        with self.assertRaises(TrackWriteRefused):
            Track.objects.create(**values)                        # an uncoordinated writer is refused
        song = self.song("Ordinary")
        with self.assertRaises(TrackWriteRefused):
            Track.objects.filter(pk=song.pk).update(filepath=path)
        song.filepath = path
        with self.assertRaises(TrackWriteRefused):
            song.save()
        with self.assertRaises(TrackWriteRefused):
            Track.objects.bulk_update([song], ["filepath"])
        Track.objects.filter(pk=song.pk).update(title="Edits elsewhere are untouched")
        self.assertTrue(self.file(publication).is_file())

    def test_a_file_that_is_not_the_publications_is_never_removed(self):
        execution, publication = self.interrupted_retirement()
        self.file(publication).write_bytes(b"someone else's audio")
        self.assertEqual([o.reasons for o in retention.finish_retirements(apply=True).outcomes],
                         [("file_identity_mismatch",)])
        self.assertEqual(self.file(publication).read_bytes(), b"someone else's audio")

    def test_a_file_replaced_between_verification_and_removal_is_kept(self):
        execution, publication = self.interrupted_retirement()
        path = self.file(publication)
        real = retention._digest_fd

        def swap_after_hashing(fd):
            digest = real(fd)
            path.rename(path.with_name("moved-away.flac"))
            path.write_bytes(b"substituted")
            return digest

        with mock.patch.object(retention, "_digest_fd", side_effect=swap_after_hashing):
            outcomes = retention.finish_retirements(apply=True).outcomes
        self.assertEqual([o.reasons for o in outcomes], [("file_replaced",)])
        self.assertEqual(path.read_bytes(), b"substituted")

    def test_a_database_failure_during_removal_keeps_the_bytes(self):
        execution, publication = self.interrupted_retirement()
        with mock.patch.object(retention, "_path_owners", side_effect=DatabaseError("connection lost")):
            outcomes = retention.finish_retirements(apply=True).outcomes
        self.assertEqual([(o.action, o.reasons) for o in outcomes], [("failed", ("ownership_unavailable",))])
        self.assertTrue(self.file(publication).is_file())
        self.assertEqual([o.action for o in retention.finish_retirements(apply=True).outcomes], ["removed"])
