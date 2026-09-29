import json
import stat
from pathlib import Path
import tempfile
import uuid

from django.test import SimpleTestCase

from .phase_b_helpers import config_dict
from isadoraair_updater.approvals import ApprovalError, ApprovalStore, approval_identity
from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.jobs import JobStore


DIGEST = "d" * 64
FINGERPRINT = "f" * 64


def review():
    return {
        "release_id": "r0092", "target_commit": "b" * 40,
        "manifest_sha256": "c" * 64, "migration_plan_digest": DIGEST,
        "trusted_plan_fingerprint": FINGERPRINT,
        "manual_operations": [{
            "ref": "sample.0001_initial", "operation_index": 0,
            "operation": "RunPython", "classification": "manual",
            "detail": "operation is outside the automatic allowlist",
        }],
    }


def trusted_plan():
    return {
        "target_release_id": "r0092", "target_commit": "b" * 40,
        "fingerprint": FINGERPRINT,
        "releases_in_plan": ["r0089", "r0090", "r0091", "r0092"],
        "migrations_required": ["sample.0001_initial"],
    }


class ProtectedApprovalStoreTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = validate_config_dict(
            config_dict(self.root, str(self.root / "upstream.git")),
            allow_local_repository=True,
        )
        self.jobs = JobStore(self.config.jobs_root, self.config.logs_root, acquire_daemon_lock=False)
        self.approvals = ApprovalStore(self.config.approvals_root, self.jobs)
        self.job_id = str(uuid.uuid4())

    def tearDown(self):
        self.jobs.close()
        self.temp.cleanup()

    def make_terminal_job(self):
        self.jobs.accept(self.job_id, "r0092", FINGERPRINT)
        self.jobs.update(self.job_id, trusted_plan=trusted_plan())
        self.jobs.fail(
            self.job_id, "MIGRATION_OPERATION_MANUAL", "manual operation",
            manual=True, migration_plan_review=review(),
        )

    def create(self):
        return self.approvals.create_from_job(
            self.job_id, confirmed_migration_plan_digest=DIGEST,
            approved_by_username="operator", reason="Reviewed against production schema.",
        )

    def test_approval_is_derived_from_terminal_root_job_and_exactly_matched(self):
        self.make_terminal_job()
        record, created = self.create()
        self.assertTrue(created)
        identity = approval_identity(
            target_release_id="r0092", target_commit="b" * 40,
            target_manifest_sha256="c" * 64,
            migration_plan_digest=DIGEST,
            trusted_plan_fingerprint=FINGERPRINT,
        )
        self.assertEqual(self.approvals.find(identity), record)
        self.assertEqual(stat.S_IMODE(self.config.approvals_root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.approvals._path(identity).stat().st_mode), 0o600)
        for field, changed in (
            ("target_release_id", "r0093"),
            ("target_commit", "e" * 40),
            ("target_manifest_sha256", "e" * 64),
            ("migration_plan_digest", "e" * 64),
            ("trusted_plan_fingerprint", "e" * 64),
        ):
            candidate = dict(identity)
            candidate[field] = changed
            self.assertIsNone(self.approvals.find(candidate), field)

    def test_nonexistent_nonterminal_wrong_classification_and_wrong_digest_rejected(self):
        with self.assertRaises(ApprovalError):
            self.create()
        self.jobs.accept(self.job_id, "r0092", FINGERPRINT)
        with self.assertRaises(ApprovalError):
            self.create()
        self.jobs.update(self.job_id, trusted_plan=trusted_plan())
        self.jobs.fail(self.job_id, "OTHER", "no", manual=True, migration_plan_review=review())
        with self.assertRaises(ApprovalError):
            self.create()
        # A fresh, correctly terminal Job A still requires an exact explicit digest.
        self.job_id = str(uuid.uuid4())
        self.make_terminal_job()
        with self.assertRaises(ApprovalError):
            self.approvals.create_from_job(
                self.job_id, confirmed_migration_plan_digest="e" * 64,
                approved_by_username="operator", reason="reviewed",
            )

    def test_client_cannot_supply_identity_and_malformed_review_is_rejected(self):
        self.jobs.accept(self.job_id, "r0092", FINGERPRINT)
        self.jobs.update(self.job_id, trusted_plan=trusted_plan())
        malformed = review()
        malformed.pop("target_commit")
        self.jobs.fail(
            self.job_id, "MIGRATION_OPERATION_MANUAL", "manual",
            manual=True, migration_plan_review=malformed,
        )
        with self.assertRaises(ApprovalError):
            self.create()

    def test_duplicate_is_idempotent_and_preserves_first_audit_record(self):
        self.make_terminal_job()
        first, created = self.create()
        second, created_again = self.approvals.create_from_job(
            self.job_id, confirmed_migration_plan_digest=DIGEST,
            approved_by_username="other", reason="Different later text.",
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first, second)
        self.assertEqual(second["approved_by_username"], "operator")

    def test_approval_survives_discovery_job_retention(self):
        self.make_terminal_job()
        record, _created = self.create()
        (self.config.jobs_root / f"{self.job_id}.json").unlink()
        identity = {field: record[field] for field in (
            "target_release_id", "target_commit", "target_manifest_sha256",
            "migration_plan_digest", "trusted_plan_fingerprint",
        )}
        self.assertEqual(self.approvals.find(identity)["approval_id"], record["approval_id"])

    def test_corrupt_exact_record_fails_closed(self):
        self.make_terminal_job()
        record, _created = self.create()
        identity = {field: record[field] for field in (
            "target_release_id", "target_commit", "target_manifest_sha256",
            "migration_plan_digest", "trusted_plan_fingerprint",
        )}
        path = self.approvals._path(identity)
        path.write_text(json.dumps({"schema_version": 999}), encoding="utf-8")
        path.chmod(0o600)
        with self.assertRaises(ApprovalError):
            self.approvals.find(identity)
