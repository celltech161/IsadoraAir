"""Codex Blocker 3: the configured PRODUCTION_MEDIA_ROOT must be a dedicated,
safe directory -- one policy (production/root_policy.py) for runtime, backup
and restore, proven at every layer:

* the policy itself (dangerous vs dedicated roots, symlinks, ancestors);
* .env parsing parity with python-decouple (what the runtime will really use);
* the Django runtime (layout, intake, sweeps) refusing before touching disk;
* the restore stage, really executed, in STAGED and in LIVE mode -- a hostile
  root is refused before ANY mutation, and with a safe root the only
  recursive ownership change is on <root>/media.
"""
import io
import os
import shutil
import stat
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings

from production import root_policy
from production.models import ProductionMedia
from production.services import intake, layout, reconcile

from .support import IsolatedMediaRootMixin, fixture

REPO = Path(__file__).resolve().parents[2]
POLICY = REPO / "production" / "root_policy.py"
STAGE_40 = REPO / "deploy" / "restore" / "40-station-content.sh"
CONTENT_ROOT_SAFETY = REPO / "deploy" / "restore" / "content_root_safety.py"
HOME = os.path.expanduser("~")


def run_policy(*args):
    return subprocess.run(["python3", str(POLICY), *args], capture_output=True, text=True, timeout=30)


