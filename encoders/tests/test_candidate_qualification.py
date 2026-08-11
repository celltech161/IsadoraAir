"""Phase 2G/2H/2I/2J/2K -- candidate probation, live qualification,
promotion, automatic rollback, and rollback qualification. Extends
EncoderManagerFixtureMixin (test_encoder_manager.py) with patched
candidate/LKG directories and a fast qualification clock so these
tests run in milliseconds, not CANDIDATE_QUALIFICATION_SECONDS real
seconds. subprocess.Popen is mocked throughout (inherited from the
fixture) -- no real Liquidsoap process is ever spawned. Real
`liquidsoap --check` IS exercised in a small number of end-to-end
tests, skipped cleanly if liquidsoap isn't installed, matching this
project's established convention."""
import json
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import TransactionTestCase

import encoders.services.encoder_manager as em
from encoders.models import Encoder
from encoders.services import lkg as lkg_module
from encoders.services import preflight as preflight_module
from encoders.services import validation
from encoders.tests.test_encoder_manager import EncoderManagerFixtureMixin, make_encoder
from monitoring.services import probes as probes_module

# A minimal but structurally valid stand-in LKG script -- anything
# relaunched via script_override goes through _substitute_generation,
# which requires exactly one `generation = "..."` line to exist. Real
# LKG scripts (written by _promote_candidate) always satisfy this since
# they're the actual rendered output of build_liquidsoap_script; these
# hand-written placeholders need it added explicitly.
FAKE_LKG_SCRIPT = 'generation = "fake-lkg-gen"\n# placeholder LKG script body for testing\n'


