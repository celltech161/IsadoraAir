"""r0116 (iPortal D3E Active Messages worker infrastructure): the release says
exactly what the repository does, its protected generation 15 is the reviewed
runtime tree signed by the primary-release signer, and the trusted
migration-authorization companion -- once it exists -- authorizes exactly the
one manual operation the real classifier finds.

* lineage: published r0115 (29f6a33) -> the accepted D3E-A..D history ->
  release notes commit -> this release commit -> its companion, all
  single-parent fast-forwards; nothing in the accepted history rewritten;
* protected runtime: generation 15, runtime 13, manifest protocol 5, wire
  [3, 4]; the descriptor is rebuilt here from the release commit's runtime tree
  byte-for-byte; the only runtime changes since generation 14 are the policy
  entry and runtime 13's restart authority;
* the managed worker is a NEW required unit (ENABLE_NOW) and is never in the
  restart list -- runtime 12 (generation 14) does not know it; every declared
  restart is one runtime 12 already orders;
* one migration, monitoring.0014 (the collision-safe bytes), one manual
  RunPython;
* no engine, OGRemote, encoder, spoken, static, package, nginx or
  requirements change; the native-airing gate is a source-level False.

Historical assertions are bounded by the unique commit that introduces the
r0116 manifest, never by HEAD, so later releases cannot break them.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path

from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase

from .phase_b_helpers import RUNTIME_ROOT  # noqa: F401  (puts the protected runtime on sys.path)
from deploy.updater_bootstrap.tools.protected_runtime_release import build_descriptor, build_statement
from isadoraair_updater.migration_authorization import (
    central_authorization_covers, manual_operation_identity, validate_release_companion,
)
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import (
    CORE_SERVICES, MANAGED_UNIT_POLICIES, TrustedRepository, UnitActivationPolicy, load_chain,
)
from protected_bootstrap.descriptor import parse_descriptor_dict

from updatecenter import manifest as manifest_mod
from updatecenter import migration_authorization, planner
from updatecenter.management.commands import updatecenter_probe as probe

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = "deploy/releases/r0116.json"
DESCRIPTOR_PATH = "deploy/updater_runtime/protected-runtime-descriptor.json"
POLICY_PATH = "deploy/updater_runtime/protected-policy.json"
ATTESTATION_PATH = "deploy/updater_attestations/r0116-primary.json"
COMPANION = PROJECT_ROOT / "deploy" / "migration_authorizations" / "r0116.json"
R0115_RELEASE = "fd3ac805e774f6039636f3f8178f5770ea038572"   # the r0115 release commit (its manifest)
R0115 = "29f6a3353e41f659e1e1fbe3a46859bed671cad7"           # published r0115: its companion, canonical main
ACCEPTED = "f6096ea6c1fcbf70bffd7f3c712aadee706bc918"        # the independently accepted D3E-D source
DESCRIPTOR_SHA256 = "64f320aebf015896010d29ee667c634c72163fef8f777161f229fbf3ffbb913a"
BUNDLE_SHA256 = "60cb37219986c0c8083d207cc2917f680bfd1e09dd98dd26f0f379d5ee835d8a"
STATEMENT_SHA256 = "5c41e45331a23a7b26071375282580f4dcdf630317cc53ab428486825b734be3"
GENERATION_15_POLICY_SHA256 = "1b3ce63035f2304f2425e5f1fe41c9ee316c17f13e07ceb07c1dfba0eb6f7ff1"
WORKER = "isadoraair-active-message-worker"
REF = "monitoring.0014_provision_active_message_worker_check"
PLAN = [REF]
MIGRATION_SHA256 = {REF: "e87ae5de003d01c857884821106b0d7a618d15e926ec84820fcf072e4a245566"}
SUPERSEDED_MIGRATION_SHA256_PREFIX = "5d10a044"                # never authorized (docs/releases/r0116_readiness.md)
MANUAL = [(REF, 0, "RunPython")]
RESTARTS = ["isadoraair-gunicorn", "isadoraair-monitoring"]
NOTES_COMMIT_FILES = {"docs/releases/r0116.md", "docs/releases/r0116_installation.md"}
RELEASE_COMMIT_FILES = {MANIFEST_PATH, ATTESTATION_PATH, DESCRIPTOR_PATH,
                        "updatecenter/tests/test_active_message_worker_policy.py",
                        "updatecenter/tests/test_r0116_release.py"}
# The engine imports active_messages only through playout and calls exactly these (its spoken.playout
# calls -- authorized_open, lease_intact -- are another, unchanged module); none takes the
# publication destination lock -- the only playout change in r0116 (D3E-C) -- so the engine keeps
# running r0115's code until its own next restart without any interaction with r0116's writers.
ENGINE_PLAYOUT_CALLS = {"admit", "authorize_open", "engine_started", "inspect", "record_insertion",
                        "report_not_aired", "sync_for_log_item", "is_candidate"}


def _git(*args) -> str:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _show(commit: str, path: str) -> bytes:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), "show", f"{commit}:{path}"], check=True,
                          capture_output=True).stdout


def _release_commit() -> str:
    """r0116's immutable introducing commit: the unique addition of its manifest."""
    commits = _git("log", "--full-history", "--no-renames", "--diff-filter=A", "--format=%H", "HEAD", "--",
                   MANIFEST_PATH).split()
    if len(commits) != 1:
        raise AssertionError(f"r0116.json must have exactly one introducing commit, found {commits!r}")
    return commits[0]


