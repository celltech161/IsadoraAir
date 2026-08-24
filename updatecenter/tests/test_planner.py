"""Planner integration tests -- [P0] 1.1 Phase A.

Uses Django's TestCase (real, migrated test database) because
planner._preview_current_graph_migrations/_applied_migration_set genuinely
need Django's MigrationLoader against a real connection. One
consequence worth being explicit about: `manage.py test` applies
EVERY migration (including this project's four schema-required-but-
not-yet-applied-in-production ones -- see BootstrapSchemaExpectationTests)
to the test database by default. That's fine for what most tests here
actually check -- the GRAPH structure (which migration depends on
which) is independent of what's applied. The SchemaDriftDetectionTests
class below specifically un-applies one real migration's bookkeeping
(via Django's own MigrationRecorder, not a raw DB edit) to exercise the
"something is actually pending" path deterministically. See individual
test docstrings for exactly what each one proves."""
import json
from pathlib import Path

from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.test import SimpleTestCase, TestCase

from updatecenter import manifest as m, planner, schema_health
from .gitfixtures import FakeRepo


def _write_manifest(releases_dir: Path, data: dict):
    releases_dir.mkdir(parents=True, exist_ok=True)
    (releases_dir / f"{data['release_id']}.json").write_text(json.dumps(data), encoding="utf-8")


def _bootstrap(bootstrap_commit, **overrides):
    data = {
        "schema_version": 1, "release_id": "r0001", "previous_release_id": None,
        "bootstrap_commit": bootstrap_commit, "minimum_updater_protocol_version": 1,
        "summary": "bootstrap", "migrations_required": [], "migration_compatibility": None,
        "python_requirements_changed": False, "requirements_sha256": None,
        "apt_packages_new": [], "systemd_units_changed": [], "systemd_units_new_required": [],
        "systemd_units_new_optional": [], "systemd_units_removed_or_renamed": [],
        "collectstatic_required": False, "services_requiring_restart": [],
        "nginx_changed": False, "runtime_components_changed": False,
    }
    data.update(overrides)
    return data


def _followup(release_id, previous_release_id, **overrides):
    data = {
        "schema_version": 1, "release_id": release_id, "previous_release_id": previous_release_id,
        "minimum_updater_protocol_version": 1, "summary": "followup",
        "migrations_required": [], "migration_compatibility": None,
        "python_requirements_changed": False, "requirements_sha256": None,
        "apt_packages_new": [], "systemd_units_changed": [], "systemd_units_new_required": [],
        "systemd_units_new_optional": [], "systemd_units_removed_or_renamed": [],
        "collectstatic_required": False, "services_requiring_restart": [],
        "nginx_changed": False, "runtime_components_changed": False,
    }
    data.update(overrides)
    return data


class SafetyGateTests(TestCase):
    def test_dirty_checkout_blocks_planning(self):
        with FakeRepo() as repo:
            releases_dir = repo.work / "deploy" / "releases"
            _write_manifest(releases_dir, _bootstrap(repo.rev_parse("HEAD")))
            repo.commit("add manifest")
            repo.dirty_untracked()
            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.DIRTY_CHECKOUT)

    def test_detached_head_blocks_planning(self):
        with FakeRepo() as repo:
            releases_dir = repo.work / "deploy" / "releases"
            _write_manifest(releases_dir, _bootstrap(repo.rev_parse("HEAD")))
            sha = repo.commit("add manifest")
            repo.checkout_detached(sha)
            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.DETACHED_HEAD)

    def test_no_origin_blocks_planning(self):
        import subprocess
        with FakeRepo() as repo:
            releases_dir = repo.work / "deploy" / "releases"
            _write_manifest(releases_dir, _bootstrap(repo.rev_parse("HEAD")))
            repo.commit("add manifest")
            subprocess.run(["git", "remote", "remove", "origin"], cwd=str(repo.work), check=True, capture_output=True)
            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.NO_ORIGIN_REMOTE)

    def test_diverged_from_origin_blocks_planning(self):
        with FakeRepo() as repo:
            releases_dir = repo.work / "deploy" / "releases"
            _write_manifest(releases_dir, _bootstrap(repo.rev_parse("HEAD")))
            repo.commit("add manifest")
            repo.diverge_origin()
            repo.write("local-only.txt", "x\n")
            repo.commit("local only", push=False)
            planner.fetch_updates(repo.work)
            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.DIVERGED_FROM_ORIGIN)

    def test_local_only_commit_blocks_authoritative_release_resolution(self):
        with FakeRepo() as repo:
            releases_dir = repo.work / "deploy" / "releases"
            _write_manifest(releases_dir, _bootstrap(repo.rev_parse("HEAD")))
            repo.commit("add manifest", push=True)
            repo.write("local-only.txt", "not pushed\n")
            repo.commit("local only", push=False)
            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(
                plan.safety_status, planner.SafetyStatus.LOCAL_COMMITS_NOT_ON_ORIGIN,
            )

    def test_invalid_manifest_blocks_planning(self):
        with FakeRepo() as repo:
            releases_dir = repo.work / "deploy" / "releases"
            releases_dir.mkdir(parents=True)
            (releases_dir / "r0001.json").write_text('{"not": "valid"}', encoding="utf-8")
            repo.commit("add bad manifest")
            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.INVALID_RELEASE_MANIFEST)

    def test_no_manifests_at_all_blocks_planning(self):
        with FakeRepo() as repo:
            releases_dir = repo.work / "deploy" / "releases"
            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.INVALID_RELEASE_MANIFEST)