class CandidateFixtureMixin(EncoderManagerFixtureMixin):
    """Adds patched candidate/LKG dirs and a fast qualification clock
    on top of the existing STATE_DIR/SCRIPT_DIR/Popen mocking."""

    def setUp(self):
        super().setUp()
        self._cand_tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._cand_tmpdir.cleanup)
        base = Path(self._cand_tmpdir.name)
        for patcher in (
            patch.object(lkg_module, "CANDIDATE_DIR", base / "candidate"),
            patch.object(lkg_module, "LKG_DIR", base / "lkg"),
            # Fast clock: real value (30s) would make every qualifying
            # test take 30+ real seconds. 0.05s is long enough for
            # several _check_health() ticks to matter but short enough
            # for a normal test run.
            patch.object(em, "CANDIDATE_QUALIFICATION_SECONDS", 0.05),
            patch.object(em, "CANDIDATE_QUALIFICATION_DEADLINE_SECONDS", 5),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def qualify_ok(self, manager, input_device, ticks=3, sleep=0.02):
        """Drives _check_candidate_qualification enough times, with
        evaluate_encoder_group_health mocked to report "ok" throughout,
        to cross CANDIDATE_QUALIFICATION_SECONDS and trigger promotion
        (or rollback success)."""
        with patch("monitoring.services.probes.evaluate_encoder_group_health", return_value=("ok", {"reason": "healthy"})):
            for _ in range(ticks):
                manager._check_candidate_qualification(input_device)
                time.sleep(sleep)
            manager._check_candidate_qualification(input_device)


# ---------------------------------------------------------------------
# _launch_group -- fast-path / candidate decision
# ---------------------------------------------------------------------
class LaunchGroupDecisionTests(CandidateFixtureMixin, TransactionTestCase):
    def test_no_lkg_no_prior_rejection_goes_through_candidate_pipeline(self):
        manager = em.EncoderManager()
        encoders = [make_encoder()]
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            ok = manager._launch_group("airtap", encoders)
        self.assertTrue(ok)
        self.assertEqual(manager._launch_kind["airtap"], "candidate")

    def test_matching_lkg_fingerprint_takes_fast_path_no_preflight_call(self):
        manager = em.EncoderManager()
        encoders = [make_encoder()]
        fp = lkg_module.compute_fingerprint("airtap", encoders)
        lkg_module.write_lkg(em._slug("airtap"), FAKE_LKG_SCRIPT, {"fingerprint": fp})
        mock_preflight = MagicMock()
        with patch.object(preflight_module, "run_preflight", mock_preflight):
            ok = manager._launch_group("airtap", encoders)
        self.assertTrue(ok)
        self.assertEqual(manager._launch_kind["airtap"], "accepted")
        mock_preflight.assert_not_called()

    def test_mismatched_lkg_fingerprint_goes_through_candidate_pipeline(self):
        manager = em.EncoderManager()
        encoders = [make_encoder()]
        lkg_module.write_lkg(em._slug("airtap"), "old script", {"fingerprint": "some-other-fingerprint"})
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            ok = manager._launch_group("airtap", encoders)
        self.assertTrue(ok)
        self.assertEqual(manager._launch_kind["airtap"], "candidate")

    def test_rejected_fingerprint_falls_back_to_lkg_not_retried(self):
        manager = em.EncoderManager()
        encoders = [make_encoder()]
        fp = lkg_module.compute_fingerprint("airtap", encoders)
        lkg_module.write_lkg(em._slug("airtap"), FAKE_LKG_SCRIPT, {"fingerprint": "different-fp"})
        manager._rejected_fingerprints["airtap"] = {fp}
        mock_preflight = MagicMock()
        with patch.object(preflight_module, "run_preflight", mock_preflight):
            ok = manager._launch_group("airtap", encoders)
        self.assertTrue(ok)
        self.assertEqual(manager._launch_kind["airtap"], "accepted")
        mock_preflight.assert_not_called()

    def test_rejected_fingerprint_no_lkg_falls_through_to_candidate(self):
        """No LKG to fall back to -- even a rejected fingerprint gets
        another attempt (there's nothing safer to run instead)."""
        manager = em.EncoderManager()
        encoders = [make_encoder()]
        fp = lkg_module.compute_fingerprint("airtap", encoders)
        manager._rejected_fingerprints["airtap"] = {fp}
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            ok = manager._launch_group("airtap", encoders)
        self.assertTrue(ok)
        self.assertEqual(manager._launch_kind["airtap"], "candidate")

    def test_different_fingerprint_not_in_rejected_set_gets_a_fresh_attempt(self):
        manager = em.EncoderManager()
        old_encoders = [make_encoder(name="old")]
        new_encoders = [make_encoder(name="new", host="different-host")]
        old_fp = lkg_module.compute_fingerprint("airtap", old_encoders)
        manager._rejected_fingerprints["airtap"] = {old_fp}
        lkg_module.write_lkg(em._slug("airtap"), "lkg", {"fingerprint": "yet-another-fp"})
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            ok = manager._launch_group("airtap", new_encoders)
        self.assertTrue(ok)
        self.assertEqual(manager._launch_kind["airtap"], "candidate")  # not silently skipped

    def test_critical_stopped_and_rejected_fingerprint_launches_lkg_only(self):
        """critical_stopped + a fingerprint that's ALSO already been
        rejected: still LKG-only, no candidate pipeline -- the
        rejected-fingerprint check alone already covers this case
        (see the two tests below for why critical_stopped is no
        longer, by itself, a separate blocking condition)."""
        manager = em.EncoderManager()
        encoders = [make_encoder()]
        fp = lkg_module.compute_fingerprint("airtap", encoders)
        lkg_module.write_lkg(em._slug("airtap"), FAKE_LKG_SCRIPT, {"fingerprint": "whatever"})
        manager._critical_stopped["airtap"] = True
        manager._rejected_fingerprints["airtap"] = {fp}
        mock_preflight = MagicMock()
        with patch.object(preflight_module, "run_preflight", mock_preflight):
            ok = manager._launch_group("airtap", encoders)
        self.assertTrue(ok)
        self.assertEqual(manager._launch_kind["airtap"], "accepted")
        mock_preflight.assert_not_called()

    def test_critical_stopped_with_never_tried_fingerprint_gets_a_fresh_attempt(self):
        """Phase 3 fix: critical_stopped ALONE must NOT permanently
        block a genuinely new (never-rejected) desired fingerprint --
        under Phase 2 this was unreachable in practice (the only way
        to clear critical_stopped was a full process restart, which
        always cleared it fresh in the same breath); Phase 3
        reconciliation can keep a process alive for a long time across
        many DB edits while critical_stopped, so the admin's own
        promised recovery path ("fix the underlying problem, then save
        a corrected configuration to try again") must actually be
        true, not silently ignored until an operator restarts the
        whole service."""
        manager = em.EncoderManager()
        encoders = [make_encoder()]
        lkg_module.write_lkg(em._slug("airtap"), FAKE_LKG_SCRIPT, {"fingerprint": "whatever"})
        manager._critical_stopped["airtap"] = True
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            ok = manager._launch_group("airtap", encoders)
        self.assertTrue(ok)
        self.assertEqual(manager._launch_kind["airtap"], "candidate")

    def test_critical_stopped_clears_on_successful_promotion_of_new_fingerprint(self):
        manager = em.EncoderManager()
        encoders = [make_encoder()]
        lkg_module.write_lkg(em._slug("airtap"), FAKE_LKG_SCRIPT, {"fingerprint": "whatever"})
        manager._critical_stopped["airtap"] = True
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._launch_group("airtap", encoders)
            self.qualify_ok(manager, "airtap")
        self.assertEqual(manager._launch_kind["airtap"], "accepted")
        self.assertNotIn("airtap", manager._critical_stopped)

    def test_critical_stopped_no_lkg_returns_false(self):
        manager = em.EncoderManager()
        encoders = [make_encoder()]
        manager._critical_stopped["airtap"] = True
        ok = manager._launch_group("airtap", encoders)
        self.assertFalse(ok)


# ---------------------------------------------------------------------
# Validation / preflight failure -- must NOT stop a currently healthy
# encoder. THE critical regression test.
# ---------------------------------------------------------------------
class StaticFailureDoesNotStopHealthyEncoderTests(CandidateFixtureMixin, TransactionTestCase):
    def test_validation_failure_falls_back_to_lkg_when_one_exists(self):
        manager = em.EncoderManager()
        bad_encoders = [make_encoder(host="")]  # fails validation: blank host
        lkg_module.write_lkg(em._slug("airtap"), FAKE_LKG_SCRIPT, {"fingerprint": "good-fp"})
        ok = manager._launch_group("airtap", bad_encoders)
        self.assertTrue(ok, "must still launch SOMETHING (the LKG) rather than leaving the group down")
        self.assertEqual(manager._launch_kind["airtap"], "accepted")

    def test_validation_failure_records_rejection_not_silently_dropped(self):
        manager = em.EncoderManager()
        bad_encoders = [make_encoder(host="")]
        fp = lkg_module.compute_fingerprint("airtap", bad_encoders)
        manager._launch_group("airtap", bad_encoders)
        self.assertIn(fp, manager._rejected_fingerprints.get("airtap", set()))

    def test_validation_failure_no_lkg_returns_false_not_crash(self):
        manager = em.EncoderManager()
        bad_encoders = [make_encoder(host="")]
        ok = manager._launch_group("airtap", bad_encoders)
        self.assertFalse(ok)

    def test_preflight_failure_falls_back_to_lkg(self):
        manager = em.EncoderManager()
        encoders = [make_encoder()]
        lkg_module.write_lkg(em._slug("airtap"), FAKE_LKG_SCRIPT, {"fingerprint": "good-fp"})
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=False, reason="liquidsoap --check failed")):
            ok = manager._launch_group("airtap", encoders)
        self.assertTrue(ok)
        self.assertEqual(manager._launch_kind["airtap"], "accepted")

    def test_preflight_failure_candidate_script_never_written_to_active_path(self):
        """The candidate render used for preflight must be a genuinely
        separate file from the active script path -- never touches
        SCRIPT_DIR/encoders_<slug>.liq directly."""
        manager = em.EncoderManager()
        encoders = [make_encoder()]
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=False, reason="bad")):
            manager._launch_group("airtap", encoders)
        active_path = em.SCRIPT_DIR / "encoders_airtap.liq"
        self.assertFalse(active_path.exists())

    def test_preflight_failure_leaves_no_stray_candidate_files(self):
        manager = em.EncoderManager()
        encoders = [make_encoder()]
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=False, reason="bad")):
            manager._launch_group("airtap", encoders)
        leftovers = list(lkg_module.CANDIDATE_DIR.glob("*.liq")) if lkg_module.CANDIDATE_DIR.exists() else []
        self.assertEqual(leftovers, [])


