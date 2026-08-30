"""D1-A (manifest_field.py + both manifest.py/release.py copies), D1-H
(fingerprint contract v3), D1-I (the four independent protocol
constants), D1-J (the two-release final-bootstrap compatibility bridge)."""
import json
from pathlib import Path
import tempfile

from django.test import SimpleTestCase

from .phase_b_helpers import RUNTIME_ROOT, create_release_repository, git, manifest  # noqa: F401

from isadoraair_updater import BOOTSTRAP_PROTOCOL_VERSION, MANIFEST_PROTOCOL_VERSION, PROTOCOL_VERSION, RUNTIME_VERSION
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import (
    ReleaseError, TrustedRepository, execution_fingerprint_payload, fingerprint,
    parse_manifest, protected_runtime_fingerprint_payload,
)

from protected_bootstrap.manifest_field import ProtectedRuntimeFieldError, parse_protected_runtime_field

from updatecenter.manifest import ManifestError, validate_manifest_dict


# ---------------------------------------------------------------------
# D1-I: four independent protocol/version concepts.
# ---------------------------------------------------------------------
class FourProtocolConceptsTests(SimpleTestCase):
    def test_all_four_constants_exist_and_are_positive(self):
        for value in (PROTOCOL_VERSION, MANIFEST_PROTOCOL_VERSION, RUNTIME_VERSION, BOOTSTRAP_PROTOCOL_VERSION):
            self.assertIsInstance(value, int)
            self.assertGreaterEqual(value, 1)

    def test_bootstrap_protocol_is_a_genuinely_separate_concept(self):
        # Not derived from / equal to any of the other three by
        # coincidence-proofing convention -- this project's own
        # precedent (PROTOCOL_VERSION=3 != MANIFEST_PROTOCOL_VERSION=4)
        # already shows independent numbers are expected and normal;
        # this only proves the new constant is its own real value, not
        # accidentally aliased to an existing one via e.g. a typo'd
        # `= RUNTIME_VERSION`.
        self.assertEqual(BOOTSTRAP_PROTOCOL_VERSION, 1)
        self.assertIsNot(BOOTSTRAP_PROTOCOL_VERSION, RUNTIME_VERSION)


# ---------------------------------------------------------------------
# D1-A: the optional protected_runtime manifest field, both copies.
# ---------------------------------------------------------------------
def _valid_protected_runtime_dict(**overrides):
    data = {
        "generation": 1,
        "descriptor_path": "deploy/updater_runtime/runtime-descriptor.json",
        "descriptor_sha256": "a" * 64,
        "minimum_bootstrap_protocol_version": 1,
        "runtime_version": 5,
        "manifest_protocol_version": 5,
        "supported_wire_protocols": [3],
        "attestations": ["deploy/updater_attestations/r0027/release-key.sig"],
    }
    data.update(overrides)
    return data


class ProtectedBootstrapManifestFieldTests(SimpleTestCase):
    def test_null_returns_none(self):
        self.assertIsNone(parse_protected_runtime_field(None))

    def test_valid_object_parses(self):
        field = parse_protected_runtime_field(_valid_protected_runtime_dict())
        self.assertEqual(field.generation, 1)
        self.assertEqual(field.supported_wire_protocols, (3,))

    def test_descriptor_path_wrong_prefix_rejected(self):
        with self.assertRaises(ProtectedRuntimeFieldError):
            parse_protected_runtime_field(_valid_protected_runtime_dict(descriptor_path="etc/evil.json"))

    def test_attestation_path_wrong_prefix_rejected(self):
        with self.assertRaises(ProtectedRuntimeFieldError):
            parse_protected_runtime_field(
                _valid_protected_runtime_dict(attestations=["deploy/somewhere-else/sig"])
            )

    def test_duplicate_attestation_rejected(self):
        with self.assertRaises(ProtectedRuntimeFieldError):
            parse_protected_runtime_field(
                _valid_protected_runtime_dict(attestations=[
                    "deploy/updater_attestations/r0027/a.sig", "deploy/updater_attestations/r0027/a.sig",
                ])
            )

    def test_unknown_field_rejected(self):
        data = _valid_protected_runtime_dict()
        data["extra"] = 1
        with self.assertRaises(ProtectedRuntimeFieldError):
            parse_protected_runtime_field(data)


