"""Release-identity closure regression tests (r0093 -> r0094 recovery).

r0093's manifest was introduced by commit 4643760 and its attestation by the
LATER commit f43266c.  Production resolves a release's immutable identity with
``TrustedRepository.introducing_commit()`` and reads the descriptor, every
runtime file and every attestation from that one commit, so r0093 failed with
``attestation ... is unreadable at 4643760`` even though the working tree
validated.  These tests use real Git repositories and the production
``TrustedRepository`` / ``load_chain`` / ``derive_plan`` / handoff functions to
pin: (a) the broken two-commit shape is rejected at authoring time, (b) the
atomic shape is accepted and activatable end to end, (c) the historical
atomic releases r0090/r0091/r0092 remain valid under the strengthened check.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from unittest import skipUnless

from django.test import SimpleTestCase

from deploy.updater_bootstrap.tools.protected_runtime_release import (
    OPENSSL_BINARY,
    build_descriptor,
    build_statement,
    generation_one_policy_bytes,
    sign_statement,
)

from .phase_b_helpers import PROJECT_ROOT, git

from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import (
    MANAGED_UNIT_POLICIES,
    ReleaseError,
    TrustedRepository,
    derive_plan,
    load_chain,
)
from isadoraair_updater.runtime_handoff import (
    HandoffError,
    materialize_candidate,
    new_supervisor_staging_directory,
    stage_attestations,
)
from protected_bootstrap.manifest_field import parse_protected_runtime_field
from updatecenter.protected_release_validator import (
    ProtectedReleaseValidationError,
    validate_protected_release,
)

DESCRIPTOR_PATH = "deploy/updater_runtime/protected-runtime-descriptor.json"
PRODUCTION_TRUST_POLICY = Path("/etc/isadoraair/updater-trust.json")
PRODUCTION_SIGNERS = Path("/etc/isadoraair/updater-trust")

# Historical commits from the real repository (all ancestors of every later
# release).  r0090/r0091/r0092 each introduced manifest + descriptor +
# attestation in one commit; r0093 split them across two.
REAL_RELEASES = {  # release id -> (introducing commit prefix, previous generation)
    "r0090": ("d5bc70d", 5),
    "r0091": ("e247262", 6),
    "r0092": ("abb3a54", 7),
}
R0093_MANIFEST_COMMIT = "4643760f2defb6898288a3f55b7b8fa55efedc82"
R0093_ATTESTATION_COMMIT = "f43266cca7f8b3b7f035c49ed9da2b57399f12ca"


def _manifest_dict(release_id, previous, *, generation, descriptor_sha256, bootstrap=None,
                   protected=True):
    data = {
        "schema_version": 1, "release_id": release_id, "previous_release_id": previous,
        "minimum_updater_protocol_version": 5, "summary": "fixture",
        "migrations_required": [], "migration_compatibility": None,
        "manual_bootstrap_required": False,
        "python_requirements_changed": False, "requirements_sha256": None,
        "apt_packages_new": [], "systemd_units_changed": [],
        "systemd_units_new_required": [], "systemd_units_new_optional": [],
        "systemd_units_removed_or_renamed": [], "collectstatic_required": False,
        "services_requiring_restart": [], "nginx_changed": False,
        "runtime_components_changed": False, "minimum_supported_release_id": None,
    }
    if bootstrap is not None:
        data["bootstrap_commit"] = bootstrap
    if protected:
        data["protected_runtime"] = {
            "generation": generation, "descriptor_path": DESCRIPTOR_PATH,
            "descriptor_sha256": descriptor_sha256,
            "minimum_bootstrap_protocol_version": 1, "runtime_version": 5,
            "manifest_protocol_version": 5, "supported_wire_protocols": [3],
            "attestations": [f"deploy/updater_attestations/{release_id}-primary.json"],
        }
    return data


class SyntheticReleaseRepository:
    """A real Git repository with a real Ed25519 release signer."""

    def __init__(self, test_case: SimpleTestCase):
        temp = tempfile.TemporaryDirectory(prefix="release-identity-closure-")
        test_case.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "commit.gpgsign", "false")
        (self.repo / "README").write_text("baseline\n", encoding="utf-8")
        self._runtime_files()
        git(self.repo, "add", "README")
        git(self.repo, "commit", "-m", "baseline")
        self.bootstrap = git(self.repo, "rev-parse", "HEAD")
        self.releases = self.repo / "deploy" / "releases"
        self.releases.mkdir(parents=True)
        self._write_json(self.releases / "r0001.json", _manifest_dict(
            "r0001", None, generation=0, descriptor_sha256="", bootstrap=self.bootstrap, protected=False,
        ))
        git(self.repo, "add", "deploy/releases/r0001.json")
        git(self.repo, "commit", "-m", "bootstrap manifest")
        self.signers = self.root / "signers"
        self.signers.mkdir()
        self.private = self.root / "release.key"
        self.public = self.signers / "primary.pem"
        subprocess.run(
            [OPENSSL_BINARY, "genpkey", "-algorithm", "ed25519", "-out", str(self.private)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.private.chmod(0o600)
        subprocess.run(
            [OPENSSL_BINARY, "pkey", "-in", str(self.private), "-pubout", "-out", str(self.public)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.trust_policy = self.root / "release-trust.json"
        self._write_json(self.trust_policy, {
            "schema_version": 1, "signature_algorithm": "ed25519", "threshold": 1,
            "signers": [{"id": "primary-release", "public_key_path": str(self.public)}],
        })
        self.repository = TrustedRepository(self.repo / ".git", "unused", "main", CommandRunner())

    @staticmethod
    def _write_json(path: Path, value: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def _runtime_files(self, marker: str = "1"):
        runtime = self.repo / "deploy" / "updater_runtime"
        (runtime / "isadoraair_updater").mkdir(parents=True, exist_ok=True)
        (runtime / "protected_bootstrap").mkdir(parents=True, exist_ok=True)
        files = {
            "README.md": f"reviewed runtime {marker}\n".encode(),
            "updaterctl.py": b"def main(): return 0\n",
            "updaterd.py": b"def main(): return 0\n",
            "isadoraair_updater/__init__.py": b"RUNTIME_VERSION = 5\n",
            "protected_bootstrap/__init__.py": b"BOOTSTRAP_PROTOCOL_VERSION = 1\n",
            "protected-policy.json": generation_one_policy_bytes(),
        }
        for relative, content in files.items():
            (runtime / relative).write_bytes(content)

    def stage_release(self, release_id, previous, generation, *, attestation=True, marker=None):
        """Write manifest + descriptor + (optionally) attestation into the
        working tree only -- nothing is committed."""
        if marker is not None:
            self._runtime_files(marker)
        runtime = self.repo / "deploy" / "updater_runtime"
        descriptor = build_descriptor(
            runtime_root=runtime, generation=generation, runtime_version=5,
            manifest_protocol_version=5, supported_wire_protocols=(3,),
        )
        (self.repo / DESCRIPTOR_PATH).write_bytes(descriptor)
        digest = hashlib.sha256(descriptor).hexdigest()
        self._write_json(self.releases / f"{release_id}.json", _manifest_dict(
            release_id, previous, generation=generation, descriptor_sha256=digest,
        ))
        if attestation:
            self.write_attestation(release_id, previous, generation)
        return self.releases / f"{release_id}.json"

    def write_attestation(self, release_id, previous, generation):
        descriptor = (self.repo / DESCRIPTOR_PATH).read_bytes()
        signature = sign_statement(
            statement=build_statement(
                descriptor_bytes=descriptor, release_id=release_id,
                previous_release_id=previous, generation=generation,
            ),
            private_key_path=self.private, public_key_path=self.public,
        )
        self._write_json(self.repo / f"deploy/updater_attestations/{release_id}-primary.json", {
            "schema_version": 1, "signer_id": "primary-release",
            "signature_base64": base64.b64encode(signature).decode("ascii"),
        })

    def commit(self, message, *paths) -> str:
        git(self.repo, "add", *(paths or ("deploy",)))
        git(self.repo, "commit", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def validate(self, release_id, *, previous_generation, identity_tip=None):
        return validate_protected_release(
            checkout_root=self.repo, manifest_path=self.releases / f"{release_id}.json",
            trust_policy_path=self.trust_policy, signer_directory=self.signers,
            previous_generation=previous_generation, identity_tip=identity_tip,
        )

    def field(self, release_id):
        data = json.loads((self.releases / f"{release_id}.json").read_text())
        return parse_protected_runtime_field(data["protected_runtime"], label=release_id)

    def stage(self, release_id, commit):
        """Exactly the two production handoff calls (executor.py)."""
        field = self.field(release_id)
        slots = Path(tempfile.mkdtemp(dir=self.root, prefix="slots-"))
        staging = new_supervisor_staging_directory(slots)
        materialize_candidate(self.repository, field, commit, staging)
        return stage_attestations(self.repository, field, commit, slots, "B")


class BrokenTwoCommitShapeTests(SimpleTestCase):
    """r0093's exact shape: manifest+descriptor+runtime in commit A, the
    attestation only in a later commit B."""

    def setUp(self):
        self.fixture = SyntheticReleaseRepository(self)
        fixture = self.fixture
        fixture.stage_release("r0002", "r0001", 1, attestation=False)
        self.commit_a = fixture.commit("release r0002 (unsigned)")
        fixture.write_attestation("r0002", "r0001", 1)
        self.commit_b = fixture.commit("sign r0002")

    def test_production_identity_rule_resolves_the_release_to_commit_a(self):
        self.assertEqual(
            self.fixture.repository.introducing_commit("deploy/releases/r0002.json", self.commit_b),
            self.commit_a,
        )

    def test_runtime_staging_cannot_read_the_attestation_from_commit_a(self):
        with self.assertRaisesRegex(HandoffError, r"attestation .* is unreadable at " + self.commit_a):
            self.fixture.stage("r0002", self.commit_a)
        # The attestation is valid and present -- only at the wrong commit.
        self.assertIsNotNone(self.fixture.repository.read_file(
            self.commit_b, "deploy/updater_attestations/r0002-primary.json"))

    def test_production_plan_carries_commit_a_as_the_transition_commit(self):
        plan = derive_plan(
            self.fixture.repository, self.commit_b, self.fixture.bootstrap, "r0002",
            known_units=frozenset(MANAGED_UNIT_POLICIES),
        )
        self.assertEqual(plan.target_commit, self.commit_a)
        self.assertEqual(plan.protected_runtime_transition.commit, self.commit_a)

    def test_authoring_validation_rejects_the_shape_before_publication(self):
        # The working tree is fully valid and correctly signed...
        self.assertTrue((self.fixture.repo / "deploy/updater_attestations/r0002-primary.json").is_file())
        # ...but the strengthened validator proves the commit production uses is not self-sufficient.
        with self.assertRaisesRegex(
            ProtectedReleaseValidationError,
            r"introducing commit " + self.commit_a + r" would fail: attestation .* is unreadable",
        ):
            self.fixture.validate("r0002", previous_generation=0)

    def test_committed_unsigned_release_is_rejected_even_before_signing(self):
        with self.assertRaises(ProtectedReleaseValidationError):
            self.fixture.validate("r0002", previous_generation=0, identity_tip=self.commit_a)

    def test_editing_a_committed_manifest_makes_identity_unresolvable_not_fixed(self):
        manifest = self.fixture.releases / "r0002.json"
        manifest.write_text(manifest.read_text() + "\n", encoding="utf-8")
        edited = self.fixture.commit("touch r0002 manifest again")
        self.assertIsNone(
            self.fixture.repository.introducing_commit("deploy/releases/r0002.json", edited))
        with self.assertRaisesRegex(ProtectedReleaseValidationError, "no unique immutable introducing commit"):
            self.fixture.validate("r0002", previous_generation=0)


class AtomicShapeTests(SimpleTestCase):
    def setUp(self):
        self.fixture = SyntheticReleaseRepository(self)

    def _atomic_release(self):
        self.fixture.stage_release("r0002", "r0001", 1)
        return self.fixture.commit("release r0002 (atomic)")

    def test_atomic_release_validates_and_reports_its_identity(self):
        commit = self._atomic_release()
        evidence = self.fixture.validate("r0002", previous_generation=0)
        self.assertEqual(evidence["release_identity"], {
            "mode": "committed", "identity_tip": commit, "introducing_commit": commit,
        })
        self.assertEqual(evidence["verified_signers"], ["primary-release"])

    def test_end_to_end_resolution_staging_and_verification_from_the_release_commit(self):
        commit = self._atomic_release()
        fixture = self.fixture
        # 1-2. Resolve via the production identity rule; it is the atomic commit.
        self.assertEqual(
            fixture.repository.introducing_commit("deploy/releases/r0002.json", commit), commit)
        chain = load_chain(fixture.repository, commit)
        self.assertEqual([entry.manifest.release_id for entry in chain], ["r0001", "r0002"])
        self.assertEqual(chain[-1].commit, commit)
        plan = derive_plan(
            fixture.repository, commit, fixture.bootstrap, "r0002",
            known_units=frozenset(MANAGED_UNIT_POLICIES),
        )
        self.assertEqual(plan.target_commit, commit)
        self.assertEqual(plan.protected_runtime_transition.commit, commit)
        # 3-5. Manifest, descriptor and attestation are all readable from that commit.
        field = fixture.field("r0002")
        for path in ("deploy/releases/r0002.json", field.descriptor_path, *field.attestations):
            self.assertIsNotNone(fixture.repository.read_file(commit, path), path)
        # 6. Stage the protected runtime exactly as the executor does.
        attestations = fixture.stage("r0002", commit)
        self.assertEqual([item.name for item in attestations.iterdir()], ["00-r0002-primary.json"])
        # 7. Signature, descriptor and inventory verify normally.
        evidence = fixture.validate("r0002", previous_generation=0)
        self.assertEqual(evidence["descriptor_sha256"], field.descriptor_sha256)

    def test_same_end_to_end_flow_fails_when_the_attestation_exists_only_later(self):
        fixture = self.fixture
        fixture.stage_release("r0002", "r0001", 1, attestation=False)
        commit_a = fixture.commit("unsigned")
        fixture.write_attestation("r0002", "r0001", 1)
        tip = fixture.commit("sign")
        plan = derive_plan(
            fixture.repository, tip, fixture.bootstrap, "r0002",
            known_units=frozenset(MANAGED_UNIT_POLICIES),
        )
        with self.assertRaises(HandoffError):
            fixture.stage("r0002", plan.protected_runtime_transition.commit)
        self.assertEqual(plan.protected_runtime_transition.commit, commit_a)

    def test_historical_release_is_validated_from_its_own_commit_not_the_working_tree(self):
        first = self._atomic_release()
        self.fixture.stage_release("r0003", "r0002", 2, marker="2")  # working tree moves on
        self.fixture.commit("release r0003 (atomic)")
        # The working-tree descriptor now belongs to r0003; r0002 must still validate.
        evidence = self.fixture.validate("r0002", previous_generation=0)
        self.assertEqual(evidence["release_identity"]["introducing_commit"], first)
        self.assertEqual(evidence["generation"], 1)


class ProspectiveAtomicTests(SimpleTestCase):
    """The correct signing-stop state: nothing about the release is committed."""

    def setUp(self):
        self.fixture = SyntheticReleaseRepository(self)

    def test_complete_uncommitted_candidate_is_accepted_prospectively(self):
        self.fixture.stage_release("r0002", "r0001", 1)
        evidence = self.fixture.validate("r0002", previous_generation=0)
        self.assertEqual(evidence["release_identity"]["mode"], "prospective_atomic")
        self.assertIsNone(evidence["release_identity"]["introducing_commit"])

    def test_candidate_missing_its_attestation_is_rejected(self):
        self.fixture.stage_release("r0002", "r0001", 1, attestation=False)
        with self.assertRaisesRegex(ProtectedReleaseValidationError, "attestation .* is absent|absent from the working tree"):
            self.fixture.validate("r0002", previous_generation=0)

    def test_candidate_whose_attestation_would_be_git_ignored_is_rejected(self):
        self.fixture.stage_release("r0002", "r0001", 1)
        (self.fixture.repo / ".gitignore").write_text("deploy/updater_attestations/\n", encoding="utf-8")
        with self.assertRaisesRegex(ProtectedReleaseValidationError, "Git-ignored"):
            self.fixture.validate("r0002", previous_generation=0)

    def test_non_git_checkout_cannot_establish_release_identity(self):
        self.fixture.stage_release("r0002", "r0001", 1)
        with tempfile.TemporaryDirectory() as bare_copy:
            import shutil
            destination = Path(bare_copy) / "checkout"
            shutil.copytree(self.fixture.repo, destination, ignore=shutil.ignore_patterns(".git"))
            with self.assertRaisesRegex(ProtectedReleaseValidationError, "immutable release identity"):
                validate_protected_release(
                    checkout_root=destination, manifest_path=destination / "deploy/releases/r0002.json",
                    trust_policy_path=self.fixture.trust_policy, signer_directory=self.fixture.signers,
                    previous_generation=0,
                )


class HistoricalReleaseIdentityClosureTests(SimpleTestCase):
    """Real repository history.  Skipped only if the historical objects are
    not present (for example a shallow clone)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        common = Path(git(PROJECT_ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        cls.repository = TrustedRepository(common, "unused", "main", CommandRunner())
        cls.tip = git(PROJECT_ROOT, "rev-parse", "HEAD")

    def _require(self, sha):
        if not self.repository.commit_exists(self.repository.rev_parse(sha) or "0" * 40):
            self.skipTest(f"historical commit {sha} is not present in this clone")
        return self.repository.rev_parse(sha)

    def test_r0090_r0091_r0092_manifest_and_attestation_share_one_commit(self):
        for release_id, (short, _previous) in REAL_RELEASES.items():
            expected = self._require(short)
            manifest = f"deploy/releases/{release_id}.json"
            attestation = f"deploy/updater_attestations/{release_id}-primary.json"
            self.assertEqual(self.repository.introducing_commit(manifest, self.tip), expected, release_id)
            self.assertEqual(
                self.repository.introducing_commit(attestation, self.tip), expected, release_id)
            self.assertIsNotNone(self.repository.read_file(expected, attestation), release_id)

    def test_r0090_r0091_r0092_stage_from_their_introducing_commits(self):
        for release_id, (short, _previous) in REAL_RELEASES.items():
            commit = self._require(short)
            data = json.loads(self.repository.read_file(commit, f"deploy/releases/{release_id}.json"))
            field = parse_protected_runtime_field(data["protected_runtime"], label=release_id)
            with tempfile.TemporaryDirectory() as scratch:
                slots = Path(scratch)
                staging = new_supervisor_staging_directory(slots)
                materialize_candidate(self.repository, field, commit, staging)
                stage_attestations(self.repository, field, commit, slots, "B")

    def test_r0093_is_the_documented_split_commit_defect(self):
        manifest_commit = self._require(R0093_MANIFEST_COMMIT)
        attestation_commit = self._require(R0093_ATTESTATION_COMMIT)
        path = "deploy/updater_attestations/r0093-primary.json"
        self.assertEqual(
            self.repository.introducing_commit("deploy/releases/r0093.json", self.tip), manifest_commit)
        self.assertEqual(self.repository.introducing_commit(path, self.tip), attestation_commit)
        self.assertIsNone(self.repository.read_file(manifest_commit, path))
        self.assertIsNotNone(self.repository.read_file(attestation_commit, path))

    @skipUnless(PRODUCTION_TRUST_POLICY.is_file() and PRODUCTION_SIGNERS.is_dir(),
                "production release trust fixture is not installed on this host")
    def test_full_validator_accepts_r0090_r0091_r0092_and_rejects_r0093(self):
        for release_id, (short, previous_generation) in REAL_RELEASES.items():
            self._require(short)
            evidence = validate_protected_release(
                checkout_root=PROJECT_ROOT, manifest_path=PROJECT_ROOT / f"deploy/releases/{release_id}.json",
                trust_policy_path=PRODUCTION_TRUST_POLICY, signer_directory=PRODUCTION_SIGNERS,
                previous_generation=previous_generation,
            )
            self.assertEqual(evidence["release_identity"]["mode"], "committed", release_id)
            self.assertEqual(evidence["release_identity"]["introducing_commit"],
                             self.repository.rev_parse(short), release_id)
        self._require(R0093_MANIFEST_COMMIT)
        with self.assertRaisesRegex(ProtectedReleaseValidationError, "unreadable at " + R0093_MANIFEST_COMMIT):
            validate_protected_release(
                checkout_root=PROJECT_ROOT, manifest_path=PROJECT_ROOT / "deploy/releases/r0093.json",
                trust_policy_path=PRODUCTION_TRUST_POLICY, signer_directory=PRODUCTION_SIGNERS,
                previous_generation=8,
            )