def _parent(commit: str) -> str:
    parents = _git("rev-list", "--parents", "-n", "1", commit).split()[1:]
    if len(parents) != 1:
        raise AssertionError(f"{commit} must have exactly one parent, found {parents!r}")
    return parents[0]


def _migration_path(ref: str) -> str:
    app, name = ref.split(".", 1)
    return f"{app}/migrations/{name}.py"


def _exact_plan():
    return probe._serialize_exact_plan(MigrationLoader(None, ignore_no_migrations=True), PLAN)


def _tuple_names(source: str, name: str) -> list[str]:
    """The string members of a module-level ``NAME = (...)`` / ``frozenset({...})`` in source text."""
    match = re.search(rf"^{name}\s*=\s*(?:frozenset\()?[\(\[\{{](.*?)[\)\]\}}]", source, re.M | re.S)
    if match is None:
        raise AssertionError(f"{name} not found")
    return re.findall(r'"([^"]+)"', match.group(1))


class R0116ReleaseArtifactTests(SimpleTestCase):
    """r0116's own artifacts, exactly as committed at its release commit."""

    def setUp(self):
        self.release = _release_commit()
        self.raw = _show(self.release, MANIFEST_PATH)
        self.data = json.loads(self.raw)
        self.manifest = manifest_mod.validate_manifest_dict(self.data, source_label="r0116.json")

    def test_the_manifest_is_exact(self):
        m = self.manifest
        self.assertEqual((m.release_id, m.previous_release_id, m.minimum_updater_protocol_version),
                         ("r0116", "r0115", 5))
        self.assertEqual(list(m.migrations_required), PLAN)
        self.assertEqual(m.migration_compatibility, "additive")
        self.assertEqual(list(m.systemd_units_new_required), [f"{WORKER}.service"])
        for empty in ("apt_packages_new", "systemd_units_changed", "systemd_units_new_optional",
                      "systemd_units_removed_or_renamed"):
            self.assertEqual(self.data[empty], [], empty)
        self.assertEqual(list(m.services_requiring_restart), RESTARTS)
        self.assertEqual((self.data["python_requirements_changed"], self.data["requirements_sha256"],
                          self.data["nginx_changed"], self.data["runtime_components_changed"],
                          self.data["manual_bootstrap_required"], self.data["collectstatic_required"]),
                         (False, None, False, False, False, False))
        self.assertEqual(self.data["minimum_supported_release_id"], "r0081")
        field = m.protected_runtime
        self.assertEqual((field.generation, field.runtime_version, field.manifest_protocol_version,
                          field.supported_wire_protocols, field.minimum_bootstrap_protocol_version),
                         (15, 13, 5, (3, 4), 1))
        self.assertEqual((field.descriptor_path, field.descriptor_sha256), (DESCRIPTOR_PATH, DESCRIPTOR_SHA256))
        self.assertEqual(field.attestations, (ATTESTATION_PATH,))
        self.assertEqual(self.raw, (json.dumps(self.data, indent=2) + "\n").encode())
        self.assertEqual((PROJECT_ROOT / MANIFEST_PATH).read_bytes(), self.raw)     # never edited afterwards

    def test_the_descriptor_is_rebuilt_byte_for_byte_from_the_release_runtime_tree(self):
        raw = _show(self.release, DESCRIPTOR_PATH)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), DESCRIPTOR_SHA256)
        value = json.loads(raw)
        descriptor = parse_descriptor_dict(value, label="r0116 descriptor")
        self.assertEqual(raw, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())
        self.assertEqual((descriptor.generation, descriptor.runtime_version, descriptor.manifest_protocol_version,
                          descriptor.supported_wire_protocols, descriptor.bundle_sha256),
                         (15, 13, 5, (3, 4), BUNDLE_SHA256))
        archive = subprocess.run(["git", "-C", str(PROJECT_ROOT), "archive", "--format=tar", self.release,
                                  "deploy/updater_runtime"], check=True, capture_output=True).stdout
        with tempfile.TemporaryDirectory(prefix="r0116-runtime-") as scratch:
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(scratch, filter="data")
            rebuilt = build_descriptor(runtime_root=Path(scratch) / "deploy" / "updater_runtime", generation=15,
                                       runtime_version=13, manifest_protocol_version=5,
                                       supported_wire_protocols=(3, 4))
        self.assertEqual(rebuilt, raw)
        [policy] = [entry for entry in value["files"] if entry["path"] == "protected-policy.json"]
        self.assertEqual(policy["sha256"], GENERATION_15_POLICY_SHA256)
        package = _show(self.release, "deploy/updater_runtime/isadoraair_updater/__init__.py").decode()
        self.assertIn("\nRUNTIME_VERSION = 13\n", package)
        self.assertIn("\nMANIFEST_PROTOCOL_VERSION = 5\n", package)

    def test_the_signed_statement_and_attestation_shape(self):
        statement = build_statement(descriptor_bytes=_show(self.release, DESCRIPTOR_PATH), release_id="r0116",
                                    previous_release_id="r0115", generation=15)
        self.assertEqual(hashlib.sha256(statement).hexdigest(), STATEMENT_SHA256)
        raw = _show(self.release, ATTESTATION_PATH)
        value = json.loads(raw)
        self.assertEqual(set(value), {"schema_version", "signer_id", "signature_base64"})
        self.assertEqual((value["schema_version"], value["signer_id"]), (1, "primary-release"))
        self.assertEqual(len(base64.b64decode(value["signature_base64"], validate=True)), 64)   # Ed25519
        self.assertEqual(raw, (json.dumps(value, indent=2) + "\n").encode())

    def test_the_lineage_is_single_parent_fast_forwards_of_published_r0115(self):
        notes = _parent(self.release)
        self.assertEqual(_parent(notes), ACCEPTED)
        self.assertEqual(set(_git("diff", "--name-only", ACCEPTED, notes).split()), NOTES_COMMIT_FILES)
        self.assertEqual(set(_git("diff", "--name-only", notes, self.release).split()), RELEASE_COMMIT_FILES)
        for ancestor in (R0115_RELEASE, R0115):
            _git("merge-base", "--is-ancestor", ancestor, self.release)            # raises if not a fast-forward
        self.assertEqual(_parent(R0115), R0115_RELEASE)
        self.assertEqual(_git("rev-list", "--merges", f"{R0115}..{self.release}"), "")
        self.assertEqual(len(_git("rev-list", f"{R0115}..{ACCEPTED}").split()), 31)   # D3E-A..D, accepted

    def test_the_one_migration_is_exactly_what_was_introduced_since_r0115(self):
        added = _git("diff", "--name-only", "--diff-filter=A", R0115, self.release, "--",
                     "*/migrations/0*.py").split()
        self.assertEqual(added, [_migration_path(REF)])
        self.assertEqual(_git("diff", "--name-only", "--diff-filter=MDR", R0115, self.release, "--",
                              "*/migrations/*.py").split(), [])               # no existing migration touched
        for ref, sha in MIGRATION_SHA256.items():
            content = _show(self.release, _migration_path(ref))
            self.assertEqual(hashlib.sha256(content).hexdigest(), sha, ref)
            self.assertEqual(hashlib.sha256((PROJECT_ROOT / _migration_path(ref)).read_bytes()).hexdigest(), sha)
            self.assertFalse(sha.startswith(SUPERSEDED_MIGRATION_SHA256_PREFIX))

    def test_the_real_classifier_finds_exactly_the_one_manual_operation(self):
        plan = _exact_plan()
        self.assertEqual([item["ref"] for item in plan], PLAN)
        self.assertEqual({item["ref"]: item["migration_file_sha256"] for item in plan}, MIGRATION_SHA256)
        self.assertEqual(plan[0]["dependencies"], ["monitoring.0013_backup_recovery_assurance_check"])
        self.assertEqual([(entry["ref"], entry["operation_index"], entry["operation"])
                          for entry in probe.extract_manual_operations(plan)], MANUAL)

    def test_the_new_worker_unit_is_exactly_declared_and_authorized(self):
        changed = set(_git("diff", "--name-only", R0115, self.release, "--", "deploy").split())
        self.assertEqual(changed, {f"deploy/{WORKER}.service", MANIFEST_PATH, ATTESTATION_PATH, DESCRIPTOR_PATH,
                                   "deploy/updater_runtime/isadoraair_updater/__init__.py",
                                   "deploy/updater_runtime/isadoraair_updater/release.py", POLICY_PATH})
        added = set(_git("diff", "--name-only", "--diff-filter=A", R0115, self.release, "--",
                         "deploy/*.service", "deploy/*.timer").split())
        self.assertEqual(added, {f"deploy/{WORKER}.service"})
        policy = json.loads(_show(self.release, POLICY_PATH))
        self.assertIn({"policy": "ENABLE_NOW", "unit": f"{WORKER}.service"}, policy["managed_units"])
        self.assertIs(MANAGED_UNIT_POLICIES[f"{WORKER}.service"], UnitActivationPolicy.ENABLE_NOW)
        template = _show(self.release, f"deploy/{WORKER}.service").decode()
        self.assertIn("ExecStart=@@ISA_ROOT@@/venv/bin/python @@ISA_ROOT@@/manage.py active_message_worker\n",
                      template)
        self.assertIn("User=@@ISA_USER@@\n", template)
        directives = [line for line in template.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        lowered = "\n".join(directives).lower()
        for coupled in ("ogremote", "isadoraair-engine", "encoders", "stereotool", "liquidsoap", "/bin/sh"):
            self.assertNotIn(coupled, lowered)                                # no broadcast-path coupling
        self.assertFalse([line for line in directives if line.startswith(("Requires=", "BindsTo=", "PartOf="))])
        self.assertIn("Wants=isadoraair-render.service isadoraair-validation.service", directives)

    def test_the_restart_set_is_known_to_runtime_12_and_never_the_worker(self):
        """The initial r0116 plan is validated by the INSTALLED runtime 12
        (generation 14) before the handoff: every declared restart must be one
        it orders, and the worker -- unknown to it -- must not be declared
        (it starts once through `enable --now` on r0116 code)."""
        runtime_12 = _show(R0115, "deploy/updater_runtime/isadoraair_updater/release.py").decode()
        runtime_12_order = _tuple_names(runtime_12, "RESTART_ORDER")
        self.assertNotIn(WORKER, runtime_12_order)
        self.assertTrue(set(RESTARTS) <= set(runtime_12_order))
        self.assertEqual(RESTARTS, [name for name in runtime_12_order if name in RESTARTS])
        self.assertNotIn(WORKER, self.data["services_requiring_restart"])
        self.assertTrue(set(RESTARTS) <= CORE_SERVICES)
        self.assertIn(WORKER, CORE_SERVICES)                                  # runtime 13: later releases may
        self.assertEqual(RESTARTS, [name for name in planner.RESTART_ORDER if name in RESTARTS])

    def test_no_broadcast_engine_or_undeclared_surface_changed(self):
        changed = set(_git("diff", "--name-only", R0115, self.release).split())
        for forbidden in ("ogremote/", "encoders/", "hardware/", "spoken/", "weather/", "rbds/", "aircheck/",
                          "library/services/", "deploy/updater_bootstrap/", "requirements.txt",
                          "deploy/isadoraair.nginx"):
            self.assertFalse([path for path in changed if path.startswith(forbidden)], forbidden)
        self.assertFalse([path for path in changed if "/static/" in path])         # hence no collectstatic
        engine = _show(self.release, "library/services/engine.py").decode()
        bare = re.findall(r"from active_messages import playout\n\s+(?:return )?playout\.([a-z_]+)\(", engine)
        self.assertEqual(len(bare), engine.count("from active_messages import playout\n"))
        self.assertEqual(set(re.findall(r"active_playout\.([a-z_]+)\(", engine)) | set(bare), ENGINE_PLAYOUT_CALLS)
        playout = _show(self.release, "active_messages/playout.py").decode()
        for name in ENGINE_PLAYOUT_CALLS:
            body = playout.split(f"\ndef {name}(", 1)[1].split("\ndef ", 1)[0]
            self.assertNotIn("_destination_lock", body, name)

    def test_native_airing_is_unavailable_and_retention_is_not_scheduled(self):
        self.assertIn(b"\nNATIVE_AIRING_AVAILABLE = False\n", _show(self.release, "active_messages/services.py"))
        from active_messages import services
        self.assertIs(services.NATIVE_AIRING_AVAILABLE, False)
        changed = _git("diff", "--name-only", R0115, self.release).split()
        self.assertFalse([path for path in changed if path.endswith(".timer")])  # no retention timer
        self.assertFalse([path for path in changed if "cron" in path])


class R0116CompanionTests(SimpleTestCase):
    """The companion is introduced AFTER the release commit (its own
    metadata-only commit). Before that commit exists this only proves nothing
    stray is there; once present it must be exact for BOTH validators, and the
    runtime's coverage rule must need exactly the one operation."""

    def companion(self):
        if not COMPANION.exists():
            self.skipTest("the r0116 companion is introduced after the release commit")
        return migration_authorization.parse_companion(COMPANION.read_bytes())

    def station(self):
        plan = _exact_plan()
        return {"plan_items": [{"ref": item["ref"], "migration_file_sha256": item["migration_file_sha256"]}
                               for item in plan],
                "manual_operations": probe.extract_manual_operations(plan)}

    def test_the_companion_binds_the_release_and_authorizes_exactly_the_one(self):
        record = self.companion()
        release = _release_commit()
        self.assertEqual((record["release_id"], record["target_commit"]), ("r0116", release))
        self.assertNotIn(record["target_commit"], (ACCEPTED, _git("rev-parse", "HEAD")))
        self.assertEqual(record["manifest_sha256"], hashlib.sha256(_show(release, MANIFEST_PATH)).hexdigest())
        self.assertEqual([(entry["ref"], entry["operation_index"], entry["operation"])
                          for entry in record["authorized_manual_operations"]], MANUAL)
        for entry in record["authorized_manual_operations"]:
            self.assertEqual((entry["migration_file_sha256"], entry["classification"]),
                             (MIGRATION_SHA256[entry["ref"]], "manual"))
        self.assertEqual(set(record), {"schema_version", "release_id", "target_commit", "manifest_sha256",
                                       "authorized_manual_operations"})
        self.assertEqual(COMPANION.read_bytes(), (json.dumps(record, indent=2) + "\n").encode())

    def test_the_companion_commit_is_single_parent_metadata_only_after_the_release(self):
        self.companion()
        introduced = _git("log", "--full-history", "--diff-filter=A", "--format=%H", "HEAD", "--",
                          "deploy/migration_authorizations/r0116.json").split()
        self.assertEqual(len(introduced), 1)
        self.assertEqual(_parent(introduced[0]), _release_commit())
        self.assertEqual(_git("diff", "--name-only", _release_commit(), introduced[0]).split(),
                         ["deploy/migration_authorizations/r0116.json"])

    def test_both_validators_accept_it_at_the_trusted_tip(self):
        self.companion()
        tip, release = _git("rev-parse", "HEAD"), _release_commit()
        expected = frozenset((ref, MIGRATION_SHA256[ref], index, op, "manual") for ref, index, op in MANUAL)
        django = migration_authorization.validate_release_companion(
            PROJECT_ROOT, tip, release_id="r0116", release_commit=release, migrations_required=PLAN)
        repository = TrustedRepository(Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir")),
                                       "unused", "main", CommandRunner())
        entry = {item.manifest.release_id: item for item in load_chain(repository, tip)}["r0116"]
        self.assertEqual(entry.commit, release)
        runtime = validate_release_companion(repository, tip, entry)
        self.assertEqual((django, runtime), (expected, expected))
        for wrong_commit in (ACCEPTED, _parent(release)):                     # bound to THIS release commit only
            with self.subTest(wrong_commit=wrong_commit), self.assertRaises(migration_authorization.CompanionError):
                migration_authorization.validate_release_companion(
                    PROJECT_ROOT, tip, release_id="r0116", release_commit=wrong_commit, migrations_required=PLAN)

    def test_runtime_coverage_needs_the_exact_operation(self):
        record = self.companion()
        station = self.station()
        full = frozenset(manual_operation_identity(entry, entry["migration_file_sha256"])
                         for entry in record["authorized_manual_operations"])
        self.assertTrue(central_authorization_covers({"r0116": full}, **station))
        variants = {
            "nothing authorized": set(),
            "different migration bytes": {(i[0], "0" * 64, *i[2:]) for i in full},
            "the superseded bytes' prefix": {(i[0], SUPERSEDED_MIGRATION_SHA256_PREFIX + "0" * 56, *i[2:])
                                             for i in full},
            "another index": {(i[0], i[1], 1, *i[3:]) for i in full},
            "renamed operation": {(*i[:3], "RunSQL", i[4]) for i in full},
            "r0115's set": set(frozenset(manual_operation_identity(e, e["migration_file_sha256"])
                                         for e in migration_authorization.parse_companion(
                                             (PROJECT_ROOT / "deploy/migration_authorizations/r0115.json")
                                             .read_bytes())["authorized_manual_operations"])),
        }
        for label, identities in variants.items():
            with self.subTest(label):
                self.assertNotEqual(frozenset(identities), full)
                self.assertFalse(central_authorization_covers({"r0116": frozenset(identities)}, **station))
        unexpected = dict(station, manual_operations=station["manual_operations"] + [
            {"ref": REF, "operation_index": 1, "operation": "RunSQL", "classification": "manual",
             "detail": "unexpected"}])
        self.assertFalse(central_authorization_covers({"r0116": full}, **unexpected))   # an unauthorized op
