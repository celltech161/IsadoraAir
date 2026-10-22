"""Roadmap 2.23 Phase 1B -- Remote DJ connection identity (server side).

* the engine publishes the ACTIVE session's owner (signed-token user id +
  attempt id) and nothing for an ended session;
* /api/engine/status/ never passes those raw values through: it returns a
  display-safe block (display name, "is this my account", and a browser key
  the owning page matches against its own attempt -- a presentation aid, not
  a secret or an authority) to viewers who may connect as / gate
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
from types import SimpleNamespace
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


class EngineOwnerStateHardeningTests(SimpleTestCase):
    """Second-review P3: the observational owner field never raises for a
    missing/malformed session or attempt, never invents or keeps an expired
    owner, and can never cost the rest of the engine-state snapshot."""

    def engine_with(self, session):
        engine = object.__new__(eng_module.PlaybackEngine)
        engine.remote_dj_session = session
        return engine

    def attempt(self, **overrides):
        attempt = RemoteDJConnectionAttempt(new_attempt_id(), int(time.time() * 1000))
        for name, value in overrides.items():
            setattr(attempt, name, value)
        return attempt

    def test_normal_session_output_is_unchanged(self):
        attempt = self.attempt()
        session = SimpleNamespace(user_id=7, connection_attempt=attempt)
        self.assertEqual(self.engine_with(session)._remote_dj_owner_state(),
                         {"user_id": 7, "attempt_id": attempt.attempt_id})

    def test_missing_session_attribute_entirely(self):
        self.assertIsNone(object.__new__(eng_module.PlaybackEngine)._remote_dj_owner_state())

    def test_missing_or_null_connection_attempt(self):
        for session in (SimpleNamespace(user_id=7), SimpleNamespace(user_id=7, connection_attempt=None)):
            with self.subTest(session=session):
                self.assertIsNone(self.engine_with(session)._remote_dj_owner_state())

    def test_missing_or_malformed_attempt_id(self):
        for attempt in (SimpleNamespace(status="connected"),
                        SimpleNamespace(status="connected", attempt_id=None),
                        SimpleNamespace(status="connected", attempt_id=12345),
                        SimpleNamespace(status="connected", attempt_id=""),
                        SimpleNamespace(status="connected", attempt_id="not a minted id!")):
            with self.subTest(attempt=attempt):
                session = SimpleNamespace(user_id=7, connection_attempt=attempt)
                self.assertIsNone(self.engine_with(session)._remote_dj_owner_state())

    def test_missing_or_malformed_user_id_degrades_to_unknown_owner(self):
        attempt = self.attempt()
        for session in (SimpleNamespace(connection_attempt=attempt),
                        *(SimpleNamespace(user_id=bad, connection_attempt=attempt)
                          for bad in (None, "7", True, 0, -3, 7.0, object()))):
            with self.subTest(user_id=getattr(session, "user_id", "<missing>")):
                self.assertEqual(self.engine_with(session)._remote_dj_owner_state(),
                                 {"user_id": None, "attempt_id": attempt.attempt_id})

    def test_ended_or_failed_attempt_is_never_published(self):
        for status in ("ended", "failed"):
            with self.subTest(status=status):
                session = SimpleNamespace(user_id=7, connection_attempt=self.attempt(status=status))
                self.assertIsNone(self.engine_with(session)._remote_dj_owner_state())

    def test_reconnect_grace_keeps_the_owner(self):
        attempt = self.attempt(status="reconnecting")
        session = SimpleNamespace(user_id=7, connection_attempt=attempt)
        self.assertEqual(self.engine_with(session)._remote_dj_owner_state()["attempt_id"], attempt.attempt_id)

    def test_session_teardown_clears_the_owner(self):
        engine = self.engine_with(_session(7, new_attempt_id()))
        self.assertIsNotNone(engine._remote_dj_owner_state())
        engine.remote_dj_session = None
        self.assertIsNone(engine._remote_dj_owner_state())

    def write(self, stand_in):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "engine_state.json"
            with patch.object(eng_module, "STATE_PATH", state_path):
                stand_in._write_state()
                return json.loads(state_path.read_text()) if state_path.exists() else None

    def busy_stand_in(self):
        stand_in = make_minimal_stand_in()
        stand_in.mic_live = True
        stand_in.mic_ptt_valve = object()
        stand_in.mic_ok = True
        stand_in.manual_mode = True
        stand_in._manual_from_mic = True
        return stand_in

    def test_write_state_survives_malformed_owner_fields_on_a_live_session(self):
        stand_in = self.busy_stand_in()
        session = _session(7, new_attempt_id())
        session.user_id = object()                       # malformed user
        session.connection_attempt.attempt_id = None     # malformed attempt id
        stand_in.remote_dj_session = session
        state = self.write(stand_in)
        self.assertIsNotNone(state)
        self.assertIsNone(state["remote_dj_owner"])
        self.assertTrue(state["remote_dj_connected"])
        self.assertEqual((state["mic_live"], state["mic_configured"], state["manual_mode"], state["manual_from_mic"]),
                         (True, True, True, True))
        self.assertEqual(state["decks"], {"A": None, "B": None})

    def test_write_state_contains_an_owner_helper_failure(self):
        stand_in = self.busy_stand_in()
        for exc in (AttributeError("x"), TypeError("x"), ValueError("x")):
            with self.subTest(exc=type(exc).__name__):
                with patch.object(eng_module.PlaybackEngine, "_remote_dj_owner_state", side_effect=exc):
                    state = self.write(stand_in)
                self.assertIsNotNone(state)
                self.assertIsNone(state["remote_dj_owner"])
                self.assertTrue(state["mic_live"])

    def test_unrelated_errors_are_not_swallowed_by_the_owner_guard(self):
        # Narrow isolation: anything else still reaches _write_state's own
        # handler exactly as before (no snapshot is written).
        stand_in = self.busy_stand_in()
        with patch.object(eng_module.PlaybackEngine, "_remote_dj_owner_state", side_effect=RuntimeError("bug")):
            self.assertIsNone(self.write(stand_in))


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
        self.assertIs(data["remote_dj_owner_supported"], False)

    def test_owner_supported_flag_reflects_the_engine_not_the_viewer(self):
        # The page uses this to keep r0116 owner behaviour ONLY for an engine
        # that does not publish ownership at all; a withheld/null block from
        # a publishing engine stays "unverified" (no mic gate).
        for user, state in ((self.staff, _state(owner_user_id=self.dj.pk)),
                            (self.viewer, _state(owner_user_id=self.dj.pk)),
                            (self.staff, _state(connected=False)),
                            (self.staff, _state(owner_user_id=self.dj.pk, age=30))):
            with self.subTest(user=user.username, connected=state["remote_dj_connected"]):
                self.assertIs(self.status(user, state)["remote_dj_owner_supported"], True)

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


class EngineStateRevisionTests(SimpleTestCase):
    """Fourth review: every engine state write carries an engine-authored
    snapshot revision -- {instance: this run's _engine_session_id, seq} with seq
    strictly increasing per write within the process -- produced inline, with no
    extra write, timer, thread or lock."""

    def write(self, stand_in, state_path):
        with patch.object(eng_module, "STATE_PATH", state_path):
            stand_in._write_state()
        return json.loads(state_path.read_text())

    def test_seq_strictly_increases_within_one_engine_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            stand_in = make_minimal_stand_in()
            stand_in._engine_session_id = "run-x"
            states = [self.write(stand_in, Path(tmp) / "engine_state.json") for _ in range(3)]
        revisions = [s["state_revision"] for s in states]
        self.assertEqual({r["instance"] for r in revisions}, {"run-x"})
        seqs = [r["seq"] for r in revisions]
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertTrue(all(isinstance(n, int) and n > 0 for n in seqs))

    def test_a_restarted_engine_is_a_different_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = make_minimal_stand_in(), make_minimal_stand_in()
            first._engine_session_id, second._engine_session_id = "run-x", "run-y"
            a = self.write(first, Path(tmp) / "a.json")["state_revision"]
            b = self.write(second, Path(tmp) / "b.json")["state_revision"]
        self.assertNotEqual(a["instance"], b["instance"])

    def test_missing_session_id_still_writes_the_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            stand_in = make_minimal_stand_in()
            stand_in.mic_live = True
            stand_in.mic_ptt_valve = object()
            state = self.write(stand_in, Path(tmp) / "engine_state.json")
        self.assertIsNone(state["state_revision"]["instance"])   # the Console treats it as unversioned
        self.assertTrue(state["mic_live"])
        self.assertEqual(state["decks"], {"A": None, "B": None})

    def test_revision_is_built_inline_in_the_single_write(self):
        import inspect
        source = inspect.getsource(eng_module.PlaybackEngine._write_state)
        self.assertEqual(source.count("next(_STATE_REVISION_SEQ)"), 1)
        self.assertEqual(source.count("tmp.rename(STATE_PATH)"), 1)
        self.assertNotIn("threading", source)
        self.assertNotIn("GLib.timeout_add", source)
