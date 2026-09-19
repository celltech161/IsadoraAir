from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
from unittest import TestCase


REPO_ROOT = Path(__file__).resolve().parents[2]
REBIND_PATH = REPO_ROOT / "deploy/restore/rebind_host_network.py"
BARE_METAL_WRAPPER = REPO_ROOT / "deploy/restore/bare_metal_restore.sh"
BACKUP_SCRIPT = REPO_ROOT / "deploy/backup_isadoraair.sh"
BACKUP_UNIT = REPO_ROOT / "deploy/isadoraair-backup.service"
VALIDATOR = REPO_ROOT / "monitoring/management/commands/validate_runtime_recovery_payload.py"

spec = importlib.util.spec_from_file_location("rebind_host_network", REBIND_PATH)
assert spec is not None and spec.loader is not None
rebind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rebind)


class HostNetworkRebindingTests(TestCase):
    def test_physical_drill_private_ip_is_replaced_and_public_names_survive(self):
        allowed = (
            "localhost,127.0.0.1,isadoraair,192.168.1.125,"
            "oakgroveradio.dyndns.org,oakgroveradio.com,radio.oakgroveradio.com"
        )
        csrf = (
            "https://isadoraair,https://192.168.1.125,http://isadoraair:8000,"
            "http://192.168.1.125:8000,https://oakgroveradio.dyndns.org,"
            "https://oakgroveradio.com"
        )

        new_allowed = rebind.rebind_allowed_hosts(
            allowed, hostname="isadoraair2-sandbox", primary_ip="192.168.1.111"
        )
        self.assertNotIn("192.168.1.125", new_allowed)
        self.assertIn("192.168.1.111", new_allowed)
        self.assertIn("isadoraair2-sandbox", new_allowed)
        self.assertIn("isadoraair", new_allowed)
        self.assertIn("oakgroveradio.com", new_allowed)

        new_csrf = rebind.rebind_csrf_origins(
            csrf, hostname="isadoraair2-sandbox", primary_ip="192.168.1.111"
        )
        self.assertNotIn("192.168.1.125", new_csrf)
        self.assertIn("https://192.168.1.111", new_csrf)
        self.assertIn("https://isadoraair2-sandbox", new_csrf)
        self.assertIn("http://192.168.1.111:8000", new_csrf)
        self.assertIn("http://isadoraair2-sandbox:8000", new_csrf)
        self.assertIn("https://oakgroveradio.com", new_csrf)

    def test_atomic_rewrite_preserves_mode_and_unrelated_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            env_path = Path(temp) / ".env"
            env_path.write_text(
                "SECRET_KEY=do-not-touch\n"
                "ALLOWED_HOSTS=localhost,127.0.0.1,192.168.1.125\n"
                "CSRF_TRUSTED_ORIGINS=https://192.168.1.125\n"
                "DB_PASSWORD=also-do-not-touch\n",
                encoding="utf-8",
            )
            env_path.chmod(0o600)
            lines, existing = rebind._read_env(env_path)
            content = rebind._rewrite(
                lines,
                {
                    rebind.KEY_ALLOWED: rebind.rebind_allowed_hosts(
                        existing[rebind.KEY_ALLOWED],
                        hostname="replacement",
                        primary_ip="10.10.10.25",
                    ),
                    rebind.KEY_CSRF: rebind.rebind_csrf_origins(
                        existing[rebind.KEY_CSRF],
                        hostname="replacement",
                        primary_ip="10.10.10.25",
                    ),
                },
            )
            rebind.atomic_write(env_path, content)
            restored = env_path.read_text(encoding="utf-8")
            self.assertIn("SECRET_KEY=do-not-touch", restored)
            self.assertIn("DB_PASSWORD=also-do-not-touch", restored)
            self.assertNotIn("192.168.1.125", restored)
            self.assertIn("10.10.10.25", restored)
            self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)


class PhysicalAcceptanceWrapperTests(TestCase):
    def test_bare_metal_wrapper_refuses_root_and_revalidates_after_rebind(self):
        text = BARE_METAL_WRAPPER.read_text(encoding="utf-8")
        self.assertIn('if [ "$(id -u)" -eq 0 ]', text)
        self.assertIn("Do NOT run the bare-metal restore under sudo/root", text)
        restore_index = text.index('"$SCRIPT_DIR/restore.sh"')
        rebind_index = text.index("rebind_host_network.py")
        validate_index = text.index('"$SCRIPT_DIR/95-validate.sh"')
        self.assertLess(restore_index, rebind_index)
        self.assertLess(rebind_index, validate_index)

    def test_scheduled_backup_uses_existing_automatic_policy_path(self):
        unit = BACKUP_UNIT.read_text(encoding="utf-8")
        backup = BACKUP_SCRIPT.read_text(encoding="utf-8")
        validator = VALIDATOR.read_text(encoding="utf-8")

        # No new managed systemd unit or wrapper is needed: preserve the
        # established service entrypoint and strengthen the validator that
        # backup_isadoraair.sh already calls by default.
        self.assertIn("ExecStart=@@ISA_ROOT@@/deploy/backup_isadoraair.sh", unit)
        self.assertIn("--require-current-station-policy", backup)
        self.assertIn("compare_protected_updater_to_product", validator)
        self.assertIn("protected_updater_freshness", validator)
