import ast
from pathlib import Path
import re
import tempfile
from unittest import mock

from django.test import SimpleTestCase

from .phase_b_helpers import PROJECT_ROOT, RUNTIME_ROOT
from isadoraair_updater.security import (
    ProtectionError,
    assert_root_protected,
    assert_root_protected_parents,
)


RUNTIME_PACKAGE = RUNTIME_ROOT / "isadoraair_updater"


class ProtectedRuntimeStaticTests(SimpleTestCase):
    def test_root_mode_rejects_application_owned_paths_and_writable_parents(self):
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / "state"
            candidate.mkdir()
            with mock.patch("isadoraair_updater.security.os.geteuid", return_value=0):
                with self.assertRaises(ProtectionError):
                    assert_root_protected(candidate)
                with self.assertRaises(ProtectionError):
                    assert_root_protected_parents(candidate / "child")

    def test_runtime_is_stdlib_only_and_never_imports_application(self):
        forbidden_roots = {"django", "updatecenter", "isadoraair", "psycopg2"}
        for path in RUNTIME_PACKAGE.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name.split(".")[0], forbidden_roots, str(path))
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    self.assertNotIn(node.module.split(".")[0], forbidden_roots, str(path))

    def test_only_process_module_constructs_subprocess(self):
        for path in RUNTIME_PACKAGE.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imports_subprocess = any(
                isinstance(node, ast.Import) and any(alias.name == "subprocess" for alias in node.names)
                for node in ast.walk(tree)
            )
            self.assertEqual(imports_subprocess, path.name == "process.py", str(path))

    def test_no_shell_true_or_os_system(self):
        for path in RUNTIME_PACKAGE.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("os.system(", text)
            tree = ast.parse(text, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for keyword in node.keywords:
                        self.assertFalse(keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True)

    def test_service_execstart_is_protected_system_python(self):
        unit = (PROJECT_ROOT / "deploy" / "isadoraair-updater.service").read_text(encoding="utf-8")
        exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
        self.assertIn("/usr/bin/python3 -I /usr/local/libexec/isadoraair-updater/updaterd.py", exec_line)
        self.assertNotIn("@@ISA_ROOT@@", exec_line)
        self.assertNotIn("/venv/", exec_line)

    def test_entrypoint_imports_no_privileged_package_before_install_check(self):
        tree = ast.parse((RUNTIME_ROOT / "updaterd.py").read_text(encoding="utf-8"))
        top_level_imports = [
            node for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        for node in top_level_imports:
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith("isadoraair_updater"))
            else:
                self.assertTrue(all(not alias.name.startswith("isadoraair_updater") for alias in node.names))

    def test_root_code_never_names_application_checkout_or_venv_as_executor(self):
        runtime_text = "\n".join(path.read_text(encoding="utf-8") for path in RUNTIME_PACKAGE.glob("*.py"))
        self.assertNotIn("/opt/isadoraair/venv", runtime_text)
        self.assertNotIn("/home/jreed", runtime_text)

    def test_no_general_command_ipc(self):
        protocol = (RUNTIME_PACKAGE / "protocol.py").read_text(encoding="utf-8")
        for forbidden in ("RUN_COMMAND", "RUN_SYSTEMCTL", "WRITE_FILE", '"EXEC"', '"SHELL"'):
            self.assertNotIn(forbidden, protocol)

    def test_no_http_start_route_or_view(self):
        urls = (PROJECT_ROOT / "updatecenter" / "urls.py").read_text(encoding="utf-8")
        views = (PROJECT_ROOT / "updatecenter" / "views.py").read_text(encoding="utf-8")
        self.assertNotIn("start_update", urls)
        self.assertNotIn("submit_job", views)
        self.assertNotIn("create_job", views)

    def test_root_migration_and_live_git_are_explicitly_run_as_user(self):
        executor = (RUNTIME_PACKAGE / "executor.py").read_text(encoding="utf-8")
        self.assertIn("self.runner.run_as_user", executor)
        self.assertIn('["migrate", "--noinput", "--skip-checks"]', executor)
        self.assertIn('[GIT, "-C", str(self.config.application_root)', executor)
        self.assertNotRegex(executor, r"self\.runner\.run\(\s*\[GIT,\s*\"-C\",\s*str\(self\.config\.application_root\)")

    def test_privileged_destinations_come_only_from_config_and_allowlists(self):
        protocol_fields = (RUNTIME_PACKAGE / "protocol.py").read_text(encoding="utf-8")
        self.assertNotIn("destination", protocol_fields)
        self.assertNotIn("unit", protocol_fields)
        self.assertNotIn("service", protocol_fields)

    def test_no_updater_self_replacement(self):
        text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in RUNTIME_PACKAGE.glob("*.py")
            if path.name != "__init__.py"
        )
        self.assertNotIn("/usr/local/libexec/isadoraair-updater", text)

    def test_no_automatic_rollback_commands(self):
        executor = (RUNTIME_PACKAGE / "executor.py").read_text(encoding="utf-8")
        for forbidden in ("reset --hard", "migrate --fake", "pg_restore", "reverse migration"):
            self.assertNotIn(forbidden, executor)

    def test_unrestricted_sudo_gate_is_documented(self):
        docs = (PROJECT_ROOT / "docs" / "UPDATE_CENTER.md").read_text(encoding="utf-8")
        self.assertIn("NOPASSWD: ALL", docs)
        self.assertIn("Before any production Update button", docs)
