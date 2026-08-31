"""Manifest-vs-repository-reality cross-check tests -- [P0] 1.1 Phase A."""
from django.test import SimpleTestCase

from updatecenter import cross_check as cc, manifest as m
from .gitfixtures import FakeRepo
from .test_manifest import _valid_followup


class MigrationCrossCheckTests(SimpleTestCase):
    def test_missing_migration_flagged(self):
        with FakeRepo() as repo:
            sha = repo.rev_parse("HEAD")  # no migrations/ tree exists at all
            rel = m.validate_manifest_dict(_valid_followup(migrations_required=["library.0079_mediaplaybackincident"]))
            findings = cc.cross_check_release(rel, sha, repo.work)
            self.assertTrue(any(f.field == "migrations_required" for f in findings))

    def test_present_migration_not_flagged(self):
        with FakeRepo() as repo:
            repo.write("library/migrations/0079_mediaplaybackincident.py", "# migration\n")
            repo.write("requirements.txt", "Django==5.2.15\n")
            sha = repo.commit("add migration + requirements")
            rel = m.validate_manifest_dict(_valid_followup(
                migrations_required=["library.0079_mediaplaybackincident"],
                python_requirements_changed=False, requirements_sha256=None,
            ))
            findings = cc.cross_check_release(rel, sha, repo.work)
            self.assertEqual([f for f in findings if f.field == "migrations_required"], [])

    def test_unknown_target_commit_raises(self):
        with FakeRepo() as repo:
            rel = m.validate_manifest_dict(_valid_followup(migrations_required=[], migration_compatibility=None))
            with self.assertRaises(ValueError):
                cc.cross_check_release(rel, "a" * 40, repo.work)


class RequirementsCrossCheckTests(SimpleTestCase):
    def test_matching_hash_not_flagged(self):
        with FakeRepo() as repo:
            content = "Django==5.2.15\n"
            repo.write("requirements.txt", content)
            sha = repo.commit("requirements")
            actual_hash = m.sha256_hex(content.encode("utf-8"))
            rel = m.validate_manifest_dict(_valid_followup(
                migrations_required=[], migration_compatibility=None,
                python_requirements_changed=True, requirements_sha256=actual_hash,
            ))
            findings = cc.cross_check_release(rel, sha, repo.work)
            self.assertEqual(findings, [])

    def test_manifest_claims_unchanged_but_content_differs_is_not_itself_checked(self):
        """python_requirements_changed=False means the manifest asserts
        nothing changed at THIS release -- this cross-check only
        verifies the declared hash when python_requirements_changed is
        True (there is no hash to compare against when the manifest
        claims no change); a false "unchanged" claim that is actually
        wrong is a release-authoring error this layer cannot catch
        without a previous-release baseline, which is out of scope for
        Phase A's per-release cross-check."""
        with FakeRepo() as repo:
            repo.write("requirements.txt", "Django==5.2.15\nrequests==2.32.5\n")
            sha = repo.commit("requirements changed")
            rel = m.validate_manifest_dict(_valid_followup(
                migrations_required=[], migration_compatibility=None,
                python_requirements_changed=False, requirements_sha256=None,
            ))
            findings = cc.cross_check_release(rel, sha, repo.work)
            self.assertEqual(findings, [])

    def test_mismatched_hash_flagged(self):
        with FakeRepo() as repo:
            repo.write("requirements.txt", "Django==5.2.15\n")
            sha = repo.commit("requirements")
            rel = m.validate_manifest_dict(_valid_followup(
                migrations_required=[], migration_compatibility=None,
                python_requirements_changed=True, requirements_sha256="0" * 64,
            ))
            findings = cc.cross_check_release(rel, sha, repo.work)
            self.assertTrue(any(f.field == "requirements_sha256" for f in findings))