class NoUpdateTests(TestCase):
    def test_up_to_date_when_head_is_bootstrap(self):
        with FakeRepo() as repo:
            releases_dir = repo.work / "deploy" / "releases"
            _write_manifest(releases_dir, _bootstrap(repo.rev_parse("HEAD")))
            repo.commit("add manifest", push=True)
            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.UP_TO_DATE)
            self.assertEqual(plan.installed_release_id, "r0001")
            self.assertEqual(plan.target_release_id, "r0001")


class ValidUpdateTests(TestCase):
    def _two_release_repo(self, followup_overrides=None, extra_files=None,
                          followup_release_id="r0002"):
        """Returns (repo, releases_dir, bootstrap_sha), with local HEAD
        already reset back to the bootstrap commit -- simulating "the
        station is currently on the bootstrap release; r0002 exists
        (was already pushed to origin) but has not been checked out
        yet," matching how a real station behind on releases actually
        looks (fetch brings the commit into the object database;
        nothing checks it out).

        `extra_files` (a {relative_path: content} dict) is written and
        committed IN THE SAME COMMIT as r0002's manifest -- this
        matters, not just for tidiness: cross_check.py reads a
        release's declared files (requirements.txt, migration files,
        systemd unit templates) AT THAT RELEASE'S OWN RESOLVED COMMIT
        (release_chain.resolve_release_commit, which for a non-
        bootstrap release is exactly "whichever commit introduced its
        manifest file"). A real release's manifest and the changes it
        describes land in git together, in one commit -- splitting
        them across two separate commits (as an earlier version of
        this fixture did) doesn't just look untidy, it produces a
        cross-check failure that has nothing to do with the thing
        actually under test, since the accompanying file genuinely
        would not exist yet at the manifest's own resolved commit."""
        repo = FakeRepo()
        releases_dir = repo.work / "deploy" / "releases"
        bootstrap_sha = repo.rev_parse("HEAD")
        _write_manifest(releases_dir, _bootstrap(bootstrap_sha))
        after_bootstrap_sha = repo.commit("add bootstrap manifest", push=True)

        overrides = followup_overrides or {}
        _write_manifest(releases_dir, _followup(followup_release_id, "r0001", **overrides))
        for relative_path, content in (extra_files or {}).items():
            repo.write(relative_path, content)
        repo.commit("add r0002" + (" + accompanying files" if extra_files else ""), push=True)
        repo.reset_local_to(after_bootstrap_sha)
        return repo, releases_dir, bootstrap_sha

    def test_one_step_update_ready_to_plan(self):
        repo, releases_dir, bootstrap_sha = self._two_release_repo()
        with repo:
            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.READY_TO_PLAN)
            self.assertEqual(plan.installed_release_id, "r0001")
            self.assertEqual(plan.target_release_id, "r0002")
            self.assertEqual(plan.releases_in_plan, ("r0002",))
            self.assertEqual(
                plan.target_schema_validation_status,
                planner.TargetSchemaValidationStatus.PENDING,
            )
            self.assertIn("target source", plan.target_schema_validation_detail.lower())

    def test_protected_runtime_change_without_manual_intent_is_rejected(self):
        repo, _releases_dir, _bootstrap_sha = self._two_release_repo(
            extra_files={
                "deploy/updater_runtime/isadoraair_updater/runtime.py": "changed\n",
            },
            followup_release_id="r0006",
        )
        with repo:
            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.CROSS_CHECK_FAILED)
            self.assertTrue(any(
                finding.field == "manual_bootstrap_required"
                for finding in plan.cross_check_findings
            ))

    def test_protected_runtime_change_with_manual_intent_is_manual(self):
        repo, _releases_dir, _bootstrap_sha = self._two_release_repo(
            {"manual_bootstrap_required": True},
            extra_files={
                "deploy/updater_runtime/isadoraair_updater/runtime.py": "changed\n",
            },
            followup_release_id="r0006",
        )
        with repo:
            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(
                plan.safety_status,
                planner.SafetyStatus.MANUAL_BOOTSTRAP_REQUIRED,
            )

    def test_r0006_ordinary_change_does_not_force_manual_bootstrap(self):
        repo, _releases_dir, _bootstrap_sha = self._two_release_repo(
            extra_files={"ordinary.txt": "changed\n"},
            followup_release_id="r0006",
        )
        with repo:
            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.READY_TO_PLAN)
            self.assertFalse(plan.manual_bootstrap_required)

    def test_requirements_changed_reflected(self):
        content = "Django==5.2.15\n"
        digest = m.sha256_hex(content.encode("utf-8"))
        repo, releases_dir, bootstrap_sha = self._two_release_repo(
            {"python_requirements_changed": True, "requirements_sha256": digest},
            extra_files={"requirements.txt": content},
        )
        with repo:
            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.READY_TO_PLAN)
            self.assertTrue(plan.python_requirements_changed)

    def test_manifest_requirements_contradiction_blocks_via_cross_check(self):
        """The manifest claims requirements changed with a specific
        hash; the actual requirements.txt at that commit hashes to
        something else -- planner must refuse (CROSS_CHECK_FAILED),
        never silently trust the manifest over reality."""
        repo, releases_dir, bootstrap_sha = self._two_release_repo(
            {"python_requirements_changed": True, "requirements_sha256": "0" * 64},
            extra_files={"requirements.txt": "Django==5.2.15\n"},  # real hash != "0"*64
        )
        with repo:
            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.CROSS_CHECK_FAILED)
            self.assertTrue(any(f.field == "requirements_sha256" for f in plan.cross_check_findings))

    def test_new_required_systemd_unit_surfaced(self):
        repo, releases_dir, bootstrap_sha = self._two_release_repo(
            {"systemd_units_new_required": ["isadoraair-newthing.service"]},
            extra_files={"deploy/isadoraair-newthing.service": "[Unit]\n"},
        )
        with repo:
            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.READY_TO_PLAN)
            self.assertIn("isadoraair-newthing.service", plan.systemd_units_new_required)

    def test_new_optional_systemd_unit_surfaced_but_not_required(self):
        repo, releases_dir, bootstrap_sha = self._two_release_repo(
            {"systemd_units_new_optional": ["wx-newalert.timer"]},
            extra_files={"deploy/wx-newalert.timer": "[Timer]\n"},
        )
        with repo:
            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.READY_TO_PLAN)
            self.assertIn("wx-newalert.timer", plan.systemd_units_new_optional)
            self.assertNotIn("wx-newalert.timer", plan.systemd_units_new_required)

    def test_apt_prerequisite_blocks_with_manual_status(self):
        repo, releases_dir, bootstrap_sha = self._two_release_repo({"apt_packages_new": ["ffmpeg"]})
        with repo:
            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.MANUAL_SYSTEM_PACKAGE_ACTION_REQUIRED)
            self.assertIn("ffmpeg", plan.apt_packages_new)

    def test_collectstatic_requirement_surfaced(self):
        repo, releases_dir, bootstrap_sha = self._two_release_repo({"collectstatic_required": True})
        with repo:
            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.READY_TO_PLAN)
            self.assertTrue(plan.collectstatic_required)

    def test_restart_services_ordered_per_restart_order(self):
        repo, releases_dir, bootstrap_sha = self._two_release_repo({
            "services_requiring_restart": ["isadoraair-rbds", "isadoraair-gunicorn", "isadoraair-engine"],
        })
        with repo:
            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(
                plan.services_requiring_restart,
                ("isadoraair-gunicorn", "isadoraair-engine", "isadoraair-rbds"),
            )

    def test_incompatible_migration_requires_manual_gate(self):
        repo, releases_dir, bootstrap_sha = self._two_release_repo(
            {"migrations_required": ["library.0079_mediaplaybackincident"], "migration_compatibility": "destructive"},
            extra_files={"library/migrations/0079_mediaplaybackincident.py": "# migration\n"},
        )
        with repo:
            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.MIGRATION_MANUAL_GATE_REQUIRED)

    def test_target_only_migration_is_declarative_until_target_source_is_loaded(self):
        """The target commit can introduce a migration module absent
        from this running checkout. Phase A must retain the manifest
        expectation without claiming the CURRENT MigrationLoader has
        validated the target graph or its dependency closure."""
        ref = "futureapp.0001_initial"
        repo, _releases_dir, _bootstrap_sha = self._two_release_repo(
            {"migrations_required": [ref], "migration_compatibility": "additive"},
            extra_files={"futureapp/migrations/0001_initial.py": "# target-only migration\n"},
        )
        with repo:
            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.READY_TO_PLAN)
            self.assertEqual(plan.migrations.explicitly_required, (ref,))
            self.assertEqual(plan.migrations.unknown_to_current_graph, (ref,))
            self.assertEqual(
                plan.target_schema_validation_status,
                planner.TargetSchemaValidationStatus.PENDING,
            )
            self.assertNotIn("validated", plan.target_schema_validation_detail.lower())