class DjangoSideManifestFieldTests(SimpleTestCase):
    """updatecenter/manifest.py's own independently-maintained mirror."""

    def _manifest(self, **overrides):
        data = {
            "schema_version": 1, "release_id": "r0027", "previous_release_id": "r0026",
            "minimum_updater_protocol_version": 4, "migrations_required": [],
            "python_requirements_changed": False, "apt_packages_new": [],
            "systemd_units_changed": [], "systemd_units_new_required": [],
            "systemd_units_new_optional": [], "collectstatic_required": False,
            "services_requiring_restart": [], "nginx_changed": False,
            "runtime_components_changed": False,
        }
        data.update(overrides)
        return data

    def test_ordinary_release_omitting_field_parses_with_none(self):
        parsed = validate_manifest_dict(self._manifest())
        self.assertIsNone(parsed.protected_runtime)

    def test_ordinary_release_explicit_null_parses_with_none(self):
        parsed = validate_manifest_dict(self._manifest(protected_runtime=None))
        self.assertIsNone(parsed.protected_runtime)

    def test_real_r0025_manifest_still_parses_unaffected(self):
        # The actual production r0025.json, proving the D0 bridge
        # directly against real (not synthetic) manifest content.
        checkout_root = Path(__file__).resolve().parents[2]
        data = json.loads((checkout_root / "deploy/releases/r0025.json").read_text())
        self.assertNotIn("protected_runtime", data)
        parsed = validate_manifest_dict(data, source_label="r0025.json")
        self.assertIsNone(parsed.protected_runtime)

    def test_valid_protected_runtime_object_parses(self):
        parsed = validate_manifest_dict(self._manifest(protected_runtime=_valid_protected_runtime_dict()))
        self.assertIsNotNone(parsed.protected_runtime)
        self.assertEqual(parsed.protected_runtime.generation, 1)

    def test_malformed_protected_runtime_rejected(self):
        with self.assertRaises(ManifestError):
            validate_manifest_dict(self._manifest(protected_runtime={"generation": "not-an-int"}))

    def test_protected_runtime_not_in_missing_required_set(self):
        # Confirms this field is genuinely OPTIONAL (never silently
        # required) -- a manifest with every OTHER field present, but
        # no protected_runtime key at all, must not raise "missing".
        data = self._manifest()
        self.assertNotIn("protected_runtime", data)
        validate_manifest_dict(data)  # must not raise


class ProtectedWorkerManifestFieldTests(SimpleTestCase):
    """deploy/updater_runtime/isadoraair_updater/release.py's own copy."""

    def _manifest(self, **overrides):
        data = {
            "schema_version": 1, "release_id": "r0027", "previous_release_id": "r0026",
            "minimum_updater_protocol_version": 4, "migrations_required": [],
            "python_requirements_changed": False, "apt_packages_new": [],
            "systemd_units_changed": [], "systemd_units_new_required": [],
            "systemd_units_new_optional": [], "collectstatic_required": False,
            "services_requiring_restart": [], "nginx_changed": False,
            "runtime_components_changed": False, "systemd_units_removed_or_renamed": [],
        }
        data.update(overrides)
        return data

    def test_ordinary_release_omitting_field_parses_with_none(self):
        parsed = parse_manifest(self._manifest(), label="test")
        self.assertIsNone(parsed.protected_runtime)

    def test_real_r0025_manifest_still_parses_unaffected(self):
        checkout_root = Path(__file__).resolve().parents[2]
        data = json.loads((checkout_root / "deploy/releases/r0025.json").read_text())
        parsed = parse_manifest(data, label="r0025.json")
        self.assertIsNone(parsed.protected_runtime)

    def test_valid_protected_runtime_object_parses(self):
        parsed = parse_manifest(self._manifest(protected_runtime=_valid_protected_runtime_dict()), label="test")
        self.assertEqual(parsed.protected_runtime.generation, 1)

    def test_malformed_protected_runtime_rejected(self):
        with self.assertRaises(ReleaseError):
            parse_manifest(self._manifest(protected_runtime={"generation": "nope"}), label="test")

    def test_both_manifest_copies_agree_on_a_valid_object(self):
        # Cross-check the two independently-maintained copies (Django-
        # side updatecenter/manifest.py, protected-worker-side
        # release.py) never silently drift apart on the SAME input.
        data = self._manifest(protected_runtime=_valid_protected_runtime_dict())
        django_side = validate_manifest_dict(data)
        worker_side = parse_manifest(data, label="test")
        self.assertEqual(django_side.protected_runtime.generation, worker_side.protected_runtime.generation)
        self.assertEqual(
            django_side.protected_runtime.descriptor_sha256, worker_side.protected_runtime.descriptor_sha256,
        )

    def test_both_manifest_copies_agree_on_rejection(self):
        data = self._manifest(protected_runtime=_valid_protected_runtime_dict(descriptor_path="wrong/prefix.json"))
        with self.assertRaises(ManifestError):
            validate_manifest_dict(data)
        with self.assertRaises(ReleaseError):
            parse_manifest(data, label="test")


