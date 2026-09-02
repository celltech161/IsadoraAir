"""D2-A: supervisor bootstrap configuration."""
import os
from pathlib import Path
import socket
import tempfile
from unittest import mock

from django.test import SimpleTestCase

from .phase_b_helpers import BOOTSTRAP_ROOT  # noqa: F401 -- import triggers sys.path setup

from isadoraair_updater_bootstrap.config import (
    ConfigError,
    _closest_existing_ancestor,
    _is_unclassifiable_special_file,
    validate_config_dict,
)


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


class ClosestExistingAncestorTests(SimpleTestCase):
    """r0032: activation_socket/worker_socket name a live Unix domain
    socket while the supervisor is running. _closest_existing_ancestor
    must not stop there -- a socket satisfies neither S_ISDIR nor
    S_ISREG, so assert_root_protected can never classify it. These
    exercise the helper directly: it has no os.geteuid() gate, so its
    ancestor-resolution behavior is fully provable unprivileged, unlike
    the ownership/mode check that consumes its result."""

    def test_missing_path_walks_to_closest_existing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist" / "control.sock"
            self.assertEqual(_closest_existing_ancestor(missing), Path(tmp))

    def test_regular_file_is_returned_as_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "trust.json"
            target.write_text("{}", encoding="utf-8")
            self.assertEqual(_closest_existing_ancestor(target), target)

    def test_directory_is_returned_as_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "slots"
            target.mkdir()
            self.assertEqual(_closest_existing_ancestor(target), target)

    def test_live_unix_socket_resolves_to_its_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            socket_path = run_dir / "control.sock"
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.bind(str(socket_path))
                self.assertTrue(_is_unclassifiable_special_file(socket_path))
                self.assertEqual(_closest_existing_ancestor(socket_path), run_dir)
            finally:
                sock.close()

    def test_fifo_resolves_to_its_parent_directory_the_same_way(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            fifo_path = run_dir / "worker.sock"
            os.mkfifo(fifo_path)
            self.assertTrue(_is_unclassifiable_special_file(fifo_path))
            self.assertEqual(_closest_existing_ancestor(fifo_path), run_dir)

    def test_socket_nested_two_levels_still_walks_to_a_real_directory(self):
        # A socket whose own parent is itself missing (unusual, but the
        # walk must not stop partway and must not raise.
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            nested_missing_parent = run_dir / "not-created" / "control.sock"
            self.assertEqual(_closest_existing_ancestor(nested_missing_parent), run_dir)

    def test_symlink_is_still_returned_as_is_not_skipped(self):
        # A symlink must remain a stopping point so assert_root_protected's
        # own "contains a symlink" check still fires on it -- this fix
        # only skips sockets/FIFOs/devices, never symlinks.
        with tempfile.TemporaryDirectory() as tmp:
            real_dir = Path(tmp) / "real"
            real_dir.mkdir()
            link = Path(tmp) / "link"
            link.symlink_to(real_dir)
            self.assertFalse(_is_unclassifiable_special_file(link))
            self.assertEqual(_closest_existing_ancestor(link), link)


class ValidateConfigDictLiveSocketTests(SimpleTestCase):
    """r0032: end-to-end validate_config_dict(enforce_root_ownership=True)
    proof, using the same os.geteuid() mock convention already
    established by test_phase_b_security.py's
    test_root_mode_rejects_application_owned_paths_and_writable_parents
    for the worker's own security module."""

    def setUp(self):
        self.application_root = Path("/home/jreed/isadoraair-django")

    def test_live_socket_under_ordinary_parent_fails_on_ownership_not_type(self):
        # Under simulated root, an ordinary (non-root-owned) temp
        # directory must still fail closed -- but the failure must be
        # about the *directory's* ownership, proving the walk correctly
        # skipped past the socket to its parent, rather than the
        # pre-fix "not a regular file or directory" naming the socket
        # itself. This is what would have differed before the fix.
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "control.sock"
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.bind(str(socket_path))
                data = _valid_config(
                    slots_root=f"{tmp}/slots", runtime_state_path=f"{tmp}/state.json",
                    activation_socket=str(socket_path), worker_socket=f"{tmp}/worker.sock",
                    signer_root=f"{tmp}/signers", trust_policy_path=f"{tmp}/trust.json",
                )
                with mock.patch("isadoraair_updater_bootstrap.security.os.geteuid", return_value=0):
                    with self.assertRaises(ConfigError) as ctx:
                        validate_config_dict(
                            data, application_root=self.application_root, enforce_root_ownership=True,
                        )
                message = str(ctx.exception)
                self.assertIn(str(Path(tmp)), message)
                self.assertNotIn("not a regular file or directory", message)
                self.assertNotIn(str(socket_path), message)
            finally:
                sock.close()

    def test_missing_socket_and_live_socket_fail_the_same_way(self):
        # Parity check: whether the configured socket exists yet (as it
        # would before the supervisor has bound it) or already exists
        # as a live socket (as it does once the supervisor is running),
        # validation against the same unsafe parent must fail for the
        # identical reason -- proving the live-socket case regressed to
        # no worse than the always-supported missing-socket case.
        with tempfile.TemporaryDirectory() as tmp:
            data_missing = _valid_config(
                slots_root=f"{tmp}/slots", runtime_state_path=f"{tmp}/state.json",
                activation_socket=f"{tmp}/control.sock", worker_socket=f"{tmp}/worker.sock",
                signer_root=f"{tmp}/signers", trust_policy_path=f"{tmp}/trust.json",
            )
            with mock.patch("isadoraair_updater_bootstrap.security.os.geteuid", return_value=0):
                with self.assertRaises(ConfigError) as ctx_missing:
                    validate_config_dict(
                        data_missing, application_root=self.application_root, enforce_root_ownership=True,
                    )

            socket_path = Path(tmp) / "control.sock"
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.bind(str(socket_path))
                data_live = _valid_config(
                    slots_root=f"{tmp}/slots", runtime_state_path=f"{tmp}/state.json",
                    activation_socket=str(socket_path), worker_socket=f"{tmp}/worker.sock",
                    signer_root=f"{tmp}/signers", trust_policy_path=f"{tmp}/trust.json",
                )
                with mock.patch("isadoraair_updater_bootstrap.security.os.geteuid", return_value=0):
                    with self.assertRaises(ConfigError) as ctx_live:
                        validate_config_dict(
                            data_live, application_root=self.application_root, enforce_root_ownership=True,
                        )
            finally:
                sock.close()

            self.assertNotIn("not a regular file or directory", str(ctx_missing.exception))
            self.assertNotIn("not a regular file or directory", str(ctx_live.exception))
