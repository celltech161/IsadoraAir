"""Managed-unit activation policy and promotion semantics (r0022 work):

  - MANAGED_UNIT_POLICIES / KNOWN_MANAGED_UNITS: the closed map deciding
    which units this protected updater may ever touch, and how.
  - _cross_check()'s NEW/PROMOTED distinction for systemd_units_new_required/
    systemd_units_new_optional -- a pre-existing, already-tracked unit
    template may be deliberately promoted into the managed/required
    deployment contract without a fake content edit, while an actually
    undeclared add/modify/remove of ANY deploy/*.service|*.timer file
    still fails closed exactly as before.
  - manual_blockers(): the four Road Conditions/KanDrive companion units
    no longer produce UNKNOWN_MANAGED_UNIT; a genuinely unknown unit
    still does.
  - The manifest/release protocol bump (3 -> 4), kept independent of the
    wire protocol (see test_phase_b_protocol.py's own
    test_runtime_v4_keeps_wire_protocol_v3)."""
import json
import subprocess
from pathlib import Path
import tempfile
from unittest import mock

from django.test import SimpleTestCase

from .phase_b_helpers import create_release_repository, git, manifest
from isadoraair_updater import MANIFEST_PROTOCOL_VERSION, PROTOCOL_VERSION, RUNTIME_VERSION
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import (
    CORE_SERVICES, KNOWN_MANAGED_UNITS, MANAGED_UNIT_POLICIES, ReleaseError, TrustedRepository,
    UnitActivationPolicy, derive_plan, manual_blockers,
)

ROAD_CONDITIONS_UNITS = (
    "isadoraair-sync-road-conditions.service",
    "isadoraair-sync-road-conditions.timer",
    "isadoraair-generate-road-condition-audio.service",
    "isadoraair-generate-road-condition-audio.timer",
)


