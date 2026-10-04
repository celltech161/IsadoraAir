"""P0 1.2 -- REPORTS_ROOT restore path safety.

deploy/restore/40-station-content.sh used to take REPORTS_ROOT straight
from the restored .env and run `sudo mkdir -p`, `sudo chown` and finally
`sudo chown -R` against it. These tests prove the replacement:

  * content_root_safety.py refuses system trees, anchors and their
    ancestors, application/tooling checkouts, other managed station
    roots, and symlinks resolving into any of them -- judging the value
    python-decouple (and therefore Django) actually uses;
  * a LIVE-mode stage 40 run (no --staging-root) with a hostile value
    issues ZERO mutating commands -- proven with PATH shims that record
    and refuse sudo/chown/chmod/mkdir/tar/install;
  * a safe live apply never runs `chown -R`, changes ownership of exactly
    the restored members, and leaves pre-existing unrelated content in a
    dedicated reports directory untouched.

Nothing here touches a real system path: every live-mode run uses an
explicit --target-root under a temporary directory, the
RESTORE_RECOVERY_RECEIPT_ROOT seam for the ledger, and shims that never
execute a privileged command outside that directory.
"""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RESTORE_DIR = REPO_ROOT / "deploy" / "restore"
HELPER = RESTORE_DIR / "content_root_safety.py"
STAGE40 = RESTORE_DIR / "40-station-content.sh"

_spec = importlib.util.spec_from_file_location("content_root_safety", HELPER)
crs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crs)

OWNER = f"{os.getuid()}:{os.getgid()}"

HOSTILE_VALUES = (
    "/",
    "/etc",
    "/etc/isadoraair-reports",
    "/usr/local",
    "/var",
    "/var/lib",
    "/var/log/isadoraair",
    "/srv",
    "/srv/isadoraair",
    "/var/lib/isadoraair",
    "/var/lib/isadoraair/runtime-recovery",
    "/var/lib/isadoraair/restore/reports",
    "/home",
    "/opt",
    "/opt/isadoraair",
    "/opt/isadoraair/reports",
    "/opt/isadoraair-runtime",
    "/srv/isadoraair/music",
    "/srv/isadoraair/music/reports",
    "/srv/isadoraair/carts",
    "/var/lib/isadoraair/weather",
)

REFUSE_EVERYTHING = """#!/bin/bash
printf '%s %s\\n' "@NAME@" "$*" >> "$SHIM_LOG"
echo "shim: refusing @NAME@ $*" >&2
exit 97
"""

# Executes a privileged command (unprivileged -- the test owner is the
# invoking user) only when EVERY absolute-path argument lies inside the
# test's own temporary directory or is the helper itself; anything else
# (e.g. stage 40's /srv/isadoraair/carts or /var/lib/isadoraair/weather)
# is recorded and skipped, never executed.
SELECTIVE_SUDO = """#!/bin/bash
printf 'sudo %s\\n' "$*" >> "$SHIM_LOG"
for arg in "$@"; do
  case "$arg" in
    /*)
      case "$arg" in
        "$SHIM_ALLOW_PREFIX"/*|"$SHIM_ALLOW_HELPER") ;;
        *) printf 'SKIPPED sudo %s\\n' "$*" >> "$SHIM_LOG"; exit 0 ;;
      esac
      ;;
  esac
done
exec "$@"
"""


