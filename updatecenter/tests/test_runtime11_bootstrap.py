"""P1 1.17 -- runtime-11 bootstrap through the REAL pre-handoff boundary.

Release manifests stay manifest-protocol 5 indefinitely; runtime 11 is the
capability boundary. A runtime-10 station that skipped r0105 must still
parse the WHOLE trusted chain, derive its plan, and hand off -- even when
later releases carry migration-authorization companions.

The runtime-10 side runs the ACTUAL r0104 protected runtime, extracted
byte-for-byte from the baseline commit with `git archive` and executed in an
isolated subprocess (it asserts it really is runtime 10 / protocol 5). The
trusted history is a clone of THIS repository at r0104 -- the genuine
104-release chain -- extended with:

    r0105 (runtime-11 transition) -> companion -> r0106 -> companion -> r0107 -> companion
"""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import uuid

from django.test import SimpleTestCase, TestCase

from updatecenter.management.commands.updatecenter_probe import build_probe_payload
from updatecenter.manifest import ManifestError, validate_manifest_dict

from .phase_b_helpers import PROJECT_ROOT, config_dict, git, manifest
from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.executor import Executor
from isadoraair_updater.jobs import JobStore
from isadoraair_updater.migration_authorization import load_plan_authorizations
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import (
    ReleaseError, TrustedRepository, derive_plan, manual_blockers, parse_manifest,
    resolve_known_managed_units,
)
from isadoraair_updater.runtime_handoff import handoff_required

R0104_BASELINE = "557413534c3dfb1658ea130e86f8fb5094e8c520"

_OLD_RUNTIME_DRIVER = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import isadoraair_updater as package
from isadoraair_updater import executor, release
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.runtime_handoff import handoff_required
request = json.load(sys.stdin)
result = {"runtime_version": package.RUNTIME_VERSION, "manifest_protocol": package.MANIFEST_PROTOCOL_VERSION}
try:
    if request["kind"] == "manifest":
        release.parse_manifest(request["data"], label=request["data"]["release_id"] + ".json")
    elif request["kind"] == "probe":
        executor._strict_probe(request["raw"].encode("utf-8"), review_context=True)
    else:
        repository = release.TrustedRepository(Path(request["git_dir"]), "unused", "main", CommandRunner())
        tip = request["tip"]
        known = release.resolve_known_managed_units(active_policy=None)
        # Exactly what the r0104 executor does before handoff: derive the
        # plan (loading/parsing the WHOLE chain), check blockers, decide handoff.
        plan = release.derive_plan(repository, tip, request["live_head"], request["target"], known_units=known)
        result.update(
            chain_releases=len(release.load_chain(repository, tip)),
            listed_manifest_files=repository.list_release_files(tip),
            releases_in_plan=list(plan.releases_in_plan),
            protected_transition=plan.protected_runtime_transition.release_id if plan.protected_runtime_transition else None,
            fingerprint=plan.fingerprint,
            minimum_protocol=plan.minimum_updater_protocol_version,
            blockers=list(release.manual_blockers(plan, known_units=known)),
            handoff_required=handoff_required(plan.protected_runtime),
        )
    result["ok"] = True
except (release.ReleaseError, executor.ExecutionError) as exc:
    result["ok"] = False
    result["error"] = str(exc) or getattr(exc, "detail", "")
