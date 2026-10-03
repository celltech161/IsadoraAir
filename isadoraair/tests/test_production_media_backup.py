"""iPortal production media in backup and restore (P1 2.22A).

Permanent ProductionMedia bytes (<root>/media) are durable station content and
are backed up; incoming/, work/ and locks/ are transient and are not. The rule
is executed for real against temporary trees -- never a production path:

* deploy/stage_production_media.sh   what the nightly backup copies
* deploy/restore/40-station-content.sh   what a restore recreates
* deploy/restore/inspect_backup.sh   what the archive inspector reports

The nightly script itself needs production secrets and an SFTP target, so (like
every other test of it) only its static wiring is asserted here.
"""
import io
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

DEPLOY_DIR = Path(__file__).resolve().parent.parent.parent / "deploy"
RESTORE_DIR = DEPLOY_DIR / "restore"
BACKUP_SCRIPT = DEPLOY_DIR / "backup_isadoraair.sh"
STAGE_HELPER = DEPLOY_DIR / "stage_production_media.sh"
STATION_CONTENT = RESTORE_DIR / "40-station-content.sh"
INSPECT = RESTORE_DIR / "inspect_backup.sh"

SHARD = "ab"
KEY = SHARD + "0" * 30                      # a plausible 32-hex media file name
MEDIA_BYTES = b"IMMUTABLE-MEDIA-BYTES"


def listing(root: Path):
    return sorted(
        str(path.relative_to(root)) + ("/" if path.is_dir() else "")
        for path in root.rglob("*")
    )


