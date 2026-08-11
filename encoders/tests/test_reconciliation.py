"""Phase 3 -- per-input-device group reconciliation. Replaces the
Django admin's whole-service `systemctl restart isadoraair-encoders`
for routine Encoder edits with EncoderManager discovering DB drift
itself, per input-device group, on its own existing 5s health-tick
cadence (see encoders/services/encoder_manager.py's _reconcile).

Unlike test_encoder_manager.py / test_candidate_qualification.py (which
mostly pass UNSAVED Encoder(**kwargs) instances directly into manager
methods, bypassing the DB entirely), every test here uses REAL,
PERSISTED Encoder rows -- _reconcile() queries Encoder.objects.filter(
enabled=True) fresh on every _check_health() tick, so an unsaved row
would be (correctly, per Phase 3C) treated as "no longer desired" and
torn down. make_saved_encoder() below is this file's equivalent of
those other files' make_encoder(), persisted from the start.

Reuses CandidateFixtureMixin (patched CANDIDATE_DIR/LKG_DIR + a fast
qualification clock) and qualify_ok() from test_candidate_qualification
-- reconciliation-triggered candidates go through the EXACT SAME
qualification/promotion/rollback machinery Phase 2 already has tests
for; this file is about WHEN/WHETHER that machinery gets invoked for an
already-running group, not a second implementation of qualification
itself."""
import subprocess
import time
from unittest.mock import MagicMock, patch

from django.test import TransactionTestCase

import encoders.services.encoder_manager as em
from encoders.models import Encoder
from encoders.services import lkg as lkg_module
from encoders.services import preflight as preflight_module
from encoders.services import validation as validation_module
from encoders.tests.test_candidate_qualification import CandidateFixtureMixin


def make_saved_encoder(name="test-mp3", **overrides):
    defaults = dict(
        name=name, enabled=True, protocol="shoutcast2", host="192.168.1.112",
        port=8000, mount="/1", password="secret", format="mp3", bitrate_kbps=320,
        station_name="Test Station", genre="Variety", url="https://example.com", public=False,
    )
    defaults.update(overrides)
    return Encoder.objects.create(**defaults)


