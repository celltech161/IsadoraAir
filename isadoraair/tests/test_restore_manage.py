"""Runtime Foundation E7C -- deploy/restore/restore_manage.py.

The one shared mechanism that makes THIS checkout's manage.py (never the
restored backup's own, possibly-older copy) authoritative for the Django
management commands stages 50/70/75/90 use to repair/provision a restored
target -- see that module's own docstring, and lib.sh's restore_manage /
restore_manage_command, for the full "recovery source authority vs.
restored target" split this exists to enforce.

restore_manage.py is stdlib-only, so its pure logic is exercised directly
here via import-by-path; the CLI/exec contract (argv identity, fail-closed
compatibility, .env relay/precedence, and genuine process replacement) is
exercised via real subprocess execution -- the same thing lib.sh's
restore_manage actually does."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RESTORE_DIR = REPO_ROOT / "deploy" / "restore"
HELPER = RESTORE_DIR / "restore_manage.py"


def _load_helper_module():
    spec = importlib.util.spec_from_file_location("restore_manage_under_test", HELPER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


restore_manage = _load_helper_module()


class RestoreManageDotenvParsingTests(SimpleTestCase):
    """parse_dotenv must be byte-for-byte the same rules python-decouple's
    own RepositoryEnv uses -- a divergence here would mean the relayed
    os.environ values do not actually match what decouple would have read
    from this exact file itself."""

    def _write(self, content: str) -> Path:
        tmp = Path(tempfile.mkstemp()[1])
        self.addCleanup(tmp.unlink)
        tmp.write_text(content, encoding="utf-8")
        return tmp

    def test_parses_simple_key_value_pairs(self):
        path = self._write("DB_NAME=isadoraair\nSECRET_KEY=abc123\n")
        self.assertEqual(
            restore_manage.parse_dotenv(path), {"DB_NAME": "isadoraair", "SECRET_KEY": "abc123"}
        )

    def test_strips_matching_single_and_double_quotes(self):
        path = self._write("A='quoted value'\nB=\"double quoted\"\nC=unquoted\n")
        self.assertEqual(
            restore_manage.parse_dotenv(path),
            {"A": "quoted value", "B": "double quoted", "C": "unquoted"},
        )

    def test_ignores_blank_lines_and_comments(self):
        path = self._write("\n# a comment\n   \nDB_NAME=isadoraair\n# DB_NAME=wrong\n")
        self.assertEqual(restore_manage.parse_dotenv(path), {"DB_NAME": "isadoraair"})

    def test_lines_without_equals_are_ignored(self):
        path = self._write("not-a-kv-line\nDB_NAME=isadoraair\n")
        self.assertEqual(restore_manage.parse_dotenv(path), {"DB_NAME": "isadoraair"})

    def test_value_containing_equals_keeps_only_split_on_first(self):
        path = self._write("CSRF_TRUSTED_ORIGINS=https://a,https://b?x=1\n")
        self.assertEqual(
            restore_manage.parse_dotenv(path)["CSRF_TRUSTED_ORIGINS"], "https://a,https://b?x=1"
        )


class RestoreManageRequirementsParsingTests(SimpleTestCase):
    def _write(self, content: str) -> Path:
        tmp = Path(tempfile.mkstemp()[1])
        self.addCleanup(tmp.unlink)
        tmp.write_text(content, encoding="utf-8")
        return tmp

    def test_parses_exact_pins(self):
        path = self._write("Django==5.2.15\ndiscid==1.4.2\n")
        self.assertEqual(
            restore_manage.parse_pinned_requirements(path),
            {"Django": "5.2.15", "discid": "1.4.2"},
        )

    def test_ignores_blank_comment_and_unpinned_lines(self):
        path = self._write("\n# comment\nDjango==5.2.15\nsome-package>=1.0\n")
        self.assertEqual(restore_manage.parse_pinned_requirements(path), {"Django": "5.2.15"})


class RestoreManageCompatibilityProbeTests(SimpleTestCase):
    """check_compatibility runs against whatever interpreter is CURRENTLY
    RUNNING it -- exactly what happens for real once restore_manage.py is
    invoked under the restored target's venv python."""

    def test_exact_installed_version_is_compatible(self):
        import django

        problems = restore_manage.check_compatibility({"Django": django.get_version()})
        self.assertEqual(problems, [])

    def test_missing_package_is_reported_and_fails_closed(self):
        problems = restore_manage.check_compatibility({"nonexistent-fake-pkg-e7c": "1.0.0"})
        self.assertEqual(len(problems), 1)
        self.assertIn("nonexistent-fake-pkg-e7c", problems[0])
        self.assertIn("not installed", problems[0])

    def test_version_mismatch_is_reported_with_both_versions(self):
        import django

        wrong_version = "0.0.0-definitely-not-the-real-version"
        problems = restore_manage.check_compatibility({"Django": wrong_version})
        self.assertEqual(len(problems), 1)
        self.assertIn(wrong_version, problems[0])
        self.assertIn(django.get_version(), problems[0])

    def test_multiple_pins_report_only_the_broken_ones(self):
        import django

        problems = restore_manage.check_compatibility(
            {"Django": django.get_version(), "nonexistent-fake-pkg-e7c": "1.0.0"}
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("nonexistent-fake-pkg-e7c", problems[0])


class RestoreManageEnvMergeTests(SimpleTestCase):
    """os.environ must always win -- an operator/lib.sh export (e.g. the
    staging DB_NAME override) must never be clobbered by the restored
    target's own .env content."""

    def test_injects_missing_keys_only_and_reports_only_those(self):
        environ = {"ALREADY_SET": "shell-value"}
        injected = restore_manage.merge_env_from_dotenv(
            {"ALREADY_SET": "dotenv-value", "NEW_KEY": "dotenv-value-2"}, environ
        )
        self.assertEqual(environ["ALREADY_SET"], "shell-value")
        self.assertEqual(environ["NEW_KEY"], "dotenv-value-2")
        self.assertEqual(injected, ["NEW_KEY"])

    def test_empty_dotenv_injects_nothing(self):
        environ = {"X": "1"}
        injected = restore_manage.merge_env_from_dotenv({}, environ)
        self.assertEqual(injected, [])
        self.assertEqual(environ, {"X": "1"})


class RestoreManageCliTests(SimpleTestCase):
    """The real CLI contract, via subprocess -- what lib.sh's
    restore_manage/restore_manage_command actually invoke."""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="isadoraair-restore-manage-cli-test."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _build_repo(self, *, requirements: str = "", manage_py_body: str | None = None) -> Path:
        repo = self.tmp / f"repo-{len(list(self.tmp.iterdir()))}"
        repo.mkdir()
        (repo / "requirements.txt").write_text(requirements, encoding="utf-8")
        if manage_py_body is None:
            manage_py_body = (
                "import os, sys\n"
                "print('REPO-MANAGE-RAN', sys.argv[1:])\n"
                "print('RELAYED=' + repr(os.environ.get('E7C_MARKER')))\n"
                "sys.exit(0)\n"
            )
        (repo / "manage.py").write_text(manage_py_body, encoding="utf-8")
        return repo

    def _build_target(self, *, env_content: str = "DEBUG=True\nE7C_MARKER=from-target-env\n") -> Path:
        target = self.tmp / f"target-{len(list(self.tmp.iterdir()))}"
        venv_bin = target / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").symlink_to(sys.executable)
        (target / ".env").write_text(env_content, encoding="utf-8")
        decoy = target / "manage.py"
        decoy.write_text("import sys\nprint('DECOY-MANAGE-RAN-THIS-MUST-NEVER-HAPPEN')\nsys.exit(97)\n", encoding="utf-8")
        return target

    def _run(self, repo: Path, target: Path, *forwarded: str, print_exec_argv=True, extra_env=None, exec_real=False):
        args = [sys.executable, str(HELPER), "--repo-root", str(repo), "--target-root", str(target)]
        if print_exec_argv and not exec_real:
            args.append("--print-exec-argv")
        args += ["--", *forwarded]
        # A deliberately minimal, hermetic base environment -- inheriting
        # the full parent test-runner environment here would let ambient
        # variables (e.g. this suite's own DEBUG=True) silently make an
        # "already exported, must not be overwritten" assertion pass or
        # fail for the wrong reason.
        run_env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
        if extra_env:
            run_env.update(extra_env)
        return subprocess.run(args, capture_output=True, text=True, timeout=30, env=run_env)

    def test_exec_argv_always_targets_repo_root_manage_py(self):
        repo = self._build_repo()
        target = self._build_target()
        result = self._run(repo, target, "somecommand", "--flag")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(repo / "manage.py"), result.stdout)
        self.assertNotIn(str(target / "manage.py"), result.stdout)

    def test_missing_repo_manage_py_fails_closed(self):
        repo = self.tmp / "no-manage-py"
        repo.mkdir()
        (repo / "requirements.txt").write_text("", encoding="utf-8")
        target = self._build_target()
        result = self._run(repo, target, "somecommand")
        self.assertEqual(result.returncode, 2)
        self.assertIn("manage.py", result.stderr)

    def test_missing_requirements_txt_fails_closed(self):
        repo = self.tmp / "no-requirements"
        repo.mkdir()
        (repo / "manage.py").write_text("import sys; sys.exit(0)\n", encoding="utf-8")
        target = self._build_target()
        result = self._run(repo, target, "somecommand")
        self.assertEqual(result.returncode, 2)
        self.assertIn("requirements.txt", result.stderr)

    def test_missing_target_env_fails_closed(self):
        repo = self._build_repo()
        target = self.tmp / "no-env"
        venv_bin = target / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").symlink_to(sys.executable)
        result = self._run(repo, target, "somecommand")
        self.assertEqual(result.returncode, 2)
        self.assertIn(".env", result.stderr)

    def test_no_command_given_is_a_usage_error(self):
        repo = self._build_repo()
        target = self._build_target()
        result = self._run(repo, target)  # nothing after --
        self.assertEqual(result.returncode, 2)

    def test_incompatible_venv_fails_closed_and_prints_nothing_to_stdout(self):
        repo = self._build_repo(requirements="nonexistent-fake-pkg-e7c==1.0.0\n")
        target = self._build_target()
        result = self._run(repo, target, "somecommand")
        self.assertEqual(result.returncode, 3)
        self.assertIn("nonexistent-fake-pkg-e7c", result.stderr)
        self.assertIn(str(target), result.stderr)  # names the refused fallback target
        self.assertEqual(result.stdout, "")

    def test_version_mismatch_fails_closed_naming_expected_and_actual(self):
        import django

        repo = self._build_repo(requirements="Django==999.999.999\n")
        target = self._build_target()
        result = self._run(repo, target, "somecommand")
        self.assertEqual(result.returncode, 3)
        self.assertIn("999.999.999", result.stderr)
        self.assertIn(django.get_version(), result.stderr)

    def test_compatible_pinned_requirement_passes(self):
        import django

        repo = self._build_repo(requirements=f"Django=={django.get_version()}\n")
        target = self._build_target()
        result = self._run(repo, target, "somecommand")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(repo / "manage.py"), result.stdout)

    def test_dotenv_values_are_relayed_except_already_exported_keys(self):
        repo = self._build_repo()
        target = self._build_target(env_content="DEBUG=True\nE7C_MARKER=from-target-env\nDB_NAME=from-target-env\n")
        result = self._run(repo, target, "somecommand", extra_env={"DB_NAME": "exported-should-win"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        relayed_line = next(line for line in result.stdout.splitlines() if line.startswith("env keys relayed"))
        self.assertIn("E7C_MARKER", relayed_line)
        self.assertIn("DEBUG", relayed_line)
        self.assertNotIn("DB_NAME", relayed_line)  # already exported -- must not be overwritten or even re-listed

    def test_real_exec_runs_repo_manage_py_and_the_decoy_never_runs(self):
        repo = self._build_repo()
        target = self._build_target()
        result = self._run(repo, target, "somecommand", "--json", exec_real=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("REPO-MANAGE-RAN", result.stdout)
        self.assertIn("['somecommand', '--json']", result.stdout)
        self.assertIn("RELAYED='from-target-env'", result.stdout)
        self.assertNotIn("DECOY-MANAGE-RAN-THIS-MUST-NEVER-HAPPEN", result.stdout)
        self.assertNotEqual(result.returncode, 97)  # the decoy's own exit signature

    def test_real_exec_fails_closed_before_ever_touching_the_decoy(self):
        repo = self._build_repo(requirements="nonexistent-fake-pkg-e7c==1.0.0\n")
        target = self._build_target()
        result = self._run(repo, target, "somecommand", exec_real=True)
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("DECOY-MANAGE-RAN-THIS-MUST-NEVER-HAPPEN", result.stdout)
        self.assertNotIn("REPO-MANAGE-RAN", result.stdout)
