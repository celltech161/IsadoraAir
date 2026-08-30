"""D2-A: supervisor bootstrap configuration."""
from pathlib import Path
import tempfile

from django.test import SimpleTestCase

from .phase_b_helpers import BOOTSTRAP_ROOT  # noqa: F401 -- import triggers sys.path setup

from isadoraair_updater_bootstrap.config import ConfigError, validate_config_dict


def _valid_config(**overrides):
    data = {
        "schema_version": 1,
        "bootstrap_protocol_version": 1,
        "slots_root": "/var/lib/isadoraair-updater-bootstrap/runtime-slots",
        "runtime_state_path": "/var/lib/isadoraair-updater-bootstrap/runtime-state.json",
        "activation_socket": "/run/isadoraair-updater-bootstrap/control.sock",
        "worker_socket": "/run/isadoraair-updater/updater.sock",
        "signer_root": "/etc/isadoraair/updater-signers",
        "trust_policy_path": "/etc/isadoraair/updater-trust.json",
    }
    data.update(overrides)
    return data


class ValidateConfigDictTests(SimpleTestCase):
    def setUp(self):
        self.application_root = Path("/home/jreed/isadoraair-django")

    def test_valid_config_parses(self):
        config = validate_config_dict(
            _valid_config(), application_root=self.application_root, enforce_root_ownership=False,
        )
        self.assertEqual(config.bootstrap_protocol_version, 1)
        self.assertEqual(config.slots_root, Path("/var/lib/isadoraair-updater-bootstrap/runtime-slots"))

    def test_unknown_field_rejected(self):
        data = _valid_config()
        data["extra"] = 1
        with self.assertRaises(ConfigError):
            validate_config_dict(data, application_root=self.application_root, enforce_root_ownership=False)

    def test_missing_field_rejected(self):
        data = _valid_config()
        del data["worker_socket"]
        with self.assertRaises(ConfigError):
            validate_config_dict(data, application_root=self.application_root, enforce_root_ownership=False)

    def test_no_command_or_hook_field_can_ever_be_declared(self):
        for forbidden in ("command", "commands", "hook", "hooks", "exec", "entrypoint", "python_path", "argv"):
            data = _valid_config()
            data[forbidden] = "/bin/sh"
            with self.subTest(field=forbidden):
                with self.assertRaises(ConfigError):
                    validate_config_dict(data, application_root=self.application_root, enforce_root_ownership=False)

    def test_relative_path_rejected(self):
        data = _valid_config(slots_root="relative/path")
        with self.assertRaises(ConfigError):
            validate_config_dict(data, application_root=self.application_root, enforce_root_ownership=False)

    def test_dotdot_in_path_rejected(self):
        data = _valid_config(slots_root="/var/lib/isadoraair-updater-bootstrap/../etc")
        with self.assertRaises(ConfigError):
            validate_config_dict(data, application_root=self.application_root, enforce_root_ownership=False)

    def test_path_overlapping_application_root_rejected(self):
        data = _valid_config(slots_root=str(self.application_root / "runtime-slots"))
        with self.assertRaises(ConfigError):
            validate_config_dict(data, application_root=self.application_root, enforce_root_ownership=False)

    def test_application_root_itself_rejected_as_a_configured_path(self):
        data = _valid_config(slots_root=str(self.application_root))
        with self.assertRaises(ConfigError):
            validate_config_dict(data, application_root=self.application_root, enforce_root_ownership=False)

    def test_application_root_nested_under_a_configured_path_rejected(self):
        nested_app_root = self.application_root / "nested"
        data = _valid_config(slots_root=str(self.application_root))
        with self.assertRaises(ConfigError):
            validate_config_dict(data, application_root=nested_app_root, enforce_root_ownership=False)

    def test_two_identical_configured_paths_rejected(self):
        data = _valid_config(worker_socket=_valid_config()["activation_socket"])
        with self.assertRaises(ConfigError):
            validate_config_dict(data, application_root=self.application_root, enforce_root_ownership=False)

    def test_one_configured_path_nested_under_another_rejected(self):
        data = _valid_config(runtime_state_path="/var/lib/isadoraair-updater-bootstrap/runtime-slots/state.json")
        with self.assertRaises(ConfigError):
            validate_config_dict(data, application_root=self.application_root, enforce_root_ownership=False)

    def test_oversized_path_rejected(self):
        data = _valid_config(slots_root="/" + "a" * 300)
        with self.assertRaises(ConfigError):
            validate_config_dict(data, application_root=self.application_root, enforce_root_ownership=False)

    def test_control_character_in_path_rejected(self):
        data = _valid_config(slots_root="/var/lib/bad\x01path")
        with self.assertRaises(ConfigError):
            validate_config_dict(data, application_root=self.application_root, enforce_root_ownership=False)

    def test_root_ownership_enforced_when_requested(self):
        # Unprivileged test process -- security.assert_root_protected's
        # own "inactive under euid != 0" convention means this cannot
        # positively prove enforcement runs as root here, but proves
        # the enforce_root_ownership=True path does not itself error
        # out for an ordinary user-owned temp directory (matches the
        # rest of this codebase's own established test convention).
        with tempfile.TemporaryDirectory() as tmp:
            data = _valid_config(
                slots_root=f"{tmp}/slots", runtime_state_path=f"{tmp}/state.json",
                activation_socket=f"{tmp}/control.sock", worker_socket=f"{tmp}/worker.sock",
                signer_root=f"{tmp}/signers", trust_policy_path=f"{tmp}/trust.json",
            )
            validate_config_dict(data, application_root=self.application_root, enforce_root_ownership=True)

    def test_unsupported_schema_version_rejected(self):
        with self.assertRaises(ConfigError):
            validate_config_dict(
                _valid_config(schema_version=2), application_root=self.application_root, enforce_root_ownership=False,
            )
