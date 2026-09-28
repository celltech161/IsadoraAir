"""Reviewed migration approval -- wire-level defense in depth.

_strict_probe is the boundary between an untrusted subprocess's stdout
and the executor's trust decisions. These tests prove a malformed or
inconsistent payload -- including one an attacker controlling only the
probe's OUTPUT (not its DB queries) might try to forge -- is rejected
before the executor ever acts on it. Also proves job_service's Django-
side reconciliation copies migration_plan_review defensively.
"""
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase

from . import phase_b_helpers  # noqa: F401 -- sys.path side effect, see that module's docstring
from isadoraair_updater.executor import ExecutionError, Executor, _strict_probe
from isadoraair_updater.process import ProcessResult
from updatecenter.job_service import _reconcile_response
from updatecenter.models import UpdateJob, UpdateJobState


def valid_payload(**overrides):
    payload = {
        "schema_version": 1, "status": "ok",
        "plan": [{
            "ref": "sample.0001_initial", "dependencies": [],
            "migration_file_sha256": "0" * 64,
            "operations": [{"operation": "CreateModel", "classification": "additive", "detail": "new table/model"}],
        }],
        "nodes": {"sample.0001_initial": []},
        "applied": [], "conflicts": {}, "replacements": [],
        "release_id": None, "target_commit": None, "manifest_sha256": None,
        "migration_plan_digest": None, "manual_operations": [], "approval": None,
    }
    payload.update(overrides)
    return payload


def encode(payload):
    import json
    return json.dumps(payload).encode("utf-8")


class StrictProbeMigrationApprovalShapeTests(SimpleTestCase):
    def test_valid_all_additive_payload_is_accepted(self):
        result = _strict_probe(encode(valid_payload()), review_context=True)
        self.assertEqual(result["manual_operations"], [])

    def test_missing_new_keys_are_rejected(self):
        payload = valid_payload()
        del payload["manual_operations"]
        with self.assertRaisesRegex(ExecutionError, "schema/status mismatch"):
            _strict_probe(encode(payload), review_context=True)

    def test_manual_operations_present_without_digest_is_rejected(self):
        """A probe claiming manual operations exist but no digest is an
        internally inconsistent -- and therefore untrusted -- payload."""
        payload = valid_payload(manual_operations=[{
            "ref": "sample.0001_initial", "operation_index": 0,
            "operation": "AddField", "classification": "manual", "detail": "x",
        }])
        with self.assertRaisesRegex(ExecutionError, "no digest"):
            _strict_probe(encode(payload), review_context=True)

    def test_approval_present_without_any_manual_operations_is_rejected(self):
        """A forged "approval found" claim attached to a payload with
        nothing to approve is exactly the kind of manufactured-approval
        shape this must fail closed on."""
        payload = valid_payload(
            migration_plan_digest="a" * 64,
            approval={"found": True, "id": "x", "approved_by": "y", "approved_at": "z"},
        )
        with self.assertRaisesRegex(ExecutionError, "approval with no manual operations"):
            _strict_probe(encode(payload), review_context=True)

    def test_malformed_digest_is_rejected(self):
        payload = valid_payload(migration_plan_digest="not-hex")
        with self.assertRaisesRegex(ExecutionError, "not a valid digest"):
            _strict_probe(encode(payload), review_context=True)

    def test_manual_operations_referencing_an_unknown_migration_ref_rejected(self):
        payload = valid_payload(
            migration_plan_digest="a" * 64,
            manual_operations=[{
                "ref": "nosuchapp.0001_initial", "operation_index": 0,
                "operation": "AddField", "classification": "manual", "detail": "x",
            }],
        )
        with self.assertRaisesRegex(ExecutionError, "manual_operations shape"):
            _strict_probe(encode(payload), review_context=True)

    def test_approval_shape_must_be_exactly_found_false_or_found_true_with_fields(self):
        base = valid_payload(
            migration_plan_digest="a" * 64,
            manual_operations=[{
                "ref": "sample.0001_initial", "operation_index": 0,
                "operation": "AddField", "classification": "manual", "detail": "x",
            }],
        )
        for bad_approval in (
            {"found": True},  # missing id/approved_by/approved_at
            {"found": True, "id": 5, "approved_by": "y", "approved_at": "z"},  # wrong type
            {"maybe": "found"},  # unknown shape entirely
            "found",  # not even a dict
        ):
            with self.subTest(bad_approval=bad_approval):
                payload = dict(base, approval=bad_approval)
                with self.assertRaises(ExecutionError):
                    _strict_probe(encode(payload), review_context=True)

    def test_release_id_and_target_commit_must_be_string_or_none(self):
        payload = valid_payload(release_id=12345)
        with self.assertRaisesRegex(ExecutionError, "invalid type"):
            _strict_probe(encode(payload), review_context=True)

    def test_target_probe_returning_only_the_legacy_shape_is_rejected(self):
        """A target-schema probe (review_context=True) must never accept
        the bare legacy 7-key shape -- generation-6/7 compatibility for the
        CURRENT side must never weaken the TARGET side's strictness."""
        legacy = legacy_valid_payload()
        with self.assertRaisesRegex(ExecutionError, "schema/status mismatch"):
            _strict_probe(encode(legacy), review_context=True)


