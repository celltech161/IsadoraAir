"""r0033: supervisor-side trust.py's split between live installed-state
validation (parse_trust_policy_dict, unchanged) and portable recovery-
artifact validation (parse_trust_policy_dict_for_recovery_artifact, new).

Uses only dedicated test-generated Ed25519 keys -- never a production
key, never committed anywhere. Mirrors the worker-side copy's own
ParseTrustPolicyDictTests in test_phase_d1_policy_trust_attestation.py,
plus the os.geteuid() mock convention already established by
test_phase_b_security.py, since neither existing test file exercises
THIS module (isadoraair_updater_bootstrap.trust) at the unit level on
its own."""
from pathlib import Path
import subprocess
import tempfile
from unittest import mock

from django.test import SimpleTestCase

from .phase_b_helpers import BOOTSTRAP_ROOT  # noqa: F401 -- import triggers sys.path setup

from isadoraair_updater_bootstrap.attestation import OPENSSL_BINARY
from isadoraair_updater_bootstrap.trust import (
    TrustPolicyError,
    parse_trust_policy_dict,
    parse_trust_policy_dict_for_recovery_artifact,
)


def _generate_ed25519_keypair(directory: Path, name: str) -> tuple[Path, Path]:
    """A dedicated, throwaway test keypair -- generated fresh per test
    run, never a fixture committed to the repository, and never any
    production key."""
    private_path = directory / f"{name}.key"
    public_path = directory / f"{name}.pem"
    subprocess.run(
        [OPENSSL_BINARY, "genpkey", "-algorithm", "ed25519", "-out", str(private_path)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    subprocess.run(
        [OPENSSL_BINARY, "pkey", "-in", str(private_path), "-pubout", "-out", str(public_path)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    return private_path, public_path


class _TrustFixtureBase(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # An ORDINARY, test-process-owned directory -- exactly the shape
        # of a tempfile.mkdtemp() capture/restore staging tree, never
        # root-owned, matching production's real failure condition.
        self.signer_dir = Path(self.temp.name) / "signers"
        self.signer_dir.mkdir()
        _priv, self.pub = _generate_ed25519_keypair(self.signer_dir, "primary")

    def _valid(self, **overrides):
        data = {
            "schema_version": 1,
            "signature_algorithm": "ed25519",
            "threshold": 1,
            "signers": [{"id": "primary-release", "public_key_path": str(self.pub)}],
        }
        data.update(overrides)
        return data


class ParseTrustPolicyDictLiveModeTests(_TrustFixtureBase):
    """parse_trust_policy_dict (unchanged name/signature) must keep
    enforcing root ownership exactly as before r0033 -- this is the
    real supervisor's own load-bearing activation-time check
    (updater_bootstrapd.py) and installed-state inspection
    (load_installed_phase_d_state)."""

    def test_valid_policy_parses_when_ownership_not_enforced_by_euid(self):
        # Unprivileged test process -- assert_root_protected's own
        # "inactive under euid != 0" convention (same limitation
        # test_phase_b_security.py's own tests document) means this
        # cannot positively prove enforcement runs as root; it proves
        # shape/content parsing is otherwise correct.
        policy = parse_trust_policy_dict(self._valid(), signer_directory=self.signer_dir)
        self.assertEqual(policy.threshold, 1)
        self.assertEqual(len(policy.signers), 1)

    def test_live_mode_still_fails_closed_on_an_unsafe_signer_directory_under_simulated_root(self):
        # The one thing r0033 must NOT have changed: a live-mode caller
        # against an ordinary (non-root-owned) directory must still be
        # rejected once root enforcement is actually active.
        with mock.patch("isadoraair_updater_bootstrap.security.os.geteuid", return_value=0):
            with self.assertRaises(TrustPolicyError) as ctx:
                parse_trust_policy_dict(self._valid(), signer_directory=self.signer_dir)
        self.assertIn("not safely root-protected", str(ctx.exception))

    def test_live_mode_calls_the_ownership_assertions(self):
        with mock.patch(
            "isadoraair_updater_bootstrap.trust.assert_root_protected_parents",
        ) as mocked_parents, mock.patch(
            "isadoraair_updater_bootstrap.trust.assert_root_protected",
        ) as mocked_self:
            parse_trust_policy_dict(self._valid(), signer_directory=self.signer_dir)
        mocked_parents.assert_called_once()
        mocked_self.assert_called_once()

    def test_wrong_algorithm_rejected(self):
        with self.assertRaises(TrustPolicyError):
            parse_trust_policy_dict(self._valid(signature_algorithm="rsa"), signer_directory=self.signer_dir)

    def test_threshold_zero_rejected(self):
        with self.assertRaises(TrustPolicyError):
            parse_trust_policy_dict(self._valid(threshold=0), signer_directory=self.signer_dir)

    def test_duplicate_signer_id_rejected(self):
        data = self._valid()
        data["signers"].append({"id": "primary-release", "public_key_path": str(self.pub)})
        with self.assertRaises(TrustPolicyError):
            parse_trust_policy_dict(data, signer_directory=self.signer_dir)

    def test_key_path_outside_signer_directory_rejected(self):
        data = self._valid()
        data["signers"][0]["public_key_path"] = "/etc/passwd"
        with self.assertRaises(TrustPolicyError):
            parse_trust_policy_dict(data, signer_directory=self.signer_dir)


class ParseTrustPolicyDictRecoveryArtifactModeTests(_TrustFixtureBase):
    """parse_trust_policy_dict_for_recovery_artifact (r0033, new): the
    entry point every Phase-D recovery-artifact caller
    (capture/attach/restore, all via validate_phase_d_component) must
    use. Deliberately skips ONLY the root-ownership ancestry check --
    every other rule stays identical to the live-mode function above."""

    def test_valid_artifact_parses_under_an_ordinary_non_root_owned_directory(self):
        policy = parse_trust_policy_dict_for_recovery_artifact(
            self._valid(), signer_directory=self.signer_dir,
        )
        self.assertEqual(policy.threshold, 1)
        self.assertEqual(len(policy.signers), 1)

    def test_valid_artifact_parses_even_under_simulated_root(self):
        # The defining behavior this function exists for: an ordinary,
        # non-root-owned staging directory must succeed regardless of
        # euid, because "is this live installed state" is simply the
        # wrong question for a portable artifact.
        with mock.patch("isadoraair_updater_bootstrap.security.os.geteuid", return_value=0):
            policy = parse_trust_policy_dict_for_recovery_artifact(
                self._valid(), signer_directory=self.signer_dir,
            )
        self.assertEqual(policy.threshold, 1)

    def test_artifact_mode_never_calls_the_ownership_assertions(self):
        with mock.patch(
            "isadoraair_updater_bootstrap.trust.assert_root_protected_parents",
        ) as mocked_parents, mock.patch(
            "isadoraair_updater_bootstrap.trust.assert_root_protected",
        ) as mocked_self:
            parse_trust_policy_dict_for_recovery_artifact(self._valid(), signer_directory=self.signer_dir)
        mocked_parents.assert_not_called()
        mocked_self.assert_not_called()

    def test_relative_signer_directory_still_rejected(self):
        with self.assertRaises(TrustPolicyError):
            parse_trust_policy_dict_for_recovery_artifact(
                self._valid(), signer_directory=Path("relative/signers"),
            )

    def test_wrong_algorithm_still_rejected_in_artifact_mode(self):
        with self.assertRaises(TrustPolicyError):
            parse_trust_policy_dict_for_recovery_artifact(
                self._valid(signature_algorithm="rsa"), signer_directory=self.signer_dir,
            )

    def test_threshold_out_of_range_still_rejected_in_artifact_mode(self):
        with self.assertRaises(TrustPolicyError):
            parse_trust_policy_dict_for_recovery_artifact(
                self._valid(threshold=0), signer_directory=self.signer_dir,
            )
        with self.assertRaises(TrustPolicyError):
            parse_trust_policy_dict_for_recovery_artifact(
                self._valid(threshold=2), signer_directory=self.signer_dir,
            )

    def test_duplicate_signer_id_still_rejected_in_artifact_mode(self):
        data = self._valid()
        data["signers"].append({"id": "primary-release", "public_key_path": str(self.pub)})
        with self.assertRaises(TrustPolicyError):
            parse_trust_policy_dict_for_recovery_artifact(data, signer_directory=self.signer_dir)

    def test_too_many_signers_still_rejected_in_artifact_mode(self):
        data = self._valid()
        data["signers"] = [
            {"id": f"signer-{i}", "public_key_path": str(self.pub)} for i in range(20)
        ]
        data["threshold"] = 1
        with self.assertRaises(TrustPolicyError):
            parse_trust_policy_dict_for_recovery_artifact(data, signer_directory=self.signer_dir)

    def test_key_path_outside_signer_directory_still_rejected_in_artifact_mode(self):
        data = self._valid()
        data["signers"][0]["public_key_path"] = "/etc/passwd"
        with self.assertRaises(TrustPolicyError):
            parse_trust_policy_dict_for_recovery_artifact(data, signer_directory=self.signer_dir)

    def test_key_path_with_dotdot_still_rejected_in_artifact_mode(self):
        data = self._valid()
        data["signers"][0]["public_key_path"] = str(self.signer_dir / ".." / "escape.pem")
        with self.assertRaises(TrustPolicyError):
            parse_trust_policy_dict_for_recovery_artifact(data, signer_directory=self.signer_dir)

    def test_malformed_schema_still_rejected_in_artifact_mode(self):
        with self.assertRaises(TrustPolicyError):
            parse_trust_policy_dict_for_recovery_artifact("not-a-dict", signer_directory=self.signer_dir)
        with self.assertRaises(TrustPolicyError):
            parse_trust_policy_dict_for_recovery_artifact(
                {**self._valid(), "extra": 1}, signer_directory=self.signer_dir,
            )

    def test_live_and_artifact_mode_agree_on_a_valid_policy(self):
        # Parity: identical output for identical valid input -- the only
        # difference between the two entry points is the one ownership
        # check, never the parsed result.
        live = parse_trust_policy_dict(self._valid(), signer_directory=self.signer_dir)
        artifact = parse_trust_policy_dict_for_recovery_artifact(
            self._valid(), signer_directory=self.signer_dir,
        )
        self.assertEqual(live, artifact)


class NoLiveCallerDisablesRootOwnershipEnforcementTests(SimpleTestCase):
    """Static proof that every production call site of the SUPERVISOR-
    side trust module either uses the unchanged, still fully-enforced
    parse_trust_policy_dict (live state) or the new artifact-only
    entry point (never a boolean/opt-out on the live function itself,
    which does not exist)."""

    def test_updater_bootstrapd_uses_the_live_enforced_entry_point(self):
        text = (BOOTSTRAP_ROOT / "updater_bootstrapd.py").read_text(encoding="utf-8")
        self.assertIn("parse_trust_policy_dict", text)
        self.assertNotIn("parse_trust_policy_dict_for_recovery_artifact", text)

    def test_load_installed_phase_d_state_uses_the_live_enforced_entry_point(self):
        project_root = BOOTSTRAP_ROOT.parent.parent
        text = (project_root / "isadoraair" / "phase_d_recovery.py").read_text(encoding="utf-8")
        # load_installed_phase_d_state's own import line -- the artifact
        # entry point appears too (validate_phase_d_component's import),
        # so anchor on the specific line rather than a bare substring.
        self.assertIn(
            "from isadoraair_updater_bootstrap.trust import TrustPolicyError, parse_trust_policy_dict\n",
            text,
        )

    def test_parse_trust_policy_dict_signature_has_no_ownership_opt_out(self):
        import inspect

        from isadoraair_updater_bootstrap.trust import parse_trust_policy_dict as live_fn

        parameters = inspect.signature(live_fn).parameters
        self.assertNotIn("enforce_root_ownership", parameters)