class PolicyTests(SimpleTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rootpolicy-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def refused(self, root, code=None, **kwargs):
        with self.assertRaises(root_policy.RootPolicyError) as caught:
            root_policy.check_root(root, **kwargs)
        if code:
            self.assertEqual(caught.exception.code, code, root)
        return caught.exception

    def test_the_named_dangerous_roots_are_refused(self):
        for root in ("/", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/proc", "/sys", "/dev",
                     "/run", "/var", "/srv", "/srv/isadoraair", "/home", HOME, "/root", "/tmp", "/opt",
                     "/var/lib", "/var/lib/isadoraair", "/mnt", "/media"):
            with self.subTest(root=root):
                self.refused(root)

    def test_inside_a_system_tree_is_refused(self):
        for root in ("/etc/production-media", "/usr/local/pm", "/proc/1/pm", "/run/isadoraair/pm",
                     "/boot/pm", "/var/log/pm"):
            with self.subTest(root=root):
                self.refused(root, "root_system_location")

    def test_station_content_code_and_their_ancestors_and_descendants_are_refused(self):
        for root in ("/srv/isadoraair/music", "/srv/isadoraair/music/pm", "/srv/isadoraair/waveforms",
                     "/srv/isadoraair/voicetracks", "/srv/isadoraair/carts/x", "/var/lib/isadoraair/reports",
                     "/var/lib/isadoraair/runtime-recovery/pm", str(REPO), str(REPO / "pm")):
            with self.subTest(root=root):
                self.refused(root, protected=[str(REPO)])

    def test_a_configured_library_root_and_its_ancestor_are_refused(self):
        protected = ["/data/library/music"]
        self.refused("/data/library/music", "root_overlaps_protected", protected=protected)
        self.refused("/data/library", "root_overlaps_protected", protected=protected)     # ancestor
        self.refused("/data", "root_overlaps_protected", protected=protected)
        self.assertEqual(root_policy.check_root("/data/production-media", protected=protected),
                         "/data/production-media")

    def test_malformed_values_are_refused(self):
        for root, code in (("", "root_empty"), ("   ", "root_empty"), (None, "root_empty"),
                           ("relative/pm", "root_not_absolute"), ("/srv/../etc", "root_traversal"),
                           ("/srv/isadoraair/pm\n", "root_control_characters"), ("//", "root_is_filesystem_root"),
                           ("/./", "root_is_filesystem_root")):
            with self.subTest(root=root):
                self.refused(root, code)

    def test_a_symlink_resolving_to_a_protected_location_is_refused(self):
        for target in ("/etc", "/srv/isadoraair", "/", str(REPO)):
            link = self.tmp / f"link-{abs(hash(target))}"
            os.symlink(target, link)
            with self.subTest(target=target):
                self.refused(str(link), protected=[str(REPO)])
        # ...also through an intermediate symlinked directory component.
        os.symlink("/srv", self.tmp / "srvlink")
        self.refused(str(self.tmp / "srvlink" / "isadoraair" / "music" / "pm"))

    def test_safe_dedicated_roots_are_accepted(self):
        for root in ("/srv/isadoraair/production-media", "/mnt/stationdata/production-media",
                     "/media/disk2/production-media", "/var/lib/isadoraair/production-media",
                     "/data/production-media", str(self.tmp / "production-media")):
            with self.subTest(root=root):
                self.assertEqual(root_policy.check_root(root), os.path.normpath(root))

    def test_a_symlinked_root_to_a_dedicated_disk_is_accepted(self):
        target = self.tmp / "disk2" / "production-media"
        target.mkdir(parents=True)
        link = self.tmp / "production-media"
        os.symlink(target, link)
        self.assertEqual(root_policy.check_root(str(link), dedicated_path=str(link)), str(link))

    def test_the_dedicated_rule_refuses_an_existing_populated_directory(self):
        root = self.tmp / "pm"
        root.mkdir()
        for name in ("media", "incoming", "work", "locks", "lost+found"):
            (root / name).mkdir()
        self.assertEqual(root_policy.check_root(str(root), dedicated_path=str(root)), str(root))
        (root / "Music Collection").mkdir()
        error = self.refused(str(root), "root_not_dedicated", dedicated_path=str(root))
        self.assertIn("Music Collection", error.message)
        shutil.rmtree(root / "Music Collection")
        (root / "notes.txt").write_text("x")
        self.refused(str(root), "root_not_dedicated", dedicated_path=str(root))
        (root / "notes.txt").unlink()
        shutil.rmtree(root / "media")
        os.symlink("/etc", root / "media")
        self.refused(str(root), "root_managed_entry_invalid", dedicated_path=str(root))
        (root / "media").unlink()
        (root / "media").write_text("not a dir")
        self.refused(str(root), "root_managed_entry_invalid", dedicated_path=str(root))
        regular = self.tmp / "plainfile"
        regular.write_text("x")
        self.refused(str(regular), "root_not_a_directory", dedicated_path=str(regular))

    def test_the_cli_prints_the_accepted_root_or_refuses_with_exit_3(self):
        ok = run_policy("check", "--root", "/srv/isadoraair/production-media")
        self.assertEqual((ok.returncode, ok.stdout.strip()), (0, "/srv/isadoraair/production-media"))
        for root in ("/", "/etc", "/srv"):
            bad = run_policy("check", "--root", root)
            self.assertEqual(bad.returncode, 3, root)
            self.assertEqual(bad.stdout, "")
            self.assertRegex(bad.stderr, r"^root_[a-z_]+: ")


class EnvParsingParityTests(SimpleTestCase):
    """The restore and backup judge exactly the value the runtime will use."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rootpolicy-env-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, text):
        path = self.tmp / ".env"
        path.write_text(text)
        return path

    def test_parsing_matches_python_decouple_exactly(self):
        from decouple import RepositoryEnv
        samples = [
            "PRODUCTION_MEDIA_ROOT=/srv/isadoraair/production-media\n",
            "PRODUCTION_MEDIA_ROOT=/srv/safe\nPRODUCTION_MEDIA_ROOT=/etc\n",          # last wins
            "PRODUCTION_MEDIA_ROOT='/srv/quoted'\n",
            'PRODUCTION_MEDIA_ROOT="/srv/dquoted"\n',
            "  PRODUCTION_MEDIA_ROOT = /srv/spaced  \n# PRODUCTION_MEDIA_ROOT=/commented\n",
            "OTHER=1\n\nLIBRARY_ROOT=/data/music\n",
        ]
        for text in samples:
            with self.subTest(text=text):
                path = self.write(text)
                ours = root_policy.read_env_file(path)
                theirs = RepositoryEnv(str(path)).data
                self.assertEqual(ours, dict(theirs))

    def test_check_env_uses_the_last_assignment_and_the_env_station_paths(self):
        path = self.write("PRODUCTION_MEDIA_ROOT=/srv/isadoraair/production-media\nPRODUCTION_MEDIA_ROOT=/\n")
        self.assertEqual(run_policy("check-env", "--env-file", str(path)).returncode, 3)
        path = self.write("LIBRARY_ROOT=/data/music\nPRODUCTION_MEDIA_ROOT=/data\n")
        self.assertEqual(run_policy("check-env", "--env-file", str(path)).returncode, 3)
        missing = run_policy("check-env", "--env-file", str(self.tmp / "absent.env"))
        self.assertEqual((missing.returncode, missing.stdout.strip()), (0, root_policy.DEFAULT_ROOT))


class RuntimeEnforcementTests(IsolatedMediaRootMixin, TestCase):
    def test_unsafe_roots_are_refused_by_the_runtime_before_touching_disk(self):
        for root in ("/", "/etc", "/srv", "/srv/isadoraair", "/var", str(REPO), "/srv/isadoraair/music"):
            with self.subTest(root=root), override_settings(PRODUCTION_MEDIA_ROOT=root):
                with self.assertRaises(ImproperlyConfigured):
                    layout.media_root()
                with self.assertRaises(ImproperlyConfigured):
                    intake.ingest_stream(io.BytesIO(b"x" * 100), kind="upload", validate=False)
                with self.assertRaises(ImproperlyConfigured):
                    reconcile.sweep_stale_parts(apply=True)
        self.assertEqual(ProductionMedia.objects.count(), 0)

    def test_a_configured_library_root_protects_itself_at_runtime(self):
        library = self.root.parent / "music"
        with override_settings(LIBRARY_ROOT=str(library), PRODUCTION_MEDIA_ROOT=str(library / "pm")):
            with self.assertRaises(ImproperlyConfigured):
                layout.ensure_layout()
        self.assertFalse(library.exists())

    def test_a_non_dedicated_existing_root_is_refused_and_left_untouched(self):
        self.root.mkdir(parents=True)
        (self.root / "someone-elses-file").write_text("keep")
        with self.assertRaises(ImproperlyConfigured):
            intake.ingest_stream(io.BytesIO(fixture("wav16_mono.wav")), kind="upload", validate=False)
        with self.assertRaises(ImproperlyConfigured):
            reconcile.sweep_orphan_media(apply=True, now=None)
        self.assertEqual(sorted(os.listdir(self.root)), ["someone-elses-file"])

    def test_the_dedicated_test_root_is_accepted(self):
        layout.ensure_layout()
        self.assertEqual(sorted(os.listdir(self.root)), sorted(layout.SUBDIRECTORIES))


class _RestoreHarness(SimpleTestCase):
    """Runs the REAL deploy/restore/40-station-content.sh."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rootpolicy-restore-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.target = self.tmp / "opt" / "isadoraair"
        self.target.mkdir(parents=True)
        (self.tmp / "home").mkdir()
        self.archive = self.tmp / "backup.tar.gz"
        build_archive(self.archive)
        self.owner = f"{_id('-un')}:{_id('-gn')}"
        self.log = self.tmp / "mutations.log"

    def write_env(self, value):
        (self.target / ".env").write_text(f"PRODUCTION_MEDIA_ROOT={value}\n")

    def shim_dir(self, script):
        shims = self.tmp / "shims"
        shims.mkdir(exist_ok=True)
        for name in ("sudo", "mkdir", "chmod", "chown", "tar", "cp", "mv", "rm", "ln", "install"):
            path = shims / name
            path.write_text(script.replace("@NAME@", name))
            path.chmod(0o755)
        return shims

    def run_stage(self, *args, shims=None, staging=False):
        env = {**os.environ, "HOME": str(self.tmp / "home"), "RESTORE_RECOVERY_RECEIPT_ROOT": str(self.tmp / "receipts")}
        if shims is not None:
            env["PATH"] = f"{shims}:{env['PATH']}"
            env["SHIM_LOG"] = str(self.log)
            env["SHIM_ALLOW_PREFIX"] = str(self.tmp)
            env["SHIM_ALLOW_HELPER"] = str(CONTENT_ROOT_SAFETY)
        argv = [str(STAGE_40), "--archive", str(self.archive), "--owner", self.owner, *args]
        if staging:
            argv += ["--staging-root", str(self.tmp / "stage")]
        else:
            argv += ["--target-root", str(self.target)]
        return subprocess.run(argv, capture_output=True, text=True, timeout=120, env=env)

    def recorded(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


def _id(flag):
    return subprocess.run(["id", flag], capture_output=True, text=True, check=True).stdout.strip()


def build_archive(path):
    import tarfile
    with tarfile.open(path, "w:gz") as tar:
        def add(name, data=b"", mode=0o644, directory=False):
            info = tarfile.TarInfo(name)
            info.mode = mode
            if directory:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        for directory in ("./srv-content", "./srv-content/production-media", "./srv-content/production-media/media",
                          "./srv-content/production-media/media/ab"):
            add(directory, directory=True, mode=0o750)
        add("./srv-content/production-media/media/ab/ab" + "0" * 30, b"BYTES", mode=0o440)


REFUSE_EVERYTHING = """#!/bin/bash
printf '%s %s\\n' "@NAME@" "$*" >> "$SHIM_LOG"
exit 97
"""

# Executes ONLY mkdir/chmod/chown, or the restore content-root helper
# (deploy/restore/content_root_safety.py, which r0106 restore code uses for
# no-follow establishment and member-scoped ownership), whose every path
# operand lies inside the test tree; records everything; silently skips
# anything else (e.g. /srv/isadoraair).
SELECTIVE_SUDO = """#!/bin/bash
printf 'sudo %s\\n' "$*" >> "$SHIM_LOG"
cmd="$1"; shift
case "$cmd" in mkdir|chmod|chown|python3) ;; *) exit 0 ;; esac
for arg in "$@"; do
  case "$arg" in
    -*|*:*|[0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) ;;
    "$SHIM_ALLOW_PREFIX"/*) ;;
    "$SHIM_ALLOW_HELPER") ;;
    establish|chown-members) ;;
    *) exit 0 ;;
  esac
done
PATH="${PATH#*:}" exec "$cmd" "$@"
"""


class RestoreRefusalTests(_RestoreHarness):
    HOSTILE = ("/", "/etc", "/srv", "/srv/isadoraair", "/var", "/srv/isadoraair/music", str(REPO))

    def test_live_mode_refuses_hostile_roots_before_any_mutating_command(self):
        shims = self.shim_dir(REFUSE_EVERYTHING)
        for root in self.HOSTILE:
            with self.subTest(root=root):
                self.write_env(root)
                result = self.run_stage("--apply", shims=shims)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                # r0106: PRODUCTION_MEDIA_ROOT is also a managed station root of the
                # restore-time content policy, so a value such as "/" or "/var" that
                # CONTAINS the default reports root may already be refused by the
                # (earlier) REPORTS_ROOT check -- naming PRODUCTION_MEDIA_ROOT. Either
                # refusal happens before any mutation, which is what matters here.
                output = result.stdout + result.stderr
                self.assertIn("Refusing:", output)
                self.assertIn("PRODUCTION_MEDIA_ROOT", output)
                self.assertEqual(self.recorded(), [])               # not one mutating command ran

    def test_live_mode_refuses_a_symlink_to_a_protected_root_and_an_ancestor_of_the_library(self):
        shims = self.shim_dir(REFUSE_EVERYTHING)
        link = self.tmp / "innocent-looking"
        os.symlink("/srv/isadoraair", link)
        for env_text in (f"PRODUCTION_MEDIA_ROOT={link}\n",
                         "LIBRARY_ROOT=/data/station/music\nPRODUCTION_MEDIA_ROOT=/data/station\n"):
            with self.subTest(env=env_text):
                (self.target / ".env").write_text(env_text)
                result = self.run_stage("--apply", shims=shims)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(self.recorded(), [])

    def test_staged_mode_makes_the_same_live_decision_and_creates_nothing(self):
        stage_target = self.tmp / "stage" / "opt" / "isadoraair"
        stage_target.mkdir(parents=True)
        for root in self.HOSTILE:
            with self.subTest(root=root):
                (stage_target / ".env").write_text(f"PRODUCTION_MEDIA_ROOT={root}\n")
                result = self.run_stage("--apply", staging=True)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(sorted(os.listdir(self.tmp / "stage")), ["opt"])   # no srv/, nothing else

    def test_a_populated_existing_root_is_refused_in_staged_mode(self):
        stage_target = self.tmp / "stage" / "opt" / "isadoraair"
        stage_target.mkdir(parents=True)
        (stage_target / ".env").write_text("PRODUCTION_MEDIA_ROOT=/srv/isadoraair/production-media\n")
        populated = self.tmp / "stage" / "srv" / "isadoraair" / "production-media"
        populated.mkdir(parents=True)
        (populated / "unexpected").write_text("x")
        result = self.run_stage("--apply", staging=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a dedicated production-media directory", result.stdout + result.stderr)
        self.assertEqual(os.listdir(populated), ["unexpected"])


class RestoreSafeLiveOwnershipTests(_RestoreHarness):
    def test_live_apply_with_a_safe_root_never_recursively_chowns_beyond_media(self):
        root = self.tmp / "stationdisk" / "production-media"
        self.write_env(str(root))
        shims = self.tmp / "shims"
        shims.mkdir()
        (shims / "sudo").write_text(SELECTIVE_SUDO)
        (shims / "sudo").chmod(0o755)
        result = self.run_stage("--apply", shims=shims)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((root / "media" / "ab" / ("ab" + "0" * 30)).read_bytes(), b"BYTES")
        for name in ("media", "incoming", "work", "locks"):
            self.assertEqual(stat.S_IMODE((root / name).stat().st_mode), 0o750, name)
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o750)
        # r0106 reconciliation: the root and its four managed subdirectories are
        # established one directory at a time through the no-follow descriptor
        # primitive, and ownership of restored bytes is member-scoped -- there is
        # no recursive chown at all any more, and no path-based mkdir/chown/chmod.
        pm_lines = [line for line in self.recorded() if str(root) in line]
        helper = str(CONTENT_ROOT_SAFETY)
        for directory in (root, *(root / name for name in ("media", "incoming", "work", "locks"))):
            self.assertIn(
                f"sudo python3 -I {helper} establish --root {directory} --owner {self.owner} --mode 0750", pm_lines,
            )
        member_chowns = [line for line in pm_lines if " chown-members " in line]
        self.assertEqual(len(member_chowns), 1, pm_lines)
        self.assertTrue(member_chowns[0].startswith(
            f"sudo python3 -I {helper} chown-members --root {root}/media --owner {self.owner} --members-file "
        ), member_chowns)
        for line in pm_lines:
            self.assertNotRegex(line, r"^sudo (mkdir|chown|chmod) ", line)
        for line in self.recorded():
            self.assertNotIn("chown -R", line)