def _archive(path: Path, entries: dict[str, bytes | None], *, symlinks: dict[str, str] | None = None) -> Path:
    """entries: name -> bytes (regular file) or None (directory)."""
    with tarfile.open(path, "w:gz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.mtime = int(time.time())
            if data is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
            else:
                info.size = len(data)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(data))
        for name, target in (symlinks or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tf.addfile(info)
    return path


REPORTS_ARCHIVE = {
    "./reports": None,
    "./reports/royalty-2026-09.csv": b"track,plays\n",
    "./reports/q3": None,
    "./reports/q3/soundexchange.csv": b"isrc,plays\n",
}


class _TmpCase(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="isadoraair-p0-reports-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.app = self.tmp / "app"
        self.app.mkdir()
        self.env_file = self.app / ".env"

    def check(self, value: str | None, *, staging: Path | None = None, extra_env: str = "") -> str:
        lines = [] if value is None else [f"REPORTS_ROOT={value}"]
        self.env_file.write_text("\n".join(lines) + "\n" + extra_env, encoding="utf-8")
        return crs.check_root(
            key="REPORTS_ROOT",
            default=crs.MANAGED_ROOT_DEFAULTS["REPORTS_ROOT"],
            env_file=self.env_file,
            target_root=str(self.app),
            tooling_root=str(REPO_ROOT),
            staging_root=str(staging) if staging else None,
            home=os.environ.get("HOME"),
        )


class ContentRootPolicyTests(_TmpCase):
    def test_hostile_values_are_refused(self):
        for value in HOSTILE_VALUES:
            with self.subTest(value=value):
                with self.assertRaises(crs.UnsafeRootError):
                    self.check(value)

    def test_application_and_tooling_checkouts_are_refused(self):
        for value in (str(self.app), f"{self.app}/reports", str(REPO_ROOT), f"{REPO_ROOT}/reports",
                      str(REPO_ROOT.parent)):
            with self.subTest(value=value):
                with self.assertRaises(crs.UnsafeRootError):
                    self.check(value)

    def test_operator_home_and_its_ancestors_are_refused_but_a_dedicated_subdirectory_is_not(self):
        home = os.environ["HOME"]
        with self.assertRaises(crs.UnsafeRootError):
            self.check(home)
        with self.assertRaises(crs.UnsafeRootError):
            self.check(str(Path(home).parent))
        self.assertEqual(self.check(f"{self.tmp}/reports"), f"{self.tmp}/reports")

    def test_ancestor_of_a_configured_library_root_is_refused(self):
        with self.assertRaises(crs.UnsafeRootError) as ctx:
            self.check("/mnt/media", extra_env="LIBRARY_ROOT=/mnt/media/library\n")
        self.assertIn("LIBRARY_ROOT", str(ctx.exception))
        with self.assertRaises(crs.UnsafeRootError):
            self.check("/mnt/media/library/reports", extra_env="LIBRARY_ROOT=/mnt/media/library\n")

    def test_malformed_values_are_refused(self):
        for value in ("reports", "./reports", "/var/lib/isadoraair/../../etc", "/var/lib/isa\x07doraair/r"):
            with self.subTest(value=value):
                with self.assertRaises(crs.UnsafeRootError):
                    self.check(value)

    def test_symlink_into_a_system_tree_is_refused(self):
        link = self.tmp / "reports-link"
        link.symlink_to("/etc")
        with self.assertRaises(crs.UnsafeRootError) as ctx:
            self.check(str(link))
        self.assertIn("/etc", str(ctx.exception))
        nested = self.tmp / "nested-link"
        nested.symlink_to("/etc/ssl")
        with self.assertRaises(crs.UnsafeRootError):
            self.check(str(nested))

    def test_symlink_into_another_station_root_is_refused(self):
        library = self.tmp / "library"
        library.mkdir()
        link = self.tmp / "reports-link"
        link.symlink_to(library)
        with self.assertRaises(crs.UnsafeRootError):
            self.check(str(link), extra_env=f"LIBRARY_ROOT={library}\n")

    def test_existing_non_directory_is_refused(self):
        target = self.tmp / "afile"
        target.write_text("x")
        with self.assertRaises(crs.UnsafeRootError):
            self.check(str(target))

    def test_safe_values_are_accepted_including_a_separately_mounted_root(self):
        self.assertEqual(self.check(None), "/var/lib/isadoraair/reports")
        for value in ("/var/lib/isadoraair/reports", "/srv/isadoraair/reports", "/mnt/stationdata/reports",
                      "/media/station/reports", "/var/lib/isadoraair/reports/"):
            with self.subTest(value=value):
                self.assertEqual(self.check(value), value.rstrip("/"))

    def test_quoted_value_is_unquoted_like_decouple(self):
        self.assertEqual(self.check('"/mnt/stationdata/reports"'), "/mnt/stationdata/reports")

    def test_last_assignment_wins_like_decouple(self):
        self.env_file.write_text(
            "REPORTS_ROOT=/var/lib/isadoraair/reports\nREPORTS_ROOT=/etc\n", encoding="utf-8"
        )
        with self.assertRaises(crs.UnsafeRootError):
            crs.check_root(key="REPORTS_ROOT", default="/var/lib/isadoraair/reports", env_file=self.env_file,
                           target_root=str(self.app), tooling_root=str(REPO_ROOT), staging_root=None,
                           home=os.environ.get("HOME"))
        self.env_file.write_text(
            "REPORTS_ROOT=/etc\nREPORTS_ROOT=/mnt/stationdata/reports\n", encoding="utf-8"
        )
        self.assertEqual(
            crs.check_root(key="REPORTS_ROOT", default="/var/lib/isadoraair/reports", env_file=self.env_file,
                           target_root=str(self.app), tooling_root=str(REPO_ROOT), staging_root=None,
                           home=os.environ.get("HOME")),
            "/mnt/stationdata/reports",
        )

    def test_env_parser_matches_python_decouple(self):
        from decouple import RepositoryEnv

        self.env_file.write_text(
            "# comment\n"
            "REPORTS_ROOT=/etc\n"
            "  REPORTS_ROOT = '/mnt/a b/reports'  \n"
            "LIBRARY_ROOT=\"/mnt/lib\"\n"
            "WAVEFORMS_DIR='x\n"
            "NOEQUALS\n"
            "EMPTY=\n"
            "INLINE=/a # not a comment to decouple\n"
            "export WEATHER_DATA_DIR=/w\n",
            encoding="utf-8",
        )
        self.assertEqual(crs.read_decouple_env(self.env_file), RepositoryEnv(str(self.env_file)).data)

    def test_staged_run_judges_the_live_value(self):
        staging = self.tmp / "staging"
        staging.mkdir()
        for value in ("/etc", "/srv/isadoraair", "/"):
            with self.subTest(value=value):
                with self.assertRaises(crs.UnsafeRootError):
                    self.check(value, staging=staging)
        self.assertEqual(
            self.check("/var/lib/isadoraair/reports", staging=staging),
            f"{staging}/var/lib/isadoraair/reports",
        )

    def test_staged_symlink_escaping_the_staging_root_is_refused(self):
        staging = self.tmp / "staging"
        (staging / "var" / "lib").mkdir(parents=True)
        outside = self.tmp / "outside"
        outside.mkdir()
        (staging / "var" / "lib" / "isadoraair").symlink_to(outside)
        with self.assertRaises(crs.UnsafeRootError):
            self.check("/var/lib/isadoraair/reports", staging=staging)


class ArchiveMemberTests(_TmpCase):
    def test_plain_members_are_listed(self):
        archive = _archive(self.tmp / "a.tar.gz", {**REPORTS_ARCHIVE, "./other/x": b"x"})
        self.assertEqual(
            crs.validate_members(archive, "reports"),
            ["q3", "q3/soundexchange.csv", "royalty-2026-09.csv"],
        )

    def test_links_and_traversal_are_refused(self):
        cases = {
            "symlink": dict(entries=REPORTS_ARCHIVE, symlinks={"./reports/evil": "/etc"}),
            "traversal": dict(entries={**REPORTS_ARCHIVE, "./reports/../etc/passwd": b"x"}),
            "control": dict(entries={**REPORTS_ARCHIVE, "./reports/a\nb": b"x"}),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                archive = _archive(self.tmp / f"{label}.tar.gz", **kwargs)
                with self.assertRaises(crs.UnsafeRootError):
                    crs.validate_members(archive, "reports")


class ChownMembersTests(_TmpCase):
    def test_changes_exactly_the_listed_members(self):
        root = self.tmp / "reports"
        (root / "q3").mkdir(parents=True)
        (root / "q3" / "a.csv").write_text("a")
        unrelated = root / "unrelated.csv"
        unrelated.write_text("keep")
        before = unrelated.stat().st_ctime_ns
        time.sleep(0.02)
        self.assertEqual(crs.chown_members(str(root), OWNER, ["q3", "q3/a.csv"]), 2)
        self.assertEqual(unrelated.stat().st_ctime_ns, before)

    def test_symlinked_intermediate_component_is_refused_and_target_untouched(self):
        root = self.tmp / "reports"
        root.mkdir()
        victim_dir = self.tmp / "victim"
        victim_dir.mkdir()
        victim = victim_dir / "a.csv"
        victim.write_text("victim")
        (root / "q3").symlink_to(victim_dir)
        before = victim.stat().st_ctime_ns
        time.sleep(0.02)
        with self.assertRaises(OSError):
            crs.chown_members(str(root), OWNER, ["q3/a.csv"])
        self.assertEqual(victim.stat().st_ctime_ns, before)

    def test_symlinked_root_is_refused(self):
        real = self.tmp / "real"
        real.mkdir()
        link = self.tmp / "link"
        link.symlink_to(real)
        with self.assertRaises(OSError):
            crs.chown_members(str(link), OWNER, [])

    def test_missing_member_fails_closed(self):
        root = self.tmp / "reports"
        root.mkdir()
        with self.assertRaises(OSError):
            crs.chown_members(str(root), OWNER, ["absent.csv"])


class Stage40LiveModeTrapTests(_TmpCase):
    """Real subprocess runs of 40-station-content.sh in LIVE mode (no
    --staging-root, explicit temporary --target-root)."""

    def setUp(self):
        super().setUp()
        self.log = self.tmp / "shim.log"
        self.log.write_text("")
        self.receipts = self.tmp / "receipts"
        self.archive = _archive(self.tmp / "backup.tar.gz", REPORTS_ARCHIVE)

    def _shims(self, sudo_script: str | None) -> Path:
        shims = self.tmp / "shims"
        shims.mkdir(exist_ok=True)
        for name in ("sudo", "chown", "chmod", "mkdir", "tar", "install", "cp", "mv", "ln"):
            script = REFUSE_EVERYTHING.replace("@NAME@", name)
            if name == "sudo" and sudo_script is not None:
                script = sudo_script
            elif sudo_script is not None:
                continue  # a safe run: only sudo is intercepted; it execs the real tools
            path = shims / name
            path.write_text(script)
            path.chmod(0o755)
        return shims

    def _run(self, *extra, sudo_script: str | None = None, staging: Path | None = None, mode="--apply"):
        env = dict(os.environ)
        env["PATH"] = f"{self._shims(sudo_script)}:{env['PATH']}"
        env["SHIM_LOG"] = str(self.log)
        env["SHIM_ALLOW_PREFIX"] = str(self.tmp)
        env["SHIM_ALLOW_HELPER"] = str(HELPER)
        env["RESTORE_RECOVERY_RECEIPT_ROOT"] = str(self.receipts)
        env["TMPDIR"] = str(self.tmp)  # stage 40's member list lands inside the shim's allowed prefix
        args = [str(STAGE40), "--archive", str(self.archive), mode, "--target-root", str(self.app),
                "--owner", OWNER, "--stereotool-dir", str(self.tmp / "stereotool"), *extra]
        if staging is not None:
            args += ["--staging-root", str(staging)]
        return subprocess.run(args, capture_output=True, text=True, timeout=60, env=env)

    def _snapshot(self) -> set[str]:
        return {str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*") if "shims" not in p.parts}

    def test_hostile_live_values_issue_zero_mutating_commands(self):
        link = self.tmp / "etc-link"
        link.symlink_to("/etc")
        values = ["/", "/etc", "/srv", "/srv/isadoraair", "/var", "/var/lib/isadoraair", "/home",
                  os.environ["HOME"], str(REPO_ROOT), str(self.app), "/srv/isadoraair/music", str(link)]
        for value in values:
            with self.subTest(value=value):
                self.env_file.write_text(f"REPORTS_ROOT={value}\n", encoding="utf-8")
                before = self._snapshot()
                result = self._run()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("failed restore path-safety validation", result.stderr)
                self.assertEqual(self.log.read_text(), "", "a mutating command was attempted")
                self.assertEqual(self._snapshot(), before)

    def test_ancestor_of_configured_library_root_issues_zero_mutating_commands(self):
        library = self.tmp / "media" / "library"
        library.mkdir(parents=True)
        self.env_file.write_text(f"LIBRARY_ROOT={library}\nREPORTS_ROOT={library.parent}\n", encoding="utf-8")
        result = self._run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("LIBRARY_ROOT", result.stderr)
        self.assertEqual(self.log.read_text(), "")

    def test_duplicate_key_with_hostile_last_value_issues_zero_mutating_commands(self):
        self.env_file.write_text(
            f"REPORTS_ROOT={self.tmp}/reports\nREPORTS_ROOT=/etc\n", encoding="utf-8"
        )
        result = self._run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.log.read_text(), "")
        self.assertFalse((self.tmp / "reports").exists())

    def test_plan_mode_refuses_the_same_values(self):
        self.env_file.write_text("REPORTS_ROOT=/etc\n", encoding="utf-8")
        result = self._run(mode="--plan")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.log.read_text(), "")

    def test_staged_refusal_creates_nothing(self):
        staging = self.tmp / "staging"
        staging.mkdir()
        self.env_file.write_text("REPORTS_ROOT=/srv/isadoraair\n", encoding="utf-8")
        result = self._run(staging=staging)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(list(staging.iterdir()), [])
        self.assertEqual(self.log.read_text(), "")

    def test_safe_live_apply_is_member_scoped_and_never_recursive(self):
        reports = self.tmp / "stationdata" / "reports"
        reports.mkdir(parents=True)
        unrelated = reports / "operator-notes.txt"
        unrelated.write_text("pre-existing, not from this archive")
        unrelated_dir = reports / "older"
        unrelated_dir.mkdir()
        (unrelated_dir / "2024.csv").write_text("old")
        snapshot = {p: p.stat().st_ctime_ns for p in (unrelated, unrelated_dir, unrelated_dir / "2024.csv")}
        # A hostile FIRST assignment is ignored exactly as Django ignores it.
        self.env_file.write_text(f"REPORTS_ROOT=/etc\nREPORTS_ROOT={reports}\n", encoding="utf-8")
        time.sleep(0.02)
        start = time.time_ns()

        result = self._run(sudo_script=SELECTIVE_SUDO)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = self.log.read_text()
        self.assertNotIn(" -R", log)
        self.assertNotIn("/etc", log)
        executed = [line for line in log.splitlines() if line.startswith("sudo ") and str(self.tmp) in line]
        self.assertIn(f"sudo mkdir -p -- {reports}", executed)
        self.assertIn(f"sudo chown -h -- {OWNER} {reports}", executed)
        self.assertTrue(any("chown-members" in line and f"--root {reports}" in line for line in executed), log)
        for line in log.splitlines():
            if line.startswith("SKIPPED"):
                self.assertNotIn(str(reports), line)
        # Restored members exist and had their ownership set by this run.
        for rel in ("royalty-2026-09.csv", "q3", "q3/soundexchange.csv"):
            self.assertGreaterEqual((reports / rel).stat().st_ctime_ns, start, rel)
        # Pre-existing unrelated content is untouched (chown always bumps ctime).
        for path, ctime in snapshot.items():
            self.assertEqual(path.stat().st_ctime_ns, ctime, path)
        self.assertEqual(unrelated.read_text(), "pre-existing, not from this archive")
        self.assertEqual(list(self.tmp.glob("tmp.*")), [], "member list not cleaned up")

    def test_hostile_archive_member_is_refused_before_extraction(self):
        reports = self.tmp / "reports"
        self.archive = _archive(self.tmp / "evil.tar.gz", REPORTS_ARCHIVE, symlinks={"./reports/evil": "/etc"})
        self.env_file.write_text(f"REPORTS_ROOT={reports}\n", encoding="utf-8")
        result = self._run(sudo_script=SELECTIVE_SUDO)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("not a regular file or directory", result.stderr)
        self.assertFalse((reports / "evil").exists() or (reports / "evil").is_symlink())
        self.assertFalse((reports / "royalty-2026-09.csv").exists())
        self.assertNotIn("chown-members", self.log.read_text())


# ---------------------------------------------------------------------------
# P0 1.2 follow-up -- WEATHER_DATA_DIR through the same primitive.
# A restored WEATHER_DATA_DIR=/etc previously produced `sudo mkdir -p /etc`,
# `sudo chown $OWNER /etc` and `sudo chmod 0755 /etc` in a live restore.
# ---------------------------------------------------------------------------

WEATHER_HOSTILE_VALUES = (
    "/", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/proc", "/sys", "/dev",
    "/run", "/var", "/var/lib", "/srv", "/srv/isadoraair", "/home", "/root", "/tmp", "/mnt", "/media",
    "/etc/isadoraair/weather", "/run/isadoraair/weather",
    "/var/lib/isadoraair", "/opt/isadoraair", "/opt/isadoraair/weather", "/opt/isadoraair-runtime",
    "/var/lib/isadoraair-updater", "/var/lib/isadoraair-updater-bootstrap/weather",
    "/var/lib/isadoraair/runtime-recovery", "/var/lib/isadoraair/restore",
    "/var/lib/isadoraair/reports", "/var/lib/isadoraair/reports/weather",
    "/srv/isadoraair/music", "/srv/isadoraair/music/weather", "/srv/isadoraair/waveforms",
    "/srv/isadoraair/voicetracks", "/srv/isadoraair/aircheck", "/srv/isadoraair/carts",
    "/var/lib/isadoraair/encoders",
)

WEATHER_SAFE_VALUES = (
    "/var/lib/isadoraair/weather",
    "/srv/isadoraair/weather",
    "/mnt/stationdata/weather",
    "/media/stationdisk/weather",
)


class _WeatherCase(_TmpCase):
    def check_weather(self, env_text: str, *, staging: Path | None = None, value: str | None = None) -> str:
        self.env_file.write_text(env_text, encoding="utf-8")
        return crs.check_root(
            key="WEATHER_DATA_DIR",
            default=crs.MANAGED_ROOT_DEFAULTS["WEATHER_DATA_DIR"],
            env_file=self.env_file,
            target_root=str(self.app),
            tooling_root=str(REPO_ROOT),
            staging_root=str(staging) if staging else None,
            home=os.environ.get("HOME"),
            value=value,
        )


class WeatherRootPolicyTests(_WeatherCase):
    def test_hostile_values_are_refused(self):
        for value in WEATHER_HOSTILE_VALUES:
            with self.subTest(value=value):
                with self.assertRaises(crs.UnsafeRootError):
                    self.check_weather(f"WEATHER_DATA_DIR={value}\n")

    def test_checkouts_home_and_service_state_are_refused(self):
        home = os.environ["HOME"]
        for value in (str(REPO_ROOT), f"{REPO_ROOT}/weather", str(self.app), f"{self.app}/data/weather",
                      home, f"{home}/.local/state/isadoraair", f"{home}/.local/state/isadoraair/weather"):
            with self.subTest(value=value):
                with self.assertRaises(crs.UnsafeRootError):
                    self.check_weather(f"WEATHER_DATA_DIR={value}\n")

    def test_configured_station_roots_and_their_ancestors_are_refused(self):
        env = ("LIBRARY_ROOT=/mnt/media/library\nWAVEFORMS_DIR=/mnt/wave/forms\n"
               "REPORTS_ROOT=/mnt/stationdata/reports\n")
        for value in ("/mnt/media/library", "/mnt/media", "/mnt/media/library/weather", "/mnt/wave",
                      "/mnt/stationdata/reports", "/mnt/stationdata"):
            with self.subTest(value=value):
                with self.assertRaises(crs.UnsafeRootError):
                    self.check_weather(env + f"WEATHER_DATA_DIR={value}\n")

    def test_symlinks_into_protected_trees_are_refused(self):
        etc_link = self.tmp / "etc-link"
        etc_link.symlink_to("/etc")
        library = self.tmp / "library"
        library.mkdir()
        library_link = self.tmp / "library-link"
        library_link.symlink_to(library)
        with self.assertRaises(crs.UnsafeRootError):
            self.check_weather(f"WEATHER_DATA_DIR={etc_link}\n")
        with self.assertRaises(crs.UnsafeRootError):
            self.check_weather(f"LIBRARY_ROOT={library}\nWEATHER_DATA_DIR={library_link}\n")

    def test_malformed_and_empty_values_are_refused(self):
        for value in ("weather", "./weather", "/var/lib/isadoraair/../../etc", "/var/lib/isa\x1bdora/weather",
                      ""):
            with self.subTest(value=value):
                with self.assertRaises(crs.UnsafeRootError):
                    self.check_weather(f"WEATHER_DATA_DIR={value}\n")

    def test_dedicated_roots_are_accepted(self):
        self.assertEqual(self.check_weather(""), "/var/lib/isadoraair/weather")
        for value in WEATHER_SAFE_VALUES:
            with self.subTest(value=value):
                self.assertEqual(self.check_weather(f"WEATHER_DATA_DIR='{value}'\n"), value)
        dedicated = self.tmp / "stationdata" / "weather"
        self.assertEqual(self.check_weather(f"WEATHER_DATA_DIR={dedicated}\n"), str(dedicated))

    def test_last_assignment_wins_like_decouple(self):
        with self.assertRaises(crs.UnsafeRootError):
            self.check_weather("WEATHER_DATA_DIR=/var/lib/isadoraair/weather\nWEATHER_DATA_DIR=/etc\n")
        self.assertEqual(
            self.check_weather("WEATHER_DATA_DIR=/etc\nWEATHER_DATA_DIR=/mnt/stationdata/weather\n"),
            "/mnt/stationdata/weather",
        )

    def test_value_override_is_judged_instead_of_env(self):
        self.assertEqual(
            self.check_weather("WEATHER_DATA_DIR=/etc\n", value="/var/lib/isadoraair/weather"),
            "/var/lib/isadoraair/weather",
        )
        with self.assertRaises(crs.UnsafeRootError):
            self.check_weather("", value="/etc")

    def test_staged_run_judges_the_live_value(self):
        staging = self.tmp / "staging"
        staging.mkdir()
        for value in ("/etc", "/srv/isadoraair", "/", "/var/lib/isadoraair/reports"):
            with self.subTest(value=value):
                with self.assertRaises(crs.UnsafeRootError):
                    self.check_weather(f"WEATHER_DATA_DIR={value}\n", staging=staging)
        self.assertEqual(
            self.check_weather("WEATHER_DATA_DIR=/srv/isadoraair/weather\n", staging=staging),
            f"{staging}/srv/isadoraair/weather",
        )

    def test_each_key_is_never_refused_for_its_own_default(self):
        self.env_file.write_text("", encoding="utf-8")
        for key, default in crs.MANAGED_ROOT_DEFAULTS.items():
            with self.subTest(key=key):
                self.assertEqual(
                    crs.check_root(key=key, default=default, env_file=self.env_file, target_root=str(self.app),
                                   tooling_root=str(REPO_ROOT), staging_root=None,
                                   home=os.environ.get("HOME")),
                    os.path.realpath(default),
                )

    def test_assigned_empty_reports_root_is_refused_not_defaulted(self):
        """decouple returns '' for an assigned-but-empty key (Django would use it),
        so the restore must not silently substitute the default."""
        self.env_file.write_text("REPORTS_ROOT=\n", encoding="utf-8")
        with self.assertRaises(crs.UnsafeRootError):
            crs.check_root(key="REPORTS_ROOT", default="/var/lib/isadoraair/reports", env_file=self.env_file,
                           target_root=str(self.app), tooling_root=str(REPO_ROOT), staging_root=None,
                           home=os.environ.get("HOME"))
        self.assertEqual(crs.effective_value(crs.read_decouple_env(self.env_file), "REPORTS_ROOT"), "")


class EstablishRootTests(_TmpCase):
    def test_creates_with_exact_mode_and_leaves_contents_alone(self):
        root = self.tmp / "stationdata" / "weather"
        crs.establish_root(str(root), OWNER, 0o755)
        self.assertTrue(root.is_dir())
        self.assertEqual(root.stat().st_mode & 0o7777, 0o755)
        child = root / "observations.json"
        child.write_text("{}")
        root.chmod(0o700)
        before = child.stat().st_ctime_ns
        time.sleep(0.02)
        crs.establish_root(str(root), OWNER, 0o755)
        self.assertEqual(root.stat().st_mode & 0o7777, 0o755)
        self.assertEqual(child.stat().st_ctime_ns, before)

    def test_symlinked_root_or_ancestor_is_refused_and_target_untouched(self):
        victim = self.tmp / "victim"
        victim.mkdir()
        victim.chmod(0o700)
        before = victim.stat().st_ctime_ns
        (self.tmp / "link").symlink_to(victim)
        time.sleep(0.02)
        for root in (self.tmp / "link", self.tmp / "link" / "weather"):
            with self.subTest(root=str(root)):
                with self.assertRaises(OSError):
                    crs.establish_root(str(root), OWNER, 0o755)
        self.assertEqual(victim.stat().st_ctime_ns, before)
        self.assertEqual(victim.stat().st_mode & 0o7777, 0o700)
        self.assertFalse((victim / "weather").exists())

    def test_unnormalized_root_is_refused(self):
        for root in ("/", "relative", f"{self.tmp}/a/../b", f"{self.tmp}/a/"):
            with self.subTest(root=root):
                with self.assertRaises(crs.UnsafeRootError):
                    crs.establish_root(root, None, 0o755)


class WeatherStage40LiveModeTrapTests(Stage40LiveModeTrapTests):
    """The same real subprocess/PATH-shim harness as REPORTS_ROOT's, aimed at
    WEATHER_DATA_DIR (REPORTS_ROOT left at its safe default)."""

    def setUp(self):
        super().setUp()
        self.archive = _archive(self.tmp / "backup.tar.gz", {"./other": None})

    # The inherited REPORTS_ROOT tests also run here against this archive-less
    # fixture; disable the two that need reports/ content in the archive.
    test_safe_live_apply_is_member_scoped_and_never_recursive = None
    test_hostile_archive_member_is_refused_before_extraction = None

    def test_hostile_live_weather_values_issue_zero_mutating_commands(self):
        library = self.tmp / "media" / "library"
        library.mkdir(parents=True)
        etc_link = self.tmp / "etc-link"
        etc_link.symlink_to("/etc")
        library_link = self.tmp / "library-link"
        library_link.symlink_to(library)
        cases = {
            "/": "", "/etc": "", "/srv": "", "/srv/isadoraair": "", str(REPO_ROOT): "",
            str(library): f"LIBRARY_ROOT={library}\n",
            str(library.parent): f"LIBRARY_ROOT={library}\n",
            str(etc_link): "",
            str(library_link): f"LIBRARY_ROOT={library}\n",
        }
        for value, prefix in cases.items():
            with self.subTest(value=value):
                self.env_file.write_text(prefix + f"WEATHER_DATA_DIR={value}\n", encoding="utf-8")
                before = self._snapshot()
                result = self._run()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                # "/" also contains the default reports root, so REPORTS_ROOT's
                # check (which runs first) may be the one that refuses.
                self.assertIn("failed restore path-safety validation", result.stderr)
                self.assertIn("WEATHER_DATA_DIR", result.stderr)
                self.assertEqual(self.log.read_text(), "", "a mutating command was attempted")
                self.assertEqual(self._snapshot(), before)

    def test_duplicate_weather_key_with_hostile_last_value_issues_zero_mutating_commands(self):
        self.env_file.write_text(
            f"WEATHER_DATA_DIR={self.tmp}/weather\nWEATHER_DATA_DIR=/etc\n", encoding="utf-8"
        )
        result = self._run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.log.read_text(), "")
        self.assertFalse((self.tmp / "weather").exists())

    def test_plan_mode_refuses_hostile_weather_value(self):
        self.env_file.write_text("WEATHER_DATA_DIR=/etc\n", encoding="utf-8")
        result = self._run(mode="--plan")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.log.read_text(), "")

    def test_staged_run_with_hostile_live_weather_value_creates_nothing(self):
        staging = self.tmp / "staging"
        staging.mkdir()
        for value in ("/etc", "/srv/isadoraair"):
            with self.subTest(value=value):
                self.env_file.write_text(f"WEATHER_DATA_DIR={value}\n", encoding="utf-8")
                result = self._run(staging=staging)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("WEATHER_DATA_DIR from", result.stderr)
                self.assertEqual(list(staging.iterdir()), [])
                self.assertEqual(self.log.read_text(), "")

    def test_safe_live_apply_establishes_exactly_the_dedicated_directory(self):
        weather = self.tmp / "stationdata" / "weather"
        weather.mkdir(parents=True)
        existing = weather / "observations.json"
        existing.write_text("{}")
        weather.chmod(0o700)
        before = existing.stat().st_ctime_ns
        # A hostile FIRST assignment is ignored exactly as Django ignores it.
        self.env_file.write_text(f"WEATHER_DATA_DIR=/etc\nWEATHER_DATA_DIR={weather}\n", encoding="utf-8")
        time.sleep(0.02)

        result = self._run(sudo_script=SELECTIVE_SUDO)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = self.log.read_text()
        self.assertNotIn("/etc", log)
        self.assertNotIn(" -R", log)
        weather_lines = [line for line in log.splitlines() if str(weather) in line]
        self.assertEqual(
            weather_lines,
            [f"sudo python3 -I {HELPER} establish --root {weather} --owner {OWNER} --mode 0755"],
        )
        self.assertEqual(weather.stat().st_mode & 0o7777, 0o755)
        self.assertEqual(existing.stat().st_ctime_ns, before)
        self.assertEqual(existing.read_text(), "{}")

    def test_staged_safe_apply_establishes_the_staged_directory(self):
        staging = self.tmp / "staging"
        staging.mkdir()
        self.env_file.write_text("WEATHER_DATA_DIR=/mnt/stationdata/weather\n", encoding="utf-8")
        result = self._run(staging=staging, sudo_script=SELECTIVE_SUDO)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        staged = staging / "mnt" / "stationdata" / "weather"
        self.assertTrue(staged.is_dir())
        self.assertEqual(staged.stat().st_mode & 0o7777, 0o755)
        self.assertEqual(self.log.read_text(), "")
