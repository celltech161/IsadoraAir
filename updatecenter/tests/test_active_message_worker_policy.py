"""D3E-B: protected-runtime authority for isadoraair-active-message-worker.service.

The worker becomes a managed unit ONLY through the signed protected policy of
a candidate runtime generation (the r0082 / r0107 / r0109 pattern): this
branch adds exactly one entry, ``isadoraair-active-message-worker.service:
ENABLE_NOW``, to deploy/updater_runtime/protected-policy.json -- the policy
SOURCE of the next generation. It is UNSIGNED PREPARATION: the
generation-15 descriptor, the release manifest and the primary-release
attestation are produced by the r0116 release packaging with the real
signing workflow, never here. The only other runtime change is runtime 13's
release-driven restart authority (CORE_SERVICES / RESTART_ORDER, correction
B2, test_active_message_worker_restart), which -- as r0112's did -- also seeds
the compiled fallback map; that map is never consulted while a signed policy
is active.

Proved against the real updater code (isadoraair_updater, protected_bootstrap)
with test keys and temporary repositories -- nothing is installed:

* the policy source: generation 14 (as r0115 runs it) plus exactly the one
  entry; canonical; every entry has a template;
* refusal without authority: the active generation-14 policy refuses to
  install the unit; an ordinary plan naming
  it is blocked UNKNOWN_MANAGED_UNIT; a candidate that does not authorize it,
  or a manifest that does not declare it, is refused;
* the candidate path: a candidate bundle built from THIS runtime tree and
  signed with a trusted (test) key verifies and authorizes exactly the
  worker; tampered, unsigned, wrongly signed and replayed candidates are
  refused and their policy is never trusted;
* the predecessor diff: the template must be declared exactly (new, not
  changed; present in the commit);
* activation: ENABLE_NOW is `enable --now` + verification and nothing else;
  declared optional it is never installed; every existing unit keeps its
  policy; the worker is not restartable through the updater;
* a failed activation fails the job before any declared restart and touches
  no other unit (no OGRemote, engine or Weather unit is managed by it).
"""
from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from .phase_b_helpers import RUNTIME_ROOT, config_dict, create_release_repository

from deploy.updater_bootstrap.tools.protected_runtime_release import build_descriptor, build_statement, sign_statement
from isadoraair_updater import HANDOFF_WIRE_PROTOCOL, RUNTIME_VERSION
from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.process import CommandRunner, ProcessResult
from isadoraair_updater.release import (
    CORE_SERVICES, KNOWN_MANAGED_UNITS, MANAGED_UNIT_POLICIES, RESTART_ORDER, ReleaseError, TrustedRepository,
    UnitActivationPolicy, derive_plan, manual_blockers, resolve_known_managed_units, resolve_unit_policy,
)
from isadoraair_updater.runtime_handoff import (
    verify_candidate_independently, verify_new_units_authorized_by_candidate_policy,
)
from isadoraair_updater.systemd import SystemdError, SystemdManager
from protected_bootstrap.policy import parse_policy_dict
from protected_bootstrap.trust import parse_trust_policy_dict
from updatecenter import manifest as manifest_mod
from updatecenter import planner

from .test_phase_b_systemd_daemon import FakeSystemRunner, plan

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER = "isadoraair-active-message-worker.service"
R0115 = "29f6a3353e41f659e1e1fbe3a46859bed671cad7"         # generation 14 policy, as installed today
GENERATION_14_POLICY_SHA256 = "085c8d57ca7110efd12f1d1ccdea96531c0b8ffce3faaac3738802e069b0dcce"
POLICY_PATH = "deploy/updater_runtime/protected-policy.json"
DESCRIPTOR_PATH = "deploy/updater_runtime/protected-runtime-descriptor.json"


def _git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], check=True, capture_output=True).stdout