class ReconciliationFixtureMixin(CandidateFixtureMixin):
    """On top of CandidateFixtureMixin's own patches: a default mock
    for evaluate_encoder_group_health covering the ENTIRE test body,
    not just explicit qualification calls.

    Why this is necessary here specifically (not needed by
    test_candidate_qualification.py's own tests): every test in THIS
    file drives real _check_health() ticks to exercise _reconcile()
    -- and _check_health() ALSO runs the ordinary qualification-check
    loop, unconditionally, for whatever just landed in self._procs as
    "candidate"/"rollback" in that SAME tick. Left unmocked, that
    reaches the real evaluate_encoder_group_health -> probe_systemd ->
    subprocess.run(["systemctl", ...]) -- and this fixture's OWN
    Popen mock (EncoderManagerFixtureMixin, patched at the process-
    global `subprocess.Popen`, not something scoped to encoder_
    manager.py's own module) intercepts that real subprocess.run()
    call too, since Python subprocess.run() is itself built on
    Popen() -- returning a bare MagicMock in place of a real process
    handle breaks subprocess.run()'s own internal communicate()
    unpacking. Defaulting to "unknown" (not "ok") here means a
    candidate launched incidentally by a _check_health() call a test
    isn't specifically trying to promote just stays on probation,
    exactly as an ordinary "still starting" real health read would --
    a test that DOES want promotion still nests its own inner
    `with patch(..., return_value=("ok", ...))`, which correctly wins
    over this outer default for its duration."""

    def setUp(self):
        super().setUp()
        patcher = patch(
            "monitoring.services.probes.evaluate_encoder_group_health",
            return_value=("unknown", {"reason": "starting"}),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # Same reasoning as the health-check default above, for static
        # preflight: any _check_health() call this file doesn't
        # explicitly wrap with its OWN patch.object(preflight_module,
        # "run_preflight", ...) would otherwise reach the REAL
        # check_liquidsoap_syntax -> subprocess.run(["liquidsoap",
        # "--check", ...]) -- hitting the same process-global Popen-
        # mock pollution. Defaults to "passes" (not a rejection) so an
        # incidental candidate/new-group launch inside a test that
        # isn't specifically exercising preflight failure behaves like
        # an ordinary healthy environment would; tests that specifically
        # need a rejection still nest their own inner override.
        preflight_patcher = patch.object(
            preflight_module, "run_preflight",
            return_value=preflight_module.PreflightResult(ok=True),
        )
        preflight_patcher.start()
        self.addCleanup(preflight_patcher.stop)

    def bootstrap_accepted(self, manager, input_device, encoder):
        """Take a group all the way from nothing to launch_kind
        "accepted" with a persisted LKG -- via the real _launch_group
        candidate pipeline + qualify_ok, exactly as production does at
        cold start. Returns (pid, generation) of the resulting child."""
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            ok = manager._launch_group(input_device, [encoder])
            self.assertTrue(ok)
            self.qualify_ok(manager, input_device)
        self.assertEqual(manager._launch_kind[input_device], "accepted")
        current = manager._current[input_device]
        return current["pid"], current["generation"]

    def popen_call_count(self):
        return self._fake_pid_counter

    def qualify_via_check_health(self, manager, ticks=4, sleep=0.02):
        """Like CandidateFixtureMixin.qualify_ok, but drives full
        _check_health() ticks (so _reconcile() also runs each time,
        exactly like production) with evaluate_encoder_group_health
        mocked "ok" throughout -- needs the same real-time sleep
        between ticks qualify_ok uses: CANDIDATE_QUALIFICATION_SECONDS
        requires CONTINUOUS "ok" for a real (if patched-tiny) span of
        wall-clock time, which back-to-back calls with no sleep never
        accumulate."""
        with patch("monitoring.services.probes.evaluate_encoder_group_health", return_value=("ok", {"reason": "healthy"})):
            for _ in range(ticks):
                manager._check_health()
                time.sleep(sleep)
            manager._check_health()


# ---------------------------------------------------------------------
# No-op reconciliation: desired == running == accepted.
# ---------------------------------------------------------------------
class NoOpReconciliationTests(ReconciliationFixtureMixin, TransactionTestCase):
    def test_unchanged_group_no_popen_no_terminate_same_pid_and_generation(self):
        manager = em.EncoderManager()
        encoder = make_saved_encoder()
        pid_before, gen_before = self.bootstrap_accepted(manager, "airtap", encoder)
        proc = self._live_procs[pid_before]
        calls_before = self.popen_call_count()

        with patch("monitoring.services.probes.evaluate_encoder_group_health", return_value=("ok", {"reason": "healthy"})):
            for _ in range(3):
                manager._check_health()

        self.assertEqual(self.popen_call_count(), calls_before)  # no new Popen()
        proc.terminate.assert_not_called()
        self.assertEqual(manager._current["airtap"]["pid"], pid_before)
        self.assertEqual(manager._current["airtap"]["generation"], gen_before)
        self.assertEqual(manager._launch_kind["airtap"], "accepted")

    def test_unchanged_group_reconcile_status_in_sync(self):
        manager = em.EncoderManager()
        encoder = make_saved_encoder()
        self.bootstrap_accepted(manager, "airtap", encoder)
        manager._check_health()
        state = self.read_group_state("airtap")
        self.assertEqual(state["reconcile_status"], "in_sync")
        self.assertEqual(state["desired_fingerprint"], state["running_fingerprint"])
        self.assertEqual(state["running_fingerprint"], state["accepted_fingerprint"])


# ---------------------------------------------------------------------
# Single changed group among two -- the most important Phase 3 test.
# ---------------------------------------------------------------------
class ChangedAmongTwoTests(ReconciliationFixtureMixin, TransactionTestCase):
    def test_group_a_changed_group_b_untouched(self):
        manager = em.EncoderManager()
        encoder_a = make_saved_encoder(name="a", host="10.0.0.1")
        encoder_b = make_saved_encoder(name="b", host="10.0.0.2", input_device="plughw:3,1")
        pid_a_before, gen_a_before = self.bootstrap_accepted(manager, "airtap", encoder_a)
        pid_b, gen_b = self.bootstrap_accepted(manager, "plughw:3,1", encoder_b)
        proc_b = self._live_procs[pid_b]

        # Change group A's own desired configuration.
        encoder_a.bitrate_kbps = 256
        encoder_a.save()

        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._check_health()  # kicks off A's replacement (static checks + intentional stop + launch)

        # A got a NEW generation (replaced), B is completely untouched.
        self.assertEqual(manager._launch_kind["airtap"], "candidate")
        self.assertNotEqual(manager._current["airtap"]["generation"], gen_a_before)
        self.assertEqual(manager._current["plughw:3,1"]["pid"], pid_b)
        self.assertEqual(manager._current["plughw:3,1"]["generation"], gen_b)
        proc_b.terminate.assert_not_called()

        self.qualify_via_check_health(manager)

        # A promotes; B's PID/generation are STILL exactly unchanged.
        self.assertEqual(manager._launch_kind["airtap"], "accepted")
        self.assertEqual(manager._current["plughw:3,1"]["pid"], pid_b)
        self.assertEqual(manager._current["plughw:3,1"]["generation"], gen_b)
        proc_b.terminate.assert_not_called()


# ---------------------------------------------------------------------
# Static validation / preflight failure -- current healthy child MUST
# NOT be touched, LKG MUST NOT be touched.
# ---------------------------------------------------------------------
class StaticFailureLeavesRunningGroupUntouchedTests(ReconciliationFixtureMixin, TransactionTestCase):
    def test_validation_failure_leaves_current_process_and_lkg_untouched(self):
        manager = em.EncoderManager()
        encoder = make_saved_encoder()
        pid_before, gen_before = self.bootstrap_accepted(manager, "airtap", encoder)
        proc = self._live_procs[pid_before]
        lkg_before, meta_before = lkg_module.read_lkg(em._slug("airtap"))

        encoder.host = ""  # now fails validate_connection_fields: "Host is required."
        encoder.save()

        manager._check_health()

        proc.terminate.assert_not_called()
        self.assertEqual(manager._current["airtap"]["pid"], pid_before)
        self.assertEqual(manager._current["airtap"]["generation"], gen_before)
        lkg_after, meta_after = lkg_module.read_lkg(em._slug("airtap"))
        self.assertEqual(lkg_after, lkg_before)
        self.assertEqual(meta_after["fingerprint"], meta_before["fingerprint"])

        state = self.read_group_state("airtap")
        self.assertEqual(state["reconcile_status"], "rejected")
        self.assertEqual(state["last_reconcile_result"], "static_validation_rejected")
        self.assertIn("Host is required", state["last_reconcile_error"])

    def test_rejected_fingerprint_not_retried_next_tick(self):
        manager = em.EncoderManager()
        encoder = make_saved_encoder()
        pid_before, gen_before = self.bootstrap_accepted(manager, "airtap", encoder)
        encoder.host = ""
        encoder.save()

        manager._check_health()
        with patch.object(validation_module, "validate_full_configuration") as mock_validate:
            manager._check_health()
        mock_validate.assert_not_called()  # short-circuited by the rejected-fingerprint check
        self.assertEqual(manager._current["airtap"]["pid"], pid_before)
        self.assertEqual(manager._current["airtap"]["generation"], gen_before)


class StaticPreflightFailureLeavesRunningGroupUntouchedTests(ReconciliationFixtureMixin, TransactionTestCase):
    def test_preflight_failure_leaves_current_process_and_lkg_untouched(self):
        manager = em.EncoderManager()
        encoder = make_saved_encoder()
        pid_before, gen_before = self.bootstrap_accepted(manager, "airtap", encoder)
        proc = self._live_procs[pid_before]
        lkg_before, meta_before = lkg_module.read_lkg(em._slug("airtap"))

        encoder.bitrate_kbps = 256  # a real, valid change -- passes validation
        encoder.save()

        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=False, reason="liquidsoap --check failed")):
            manager._check_health()

        proc.terminate.assert_not_called()
        self.assertEqual(manager._current["airtap"]["pid"], pid_before)
        self.assertEqual(manager._current["airtap"]["generation"], gen_before)
        lkg_after, meta_after = lkg_module.read_lkg(em._slug("airtap"))
        self.assertEqual(lkg_after, lkg_before)
        self.assertEqual(meta_after["fingerprint"], meta_before["fingerprint"])

        state = self.read_group_state("airtap")
        self.assertEqual(state["last_reconcile_result"], "static_preflight_rejected")


