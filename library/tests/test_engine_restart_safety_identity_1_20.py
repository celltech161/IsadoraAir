"""Roadmap 1.20 (revised), Codex P1 correction: the restart assessment must not
return Safe for incomplete, unsupported or contradictory engine status.

The independent review of 9e70035 found eight states that each produced Safe:
missing engine_session_id, missing state_revision, a revision from a different
engine instance, an invalid sequence, an unsupported transport, a configured
Remote DJ with either lifecycle key missing, and remote_dj_configured=False
alongside an available slot. Each is now Unknown (override required).
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

from library.services import engine_restart_safety as safety

from .engine_restart_state_support import EngineStateFile, engine_state

ENGINE_SOURCE = Path(__file__).resolve().parents[1] / "services" / "engine.py"
SESSION = "0b0f6a2c-5e6a-4f0e-9c58-3f6f2f3a9d11"


def _without(*keys, **overrides):
    state = engine_state(**overrides)
    for key in keys:
        del state[key]
    return state


class CodexNegativeControlTests(SimpleTestCase):
    """The eight independent-review cases, verbatim in intent."""

    @staticmethod
    def cases():
        # Built per test so every timestamp is fresh (a module-level fixture
        # would go stale before the run reaches it and mask the real reason).
        return {
            "missing engine_session_id": (_without("engine_session_id"), safety.STATUS_UNSUPPORTED),
            "missing state_revision": (_without("state_revision"), safety.STATUS_UNSUPPORTED),
            "revision from another engine instance": (
                engine_state(state_revision={"instance": "another-engine-run", "seq": 41}),
                safety.STATUS_CONTRADICTORY),
            "invalid sequence": (engine_state(state_revision={"instance": SESSION, "seq": "41"}),
                                 safety.STATUS_MALFORMED),
            "unsupported transport": (engine_state(transport="WARP"), safety.STATUS_UNSUPPORTED),
            "configured, slot key missing": (_without("remote_dj_slot"), safety.STATUS_UNSUPPORTED),
            "configured, connection key missing": (_without("remote_dj_connection"), safety.STATUS_UNSUPPORTED),
            "not configured but slot available": (
                engine_state(remote_dj_configured=False, remote_dj_slot="available"),
                safety.STATUS_CONTRADICTORY),
        }

    def setUp(self):
        self.state_file = EngineStateFile(self)

    def test_each_case_is_unknown_never_safe(self):
        cases = self.cases()
        self.assertEqual(len(cases), 8)
        for name, (state, reason) in cases.items():
            with self.subTest(case=name):
                self.state_file.write(state)
                result = safety.assess_engine_restart()
                self.assertEqual(result.level, safety.UNKNOWN)
                self.assertEqual(result.reasons, (reason,))
                self.assertTrue(result.requires_override)

    def test_control_the_complete_producer_shape_is_still_safe(self):
        self.state_file.write()
        result = safety.assess_engine_restart()
        self.assertEqual(result.level, safety.SAFE)
        self.assertEqual(result.engine_instance, SESSION)
        self.assertEqual(result.state_revision, {"instance": SESSION, "seq": 41})


class IdentityTests(SimpleTestCase):
    def setUp(self):
        self.state_file = EngineStateFile(self)

    def assess(self, state):
        self.state_file.write(state)
        return safety.assess_engine_restart()

    def test_bad_engine_session_ids_are_malformed(self):
        for bad in (None, "", 7, True, [], {}, "x" * 65):
            with self.subTest(bad=bad):
                result = self.assess(engine_state(engine_session_id=bad))
                self.assertEqual((result.level, result.reasons), (safety.UNKNOWN, (safety.STATUS_MALFORMED,)))

    def test_bad_revisions_are_malformed(self):
        for bad in (None, "r41", 41, [], {}, {"instance": SESSION},
                    {"seq": 41}, {"instance": None, "seq": 41}, {"instance": 5, "seq": 41}):
            with self.subTest(bad=bad):
                result = self.assess(engine_state(state_revision=bad))
                self.assertEqual((result.level, result.reasons), (safety.UNKNOWN, (safety.STATUS_MALFORMED,)))

    def test_bad_sequences_are_malformed(self):
        for bad in (None, 0, -1, True, False, 1.5, "1", []):
            with self.subTest(bad=bad):
                result = self.assess(engine_state(state_revision={"instance": SESSION, "seq": bad}))
                self.assertEqual((result.level, result.reasons), (safety.UNKNOWN, (safety.STATUS_MALFORMED,)))

    def test_revision_instance_must_match_the_session(self):
        result = self.assess(engine_state(state_revision={"instance": SESSION.upper(), "seq": 1}))
        self.assertEqual((result.level, result.reasons), (safety.UNKNOWN, (safety.STATUS_CONTRADICTORY,)))

    def test_identity_failures_win_even_over_a_live_microphone(self):
        # Unknown, not Blocked: the snapshot is not evidence of anything.
        result = self.assess(_without("engine_session_id", mic_live=True))
        self.assertEqual(result.level, safety.UNKNOWN)


class TransportAndLifecycleTests(SimpleTestCase):
    def setUp(self):
        self.state_file = EngineStateFile(self)

    def assess(self, state):
        self.state_file.write(state)
        return safety.assess_engine_restart()

    def test_only_published_transports_are_supported(self):
        for good in ("PLAYING", "STOPPED"):
            with self.subTest(good=good):
                self.assertEqual(self.assess(engine_state(transport=good)).level, safety.SAFE)
        for bad in ("playing", "PAUSED", "STALE", "", None, 1, True, ["PLAYING"]):
            with self.subTest(bad=bad):
                result = self.assess(engine_state(transport=bad))
                self.assertEqual((result.level, result.reasons), (safety.UNKNOWN, (safety.STATUS_UNSUPPORTED,)))

    def test_configured_remote_dj_needs_a_slot_state(self):
        result = self.assess(engine_state(remote_dj_configured=True, remote_dj_slot=None))
        self.assertEqual((result.level, result.reasons), (safety.UNKNOWN, (safety.STATUS_CONTRADICTORY,)))

    def test_unconfigured_remote_dj_with_any_remote_dj_activity_is_contradictory(self):
        base = dict(remote_dj_configured=False, remote_dj_slot=None)
        for extra in ({"remote_dj_slot": "available"}, {"remote_dj_slot": "occupied"},
                      {"remote_dj_slot": "quarantined"},
                      {"remote_dj_connection": {"status": "ended"}},
                      {"remote_dj_connected": True}, {"remote_dj_live": True},
                      {"remote_dj_reconnecting": True}):
            with self.subTest(extra=extra):
                result = self.assess(engine_state(**{**base, **extra}))
                self.assertEqual((result.level, result.reasons), (safety.UNKNOWN, (safety.STATUS_CONTRADICTORY,)))

    def test_unconfigured_remote_dj_with_null_lifecycle_is_safe(self):
        result = self.assess(engine_state(remote_dj_configured=False, remote_dj_slot=None, remote_dj_connection=None))
        self.assertEqual(result.level, safety.SAFE)
        self.assertFalse(result.facts["remote_dj_configured"])

    def test_configured_and_idle_is_safe_with_null_connection(self):
        self.assertEqual(self.assess(engine_state(remote_dj_connection=None)).level, safety.SAFE)


class ProducerContractTests(SimpleTestCase):
    """The keys and values this module requires are the ones the real writer emits."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        source = ENGINE_SOURCE.read_text(encoding="utf-8")
        start = source.index("    def _write_state(self, transport=\"PLAYING\"):")
        cls.writer = source[start:source.index("tmp.rename(STATE_PATH)", start)]
        cls.source = source

    def test_writer_publishes_every_required_key(self):
        required = (("timestamp", "engine_session_id", "state_revision", "transport")
                    + safety._REQUIRED_BOOLEANS + safety._REQUIRED_LIFECYCLE_KEYS)
        for key in required:
            with self.subTest(key=key):
                self.assertIn(f'"{key}":', self.writer)

    def test_revision_instance_is_the_session_id(self):
        self.assertIn('"instance": getattr(self, "_engine_session_id", None),', self.writer)
        self.assertIn('"engine_session_id": getattr(self, "_engine_session_id", None),', self.writer)
        self.assertIn("_STATE_REVISION_SEQ = itertools.count(1)", self.source)

    def test_every_transport_the_engine_writes_is_supported(self):
        written = set(re.findall(r'_write_state\(transport="([A-Z]+)"\)', self.source)) | {"PLAYING"}
        self.assertEqual(written, set(safety._KNOWN_TRANSPORTS))

    def test_slot_is_null_exactly_when_remote_dj_is_not_built(self):
        self.assertIn('"remote_dj_configured": self.remote_dj_tee is not None,', self.writer)
        body = self.source[self.source.index("    def _remote_dj_slot_state(self):"):]
        body = body[:body.index("\n    def ", 10)]
        self.assertIn('if getattr(self, "remote_dj_tee", None) is None:\n            return None', body)
        self.assertEqual(set(re.findall(r'return "([a-z]+)"', body)), set(safety._KNOWN_SLOT_STATES))
