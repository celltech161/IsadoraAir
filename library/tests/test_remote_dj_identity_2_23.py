"""Roadmap 2.23 Phase 1B -- Remote DJ connection identity (server side).

* the engine publishes the ACTIVE session's owner (signed-token user id +
  attempt id) and nothing for an ended session;
* /api/engine/status/ never passes those raw values through: it returns a
  display-safe block (display name, "is this my account", and a one-way key
  only the owning browser can match) to viewers who may connect as / gate
  the Remote DJ, and nothing to anyone else, for a stale state file, or
  without an active session.

The status view reads a temporary state file; nothing touches
/run/isadoraair or the engine.
"""
import hashlib
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import SimpleTestCase, TestCase, override_settings

import library.services.engine as eng_module
import library.views as views
from library.models import GroupAccess
from library.services.remote_dj_connection import RemoteDJConnectionAttempt, new_attempt_id
from library.tests.test_engine_runtime_commit import make_minimal_stand_in

User = get_user_model()


def _session(user_id, attempt_id):
    session = eng_module.RemoteDJSession()
    session.user_id = user_id
    session.connection_attempt = RemoteDJConnectionAttempt(attempt_id, int(time.time() * 1000))
    return session


class EngineOwnerStateTests(SimpleTestCase):
    def test_no_session_means_no_owner_even_with_a_last_attempt(self):
        engine = object.__new__(eng_module.PlaybackEngine)
        engine.remote_dj_session = None
        engine._remote_dj_last_attempt = RemoteDJConnectionAttempt(new_attempt_id(), 1)
        self.assertIsNone(engine._remote_dj_owner_state())

    def test_active_session_owner_is_its_token_user_and_attempt(self):
        attempt_id = new_attempt_id()
        engine = object.__new__(eng_module.PlaybackEngine)
        engine.remote_dj_session = _session(42, attempt_id)
        self.assertEqual(engine._remote_dj_owner_state(), {"user_id": 42, "attempt_id": attempt_id})

    def test_write_state_carries_the_owner_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "engine_state.json"
            with patch.object(eng_module, "STATE_PATH", state_path):
                stand_in = make_minimal_stand_in()
                stand_in._write_state()
                self.assertIsNone(json.loads(state_path.read_text())["remote_dj_owner"])


def _state(*, connected=True, owner_user_id=None, attempt_id="A" * 24, age=0.0, include_owner=True):
    state = {
        "transport": "PLAYING", "decks": {"A": None, "B": None}, "queue": [], "fx_fires": [],
        "timestamp": time.time() - age,
        "remote_dj_configured": True, "remote_dj_connected": connected, "remote_dj_live": False,
        "remote_dj_reconnecting": False, "remote_dj_quality": None,
    }
    if include_owner:
        state["remote_dj_owner"] = (
            {"user_id": owner_user_id, "attempt_id": attempt_id} if connected else None
        )
    return state