def legacy_valid_payload(**overrides):
    """The exact legacy 7-key shape a pre-693ea94 updatecenter_probe.py
    emits -- the honest current-schema shape generation 7 must accept.
    See test_gen7_heterogeneous_probe_compatibility.py for the real,
    subprocess-derived proof of this same shape from the actual r0088
    script; this hand-built version exists only for fast, isolated
    coverage of the individual rejection paths below."""
    payload = {
        "schema_version": 1, "status": "ok",
        "plan": [], "nodes": {}, "applied": [], "conflicts": {}, "replacements": [],
    }
    payload.update(overrides)
    return payload


class LegacyCurrentProbeShapeTests(SimpleTestCase):
    """review_context=False (the current-schema probe) coverage -- proves
    the compatibility fix is context-sensitive, not permissive."""

    def test_valid_legacy_payload_is_accepted(self):
        result = _strict_probe(encode(legacy_valid_payload()), review_context=False)
        self.assertEqual(result["status"], "ok")

    def test_legacy_payload_missing_a_required_key_is_rejected(self):
        payload = legacy_valid_payload()
        del payload["conflicts"]
        with self.assertRaisesRegex(ExecutionError, "schema/status mismatch"):
            _strict_probe(encode(payload), review_context=False)

    def test_legacy_payload_with_wrong_status_is_rejected(self):
        payload = legacy_valid_payload(status="pending")
        with self.assertRaisesRegex(ExecutionError, "schema/status mismatch"):
            _strict_probe(encode(payload), review_context=False)

    def test_legacy_payload_with_wrong_schema_version_is_rejected(self):
        payload = legacy_valid_payload(schema_version=2)
        with self.assertRaisesRegex(ExecutionError, "schema/status mismatch"):
            _strict_probe(encode(payload), review_context=False)

    def test_legacy_payload_with_extraneous_unknown_key_is_rejected(self):
        payload = legacy_valid_payload(extra_unexpected_field="x")
        with self.assertRaisesRegex(ExecutionError, "schema/status mismatch"):
            _strict_probe(encode(payload), review_context=False)

    def test_already_upgraded_current_probe_with_honest_null_defaults_is_accepted(self):
        """A current-schema probe run against an ALREADY-upgraded
        application source (which unconditionally emits all 13 keys) must
        still be accepted, provided the six review fields hold their
        documented null/empty defaults -- proving this never regresses
        once a station's application source eventually catches up."""
        payload = legacy_valid_payload(
            release_id=None, target_commit=None, manifest_sha256=None,
            migration_plan_digest=None, manual_operations=[], approval=None,
        )
        result = _strict_probe(encode(payload), review_context=False)
        self.assertEqual(result["manual_operations"], [])

    def test_already_upgraded_current_probe_reporting_real_review_data_is_rejected(self):
        payload = legacy_valid_payload(
            release_id="r0089", target_commit="a" * 40, manifest_sha256="b" * 64,
            migration_plan_digest="c" * 64, manual_operations=[], approval=None,
        )
        with self.assertRaisesRegex(ExecutionError, "review evidence without review context"):
            _strict_probe(encode(payload), review_context=False)


