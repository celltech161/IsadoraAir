"""Trusted companion migration authorization: runtime 11 and Django agree.

Every case builds a small, real Git history (r0001 -> r0002 -> r0003, where
r0003 introduces a migration) plus companion commits, then asks BOTH the
protected runtime-11 validator and Django's release-validator mirror to
judge deploy/migration_authorizations/r0003.json. They must agree.
"""
import hashlib
import json
from pathlib import Path
import tempfile

from django.test import SimpleTestCase

from updatecenter import migration_authorization as django_side

from .phase_b_helpers import create_release_repository, git
from isadoraair_updater.migration_authorization import (
    central_authorization_covers, manual_operation_identity, validate_release_companion,
)
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import ReleaseError, TrustedRepository, load_chain

MIGRATION = "sample.0001_first"
COMPANION = "deploy/migration_authorizations/r0003.json"


class CompanionRepository:
    def __init__(self, root: Path):
        self.author, _upstream, _bootstrap, self.r0002, self.r0003 = create_release_repository(
            root, third_release_changes={
                "migrations_required": [MIGRATION], "migration_compatibility": "additive",
            },
        )
        self.migration_sha = hashlib.sha256((self.author / "sample/migrations/0001_first.py").read_bytes()).hexdigest()
        self.manifest_sha = hashlib.sha256((self.author / "deploy/releases/r0003.json").read_bytes()).hexdigest()

    def record(self, **changes):
        value = {
            "schema_version": 1, "release_id": "r0003", "target_commit": self.r0003,
            "manifest_sha256": self.manifest_sha,
            "authorized_manual_operations": [{
                "ref": MIGRATION, "migration_file_sha256": self.migration_sha, "operation_index": 0,
                "operation": "RunPython", "classification": "manual",
            }],
        }
        value.update(changes)
        return value

    def commit(self, files: dict, message="companion", *, delete=()):
        for relative, content in files.items():
            destination = self.author / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
            git(self.author, "add", relative)
        for relative in delete:
            git(self.author, "rm", "-q", relative)
        git(self.author, "commit", "-q", "-m", message)
        return git(self.author, "rev-parse", "HEAD")

    def tip(self):
        return git(self.author, "rev-parse", "HEAD")

    def runtime_verdict(self):
        repository = TrustedRepository(self.author / ".git", "unused", "main", CommandRunner())
        tip = self.tip()
        entry = {item.manifest.release_id: item for item in load_chain(repository, tip)}["r0003"]
        try:
            return ("ok", validate_release_companion(repository, tip, entry))
        except ReleaseError as exc:
            return ("rejected", str(exc))

    def django_verdict(self):
        try:
            return ("ok", django_side.validate_release_companion(
                self.author, self.tip(), release_id="r0003", release_commit=self.r0003,
                migrations_required=[MIGRATION],
            ))
        except django_side.CompanionError as exc:
            return ("rejected", str(exc))


class CompanionParityTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = CompanionRepository(Path(self.temp.name) / "repo")

    def tearDown(self):
        self.temp.cleanup()

    def verdicts(self):
        runtime = self.repo.runtime_verdict()
        django = self.repo.django_verdict()
        self.assertEqual(runtime[0], django[0], (runtime, django))
        if runtime[0] == "ok":
            self.assertEqual(runtime[1], django[1])
        return runtime

    def test_correct_companion_is_accepted_by_both(self):
        self.repo.commit({COMPANION: self.repo.record()})
        status, identities = self.verdicts()
        self.assertEqual(status, "ok")
        self.assertEqual(identities, {(MIGRATION, self.repo.migration_sha, 0, "RunPython", "manual")})

    def test_absent_companion_contributes_nothing_in_both(self):
        self.assertEqual(self.verdicts(), ("ok", None))

    def test_wrong_target_commit_is_rejected_by_both(self):
        self.repo.commit({COMPANION: self.repo.record(target_commit=self.repo.r0002)})
        self.assertEqual(self.verdicts()[0], "rejected")

    def test_wrong_manifest_digest_is_rejected_by_both(self):
        self.repo.commit({COMPANION: self.repo.record(manifest_sha256="1" * 64)})
        self.assertEqual(self.verdicts()[0], "rejected")

    def test_malformed_schema_is_rejected_by_both(self):
        for content in ("{not json", {**self.repo.record(), "extra": 1}, self.repo.record(schema_version=2)):
            with self.subTest(content=str(content)[:40]):
                self.tearDown()
                self.setUp()
                self.repo.commit({COMPANION: content})
                self.assertEqual(self.verdicts()[0], "rejected")

    def test_companion_introduced_in_the_release_commit_itself_is_rejected_by_both(self):
        self.temp.cleanup()
        self.temp = tempfile.TemporaryDirectory()
        author, _u, _b, _r2, r3 = create_release_repository(
            Path(self.temp.name) / "repo",
            third_release_changes={"migrations_required": [MIGRATION], "migration_compatibility": "additive"},
            third_release_files={COMPANION: json.dumps({"placeholder": True})},
        )
        self.repo.author, self.repo.r0003 = author, r3
        self.assertEqual(self.verdicts()[0], "rejected")

    def test_later_modification_is_rejected_by_both(self):
        self.repo.commit({COMPANION: self.repo.record()})
        self.repo.commit({COMPANION: {**self.repo.record(), "authorized_manual_operations": [
            {**self.repo.record()["authorized_manual_operations"][0], "operation_index": 1},
        ]}}, "tamper")
        self.assertEqual(self.verdicts()[0], "rejected")

    def test_deletion_is_rejected_by_both(self):
        self.repo.commit({COMPANION: self.repo.record()})
        self.repo.commit({}, "delete", delete=(COMPANION,))
        self.assertEqual(self.verdicts()[0], "rejected")

    def test_delete_then_re_add_is_rejected_by_both(self):
        self.repo.commit({COMPANION: self.repo.record()})
        self.repo.commit({}, "delete", delete=(COMPANION,))
        self.repo.commit({COMPANION: self.repo.record()}, "re-add")
        self.assertEqual(self.verdicts()[0], "rejected")

    def test_operation_not_introduced_by_that_release_is_rejected_by_both(self):
        record = self.repo.record()
        record["authorized_manual_operations"][0]["ref"] = "sample.0002_other"
        self.repo.commit({COMPANION: record})
        self.assertEqual(self.verdicts()[0], "rejected")

    def test_real_migration_not_declared_by_that_release_is_rejected_by_both(self):
        """The migration exists at the release commit with exactly the bytes
        the companion claims -- only the release-local rule can reject it."""
        self.temp.cleanup()
        self.temp = tempfile.TemporaryDirectory()
        undeclared = "sample/migrations/0000_earlier.py"
        author, _u, _b, r2, r3 = create_release_repository(
            Path(self.temp.name) / "repo",
            third_release_changes={"migrations_required": [MIGRATION], "migration_compatibility": "additive"},
            third_release_files={undeclared: "# introduced but not declared by r0003\n"},
        )
        self.repo.author, self.repo.r0002, self.repo.r0003 = author, r2, r3
        self.repo.manifest_sha = hashlib.sha256((author / "deploy/releases/r0003.json").read_bytes()).hexdigest()
        record = self.repo.record()
        record["authorized_manual_operations"][0].update(
            ref="sample.0000_earlier",
            migration_file_sha256=hashlib.sha256((author / undeclared).read_bytes()).hexdigest(),
        )
        self.repo.commit({COMPANION: record})
        status, detail = self.verdicts()
        self.assertEqual(status, "rejected")
        self.assertIn("did not introduce", detail)

    def test_operation_with_wrong_migration_bytes_is_rejected_by_both(self):
        record = self.repo.record()
        record["authorized_manual_operations"][0]["migration_file_sha256"] = "2" * 64
        self.repo.commit({COMPANION: record})
        self.assertEqual(self.verdicts()[0], "rejected")

    def test_non_metadata_only_introducing_commit_is_rejected_by_both(self):
        self.repo.commit({COMPANION: self.repo.record(), "README": "changed alongside\n"})
        self.assertEqual(self.verdicts()[0], "rejected")

    def test_django_validator_flags_orphan_companion_files(self):
        self.repo.commit({"deploy/migration_authorizations/r0999.json": self.repo.record(release_id="r0999"),
                          "deploy/migration_authorizations/notes.txt": "x"})
        orphans = django_side.orphan_companions(self.repo.author, self.repo.tip(), ["r0001", "r0002", "r0003"])
        self.assertEqual(sorted(orphans), ["notes.txt", "r0999.json"])


class SubsetCoverageTests(SimpleTestCase):
    """F1-F6 against the union rule: one target-side set {A,B,C}."""

    A = ("app.0101_a", "a1" * 32, 0, "RunPython", "manual")
    B = ("app.0102_b", "b2" * 32, 0, "RunSQL", "manual")
    C = ("app.0103_c", "c3" * 32, 1, "RunPython", "manual")

    def station(self, *identities):
        items, manual = [], []
        for ref, sha, index, operation, classification in identities:
            items.append({"ref": ref, "migration_file_sha256": sha})
            manual.append({"ref": ref, "operation_index": index, "operation": operation,
                           "classification": classification, "detail": "outside allowlist"})
        return {"plan_items": items, "manual_operations": manual}

    def covered(self, *identities, authorizations=None):
        authorizations = authorizations or {"r0105": frozenset({self.A, self.B, self.C})}
        return central_authorization_covers(authorizations, **self.station(*identities))

    def test_f1_f2_f3_three_baselines_are_covered_by_one_set(self):
        self.assertTrue(self.covered(self.A, self.B))
        self.assertTrue(self.covered(self.A, self.B, self.C))
        self.assertTrue(self.covered(self.A))

    def test_f4_unauthorized_operation_is_not_covered(self):
        self.assertFalse(self.covered(self.A, self.B, ("app.0104_d", "d4" * 32, 0, "RunPython", "manual")))

    def test_f5_same_migration_with_different_bytes_is_not_covered(self):
        self.assertFalse(self.covered(("app.0101_a", "ee" * 32, 0, "RunPython", "manual")))

    def test_f6_same_operation_at_another_index_is_not_covered(self):
        self.assertFalse(self.covered(("app.0101_a", "a1" * 32, 1, "RunPython", "manual")))

    def test_skipped_releases_are_covered_by_the_union_of_release_local_sets(self):
        authorizations = {"r0106": frozenset({self.A}), "r0107": frozenset({self.B})}
        self.assertTrue(self.covered(self.A, self.B, authorizations=authorizations))
        self.assertFalse(self.covered(self.A, self.C, authorizations=authorizations))

    def test_nothing_is_covered_without_manual_operations_or_companions(self):
        self.assertFalse(self.covered(authorizations={"r0105": frozenset({self.A})}))
        self.assertFalse(central_authorization_covers({}, **self.station(self.A)))

    def test_identity_ignores_free_text_detail(self):
        operation = {"ref": "app.0101_a", "operation_index": 0, "operation": "RunPython",
                     "classification": "manual", "detail": "anything"}
        self.assertEqual(manual_operation_identity(operation, "a1" * 32), self.A)