@override_settings(SECURE_SSL_REDIRECT=False)
class StatusIdentityTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "engine_state.json"
        patcher = patch.object(views, "ENGINE_STATE_PATH", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.dj = User.objects.create_user("jsmith", password="x", first_name="Jane", last_name="Smith")
        self.dj.groups.add(Group.objects.get(name="remote_dj"))           # seeded Remote Host Role
        self.other_host = User.objects.create_user("rhost2", password="x")
        self.other_host.groups.add(Group.objects.get(name="remote_dj"))
        self.staff = User.objects.create_user("c223_staff_id", password="x", is_staff=True)
        # Reaches /api/engine/status/ (GroupAccess) but holds no Remote DJ capability.
        self.viewer = User.objects.create_user("statusonly", password="x")
        viewers = Group.objects.create(name="c223_status_viewers")
        GroupAccess.objects.create(group=viewers, allowed_prefixes="/api/engine/status/", landing_url="/")
        self.viewer.groups.add(viewers)

    def status(self, user, state):
        self.state_path.write_text(json.dumps(state))
        self.client.force_login(user)
        response = self.client.get("/api/engine/status/")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_authorized_observer_gets_the_display_name_and_an_opaque_key(self):
        attempt_id = new_attempt_id()
        data = self.status(self.other_host, _state(owner_user_id=self.dj.pk, attempt_id=attempt_id))
        self.assertEqual(data["remote_dj_owner"], {
            "identity": "known",
            "display_name": "Jane Smith",
            "is_current_user": False,
            "browser_key": hashlib.sha256(attempt_id.encode()).hexdigest()[:32],
        })

    def test_staff_observer_sees_the_owner(self):
        data = self.status(self.staff, _state(owner_user_id=self.dj.pk))
        self.assertEqual(data["remote_dj_owner"]["display_name"], "Jane Smith")

    def test_owner_account_is_flagged_but_ownership_is_by_browser_key(self):
        data = self.status(self.dj, _state(owner_user_id=self.dj.pk))
        self.assertTrue(data["remote_dj_owner"]["is_current_user"])
        self.assertIn("browser_key", data["remote_dj_owner"])

    def test_raw_owner_values_never_leave_the_server(self):
        attempt_id = new_attempt_id()
        self.client.force_login(self.dj)
        session_key = self.client.session.session_key
        data = self.status(self.other_host, _state(owner_user_id=self.dj.pk, attempt_id=attempt_id))
        owner = data["remote_dj_owner"]
        self.assertEqual(set(owner), {"identity", "display_name", "is_current_user", "browser_key"})
        self.assertNotIn("user_id", json.dumps(owner))
        self.assertNotIn(attempt_id, json.dumps(owner))
        body = json.dumps(data)
        self.assertNotIn(session_key, body)
        self.assertNotIn("sessionid", body)
        self.assertNotIn("csrftoken", body)
        self.assertNotIn('"user_id"', body)

    def test_viewer_without_remote_dj_capability_gets_no_identity(self):
        data = self.status(self.viewer, _state(owner_user_id=self.dj.pk))
        self.assertTrue(data["remote_dj_connected"])         # unchanged r0116 field
        self.assertIsNone(data["remote_dj_owner"])

    def test_username_fallback_and_length_bound(self):
        self.assertEqual(self.status(self.staff, _state(owner_user_id=self.other_host.pk))
                         ["remote_dj_owner"]["display_name"], "rhost2")
        self.dj.first_name, self.dj.last_name = "A" * 80, "B" * 80
        self.dj.save()
        name = self.status(self.staff, _state(owner_user_id=self.dj.pk))["remote_dj_owner"]["display_name"]
        self.assertEqual(len(name), views.REMOTE_DJ_OWNER_NAME_MAX)

    def test_unestablishable_owner_is_unavailable_not_invented(self):
        for user_id in (None, 999_999):
            with self.subTest(user_id=user_id):
                owner = self.status(self.staff, _state(owner_user_id=user_id))["remote_dj_owner"]
                self.assertEqual((owner["identity"], owner["display_name"], owner["is_current_user"]),
                                 ("unavailable", None, False))

    def test_no_active_session_no_owner(self):
        self.assertIsNone(self.status(self.staff, _state(connected=False))["remote_dj_owner"])
        inconsistent = _state(connected=False)
        inconsistent["remote_dj_owner"] = {"user_id": self.dj.pk, "attempt_id": "A" * 24}
        self.assertIsNone(self.status(self.staff, inconsistent)["remote_dj_owner"])

    def test_stale_state_withholds_identity(self):
        data = self.status(self.staff, _state(owner_user_id=self.dj.pk, age=30))
        self.assertEqual(data["transport"], "STALE")
        self.assertIsNone(data["remote_dj_owner"])

    def test_older_engine_without_the_field_is_generic(self):
        data = self.status(self.staff, _state(owner_user_id=self.dj.pk, include_owner=False))
        self.assertIsNone(data["remote_dj_owner"])

    def test_anonymous_is_refused(self):
        self.state_path.write_text(json.dumps(_state(owner_user_id=self.dj.pk)))
        response = self.client.get("/api/engine/status/")
        self.assertNotEqual(response.status_code, 200)
        self.assertNotIn(b"Jane", response.content)

    def test_offline_and_unreadable_states_are_unchanged(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/api/engine/status/").json()["transport"], "OFFLINE")
        self.state_path.write_text("{not json")
        self.assertEqual(self.client.get("/api/engine/status/").json()["transport"], "ERROR")