class MultiReleaseAggregateTests(TestCase):
    def test_skip_several_releases_aggregates_all(self):
        with FakeRepo() as repo:
            releases_dir = repo.work / "deploy" / "releases"
            _write_manifest(releases_dir, _bootstrap(repo.rev_parse("HEAD")))
            after_bootstrap_sha = repo.commit("bootstrap", push=True)

            _write_manifest(releases_dir, _followup("r0002", "r0001", services_requiring_restart=["isadoraair-engine"]))
            repo.commit("r0002", push=True)
            _write_manifest(releases_dir, _followup("r0003", "r0002", collectstatic_required=True))
            repo.commit("r0003", push=True)
            _write_manifest(releases_dir, _followup(
                "r0004", "r0003",
                migrations_required=["library.0079_mediaplaybackincident"], migration_compatibility="additive",
                services_requiring_restart=["isadoraair-gunicorn"],
            ))
            repo.write("library/migrations/0079_mediaplaybackincident.py", "# migration\n")
            repo.commit("r0004", push=True)

            # Simulate a station currently on r0001 (only) -- r0002-4
            # exist in history (already pushed to origin) but were
            # never checked out here, exactly like a real station
            # behind on releases after a fetch but no checkout.
            repo.reset_local_to(after_bootstrap_sha)

            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.READY_TO_PLAN)
            self.assertEqual(plan.releases_in_plan, ("r0002", "r0003", "r0004"))
            self.assertTrue(plan.collectstatic_required)  # from r0003
            self.assertEqual(plan.services_requiring_restart, ("isadoraair-gunicorn", "isadoraair-engine"))  # union of r0002+r0004, ordered
            self.assertIn(
                "library.0079_mediaplaybackincident",
                plan.migrations.expected_transition_unapplied + plan.migrations.already_applied,
            )

    def test_unresolvable_target_commit_fails_closed(self):
        """r0002's manifest file is committed, then deleted, then
        re-added with identical content -- all as clean, committed
        history (the working tree is clean throughout). This makes
        `git log --diff-filter=A` for deploy/releases/r0002.json
        return TWO commits instead of one, which
        release_chain.resolve_release_commit treats as unresolvable
        (see its own docstring: "added more than once... treat as
        commit identity unknown, never guess"). The chain is still
        structurally valid (build_chain succeeds -- it's a pure
        JSON-graph question), but planning must still fail closed
        (TARGET_COMMIT_UNKNOWN) rather than silently proceeding with
        an ambiguous target."""
        with FakeRepo() as repo:
            releases_dir = repo.work / "deploy" / "releases"
            _write_manifest(releases_dir, _bootstrap(repo.rev_parse("HEAD")))
            repo.commit("bootstrap", push=True)

            _write_manifest(releases_dir, _followup("r0002", "r0001"))
            repo.commit("add r0002 manifest", push=True)
            (releases_dir / "r0002.json").unlink()
            repo.commit("delete r0002 manifest", push=True)
            _write_manifest(releases_dir, _followup("r0002", "r0001"))
            repo.commit("re-add r0002 manifest with identical content", push=True)

            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.TARGET_COMMIT_UNKNOWN)

    def test_two_manifests_added_in_one_commit_fail_closed(self):
        with FakeRepo() as repo:
            releases_dir = repo.work / "deploy" / "releases"
            _write_manifest(releases_dir, _bootstrap(repo.rev_parse("HEAD")))
            after_bootstrap_sha = repo.commit("bootstrap", push=True)
            _write_manifest(releases_dir, _followup("r0002", "r0001"))
            _write_manifest(releases_dir, _followup("r0003", "r0002"))
            repo.commit("incorrectly add r0002 and r0003 together", push=True)
            repo.reset_local_to(after_bootstrap_sha)

            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.TARGET_COMMIT_UNKNOWN)
            self.assertIn("same commit", plan.safety_detail)

    def test_minimum_supported_release_blocks_too_old_station(self):
        with FakeRepo() as repo:
            releases_dir = repo.work / "deploy" / "releases"
            _write_manifest(releases_dir, _bootstrap(repo.rev_parse("HEAD")))
            after_bootstrap_sha = repo.commit("bootstrap", push=True)
            _write_manifest(releases_dir, _followup("r0002", "r0001"))
            repo.commit("r0002", push=True)
            _write_manifest(
                releases_dir,
                _followup("r0003", "r0002", minimum_supported_release_id="r0002"),
            )
            repo.commit("r0003", push=True)
            repo.reset_local_to(after_bootstrap_sha)

            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(
                plan.safety_status, planner.SafetyStatus.INSTALLED_RELEASE_TOO_OLD,
            )


