import io
import hashlib
import json
import os
from pathlib import Path
import tarfile
import tempfile

from django.test import SimpleTestCase

from .phase_b_helpers import create_release_repository, git
from isadoraair_updater.process import CommandRunner, ProcessResult
from isadoraair_updater.release import (
    ReleaseError, TrustedRepository, derive_plan, load_chain, manual_blockers,
)
from isadoraair_updater.staging import StagingError, cleanup, materialize


class TrustedReleaseTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.author, self.upstream, self.bootstrap, self.r0002, self.r0003 = create_release_repository(self.root)
        self.repo = TrustedRepository(self.root / "trusted.git", str(self.upstream), "main", CommandRunner())
        self.tip = self.repo.fetch()

    def tearDown(self):
        self.temp.cleanup()

    def test_trusted_bare_repo_and_chain(self):
        chain = load_chain(self.repo, self.tip)
        self.assertEqual([item.manifest.release_id for item in chain], ["r0001", "r0002", "r0003"])
        self.assertEqual(chain[-1].commit, self.r0003)

    def test_target_release_independently_derived(self):
        with self.assertRaisesRegex(ReleaseError, "latest trusted release"):
            derive_plan(self.repo, self.tip, self.r0002, "r9999")

    def test_exact_plan_and_fingerprint_rederived(self):
        plan = derive_plan(self.repo, self.tip, self.r0002, "r0003")
        self.assertEqual(plan.installed_commit, self.r0002)
        self.assertEqual(plan.target_commit, self.r0003)
        self.assertEqual(len(plan.fingerprint), 64)

    def test_live_unreleased_commit_rejected(self):
        (self.author / "extra").write_text("x", encoding="utf-8")
        git(self.author, "add", "extra")
        git(self.author, "commit", "-m", "unreleased")
        untagged = git(self.author, "rev-parse", "HEAD")
        with self.assertRaisesRegex(ReleaseError, "exactly equal"):
            derive_plan(self.repo, self.tip, untagged, "r0003")

    def test_modified_manifest_identity_rejected(self):
        path = self.author / "deploy" / "releases" / "r0003.json"
        data = json.loads(path.read_text())
        data["summary"] = "modified"
        path.write_text(json.dumps(data), encoding="utf-8")
        git(self.author, "add", str(path.relative_to(self.author)))
        git(self.author, "commit", "-m", "illegally modify manifest")
        git(self.author, "push", "origin", "main")
        tip = self.repo.fetch()
        with self.assertRaisesRegex(ReleaseError, "unique immutable"):
            load_chain(self.repo, tip)

    def test_force_push_is_rejected(self):
        git(self.author, "checkout", "--orphan", "rewritten")
        for child in list(self.author.iterdir()):
            if child.name != ".git":
                if child.is_dir():
                    import shutil
                    shutil.rmtree(child)
                else:
                    child.unlink()
        (self.author / "other").write_text("rewritten", encoding="utf-8")
        git(self.author, "add", "other")
        git(self.author, "commit", "-m", "rewrite")
        git(self.author, "push", "--force", "origin", "HEAD:main")
        with self.assertRaisesRegex(ReleaseError, "non-fast-forward"):
            self.repo.fetch()

    def test_manual_blockers_are_conservative(self):
        _author, upstream, _bootstrap, r2, _r3 = create_release_repository(
            self.root / "blocked",
            third_release_changes={"python_requirements_changed": True, "requirements_sha256": "0" * 64},
        )
        # Cross-check rejects the forged requirements digest before it can become a plan.
        repo = TrustedRepository(self.root / "blocked-trusted.git", str(upstream), "main", CommandRunner())
        tip = repo.fetch()
        with self.assertRaisesRegex(ReleaseError, "requirements hash"):
            derive_plan(repo, tip, r2, "r0003")

    def test_undeclared_requirements_change_is_rejected_from_predecessor_diff(self):
        _author, upstream, _bootstrap, r2, _r3 = create_release_repository(
            self.root / "undeclared-requirements",
            third_release_files={"requirements.txt": "Django==5.2.15\n"},
        )
        repo = TrustedRepository(self.root / "undeclared-requirements.git", str(upstream), "main", CommandRunner())
        tip = repo.fetch()
        with self.assertRaisesRegex(ReleaseError, "python_requirements_changed"):
            derive_plan(repo, tip, r2, "r0003")

    def test_declared_requirements_change_becomes_manual_blocker(self):
        content = "Django==5.2.15\n"
        _author, upstream, _bootstrap, r2, _r3 = create_release_repository(
            self.root / "declared-requirements",
            third_release_changes={
                "python_requirements_changed": True,
                "requirements_sha256": hashlib.sha256(content.encode()).hexdigest(),
            },
            third_release_files={"requirements.txt": content},
        )
        repo = TrustedRepository(self.root / "declared-requirements.git", str(upstream), "main", CommandRunner())
        tip = repo.fetch()
        plan = derive_plan(repo, tip, r2, "r0003")
        self.assertIn("PYTHON_REQUIREMENTS_MANUAL", manual_blockers(plan))

    def test_explicit_manual_bootstrap_gate_aggregates_and_blocks_root_execution(self):
        _author, upstream, _bootstrap, r2, _r3 = create_release_repository(
            self.root / "manual-bootstrap",
            third_release_changes={
                "manual_bootstrap_required": True,
                "minimum_updater_protocol_version": 3,
            },
        )
        repo = TrustedRepository(
            self.root / "manual-bootstrap-trusted.git", str(upstream), "main", CommandRunner()
        )
        tip = repo.fetch()
        plan = derive_plan(repo, tip, r2, "r0003")
        self.assertTrue(plan.manual_bootstrap_required)
        self.assertIn("MANUAL_BOOTSTRAP_REQUIRED", manual_blockers(plan))

    def test_undeclared_systemd_unit_change_is_rejected_from_predecessor_diff(self):
        _author, upstream, _bootstrap, r2, _r3 = create_release_repository(
            self.root / "undeclared-unit",
            third_release_files={"deploy/isadoraair-engine.service": "[Service]\nExecStart=/bin/true\n"},
        )
        repo = TrustedRepository(self.root / "undeclared-unit.git", str(upstream), "main", CommandRunner())
        tip = repo.fetch()
        with self.assertRaisesRegex(ReleaseError, "systemd unit intent"):
            derive_plan(repo, tip, r2, "r0003")

    def test_declared_optional_unit_matches_predecessor_diff(self):
        _author, upstream, _bootstrap, r2, _r3 = create_release_repository(
            self.root / "declared-optional-unit",
            third_release_changes={
                "systemd_units_new_optional": ["isadoraair-updater.service"],
            },
        )
        repo = TrustedRepository(self.root / "declared-optional-unit.git", str(upstream), "main", CommandRunner())
        tip = repo.fetch()
        plan = derive_plan(repo, tip, r2, "r0003")
        self.assertEqual(plan.systemd_units_new_optional, ("isadoraair-updater.service",))

    def test_undeclared_nginx_and_runtime_authority_changes_are_rejected(self):
        for label, relative, expected in (
            ("nginx", "deploy/isadoraair.nginx", "nginx_changed"),
            ("runtime", "isadoraair/runtime_components.json", "runtime_components_changed"),
        ):
            _author, upstream, _bootstrap, r2, _r3 = create_release_repository(
                self.root / label,
                third_release_files={relative: "changed\n"},
            )
            repo = TrustedRepository(self.root / f"{label}.git", str(upstream), "main", CommandRunner())
            tip = repo.fetch()
            with self.assertRaisesRegex(ReleaseError, expected):
                derive_plan(repo, tip, r2, "r0003")


class StagingTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _author, upstream, _bootstrap, _r2, self.target = create_release_repository(self.root)
        self.repo = TrustedRepository(self.root / "trusted.git", str(upstream), "main", CommandRunner())
        self.repo.fetch()

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_tree_materializes_read_only(self):
        job = "123e4567-e89b-42d3-a456-426614174000"
        staged = materialize(self.repo, self.target, self.root / "staging", job)
        self.assertEqual((staged.source_root / "README").read_text(), "baseline\n")
        self.assertEqual((staged.source_root / "README").stat().st_mode & 0o222, 0)
        cleanup(self.root / "staging", job)
        self.assertFalse(staged.job_root.exists())

    def test_cleanup_rejects_non_uuid(self):
        with self.assertRaises(StagingError):
            cleanup(self.root / "staging", "../../etc")

    def test_links_in_target_are_rejected(self):
        author = self.root / "link-author"
        upstream = self.root / "link-upstream.git"
        author.mkdir()
        git(author, "init", "-b", "main")
        (author / "plain").write_text("x", encoding="utf-8")
        os.symlink("plain", author / "linked")
        git(author, "add", ".")
        git(author, "commit", "-m", "link")
        target = git(author, "rev-parse", "HEAD")
        import subprocess
        subprocess.run(["git", "init", "--bare", str(upstream)], check=True, stdout=subprocess.PIPE)
        git(author, "remote", "add", "origin", str(upstream))
        git(author, "push", "origin", "main")
        repo = TrustedRepository(self.root / "link-trusted.git", str(upstream), "main", CommandRunner())
        repo.fetch()
        with self.assertRaisesRegex(StagingError, "links and special"):
            materialize(repo, target, self.root / "link-stage", "123e4567-e89b-42d3-a456-426614174000")
