"""D3E-C: ``manage.py active_message_retention`` -- read-only by default,
explicit opt-in for anything destructive, no path ever accepted, bounded
output, nonzero exit for operational failures."""
import io
import os
import tempfile
import unittest
import uuid
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import TestCase

from active_messages import reconcile, retention
from active_messages.models import ActiveBroadcastMessageEvent, ActiveBroadcastPolicy
from production.tests.support import ffmpeg_available

from .d3ec_support import Lifecycle
from .support import Domain


def run(*args):
    out = io.StringIO()
    call_command("active_message_retention", *args, stdout=out, stderr=io.StringIO())
    return out.getvalue()


class CommandContractTests(Domain, TestCase):
    def setUp(self):
        super().setUp()
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        os.mkdir(os.path.join(self.root.name, "UrgentPA"))
        settings = self.settings(LIBRARY_ROOT=self.root.name, WAVEFORMS_DIR=self.root.name)
        settings.enable()
        self.addCleanup(settings.disable)

    def test_the_default_is_a_read_only_report(self):
        policies = ActiveBroadcastPolicy.objects.count()
        output = run()
        self.assertIn("read-only", output)
        self.assertIn("inspected:", output)
        self.assertEqual(ActiveBroadcastPolicy.objects.count(), policies)
        self.assertEqual(ActiveBroadcastMessageEvent.objects.count(), 0)

    def test_destructive_modes_are_explicit(self):
        with self.assertRaisesRegex(CommandError, "--apply needs --sweep or --retire"):
            run("--apply")
        self.assertIn("DRY RUN", run("--sweep"))
        self.assertIn("reclaimable (not reclaimed)", run("--sweep"))
        self.assertIn("DRY RUN", run("--retire", str(uuid.uuid4())))
        self.assertIn("no_publication", run("--retire", str(uuid.uuid4()), "--apply"))

    def test_no_identity_can_be_asserted(self):
        """Review 2: the command takes no username at all (any --operator is an error)."""
        for name in ("nobody-here", self.producer.username, self.admin.username):
            with self.subTest(name=name), self.assertRaises(CommandError):
                run("--retire", str(uuid.uuid4()), "--apply", "--operator", name)

    def test_a_path_is_never_accepted(self):
        victim = os.path.join(self.root.name, "UrgentPA", "precious.flac")
        with open(victim, "wb") as handle:
            handle.write(b"keep me")
        output = run("--retire", victim, os.path.join("UrgentPA", "precious.flac"), "--apply")
        self.assertIn("refused", output)
        self.assertIn("unknown_execution", output)
        with open(victim, "rb") as handle:
            self.assertEqual(handle.read(), b"keep me")

    def test_bounds(self):
        for args in (("--limit", "0"), ("--limit", str(retention.MAX_LIMIT + 1))):
            with self.subTest(args=args), self.assertRaises(CommandError):
                run(*args)
        with self.assertRaises(CommandError):
            run("--sweep", "--grace-hours", "0.5")
        with self.assertRaisesRegex(CommandError, "at most"):
            run("--retire", *[str(uuid.uuid4()) for _ in range(51)])

    def test_operational_failures_exit_nonzero(self):
        with self.settings(LIBRARY_ROOT=os.path.join(self.root.name, "absent")):
            with self.assertRaisesRegex(CommandError, "storage unavailable: library_unavailable"):
                run("--sweep", "--apply")
        with mock.patch.object(reconcile, "reconcile", side_effect=OperationalError("server closed the connection")):
            with self.assertRaisesRegex(CommandError, "database unavailable: OperationalError"):
                run()
        failed = retention.CleanupReport(apply=True, outcomes=[retention.Outcome("namespace_file", "am-x.flac",
                                                                                  "failed", ("PermissionError",))])
        with mock.patch.object(retention, "sweep_publication_namespace", return_value=failed):
            with self.assertRaisesRegex(CommandError, "1 cleanup"):
                run("--sweep", "--apply")

    def test_attention_only_for_integrity_findings(self):
        damage = reconcile.Report(deep=False)
        damage.add("publication", "x", "publication_file_missing", "warning", "integrity_damage", "gone")
        damage.add("insertion", "y", "insertion_log_item_pruned", "info", "expected_pruning", "pruned")
        with mock.patch.object(reconcile, "reconcile", return_value=damage), \
                mock.patch("monitoring.models.emit_event") as emitted:
            run()                                                # the default never records attention
            emitted.assert_not_called()
            run("--attention")
            self.assertEqual([call.kwargs["dedupe_key"] for call in emitted.call_args_list],
                             ["active_messages|retention|publication_file_missing"])
        with self.assertRaises(CommandError):
            run("--attention", "--sweep")


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class CommandRetirementTests(Lifecycle, TestCase):
    def test_retire_end_to_end(self):
        execution, publication, _item = self.completed()
        dry = run("--retire", str(execution.pk))
        self.assertIn("eligible 1", dry)
        self.assertTrue(self.file(publication).is_file())
        with self.captureOnCommitCallbacks(execute=True):
            done = run("--retire", str(execution.pk), "--apply")
        self.assertIn("retired 1", done)
        self.assertIn("reclaimed", done)
        self.assertFalse(self.file(publication).exists())
        self.assertTrue(retention.is_retired(execution.pk))
        report = run()
        self.assertNotIn("[critical]", report)
        self.assertNotIn("[warning]", report)