class BootstrapSchemaExpectationTests(TestCase):
    """[P0] 1.1 CORRECTION -- replaces the old DeferredMigrationPolicyTests,
    which encoded a now-known-wrong invariant. Real incident: at
    5a0cb0e, WebRequestConfig's deployed model state already declared
    dedication_tts_voice/dedication_tts_timeout_seconds, but
    webrequests.0008 (which creates those columns) was left
    deliberately unapplied on the theory that the FEATURE using them
    hadn't cut over -- Django doesn't care about feature cutover, it
    selects every column the model class declares, and production broke
    within one restart cycle ("column ...dedication_tts_voice_id does
    not exist"). See docs/UPDATE_CENTER.md's "Schema vs. feature
    activation" section for the corrected principle: SCHEMA required
    by deployed model state may not be deferred; only FEATURE
    ACTIVATION may be."""

    REAL_BOOTSTRAP_MANIFEST_PATH = Path(__file__).resolve().parents[2] / "deploy" / "releases" / "r0001.json"

    def test_real_bootstrap_manifest_declares_the_four_schema_required_migrations(self):
        """Independently re-derived from source in this same
        correction pass (not copied from the incident report): each of
        these four either adds a field to an actively-`.load()`-ed
        singleton config model (webrequests, road_conditions) or is
        reachable via Django admin registration (weather's
        WeatherVoicePersona, tts's StationTTSVoice/PiperVoiceModel) --
        see this correction's own report for the exact evidence per
        migration. tts.0001_initial is additionally required
        structurally: it's a Django migration-graph dependency of all
        three others."""
        data = json.loads(self.REAL_BOOTSTRAP_MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            set(data["migrations_required"]),
            {
                "tts.0001_initial",
                "road_conditions.0010_roadconditionsconfiguration_tts",
                "weather.0007_weathervoicepersona",
                "webrequests.0008_webrequestconfig_dedication_tts",
            },
        )

    def test_real_bootstrap_manifest_declares_additive_compatibility(self):
        data = json.loads(self.REAL_BOOTSTRAP_MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["migration_compatibility"], "additive")

    def test_real_bootstrap_manifest_summary_distinguishes_schema_from_feature_cutover(self):
        """Loose prose check on purpose (not brittle) -- confirms the
        summary documents BOTH halves of the corrected distinction:
        schema is present/required, but feature behavior is not cut
        over. The exact wording is free to evolve; the distinction
        itself must remain visible to a human reading the manifest."""
        data = json.loads(self.REAL_BOOTSTRAP_MANIFEST_PATH.read_text(encoding="utf-8"))
        summary = data["summary"].lower()
        self.assertIn("schema", summary)
        self.assertIn("cut over", summary)

    def test_real_bootstrap_manifest_passes_the_validator(self):
        """End-to-end: the actual shipped file validates cleanly against
        the actual shipped source at its own bootstrap_commit -- not
        just individually well-formed, but cross-checked against real
        git content (each of the four migration files genuinely exists
        at 5a0cb0e)."""
        import io
        from django.core.management import call_command
        out = io.StringIO()
        call_command("validate_release_manifests", stdout=out, stderr=out)  # raises SystemExit(1) on failure

    def test_r0001_to_r0002_transition_excludes_r0001_baseline_migrations(self):
        """r0001's four refs describe healthy state on entry to the
        bootstrap anchor. Updating an already-healthy r0001 station to
        Phase A must include only r0002's transition migration."""
        with FakeRepo() as repo:
            releases_dir = repo.work / "deploy" / "releases"
            _write_manifest(
                releases_dir,
                _bootstrap(
                    repo.rev_parse("HEAD"),
                    migrations_required=[
                        "tts.0001_initial",
                        "road_conditions.0010_roadconditionsconfiguration_tts",
                        "weather.0007_weathervoicepersona",
                        "webrequests.0008_webrequestconfig_dedication_tts",
                    ],
                    migration_compatibility="additive",
                ),
            )
            installed_sha = repo.commit("r0001", push=True)
            _write_manifest(
                releases_dir,
                _followup(
                    "r0002", "r0001",
                    migrations_required=["updatecenter.0001_initial"],
                    migration_compatibility="additive",
                    services_requiring_restart=["isadoraair-gunicorn"],
                    minimum_supported_release_id="r0001",
                ),
            )
            repo.write("updatecenter/migrations/0001_initial.py", "# phase A migration\n")
            repo.commit("r0002", push=True)
            repo.reset_local_to(installed_sha)

            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.READY_TO_PLAN)
            self.assertEqual(plan.releases_in_plan, ("r0002",))
            self.assertEqual(plan.migrations.explicitly_required, ("updatecenter.0001_initial",))
            for baseline_ref in (
                "tts.0001_initial",
                "road_conditions.0010_roadconditionsconfiguration_tts",
                "weather.0007_weathervoicepersona",
                "webrequests.0008_webrequestconfig_dedication_tts",
            ):
                self.assertNotIn(baseline_ref, plan.migrations.explicitly_required)