class ProtectedRuntimeIntentCrossCheckTests(SimpleTestCase):
    def test_runtime_change_requires_manual_bootstrap(self):
        with FakeRepo() as repo:
            previous = repo.rev_parse("HEAD")
            repo.write("deploy/updater_runtime/isadoraair_updater/runtime.py", "changed\n")
            target = repo.commit("protected runtime")
            rel = m.validate_manifest_dict(_no_migrations_no_requirements(
                release_id="r0006", previous_release_id="r0005",
            ))
            findings = cc.cross_check_release(
                rel, target, repo.work, previous_commit=previous,
            )
            self.assertTrue(any(f.field == "manual_bootstrap_required" for f in findings))

    def test_runtime_change_with_manual_bootstrap_is_accepted(self):
        with FakeRepo() as repo:
            previous = repo.rev_parse("HEAD")
            repo.write("deploy/updater_runtime/isadoraair_updater/runtime.py", "changed\n")
            target = repo.commit("protected runtime")
            rel = m.validate_manifest_dict(
                _no_migrations_no_requirements(
                    release_id="r0006", previous_release_id="r0005",
                    manual_bootstrap_required=True,
                )
            )
            findings = cc.cross_check_release(
                rel, target, repo.work, previous_commit=previous,
            )
            self.assertEqual(findings, [])

    def test_runtime_change_with_protected_candidate_is_accepted(self):
        with FakeRepo() as repo:
            previous = repo.rev_parse("HEAD")
            repo.write("deploy/updater_runtime/isadoraair_updater/runtime.py", "changed\n")
            target = repo.commit("signed protected runtime candidate")
            rel = m.validate_manifest_dict(
                _no_migrations_no_requirements(
                    release_id="r0027",
                    previous_release_id="r0026",
                    protected_runtime={
                        "generation": 2,
                        "descriptor_path": "deploy/updater_runtime/runtime-descriptor.json",
                        "descriptor_sha256": "a" * 64,
                        "minimum_bootstrap_protocol_version": 1,
                        "runtime_version": 5,
                        "manifest_protocol_version": 5,
                        "supported_wire_protocols": [3],
                        "attestations": [
                            "deploy/updater_attestations/runtime-descriptor.sig.json"
                        ],
                    },
                )
            )
            findings = cc.cross_check_release(
                rel, target, repo.work, previous_commit=previous,
            )
            self.assertEqual(findings, [])

    def test_ordinary_change_does_not_force_manual_bootstrap(self):
        with FakeRepo() as repo:
            previous = repo.rev_parse("HEAD")
            repo.write("ordinary.txt", "changed\n")
            target = repo.commit("ordinary change")
            rel = m.validate_manifest_dict(_no_migrations_no_requirements(
                release_id="r0006", previous_release_id="r0005",
            ))
            findings = cc.cross_check_release(
                rel, target, repo.work, previous_commit=previous,
            )
            self.assertEqual(findings, [])


def _no_migrations_no_requirements(**overrides):
    """`_valid_followup`'s own defaults declare a required migration
    AND a changed-requirements hash (see test_manifest.py) -- fine for
    manifest-shape tests, but noise for these systemd-only cross-check
    tests, which use a FakeRepo that never contains a matching
    migration file or requirements.txt. Cleared here so each test below
    checks exactly one thing."""
    return _valid_followup(
        migrations_required=[], migration_compatibility=None,
        python_requirements_changed=False, requirements_sha256=None,
        **overrides,
    )


class SystemdUnitCrossCheckTests(SimpleTestCase):
    def test_declared_unit_with_template_not_flagged(self):
        with FakeRepo() as repo:
            repo.write("deploy/isadoraair-engine.service", "[Unit]\n")
            sha = repo.commit("add unit")
            rel = m.validate_manifest_dict(_no_migrations_no_requirements(systemd_units_changed=["isadoraair-engine.service"]))
            findings = cc.cross_check_release(rel, sha, repo.work)
            self.assertEqual(findings, [])

    def test_declared_unit_without_template_flagged(self):
        with FakeRepo() as repo:
            sha = repo.rev_parse("HEAD")
            rel = m.validate_manifest_dict(_no_migrations_no_requirements(systemd_units_new_required=["isadoraair-newthing.service"]))
            findings = cc.cross_check_release(rel, sha, repo.work)
            self.assertTrue(any(f.field == "systemd_units" for f in findings))

    def test_removed_unit_still_present_flagged(self):
        with FakeRepo() as repo:
            repo.write("deploy/isadoraair-oldthing.service", "[Unit]\n")
            sha = repo.commit("add unit that should have been removed")
            rel = m.validate_manifest_dict(_no_migrations_no_requirements(systemd_units_removed_or_renamed=["isadoraair-oldthing.service"]))
            findings = cc.cross_check_release(rel, sha, repo.work)
            self.assertTrue(any(f.field == "systemd_units_removed_or_renamed" for f in findings))

    def test_removed_unit_correctly_absent_not_flagged(self):
        with FakeRepo() as repo:
            sha = repo.rev_parse("HEAD")
            rel = m.validate_manifest_dict(_no_migrations_no_requirements(systemd_units_removed_or_renamed=["isadoraair-oldthing.service"]))
            findings = cc.cross_check_release(rel, sha, repo.work)
            self.assertEqual(findings, [])