# ---------------------------------------------------------------------
# Candidate Popen() failure AFTER the old proven child was intentionally
# stopped -- must NOT be an ordinary retry; must immediately roll back.
# ---------------------------------------------------------------------
class PopenFailureAfterIntentionalStopTests(ReconciliationFixtureMixin, TransactionTestCase):
    def test_popen_failure_after_intentional_stop_triggers_immediate_rollback(self):
        """The REPLACEMENT candidate's own Popen() fails, but the
        SUBSEQUENT rollback-to-LKG launch succeeds -- isolating the
        one behavior this test actually targets (candidate Popen
        failure routes straight to rollback, not ordinary candidate
        retry/backoff) from a SEPARATE, already-covered-elsewhere
        concern (what happens if infra is so broken even the rollback
        itself can't launch -- see StaticFailureLeavesRunningGroup*
        and Phase 2's own existing rollback-failure tests)."""
        manager = em.EncoderManager()
        encoder = make_saved_encoder()
        pid_before, gen_before = self.bootstrap_accepted(manager, "airtap", encoder)

        encoder.bitrate_kbps = 256
        encoder.save()

        call_count = {"n": 0}

        def flaky_popen(cmd, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OSError("no such file or directory: liquidsoap")
            self._fake_pid_counter += 1
            proc = MagicMock()
            proc.pid = self._fake_pid_counter
            proc.poll.return_value = None
            proc.returncode = None
            self._live_procs[proc.pid] = proc
            return proc

        with patch.object(em.subprocess, "Popen", side_effect=flaky_popen):
            manager._check_health()

        # NOT an ordinary candidate retry -- rolled back immediately,
        # with its own fresh generation, never sitting in backoff.
        self.assertNotIn("airtap", manager._retry_at)
        self.assertEqual(manager._launch_kind.get("airtap"), "rollback")
        current = manager._current.get("airtap")
        self.assertIsNotNone(current)
        self.assertNotEqual(current["pid"], pid_before)
        self.assertNotEqual(current["generation"], gen_before)

    def test_popen_failure_after_intentional_stop_eventually_restores_lkg(self):
        manager = em.EncoderManager()
        encoder = make_saved_encoder()
        self.bootstrap_accepted(manager, "airtap", encoder)
        lkg_before, meta_before = lkg_module.read_lkg(em._slug("airtap"))

        encoder.bitrate_kbps = 256
        encoder.save()
        self._popen_should_fail = True
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._check_health()
        self._popen_should_fail = False

        self.qualify_via_check_health(manager)

        self.assertEqual(manager._launch_kind["airtap"], "accepted")
        lkg_after, meta_after = lkg_module.read_lkg(em._slug("airtap"))
        self.assertEqual(meta_after["fingerprint"], meta_before["fingerprint"])  # rolled back, not the rejected edit


# ---------------------------------------------------------------------
# Candidate LIVE failure (crash during probation) for a reconciliation-
# triggered candidate -- proves the SAME Phase 2 machinery handles it,
# not a parallel implementation.
# ---------------------------------------------------------------------
class ReconciliationCandidateLiveFailureTests(ReconciliationFixtureMixin, TransactionTestCase):
    def test_reconciliation_candidate_crash_during_probation_rolls_back(self):
        manager = em.EncoderManager()
        encoder = make_saved_encoder()
        self.bootstrap_accepted(manager, "airtap", encoder)
        lkg_before, meta_before = lkg_module.read_lkg(em._slug("airtap"))

        encoder.bitrate_kbps = 256
        encoder.save()
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._check_health()
        self.assertEqual(manager._launch_kind["airtap"], "candidate")

        self.exit_current_child(manager, "airtap", returncode=1)
        manager._check_health()  # observes the crash -> _reject_live_candidate -> _start_rollback

        self.qualify_via_check_health(manager)

        self.assertEqual(manager._launch_kind["airtap"], "accepted")
        lkg_after, meta_after = lkg_module.read_lkg(em._slug("airtap"))
        self.assertEqual(meta_after["fingerprint"], meta_before["fingerprint"])


# ---------------------------------------------------------------------
# Successful replacement, end to end.
# ---------------------------------------------------------------------
class SuccessfulReplacementTests(ReconciliationFixtureMixin, TransactionTestCase):
    def test_full_replacement_cycle_desired_running_accepted_converge(self):
        manager = em.EncoderManager()
        encoder = make_saved_encoder()
        pid_before, gen_before = self.bootstrap_accepted(manager, "airtap", encoder)

        encoder.bitrate_kbps = 256
        encoder.save()
        new_fp = lkg_module.compute_fingerprint("airtap", [encoder])

        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._check_health()

        self.assertEqual(manager._launch_kind["airtap"], "candidate")
        self.assertNotEqual(manager._current["airtap"]["pid"], pid_before)
        self.assertNotEqual(manager._current["airtap"]["generation"], gen_before)

        self.qualify_via_check_health(manager)

        self.assertEqual(manager._launch_kind["airtap"], "accepted")
        state = self.read_group_state("airtap")
        self.assertEqual(state["desired_fingerprint"], new_fp)
        self.assertEqual(state["running_fingerprint"], new_fp)
        self.assertEqual(state["accepted_fingerprint"], new_fp)
        self.assertEqual(state["reconcile_status"], "in_sync")
        lkg_script, lkg_meta = lkg_module.read_lkg(em._slug("airtap"))
        self.assertEqual(lkg_meta["fingerprint"], new_fp)


# ---------------------------------------------------------------------
# Rollback: rejected desired fingerprint not retried; running==accepted
# != desired.
# ---------------------------------------------------------------------
class RollbackConvergenceTests(ReconciliationFixtureMixin, TransactionTestCase):
    def test_failed_candidate_rolls_back_and_rejected_desired_not_retried(self):
        manager = em.EncoderManager()
        encoder = make_saved_encoder()
        self.bootstrap_accepted(manager, "airtap", encoder)
        old_fp = lkg_module.compute_fingerprint("airtap", [encoder])

        encoder.bitrate_kbps = 256
        encoder.save()
        bad_fp = lkg_module.compute_fingerprint("airtap", [encoder])
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._check_health()
        self.exit_current_child(manager, "airtap", returncode=1)
        manager._check_health()  # crash observed -> rollback launched
        self.qualify_via_check_health(manager)

        self.assertEqual(manager._launch_kind["airtap"], "accepted")
        state = self.read_group_state("airtap")
        self.assertEqual(state["running_fingerprint"], old_fp)
        self.assertEqual(state["accepted_fingerprint"], old_fp)
        self.assertEqual(state["desired_fingerprint"], bad_fp)
        self.assertNotEqual(state["running_fingerprint"], state["desired_fingerprint"])
        self.assertIn(bad_fp, manager._rejected_fingerprints.get("airtap", set()))

        pid_after_rollback = manager._current["airtap"]["pid"]
        gen_after_rollback = manager._current["airtap"]["generation"]
        with patch.object(validation_module, "validate_full_configuration") as mock_validate:
            manager._check_health()
        mock_validate.assert_not_called()  # rejected fingerprint short-circuited, no thrash
        self.assertEqual(manager._current["airtap"]["pid"], pid_after_rollback)
        self.assertEqual(manager._current["airtap"]["generation"], gen_after_rollback)


# ---------------------------------------------------------------------
# New group: bootstraps without disturbing an existing group.
# ---------------------------------------------------------------------
class NewGroupTests(ReconciliationFixtureMixin, TransactionTestCase):
    def test_new_group_bootstraps_without_disturbing_existing_group(self):
        manager = em.EncoderManager()
        encoder_a = make_saved_encoder(name="a", host="10.0.0.1")
        pid_a, gen_a = self.bootstrap_accepted(manager, "airtap", encoder_a)
        proc_a = self._live_procs[pid_a]

        make_saved_encoder(name="b", host="10.0.0.2", mount="/2", input_device="plughw:3,1")

        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._check_health()

        self.assertIn("plughw:3,1", manager._current)
        self.assertEqual(manager._launch_kind["plughw:3,1"], "candidate")
        self.assertEqual(manager._current["airtap"]["pid"], pid_a)
        self.assertEqual(manager._current["airtap"]["generation"], gen_a)
        proc_a.terminate.assert_not_called()


# ---------------------------------------------------------------------
# Removed group: only the removed group stops; retry canceled; other
# group untouched. Re-enabling gets correct LKG/candidate behavior.
# ---------------------------------------------------------------------
class RemovedGroupTests(ReconciliationFixtureMixin, TransactionTestCase):
    def test_removed_group_stops_others_untouched_no_resurrection(self):
        manager = em.EncoderManager()
        encoder_a = make_saved_encoder(name="a", host="10.0.0.1")
        encoder_b = make_saved_encoder(name="b", host="10.0.0.2", input_device="plughw:3,1")
        pid_a, gen_a = self.bootstrap_accepted(manager, "airtap", encoder_a)
        pid_b, gen_b = self.bootstrap_accepted(manager, "plughw:3,1", encoder_b)
        proc_a = self._live_procs[pid_a]
        proc_b = self._live_procs[pid_b]

        encoder_b.enabled = False
        encoder_b.save()

        manager._check_health()

        proc_b.terminate.assert_called_once()
        self.assertNotIn("plughw:3,1", manager._current)
        self.assertNotIn("plughw:3,1", manager._retry_at)
        self.assertNotIn("plughw:3,1", manager._launch_kind)
        self.assertFalse(em._group_state_path_for_slug("plughw_3_1").exists())
        # airtap is completely unaffected.
        self.assertEqual(manager._current["airtap"]["pid"], pid_a)
        self.assertEqual(manager._current["airtap"]["generation"], gen_a)
        proc_a.terminate.assert_not_called()

        # No resurrection on a later, unrelated tick.
        manager._check_health()
        self.assertNotIn("plughw:3,1", manager._current)

    def test_removal_does_not_delete_persistent_lkg(self):
        manager = em.EncoderManager()
        encoder = make_saved_encoder()
        self.bootstrap_accepted(manager, "airtap", encoder)
        script_before, meta_before = lkg_module.read_lkg(em._slug("airtap"))
        self.assertIsNotNone(script_before)

        encoder.enabled = False
        encoder.save()
        manager._check_health()

        script_after, meta_after = lkg_module.read_lkg(em._slug("airtap"))
        self.assertEqual(script_after, script_before)

    def test_re_enable_matching_lkg_takes_fast_path_no_probation(self):
        """Re-enabling a removed group whose configuration still
        matches its (never-deleted) LKG goes straight to "accepted" --
        no candidate probation needed, exactly matching bootstrap
        behavior for an unchanged configuration."""
        manager = em.EncoderManager()
        encoder = make_saved_encoder()
        self.bootstrap_accepted(manager, "airtap", encoder)

        encoder.enabled = False
        encoder.save()
        manager._check_health()
        self.assertNotIn("airtap", manager._current)

        encoder.enabled = True
        encoder.save()
        manager._check_health()

        self.assertIn("airtap", manager._current)
        self.assertEqual(manager._launch_kind["airtap"], "accepted")  # fast path, not "candidate"

    def test_re_enable_changed_configuration_goes_through_candidate_pipeline(self):
        manager = em.EncoderManager()
        encoder = make_saved_encoder()
        self.bootstrap_accepted(manager, "airtap", encoder)

        encoder.enabled = False
        encoder.save()
        manager._check_health()

        encoder.enabled = True
        encoder.bitrate_kbps = 256  # now differs from the LKG
        encoder.save()
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._check_health()

        self.assertEqual(manager._launch_kind["airtap"], "candidate")


# ---------------------------------------------------------------------
# Input-device move: source first / target second in spirit (enforced
# by the cross-group collision check, not fixed ordering); destination
# never simultaneously live in both groups.
# ---------------------------------------------------------------------
class DesiredVsDesiredCollisionTests(ReconciliationFixtureMixin, TransactionTestCase):
    """Phase 3 review-fix pass, Issue 2: an intrinsically ambiguous
    DESIRED topology (two groups' desired rows claiming the same
    normalized destination) must never let reconciliation ORDER
    silently pick a winner -- neither side may launch until the
    operator resolves it, regardless of whether either side is
    currently running anything at all."""

    def test_two_brand_new_groups_same_destination_neither_launches(self):
        manager = em.EncoderManager()
        make_saved_encoder(name="a", host="10.0.0.5", port=9000, mount="/4", input_device="airtap")
        make_saved_encoder(name="b", host="10.0.0.5", port=9000, mount="/4", input_device="plughw:3,1")

        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            with patch("encoders.services.encoder_manager.emit_event") as mock_emit:
                manager._check_health()

        self.assertNotIn("airtap", manager._current)
        self.assertNotIn("plughw:3,1", manager._current)

        titles = [call.kwargs.get("title", "") for call in mock_emit.call_args_list]
        conflict_titles = [t for t in titles if "conflicts with another group's desired configuration" in t]
        # Both involved groups independently reported the conflict.
        self.assertEqual(len(conflict_titles), 2)

        for slug in ("airtap", "plughw_3_1"):
            state = self.read_group_state(slug)
            self.assertEqual(state["last_reconcile_result"], "blocked_desired_collision")

    def test_two_changed_existing_groups_converge_neither_replacement_launches(self):
        manager = em.EncoderManager()
        encoder_a = make_saved_encoder(name="a", host="10.0.0.1", mount="/1")
        encoder_b = make_saved_encoder(name="b", host="10.0.0.2", mount="/2", input_device="plughw:3,1")
        pid_a, gen_a = self.bootstrap_accepted(manager, "airtap", encoder_a)
        pid_b, gen_b = self.bootstrap_accepted(manager, "plughw:3,1", encoder_b)
        proc_a = self._live_procs[pid_a]
        proc_b = self._live_procs[pid_b]

        # Both groups edited to the SAME new destination.
        encoder_a.host = "10.0.0.9"
        encoder_a.mount = "/5"
        encoder_a.save()
        encoder_b.host = "10.0.0.9"
        encoder_b.mount = "/5"
        encoder_b.save()

        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._check_health()

        # Neither replacement launched -- both old children remain
        # running, completely untouched.
        self.assertEqual(manager._current["airtap"]["pid"], pid_a)
        self.assertEqual(manager._current["airtap"]["generation"], gen_a)
        self.assertEqual(manager._current["plughw:3,1"]["pid"], pid_b)
        self.assertEqual(manager._current["plughw:3,1"]["generation"], gen_b)
        proc_a.terminate.assert_not_called()
        proc_b.terminate.assert_not_called()
        self.assertEqual(manager._launch_kind["airtap"], "accepted")
        self.assertEqual(manager._launch_kind["plughw:3,1"], "accepted")

    def test_legitimate_move_not_blocked_by_desired_desired_check(self):
        """The moved row can only ever be in ONE group's desired set
        at a time -- desired(old) and desired(new) never actually
        overlap once the move commits, so this must NOT be treated as
        a desired-vs-desired collision at all (only the existing
        running-vs-desired check legitimately serializes it -- see
        InputDeviceMoveTests)."""
        manager = em.EncoderManager()
        encoder = make_saved_encoder(host="10.0.0.9", mount="/7")
        self.bootstrap_accepted(manager, "airtap", encoder)

        encoder.input_device = "plughw:3,1"
        encoder.save()

        desired_groups = em._group_by_input_device(Encoder.objects.filter(enabled=True))
        conflicts = manager._desired_vs_desired_conflicts(desired_groups)
        self.assertEqual(conflicts, {})

    def test_unrelated_group_remains_eligible_despite_ab_collision(self):
        manager = em.EncoderManager()
        make_saved_encoder(name="a", host="10.0.0.5", port=9000, mount="/4", input_device="airtap")
        make_saved_encoder(name="b", host="10.0.0.5", port=9000, mount="/4", input_device="plughw:3,1")
        make_saved_encoder(name="c", host="10.0.0.7", port=9100, mount="/1", input_device="plughw:4,1")

        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._check_health()

        self.assertNotIn("airtap", manager._current)
        self.assertNotIn("plughw:3,1", manager._current)
        # C is entirely unrelated to A/B's collision and reconciles normally.
        self.assertIn("plughw:4,1", manager._current)
        self.assertEqual(manager._launch_kind["plughw:4,1"], "candidate")


class InputDeviceMoveTests(ReconciliationFixtureMixin, TransactionTestCase):
    def test_move_sole_encoder_no_duplicate_destination_ever_live(self):
        manager = em.EncoderManager()
        encoder = make_saved_encoder(host="10.0.0.9", mount="/7")
        self.bootstrap_accepted(manager, "airtap", encoder)

        encoder.input_device = "plughw:3,1"
        encoder.save()

        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._check_health()

        # airtap (source) has been removed -- it never simultaneously
        # holds the SAME destination as the new group (there IS no old
        # group left to hold it once removal completes in the same
        # tick a launch could occur).
        self.assertNotIn("airtap", manager._current)

        self.qualify_via_check_health(manager)

        self.assertIn("plughw:3,1", manager._current)
        self.assertEqual(manager._launch_kind["plughw:3,1"], "accepted")
        # At no point did BOTH groups hold this destination: airtap's
        # own _running_encoders entry is gone.
        self.assertNotIn("airtap", manager._running_encoders)

    def test_move_one_encoder_out_of_multi_encoder_group_is_a_change_not_a_removal(self):
        manager = em.EncoderManager()
        stay = make_saved_encoder(name="stay", host="10.0.0.1", mount="/1")
        move = make_saved_encoder(name="move", host="10.0.0.2", mount="/2")
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._launch_group("airtap", [stay, move])
            self.qualify_ok(manager, "airtap")
        pid_before = manager._current["airtap"]["pid"]

        move.input_device = "plughw:3,1"
        move.save()

        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._check_health()

        # airtap is CHANGED (still has "stay"), not removed.
        self.assertIn("airtap", manager._current)
        self.assertEqual(manager._launch_kind["airtap"], "candidate")
        self.assertNotEqual(manager._current["airtap"]["pid"], pid_before)

    def test_old_group_candidate_failure_does_not_launch_new_group_with_duplicate(self):
        """If the OLD group's own replacement (post-move, minus the
        moved encoder) fails validation, the new group's launch must
        still never end up holding the SAME destination as anything
        the old group's LKG still describes -- covered by the
        cross-group check regardless of which side reconciles first."""
        manager = em.EncoderManager()
        encoder = make_saved_encoder(host="10.0.0.9", mount="/7")
        self.bootstrap_accepted(manager, "airtap", encoder)
        old_fp = lkg_module.compute_fingerprint("airtap", [encoder])

        encoder.input_device = "plughw:3,1"
        encoder.save()
        # airtap is now "removed" (0 enabled encoders) -- that always
        # succeeds unconditionally (Phase 3C), so this scenario reduces
        # to: airtap's LKG (still on disk, unaffected by removal)
        # describes the old destination; the new group must still be
        # allowed to launch it once airtap is no longer actually
        # running it (which it isn't, right after removal).
        manager._check_health()
        self.assertNotIn("airtap", manager._current)
        self.assertNotIn("airtap", manager._running_encoders)

        script_before, meta_before = lkg_module.read_lkg(em._slug("airtap"))
        self.assertEqual(meta_before["fingerprint"], old_fp)  # LKG untouched by removal

        self.qualify_via_check_health(manager)
        self.assertEqual(manager._launch_kind.get("plughw:3,1"), "accepted")


# ---------------------------------------------------------------------
# Rapid edits: candidate A in progress -> DB becomes B -> DB becomes C
# -> no thrash, eventually reconciles C only.
# ---------------------------------------------------------------------
class RapidEditCoalescingTests(ReconciliationFixtureMixin, TransactionTestCase):
    def test_intermediate_edit_during_probation_is_never_itself_launched(self):
        manager = em.EncoderManager()
        encoder = make_saved_encoder(bitrate_kbps=192)
        self.bootstrap_accepted(manager, "airtap", encoder)

        encoder.bitrate_kbps = 256  # edit "B"
        encoder.save()
        fp_b = lkg_module.compute_fingerprint("airtap", [encoder])
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._check_health()
        self.assertEqual(manager._candidate_fingerprint["airtap"], fp_b)

        # While B is still on probation (never healthy yet), save
        # edit "C" -- a third, different configuration.
        encoder.bitrate_kbps = 320
        encoder.save()
        fp_c = lkg_module.compute_fingerprint("airtap", [encoder])
        self.assertNotEqual(fp_b, fp_c)

        # Several ticks pass with B still probating (health mocked
        # "unknown", never "ok") -- reconciliation must not thrash.
        with patch("monitoring.services.probes.evaluate_encoder_group_health", return_value=("unknown", {"reason": "starting"})):
            for _ in range(3):
                manager._check_health()
        # STILL running B's own candidate fingerprint -- C was never
        # applied mid-flight, and B was never abandoned/relaunched
        # just because C showed up.
        self.assertEqual(manager._candidate_fingerprint["airtap"], fp_b)
        self.assertEqual(manager._launch_kind["airtap"], "candidate")

        # Let B finish qualifying and promote -- exactly as Phase 3I
        # explicitly allows ("acceptable for an older candidate that
        # is already running to finish qualification and briefly
        # become LKG before the newest desired state is applied").
        # qualify_via_check_health drives several ticks in one call,
        # and the transition slot frees up the MOMENT B promotes -- so
        # by the time control returns here, reconciliation may already
        # have moved straight on to C within the same call (a GOOD
        # sign, not a race to chase): accepted_fingerprint, which only
        # ever changes AT promotion, is the durable proof B really did
        # promote first, regardless of exactly which tick this
        # assertion lands on.
        self.qualify_via_check_health(manager)
        self.assertEqual(manager._accepted_fingerprint["airtap"], fp_b)
        self.assertNotEqual(manager._accepted_fingerprint["airtap"], fp_c)

        # Drain any remaining ticks so C's own replacement (already
        # under way, or about to begin) reaches "candidate" -- proving
        # reconciliation coalesced straight to the LATEST desired
        # snapshot, never anything else in between.
        with patch("monitoring.services.probes.evaluate_encoder_group_health", return_value=("unknown", {"reason": "starting"})):
            for _ in range(3):
                manager._check_health()
        self.assertEqual(manager._launch_kind["airtap"], "candidate")
        self.assertEqual(manager._candidate_fingerprint["airtap"], fp_c)


# ---------------------------------------------------------------------
# Cross-group duplicate destination: unsafe target launch blocked while
# current streams remain healthy.
# ---------------------------------------------------------------------
class CrossGroupCollisionTests(ReconciliationFixtureMixin, TransactionTestCase):
    def test_new_group_wanting_a_live_destination_is_blocked_not_launched(self):
        manager = em.EncoderManager()
        encoder_a = make_saved_encoder(name="a", host="10.0.0.5", port=9000, mount="/9")
        pid_a, gen_a = self.bootstrap_accepted(manager, "airtap", encoder_a)
        proc_a = self._live_procs[pid_a]

        # Same normalized destination (shoutcast2, host, port, sid),
        # different input_device group.
        make_saved_encoder(name="b", host="10.0.0.5", port=9000, mount="/9", input_device="plughw:3,1")

        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            with patch("encoders.services.encoder_manager.emit_event") as mock_emit:
                manager._check_health()

        self.assertNotIn("plughw:3,1", manager._current)  # never launched
        self.assertEqual(manager._current["airtap"]["pid"], pid_a)
        self.assertEqual(manager._current["airtap"]["generation"], gen_a)
        proc_a.terminate.assert_not_called()

        # Both the (Issue 2) desired-vs-desired check and the running-
        # vs-desired check (_cross_group_collision_blocked) genuinely
        # apply here -- airtap's own DESIRED configuration still
        # includes this destination too (nothing changed it), so the
        # desired-vs-desired pre-filter (evaluated first, before "b"
        # is even considered a dispatch candidate) is what actually
        # reports it. Either wording would correctly describe the
        # block; asserting on the one that's actually emitted.
        titles = [call.kwargs.get("title", "") for call in mock_emit.call_args_list]
        self.assertTrue(any("conflicts with another group's desired configuration" in t for t in titles))

    def test_collision_self_heals_once_other_group_relinquishes(self):
        manager = em.EncoderManager()
        encoder_a = make_saved_encoder(name="a", host="10.0.0.5", port=9000, mount="/9")
        self.bootstrap_accepted(manager, "airtap", encoder_a)
        make_saved_encoder(name="b", host="10.0.0.5", port=9000, mount="/9", input_device="plughw:3,1")

        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._check_health()
        self.assertNotIn("plughw:3,1", manager._current)

        # airtap's own encoder is disabled -- it relinquishes the
        # destination.
        encoder_a.enabled = False
        encoder_a.save()
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._check_health()  # removes airtap
            manager._check_health()  # now group b can launch, unblocked

        self.assertIn("plughw:3,1", manager._current)


# ---------------------------------------------------------------------
# Phase 3 review-fix pass, Issue 1: a legacy LKG (promoted before
# protocol/mount were added to the persisted destination metadata --
# see lkg.py's _promote_candidate) must not create a permanent
# cross-group collision blind spot, but DB values may NEVER be
# substituted for a legacy/incomplete LKG's identity unless the LKG's
# own accepted fingerprint is PROVEN to still equal the current
# desired fingerprint.
# ---------------------------------------------------------------------
class LegacyLkgCollisionTests(ReconciliationFixtureMixin, TransactionTestCase):
    def _write_legacy_lkg(self, slug, encoders, script="generation = \"g\"\n"):
        """A promoted LKG whose metadata predates protocol/mount --
        exactly the shape encoder_manager.py wrote before this review-
        fix pass. Returns the fingerprint it was written under."""
        fp = lkg_module.compute_fingerprint(slug, encoders)
        meta = {
            "fingerprint": fp, "input_device": slug,
            "encoder_ids": [e.id for e in encoders], "encoder_names": [e.name for e in encoders],
            "destinations": [
                {"encoder_id": e.id, "name": e.name, "host": e.host, "port": e.port, "shoutcast_sid": e.shoutcast_sid}
                for e in encoders
            ],
        }
        lkg_module.write_lkg(em._slug(slug), script, meta)
        return fp

    def test_legacy_matching_case_collision_key_can_be_derived(self):
        """LKG valid, fingerprint == current desired DB fingerprint,
        legacy destinations omit protocol/mount -- no "can't check"
        blind spot: after one reconciliation tick (which self-heals
        the moment desired==running is established), a genuine
        cross-group collision against this legacy-accepted group IS
        correctly detected."""
        manager = em.EncoderManager()
        encoder = make_saved_encoder(host="10.0.0.5", port=9000, mount="/9")
        self._write_legacy_lkg("airtap", [encoder])

        # Bootstrap: desired == lkg fingerprint -> fast path, real DB
        # rows recorded directly (already complete) -- so drive this
        # through the ACTUAL rollback/fallback path instead, which is
        # where an incomplete stand-in genuinely gets recorded.
        manager._launch_group("airtap", [encoder])
        self.assertEqual(manager._launch_kind["airtap"], "accepted")
        # Confirm this test's own premise: the stand-in path is what's
        # actually recorded (the fast path -- desired==lkg -- was
        # taken here, which already uses real rows; simulate the
        # legacy-incomplete-cache condition directly, matching the
        # real-world trigger -- a rollback landing an incomplete
        # stand-in, later left unrefreshed).
        manager._running_encoders["airtap"] = lkg_module.destinations_from_lkg_meta(
            lkg_module.read_lkg_meta(em._slug("airtap"))
        )
        self.assertIsNone(manager._running_encoders["airtap"][0].protocol)  # legacy: incomplete

        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._check_health()  # one tick: desired==running -> self-heal fires

        healed = manager._running_encoders["airtap"]
        self.assertEqual(healed[0].protocol, "shoutcast2")  # enriched from real DB rows

        # Now a genuine cross-group collision against this (now-
        # complete) legacy-accepted group must actually be blocked.
        make_saved_encoder(name="b", host="10.0.0.5", port=9000, mount="/9", input_device="plughw:3,1")
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._check_health()
        self.assertNotIn("plughw:3,1", manager._current)

    def test_legacy_mismatched_case_db_values_never_substituted(self):
        """LKG accepted at SID1 (legacy metadata, no protocol/mount).
        DB desired is a REJECTED SID5 edit. The rejected DB values
        must NEVER be used to fill in the LKG's missing identity --
        confirms the fix to _launch_group's rejected-fingerprint
        fallback (Issue 1's underlying bug: it used to record the
        REJECTED encoders as "running" instead of the LKG's own
        identity)."""
        manager = em.EncoderManager()
        accepted_encoder = make_saved_encoder(host="10.0.0.5", port=9000, mount="/1")
        self._write_legacy_lkg("airtap", [accepted_encoder])

        # Now the DB is edited to a DIFFERENT (about-to-be-rejected)
        # configuration -- SID 5, a different destination entirely.
        accepted_encoder.mount = "/5"
        accepted_encoder.host = "10.0.0.9"
        accepted_encoder.save()
        rejected_fp = lkg_module.compute_fingerprint("airtap", [accepted_encoder])
        manager._rejected_fingerprints["airtap"] = {rejected_fp}

        ok = manager._launch_group("airtap", [accepted_encoder])
        self.assertTrue(ok)
        self.assertEqual(manager._launch_kind["airtap"], "accepted")

        running = manager._running_encoders["airtap"]
        # The recorded "running" identity must be the LKG's own (SID
        # 1 / host 10.0.0.5) -- NEVER the rejected DB row's values
        # (SID 5 / host 10.0.0.9).
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0].host, "10.0.0.5")
        self.assertEqual(running[0].shoutcast_sid, "1")
        self.assertIsNone(running[0].protocol)  # legacy, correctly NOT enriched (fingerprints differ)

    def test_legacy_mismatched_case_no_self_heal_when_desired_differs(self):
        """The self-heal in _reconcile_inner's "unchanged" branch must
        never fire while desired genuinely differs from running -- a
        "changed" classification goes through the ordinary candidate
        pipeline instead (which, once it actually launches, correctly
        records the NEW real candidate rows -- never a silent identity
        swap performed BY the self-heal check itself, which must not
        even run this tick)."""
        manager = em.EncoderManager()
        accepted_encoder = make_saved_encoder(host="10.0.0.5", port=9000, mount="/1")
        self._write_legacy_lkg("airtap", [accepted_encoder])
        manager._launch_group("airtap", [accepted_encoder])  # fast path, real rows (complete)
        # Force the recorded identity back to an incomplete stand-in,
        # simulating a prior rollback that's never since been healed.
        manager._running_encoders["airtap"] = lkg_module.destinations_from_lkg_meta(
            lkg_module.read_lkg_meta(em._slug("airtap"))
        )
        manager._running_fingerprint["airtap"] = lkg_module.compute_fingerprint("airtap", [accepted_encoder])

        accepted_encoder.mount = "/5"
        accepted_encoder.save()  # now desired != running/accepted -- a "changed" group, not "unchanged"

        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._check_health()

        # The group was replaced via the ordinary candidate pipeline
        # (not the self-heal path) -- now correctly running the NEW,
        # real (complete) SID 5 candidate, never a silently-substituted
        # SID 1 rejected-or-otherwise identity.
        self.assertEqual(manager._launch_kind["airtap"], "candidate")
        running = manager._running_encoders["airtap"]
        self.assertEqual(running[0].shoutcast_sid, "5")
        self.assertEqual(running[0].protocol, "shoutcast2")

    def test_cross_group_collision_using_legacy_accepted_group_is_blocked(self):
        """The exact scenario required by the review: a legacy
        accepted group's destination (SID 1) must still block a new
        desired group attempting the same normalized destination,
        even though the legacy LKG itself never recorded protocol/
        mount -- proving the collision key CAN be derived (via the
        proven-matching self-heal), not silently skipped."""
        manager = em.EncoderManager()
        encoder = make_saved_encoder(host="10.0.0.5", port=9000, mount="/1")
        self._write_legacy_lkg("airtap", [encoder])
        manager._launch_group("airtap", [encoder])
        manager._running_encoders["airtap"] = lkg_module.destinations_from_lkg_meta(
            lkg_module.read_lkg_meta(em._slug("airtap"))
        )

        make_saved_encoder(name="b", host="10.0.0.5", port=9000, mount="/1", input_device="plughw:3,1")

        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._check_health()  # tick 1: airtap self-heals (unchanged, desired==running)
            manager._check_health()  # tick 2: new group b's collision check now sees the healed, complete identity

        self.assertNotIn("plughw:3,1", manager._current)