def _keypair(directory: Path, name: str):
    private, public = directory / f"{name}.key", directory / f"{name}.pem"
    subprocess.run(["/usr/bin/openssl", "genpkey", "-algorithm", "ed25519", "-out", str(private)], check=True,
                   capture_output=True)
    private.chmod(0o600)
    subprocess.run(["/usr/bin/openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)], check=True,
                   capture_output=True)
    return private, public


GENERATION_14 = parse_policy_dict(json.loads(_git("show", f"{R0115}:{POLICY_PATH}")), label="generation 14")
CANDIDATE_RAW = (PROJECT_ROOT / POLICY_PATH).read_bytes()
CANDIDATE = parse_policy_dict(json.loads(CANDIDATE_RAW), label="candidate policy source")


class PolicySourceTests(SimpleTestCase):
    def test_generation_14_is_what_r0115_runs(self):
        self.assertEqual(hashlib.sha256(_git("show", f"{R0115}:{POLICY_PATH}")).hexdigest(),
                         GENERATION_14_POLICY_SHA256)
        self.assertNotIn(WORKER, GENERATION_14.as_mapping())

    def test_exactly_one_entry_is_added(self):
        before, after = GENERATION_14.as_mapping(), CANDIDATE.as_mapping()
        self.assertEqual(set(after) - set(before), {WORKER})
        self.assertEqual(after[WORKER], "ENABLE_NOW")
        self.assertEqual({unit: policy for unit, policy in after.items() if unit != WORKER}, before)

    def test_canonical_and_every_entry_has_a_template(self):
        value = json.loads(CANDIDATE_RAW)
        self.assertEqual(CANDIDATE_RAW, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())
        units = [entry["unit"] for entry in value["managed_units"]]
        self.assertEqual(units, sorted(units))
        for unit in units:
            self.assertTrue((PROJECT_ROOT / "deploy" / unit).is_file(), unit)

    def test_unsigned_preparation_only(self):
        """The descriptor still describes generation 14 (its policy hash is the
        installed one); no r0116 manifest or attestation is fabricated here."""
        descriptor = json.loads((PROJECT_ROOT / DESCRIPTOR_PATH).read_text())
        self.assertEqual(descriptor["generation"], 14)
        [entry] = [item for item in descriptor["files"] if item["path"] == "protected-policy.json"]
        self.assertEqual(entry["sha256"], GENERATION_14_POLICY_SHA256)
        self.assertNotEqual(hashlib.sha256(CANDIDATE_RAW).hexdigest(), GENERATION_14_POLICY_SHA256)
        self.assertFalse((PROJECT_ROOT / "deploy" / "releases" / "r0116.json").exists())
        self.assertFalse(list((PROJECT_ROOT / "deploy" / "updater_attestations").glob("r0116*")))

    def test_the_protected_runtime_changes_are_exactly_policy_and_restart_authority(self):
        """Generation 15's runtime differs from generation 14's by the policy entry
        and runtime 13's restart authority (release.py CORE_SERVICES /
        RESTART_ORDER, __init__.py RUNTIME_VERSION) -- see
        test_active_message_worker_restart."""
        tracked = _git("ls-files", "deploy/updater_runtime").decode().split()
        changed = sorted(path for path in tracked
                         if (PROJECT_ROOT / path).read_bytes() != _git("show", f"{R0115}:{path}"))
        self.assertEqual(changed, ["deploy/updater_runtime/isadoraair_updater/__init__.py",
                                   "deploy/updater_runtime/isadoraair_updater/release.py", POLICY_PATH])
        service = WORKER.removesuffix(".service")
        self.assertIs(MANAGED_UNIT_POLICIES[WORKER], UnitActivationPolicy.ENABLE_NOW)
        self.assertIn(service, CORE_SERVICES)
        self.assertIn(service, RESTART_ORDER)
        self.assertEqual(manifest_mod.CORE_RESTARTABLE_SERVICES, CORE_SERVICES)
        self.assertEqual(planner.RESTART_ORDER, RESTART_ORDER)

    def test_wildcards_paths_templates_and_unknown_policies_are_refused(self):
        for unit in ("isadoraair-active-message-*.service", "../isadoraair-active-message-worker.service",
                     "deploy/isadoraair-active-message-worker.service", "isadoraair-active-message-worker@.service",
                     "isadoraair-active-message-worker"):
            with self.subTest(unit=unit), self.assertRaises(ValueError):
                parse_policy_dict({"schema_version": 1, "managed_units": [{"unit": unit, "policy": "ENABLE_NOW"}]},
                                  label="hostile")
        for policy in ("ENABLE_AND_RESTART", "enable_now", "*", ""):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                parse_policy_dict({"schema_version": 1, "managed_units": [{"unit": WORKER, "policy": policy}]},
                                  label="hostile")


class Station(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = validate_config_dict(config_dict(self.root, str(self.root / "upstream.git")),
                                           allow_local_repository=True)
        self.source = self.root / "source"
        (self.source / "deploy").mkdir(parents=True)
        shutil.copyfile(PROJECT_ROOT / "deploy" / WORKER, self.source / "deploy" / WORKER)

    def manager(self, runner, policy):
        return SystemdManager(self.config, runner, enforce_root_ownership=False, signed_policy=policy)


class RefusalWithoutAuthorityTests(Station):
    def test_the_active_generation_14_policy_refuses_it(self):
        """The active signed policy decides membership. Runtime 13's compiled
        fallback names the worker (CORE_SERVICES seeds it, the r0112 precedent),
        but that map is consulted only with NO active signed policy -- never on a
        Phase-D station, whose generation-14 policy refuses the unit outright."""
        runner = FakeSystemRunner()
        self.assertNotIn(WORKER, resolve_known_managed_units(active_policy=GENERATION_14))
        with self.assertRaisesRegex(SystemdError, "outside the installed updater allowlist"):
            self.manager(runner, GENERATION_14).reconcile(self.source, plan(systemd_units_new_required=(WORKER,)))
        self.assertEqual(runner.calls, [])                                       # nothing ran
        self.assertFalse((self.config.systemd_unit_root / WORKER).exists())       # nothing installed
        self.assertIn(WORKER, resolve_known_managed_units(active_policy=None))    # D0/legacy fallback only

    def test_an_ordinary_plan_naming_it_is_blocked(self):
        ordinary = plan(systemd_units_new_required=(WORKER,))
        self.assertIsNone(ordinary.protected_runtime)
        known = resolve_known_managed_units(active_policy=GENERATION_14)
        self.assertIn("UNKNOWN_MANAGED_UNIT", manual_blockers(ordinary, known_units=known))
        # declared optional it is never installed, so it is reported only
        self.assertNotIn("UNKNOWN_MANAGED_UNIT",
                         manual_blockers(plan(systemd_units_new_optional=(WORKER,)), known_units=known))

    def test_the_candidate_policy_must_authorize_it_and_the_manifest_must_declare_it(self):
        needed = frozenset({WORKER}) - resolve_known_managed_units(active_policy=GENERATION_14)
        self.assertEqual(needed, {WORKER})
        cases = {
            "no candidate policy": (None, {WORKER}),
            "candidate without it (generation 14 again)": (GENERATION_14, {WORKER}),
            "manifest never declared it": (CANDIDATE, set()),
            "manifest declared another unit": (CANDIDATE, {"isadoraair-render.service"}),
        }
        for label, (candidate, declared) in cases.items():
            with self.subTest(label):
                self.assertTrue(verify_new_units_authorized_by_candidate_policy(
                    needed_units=needed, manifest_declared_units=frozenset(declared), candidate_policy=candidate))
        self.assertEqual(verify_new_units_authorized_by_candidate_policy(
            needed_units=needed, manifest_declared_units=frozenset({WORKER}), candidate_policy=CANDIDATE), ())


class CandidateVerificationTests(SimpleTestCase):
    """A candidate built from THIS runtime tree, signed with a trusted TEST key
    (never the station's), through the worker's own independent verification."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        signers = root / "signers"
        signers.mkdir()
        self.private, self.public = _keypair(signers, "primary")
        self.trust = parse_trust_policy_dict(
            {"schema_version": 1, "signature_algorithm": "ed25519", "threshold": 1,
             "signers": [{"id": "primary-release", "public_key_path": str(self.public)}]},
            signer_directory=signers)
        self.other_private, _ = _keypair(root, "untrusted")
        self.bundle = root / "bundle"
        for relative in _git("ls-files", "deploy/updater_runtime").decode().split():
            name = Path(relative).relative_to("deploy/updater_runtime")
            if name.as_posix() == "protected-runtime-descriptor.json":
                continue
            destination = self.bundle / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PROJECT_ROOT / relative, destination)
            destination.chmod(0o755 if name.as_posix() == "updaterd.py" else 0o644)
        self.attestations = root / "attestations"
        self.attestations.mkdir()

    def stage(self, *, generation=15, key=None, sign=True):
        descriptor = build_descriptor(runtime_root=self.bundle, generation=generation,
                                      runtime_version=RUNTIME_VERSION,
                                      manifest_protocol_version=5, supported_wire_protocols=(3, 4))
        if sign:
            statement = build_statement(descriptor_bytes=descriptor, release_id="r0116", previous_release_id="r0115",
                                        generation=generation)
            if key is None:
                signature = sign_statement(statement=statement, private_key_path=self.private,
                                           public_key_path=self.public)
            else:
                with tempfile.NamedTemporaryFile() as handle:
                    handle.write(statement)
                    handle.flush()
                    signature = subprocess.run(["/usr/bin/openssl", "pkeyutl", "-sign", "-inkey", str(key), "-rawin",
                                                "-in", handle.name], check=True, capture_output=True).stdout
            (self.attestations / "00.json").write_text(json.dumps({
                "schema_version": 1, "signer_id": "primary-release",
                "signature_base64": base64.b64encode(signature).decode()}))
        return descriptor

    def verify(self, descriptor, previous_generation=14):
        return verify_candidate_independently(
            trust_policy=self.trust, descriptor_bytes=descriptor, bundle_root=self.bundle,
            attestations_dir=self.attestations, release_id="r0116", previous_release_id="r0115",
            previous_generation=previous_generation, current_bootstrap_protocol_version=1,
            current_wire_protocol_version=HANDOFF_WIRE_PROTOCOL)

    def test_a_valid_signed_candidate_authorizes_exactly_the_worker(self):
        outcome = self.verify(self.stage())
        self.assertTrue(outcome.ok, outcome.reasons)
        self.assertEqual(outcome.candidate_policy.as_mapping(), CANDIDATE.as_mapping())
        needed = frozenset({WORKER}) - resolve_known_managed_units(active_policy=GENERATION_14)
        self.assertEqual(verify_new_units_authorized_by_candidate_policy(
            needed_units=needed, manifest_declared_units=frozenset({WORKER}),
            candidate_policy=outcome.candidate_policy), ())
        self.assertIs(resolve_unit_policy(WORKER, signed_policy=outcome.candidate_policy),
                      UnitActivationPolicy.ENABLE_NOW)

    def test_invalid_candidates_are_refused_and_their_policy_never_trusted(self):
        cases = {}
        descriptor = self.stage()
        (self.bundle / "protected-policy.json").write_bytes(CANDIDATE_RAW.replace(b"ENABLE_NOW", b"INSTALL_ONLY", 1))
        cases["policy edited after signing"] = self.verify(descriptor)
        (self.bundle / "protected-policy.json").write_bytes(CANDIDATE_RAW)
        cases["signed by an untrusted key"] = self.verify(self.stage(key=self.other_private))
        (self.attestations / "00.json").unlink()
        cases["unsigned"] = self.verify(self.stage(sign=False))
        cases["replayed generation"] = self.verify(self.stage(generation=14))
        descriptor = self.stage()
        (self.bundle / "updaterd.py").write_bytes(b"import sys\n")
        cases["incomplete / altered bundle"] = self.verify(descriptor)
        for label, outcome in cases.items():
            with self.subTest(label):
                self.assertFalse(outcome.ok)
                self.assertIsNone(outcome.candidate_policy)
                self.assertTrue(verify_new_units_authorized_by_candidate_policy(
                    needed_units=frozenset({WORKER}), manifest_declared_units=frozenset({WORKER}),
                    candidate_policy=outcome.candidate_policy))


class ManifestDiffTests(SimpleTestCase):
    """The release manifest's unit declarations against the predecessor diff."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.template = (PROJECT_ROOT / "deploy" / WORKER).read_text()

    def derive(self, name, **kwargs):
        _author, upstream, _bootstrap, r0002, _r0003 = create_release_repository(self.root / name, **kwargs)
        repository = TrustedRepository(self.root / f"{name}.git", str(upstream), "main", CommandRunner())
        return derive_plan(repository, repository.fetch(), r0002, "r0003",
                           known_units=resolve_known_managed_units(active_policy=GENERATION_14))

    def test_declared_new_and_present(self):
        result = self.derive("declared", third_release_changes={"systemd_units_new_required": [WORKER]},
                             third_release_files={f"deploy/{WORKER}": self.template})
        self.assertEqual(result.systemd_units_new_required, (WORKER,))
        self.assertEqual(result.systemd_units_changed, ())
        # without a protected-runtime transition it still cannot be installed
        self.assertIn("UNKNOWN_MANAGED_UNIT",
                      manual_blockers(result, known_units=resolve_known_managed_units(active_policy=GENERATION_14)))

    def test_undeclared_or_misdeclared_templates_are_refused(self):
        with self.assertRaisesRegex(ReleaseError, "systemd unit intent"):
            self.derive("undeclared", third_release_files={f"deploy/{WORKER}": self.template})
        with self.assertRaisesRegex(ReleaseError, "systemd unit intent"):
            self.derive("as-changed", third_release_changes={"systemd_units_changed": [WORKER]},
                        third_release_files={f"deploy/{WORKER}": self.template})


class ActivationTests(Station):
    def test_enable_now_is_exactly_install_reload_enable_now_and_verify(self):
        runner = FakeSystemRunner()
        result = self.manager(runner, CANDIDATE).reconcile(self.source, plan(systemd_units_new_required=(WORKER,)))
        self.assertEqual(result["enabled"], [WORKER])
        self.assertEqual(result["installed_only"], [])
        self.assertEqual(runner.calls, [
            ["/usr/bin/systemctl", "daemon-reload"],
            ["/usr/bin/systemctl", "enable", "--now", WORKER],
            ["/usr/bin/systemctl", "show", WORKER, "--property=Type", "--property=ActiveState",
             "--property=SubState", "--property=Result"],
        ])
        installed = (self.config.systemd_unit_root / WORKER).read_text()
        self.assertNotIn("@@", installed)
        self.assertIn(f"User={self.config.render_values['isa_user']}\n", installed)
        self.assertIn(f"ExecStart={self.config.render_values['isa_root']}/venv/bin/python "
                      f"{self.config.render_values['isa_root']}/manage.py active_message_worker\n", installed)
        self.assertIn("--pid ${MAINPID} --timeout 45\n", installed)        # systemd's variable, not a token

    def test_declared_optional_it_is_never_installed(self):
        runner = FakeSystemRunner()
        result = self.manager(runner, CANDIDATE).reconcile(self.source, plan(systemd_units_new_optional=(WORKER,)))
        self.assertEqual((result["enabled"], result["optional_report_only"]), ([], [WORKER]))
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.config.systemd_unit_root / WORKER).exists())

    def test_every_existing_unit_keeps_its_policy(self):
        for unit in GENERATION_14.as_mapping():
            with self.subTest(unit=unit):
                self.assertIs(resolve_unit_policy(unit, signed_policy=CANDIDATE),
                              resolve_unit_policy(unit, signed_policy=GENERATION_14))
        self.assertEqual(KNOWN_MANAGED_UNITS, frozenset(MANAGED_UNIT_POLICIES))
        self.assertTrue(resolve_known_managed_units(active_policy=GENERATION_14)
                        < resolve_known_managed_units(active_policy=CANDIDATE))

    def test_release_driven_restart_only_never_an_operator_restart(self):
        """Runtime 13 (correction B2): a trusted plan may restart it
        (test_active_message_worker_restart); the operator/Monitoring path may not."""
        manager = self.manager(FakeSystemRunner(), CANDIDATE)
        self.assertEqual(manager.restart_declared(("isadoraair-active-message-worker",)),
                         ["isadoraair-active-message-worker"])
        self.assertNotIn(WORKER, self.config.operator_restart_units)
        with self.assertRaisesRegex(SystemdError, "operator restart allowlist"):
            manager.restart_operator_service(WORKER)

    def test_a_failed_start_fails_the_job_and_touches_nothing_else(self):
        class StartFails(FakeSystemRunner):
            def run(self, argv, **kwargs):
                if "enable" in argv:
                    self.calls.append(list(argv))
                    return ProcessResult(tuple(argv), 1, b"", b"Job failed")
                return super().run(argv, **kwargs)

        runner = StartFails()
        with self.assertRaisesRegex(SystemdError, "systemctl enable failed"):
            self.manager(runner, CANDIDATE).reconcile(self.source, plan(systemd_units_new_required=(WORKER,),
                                                                        services_requiring_restart=("isadoraair-gunicorn",)))
        self.assertEqual([call[1:] for call in runner.calls], [["daemon-reload"], ["enable", "--now", WORKER]])
        touched = {argument for call in runner.calls for argument in call if argument.endswith((".service", ".timer"))}
        self.assertEqual(touched, {WORKER})

    def test_no_broadcast_unit_is_in_its_authority(self):
        for unit in CANDIDATE.as_mapping():
            self.assertFalse(unit.startswith(("ogremote", "syndicated-")), unit)
        self.assertEqual(set(CANDIDATE.as_mapping()) - set(GENERATION_14.as_mapping()), {WORKER})