class ProbeEchoConsistencyTests(SimpleTestCase):
    """Executor._probe() (not _strict_probe alone) must reject a target
    probe that echoes a DIFFERENT release_id/target_commit than the one
    it was actually asked for -- a forged/stale echo is exactly as
    dangerous as a forged digest or approval."""

    def _probe_returning(self, payload):
        executor = object.__new__(Executor)
        raw = encode(payload)
        result = ProcessResult(("manage.py", "updatecenter_probe"), 0, raw, b"")
        executor._run_app = mock.Mock(return_value=(result, {}))
        return executor

    def test_wrong_release_id_echo_is_rejected(self):
        payload = valid_payload(release_id="r0999", target_commit="a" * 40)
        executor = self._probe_returning(payload)
        with self.assertRaisesRegex(ExecutionError, "echoed a different"):
            executor._probe(Path("."), release_id="r0089", target_commit="a" * 40)

    def test_wrong_target_commit_echo_is_rejected(self):
        payload = valid_payload(release_id="r0089", target_commit="b" * 40)
        executor = self._probe_returning(payload)
        with self.assertRaisesRegex(ExecutionError, "echoed a different"):
            executor._probe(Path("."), release_id="r0089", target_commit="a" * 40)

    def test_matching_echo_is_accepted(self):
        payload = valid_payload(release_id="r0089", target_commit="a" * 40)
        executor = self._probe_returning(payload)
        result = executor._probe(Path("."), release_id="r0089", target_commit="a" * 40)
        self.assertEqual(result["release_id"], "r0089")


class ReconcileMigrationPlanReviewTests(TestCase):
    class _FakeBackend:
        def get_job_log(self, job_id, max_bytes=65536):
            return ""

    def _job(self):
        return UpdateJob.objects.create(
            installed_release_id="r0088", target_release_id="r0089",
            installed_commit="1" * 40, target_commit="2" * 40,
            state=UpdateJobState.RUNNING, active_lock=1,
        )

    def test_dict_review_is_copied_through(self):
        job = self._job()
        review = {"release_id": "r0089", "migration_plan_digest": "a" * 64, "manual_operations": []}
        response = {"job": {
            "job_id": str(job.id), "state": "manual_intervention_required",
            "current_step": "failed", "failure_classification": "MIGRATION_OPERATION_MANUAL",
            "failure_detail": "x", "trusted_plan": None, "migration_plan_review": review,
        }}
        _reconcile_response(job, response, backend=self._FakeBackend())
        job.refresh_from_db()
        self.assertEqual(job.migration_plan_review, review)

    def test_missing_review_key_defaults_to_none_not_a_crash(self):
        """Reconciling against an OLDER root worker's response (no
        migration_plan_review key at all) must not raise."""
        job = self._job()
        response = {"job": {
            "job_id": str(job.id), "state": "succeeded",
            "current_step": "done", "failure_classification": "", "failure_detail": "",
            "trusted_plan": None,
        }}
        _reconcile_response(job, response, backend=self._FakeBackend())
        job.refresh_from_db()
        self.assertIsNone(job.migration_plan_review)

    def test_non_dict_review_is_rejected_not_stored(self):
        job = self._job()
        response = {"job": {
            "job_id": str(job.id), "state": "failed",
            "current_step": "failed", "failure_classification": "X", "failure_detail": "y",
            "trusted_plan": None, "migration_plan_review": "not-a-dict",
        }}
        _reconcile_response(job, response, backend=self._FakeBackend())
        job.refresh_from_db()
        self.assertIsNone(job.migration_plan_review)