# ---------------------------------------------------------------------
# Aircheck/airtap protection: changing a NON-airtap group leaves the
# airtap process, its generation, and (by construction -- see
# _reconcile's per-device isolation) its telnet/aircheck hosting
# completely untouched.
# ---------------------------------------------------------------------
class AircheckAirtapProtectionTests(ReconciliationFixtureMixin, TransactionTestCase):
    def test_changing_other_group_leaves_airtap_pid_and_generation_untouched(self):
        manager = em.EncoderManager()
        airtap_encoder = make_saved_encoder(name="airtap-enc", host="10.0.0.1")
        other_encoder = make_saved_encoder(name="other-enc", host="10.0.0.2", input_device="plughw:3,1")
        pid_air, gen_air = self.bootstrap_accepted(manager, em.DEFAULT_INPUT_DEVICE, airtap_encoder)
        self.bootstrap_accepted(manager, "plughw:3,1", other_encoder)
        proc_air = self._live_procs[pid_air]

        other_encoder.bitrate_kbps = 256
        other_encoder.save()

        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._check_health()

        self.assertEqual(manager._current[em.DEFAULT_INPUT_DEVICE]["pid"], pid_air)
        self.assertEqual(manager._current[em.DEFAULT_INPUT_DEVICE]["generation"], gen_air)
        proc_air.terminate.assert_not_called()
        self.assertEqual(manager._launch_kind["plughw:3,1"], "candidate")

    def test_airtap_replacement_only_launches_new_child_after_old_one_confirmed_stopped(self):
        """The generated script only ever hosts telnet/aircheck for
        DEFAULT_INPUT_DEVICE (host_aircheck = input_device ==
        DEFAULT_INPUT_DEVICE, set in _static_check_candidate/
        _launch_group) -- so two children never compete for port 1234
        as long as the old one is confirmed gone before the new one is
        launched, which _reconcile_changed_group always does via
        _stop_group_intentionally BEFORE _start_group."""
        manager = em.EncoderManager()
        encoder = make_saved_encoder(host="10.0.0.1")
        self.bootstrap_accepted(manager, em.DEFAULT_INPUT_DEVICE, encoder)

        encoder.bitrate_kbps = 256
        encoder.save()

        stop_order = []
        real_stop = manager._stop_group_intentionally
        real_start = manager._start_group

        def spy_stop(device, reason):
            stop_order.append(("stop", device))
            return real_stop(device, reason)

        def spy_start(device, encoders, script_override=None):
            stop_order.append(("start", device))
            return real_start(device, encoders, script_override=script_override)

        with patch.object(manager, "_stop_group_intentionally", side_effect=spy_stop):
            with patch.object(manager, "_start_group", side_effect=spy_start):
                with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
                    manager._check_health()

        self.assertEqual(stop_order, [("stop", em.DEFAULT_INPUT_DEVICE), ("start", em.DEFAULT_INPUT_DEVICE)])