class TempDirCase(SimpleTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pmbackup-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


def build_store(root: Path, *, media=True, transient=True):
    """A realistic production-media root: media/ab/<key> plus transient trees."""
    if media:
        (root / "media" / SHARD).mkdir(parents=True)
        permanent = root / "media" / SHARD / KEY
        permanent.write_bytes(MEDIA_BYTES)
        permanent.chmod(0o440)
    if transient:
        (root / "incoming").mkdir(parents=True, exist_ok=True)
        (root / "incoming" / ("f" * 32 + ".part")).write_bytes(b"partial upload")
        (root / "work" / ("e" * 32)).mkdir(parents=True, exist_ok=True)
        (root / "work" / ("e" * 32) / "scratch.wav").write_bytes(b"scratch")
        (root / "locks").mkdir(parents=True, exist_ok=True)
        (root / "locks" / "x.lock").write_bytes(b"")


class StageHelperFunctionalTests(TempDirCase):
    def stage(self, root, dest):
        return subprocess.run([str(STAGE_HELPER), str(root), str(dest)], capture_output=True, text=True, timeout=30)

    def test_the_helper_is_executable_with_valid_syntax(self):
        self.assertTrue(os.access(STAGE_HELPER, os.X_OK))
        subprocess.run(["bash", "-n", str(STAGE_HELPER)], check=True)

    def test_only_permanent_media_is_staged_and_every_transient_tree_is_excluded(self):
        root, dest = self.tmp / "production-media", self.tmp / "dest"
        dest.mkdir()
        build_store(root)
        result = self.stage(root, dest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(listing(dest), [
            "production-media/", "production-media/media/", f"production-media/media/{SHARD}/",
            f"production-media/media/{SHARD}/{KEY}",
        ])
        staged = dest / "production-media" / "media" / SHARD / KEY
        self.assertEqual(staged.read_bytes(), MEDIA_BYTES)
        self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o440)        # modes preserved
        self.assertIn("1 file(s)", result.stdout)

    def test_incoming_work_and_locks_never_appear_even_when_media_is_empty(self):
        root, dest = self.tmp / "pm", self.tmp / "dest"
        dest.mkdir()
        (root / "media").mkdir(parents=True)
        build_store(root, media=False)
        self.assertEqual(self.stage(root, dest).returncode, 0)
        self.assertEqual(listing(dest), ["production-media/", "production-media/media/"])

    def test_no_store_at_all_is_not_an_error_and_creates_nothing(self):
        dest = self.tmp / "dest"
        dest.mkdir()
        for root in (self.tmp / "absent", ):
            result = self.stage(root, dest)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("no production media found", result.stdout)
        self.assertEqual(listing(dest), [])

    def test_a_store_with_only_transient_data_stages_nothing(self):
        root, dest = self.tmp / "pm", self.tmp / "dest"
        dest.mkdir()
        build_store(root, media=False)
        self.assertEqual(self.stage(root, dest).returncode, 0)
        self.assertEqual(listing(dest), [])

    def test_a_symlinked_media_directory_fails_closed_rather_than_skipping_durable_content(self):
        root, dest = self.tmp / "pm", self.tmp / "dest"
        dest.mkdir()
        root.mkdir()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, root / "media")
        result = self.stage(root, dest)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(listing(dest), [])

    def test_a_regular_file_where_media_should_be_fails_closed(self):
        root, dest = self.tmp / "pm", self.tmp / "dest"
        dest.mkdir()
        root.mkdir()
        (root / "media").write_text("not a directory")
        self.assertEqual(self.stage(root, dest).returncode, 3)

    def test_unsafe_arguments_are_refused(self):
        dest = self.tmp / "dest"
        dest.mkdir()
        for root in ("relative/path", "/srv/../etc", ""):
            with self.subTest(root=root):
                self.assertEqual(self.stage(root, dest).returncode, 2)
        self.assertEqual(self.stage(self.tmp, self.tmp / "no-such-destination").returncode, 2)
        self.assertEqual(subprocess.run([str(STAGE_HELPER)], capture_output=True).returncode, 2)
        self.assertEqual(subprocess.run([str(STAGE_HELPER), "/a", "/b", "/c"], capture_output=True).returncode, 2)
        self.assertEqual(listing(dest), [])

    def test_staging_twice_into_the_same_destination_never_nests(self):
        root, dest = self.tmp / "pm", self.tmp / "dest"
        dest.mkdir()
        build_store(root)
        self.assertEqual(self.stage(root, dest).returncode, 0)
        self.assertEqual(self.stage(root, dest).returncode, 2)
        self.assertFalse((dest / "production-media" / "media" / "media").exists())

    def test_paths_with_spaces_are_handled(self):
        root, dest = self.tmp / "with space" / "pm", self.tmp / "dest dir"
        dest.mkdir()
        build_store(root)
        self.assertEqual(self.stage(root, dest).returncode, 0)
        self.assertTrue((dest / "production-media" / "media" / SHARD / KEY).is_file())

    def test_the_source_store_is_never_modified(self):
        root, dest = self.tmp / "pm", self.tmp / "dest"
        dest.mkdir()
        build_store(root)
        before = listing(root)
        self.stage(root, dest)
        self.assertEqual(listing(root), before)
        self.assertEqual((root / "media" / SHARD / KEY).read_bytes(), MEDIA_BYTES)