# ---------------------------------------------------------------------
# Qualification -> promotion
# ---------------------------------------------------------------------
class QualificationPromotionTests(CandidateFixtureMixin, TransactionTestCase):
    def _launch_candidate(self, manager, input_device="airtap", encoders=None):
        encoders = encoders or [make_encoder()]
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            ok = manager._launch_group(input_device, encoders)
        self.assertTrue(ok)
        return encoders

    def test_candidate_starts_in_probation_not_promoted_yet(self):
        manager = em.EncoderManager()
        self._launch_candidate(manager)
        self.assertEqual(manager._launch_kind["airtap"], "candidate")
        self.assertFalse(lkg_module.lkg_exists(em._slug("airtap")))

    def test_popen_success_alone_does_not_promote(self):
        """Phase 2F: a successful launch is not, by itself, LKG."""
        manager = em.EncoderManager()
        self._launch_candidate(manager)
        # No qualification ticks at all -- just launched.
        self.assertFalse(lkg_module.lkg_exists(em._slug("airtap")))

    def test_sustained_ok_health_promotes_to_lkg(self):
        manager = em.EncoderManager()
        self._launch_candidate(manager)
        self.qualify_ok(manager, "airtap")
        self.assertTrue(lkg_module.lkg_exists(em._slug("airtap")))
        self.assertEqual(manager._launch_kind["airtap"], "accepted")

    def test_promoted_lkg_fingerprint_matches_desired(self):
        manager = em.EncoderManager()
        encoders = self._launch_candidate(manager)
        fp = lkg_module.compute_fingerprint("airtap", encoders)
        self.qualify_ok(manager, "airtap")
        meta = lkg_module.read_lkg_meta(em._slug("airtap"))
        self.assertEqual(meta["fingerprint"], fp)

    def test_promotion_occurs_exactly_once(self):
        """A second qualification pass (e.g. a stray extra health tick
        after promotion) must not re-promote / re-write the LKG a
        second time with different accepted_at metadata."""
        manager = em.EncoderManager()
        self._launch_candidate(manager)
        self.qualify_ok(manager, "airtap")
        first_meta = lkg_module.read_lkg_meta(em._slug("airtap"))
        # launch_kind is now "accepted" -- a further qualification
        # check call is a no-op by construction (kind not in
        # ("candidate", "rollback")).
        manager._check_candidate_qualification("airtap")
        second_meta = lkg_module.read_lkg_meta(em._slug("airtap"))
        self.assertEqual(first_meta["accepted_at"], second_meta["accepted_at"])

    def test_not_ok_status_prevents_promotion(self):
        manager = em.EncoderManager()
        self._launch_candidate(manager)
        with patch("monitoring.services.probes.evaluate_encoder_group_health", return_value=("warning", {"reason": "stabilizing"})):
            for _ in range(3):
                manager._check_candidate_qualification("airtap")
                time.sleep(0.02)
        self.assertFalse(lkg_module.lkg_exists(em._slug("airtap")))
        self.assertEqual(manager._launch_kind["airtap"], "candidate")  # still on probation

    def test_intermittent_ok_does_not_accumulate_across_gaps(self):
        """A single non-"ok" tick between two "ok" ticks must reset
        the continuous-health clock -- CANDIDATE_QUALIFICATION_SECONDS
        must be CONTINUOUS, not cumulative."""
        manager = em.EncoderManager()
        self._launch_candidate(manager)
        with patch("monitoring.services.probes.evaluate_encoder_group_health") as mock_health:
            mock_health.return_value = ("ok", {})
            manager._check_candidate_qualification("airtap")
            time.sleep(0.03)
            mock_health.return_value = ("warning", {"reason": "blip"})
            manager._check_candidate_qualification("airtap")  # resets the clock
            mock_health.return_value = ("ok", {})
            manager._check_candidate_qualification("airtap")  # clock restarts here
        # Only ~0.03s of continuous "ok" has elapsed since the reset --
        # under CANDIDATE_QUALIFICATION_SECONDS (0.05s) -- not promoted.
        self.assertFalse(lkg_module.lkg_exists(em._slug("airtap")))

    def test_generation_mismatch_is_a_no_op(self):
        """Defensive case: qualification tracking keyed to a generation
        that no longer matches self._current must not crash or
        silently promote using stale tracking."""
        manager = em.EncoderManager()
        self._launch_candidate(manager)
        manager._qualify_generation["airtap"] = "some-other-generation"
        with patch("monitoring.services.probes.evaluate_encoder_group_health", return_value=("ok", {})) as mock_health:
            manager._check_candidate_qualification("airtap")
        mock_health.assert_not_called()

    def test_accepted_launch_kind_qualification_check_is_a_no_op(self):
        manager = em.EncoderManager()
        manager._start_group("airtap", [make_encoder()])
        manager._launch_kind["airtap"] = "accepted"
        with patch("monitoring.services.probes.evaluate_encoder_group_health") as mock_health:
            manager._check_candidate_qualification("airtap")
        mock_health.assert_not_called()

    def test_zero_listeners_still_qualifies(self):
        """evaluate_encoder_group_health's own "ok" status never
        depends on listener count (see monitoring/tests/
        test_probe_encoder_group.py) -- this just confirms the
        orchestration layer doesn't ALSO add a listener requirement
        of its own."""
        manager = em.EncoderManager()
        self._launch_candidate(manager)
        with patch("monitoring.services.probes.evaluate_encoder_group_health", return_value=("ok", {"destinations": [{"up": True}]})):
            for _ in range(3):
                manager._check_candidate_qualification("airtap")
                time.sleep(0.02)
            manager._check_candidate_qualification("airtap")
        self.assertTrue(lkg_module.lkg_exists(em._slug("airtap")))


