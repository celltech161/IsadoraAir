"""Update Center Phase D, D3-B/D3-D/D3-H/D3-K: candidate materialization
from root-trusted Git, the runtime-handoff milestone vocabulary, the
central pre-mutation gate, and handoff-recovery classification."""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from .phase_b_helpers import RUNTIME_ROOT, git  # noqa: F401

from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import TrustedRepository
from isadoraair_updater.runtime_handoff import (
    HANDOFF_MILESTONES, MUTATION_GATE_MILESTONE, SAFE_YIELD_MILESTONE,
    HandoffError, MutationGateError, RecoveryAmbiguous,
    classify_handoff_recovery, handoff_required, materialize_candidate,
    require_mutation_allowed,
)
from protected_bootstrap.descriptor import FileEntry, compute_bundle_sha256
from protected_bootstrap.manifest_field import ProtectedRuntimeField


def _descriptor_and_files(*, generation=1, wire=(3,)):
    entry_files = {
        "updaterd.py": b"import sys\nsys.exit(0)\n",
        "isadoraair_updater/__init__.py": b"PROTOCOL_VERSION = 3\n",
    }
    entries = []
    for path, content in entry_files.items():
        mode = "0755" if path == "updaterd.py" else "0644"
        entries.append(FileEntry(path, hashlib.sha256(content).hexdigest(), mode, len(content)))
    entries = tuple(sorted(entries, key=lambda e: e.path))
    descriptor = {
        "schema_version": 1, "generation": generation, "runtime_version": 5,
        "manifest_protocol_version": 5, "supported_wire_protocols": sorted(wire),
        "entrypoint": "updaterd.py",
        "files": [{"path": e.path, "sha256": e.sha256, "mode": e.mode, "size_bytes": e.size_bytes} for e in entries],
        "bundle_sha256": compute_bundle_sha256(entries),
    }
    descriptor_bytes = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return descriptor_bytes, entry_files


class MaterializeCandidateTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.author = self.root / "author"
        self.author.mkdir()
        git(self.author, "init", "-b", "main")
        (self.author / "README").write_text("baseline\n")
        git(self.author, "add", "README")
        git(self.author, "commit", "-m", "baseline")

        self.descriptor_bytes, self.entry_files = _descriptor_and_files()
        runtime_dir = self.author / "deploy" / "updater_runtime"
        runtime_dir.mkdir(parents=True)
        (runtime_dir / "updater-descriptor.json").write_bytes(self.descriptor_bytes)
        for path, content in self.entry_files.items():
            destination = runtime_dir / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        git(self.author, "add", ".")
        git(self.author, "commit", "-m", "protected runtime generation 1")
        self.target_commit = git(self.author, "rev-parse", "HEAD")

        self.upstream = self.root / "upstream.git"
        subprocess.run(["git", "init", "--bare", str(self.upstream)], check=True,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        git(self.author, "remote", "add", "origin", str(self.upstream))
        git(self.author, "push", "-u", "origin", "main")

        self.repo = TrustedRepository(self.root / "trusted.git", str(self.upstream), "main", CommandRunner())
        self.repo.fetch()

        self.field = ProtectedRuntimeField(
            generation=1, descriptor_path="deploy/updater_runtime/updater-descriptor.json",
            descriptor_sha256=hashlib.sha256(self.descriptor_bytes).hexdigest(),
            minimum_bootstrap_protocol_version=1, runtime_version=5, manifest_protocol_version=5,
            supported_wire_protocols=(3,), attestations=("deploy/updater_attestations/r0027.sig.json",),
        )
        self.staging = self.root / "staging"
        self.staging.mkdir()

    def test_real_materialization_matches_descriptor_exactly(self):
        result = materialize_candidate(self.repo, self.field, self.target_commit, self.staging)
        self.assertEqual(result.descriptor_sha256, self.field.descriptor_sha256)
        self.assertEqual(result.descriptor.entrypoint, "updaterd.py")
        for path, content in self.entry_files.items():
            self.assertEqual((self.staging / path).read_bytes(), content)
        self.assertEqual(oct((self.staging / "updaterd.py").stat().st_mode & 0o777), "0o755")

    def test_wrong_descriptor_sha_pin_rejected(self):
        wrong_field = ProtectedRuntimeField(
            generation=1, descriptor_path=self.field.descriptor_path,
            descriptor_sha256="a" * 64, minimum_bootstrap_protocol_version=1,
            runtime_version=5, manifest_protocol_version=5, supported_wire_protocols=(3,),
            attestations=self.field.attestations,
        )
        with self.assertRaises(HandoffError):
            materialize_candidate(self.repo, wrong_field, self.target_commit, self.staging)

    def test_tampered_file_content_rejected(self):
        # Simulates a trusted-Git file whose bytes were changed WITHOUT
        # updating the descriptor -- the per-file sha256 check must
        # catch it even though the descriptor's own pin matched.
        runtime_dir = self.author / "deploy" / "updater_runtime"
        (runtime_dir / "updaterd.py").write_bytes(b"import sys\nsys.exit(1)\n")
        git(self.author, "commit", "-am", "tamper")
        tampered_commit = git(self.author, "rev-parse", "HEAD")
        with self.assertRaises(HandoffError):
            materialize_candidate(self.repo, self.field, tampered_commit, self.staging)

    def test_missing_descriptor_file_rejected(self):
        missing_field = ProtectedRuntimeField(
            generation=1, descriptor_path="deploy/updater_runtime/does-not-exist.json",
            descriptor_sha256=self.field.descriptor_sha256, minimum_bootstrap_protocol_version=1,
            runtime_version=5, manifest_protocol_version=5, supported_wire_protocols=(3,),
            attestations=self.field.attestations,
        )
        with self.assertRaises(HandoffError):
            materialize_candidate(self.repo, missing_field, self.target_commit, self.staging)

    def test_never_writes_outside_staging_directory(self):
        # A descriptor file entry with a real DescriptorError-blocked
        # path (".." segment) never reaches materialize_candidate at
        # all -- parse_descriptor_dict() itself refuses it. This test
        # proves that refusal happens (not a false-positive escape).
        malicious_descriptor = json.loads(self.descriptor_bytes.decode("utf-8"))
        malicious_descriptor["files"][0]["path"] = "../../etc/passwd"
        raw = json.dumps(malicious_descriptor, sort_keys=True, separators=(",", ":")).encode("utf-8")
        runtime_dir = self.author / "deploy" / "updater_runtime"
        (runtime_dir / "updater-descriptor.json").write_bytes(raw)
        git(self.author, "commit", "-am", "malicious descriptor")
        malicious_commit = git(self.author, "rev-parse", "HEAD")
        malicious_field = ProtectedRuntimeField(
            generation=1, descriptor_path=self.field.descriptor_path,
            descriptor_sha256=hashlib.sha256(raw).hexdigest(), minimum_bootstrap_protocol_version=1,
            runtime_version=5, manifest_protocol_version=5, supported_wire_protocols=(3,),
            attestations=self.field.attestations,
        )
        with self.assertRaises(HandoffError):
            materialize_candidate(self.repo, malicious_field, malicious_commit, self.staging)
        self.assertFalse((self.staging.parent / "etc").exists())


class MutationGateTests(SimpleTestCase):
    def test_none_protected_runtime_is_a_complete_no_op(self):
        # Parity: an ordinary release's mutation calls are unaffected
        # regardless of milestones -- even an EMPTY milestone list.
        require_mutation_allowed(None, [])
        require_mutation_allowed(None, ["anything"])

    def test_protected_runtime_without_acceptance_milestone_refused(self):
        field = ProtectedRuntimeField(
            generation=1, descriptor_path="deploy/updater_runtime/d.json",
            descriptor_sha256="a" * 64, minimum_bootstrap_protocol_version=1,
            runtime_version=5, manifest_protocol_version=5, supported_wire_protocols=(3,),
            attestations=("deploy/updater_attestations/a.json",),
        )
        with self.assertRaises(MutationGateError):
            require_mutation_allowed(field, ["runtime_activation_requested"])

    def test_protected_runtime_with_acceptance_milestone_allowed(self):
        field = ProtectedRuntimeField(
            generation=1, descriptor_path="deploy/updater_runtime/d.json",
            descriptor_sha256="a" * 64, minimum_bootstrap_protocol_version=1,
            runtime_version=5, manifest_protocol_version=5, supported_wire_protocols=(3,),
            attestations=("deploy/updater_attestations/a.json",),
        )
        require_mutation_allowed(field, ["runtime_activation_requested", MUTATION_GATE_MILESTONE])

    def test_handoff_required_predicate(self):
        self.assertFalse(handoff_required(None))
        field = ProtectedRuntimeField(
            generation=1, descriptor_path="deploy/updater_runtime/d.json",
            descriptor_sha256="a" * 64, minimum_bootstrap_protocol_version=1,
            runtime_version=5, manifest_protocol_version=5, supported_wire_protocols=(3,),
            attestations=("deploy/updater_attestations/a.json",),
        )
        self.assertTrue(handoff_required(field))

    def test_milestone_vocabulary_is_exactly_six_and_ordered(self):
        self.assertEqual(HANDOFF_MILESTONES, (
            "runtime_descriptor_validated", "runtime_candidate_staged",
            "runtime_candidate_verified", "runtime_activation_requested",
            "runtime_activation_accepted", "runtime_generation_committed",
        ))
        self.assertEqual(SAFE_YIELD_MILESTONE, "runtime_activation_requested")
        self.assertEqual(MUTATION_GATE_MILESTONE, "runtime_activation_accepted")
        self.assertLess(
            HANDOFF_MILESTONES.index(SAFE_YIELD_MILESTONE),
            HANDOFF_MILESTONES.index(MUTATION_GATE_MILESTONE),
        )


def _job_state(job_id, *, milestones, protected_runtime_candidate=None, state="running"):
    return {
        "job_id": job_id, "state": state, "milestones": milestones,
        "requested_target_release_id": "r0027",
        "protected_runtime_candidate": protected_runtime_candidate,
    }


class ClassifyHandoffRecoveryTests(SimpleTestCase):
    def test_no_candidates_is_ordinary_clean_startup(self):
        self.assertIsNone(classify_handoff_recovery([], expected_generation=1, expected_descriptor_sha256="a" * 64))
        self.assertIsNone(classify_handoff_recovery(
            [_job_state("j1", milestones=["trusted_source_fetched"])],
            expected_generation=1, expected_descriptor_sha256="a" * 64,
        ))

    def test_candidate_before_safe_yield_milestone_is_not_resumable(self):
        state = _job_state(
            "j1", milestones=["runtime_descriptor_validated", "runtime_candidate_staged"],
            protected_runtime_candidate={"generation": 1, "descriptor_sha256": "a" * 64},
        )
        self.assertIsNone(classify_handoff_recovery([state], expected_generation=1, expected_descriptor_sha256="a" * 64))

    def test_terminal_or_inactive_job_never_counted(self):
        state = _job_state(
            "j1", state="succeeded", milestones=list(HANDOFF_MILESTONES),
            protected_runtime_candidate={"generation": 1, "descriptor_sha256": "a" * 64},
        )
        self.assertIsNone(classify_handoff_recovery([state], expected_generation=1, expected_descriptor_sha256="a" * 64))

    def test_exactly_one_matching_candidate_resolves(self):
        state = _job_state(
            "j1", milestones=["runtime_descriptor_validated", "runtime_candidate_staged",
                              "runtime_candidate_verified", SAFE_YIELD_MILESTONE],
            protected_runtime_candidate={"generation": 2, "descriptor_sha256": "b" * 64},
        )
        facts = classify_handoff_recovery([state], expected_generation=2, expected_descriptor_sha256="b" * 64)
        self.assertEqual(facts.job_id, "j1")
        self.assertEqual(facts.target_release_id, "r0027")
        self.assertEqual(facts.protected_runtime_generation, 2)
        self.assertEqual(facts.protected_runtime_descriptor_sha256, "b" * 64)

    def test_two_candidates_is_ambiguous(self):
        make = lambda job_id: _job_state(
            job_id, milestones=[SAFE_YIELD_MILESTONE],
            protected_runtime_candidate={"generation": 1, "descriptor_sha256": "a" * 64},
        )
        with self.assertRaises(RecoveryAmbiguous):
            classify_handoff_recovery([make("j1"), make("j2")], expected_generation=1, expected_descriptor_sha256="a" * 64)

    def test_mismatched_generation_is_ambiguous_never_silently_resumed(self):
        state = _job_state(
            "j1", milestones=[SAFE_YIELD_MILESTONE],
            protected_runtime_candidate={"generation": 1, "descriptor_sha256": "a" * 64},
        )
        with self.assertRaises(RecoveryAmbiguous):
            classify_handoff_recovery([state], expected_generation=2, expected_descriptor_sha256="a" * 64)

    def test_mismatched_descriptor_sha_is_ambiguous(self):
        state = _job_state(
            "j1", milestones=[SAFE_YIELD_MILESTONE],
            protected_runtime_candidate={"generation": 1, "descriptor_sha256": "a" * 64},
        )
        with self.assertRaises(RecoveryAmbiguous):
            classify_handoff_recovery([state], expected_generation=1, expected_descriptor_sha256="c" * 64)

    def test_malformed_candidate_record_is_ambiguous(self):
        state = _job_state(
            "j1", milestones=[SAFE_YIELD_MILESTONE],
            protected_runtime_candidate={"generation": 1},
        )
        with self.assertRaises(RecoveryAmbiguous):
            classify_handoff_recovery([state], expected_generation=1, expected_descriptor_sha256="a" * 64)