# ---------------------------------------------------------------------
# Intentional stop: cannot confirm the old process is dead -> refuses
# to launch a second one. Filesystem/error containment for the new
# Phase 3 helpers, matching Phase 2's existing _guarded guarantees.
# ---------------------------------------------------------------------
class IntentionalStopSafetyTests(ReconciliationFixtureMixin, TransactionTestCase):
    def test_unconfirmed_stop_refuses_to_launch_replacement(self):
        manager = em.EncoderManager()
        encoder = make_saved_encoder()
        pid_before, gen_before = self.bootstrap_accepted(manager, "airtap", encoder)
        proc = self._live_procs[pid_before]
        # terminate() -> wait() times out, kill() -> wait() ALSO times
        # out -- the genuinely rare "can't confirm death even after
        # SIGKILL" case (e.g. a process stuck in uninterruptible I/O).
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd="liquidsoap", timeout=5)
        calls_before = self.popen_call_count()

        encoder.bitrate_kbps = 256
        encoder.save()

        with patch("os.kill") as mock_kill:  # last-resort liveness check also says "still alive"
            mock_kill.return_value = None
            with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
                with patch("encoders.services.encoder_manager.emit_event") as mock_emit:
                    manager._check_health()

        # Nothing was cleared or resurrected -- since the old process's
        # death could NOT be confirmed, the safest available state is
        # to leave everything describing it exactly as it was (the
        # conservative "presumed still alive" reading), never a second
        # process launched against the same device.
        self.assertEqual(manager._current["airtap"]["pid"], pid_before)
        self.assertEqual(manager._current["airtap"]["generation"], gen_before)
        titles = [call.kwargs.get("title", "") for call in mock_emit.call_args_list]
        self.assertTrue(any("could not confirm prior process stopped" in t for t in titles))
        # No replacement was ever launched for this device.
        self.assertEqual(self.popen_call_count(), calls_before)

    def test_reconcile_scan_exception_does_not_crash_check_health(self):
        manager = em.EncoderManager()
        make_saved_encoder()
        with patch("encoders.services.encoder_manager._group_by_input_device", side_effect=RuntimeError("boom")):
            manager._check_health()  # must not raise
        # The manager loop survives -- a subsequent, un-patched tick
        # works normally.
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._check_health()
        self.assertIn("airtap", manager._current)

    def test_reconcile_scan_exception_emits_event(self):
        manager = em.EncoderManager()
        make_saved_encoder()
        with patch("encoders.services.encoder_manager._group_by_input_device", side_effect=RuntimeError("boom")):
            with patch("encoders.services.encoder_manager.emit_event") as mock_emit:
                manager._check_health()
        titles = [call.kwargs.get("title", "") for call in mock_emit.call_args_list]
        self.assertTrue(any("reconciliation scan failed" in t for t in titles))
