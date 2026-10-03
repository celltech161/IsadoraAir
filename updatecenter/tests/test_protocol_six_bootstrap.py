"""P1 1.17 B3 -- the protocol-6 / runtime-11 bootstrap contract.

A runtime-10 (protocol-5) worker parses EVERY manifest on the trusted tip and
hard-rejects unknown fields, and its _strict_probe requires the exact 13-key
review shape. These tests run the ACTUAL r0104 protected runtime -- extracted
byte-for-byte from the baseline commit with `git archive` and executed in an
isolated subprocess -- rather than a hand-written approximation of it.
"""
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

from django.test import SimpleTestCase, TestCase

from updatecenter import release_chain
from updatecenter.management.commands.updatecenter_probe import build_probe_payload
from updatecenter.manifest import validate_manifest_dict

from .phase_b_helpers import PROJECT_ROOT, manifest
from isadoraair_updater.release import protocol_six_bootstrap_problem

R0104_BASELINE = "557413534c3dfb1658ea130e86f8fb5094e8c520"
AUTH = "deploy/migration_authorizations/r0106.json"

_OLD_RUNTIME_DRIVER = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import isadoraair_updater as package
from isadoraair_updater import executor, release
request = json.load(sys.stdin)
result = {"runtime_version": package.RUNTIME_VERSION, "manifest_protocol": package.MANIFEST_PROTOCOL_VERSION}
try:
    if request["kind"] == "manifest":
        release.parse_manifest(request["data"], label=request["data"]["release_id"] + ".json")
    else:
        executor._strict_probe(request["raw"].encode("utf-8"), review_context=True)
    result["ok"] = True
except (release.ReleaseError, executor.ExecutionError) as exc:
    result["ok"] = False
    result["error"] = str(exc) or getattr(exc, "detail", "")