# ---------------------------------------------------------------------
# Qualification deadline expiry (still-running candidate, not a crash)
# ---------------------------------------------------------------------
class QualificationDeadlineTests(CandidateFixtureMixin, TransactionTestCase):
    def test_deadline_expiry_without_ever_reaching_ok_triggers_rejection(self):
        manager = em.EncoderManager()
        with patch.object(em, "CANDIDATE_QUALIFICATION_DEADLINE_SECONDS", 0.01):
            with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
                manager._launch_group("airtap", [make_encoder()])
            time.sleep(0.05)
            with patch("monitoring.services.probes.evaluate_encoder_group_health", return_value=("warning", {"reason": "still starting"})):
                manager._check_candidate_qualification("airtap")
        # Rejected -- no LKG existed, so this becomes an ordinary
        # retry-scheduled failure, not a promotion.
        self.assertNotIn("airtap", manager._procs)

    def test_deadline_expiry_terminates_the_child_process(self):
        manager = em.EncoderManager()
        with patch.object(em, "CANDIDATE_QUALIFICATION_DEADLINE_SECONDS", 0.01):
            with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
                manager._launch_group("airtap", [make_encoder()])
            proc = self._live_procs[manager._current["airtap"]["pid"]]
            time.sleep(0.05)
            with patch("monitoring.services.probes.evaluate_encoder_group_health", return_value=("warning", {})):
                manager._check_candidate_qualification("airtap")
        proc.terminate.assert_called_once()


# ---------------------------------------------------------------------
# Rollback -- candidate crash / failure while a real LKG exists
# ---------------------------------------------------------------------
class RollbackTests(CandidateFixtureMixin, TransactionTestCase):
    def _seed_lkg(self, encoders=None):
        encoders = encoders or [make_encoder(name="lkg-encoder")]
        fp = lkg_module.compute_fingerprint("airtap", encoders)
        script = em.build_liquidsoap_script("airtap", encoders, generation="lkg-original-gen")
        lkg_module.write_lkg(em._slug("airtap"), script, {"fingerprint": fp, "accepted_at": time.time()})
        return fp, encoders

    def test_candidate_crash_triggers_rollback_to_lkg(self):
        manager = em.EncoderManager()
        self._seed_lkg()
        new_encoders = [make_encoder(name="new-candidate", host="different-host")]
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._launch_group("airtap", new_encoders)
        self.assertEqual(manager._launch_kind["airtap"], "candidate")

        self.exit_current_child(manager, "airtap", returncode=1)
        manager._handle_exit("airtap", 1)

        self.assertEqual(manager._launch_kind["airtap"], "rollback")
        self.assertIn("airtap", manager._current)  # a NEW generation is now running

    def test_rollback_launches_a_fresh_generation_not_the_old_one(self):
        manager = em.EncoderManager()
        self._seed_lkg()
        new_encoders = [make_encoder(name="new-candidate", host="different-host")]
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._launch_group("airtap", new_encoders)
        self.exit_current_child(manager, "airtap", returncode=1)
        manager._handle_exit("airtap", 1)
        self.assertNotEqual(manager._current["airtap"]["generation"], "lkg-original-gen")

    def test_rejected_candidate_fingerprint_recorded_on_crash(self):
        manager = em.EncoderManager()
        self._seed_lkg()
        new_encoders = [make_encoder(name="new-candidate", host="different-host")]
        fp = lkg_module.compute_fingerprint("airtap", new_encoders)
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._launch_group("airtap", new_encoders)
        self.exit_current_child(manager, "airtap", returncode=1)
        manager._handle_exit("airtap", 1)
        self.assertIn(fp, manager._rejected_fingerprints.get("airtap", set()))

    def test_rollback_must_qualify_before_being_reported_healthy(self):
        manager = em.EncoderManager()
        self._seed_lkg()
        new_encoders = [make_encoder(name="new-candidate", host="different-host")]
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._launch_group("airtap", new_encoders)
        self.exit_current_child(manager, "airtap", returncode=1)
        manager._handle_exit("airtap", 1)
        self.assertEqual(manager._launch_kind["airtap"], "rollback")  # not yet "accepted"

    def test_rollback_qualifies_successfully_with_sustained_health(self):
        manager = em.EncoderManager()
        self._seed_lkg()
        new_encoders = [make_encoder(name="new-candidate", host="different-host")]
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._launch_group("airtap", new_encoders)
        self.exit_current_child(manager, "airtap", returncode=1)
        manager._handle_exit("airtap", 1)

        self.qualify_ok(manager, "airtap")
        self.assertEqual(manager._launch_kind["airtap"], "accepted")

    def test_previous_lkg_remains_intact_until_candidate_actually_qualifies(self):
        """The OLD LKG (from _seed_lkg) must survive completely
        untouched while the new candidate is still on probation --
        only a SUCCESSFUL new qualification may ever supersede it."""
        manager = em.EncoderManager()
        fp, _ = self._seed_lkg()
        original_meta = lkg_module.read_lkg_meta(em._slug("airtap"))
        new_encoders = [make_encoder(name="new-candidate", host="different-host")]
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._launch_group("airtap", new_encoders)
        # Still on probation -- LKG must be exactly what it was.
        self.assertEqual(lkg_module.read_lkg_meta(em._slug("airtap")), original_meta)

    def test_double_failure_candidate_and_rollback_both_fail_stops_switching(self):
        manager = em.EncoderManager()
        self._seed_lkg()
        new_encoders = [make_encoder(name="new-candidate", host="different-host")]
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._launch_group("airtap", new_encoders)
        self.exit_current_child(manager, "airtap", returncode=1)
        manager._handle_exit("airtap", 1)  # -> rollback
        self.assertEqual(manager._launch_kind["airtap"], "rollback")

        self.exit_current_child(manager, "airtap", returncode=1)
        manager._handle_exit("airtap", 1)  # rollback ALSO crashes

        self.assertTrue(manager._critical_stopped.get("airtap"))
        self.assertEqual(manager._launch_kind["airtap"], "accepted")  # no longer tracked as an active rollback attempt

    def test_double_failure_does_not_bounce_back_to_rejected_candidate(self):
        """After both fail, a subsequent _launch_group call for the
        SAME (still-rejected) candidate configuration must launch the
        LKG again, not re-attempt the candidate."""
        manager = em.EncoderManager()
        self._seed_lkg()
        new_encoders = [make_encoder(name="new-candidate", host="different-host")]
        mock_preflight = MagicMock(return_value=preflight_module.PreflightResult(ok=True))
        with patch.object(preflight_module, "run_preflight", mock_preflight):
            manager._launch_group("airtap", new_encoders)
        self.exit_current_child(manager, "airtap", returncode=1)
        manager._handle_exit("airtap", 1)
        self.exit_current_child(manager, "airtap", returncode=1)
        manager._handle_exit("airtap", 1)
        mock_preflight.reset_mock()

        # A later retry attempt for the SAME candidate configuration:
        ok = manager._launch_group("airtap", new_encoders)
        self.assertTrue(ok)
        self.assertEqual(manager._launch_kind["airtap"], "accepted")
        mock_preflight.assert_not_called()  # never re-attempted the candidate

    def test_no_lkg_bootstrap_candidate_crash_is_handled_safely(self):
        """No LKG exists at all -- a candidate crash must fall through
        to the ordinary retry/backoff, not crash the manager or invent
        a rollback target."""
        manager = em.EncoderManager()
        new_encoders = [make_encoder()]
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._launch_group("airtap", new_encoders)
        self.exit_current_child(manager, "airtap", returncode=1)
        manager._handle_exit("airtap", 1)  # must not raise
        self.assertNotIn("airtap", manager._procs)
        self.assertIn("airtap", manager._retry_at)  # ordinary backoff was scheduled

    def test_desired_config_remains_visible_in_db_not_silently_mutated(self):
        """Rollback must never write back to the Encoder DB rows --
        the rejected "desired" configuration stays exactly as the
        operator saved it, for diagnosis/correction, while the
        accepted LKG runs operationally."""
        manager = em.EncoderManager()
        self._seed_lkg()
        bad_encoder = Encoder.objects.create(
            name="db-row", protocol="shoutcast2", host="bad-host-should-remain", port=8000,
            mount="/9", password="secret", format="mp3", bitrate_kbps=192, station_name="s",
        )
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._launch_group("airtap", [bad_encoder])
        self.exit_current_child(manager, "airtap", returncode=1)
        manager._handle_exit("airtap", 1)

        bad_encoder.refresh_from_db()
        self.assertEqual(bad_encoder.host, "bad-host-should-remain")  # untouched