class R0006ReleaseContractTests(SimpleTestCase):
    ROOT = Path(__file__).resolve().parents[2]

    def test_manifest_requires_only_gunicorn_restart(self):
        data = json.loads(
            (self.ROOT / "deploy" / "releases" / "r0006.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            data["services_requiring_restart"],
            ["isadoraair-gunicorn"],
        )

    def test_bridge_runs_checkout_git_and_materialization_as_application_user(self):
        runbook = (self.ROOT / "docs" / "UPDATE_CENTER.md").read_text(encoding="utf-8")
        bridge = runbook.split(
            "### Historical exact r0005 to r0006 manual production bridge", 1,
        )[1]
        self.assertNotRegex(bridge, r"(?m)^\s*git -C")
        self.assertIn('sudo -u "$ISA_USER" git -C "$ISA_ROOT" fetch origin main', bridge)
        self.assertIn('sudo -u "$ISA_USER" mktemp -d', bridge)
        self.assertIn('| sudo -u "$ISA_USER" tar -x', bridge)
        self.assertNotIn("git config", bridge)

    def test_bridge_restarts_only_gunicorn_after_source_advancement(self):
        runbook = (self.ROOT / "docs" / "UPDATE_CENTER.md").read_text(encoding="utf-8")
        bridge = runbook.split(
            "### Historical exact r0005 to r0006 manual production bridge", 1,
        )[1]
        source_checkpoint = bridge.split("8. Fast-forward", 1)[1]
        self.assertIn("sudo systemctl restart isadoraair-gunicorn.service", source_checkpoint)
        self.assertIn("http://127.0.0.1:8000/healthz/", source_checkpoint)
        self.assertIn("systemctl show isadoraair-gunicorn.service", source_checkpoint)
        for forbidden in (
            "restart isadoraair-engine.service",
            "restart isadoraair-monitoring.service",
            "restart isadoraair-encoders.service",
            "restart isadoraair-rbds.service",
        ):
            self.assertNotIn(forbidden, source_checkpoint)


class MigrationAggregationTests(TestCase):
    """Planner aggregation-logic tests, independent of the test
    database's applied-migration state (see this module's own top
    docstring)."""

    def test_planner_never_aggregates_beyond_explicitly_declared_migrations(self):
        """A release declaring only library.0079 as required must never
        pull in unrelated apps' unapplied migrations (this project
        currently has real ones: tts/road_conditions/weather/
        webrequests) merely because they also happen to be unapplied
        somewhere. Checked against current_graph_dependency_preview (a
        CURRENT-graph preview, not target validation) -- library.0079 has no
        dependency edge to any of these four, so none of them should
        ever appear."""
        with FakeRepo() as repo:
            releases_dir = repo.work / "deploy" / "releases"
            _write_manifest(releases_dir, _bootstrap(repo.rev_parse("HEAD")))
            after_bootstrap_sha = repo.commit("bootstrap", push=True)
            _write_manifest(releases_dir, _followup(
                "r0002", "r0001",
                migrations_required=["library.0079_mediaplaybackincident"], migration_compatibility="additive",
            ))
            repo.write("library/migrations/0079_mediaplaybackincident.py", "# migration\n")
            repo.commit("r0002", push=True)
            repo.reset_local_to(after_bootstrap_sha)

            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.READY_TO_PLAN)
            all_included = (
                plan.migrations.explicitly_required
                + plan.migrations.current_graph_dependency_preview
            )
            for forbidden in ("tts.0001_initial", "road_conditions.0010_roadconditionsconfiguration_tts",
                               "weather.0007_weathervoicepersona", "webrequests.0008_webrequestconfig_dedication_tts"):
                self.assertNotIn(forbidden, all_included)

    def test_explicit_future_requirement_pulls_in_real_django_dependency_closure(self):
        """road_conditions.0010_roadconditionsconfiguration_tts really
        depends on tts.0001_initial in this actual project (see that
        migration file's own `dependencies` list) -- requiring the
        former must surface the latter as an explicit member of the
        dependency closure, proving Django's real graph resolution is
        actually being consulted, not bypassed."""
        with FakeRepo() as repo:
            releases_dir = repo.work / "deploy" / "releases"
            _write_manifest(releases_dir, _bootstrap(repo.rev_parse("HEAD")))
            after_bootstrap_sha = repo.commit("bootstrap", push=True)
            _write_manifest(releases_dir, _followup(
                "r0002", "r0001",
                migrations_required=["road_conditions.0010_roadconditionsconfiguration_tts"],
                migration_compatibility="additive",
            ))
            repo.write("road_conditions/migrations/0010_roadconditionsconfiguration_tts.py", "# migration\n")
            repo.commit("r0002", push=True)
            repo.reset_local_to(after_bootstrap_sha)

            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.READY_TO_PLAN)
            self.assertIn("road_conditions.0010_roadconditionsconfiguration_tts", plan.migrations.explicitly_required)
            self.assertIn("tts.0001_initial", plan.migrations.current_graph_dependency_preview)

    def test_unrelated_migration_never_marked_pending_for_an_unrelated_plan(self):
        """A plan for release r0002 (which only requires library.0079)
        must not list weather.0007 as pending FOR THAT PLAN, even
        even if a future target source contained weather.0007 -- checked
        against explicitly_required + current_graph_dependency_preview
        (the only two sources
        expected_transition_unapplied is ever drawn from). This is about
        aggregation correctness for one specific plan, NOT a claim that
        weather.0007 is safe to leave unapplied in general -- that
        broader question is schema_health.py's job (see
        SchemaDriftDetectionTests below), not this one."""
        with FakeRepo() as repo:
            releases_dir = repo.work / "deploy" / "releases"
            _write_manifest(releases_dir, _bootstrap(repo.rev_parse("HEAD")))
            after_bootstrap_sha = repo.commit("bootstrap", push=True)
            _write_manifest(releases_dir, _followup(
                "r0002", "r0001",
                migrations_required=["library.0079_mediaplaybackincident"], migration_compatibility="additive",
            ))
            repo.write("library/migrations/0079_mediaplaybackincident.py", "# migration\n")
            repo.commit("r0002", push=True)
            repo.reset_local_to(after_bootstrap_sha)

            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.READY_TO_PLAN)
            all_in_plan = (
                set(plan.migrations.explicitly_required)
                | set(plan.migrations.current_graph_dependency_preview)
            )
            self.assertNotIn("weather.0007_weathervoicepersona", all_in_plan)