print(json.dumps(result))
"""


def protected_runtime(release_id, *, runtime_version, manifest_protocol_version, generation):
    return {
        "generation": generation,
        "descriptor_path": "deploy/updater_runtime/protected-runtime-descriptor.json",
        "descriptor_sha256": "5" * 64,
        "minimum_bootstrap_protocol_version": 1,
        "runtime_version": runtime_version,
        "manifest_protocol_version": manifest_protocol_version,
        "supported_wire_protocols": [3, 4],
        "attestations": [f"deploy/updater_attestations/{release_id}-primary.json"],
    }


def r0105_bootstrap(**changes):
    """The shape the eventual r0105 runtime-11 bootstrap manifest must have."""
    value = manifest(
        "r0105", "r0104",
        minimum_updater_protocol_version=5,
        migrations_required=["updatecenter.0004_updatejob_migration_recovery"],
        migration_compatibility="additive",
        services_requiring_restart=["isadoraair-gunicorn"],
        protected_runtime=protected_runtime("r0105", runtime_version=11, manifest_protocol_version=6, generation=11),
    )
    value.update(changes)
    return value


def r0106(**changes):
    value = manifest(
        "r0106", "r0105",
        minimum_updater_protocol_version=6,
        migrations_required=["library.0089_example"],
        migration_compatibility="additive",
        migration_authorization=AUTH,
        migration_preflight_checks=["library.schedule_block_duplicate_times"],
    )
    value.update(changes)
    return value


class OldRuntimeMixin:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._temp = tempfile.TemporaryDirectory()
        archive = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "archive", R0104_BASELINE, "deploy/updater_runtime"],
            check=True, capture_output=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(cls._temp.name, filter="data")
        cls.old_runtime_root = Path(cls._temp.name) / "deploy" / "updater_runtime"

    @classmethod
    def tearDownClass(cls):
        cls._temp.cleanup()
        super().tearDownClass()

    def old_runtime(self, request):
        completed = subprocess.run(
            [sys.executable, "-I", "-c", _OLD_RUNTIME_DRIVER, str(self.old_runtime_root)],
            input=json.dumps(request), capture_output=True, text=True, timeout=60, cwd=self._temp.name,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        # Prove this really is the runtime-10 / protocol-5 worker.
        self.assertEqual((result["runtime_version"], result["manifest_protocol"]), (10, 5))
        return result


class OldRuntimeManifestParserTests(OldRuntimeMixin, SimpleTestCase):
    def test_runtime_10_parses_the_r0105_bootstrap_manifest(self):
        result = self.old_runtime({"kind": "manifest", "data": r0105_bootstrap()})
        self.assertTrue(result["ok"], result.get("error"))

    def test_runtime_10_rejects_protocol_six_authorization_field(self):
        result = self.old_runtime({"kind": "manifest", "data": r0106(migration_preflight_checks=[])})
        self.assertFalse(result["ok"])
        self.assertIn("migration_authorization", result["error"])

    def test_runtime_10_rejects_protocol_six_preflight_field(self):
        data = r0106()
        del data["migration_authorization"]
        result = self.old_runtime({"kind": "manifest", "data": data})
        self.assertFalse(result["ok"])
        self.assertIn("migration_preflight_checks", result["error"])


class OldRuntimeProbeCompatibilityTests(OldRuntimeMixin, TestCase):
    """The NEW probe's ordinary target output stays acceptable to runtime 10;
    the recovery shape exists only after the handoff to runtime 11."""

    def test_runtime_10_accepts_the_new_ordinary_target_probe(self):
        payload = build_probe_payload(release_id="r0104", target_commit="b" * 40)
        result = self.old_runtime({"kind": "probe", "raw": json.dumps(payload)})
        self.assertTrue(result["ok"], result.get("error"))

    def test_recovery_shape_is_only_emitted_on_request_and_runtime_10_would_refuse_it(self):
        payload = build_probe_payload(
            release_id="r0104", target_commit="b" * 40,
            recovery_plan_refs=["updatecenter.0004_updatejob_migration_recovery"],
        )
        self.assertIn("recovery_plan", payload)
        result = self.old_runtime({"kind": "probe", "raw": json.dumps(payload)})
        self.assertFalse(result["ok"])


class HandoffOrderingTests(SimpleTestCase):
    """Runtime 10 hands off to the candidate BEFORE staging/probing the
    target, so the target probe of an r0105 install is always run by
    runtime 11. Asserted on the actual r0104 executor source and on the
    current one (the order must never regress)."""

    def _assert_handoff_precedes_target_probe(self, source):
        body = source[source.index("    def execute(self, job_id"):]
        self.assertLess(body.index("self._execute_runtime_handoff("), body.index("target_payload = self._probe("))
        self.assertLess(body.index("self._execute_runtime_handoff("), body.index("materialize("))

    def test_r0104_runtime_hands_off_before_target_probe(self):
        source = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "show",
             f"{R0104_BASELINE}:deploy/updater_runtime/isadoraair_updater/executor.py"],
            check=True, capture_output=True, text=True,
        ).stdout
        self._assert_handoff_precedes_target_probe(source)

    def test_runtime_11_hands_off_before_target_probe(self):
        source = (PROJECT_ROOT / "deploy/updater_runtime/isadoraair_updater/executor.py").read_text()
        self._assert_handoff_precedes_target_probe(source)


def _chain(*extra):
    manifests = release_chain.load_manifest_files(PROJECT_ROOT / "deploy" / "releases")
    for data in extra:
        parsed = validate_manifest_dict(data, source_label=f"{data['release_id']}.json")
        manifests[parsed.release_id] = parsed
    return manifests


def _runtime_tuples(ordered):
    return [
        (
            item.manifest.release_id,
            item.manifest.minimum_updater_protocol_version,
            item.manifest.migration_authorization is not None or bool(item.manifest.migration_preflight_checks),
            item.manifest.protected_runtime.runtime_version if item.manifest.protected_runtime else None,
            item.manifest.protected_runtime.manifest_protocol_version if item.manifest.protected_runtime else None,
        )
        for item in ordered
    ]


class BootstrapValidatorTests(SimpleTestCase):
    """Both chain builders enforce the rule; it never relies on docs."""

    def verdicts(self, *extra):
        manifests = _chain(*extra)
        try:
            ordered = release_chain.build_chain(manifests)
            django_error = None
        except release_chain.ChainError as exc:
            django_error = str(exc)
            # Rebuild the order without the bootstrap rule for the runtime mirror.
            from unittest import mock
            with mock.patch.object(release_chain, "protocol_six_bootstrap_problem", return_value=None):
                ordered = release_chain.build_chain(manifests)
        runtime_error = protocol_six_bootstrap_problem(_runtime_tuples(ordered))
        self.assertEqual(django_error is None, runtime_error is None, (django_error, runtime_error))
        return django_error

    def test_current_published_chain_is_valid(self):
        self.assertIsNone(self.verdicts())

    def test_protocol_five_runtime_11_bootstrap_release_is_valid(self):
        self.assertIsNone(self.verdicts(r0105_bootstrap()))

    def test_protocol_six_fields_after_the_bootstrap_release_are_valid(self):
        self.assertIsNone(self.verdicts(r0105_bootstrap(), r0106()))

    def test_bootstrap_release_may_not_use_protocol_six_fields(self):
        error = self.verdicts(r0105_bootstrap(
            minimum_updater_protocol_version=6, migration_preflight_checks=["library.schedule_block_duplicate_times"],
        ))
        self.assertIn("must stay protocol-5 compatible", error)

    def test_bootstrap_release_may_not_require_protocol_six(self):
        self.assertIsNotNone(self.verdicts(r0105_bootstrap(minimum_updater_protocol_version=6)))

    def test_protocol_six_fields_without_any_runtime_11_delivery_are_refused(self):
        plain = r0105_bootstrap()
        del plain["protected_runtime"]
        error = self.verdicts(plain, r0106())
        self.assertIn("before any earlier release delivers protected runtime 11", error)

    def test_protocol_six_minimum_without_fields_before_delivery_is_refused(self):
        plain = r0105_bootstrap()
        del plain["protected_runtime"]
        bare = r0106(migration_authorization=None, migration_preflight_checks=[])
        del bare["migration_authorization"]
        self.assertIsNotNone(self.verdicts(plain, bare))

    def test_runtime_older_than_11_does_not_count_as_delivery(self):
        old = r0105_bootstrap(protected_runtime=protected_runtime(
            "r0105", runtime_version=10, manifest_protocol_version=5, generation=11,
        ))
        self.assertIsNotNone(self.verdicts(old, r0106()))
