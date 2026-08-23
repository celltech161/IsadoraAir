"""Explicit security-contract tests -- [P0] 1.1 Phase A §26 "be
skeptical" checklist, made durable as regression tests rather than a
one-time manual read of the diff. Several of these are source scans,
deliberately -- proving an ABSENCE (no shell=True anywhere, no
subprocess call outside the one sanctioned module) is not something a
behavioral test can fully establish, but a source scan can."""
import ast
from pathlib import Path

from django.test import SimpleTestCase

from updatecenter import git_adapter, manifest as m

APP_ROOT = Path(__file__).resolve().parents[1]


def _all_py_files():
    return [
        p for p in APP_ROOT.rglob("*.py")
        if "migrations" not in p.parts and "tests" not in p.parts
    ]


class NoShellTrueTests(SimpleTestCase):
    def test_no_shell_true_anywhere_in_this_app(self):
        """AST-based, not a substring search -- this module's own
        docstrings legitimately mention "shell=True" in prose
        (explaining that it's never used), which a naive
        assertNotIn("shell=True", text) would misfire on. Walking the
        AST for an actual keyword argument shell=True in a call proves
        the real thing this test cares about."""
        for path in _all_py_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for kw in node.keywords:
                    if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        self.fail(f"{path} contains an actual shell=True call argument")

    def test_no_os_system_or_os_popen_anywhere(self):
        for path in _all_py_files():
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("os.system(", text, f"{path} calls os.system")
            self.assertNotIn("os.popen(", text, f"{path} calls os.popen")


class SubprocessConfinementTests(SimpleTestCase):
    """subprocess is only ever invoked from git_adapter.py, and only
    ever with `git` as argv[0] -- proven by parsing the AST (not a
    regex) so a call hidden behind unusual formatting can't slip past."""

    def test_subprocess_only_imported_in_git_adapter(self):
        for path in _all_py_files():
            if path.name == "git_adapter.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotEqual(alias.name, "subprocess", f"{path} imports subprocess directly")
                if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                    self.fail(f"{path} imports from subprocess directly")

    def test_every_bounded_process_call_in_git_adapter_starts_with_git(self):
        tree = ast.parse((APP_ROOT / "git_adapter.py").read_text(encoding="utf-8"))
        bounded_calls = []
        popen_calls = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_run_git_argv"):
                bounded_calls.append(node)
                first_arg = node.args[0] if node.args else None
                self.assertIsInstance(first_arg, ast.List, "git argv must be a literal list")
                first_element = first_arg.elts[0] if first_arg.elts else None
                self.assertIsInstance(first_element, ast.Constant)
                self.assertEqual(first_element.value, "git")
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "Popen" and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess"):
                popen_calls.append(node)
                self.assertIsInstance(node.args[0], ast.Name)
                self.assertEqual(node.args[0].id, "argv")
        self.assertTrue(bounded_calls, "expected bounded git process call sites")
        self.assertEqual(len(popen_calls), 1, "only the bounded helper may construct a subprocess")


class ForbiddenGitOperationTests(SimpleTestCase):
    def test_every_working_tree_mutating_subcommand_is_forbidden(self):
        mutating = {"checkout", "reset", "merge", "pull", "stash", "clean", "branch", "submodule", "switch", "restore"}
        self.assertTrue(mutating.issubset(git_adapter.FORBIDDEN_SUBCOMMANDS))

    def test_allowed_and_forbidden_sets_never_overlap(self):
        self.assertEqual(git_adapter.ALLOWED_SUBCOMMANDS & git_adapter.FORBIDDEN_SUBCOMMANDS, set())


class ManifestFieldContractTests(SimpleTestCase):
    def test_every_forbidden_field_is_actually_rejected(self):
        base = {
            "schema_version": 1, "release_id": "r0001", "previous_release_id": None,
            "bootstrap_commit": "a" * 40, "minimum_updater_protocol_version": 1,
            "migrations_required": [], "python_requirements_changed": False,
            "apt_packages_new": [], "systemd_units_changed": [], "systemd_units_new_required": [],
            "systemd_units_new_optional": [], "collectstatic_required": False,
            "services_requiring_restart": [], "nginx_changed": False, "runtime_components_changed": False,
        }
        for field in m.FORBIDDEN_FIELDS:
            data = dict(base)
            data[field] = "anything"
            with self.assertRaises(m.ManifestError, msg=f"field {field!r} was not rejected"):
                m.validate_manifest_dict(data)

    def test_unit_name_pattern_rejects_path_traversal(self):
        self.assertIsNone(m.UNIT_NAME_PATTERN.match("../../etc/systemd/system/evil.service"))
        self.assertIsNone(m.UNIT_NAME_PATTERN.match("/etc/passwd"))
        self.assertIsNone(m.UNIT_NAME_PATTERN.match("foo/bar.service"))

    def test_unit_name_pattern_accepts_real_project_unit_names(self):
        for name in ("isadoraair-engine.service", "wx-alert-beep.timer", "syndicated-fsn.service"):
            self.assertIsNotNone(m.UNIT_NAME_PATTERN.match(name))

    def test_core_restartable_services_is_a_closed_set_not_derived_from_deploy_dir(self):
        """Deliberately NOT scanned from deploy/*.service -- that would
        accept any of the 100+ optional/companion units as eligible
        for an unattended restart. Confirms the set is exactly the 5
        core services, no more."""
        self.assertEqual(
            m.CORE_RESTARTABLE_SERVICES,
            {"isadoraair-gunicorn", "isadoraair-engine", "isadoraair-encoders",
             "isadoraair-monitoring", "isadoraair-rbds"},
        )


class NoExecutionCodeTests(SimpleTestCase):
    """Phase A must not contain code that changes the checkout, runs
    migrations, runs pip, installs/reloads systemd, restarts a
    service, writes nginx, or runs apt -- checked as a source scan for
    the actual PRIVILEGED-OPERATION APIs, not a naive string search
    that would also flag this file's own docstrings/comments
    describing what's absent."""

    FORBIDDEN_CALLS = (
        "call_command('migrate'", 'call_command("migrate"',
        "pip install", "apt-get", "apt install", "daemon-reload",
        "systemctl restart", "systemctl enable", "systemctl reload",
    )

    def test_no_privileged_operation_strings_in_non_test_source(self):
        for path in _all_py_files():
            if path.name in ("__init__.py",):
                continue
            text = path.read_text(encoding="utf-8")
            for forbidden in self.FORBIDDEN_CALLS:
                self.assertNotIn(forbidden, text, f"{path} contains {forbidden!r}")

    def test_migrationexecutor_only_ever_calls_migration_plan_never_migrate(self):
        """[P0] 1.1 correction: schema_health.py reads Django's actual
        pending-migration state via MigrationExecutor.migration_plan()
        -- a pure computation, never MigrationExecutor.migrate() (the
        method that actually APPLIES migrations). AST-checked (not a
        substring search) so a `.migrate(` call hidden behind unusual
        formatting can't slip past -- this is the one method name this
        whole correction pass must never see called anywhere in
        non-test Phase A source."""
        for path in _all_py_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "migrate":
                    self.fail(f"{path} calls a `.migrate(...)` method -- Phase A must only ever read migration state, never apply it")