# ---------------------------------------------------------------------
# Phase 2 review fix #2 -- a real, live bug found on review: an earlier
# draft left self._candidate_encoders holding the REJECTED CANDIDATE's
# rows all the way through rollback, so _check_candidate_qualification
# asked evaluate_encoder_group_health "is the candidate's SID up?"
# instead of "is the RESTORED LKG's SID up?" during rollback
# qualification. Fixed via self._qualify_expected, populated from the
# LKG's own frozen metadata (_lkg_destinations_to_expected) rather than
# re-derived from the (possibly still-bad) live DB. These tests exist
# specifically to catch a regression of that exact bug -- the existing
# RollbackTests above all mock evaluate_encoder_group_health with an
# unconditional "ok" and never inspected what encoders it was actually
# asked about, so none of them would have caught this.
# ---------------------------------------------------------------------
class RollbackQualificationExpectedEncodersTests(CandidateFixtureMixin, TransactionTestCase):
    def _promote_real_lkg(self, manager, encoders, input_device="airtap"):
        """Launches + qualifies a real candidate so _promote_candidate
        writes genuine, correctly-shaped LKG metadata (including
        "destinations") -- rather than hand-constructing a meta dict
        that could drift from the real schema."""
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._launch_group(input_device, encoders)
        self.qualify_ok(manager, input_device)
        self.assertTrue(lkg_module.lkg_exists(em._slug(input_device)))

    def _write_healthy_audio_state(self, manager, input_device="airtap"):
        """Flips the audio-state file the rollback's own _start_group
        call already wrote (still "starting") to a stabilized "audio_
        ok", keeping the SAME generation/pid -- exactly the shape a
        real Liquidsoap child would eventually publish, and what
        evaluate_encoder_group_health's generation/pid cross-check
        requires to trust it."""
        current = manager._current[input_device]
        slug = em._slug(input_device)
        now = time.time()
        em._atomic_write_json(em._audio_state_path_for_slug(slug), {
            "status": "audio_ok", "is_blank": False, "audio_observed": True,
            "input_device": input_device, "pid": current["pid"], "generation": current["generation"],
            "started_at": now - 60, "since": now - 60, "timestamp": now,
        })

    def test_wiring_rollback_expected_encoders_reflect_lkg_not_candidate(self):
        """Focused check on exactly what changed: after a candidate
        crash triggers rollback, self._qualify_expected must hold the
        LKG's OWN SID -- never the rejected candidate's."""
        manager = em.EncoderManager()
        self._promote_real_lkg(manager, [make_encoder(name="lkg-enc", mount="/1")])

        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._launch_group("airtap", [make_encoder(name="bad-candidate", mount="/5")])
        self.assertEqual(manager._launch_kind["airtap"], "candidate")

        self.exit_current_child(manager, "airtap", returncode=1)
        manager._handle_exit("airtap", 1)  # -> rollback
        self.assertEqual(manager._launch_kind["airtap"], "rollback")

        expected = manager._qualify_expected.get("airtap")
        self.assertIsNotNone(expected)
        self.assertEqual([e.shoutcast_sid for e in expected], ["1"])  # the LKG's SID, never "5"

    def test_real_qualification_rollback_checks_lkg_sid_not_candidate_sid(self):
        """The literal scenario from review: LKG=SID1, DB desired
        candidate=SID5, candidate fails, rollback launches SID1. Only
        fetch_shoutcast_stats and probe_systemd are mocked -- everything
        else (real evaluate_encoder_group_health, real group-state/
        audio-state freshness and generation/pid cross-checking, real
        stabilization gate) runs for real. The mocked Shoutcast server
        reports SID 1 up AND SID 5 down (as it well might -- e.g. the
        candidate's SID was simply never actually connected).
        EXPECTED: rollback qualifies successfully anyway, because it
        was never asking about SID 5 in the first place."""
        manager = em.EncoderManager()
        self._promote_real_lkg(manager, [make_encoder(name="lkg-enc", host="10.0.0.5", port=8000, mount="/1")])

        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._launch_group("airtap", [make_encoder(name="bad-candidate", host="10.0.0.5", port=8000, mount="/5")])
        self.exit_current_child(manager, "airtap", returncode=1)
        manager._handle_exit("airtap", 1)  # -> rollback, relaunches the LKG (SID 1) for real (Popen mocked)
        self.assertEqual(manager._launch_kind["airtap"], "rollback")

        self._write_healthy_audio_state(manager)
        shoutcast_stats = {"1": {"up": True, "listeners": 4}, "5": {"up": False, "listeners": 0}}

        with patch.object(probes_module, "probe_systemd", return_value=("ok", {})), \
             patch("monitoring.services.shoutcast.fetch_shoutcast_stats", return_value=shoutcast_stats):
            for _ in range(3):
                manager._check_candidate_qualification("airtap")
                time.sleep(0.02)
            manager._check_candidate_qualification("airtap")

        self.assertEqual(manager._launch_kind["airtap"], "accepted")  # rollback succeeded

    def test_real_qualification_rollback_checks_lkg_host_port_not_candidate_host_port(self):
        """Same scenario, but the changed field is host+port (e.g. a
        server migration) rather than just the SID -- the LKG's OWN
        server must be the one polled. The candidate's (abandoned)
        server is wired to look UNREACHABLE ({} -- fetch_shoutcast_
        stats' own real "couldn't reach it" return shape) if it's ever
        consulted, which would make the aggregate anything but "ok" --
        so successful qualification here is only possible if the
        candidate's host/port was never queried at all."""
        manager = em.EncoderManager()
        self._promote_real_lkg(manager, [make_encoder(name="lkg-enc", host="10.0.0.5", port=8000, mount="/1")])

        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._launch_group("airtap", [make_encoder(name="bad-candidate", host="10.0.0.99", port=9000, mount="/1")])
        self.exit_current_child(manager, "airtap", returncode=1)
        manager._handle_exit("airtap", 1)
        self.assertEqual(manager._launch_kind["airtap"], "rollback")

        self._write_healthy_audio_state(manager)

        def fake_fetch(host, port, timeout=5):
            if (host, port) == ("10.0.0.5", 8000):
                return {"1": {"up": True, "listeners": 2}}
            return {}  # the candidate's abandoned server -- must never be consulted

        with patch.object(probes_module, "probe_systemd", return_value=("ok", {})), \
             patch("monitoring.services.shoutcast.fetch_shoutcast_stats", side_effect=fake_fetch):
            for _ in range(3):
                manager._check_candidate_qualification("airtap")
                time.sleep(0.02)
            manager._check_candidate_qualification("airtap")

        self.assertEqual(manager._launch_kind["airtap"], "accepted")

    def test_promoted_lkg_metadata_includes_destinations(self):
        """Regression guard on the metadata shape itself -- if a future
        change to _promote_candidate ever drops the "destinations"
        field, _lkg_destinations_to_expected silently degrades to an
        empty list (by design, fail-closed) rather than raising, so
        this needs its own explicit check."""
        manager = em.EncoderManager()
        self._promote_real_lkg(manager, [make_encoder(name="lkg-enc", host="10.0.0.5", port=8000, mount="/1")])
        meta = lkg_module.read_lkg_meta(em._slug("airtap"))
        self.assertIn("destinations", meta)
        self.assertEqual(len(meta["destinations"]), 1)
        dest = meta["destinations"][0]
        self.assertEqual(dest["host"], "10.0.0.5")
        self.assertEqual(dest["port"], 8000)
        self.assertEqual(dest["shoutcast_sid"], "1")
        self.assertEqual(dest["name"], "lkg-enc")
        # Phase 3M: protocol/mount are needed by normalized_destination_key
        # for cross-group collision checks -- must round-trip too.
        self.assertEqual(dest["protocol"], "shoutcast2")
        self.assertEqual(dest["mount"], "/1")

    def test_legacy_lkg_metadata_without_destinations_fails_closed_not_wrong(self):
        """An LKG promoted before this fix existed (or with corrupt
        metadata) has no "destinations" -- rollback must never fall
        back to checking the candidate's SIDs in that case either;
        _lkg_destinations_to_expected returns [], which makes the REAL
        (not mocked) evaluate_encoder_group_health report "unknown"
        (its own explicit first-line behavior for an empty encoders
        list -- never a false "ok") until the qualification deadline
        expires, at which point it's treated as an ordinary rollback
        failure -- conservative by construction, not a crash and not a
        silent wrong-SID check."""
        manager = em.EncoderManager()
        script = em.build_liquidsoap_script("airtap", [make_encoder(name="lkg-enc", mount="/1")], generation="legacy-gen")
        lkg_module.write_lkg(em._slug("airtap"), script, {"fingerprint": "legacy-fp"})  # no "destinations" key

        with patch.object(em, "CANDIDATE_QUALIFICATION_DEADLINE_SECONDS", 0.03):
            with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
                manager._launch_group("airtap", [make_encoder(name="bad-candidate", mount="/5")])
            self.exit_current_child(manager, "airtap", returncode=1)
            manager._handle_exit("airtap", 1)  # -> rollback
            self.assertEqual(manager._qualify_expected.get("airtap"), [])

            time.sleep(0.05)
            manager._check_candidate_qualification("airtap")  # real evaluate_encoder_group_health, not mocked

        # Deadline-expired rollback qualification failure -> ordinary
        # rollback-failed handling: critical-stopped, never silently
        # promoted/accepted.
        self.assertNotEqual(manager._launch_kind.get("airtap"), "candidate")
        self.assertNotEqual(manager._launch_kind.get("airtap"), "rollback")
        self.assertTrue(manager._critical_stopped.get("airtap"))