print(json.dumps(result))
"""


def protected_runtime(release_id, *, generation=11, runtime_version=11, manifest_protocol_version=5):
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
    """The shape of the real r0105 runtime-11 bootstrap manifest."""
    value = manifest(
        "r0105", "r0104",
        minimum_updater_protocol_version=5,
        migrations_required=["updatecenter.0004_updatejob_migration_recovery"],
        migration_compatibility="additive",
        services_requiring_restart=["isadoraair-gunicorn"],
        minimum_supported_release_id="r0081",
        protected_runtime=protected_runtime("r0105"),
    )
    value.update(changes)
    return value


class OldRuntimeMixin:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._runtime_temp = tempfile.TemporaryDirectory()
        archive = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "archive", R0104_BASELINE, "deploy/updater_runtime"],
            check=True, capture_output=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(cls._runtime_temp.name, filter="data")
        cls.old_runtime_root = Path(cls._runtime_temp.name) / "deploy" / "updater_runtime"

    @classmethod
    def tearDownClass(cls):
        cls._runtime_temp.cleanup()
        super().tearDownClass()

    def old_runtime(self, request):
        completed = subprocess.run(
            [sys.executable, "-I", "-c", _OLD_RUNTIME_DRIVER, str(self.old_runtime_root)],
            input=json.dumps(request), capture_output=True, text=True, timeout=600, cwd=self._runtime_temp.name,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual((result["runtime_version"], result["manifest_protocol"]), (10, 5))
        return result


class ManifestContractTests(OldRuntimeMixin, SimpleTestCase):
    """Manifests stay protocol 5: the bootstrap shape parses everywhere; the
    withdrawn protocol-6 fields are unknown to EVERY parser."""

    def test_runtime_10_parses_the_r0105_bootstrap_manifest(self):
        result = self.old_runtime({"kind": "manifest", "data": r0105_bootstrap()})
        self.assertTrue(result["ok"], result.get("error"))

    def test_current_parsers_accept_the_r0105_bootstrap_manifest(self):
        validate_manifest_dict(r0105_bootstrap(), source_label="r0105.json")
        parse_manifest(r0105_bootstrap(), label="r0105.json")

    def test_withdrawn_protocol_six_fields_are_unknown_to_every_parser(self):
        for field, value in (
            ("migration_authorization", "deploy/migration_authorizations/r0105.json"),
            ("migration_preflight_checks", ["library.schedule_block_duplicate_times"]),
        ):
            with self.subTest(field=field):
                data = r0105_bootstrap(**{field: value})
                self.assertFalse(self.old_runtime({"kind": "manifest", "data": data})["ok"])
                with self.assertRaises(ManifestError):
                    validate_manifest_dict(data, source_label="r0105.json")
                with self.assertRaises(ReleaseError):
                    parse_manifest(data, label="r0105.json")


class OldRuntimeProbeCompatibilityTests(OldRuntimeMixin, TestCase):
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
        self.assertFalse(self.old_runtime({"kind": "probe", "raw": json.dumps(payload)})["ok"])


R0106_MIGRATION = "library.0089_r0106_reviewed_backfill"
R0107_MIGRATION = "library.0090_r0107_reviewed_cleanup"
R0105_MIGRATION = "updatecenter.0004_updatejob_migration_recovery"


class SkippedReleaseHistory:
    """A clone of this repository at r0104 extended with r0105..r0107 and
    their companions, committed exactly as the publication contract says."""

    def __init__(self, root: Path):
        self.root = root
        self.repo = root / "trusted"
        subprocess.run(
            ["git", "clone", "--quiet", "--shared", "--no-checkout", str(PROJECT_ROOT), str(self.repo)],
            check=True, capture_output=True,
        )
        git(self.repo, "checkout", "-q", "-B", "main", R0104_BASELINE)
        self.r0104 = R0104_BASELINE
        self.r0105 = self._release(
            "r0105", r0105_bootstrap(),
            files={
                f"updatecenter/migrations/{R0105_MIGRATION.split('.', 1)[1]}.py":
                    (PROJECT_ROOT / "updatecenter/migrations/0004_updatejob_migration_recovery.py").read_text(),
                "deploy/updater_runtime/isadoraair_updater/__init__.py":
                    (self.repo / "deploy/updater_runtime/isadoraair_updater/__init__.py").read_text()
                    + "\n# runtime 11 (synthetic bootstrap change)\n",
            },
        )
        self._companion("r0105", self.r0105, [(R0105_MIGRATION, 0, "AddField")])
        self.r0106 = self._release("r0106", self._manifest("r0106", "r0105", R0106_MIGRATION), files={
            "library/migrations/0089_r0106_reviewed_backfill.py": self._migration("RunPython"),
        })
        self._companion("r0106", self.r0106, [(R0106_MIGRATION, 0, "RunPython")])
        self.r0107 = self._release("r0107", self._manifest("r0107", "r0106", R0107_MIGRATION), files={
            "library/migrations/0090_r0107_reviewed_cleanup.py": self._migration("RunSQL"),
        })
        self._companion("r0107", self.r0107, [(R0107_MIGRATION, 0, "RunSQL")])
        self.tip = git(self.repo, "rev-parse", "HEAD")
        self.git_dir = self.repo / ".git"

    @staticmethod
    def _manifest(release_id, previous, migration):
        return manifest(
            release_id, previous, minimum_updater_protocol_version=5,
            migrations_required=[migration], migration_compatibility="additive",
            minimum_supported_release_id="r0081",
        )

    @staticmethod
    def _migration(operation):
        return (
            "from django.db import migrations\n\n\n"
            f"class Migration(migrations.Migration):  # synthetic reviewed {operation}\n"
            "    dependencies = [(\"library\", \"0088_enforce_schedule_profile_integrity\")]\n"
            "    operations = []\n"
        )

    def _write(self, relative, content):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        git(self.repo, "add", relative)

    def _release(self, release_id, data, *, files):
        for relative, content in files.items():
            self._write(relative, content)
        self._write(f"deploy/releases/{release_id}.json", json.dumps(data, indent=2) + "\n")
        git(self.repo, "commit", "-q", "-m", f"release {release_id}")
        return git(self.repo, "rev-parse", "HEAD")

    def file_sha(self, commit, relative):
        raw = subprocess.run(["git", "-C", str(self.repo), "show", f"{commit}:{relative}"],
                             check=True, capture_output=True).stdout
        return hashlib.sha256(raw).hexdigest()

    def migration_path(self, ref):
        app, name = ref.split(".", 1)
        return f"{app}/migrations/{name}.py"

    def _companion(self, release_id, commit, operations):
        record = {
            "schema_version": 1, "release_id": release_id, "target_commit": commit,
            "manifest_sha256": self.file_sha(commit, f"deploy/releases/{release_id}.json"),
            "authorized_manual_operations": [
                {"ref": ref, "migration_file_sha256": self.file_sha(commit, self.migration_path(ref)),
                 "operation_index": index, "operation": operation, "classification": "manual"}
                for ref, index, operation in operations
            ],
        }
        self._write(f"deploy/migration_authorizations/{release_id}.json", json.dumps(record, indent=2) + "\n")
        git(self.repo, "commit", "-q", "-m", f"migration authorization companion for {release_id}")


class SkippedReleaseEndToEndTests(OldRuntimeMixin, SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._history_temp = tempfile.TemporaryDirectory()
        cls.history = SkippedReleaseHistory(Path(cls._history_temp.name))
        cls.runtime10 = None

    @classmethod
    def tearDownClass(cls):
        cls._history_temp.cleanup()
        super().tearDownClass()

    def runtime_10_plan(self):
        if type(self).runtime10 is None:
            type(self).runtime10 = self.old_runtime({
                "kind": "derive", "git_dir": str(self.history.git_dir), "tip": self.history.tip,
                "live_head": self.history.r0104, "target": "r0107",
            })
        return type(self).runtime10

    def runtime_11_plan(self):
        repository = TrustedRepository(self.history.git_dir, "unused", "main", CommandRunner())
        known = resolve_known_managed_units(active_policy=None)
        return repository, derive_plan(repository, self.history.tip, self.history.r0104, "r0107", known_units=known), known

    def test_runtime_10_parses_full_chain_derives_plan_and_reaches_handoff(self):
        result = self.runtime_10_plan()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertGreaterEqual(result["chain_releases"], 107)                    # 1. whole trusted chain loaded
        self.assertEqual(result["releases_in_plan"], ["r0105", "r0106", "r0107"])  # 2. r0104 -> r0107 derived
        self.assertTrue(all(name.endswith(".json") and name.startswith("r") for name in result["listed_manifest_files"]))
        self.assertNotIn("migration_authorizations", json.dumps(result["listed_manifest_files"]))
        self.assertEqual(result["minimum_protocol"], 5)                            # 4. protocol never blocks
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["protected_transition"], "r0105")                  # 5. r0105 is the required transition
        self.assertTrue(result["handoff_required"])                                # 6. handoff is reachable
        # 3. companions sat between releases throughout derive_plan()'s
        # predecessor-diff cross-checks of r0105, r0106 and r0107.

    def test_runtime_11_re_derives_the_identical_fingerprint(self):
        _repository, plan, known = self.runtime_11_plan()
        self.assertEqual(plan.fingerprint, self.runtime_10_plan()["fingerprint"])
        self.assertEqual(list(plan.releases_in_plan), ["r0105", "r0106", "r0107"])
        self.assertEqual(manual_blockers(plan, known_units=known), ())
        self.assertTrue(handoff_required(plan.protected_runtime))

    def test_runtime_11_discovers_every_companion_and_authorizes_the_skipped_plan_centrally(self):
        repository, plan, _known = self.runtime_11_plan()
        authorizations = load_plan_authorizations(repository, self.history.tip, plan)
        self.assertEqual(sorted(authorizations), ["r0105", "r0106", "r0107"])
        refs = [R0105_MIGRATION, R0106_MIGRATION, R0107_MIGRATION]
        items = [
            {"ref": ref, "dependencies": [], "migration_file_sha256": self.history.file_sha(
                self.history.tip, self.history.migration_path(ref)),
             "operations": [{"operation": op, "classification": cls, "detail": "x"}]}
            for ref, op, cls in (
                (R0105_MIGRATION, "AddField", "additive"),
                (R0106_MIGRATION, "RunPython", "manual"),
                (R0107_MIGRATION, "RunSQL", "manual"),
            )
        ]
        manual = [
            {"ref": R0106_MIGRATION, "operation_index": 0, "operation": "RunPython", "classification": "manual", "detail": "x"},
            {"ref": R0107_MIGRATION, "operation_index": 0, "operation": "RunSQL", "classification": "manual", "detail": "x"},
        ]
        payload = {
            "nodes": {ref: [] for ref in refs}, "applied": [], "plan": items, "conflicts": {}, "replacements": [],
            "release_id": "r0107", "target_commit": self.history.r0107, "manifest_sha256": "c" * 64,
            "migration_plan_digest": "7" * 64, "manual_operations": manual, "approval": None,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = validate_config_dict(config_dict(root, str(root / "upstream.git")), allow_local_repository=True)
            store = JobStore(config.jobs_root, config.logs_root, acquire_daemon_lock=False)
            try:
                executor = Executor(config, store, CommandRunner())
                executor.repository = repository
                job_id = str(uuid.uuid4())
                store.accept(job_id, "r0107", plan.fingerprint)
                actual = executor._validate_target_schema(
                    plan, payload, {"nodes": {}, "applied": []}, job_id,
                    migration_already_started=False, trusted_tip=self.history.tip,
                )
                self.assertEqual(set(actual), set(refs))
                self.assertEqual(executor.last_authorization_source, "central")   # 10. proceeds centrally
            finally:
                store.close()


class InitiatingPlannerFingerprintTests(SimpleTestCase):
    """The fingerprint the operator confirms on an r0104 station is computed
    by r0104 code (the r0104 Django planner, in lock-step with runtime 10,
    which re-checks it at line `PLAN_FINGERPRINT_MISMATCH` before handoff).
    Companions never enter it, so runtime 11 reproduces it exactly: proven
    end to end above (runtime 10 == runtime 11). Here: the contract itself
    is unchanged -- current Django and runtime fingerprints are the legacy
    ones, with no companion input at all."""

    def test_fingerprint_contract_has_no_companion_input(self):
        from updatecenter import execution_contract
        source = Path(execution_contract.__file__).read_text()
        self.assertNotIn("migration_authorization", source)
        self.assertNotIn("contract_version\": 5", source)
        runtime_source = (PROJECT_ROOT / "deploy/updater_runtime/isadoraair_updater/release.py").read_bytes()
        baseline = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "show", f"{R0104_BASELINE}:deploy/updater_runtime/isadoraair_updater/release.py"],
            check=True, capture_output=True,
        ).stdout
        self.assertEqual(runtime_source, baseline, "trusted-chain/fingerprint code must stay byte-identical to r0104")
