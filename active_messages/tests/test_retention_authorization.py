"""D3E-C review 2: no asserted identity grants authority. Command retirement
is a trusted MAINTENANCE operation recorded as ActorKind.SYSTEM; naming any
user -- privileged, superuser, inactive, unprivileged -- is refused and
changes nothing."""
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
class AuthorizationTests(Lifecycle, TestCase):
    """Correction 2: the command never takes an identity from its caller."""

    def unchanged(self, execution, publication):
        self.assertTrue(self.file(publication).is_file())
        self.assertTrue(self.wave(publication).is_file())
        self.assertTrue(self.tracks(publication).exists())
        self.assertFalse(retention.is_retired(execution.pk))

    def test_naming_any_user_never_authorizes_retirement(self):
        execution, publication, _item = self.completed()
        superuser = get_user_model().objects.create_superuser("root-like", "r@example.invalid", "x")
        inactive = get_user_model().objects.create_user("gone", is_active=False)
        inactive_admin = self.admin
        before = (self.history(), self.disk())
        for name in (self.admin.username, superuser.username, inactive.username, inactive_admin.username,
                     self.producer.username, "nobody-at-all"):
            with self.subTest(name=name), self.assertRaises(CommandError):
                run("--retire", str(execution.pk), "--apply", "--operator", name)
        self.assertEqual((self.history(), self.disk()), before)
        self.unchanged(execution, publication)

    def test_no_asserted_identity_reaches_the_service(self):
        execution, publication, _item = self.completed()
        with self.assertRaises(TypeError):
            retention.retire_publication(execution.pk, operator=self.admin, apply=True)
        self.unchanged(execution, publication)

    def test_maintenance_retirement_records_the_system_actor(self):
        execution, publication, _item = self.completed()
        with self.captureOnCommitCallbacks(execute=True):
            output = run("--retire", str(execution.pk), "--apply")
        self.assertIn("retired 1", output)
        [event] = ActiveBroadcastMessageEvent.objects.filter(execution=execution, event=retention.RETIRED_EVENT)
        self.assertEqual((event.actor_kind, event.actor_id, event.actor_username),
                         (ActorKind.SYSTEM, None, ""))
        self.assertEqual(event.detail["via"], "maintenance_command")
        self.assertNotIn("operator", event.detail)
        self.assertFalse(ActiveBroadcastMessageEvent.objects.filter(event=retention.RETIRED_EVENT,
                                                                    actor_kind=ActorKind.USER).exists())
