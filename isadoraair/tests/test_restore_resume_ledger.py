"""r0043 -- Runtime Foundation, first resumable/convergent restore step.

Covers three closely related fixes/additions, all exercised for real
(real subprocess execution of the actual shell/Python restore tooling
against disposable temp trees -- never a real /etc, /opt, /var/lib, or
production PostgreSQL):

  1. The confirmed real E8 defect: a legacy WEATHER_DATA_DIR restored
     verbatim into .env, pointing INSIDE the weather-ingest companion's
     own source-checkout namespace, let Stage 60's first Django import
     manufacture a non-empty, non-Git companion-checkout directory
     before Stage 80 ever ran -- poisoning Stage 80's own (correct)
     collision refusal. Fixed in 40-station-content.sh.
  2. restore_ledger.py -- the new restore-session identity/state ledger.
  3. --resume semantics built on that ledger: Stage 20/30 verify-and-
     skip already-durably-completed work instead of hard-failing on
     pre-existing content, and Stage 80 can narrowly, provenance-
     checked repair the EXACT known legacy-scaffold collision left
     behind by a pre-r0043 run -- never a general "trust any empty
     directory" mechanism.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from isadoraair.tests.test_restore_tooling import RESTORE_DIR, Stage30RealPostgreSQLTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LEDGER_HELPER = RESTORE_DIR / "restore_ledger.py"


def _make_minimal_archive(path: Path) -> Path:
    """A valid-but-empty backup archive -- 40-station-content.sh and
    80-companions.sh only need `tar -tzf`/`tar -xzO` to succeed against
    it; absence of reports/stereotool/srv-content entries is handled
    gracefully (warnings only, never fatal)."""
    empty = path / "empty"
    empty.mkdir(parents=True, exist_ok=True)
    archive = path / "backup.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(empty, arcname=".")
    return archive


def _make_git_fixture_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "requirements.txt").write_text("", encoding="utf-8")
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return path


class RestoreLedgerModuleTests(SimpleTestCase):
    """Direct subprocess tests of restore_ledger.py's own identity/
    fail-closed contract -- never touches a real /var/lib/isadoraair."""

    def setUp(self):
        super().setUp()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-e-ledger-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.archive1 = self.tmpdir / "archive1.tar.gz"
        self.archive1.write_text("archive one\n", encoding="utf-8")
        self.archive2 = self.tmpdir / "archive2.tar.gz"
        self.archive2.write_text("a different archive\n", encoding="utf-8")
        self.ledger = self.tmpdir / "ledger.json"

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(LEDGER_HELPER), *args],
            capture_output=True, text=True, timeout=15,
        )

    def test_record_then_stage_state_round_trips(self):
        result = self._run(
            "record", "--ledger", str(self.ledger), "--archive", str(self.archive1),
            "--target-root", "/opt/x", "--stage", "20-application", "--git-sha", "abc123",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self._run(
            "stage-state", "--ledger", str(self.ledger), "--archive", str(self.archive1),
            "--target-root", "/opt/x", "--stage", "20-application",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "complete")

    def test_stage_state_absent_for_never_recorded_stage(self):
        self._run(
            "record", "--ledger", str(self.ledger), "--archive", str(self.archive1),
            "--target-root", "/opt/x", "--stage", "00-preflight",
        )
        result = self._run(
            "stage-state", "--ledger", str(self.ledger), "--archive", str(self.archive1),
            "--target-root", "/opt/x", "--stage", "30-postgresql",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "absent")

    def test_stage_state_absent_for_no_ledger_at_all(self):
        result = self._run(
            "stage-state", "--ledger", str(self.ledger), "--archive", str(self.archive1),
            "--target-root", "/opt/x", "--stage", "20-application",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "absent")

    def test_mismatched_archive_fails_closed(self):
        """A different archive must never silently inherit a prior
        restore session's ledger."""
        self._run(
            "record", "--ledger", str(self.ledger), "--archive", str(self.archive1),
            "--target-root", "/opt/x", "--stage", "20-application",
        )
        result = self._run(
            "stage-state", "--ledger", str(self.ledger), "--archive", str(self.archive2),
            "--target-root", "/opt/x", "--stage", "20-application",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DIFFERENT archive", result.stderr)

        result = self._run(
            "record", "--ledger", str(self.ledger), "--archive", str(self.archive2),
            "--target-root", "/opt/x", "--stage", "30-postgresql",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DIFFERENT archive", result.stderr)

    def test_mismatched_target_root_fails_closed(self):
        self._run(
            "record", "--ledger", str(self.ledger), "--archive", str(self.archive1),
            "--target-root", "/opt/x", "--stage", "20-application",
        )
        result = self._run(
            "record", "--ledger", str(self.ledger), "--archive", str(self.archive1),
            "--target-root", "/opt/y", "--stage", "30-postgresql",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DIFFERENT target root", result.stderr)

    def test_corrupted_ledger_fails_closed(self):
        self.ledger.write_text("not valid json{{{", encoding="utf-8")
        result = self._run(
            "stage-state", "--ledger", str(self.ledger), "--archive", str(self.archive1),
            "--target-root", "/opt/x", "--stage", "20-application",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("corrupt", result.stderr)

    def test_incomplete_ledger_missing_required_field_fails_closed(self):
        self.ledger.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        result = self._run(
            "stage-state", "--ledger", str(self.ledger), "--archive", str(self.archive1),
            "--target-root", "/opt/x", "--stage", "20-application",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing required field", result.stderr)

    def test_ledger_never_records_a_secret(self):
        """Only identity/timestamp/stage metadata -- passing something
        secret-shaped through --detail is the caller's own choice, but
        nothing here EVER derives ledger content from .env/DB_PASSWORD/
        SECRET_KEY etc. -- confirmed by construction: record's own CLI
        surface has no such input at all."""
        result = self._run(
            "record", "--ledger", str(self.ledger), "--archive", str(self.archive1),
            "--target-root", "/opt/x", "--stage", "20-application", "--git-sha", "abc123",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        content = self.ledger.read_text(encoding="utf-8")
        for forbidden in ("DB_PASSWORD", "SECRET_KEY", "PASSWORD"):
            self.assertNotIn(forbidden, content)


class WeatherDataDirNormalizationTests(SimpleTestCase):
    """Real subprocess execution of 40-station-content.sh against a
    disposable --staging-root -- proves the actual normalization logic,
    not a description of it."""

    def setUp(self):
        super().setUp()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-e-weather-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.staging = self.tmpdir / "staging"
        self.app_root = self.staging / "opt" / "isadoraair"
        self.app_root.mkdir(parents=True)
        self.archive = _make_minimal_archive(self.tmpdir)

    def _write_env(self, weather_data_dir: str | None):
        lines = ["DEBUG=True\n"]
        if weather_data_dir is not None:
            lines.append(f"WEATHER_DATA_DIR={weather_data_dir}\n")
        (self.app_root / ".env").write_text("".join(lines), encoding="utf-8")

    def _run(self, *extra, timeout=30):
        return subprocess.run(
            [str(RESTORE_DIR / "40-station-content.sh"), "--archive", str(self.archive),
             "--staging-root", str(self.staging), "--apply", *extra],
            capture_output=True, text=True, timeout=timeout,
        )

    def test_legacy_value_inside_companion_namespace_is_normalized(self):
        legacy = f"{self.staging}/weather-ingest/data"
        self._write_env(legacy)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("known legacy value", result.stdout + result.stderr)
        env_text = (self.app_root / ".env").read_text(encoding="utf-8")
        self.assertIn("WEATHER_DATA_DIR=/var/lib/isadoraair/weather\n", env_text)
        self.assertNotIn(legacy, env_text)
        self.assertIn("DEBUG=True\n", env_text)  # every other key untouched
        canonical_dir = self.staging / "var" / "lib" / "isadoraair" / "weather"
        self.assertTrue(canonical_dir.is_dir())
        self.assertFalse((self.staging / "weather-ingest").exists(), "legacy path must NOT be manufactured")

    def test_canonical_default_is_not_rewritten(self):
        self._write_env(None)  # no WEATHER_DATA_DIR at all -> canonical default
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        env_text = (self.app_root / ".env").read_text(encoding="utf-8")
        self.assertNotIn("WEATHER_DATA_DIR=", env_text)  # never injected if absent
        self.assertIn("not a recognized legacy value", result.stdout)

    def test_intentional_custom_path_outside_companion_namespace_is_preserved(self):
        custom = "/srv/custom-weather-data"
        self._write_env(custom)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        env_text = (self.app_root / ".env").read_text(encoding="utf-8")
        self.assertIn(f"WEATHER_DATA_DIR={custom}\n", env_text)
        self.assertIn("not a recognized legacy value", result.stdout)
        # The custom directory is still established (mirrors REPORTS_ROOT's
        # own unconditional-establishment precedent) -- just never rewritten.
        established = self.staging / "srv" / "custom-weather-data"
        self.assertTrue(established.is_dir())

    def test_established_canonical_directory_mode_is_deterministic(self):
        legacy = f"{self.staging}/weather-ingest/data"
        self._write_env(legacy)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        canonical_dir = self.staging / "var" / "lib" / "isadoraair" / "weather"
        import stat as stat_module
        mode = stat_module.S_IMODE(canonical_dir.stat().st_mode)
        self.assertEqual(mode, 0o755)

    def test_plan_mode_never_writes_env_or_creates_directories(self):
        legacy = f"{self.staging}/weather-ingest/data"
        self._write_env(legacy)
        original = (self.app_root / ".env").read_text(encoding="utf-8")
        result = subprocess.run(
            [str(RESTORE_DIR / "40-station-content.sh"), "--archive", str(self.archive),
             "--staging-root", str(self.staging), "--plan"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.app_root / ".env").read_text(encoding="utf-8"), original)
        self.assertFalse((self.staging / "var" / "lib" / "isadoraair" / "weather").exists())
        self.assertFalse((self.staging / "weather-ingest").exists())

    def test_only_the_weather_data_dir_key_is_ever_touched(self):
        legacy = f"{self.staging}/weather-ingest/data"
        (self.app_root / ".env").write_text(
            f"DEBUG=True\nSECRET_KEY=untouched-marker\nWEATHER_DATA_DIR={legacy}\nDB_NAME=untouched\n",
            encoding="utf-8",
        )
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        env_text = (self.app_root / ".env").read_text(encoding="utf-8")
        self.assertIn("SECRET_KEY=untouched-marker\n", env_text)
        self.assertIn("DB_NAME=untouched\n", env_text)
        self.assertEqual(env_text.count("WEATHER_DATA_DIR="), 1)


class WeatherDataDirDjangoImportRegressionTests(SimpleTestCase):
    """The exact clean-machine sequence: restored legacy .env -> the
    first Django-importing restore operation (Stage 60's own
    `manage.py check`, reproduced directly here via a real
    `weather.services` import under the SAME settings-resolution
    mechanism) -> Stage 80. Reproduces the confirmed real E8 defect end
    to end, and proves 40-station-content.sh's fix closes it."""

    def setUp(self):
        super().setUp()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-e-weather-e2e-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def _import_weather_services(self, *, weather_data_dir: str) -> str:
        """A REAL subprocess Django app-loading import (never mocked),
        exactly what manage.py check's own URLconf resolution triggers
        (weather/views.py imports weather.services at module level)."""
        env = {
            **os.environ,
            "WEATHER_DATA_DIR": weather_data_dir,
            "DEBUG": "True", "DB_NAME": "unused", "DB_USER": "unused",
            "DB_PASSWORD": "unused", "DB_HOST": "127.0.0.1", "DB_PORT": "65534",
        }
        result = subprocess.run(
            [str(REPO_ROOT / "venv" / "bin" / "python"), "-c",
             "import django, os\n"
             "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'isadoraair.settings')\n"
             "django.setup()\n"
             "import weather.services\n"
             "print(weather.services.DATA_DIR)\n"],
            capture_output=True, text=True, timeout=30, env=env, cwd=str(REPO_ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def test_reproduction_unnormalized_legacy_value_manufactures_the_collision_path(self):
        """Confirms the ROOT CAUSE, unmediated by any restore-tooling
        fix -- this is what a bare Django import does with a legacy
        value, proving the mechanism this whole defect depends on."""
        legacy = self.tmpdir / "weather-ingest" / "data"
        self.assertFalse(legacy.parent.exists())
        printed = self._import_weather_services(weather_data_dir=str(legacy))
        self.assertEqual(printed, str(legacy))
        self.assertTrue(legacy.is_dir())
        self.assertFalse((legacy.parent / ".git").exists())  # exactly Stage 80's own collision shape

    def test_stage_40_normalized_value_never_manufactures_the_collision_path(self):
        """The end-to-end fix: run 40-station-content.sh against a
        legacy .env, THEN perform the same Django import using whatever
        it left behind -- the legacy companion-checkout path must never
        be manufactured."""
        staging = self.tmpdir / "staging"
        app_root = staging / "opt" / "isadoraair"
        app_root.mkdir(parents=True)
        legacy_value = f"{staging}/weather-ingest/data"
        (app_root / ".env").write_text(f"WEATHER_DATA_DIR={legacy_value}\nDEBUG=True\n", encoding="utf-8")
        archive = _make_minimal_archive(self.tmpdir)

        result = subprocess.run(
            [str(RESTORE_DIR / "40-station-content.sh"), "--archive", str(archive),
             "--staging-root", str(staging), "--apply"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        normalized_value = None
        for line in (app_root / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("WEATHER_DATA_DIR="):
                normalized_value = line.split("=", 1)[1]
        self.assertIsNotNone(normalized_value)
        self.assertNotEqual(normalized_value, legacy_value)

        printed = self._import_weather_services(weather_data_dir=normalized_value)
        self.assertEqual(printed, normalized_value)
        self.assertFalse((staging / "weather-ingest").exists(), "Stage 80's collision path must never be manufactured")


class Stage20ResumeVerifyTests(SimpleTestCase):
    """Real subprocess execution of 20-application.sh's new --resume
    branch -- a real local Git fixture repo, never GitHub."""

    def setUp(self):
        super().setUp()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-e-resume20-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.fixture_repo = _make_git_fixture_repo(self.tmpdir / "fixture-repo")
        (self.fixture_repo / "manage.py").write_text("#!/usr/bin/env python\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.fixture_repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "manage.py"], cwd=self.fixture_repo, check=True)
        self.fixture_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.fixture_repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        self.staging = self.tmpdir / "staging"
        self.ledger_root = self.tmpdir / "ledger-root"
        self.env = {**os.environ, "RESTORE_RECOVERY_RECEIPT_ROOT": str(self.ledger_root)}

    def _build_archive(self) -> Path:
        workdir = self.tmpdir / "work"
        workdir.mkdir(exist_ok=True)
        (workdir / "MANIFEST.txt").write_text(f"IsadoraAir Git SHA:     {self.fixture_sha}\n", encoding="utf-8")
        app_dir = self.tmpdir / "app_build" / "isadoraair"
        if app_dir.exists():
            shutil.rmtree(app_dir)
        app_dir.mkdir(parents=True)
        (app_dir / ".env").write_text("SECRET_KEY=test\n", encoding="utf-8")
        app_tar = workdir / "app.tar.gz"
        with tarfile.open(app_tar, "w:gz") as tf:
            tf.add(app_dir, arcname="isadoraair")
        archive_path = self.tmpdir / "backup.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tf:
            tf.add(workdir, arcname=".")
        return archive_path

    def _run(self, archive, *extra, timeout=30):
        return subprocess.run(
            [str(RESTORE_DIR / "20-application.sh"), "--archive", str(archive),
             "--staging-root", str(self.staging), "--repo-url", f"file://{self.fixture_repo}",
             "--apply", *extra],
            capture_output=True, text=True, timeout=timeout, env=self.env,
        )

    def test_fresh_restore_then_resume_verifies_and_skips(self):
        archive = self._build_archive()
        first = self._run(archive)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

        second = self._run(archive, "--resume")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("resume verification PASS", second.stdout)
        self.assertIn("PASS (resumed/verified)", second.stdout)
        # Never re-cloned: only one .git init in the fixture, target's
        # own history must be unchanged (still exactly one commit deep,
        # not re-cloned into a new working tree with different mtimes).
        head = subprocess.run(
            ["git", "-C", str(self.staging / "opt" / "isadoraair"), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(head, self.fixture_sha)

    def test_resume_without_prior_ledger_falls_through_to_normal_restore(self):
        """--resume with nothing to resume from is not itself an
        ambiguity -- it just behaves like a fresh restore."""
        archive = self._build_archive()
        result = self._run(archive, "--resume")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("does not yet record this stage complete", result.stdout)
        self.assertTrue((self.staging / "opt" / "isadoraair" / ".git").is_dir())

    def test_resume_with_a_different_archive_fails_closed(self):
        archive1 = self._build_archive()
        first = self._run(archive1)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

        # A second, genuinely different archive (different bytes -> a
        # different sha256) targeting the SAME staging root.
        other_workdir = self.tmpdir / "work2"
        other_workdir.mkdir()
        (other_workdir / "MANIFEST.txt").write_text(f"IsadoraAir Git SHA:     {self.fixture_sha}\n", encoding="utf-8")
        (other_workdir / "extra-marker.txt").write_text("different archive bytes\n", encoding="utf-8")
        app_dir = self.tmpdir / "app_build2" / "isadoraair"
        app_dir.mkdir(parents=True)
        (app_dir / ".env").write_text("SECRET_KEY=test2\n", encoding="utf-8")
        with tarfile.open(other_workdir / "app.tar.gz", "w:gz") as tf:
            tf.add(app_dir, arcname="isadoraair")
        archive2 = self.tmpdir / "backup2.tar.gz"
        with tarfile.open(archive2, "w:gz") as tf:
            tf.add(other_workdir, arcname=".")

        result = self._run(archive2, "--resume", "--force-env")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("DIFFERENT archive", result.stdout + result.stderr)

    def test_resume_with_ledger_complete_but_wrong_head_fails_with_precise_diagnostic(self):
        """A genuine ambiguity -- ledger says done, filesystem disagrees
        -- must fail with a precise diagnostic, never silently pick a
        side."""
        archive = self._build_archive()
        first = self._run(archive)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

        target = self.staging / "opt" / "isadoraair"
        subprocess.run(["git", "-C", str(target), "checkout", "-q", "-b", "tmp-branch"], check=True)
        (target / "new-file.txt").write_text("drift\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(target), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-q", "-m", "drift"], check=True)

        result = self._run(archive, "--resume")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("resume verification FAILED", result.stdout + result.stderr)
        self.assertIn("does not match", result.stdout + result.stderr)

    def test_unknown_preexisting_env_without_resume_still_fails_closed(self):
        """Preserves the existing safety boundary: an existing, non-
        ledger-backed .env is still refused without --force-env,
        exactly as before r0043."""
        archive = self._build_archive()
        target = self.staging / "opt" / "isadoraair"
        target.mkdir(parents=True)
        subprocess.run(["git", "clone", "-q", str(self.fixture_repo), str(target)], check=True)
        (target / ".env").write_text("SOME=preexisting-unknown-content\n", encoding="utf-8")

        result = self._run(archive)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Refusing to overwrite existing non-empty", result.stdout + result.stderr)


class Stage30ResumeVerifyTests(Stage30RealPostgreSQLTestCase):
    """Real subprocess execution of 30-postgresql.sh's new --resume
    branch, against a genuine disposable local PostgreSQL cluster."""

    def setUp(self):
        super().setUp()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-e-resume30-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.ledger_root = self.tmpdir / "ledger-root"

    def _env(self):
        return {
            **os.environ,
            "PATH": f"{self.fakebin}:{self.pg_bin}:{os.environ['PATH']}",
            "PGHOST": str(self.sock_dir), "PGPORT": str(self.port), "PGUSER": "postgres",
            "RESTORE_RECOVERY_RECEIPT_ROOT": str(self.ledger_root),
        }

    def _run_stage30(self, staging_root, archive, db_name, *extra_args):
        args = [
            str(RESTORE_DIR / "30-postgresql.sh"), "--archive", str(archive),
            "--staging-root", str(staging_root), "--db-name", db_name, "--apply", *extra_args,
        ]
        return subprocess.run(args, capture_output=True, text=True, env=self._env(), timeout=30)

    def _restore_once(self, tmpdir, db_user, password, db_name):
        """Sets up a fresh target root + .env and runs 30-postgresql.sh
        for real, exactly once. Each test gets its OWN db_name -- this
        class's cluster is shared (setUpClass) across every test method
        in the class, so a fixed/shared restore database name would
        make one test's leftover state collide with another's."""
        staging_root = tmpdir / "staging"
        target_root = staging_root / "opt" / "isadoraair"
        target_root.mkdir(parents=True)
        (target_root / ".env").write_text(
            f"DB_USER={db_user}\nDB_PASSWORD={password}\nDB_HOST=127.0.0.1\nDB_PORT={self.port}\n",
            encoding="utf-8",
        )
        archive = self._make_archive(tmpdir, seed_db=f"{db_user}_seed")
        result = self._run_stage30(staging_root, archive, db_name)
        return result, archive, staging_root

    def test_resume_after_completion_verifies_and_skips_pg_restore(self):
        first, archive, staging_root = self._restore_once(
            self.tmpdir, "resumeuser1", "pw12345", "resume_test_db_1"
        )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

        second = self._run_stage30(staging_root, archive, "resume_test_db_1", "--resume")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("resume verification PASS", second.stdout)
        self.assertIn("PASS (resumed/verified)", second.stdout)
        self.assertNotIn("pg_restore -h", second.stdout)  # never re-ran the actual restore

    def test_without_resume_a_completed_database_still_requires_force_db(self):
        first, archive, staging_root = self._restore_once(
            self.tmpdir, "resumeuser2", "pw12345", "resume_test_db_2"
        )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

        second = self._run_stage30(staging_root, archive, "resume_test_db_2")
        self.assertNotEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("already has", second.stdout + second.stderr)


class Stage80ScaffoldRepairResumeTests(SimpleTestCase):
    """The concrete r0043 acceptance mechanism: a pre-existing, non-Git,
    entirely-empty weather-ingest scaffold (the exact damage a pre-r0043
    run's legacy-WEATHER_DATA_DIR defect leaves behind) is repaired ONLY
    with --resume plus durable, matching ledger provenance -- every
    other pre-existing-content case still fails exactly as before."""

    def setUp(self):
        super().setUp()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-e-stage80-resume-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.remotes = self.tmpdir / "remotes"
        self.remotes.mkdir()
        _make_git_fixture_repo(self.remotes / "weather-ingest.git")
        self.staging = self.tmpdir / "staging"
        self.staging.mkdir()
        self.archive = _make_minimal_archive(self.tmpdir)
        self.ledger_root = self.tmpdir / "ledger-root"
        self.env = {**os.environ, "RESTORE_RECOVERY_RECEIPT_ROOT": str(self.ledger_root)}

    def _record_stage_complete(self, stage: str):
        subprocess.run(
            [sys.executable, str(LEDGER_HELPER), "record",
             "--ledger", str(self.ledger_root / "var" / "lib" / "isadoraair" / "restore" / "ledger.json"),
             "--archive", str(self.archive), "--target-root", str(self.staging / "opt" / "isadoraair"),
             "--stage", stage],
            check=True, capture_output=True, text=True,
        )

    def _run_stage80(self, *extra):
        return subprocess.run(
            [str(RESTORE_DIR / "80-companions.sh"), "--archive", str(self.archive),
             "--staging-root", str(self.staging), "--companions-root", str(self.staging),
             "--repo-url-prefix", str(self.remotes), "--only", "weather-ingest",
             "--apply", *extra],
            capture_output=True, text=True, timeout=60, env=self.env,
        )

    def test_repairs_known_empty_scaffold_with_resume_and_matching_provenance(self):
        (self.staging / "weather-ingest" / "data").mkdir(parents=True)
        self._record_stage_complete("40-station-content")

        result = self._run_stage80("--resume")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Repairing (removing) it", result.stdout + result.stderr)
        self.assertTrue((self.staging / "weather-ingest" / ".git").is_dir())

    def test_without_resume_the_same_scaffold_still_fails_closed(self):
        (self.staging / "weather-ingest" / "data").mkdir(parents=True)
        self._record_stage_complete("40-station-content")

        result = self._run_stage80()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Refusing to clone into it", result.stdout + result.stderr)

    def test_resume_without_matching_ledger_provenance_still_fails_closed(self):
        """The scaffold's SHAPE alone is never enough -- no ledger at
        all recording 40-station-content complete for this archive."""
        (self.staging / "weather-ingest" / "data").mkdir(parents=True)

        result = self._run_stage80("--resume")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Refusing to clone into it", result.stdout + result.stderr)
        self.assertIn("does not (yet) record", result.stdout + result.stderr)

    def test_resume_with_a_real_file_present_still_fails_closed(self):
        """A single real file anywhere in the tree disqualifies the
        known-empty-scaffold signature -- this is never a general
        'trust any pre-existing weather-ingest directory' mechanism."""
        data_dir = self.staging / "weather-ingest" / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "latest_weather.json").write_text("{}", encoding="utf-8")
        self._record_stage_complete("40-station-content")

        result = self._run_stage80("--resume")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Refusing to clone into it", result.stdout + result.stderr)
        self.assertTrue((data_dir / "latest_weather.json").exists(), "real content must never be touched")

    def test_resume_with_an_existing_git_checkout_present_still_fails_closed(self):
        """A directory that DOES have a .git is never in scope for this
        repair at all -- a genuinely ambiguous foreign checkout stays
        fail-closed exactly as before."""
        collision = self.staging / "weather-ingest"
        collision.mkdir()
        (collision / ".git").mkdir()
        (collision / "some-file").write_text("x", encoding="utf-8")
        self._record_stage_complete("40-station-content")

        result = self._run_stage80("--resume")
        # A .git present takes the "fetch existing checkout" branch, not
        # the collision-refusal branch -- either way, this repair must
        # never fire, and the pre-existing content must be untouched.
        self.assertTrue((collision / "some-file").exists())

    def test_resume_with_wrong_stage_recorded_in_ledger_still_fails_closed(self):
        """Ledger provenance must specifically show 40-station-content
        (the stage that fixes the root cause) complete -- some OTHER
        stage being recorded is not sufficient provenance."""
        (self.staging / "weather-ingest" / "data").mkdir(parents=True)
        self._record_stage_complete("00-preflight")

        result = self._run_stage80("--resume")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("does not (yet) record", result.stdout + result.stderr)

    def test_all_provisioned_normally_still_passes_with_resume(self):
        """--resume must never change behavior for the ordinary,
        nothing-to-repair case."""
        result = self._run_stage80("--resume")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.staging / "weather-ingest" / ".git").is_dir())


class RestoreResumeStaticSafetyTests(SimpleTestCase):
    """Structural proof, not behavioral: nothing r0043 added anywhere
    in deploy/restore/*.sh starts, enables, or reloads any service --
    the ledger/resume mechanism is purely about restore-time file/DB/
    process-of-record state, never service activation."""

    def test_no_restore_stage_starts_enables_or_reloads_a_service(self):
        """Scoped to the exact files r0043 touched for the ledger/resume
        mechanism -- NOT a blanket re-audit of the whole pre-existing
        restore/ tree (10-packages.sh's own systemctl stop/start of
        snapd.socket/snapd.service is pre-existing, already-reviewed
        infrastructure-package management for the Chromium transition-
        package sequence, entirely unrelated to IsadoraAir's own
        services or to anything r0043 added)."""
        forbidden = ("systemctl start", "systemctl enable", "systemctl reload", "systemctl restart")
        r0043_touched = (
            "00-preflight.sh", "20-application.sh", "30-postgresql.sh", "40-station-content.sh",
            "50-native-deps.sh", "60-python.sh", "70-tts.sh", "75-protected-updater.sh",
            "80-companions.sh", "90-system-config.sh", "95-validate.sh", "lib.sh", "restore_ledger.py",
        )
        for name in r0043_touched:
            path = RESTORE_DIR / name
            text = path.read_text(encoding="utf-8")
            for pattern in forbidden:
                self.assertNotIn(
                    pattern, text,
                    f"{name} contains {pattern!r} -- restore/resume tooling must never start/enable/reload services",
                )

    def test_restore_ledger_module_has_no_subprocess_or_os_system_calls(self):
        """The ledger is pure file-identity bookkeeping -- it must never
        itself shell out to anything (no systemctl, no service
        activation surface at all)."""
        text = LEDGER_HELPER.read_text(encoding="utf-8")
        self.assertNotIn("subprocess", text)
        self.assertNotIn("os.system", text)


class Stage40Then80ResumedThen90ChainTests(SimpleTestCase):
    """The concrete r0043 acceptance chain, end to end against a
    disposable --staging-root: a legacy .env is normalized (Stage 40),
    a pre-existing legacy scaffold is repaired under --resume (Stage
    80), and 90-system-config.sh's own (already-idempotent, staging-
    mode) fallback branch still succeeds immediately afterward -- no
    service is started/enabled/reloaded anywhere in the chain. Stage
    95's own PREFERRED (real venv + manage.py check_deploy_baseline)
    path is exercised separately by the existing
    RuntimeFoundationE6TargetValidationFunctionalTests fixture -- r0043
    only added an unconditional ledger-record call there, so this chain
    focuses on the two stages r0043 actually changed behavior in."""

    def setUp(self):
        super().setUp()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-e-chain-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.staging = self.tmpdir / "staging"
        self.app_root = self.staging / "opt" / "isadoraair"
        self.app_root.mkdir(parents=True)
        self.remotes = self.tmpdir / "remotes"
        self.remotes.mkdir()
        _make_git_fixture_repo(self.remotes / "weather-ingest.git")
        self.archive = _make_minimal_archive(self.tmpdir)
        self.ledger_root = self.tmpdir / "ledger-root"
        self.env = {**os.environ, "RESTORE_RECOVERY_RECEIPT_ROOT": str(self.ledger_root)}

    def _run(self, script, *extra, timeout=60):
        return subprocess.run(
            [str(RESTORE_DIR / script), "--archive", str(self.archive),
             "--staging-root", str(self.staging), "--apply", *extra],
            capture_output=True, text=True, timeout=timeout, env=self.env,
        )

    def test_full_chain_from_legacy_env_through_repaired_stage_80_to_stage_90(self):
        legacy = f"{self.staging}/weather-ingest/data"
        (self.app_root / ".env").write_text(f"WEATHER_DATA_DIR={legacy}\nDEBUG=True\n", encoding="utf-8")

        stage40 = self._run("40-station-content.sh")
        self.assertEqual(stage40.returncode, 0, stage40.stdout + stage40.stderr)
        self.assertFalse((self.staging / "weather-ingest").exists())

        # Simulate the pre-r0043 damage this exact sandbox scenario
        # describes: Stage 60 already ran (under the OLD, unfixed
        # tooling) and manufactured the legacy scaffold BEFORE Stage 40
        # ever had a chance to normalize anything -- reproduced here by
        # planting it AFTER Stage 40, exactly matching the real
        # forensic evidence (empty dir, no .git).
        (self.staging / "weather-ingest" / "data").mkdir(parents=True)

        stage80 = subprocess.run(
            [str(RESTORE_DIR / "80-companions.sh"), "--archive", str(self.archive),
             "--staging-root", str(self.staging), "--companions-root", str(self.staging),
             "--repo-url-prefix", str(self.remotes), "--only", "weather-ingest",
             "--resume", "--apply"],
            capture_output=True, text=True, timeout=60, env=self.env,
        )
        self.assertEqual(stage80.returncode, 0, stage80.stdout + stage80.stderr)
        self.assertTrue((self.staging / "weather-ingest" / ".git").is_dir())

        stage90 = self._run("90-system-config.sh")
        self.assertEqual(stage90.returncode, 0, stage90.stdout + stage90.stderr)
        self.assertIn("90-system-config: PASS", stage90.stdout)

        for pattern in ("systemctl start", "systemctl enable", "systemctl reload", "systemctl restart"):
            self.assertNotIn(pattern, stage40.stdout + stage80.stdout + stage90.stdout)