class BackupScriptWiringTests(SimpleTestCase):
    """Static wiring of the nightly script (it cannot run without secrets)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.text = BACKUP_SCRIPT.read_text()

    def test_script_syntax_is_valid(self):
        subprocess.run(["bash", "-n", str(BACKUP_SCRIPT)], check=True)

    def test_it_stages_only_through_the_helper_with_the_configured_root(self):
        self.assertIn('"$SCRIPT_DIR/stage_production_media.sh" "$PRODUCTION_MEDIA_ROOT" "$WORKDIR/srv-content"', self.text)
        self.assertIn("grep -E '^PRODUCTION_MEDIA_ROOT=' \"$ENV_FILE\"", self.text)
        self.assertIn('PRODUCTION_MEDIA_ROOT="${PRODUCTION_MEDIA_ROOT:-/srv/isadoraair/production-media}"', self.text)

    def test_the_whole_root_is_never_a_copy_source(self):
        for lineno, line in enumerate(self.text.splitlines(), start=1):
            if line.strip().startswith("#"):
                continue
            if re.search(r"\b(cp|tar|rsync)\b", line) and "PRODUCTION_MEDIA_ROOT" in line:
                self.fail(f"line {lineno} copies the production-media root directly: {line!r}")
            for transient in ("incoming", "locks"):
                if re.search(rf"production-media/{transient}", line):
                    self.assertTrue(
                        line.strip().startswith("#") or "transient" in line.lower() or "not backed up" in line.lower()
                        or "{incoming" in line, f"line {lineno} mentions {transient} outside documentation: {line!r}")

    def test_the_stage_runs_between_station_content_and_reports(self):
        order = [m.start() for m in (re.search(rf'CURRENT_STAGE="{name}"', self.text)
                                     for name in ("srv_content", "production_media", "reports"))]
        self.assertEqual(order, sorted(order))
        self.assertTrue(all(order))

    def test_the_manifest_documents_inclusion_and_exclusion(self):
        self.assertIn("srv-content/production-media/media/", self.text)
        self.assertRegex(self.text, r"production-media/\{incoming,work,locks\}")

    def test_a_helper_failure_aborts_the_backup(self):
        self.assertTrue(re.search(r"^set -[a-z]*e", self.text, re.MULTILINE) or "set -euo pipefail" in self.text)
        block = self.text[self.text.index('CURRENT_STAGE="production_media"'):self.text.index('CURRENT_STAGE="reports"')]
        self.assertNotIn("|| true\n\"$SCRIPT_DIR", block)
        self.assertNotRegex(block.splitlines()[-3], r"\|\|")


class RestoreStageFunctionalTests(TempDirCase):
    """deploy/restore/40-station-content.sh, really executed under --staging-root."""

    def archive(self, *, media=True, transient_in_archive=True, extra=()):
        path = self.tmp / "backup.tar.gz"
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
            add("./srv-content", directory=True, mode=0o755)
            add("./srv-content/carts", directory=True, mode=0o755)
            add("./srv-content/carts/cart.wav", b"cart")
            if media:
                add("./srv-content/production-media", directory=True, mode=0o755)
                add("./srv-content/production-media/media", directory=True, mode=0o750)
                add(f"./srv-content/production-media/media/{SHARD}", directory=True, mode=0o750)
                add(f"./srv-content/production-media/media/{SHARD}/{KEY}", MEDIA_BYTES, mode=0o440)
            if transient_in_archive:
                # An (illegitimate) archive carrying transient trees: restore must ignore them.
                add("./srv-content/production-media/incoming/x.part", b"partial")
                add("./srv-content/production-media/work/scratch", b"scratch")
                add("./srv-content/production-media/locks/x.lock", b"")
            for name, data in extra:
                add(name, data)
        return path

    def restore(self, archive, *args, env_file=None):
        stage = self.tmp / "stage"
        stage.mkdir(exist_ok=True)
        if env_file is not None:
            target = stage / "opt" / "isadoraair"
            target.mkdir(parents=True, exist_ok=True)
            (target / ".env").write_text(env_file)
        owner = f"{os.getlogin() if False else subprocess.run(['id', '-un'], capture_output=True, text=True).stdout.strip()}:" \
                f"{subprocess.run(['id', '-gn'], capture_output=True, text=True).stdout.strip()}"
        result = subprocess.run(
            [str(STATION_CONTENT), "--archive", str(archive), "--staging-root", str(stage), "--owner", owner, *args],
            capture_output=True, text=True, timeout=60, env={**os.environ, "HOME": str(self.tmp / "home")},
        )
        return result, stage / "srv" / "isadoraair" / "production-media"

    def test_permanent_media_is_restored_byte_identical_with_its_restrictive_mode(self):
        result, root = self.restore(self.archive(), "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        restored = root / "media" / SHARD / KEY
        self.assertEqual(restored.read_bytes(), MEDIA_BYTES)
        self.assertEqual(stat.S_IMODE(restored.stat().st_mode), 0o440)
        self.assertIn("production media: restored, 1 file(s)", result.stdout)
        self.assertIn("production_reconcile", result.stdout)            # the post-restore verification step is named

    def test_transient_directories_are_recreated_empty_with_restrictive_modes(self):
        result, root = self.restore(self.archive(), "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in ("incoming", "work", "locks"):
            with self.subTest(directory=name):
                self.assertTrue((root / name).is_dir())
                self.assertEqual(list((root / name).iterdir()), [])            # nothing from the archive leaked in
        for directory in (root, root / "media", root / "incoming", root / "work", root / "locks"):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o750, directory.name)

    def test_only_the_media_subtree_is_ever_extracted(self):
        _result, root = self.restore(self.archive(), "--apply")
        files = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
        self.assertEqual(files, [f"media/{SHARD}/{KEY}"])

    def test_the_existing_station_content_is_still_restored(self):
        result, _root = self.restore(self.archive(), "--apply")
        self.assertEqual((self.tmp / "stage" / "srv" / "isadoraair" / "carts" / "cart.wav").read_bytes(), b"cart")

    def test_an_archive_without_production_media_creates_empty_directories_and_warns(self):
        result, root = self.restore(self.archive(media=False, transient_in_archive=False), "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("archive has no srv-content/production-media/media entries", result.stdout + result.stderr)
        for name in ("media", "incoming", "work", "locks"):
            self.assertEqual(list((root / name).iterdir()), [])

    def test_plan_mode_changes_nothing_and_describes_the_extraction(self):
        result, root = self.restore(self.archive())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(root.exists())
        self.assertIn("--strip-components=3 ./srv-content/production-media/media", result.stdout)

    def test_the_root_follows_production_media_root_from_the_restored_env_file(self):
        result, _default_root = self.restore(self.archive(), "--apply", env_file="PRODUCTION_MEDIA_ROOT=/mnt/custom/pm\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        custom = self.tmp / "stage" / "mnt" / "custom" / "pm"
        self.assertEqual((custom / "media" / SHARD / KEY).read_bytes(), MEDIA_BYTES)
        self.assertFalse((self.tmp / "stage" / "srv" / "isadoraair" / "production-media" / "media" / SHARD).exists())

    def test_the_music_library_guard_still_protects_the_library(self):
        # production-media is NOT the music library; restoring it must never touch music/.
        _result, _root = self.restore(self.archive(), "--apply")
        music = self.tmp / "stage" / "srv" / "isadoraair" / "music"
        self.assertEqual(list(music.iterdir()), [])


class InspectBackupTests(TempDirCase):
    def inspect(self, with_media):
        workdir = self.tmp / "work"
        workdir.mkdir()
        (workdir / "MANIFEST.txt").write_text("IsadoraAir Git SHA: deadbeefcafebabe0000\n")
        (workdir / "database.dump").write_bytes(b"PGDMP" + b"\x00" * 100)
        app_dir = self.tmp / "app_build" / "isadoraair"
        app_dir.mkdir(parents=True)
        (app_dir / "manage.py").write_text("#!/usr/bin/env python\n")
        (app_dir / ".env").write_text("SECRET_KEY=test\n")
        with tarfile.open(workdir / "app.tar.gz", "w:gz") as tar:
            tar.add(app_dir, arcname="isadoraair")
        if with_media:
            store = workdir / "srv-content" / "production-media" / "media" / SHARD
            store.mkdir(parents=True)
            (store / KEY).write_bytes(MEDIA_BYTES)
        archive = self.tmp / "backup.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(workdir, arcname=".")
        return subprocess.run([str(INSPECT), str(archive)], capture_output=True, text=True, timeout=30)

    def test_the_inspector_reports_production_media_when_present(self):
        result = self.inspect(True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"iPortal production media\s+PASS\s+\d+ entries")
        self.assertNotRegex(result.stdout, r"iPortal production media\s+WARN")

    def test_its_absence_is_only_a_warning_for_stations_that_have_not_used_iportal(self):
        result = self.inspect(False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"iPortal production media\s+WARN")