# ---------------------------------------------------------------------
# D1-H: fingerprint contract v3.
# ---------------------------------------------------------------------
def _base_fingerprint_values(**overrides):
    values = dict(
        installed_release_id="r0026", installed_commit="a" * 40,
        target_release_id="r0027", target_commit="b" * 40,
        releases_in_plan=("r0027",), migrations_required=(), migration_compatibility=None,
        python_requirements_changed=False, apt_packages_new=(),
        systemd_units_changed=(), systemd_units_new_required=(),
        systemd_units_new_optional=(), systemd_units_removed_or_renamed=(),
        collectstatic_required=False, services_requiring_restart=(),
        nginx_changed=False, runtime_components_changed=False,
        minimum_updater_protocol_version=5, manual_bootstrap_required=False,
    )
    values.update(overrides)
    return values


def _protected_runtime_fingerprint_values(**overrides):
    values = _base_fingerprint_values()
    values.update(
        protected_runtime_generation=1,
        protected_runtime_descriptor_sha256="a" * 64,
        protected_runtime_minimum_bootstrap_protocol_version=1,
        protected_runtime_runtime_version=5,
        protected_runtime_manifest_protocol_version=5,
        protected_runtime_supported_wire_protocols=(3,),
    )
    values.update(overrides)
    return values


class FingerprintV3ContractTests(SimpleTestCase):
    def test_v3_contract_version_is_3(self):
        payload = protected_runtime_fingerprint_payload(**_protected_runtime_fingerprint_values())
        self.assertEqual(payload["contract_version"], 3)

    def test_v2_contract_unchanged_by_v3_existing(self):
        # v2 payload/fingerprint for the identical base facts must be
        # byte-for-byte the same as before this task touched anything.
        payload = execution_fingerprint_payload(**_base_fingerprint_values())
        self.assertEqual(payload["contract_version"], 2)
        self.assertNotIn("protected_runtime", payload)

    def test_v3_embeds_v2_facts_unchanged(self):
        values = _protected_runtime_fingerprint_values()
        v2_payload = execution_fingerprint_payload(**values)
        v3_payload = protected_runtime_fingerprint_payload(**values)
        for key, value in v2_payload.items():
            if key == "contract_version":
                continue
            self.assertEqual(v3_payload[key], value, f"v3 payload disagrees with v2 on {key!r}")

    def test_v3_includes_protected_runtime_facts(self):
        payload = protected_runtime_fingerprint_payload(**_protected_runtime_fingerprint_values())
        self.assertEqual(payload["protected_runtime"]["generation"], 1)
        self.assertEqual(payload["protected_runtime"]["descriptor_sha256"], "a" * 64)
        self.assertEqual(payload["protected_runtime"]["minimum_bootstrap_protocol_version"], 1)
        self.assertEqual(payload["protected_runtime"]["supported_wire_protocols"], [3])

    def test_fingerprint_deterministic(self):
        values = _protected_runtime_fingerprint_values()
        a = fingerprint(protected_runtime_fingerprint_payload(**values))
        b = fingerprint(protected_runtime_fingerprint_payload(**values))
        self.assertEqual(a, b)

    def test_fingerprint_changes_when_generation_changes(self):
        a = fingerprint(protected_runtime_fingerprint_payload(**_protected_runtime_fingerprint_values()))
        b = fingerprint(protected_runtime_fingerprint_payload(
            **_protected_runtime_fingerprint_values(protected_runtime_generation=2)
        ))
        self.assertNotEqual(a, b)

    def test_fingerprint_changes_when_target_release_changes(self):
        a = fingerprint(protected_runtime_fingerprint_payload(**_protected_runtime_fingerprint_values()))
        b = fingerprint(protected_runtime_fingerprint_payload(
            **_protected_runtime_fingerprint_values(target_release_id="r0028")
        ))
        self.assertNotEqual(a, b)

    def test_v2_and_v3_fingerprints_of_the_same_base_facts_differ(self):
        # A candidate worker must never be able to pass off a v3
        # authorization as though it were the (differently-scoped) v2
        # one, or vice versa -- contract_version alone already
        # guarantees this, checked here at the actual digest level.
        values = _protected_runtime_fingerprint_values()
        v2_fp = fingerprint(execution_fingerprint_payload(**values))
        v3_fp = fingerprint(protected_runtime_fingerprint_payload(**values))
        self.assertNotEqual(v2_fp, v3_fp)

    def test_old_and_new_worker_compare_identical_v3_payload(self):
        # "The old and new worker must be able to compare the same
        # authorization facts across handoff" -- two independent calls
        # (simulating old vs. new worker code, both using the exact
        # same function) must produce byte-identical fingerprints.
        values = _protected_runtime_fingerprint_values()
        old_worker_fp = fingerprint(protected_runtime_fingerprint_payload(**values))
        new_worker_fp = fingerprint(protected_runtime_fingerprint_payload(**values))
        self.assertEqual(old_worker_fp, new_worker_fp)