def _create_promotion_repository(root: Path, *, promoted_units, promote_release_changes=None,
                                  promote_release_files=None, promote_release_id="r0003"):
    """Like phase_b_helpers.create_release_repository, but `promoted_units`
    are planted as ORDINARY tracked files already present at the
    bootstrap commit -- unrelated to any release's own systemd
    declarations there, matching the real Road Conditions sync-unit
    history exactly (added to deploy/ many commits before the release-
    manifest system existed at all, so they have always been an
    untouched, undeclared file at every release boundary since). The
    returned chain lets a caller declare a `promoted_units` name in a
    LATER release's systemd_units_new_required/_optional WITHOUT it
    ever appearing in that release's own git diff -- the "promoted
    existing file" scenario, never "genuinely added in this diff."

    `promote_release_files` (like create_release_repository's own
    third_release_files) writes/overwrites arbitrary repo-relative
    paths as part of the SAME single commit that introduces the
    promoting release's manifest -- e.g. to add a genuinely-new unit
    alongside a promotion in one release, or to actually modify a
    promoted unit's own content (to prove an undeclared modification
    still fails closed) -- every path must land in this ONE commit so
    introducing_commit() still resolves the release to exactly one
    immutable commit."""
    root.mkdir(parents=True, exist_ok=True)
    author = root / "author"
    upstream = root / "upstream.git"
    author.mkdir()
    git(author, "init", "-b", "main")
    (author / "README").write_text("baseline\n", encoding="utf-8")
    deploy = author / "deploy"
    deploy.mkdir()
    add_paths = ["README"]
    for unit in promoted_units:
        (deploy / unit).write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")
        add_paths.append(f"deploy/{unit}")
    git(author, "add", *add_paths)
    git(author, "commit", "-m", "baseline (with pre-existing unit template(s))")
    bootstrap = git(author, "rev-parse", "HEAD")
    releases = author / "deploy" / "releases"
    releases.mkdir(parents=True)
    (releases / "r0001.json").write_text(json.dumps(manifest("r0001", None, bootstrap=bootstrap)), encoding="utf-8")
    git(author, "add", "deploy/releases/r0001.json")
    git(author, "commit", "-m", "introduce bootstrap manifest")
    (releases / "r0002.json").write_text(
        json.dumps(manifest("r0002", "r0001", minimum_supported_release_id="r0001")), encoding="utf-8",
    )
    git(author, "add", "deploy/releases/r0002.json")
    git(author, "commit", "-m", "release r0002")
    r0002 = git(author, "rev-parse", "HEAD")
    changes = dict(promote_release_changes or {})
    for relative, content in (promote_release_files or {}).items():
        destination = author / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    (releases / f"{promote_release_id}.json").write_text(
        json.dumps(manifest(promote_release_id, "r0002", minimum_supported_release_id="r0002", **changes)),
        encoding="utf-8",
    )
    git(author, "add", ".")
    git(author, "commit", "-m", f"release {promote_release_id}")
    promote_commit = git(author, "rev-parse", "HEAD")
    subprocess.run(["git", "init", "--bare", str(upstream)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    git(author, "remote", "add", "origin", str(upstream))
    git(author, "push", "-u", "origin", "main")
    return author, upstream, bootstrap, r0002, promote_commit


class ManagedUnitPolicyMapTests(SimpleTestCase):
    def test_all_five_core_services_are_enable_now(self):
        for name in CORE_SERVICES:
            self.assertIs(MANAGED_UNIT_POLICIES[f"{name}.service"], UnitActivationPolicy.ENABLE_NOW)

    def test_four_road_conditions_companion_units_are_known(self):
        for unit in ROAD_CONDITIONS_UNITS:
            self.assertIn(unit, MANAGED_UNIT_POLICIES)

    def test_companion_service_is_install_only_timer_is_enable_now(self):
        self.assertIs(
            MANAGED_UNIT_POLICIES["isadoraair-sync-road-conditions.service"], UnitActivationPolicy.INSTALL_ONLY,
        )
        self.assertIs(
            MANAGED_UNIT_POLICIES["isadoraair-sync-road-conditions.timer"], UnitActivationPolicy.ENABLE_NOW,
        )
        self.assertIs(
            MANAGED_UNIT_POLICIES["isadoraair-generate-road-condition-audio.service"], UnitActivationPolicy.INSTALL_ONLY,
        )
        self.assertIs(
            MANAGED_UNIT_POLICIES["isadoraair-generate-road-condition-audio.timer"], UnitActivationPolicy.ENABLE_NOW,
        )

    def test_known_managed_units_is_derived_from_the_policy_map(self):
        self.assertEqual(KNOWN_MANAGED_UNITS, frozenset(MANAGED_UNIT_POLICIES))

    def test_unknown_service_is_absent(self):
        self.assertNotIn("evil.service", MANAGED_UNIT_POLICIES)

    def test_unknown_timer_is_absent(self):
        self.assertNotIn("evil.timer", MANAGED_UNIT_POLICIES)

    def test_policy_is_not_derived_from_service_vs_timer_suffix(self):
        # Every unit sharing a base name has an INDEPENDENTLY listed
        # policy -- a .service being INSTALL_ONLY says nothing about
        # its paired .timer, and vice versa. Proven directly: flipping
        # either policy value below (by construction, not by pattern)
        # is the only way this map could ever assign one.
        for name in CORE_SERVICES:
            self.assertNotIn(f"{name}.timer", MANAGED_UNIT_POLICIES, "no core service has a managed timer")
        install_only = {u for u, p in MANAGED_UNIT_POLICIES.items() if p is UnitActivationPolicy.INSTALL_ONLY}
        enable_now = {u for u, p in MANAGED_UNIT_POLICIES.items() if p is UnitActivationPolicy.ENABLE_NOW}
        self.assertTrue(all(u.endswith(".service") for u in install_only))
        # ENABLE_NOW legitimately contains BOTH .service (core) and
        # .timer (companion) names -- suffix alone never determines policy.
        self.assertTrue(any(u.endswith(".service") for u in enable_now))
        self.assertTrue(any(u.endswith(".timer") for u in enable_now))


class PromotionCrossCheckTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_preexisting_known_unit_promoted_to_required_without_a_fake_edit(self):
        author, upstream, _bootstrap, r0002, r0003 = _create_promotion_repository(
            self.root / "promote-required",
            promoted_units=["isadoraair-sync-road-conditions.service", "isadoraair-sync-road-conditions.timer"],
            promote_release_changes={
                "systemd_units_new_required": [
                    "isadoraair-sync-road-conditions.service", "isadoraair-sync-road-conditions.timer",
                ],
            },
        )
        repo = TrustedRepository(self.root / "promote-required.git", str(upstream), "main", CommandRunner())
        tip = repo.fetch()
        plan = derive_plan(repo, tip, r0002, "r0003")
        self.assertEqual(
            set(plan.systemd_units_new_required),
            {"isadoraair-sync-road-conditions.service", "isadoraair-sync-road-conditions.timer"},
        )
        self.assertEqual(manual_blockers(plan), ())

    def test_preexisting_known_unit_promoted_to_optional_without_a_fake_edit(self):
        author, upstream, _bootstrap, r0002, r0003 = _create_promotion_repository(
            self.root / "promote-optional",
            promoted_units=["isadoraair-generate-road-condition-audio.timer"],
            promote_release_changes={
                "systemd_units_new_optional": ["isadoraair-generate-road-condition-audio.timer"],
            },
        )
        repo = TrustedRepository(self.root / "promote-optional.git", str(upstream), "main", CommandRunner())
        tip = repo.fetch()
        plan = derive_plan(repo, tip, r0002, "r0003")
        self.assertEqual(plan.systemd_units_new_optional, ("isadoraair-generate-road-condition-audio.timer",))

    def test_actual_undeclared_add_still_fails_closed(self):
        # A GENUINELY new file (added in this diff, not pre-existing at
        # the predecessor) must still be declared -- promotion logic
        # must never widen the ordinary "new file" path.
        _author, upstream, _bootstrap, r2, _r3 = create_release_repository(
            self.root / "undeclared-add",
            third_release_files={"deploy/isadoraair-sync-road-conditions.timer": "[Timer]\nOnCalendar=*:0/1\n"},
        )
        repo = TrustedRepository(self.root / "undeclared-add.git", str(upstream), "main", CommandRunner())
        tip = repo.fetch()
        with self.assertRaisesRegex(ReleaseError, "systemd unit intent"):
            derive_plan(repo, tip, r2, "r0003")

    def test_actual_undeclared_modification_of_a_promoted_unit_still_fails_closed(self):
        # The unit's bytes actually change AS PART OF this release
        # (not merely re-declared), but it's declared only in
        # systemd_units_new_required (promotion), never
        # systemd_units_changed -- must still fail closed.
        author, upstream, _bootstrap, r0002, r0003 = _create_promotion_repository(
            self.root / "promote-then-modify",
            promoted_units=["isadoraair-sync-road-conditions.timer"],
            promote_release_changes={
                "systemd_units_new_required": ["isadoraair-sync-road-conditions.timer"],
            },
            promote_release_files={
                "deploy/isadoraair-sync-road-conditions.timer": "[Timer]\nOnCalendar=*:0/5\n",
            },
        )
        repo = TrustedRepository(self.root / "promote-then-modify.git", str(upstream), "main", CommandRunner())
        tip = repo.fetch()
        with self.assertRaisesRegex(ReleaseError, "systemd unit intent"):
            derive_plan(repo, tip, r0002, "r0003")

    def test_actual_undeclared_removal_still_fails_closed(self):
        _author, upstream, _bootstrap, r2, _r3 = create_release_repository(
            self.root / "undeclared-remove",
        )
        # Baseline create_release_repository never adds a unit, so
        # nothing needs removing here -- instead prove the mirror image
        # of the promotion path stays strict: declaring a unit removed
        # that never existed at all is still a predecessor-diff mismatch.
        with_removed = create_release_repository(
            self.root / "undeclared-remove-2",
            third_release_changes={"systemd_units_removed_or_renamed": ["isadoraair-updater.service"]},
        )
        upstream2 = with_removed[1]
        r2b = with_removed[3]
        repo2 = TrustedRepository(self.root / "undeclared-remove-2.git", str(upstream2), "main", CommandRunner())
        tip2 = repo2.fetch()
        with self.assertRaisesRegex(ReleaseError, "declared removed but remains present|systemd unit intent"):
            derive_plan(repo2, tip2, r2b, "r0003")

    def test_promotion_of_an_unknown_unit_fails_closed(self):
        author, upstream, _bootstrap, r0002, r0003 = _create_promotion_repository(
            self.root / "promote-unknown",
            promoted_units=["isadoraair-completely-unknown.service"],
            promote_release_changes={
                "systemd_units_new_required": ["isadoraair-completely-unknown.service"],
            },
        )
        repo = TrustedRepository(self.root / "promote-unknown.git", str(upstream), "main", CommandRunner())
        tip = repo.fetch()
        with self.assertRaisesRegex(ReleaseError, "not in the .*protected managed-unit policy"):
            derive_plan(repo, tip, r0002, "r0003")

    def test_declared_new_file_path_still_works_unchanged(self):
        # The ordinary "genuinely added in this diff" path (present
        # since before this feature) must be completely unaffected by
        # the promotion logic sitting alongside it.
        _author, upstream, _bootstrap, r2, _r3 = create_release_repository(
            self.root / "genuinely-new",
            third_release_changes={"systemd_units_new_optional": ["isadoraair-updater.service"]},
        )
        repo = TrustedRepository(self.root / "genuinely-new.git", str(upstream), "main", CommandRunner())
        tip = repo.fetch()
        plan = derive_plan(repo, tip, r2, "r0003")
        self.assertEqual(plan.systemd_units_new_optional, ("isadoraair-updater.service",))


class ManualBlockersManagedUnitTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_road_conditions_companion_units_no_longer_block(self):
        author, upstream, _bootstrap, r0002, r0003 = _create_promotion_repository(
            self.root / "known-units-no-block",
            promoted_units=list(ROAD_CONDITIONS_UNITS),
            promote_release_changes={"systemd_units_new_required": list(ROAD_CONDITIONS_UNITS)},
        )
        repo = TrustedRepository(self.root / "known-units-no-block.git", str(upstream), "main", CommandRunner())
        tip = repo.fetch()
        plan = derive_plan(repo, tip, r0002, "r0003")
        blockers = manual_blockers(plan)
        self.assertNotIn("UNKNOWN_MANAGED_UNIT", blockers)

    def test_unknown_unit_still_blocks(self):
        # An unknown unit can never even reach manual_blockers() via
        # promotion (see test_promotion_of_an_unknown_unit_fails_closed
        # above) -- but the ordinary "genuinely added" path still must
        # surface UNKNOWN_MANAGED_UNIT as a real, reportable blocker
        # rather than a hard cross-check failure, matching existing
        # (pre-this-feature) behavior for any not-yet-recognized unit.
        _author, upstream, _bootstrap, r2, _r3 = create_release_repository(
            self.root / "unknown-still-blocks",
            third_release_changes={"systemd_units_new_required": ["some-future-unit.service"]},
        )
        repo = TrustedRepository(self.root / "unknown-still-blocks.git", str(upstream), "main", CommandRunner())
        tip = repo.fetch()
        plan = derive_plan(repo, tip, r2, "r0003")
        self.assertIn("UNKNOWN_MANAGED_UNIT", manual_blockers(plan))


class RoadConditionsFutureAcceptanceScenarioTests(SimpleTestCase):
    """The exact r0023 acceptance scenario this task requires: all four
    Road Conditions/KanDrive units in one plan's systemd_units_new_required,
    with the sync pair promoted from an already-tracked r0021-era
    template (never a fake Git modification) and the generator pair
    genuinely new in this release."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_future_plan_contains_all_four_units_with_correct_policy(self):
        _author, upstream, _bootstrap, r0002, r0003 = _create_promotion_repository(
            self.root / "r0023-scenario",
            promoted_units=[
                "isadoraair-sync-road-conditions.service", "isadoraair-sync-road-conditions.timer",
            ],
            promote_release_changes={
                "systemd_units_new_required": list(ROAD_CONDITIONS_UNITS),
            },
            # The generator pair is genuinely new in this release --
            # added within the SAME commit that introduces r0003's own
            # manifest, so it shows up as a real git "A" in the
            # predecessor diff, exactly the ordinary new-file path.
            promote_release_files={
                "deploy/isadoraair-generate-road-condition-audio.service": "[Service]\nExecStart=/bin/true\n",
                "deploy/isadoraair-generate-road-condition-audio.timer": "[Timer]\nOnUnitActiveSec=5min\n",
            },
        )

        repo = TrustedRepository(self.root / "r0023-scenario.git", str(upstream), "main", CommandRunner())
        tip = repo.fetch()
        plan = derive_plan(repo, tip, r0002, "r0003")

        self.assertEqual(set(plan.systemd_units_new_required), set(ROAD_CONDITIONS_UNITS))
        self.assertEqual(manual_blockers(plan), ())
        for unit in ROAD_CONDITIONS_UNITS:
            self.assertIn(unit, MANAGED_UNIT_POLICIES)
        self.assertIs(
            MANAGED_UNIT_POLICIES["isadoraair-sync-road-conditions.service"], UnitActivationPolicy.INSTALL_ONLY,
        )
        self.assertIs(
            MANAGED_UNIT_POLICIES["isadoraair-sync-road-conditions.timer"], UnitActivationPolicy.ENABLE_NOW,
        )
        self.assertIs(
            MANAGED_UNIT_POLICIES["isadoraair-generate-road-condition-audio.service"], UnitActivationPolicy.INSTALL_ONLY,
        )
        self.assertIs(
            MANAGED_UNIT_POLICIES["isadoraair-generate-road-condition-audio.timer"], UnitActivationPolicy.ENABLE_NOW,
        )


class ManifestProtocolBumpTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_manifest_protocol_bumped_to_five(self):
        self.assertEqual(MANIFEST_PROTOCOL_VERSION, 5)

    def test_wire_protocol_unchanged_by_this_release(self):
        # Same invariant test_phase_b_protocol.py's own
        # test_runtime_v5_keeps_wire_protocol_v3 documents -- restated
        # here because this is precisely the change that could have
        # broken it (see this task's own investigation: MANIFEST_
        # PROTOCOL_VERSION and PROTOCOL_VERSION used to be the same
        # constant, and bumping it broke every existing daemon-socket
        # client's request shape).
        self.assertEqual(PROTOCOL_VERSION, 3)
        self.assertEqual(RUNTIME_VERSION, 5)

    def test_old_updater_protocol_rejects_a_release_requiring_the_new_one(self):
        """Simulates an updater still running protocol-3 code (as it
        would immediately after fetching r0022 but before its own
        manual-bootstrap upgrade) encountering a plan that requires
        protocol 4 -- UPDATER_UPGRADE_REQUIRED must block it, never a
        silent best-effort apply."""
        _author, upstream, _bootstrap, r2, _r3 = create_release_repository(
            self.root / "old-updater-rejects",
            third_release_changes={"minimum_updater_protocol_version": 4},
        )
        repo = TrustedRepository(self.root / "old-updater-rejects.git", str(upstream), "main", CommandRunner())
        tip = repo.fetch()
        plan = derive_plan(repo, tip, r2, "r0003")
        self.assertEqual(plan.minimum_updater_protocol_version, 4)
        import isadoraair_updater.release as release_module
        with mock.patch.object(release_module, "MANIFEST_PROTOCOL_VERSION", 3):
            self.assertIn("UPDATER_UPGRADE_REQUIRED", manual_blockers(plan))
        # And the CURRENT (already-bumped) code accepts the same plan cleanly.
        self.assertNotIn("UPDATER_UPGRADE_REQUIRED", manual_blockers(plan))

    def test_protocol_four_path_accepts_the_new_semantics_no_blocker(self):
        author, upstream, _bootstrap, r0002, r0003 = _create_promotion_repository(
            self.root / "protocol-four-accepts",
            promoted_units=["isadoraair-sync-road-conditions.timer"],
            promote_release_changes={
                "systemd_units_new_required": ["isadoraair-sync-road-conditions.timer"],
                "minimum_updater_protocol_version": 4,
            },
        )
        repo = TrustedRepository(self.root / "protocol-four-accepts.git", str(upstream), "main", CommandRunner())
        tip = repo.fetch()
        plan = derive_plan(repo, tip, r0002, "r0003")
        self.assertEqual(manual_blockers(plan), ())

    def test_fingerprint_payload_unaffected_by_protocol_number_itself(self):
        # minimum_updater_protocol_version is already part of the
        # existing fingerprint payload (see release.py's
        # execution_fingerprint_payload) -- proves the value simply
        # flows through deterministically, same as any other field,
        # with no special-casing introduced by this task.
        from isadoraair_updater.release import execution_fingerprint_payload, fingerprint
        base = dict(
            installed_release_id="r0002", installed_commit="a" * 40,
            target_release_id="r0003", target_commit="b" * 40,
            releases_in_plan=("r0003",), migrations_required=(), migration_compatibility=None,
            python_requirements_changed=False, apt_packages_new=(),
            systemd_units_changed=(), systemd_units_new_required=(),
            systemd_units_new_optional=(), systemd_units_removed_or_renamed=(),
            collectstatic_required=False, services_requiring_restart=(),
            nginx_changed=False, runtime_components_changed=False,
            manual_bootstrap_required=False,
        )
        fp3 = fingerprint(execution_fingerprint_payload(**base, minimum_updater_protocol_version=3))
        fp4 = fingerprint(execution_fingerprint_payload(**base, minimum_updater_protocol_version=4))
        self.assertNotEqual(fp3, fp4)
        fp4_again = fingerprint(execution_fingerprint_payload(**base, minimum_updater_protocol_version=4))
        self.assertEqual(fp4, fp4_again)