class SchemaDriftDetectionTests(TestCase):
    """[P0] 1.1 CORRECTION -- the real product guarantee this whole
    correction pass exists to add. Marks one REAL migration
    (webrequests.0008, the exact migration from the actual production
    incident) as unapplied via Django's own MigrationRecorder --
    bookkeeping only, no raw SQL, no dependence on any DB error string
    -- and proves build_plan() correctly refuses to call the station
    healthy when a release manifest doesn't account for it, and
    blocks regardless of whether a manifest declares it. A manifest
    describes the release transition; it cannot make CURRENT schema
    healthy."""

    MIGRATION = ("webrequests", "0008_webrequestconfig_dedication_tts")

    def _mark_unapplied(self):
        recorder = MigrationRecorder(connection)
        recorder.record_unapplied(*self.MIGRATION)
        return recorder

    def _mark_applied(self, recorder):
        recorder.record_applied(*self.MIGRATION)

    def test_unexpected_pending_migration_blocks_up_to_date(self):
        """The exact shape of the real incident: a migration is
        genuinely pending against the loaded model state, but NO
        release manifest up to the installed release declares it --
        this must never read as UP_TO_DATE/healthy."""
        recorder = self._mark_unapplied()
        try:
            with FakeRepo() as repo:
                releases_dir = repo.work / "deploy" / "releases"
                _write_manifest(releases_dir, _bootstrap(repo.rev_parse("HEAD")))  # migrations_required=[] by fixture default
                repo.commit("bootstrap", push=True)

                plan = planner.build_plan(repo.work, "deploy/releases")
                self.assertEqual(plan.safety_status, planner.SafetyStatus.SCHEMA_DRIFT_DETECTED)
                self.assertIn("webrequests.0008_webrequestconfig_dedication_tts", plan.schema_pending_migrations)
                self.assertEqual(plan.schema_health_status, schema_health.SchemaHealthStatus.UNAPPLIED_MIGRATIONS_DETECTED)
        finally:
            self._mark_applied(recorder)

    def test_manifest_declared_pending_migration_still_blocks_current_schema(self):
        """Same real migration marked unapplied, but this time the
        bootstrap manifest declares it required. CURRENT schema is
        still unhealthy: a manifest cannot mask the live ORM/DB gap."""
        recorder = self._mark_unapplied()
        try:
            with FakeRepo() as repo:
                releases_dir = repo.work / "deploy" / "releases"
                _write_manifest(releases_dir, _bootstrap(
                    repo.rev_parse("HEAD"),
                    migrations_required=["webrequests.0008_webrequestconfig_dedication_tts"],
                    migration_compatibility="additive",
                ))
                repo.commit("bootstrap", push=True)

                plan = planner.build_plan(repo.work, "deploy/releases")
                self.assertEqual(plan.safety_status, planner.SafetyStatus.SCHEMA_DRIFT_DETECTED)
                self.assertIn(
                    "webrequests.0008_webrequestconfig_dedication_tts",
                    plan.schema_pending_migrations,
                )
        finally:
            self._mark_applied(recorder)

    def test_drift_blocks_even_when_a_newer_release_is_available(self):
        """Drift on the INSTALLED release must block planning
        regardless of whether a newer release also exists -- an
        operator must resolve the existing gap before layering a new
        update on top of it, not have the update planner silently step
        over it."""
        recorder = self._mark_unapplied()
        try:
            with FakeRepo() as repo:
                releases_dir = repo.work / "deploy" / "releases"
                _write_manifest(releases_dir, _bootstrap(repo.rev_parse("HEAD")))
                after_bootstrap_sha = repo.commit("bootstrap", push=True)
                _write_manifest(releases_dir, _followup("r0002", "r0001"))
                repo.commit("r0002", push=True)
                repo.reset_local_to(after_bootstrap_sha)

                plan = planner.build_plan(repo.work, "deploy/releases")
                self.assertEqual(plan.safety_status, planner.SafetyStatus.SCHEMA_DRIFT_DETECTED)
                self.assertIsNone(plan.target_release_id)  # refused before even considering r0002
        finally:
            self._mark_applied(recorder)

    def test_schema_healthy_when_nothing_pending(self):
        """Baseline/control for the two tests above -- confirms
        READY/UP_TO_DATE states report SCHEMA_CURRENT when the test
        database is (as normal) fully migrated."""
        with FakeRepo() as repo:
            releases_dir = repo.work / "deploy" / "releases"
            _write_manifest(releases_dir, _bootstrap(repo.rev_parse("HEAD")))
            repo.commit("bootstrap", push=True)
            plan = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(plan.safety_status, planner.SafetyStatus.UP_TO_DATE)
            self.assertEqual(plan.schema_health_status, schema_health.SchemaHealthStatus.SCHEMA_CURRENT)
            self.assertEqual(plan.schema_pending_migrations, ())
            self.assertEqual(
                plan.target_schema_validation_status,
                planner.TargetSchemaValidationStatus.NOT_APPLICABLE,
            )


class FeatureActivationRemainsSeparateFromSchemaTests(TestCase):
    """[P0] 1.1 correction §4/§8 item 7: applying additive schema must
    not itself activate any feature -- proven directly against the
    real models involved in the production incident, not asserted in
    the abstract."""

    def test_webrequestconfig_dedication_tts_voice_defaults_to_null(self):
        from webrequests.models import WebRequestConfig
        field = WebRequestConfig._meta.get_field("dedication_tts_voice")
        self.assertTrue(field.null)
        config = WebRequestConfig()
        self.assertIsNone(config.dedication_tts_voice_id)

    def test_roadconditionsconfiguration_tts_voice_defaults_to_null(self):
        from road_conditions.models import RoadConditionsConfiguration
        field = RoadConditionsConfiguration._meta.get_field("tts_voice")
        self.assertTrue(field.null)
        self.assertFalse(RoadConditionsConfiguration._meta.get_field("tts_use_weather_schedule").default)

    def test_weathervoicepersona_table_can_exist_with_zero_rows(self):
        """A new table existing (schema applied) is not itself a claim
        that anything populates or reads it yet."""
        from weather.models import WeatherVoicePersona
        self.assertEqual(WeatherVoicePersona.objects.count(), 0)