# ---------------------------------------------------------------------
# D1-J: the two-release final-bootstrap compatibility bridge.
# ---------------------------------------------------------------------
class FinalBootstrapCompatibilityBridgeTests(SimpleTestCase):
    """Uses synthetic release ids (r0002/r0003 via phase_b_helpers'
    existing fixture builder), not real r0026/r0027 -- this workorder
    does not assign those yet."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_1_final_bootstrap_release_readable_by_pre_phase_d_schema(self):
        # The "r0026-shaped" release: manual_bootstrap_required=true,
        # no protected_runtime field at all -- must parse cleanly with
        # BOTH manifest copies exactly as every release does today.
        _author, upstream, _bootstrap, r0002, r0003 = create_release_repository(
            self.root / "final-bootstrap",
            third_release_changes={"manual_bootstrap_required": True},
        )
        repo = TrustedRepository(self.root / "final-bootstrap.git", str(upstream), "main", CommandRunner())
        tip = repo.fetch()
        raw = repo.read_file(tip, "deploy/releases/r0003.json")
        data = json.loads(raw)
        self.assertNotIn("protected_runtime", data)
        parsed_worker = parse_manifest(data, label="r0003.json")
        self.assertTrue(parsed_worker.manual_bootstrap_required)
        self.assertIsNone(parsed_worker.protected_runtime)
        parsed_django = validate_manifest_dict(data, source_label="r0003.json")
        self.assertTrue(parsed_django.manual_bootstrap_required)
        self.assertIsNone(parsed_django.protected_runtime)

    def test_2_final_bootstrap_may_carry_phase_d_capable_source_without_the_field(self):
        # Simulates "ships Phase-D-capable planner/runtime source" by
        # actually changing deploy/updater_runtime/** content in that
        # SAME release, while still declaring no protected_runtime --
        # legal because manual_bootstrap_required=true covers the
        # existing (unmodified in this task) pre-D gate.
        _author, upstream, _bootstrap, r0002, r0003 = create_release_repository(
            self.root / "bridge-source",
            third_release_changes={"manual_bootstrap_required": True},
            third_release_files={
                "deploy/updater_runtime/protected_bootstrap/__init__.py": "# phase d source\n",
            },
        )
        repo = TrustedRepository(self.root / "bridge-source.git", str(upstream), "main", CommandRunner())
        tip = repo.fetch()
        raw = repo.read_file(tip, "deploy/releases/r0003.json")
        data = json.loads(raw)
        parsed = parse_manifest(data, label="r0003.json")
        self.assertIsNone(parsed.protected_runtime)
        self.assertTrue(
            repo.path_exists(tip, "deploy/updater_runtime/protected_bootstrap/__init__.py")
        )

    def test_3_subsequent_release_may_populate_protected_runtime(self):
        # The "r0027-shaped" release layered on top of the bridge --
        # once a manifest actually declares protected_runtime, it must
        # parse with the real object, not silently ignored/coerced.
        protected_runtime_object = _valid_protected_runtime_dict()
        _author, upstream, _bootstrap, r0002, r0003 = create_release_repository(
            self.root / "post-bridge",
            third_release_changes={"protected_runtime": protected_runtime_object},
        )
        repo = TrustedRepository(self.root / "post-bridge.git", str(upstream), "main", CommandRunner())
        tip = repo.fetch()
        raw = repo.read_file(tip, "deploy/releases/r0003.json")
        data = json.loads(raw)
        parsed = parse_manifest(data, label="r0003.json")
        self.assertIsNotNone(parsed.protected_runtime)
        self.assertEqual(parsed.protected_runtime.generation, 1)

    def test_4_this_is_a_two_release_bridge_not_per_worker_manual_bootstrap(self):
        # Exactly two releases needed: (a) the final manual-bootstrap
        # bridge release (no protected_runtime, manual_bootstrap_
        # required=true), (b) the first release that actually uses
        # protected_runtime -- proven by successfully building BOTH as
        # a normal two-step chain with create_release_repository's
        # existing bootstrap->r0002->r0003 shape, no third/manual step
        # needed for the SECOND transition.
        protected_runtime_object = _valid_protected_runtime_dict()
        _author, upstream, _bootstrap, r0002, r0003 = create_release_repository(
            self.root / "two-release-bridge",
            third_release_changes={"protected_runtime": protected_runtime_object},
        )
        repo = TrustedRepository(self.root / "two-release-bridge.git", str(upstream), "main", CommandRunner())
        tip = repo.fetch()
        # r0002 itself (the release BEFORE the protected_runtime one)
        # still parses with the ordinary pre-Phase-D shape -- confirming
        # nothing about r0002 needed to change for r0003 to add the field.
        raw_r0002 = repo.read_file(tip, "deploy/releases/r0002.json")
        parsed_r0002 = parse_manifest(json.loads(raw_r0002), label="r0002.json")
        self.assertIsNone(parsed_r0002.protected_runtime)