# ---------------------------------------------------------------------
# Real end-to-end: real build_liquidsoap_script + real liquidsoap
# --check, mocked Popen only. Skipped cleanly if liquidsoap isn't
# installed.
# ---------------------------------------------------------------------
class RealPreflightIntegrationTests(TransactionTestCase):
    """Exercises the same validate -> render -> write_candidate ->
    run_preflight -> cleanup_candidate sequence _launch_group performs,
    using the REAL installed liquidsoap binary -- calling those steps
    directly rather than through _launch_group/EncoderManager. Reason:
    EncoderManagerFixtureMixin's Popen mock (used throughout the rest
    of this file, necessary so nothing there ever spawns a real
    Liquidsoap ENCODER process) patches the module-global
    `subprocess.Popen` symbol -- which `subprocess.run` uses internally
    too, so it would also intercept a real `liquidsoap --check`
    invocation and fail confusingly. Deliberately does NOT inherit
    that fixture; only the candidate/LKG directories are patched here.
    The manager-level orchestration around this exact sequence
    (decision logic, event emission, state transitions) is covered
    with a mocked preflight elsewhere in this file -- this class's own
    job is narrower: prove the real Liquidsoap binary genuinely
    accepts what build_liquidsoap_script produces. Skipped cleanly if
    liquidsoap isn't installed."""

    def setUp(self):
        super().setUp()
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        base = Path(tmpdir.name)
        for patcher in (
            patch.object(lkg_module, "CANDIDATE_DIR", base / "candidate"),
            patch.object(lkg_module, "LKG_DIR", base / "lkg"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_real_valid_candidate_passes_preflight(self):
        if shutil.which("liquidsoap") is None:
            self.skipTest("liquidsoap not installed on this box")
        encoders = [make_encoder(format="mp3")]
        errors = validation.validate_full_configuration(encoders)
        self.assertEqual(errors, [])
        script = em.build_liquidsoap_script("airtap", encoders, generation="real-check")
        path = lkg_module.write_candidate(em._slug("airtap"), script)
        try:
            result = preflight_module.run_preflight(path, encoders)
        finally:
            lkg_module.cleanup_candidate(path)
        self.assertTrue(result.ok, result.reason)

    def test_real_invalid_protocol_format_rejected_before_ever_reaching_liquidsoap(self):
        """A genuinely invalid protocol/format combination is caught by
        validate_full_configuration -- confirms _launch_group's own
        ordering (validate first) means liquidsoap is never even
        invoked for a statically-invalid candidate."""
        bad = make_encoder(protocol="shoutcast1", format="vorbis")
        errors = validation.validate_full_configuration([bad])
        self.assertTrue(errors)


# ---------------------------------------------------------------------
# Phase 2 review-fix pass 2, Issue 3: a filesystem failure anywhere in
# the candidate/LKG persistence path must never escape and crash
# EncoderManager -- confirmed as a real, live risk during review: the
# supervisor's own main loop (EncoderManager.start()'s `while self.
# running: ...`) and run_encoders.py's handle() both have NOTHING
# above them catching an escaping exception, so an unhandled OSError
# from e.g. _promote_candidate's write_lkg() call would take down the
# entire process -- orphaning whatever Liquidsoap children were
# healthy and running at the time (never cleanly stopped, since
# self.stop() sits after the crashed while loop and never runs).
# ---------------------------------------------------------------------
class FilesystemFailureContainmentTests(CandidateFixtureMixin, TransactionTestCase):
    def test_promotion_persist_failure_leaves_candidate_running_not_accepted(self):
        """The core Issue 3 scenario: candidate is proven-healthy and
        running, but write_lkg() fails. The candidate must NOT be
        killed, must NOT be marked accepted, and the manager must not
        crash."""
        manager = em.EncoderManager()
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._launch_group("airtap", [make_encoder(name="healthy-candidate")])
        self.assertEqual(manager._launch_kind["airtap"], "candidate")
        pid_before = manager._current["airtap"]["pid"]

        with patch.object(lkg_module, "write_lkg", side_effect=OSError(28, "No space left on device")):
            self.qualify_ok(manager, "airtap")  # must not raise

        # Candidate is untouched: still running, same pid, not promoted.
        self.assertEqual(manager._launch_kind["airtap"], "candidate")
        self.assertEqual(manager._current["airtap"]["pid"], pid_before)
        self.assertFalse(lkg_module.lkg_exists(em._slug("airtap")))

    def test_promotion_persist_failure_does_not_touch_old_lkg(self):
        manager = em.EncoderManager()
        old_script = em.build_liquidsoap_script("airtap", [make_encoder(name="old-lkg")], generation="old-gen")
        lkg_module.write_lkg(em._slug("airtap"), old_script, {"fingerprint": "old-fp"})

        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._launch_group("airtap", [make_encoder(name="new-candidate", mount="/9")])

        with patch.object(lkg_module, "write_lkg", side_effect=OSError("simulated disk failure")):
            self.qualify_ok(manager, "airtap")

        script, meta = lkg_module.read_lkg(em._slug("airtap"))
        self.assertEqual(meta["fingerprint"], "old-fp")  # completely unchanged

    def test_promotion_retries_automatically_on_next_tick_after_transient_failure(self):
        """Qualification tracking is deliberately NOT cleared on a
        persist failure -- the very next health tick, seeing the same
        already-sustained health, retries the promotion with no new
        state machinery. Once the transient failure clears, promotion
        succeeds without a fresh candidate launch."""
        manager = em.EncoderManager()
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._launch_group("airtap", [make_encoder(name="healthy-candidate")])

        with patch.object(lkg_module, "write_lkg", side_effect=OSError("simulated transient failure")):
            self.qualify_ok(manager, "airtap")
        self.assertFalse(lkg_module.lkg_exists(em._slug("airtap")))
        self.assertEqual(manager._launch_kind["airtap"], "candidate")  # still armed

        # Failure clears -- next qualification tick (real write_lkg, no
        # patch) succeeds without relaunching anything.
        pid_before = manager._current["airtap"]["pid"]
        with patch("monitoring.services.probes.evaluate_encoder_group_health", return_value=("ok", {})):
            manager._check_candidate_qualification("airtap")
        self.assertTrue(lkg_module.lkg_exists(em._slug("airtap")))
        self.assertEqual(manager._launch_kind["airtap"], "accepted")
        self.assertEqual(manager._current["airtap"]["pid"], pid_before)  # never relaunched

    def test_promotion_persist_failure_emits_critical_event_not_silence(self):
        manager = em.EncoderManager()
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            manager._launch_group("airtap", [make_encoder(name="healthy-candidate")])

        with patch.object(lkg_module, "write_lkg", side_effect=OSError("simulated disk failure")):
            with patch("encoders.services.encoder_manager.emit_event") as mock_emit:
                self.qualify_ok(manager, "airtap")

        titles = [call.kwargs.get("title", "") for call in mock_emit.call_args_list]
        self.assertTrue(any("could not persist" in t for t in titles))

    def test_launch_group_read_lkg_failure_degrades_to_bootstrap_not_crash(self):
        manager = em.EncoderManager()
        with patch.object(lkg_module, "read_lkg", side_effect=OSError("simulated read failure")):
            with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
                ok = manager._launch_group("airtap", [make_encoder(name="new-config")])
        self.assertTrue(ok)  # degrades to the full candidate pipeline, still launches
        self.assertEqual(manager._launch_kind["airtap"], "candidate")

    def test_launch_group_write_candidate_failure_falls_back_to_lkg(self):
        manager = em.EncoderManager()
        old_script = em.build_liquidsoap_script("airtap", [make_encoder(name="old-lkg")], generation="old-gen")
        lkg_module.write_lkg(em._slug("airtap"), old_script, {"fingerprint": "old-fp"})

        with patch.object(lkg_module, "write_candidate", side_effect=OSError("simulated disk failure")):
            ok = manager._launch_group("airtap", [make_encoder(name="new-candidate", mount="/9")])

        self.assertTrue(ok)  # still launches SOMETHING -- the LKG
        self.assertEqual(manager._launch_kind["airtap"], "accepted")

    def test_launch_group_write_candidate_failure_no_lkg_returns_false_not_crash(self):
        manager = em.EncoderManager()
        with patch.object(lkg_module, "write_candidate", side_effect=OSError("simulated disk failure")):
            ok = manager._launch_group("airtap", [make_encoder(name="new-config")])
        self.assertFalse(ok)  # nothing safe to launch -- caller's own retry/backoff continues

    def test_admin_style_candidate_write_failure_never_crashes_pipeline(self):
        """Mirrors what encoders/admin.py's own _run_predispatch_
        preflight must do with the same failure -- covered directly
        there in test_admin.py; this confirms the manager's OWN
        candidate pipeline degrades the identical way for the same
        underlying failure."""
        manager = em.EncoderManager()
        with patch.object(lkg_module, "write_candidate", side_effect=PermissionError("simulated EACCES")):
            ok = manager._launch_group("airtap", [make_encoder(name="new-config")])
        self.assertFalse(ok)
        fp = lkg_module.compute_fingerprint("airtap", [make_encoder(name="new-config")])
        self.assertIn(fp, manager._rejected_fingerprints.get("airtap", set()))

    def test_start_group_script_write_failure_returns_false_not_raise(self):
        manager = em.EncoderManager()
        with patch.object(em, "SCRIPT_DIR", Path("/nonexistent-directory-for-test/sub")):
            ok = manager._start_group("airtap", [make_encoder()])
        self.assertFalse(ok)
        self.assertNotIn("airtap", manager._current)

    def test_check_health_one_groups_exception_does_not_block_another_groups_heartbeat(self):
        """The outer _guarded defense-in-depth backstop: an unexpected
        exception raised while processing ONE group's qualification
        check must not prevent a DIFFERENT, healthy group's state
        heartbeat from still being written in the SAME _check_health()
        tick.

        Persisted (not just in-memory) Encoder rows -- Phase 3's own
        _reconcile() now runs first in _check_health() and queries the
        DB for real; an unsaved row would make it correctly (per
        Phase 3C) treat these as "removed" and stop them before the
        qualification-check loop this test cares about ever runs."""
        manager = em.EncoderManager()
        encoder_a = make_encoder(name="a")
        encoder_a.save()
        encoder_b = make_encoder(name="b", input_device="plughw:3,1")
        encoder_b.save()
        manager._start_group("airtap", [encoder_a])
        manager._start_group("plughw:3,1", [encoder_b])
        manager._launch_kind["airtap"] = "candidate"  # so _check_candidate_qualification does real work for it
        manager._qualify_generation["airtap"] = manager._current["airtap"]["generation"]
        manager._qualify_expected["airtap"] = [encoder_a]
        manager._qualify_deadline["airtap"] = None

        with patch("monitoring.services.probes.evaluate_encoder_group_health", side_effect=RuntimeError("boom")):
            manager._check_health()  # must not raise

        # The OTHER group's heartbeat still ran this same tick.
        other_state = self.read_group_state("plughw_3_1")
        self.assertIsNotNone(other_state["pid"])

    def test_check_health_guarded_exception_emits_event_not_silence(self):
        manager = em.EncoderManager()
        encoder_a = make_encoder(name="a")
        encoder_a.save()  # see comment above -- Phase 3's _reconcile() reads the real DB
        manager._start_group("airtap", [encoder_a])
        manager._launch_kind["airtap"] = "candidate"
        manager._qualify_generation["airtap"] = manager._current["airtap"]["generation"]
        manager._qualify_expected["airtap"] = [encoder_a]
        manager._qualify_deadline["airtap"] = None

        with patch("monitoring.services.probes.evaluate_encoder_group_health", side_effect=RuntimeError("boom")):
            with patch("encoders.services.encoder_manager.emit_event") as mock_emit:
                manager._check_health()

        titles = [call.kwargs.get("title", "") for call in mock_emit.call_args_list]
        self.assertTrue(any("unexpected error" in t for t in titles))
