"""D3E-C review 4: the default report writes nothing at all; Monitoring
attention is an explicit --attention opt-in."""
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
class ReadOnlyCommandTests(Lifecycle, TestCase):
    """Correction 4: diagnostics write nothing unless --attention is asked for."""

    def test_the_default_report_writes_nothing(self):
        from monitoring.models import SystemEvent
        execution, publication, _item = self.completed()
        self.wave(publication).unlink()                           # an integrity finding
        before = (self.history(), self.disk(), SystemEvent.objects.count())
        with CaptureQueriesContext(connection) as queries:
            output = run()
        self.assertEqual(writes(queries.captured_queries), [])
        self.assertIn("publication_waveform_missing", output)
        self.assertIn("read-only", output)
        self.assertEqual((self.history(), self.disk(), SystemEvent.objects.count()), before)
        for args in (("--deep",), ("--sweep",), ("--retire", str(execution.pk))):
            with self.subTest(args=args), CaptureQueriesContext(connection) as queries:
                run(*args)
            self.assertEqual(writes(queries.captured_queries), [])

    def test_attention_is_an_explicit_opt_in_and_deduplicated(self):
        from monitoring.models import SystemEvent
        execution, publication, _item = self.completed()
        self.wave(publication).unlink()
        before = (self.history(), self.disk())
        run("--attention")
        run("--attention")
        rows = SystemEvent.objects.filter(title="Active messages retention: publication_waveform_missing")
        self.assertEqual(rows.count(), 1)                           # coalesced
        self.assertEqual((self.history(), self.disk()), before)    # nothing else changes
        with self.assertRaises(CommandError):
            run("--no-attention")                                   # the old flag is gone (no contradictions)

    def test_pruning_and_the_closed_gate_never_alert(self):
        from library.views import _delete_track_and_file
        from monitoring.models import SystemEvent
        execution, publication, _item = self.completed()
        _delete_track_and_file(Track.objects.get(pk=publication.track_id_snapshot))
        with transaction.atomic(), mock.patch("active_messages.services.NATIVE_AIRING_AVAILABLE", False):
            count = SystemEvent.objects.count()
            run("--attention")
            self.assertEqual(SystemEvent.objects.count(), count)
