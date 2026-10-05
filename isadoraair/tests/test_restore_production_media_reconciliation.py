"""2.22A on r0106 -- ProductionMedia restore integration under r0106 hardening.

Phase A (accepted on r0105) restored PRODUCTION_MEDIA_ROOT with path-based
`ensure_dir` + `chmod` and a recursive `chown -R -P` of media/, and judged a
staged root only by production/root_policy.py's dedicated-content rule. r0106
then established, for every restore-written station-content root, that:

  * the restore-time value is read with python-decouple semantics and an
    explicitly empty assignment fails closed;
  * under --staging-root the RESOLVED staged root must stay separate from the
    resolved staged equivalent of every other managed root;
  * directories are established through no-follow descriptors (an ancestor
    swapped for a symlink after validation creates nothing);
  * archive members are validated before extraction and ownership is
    member-scoped, never recursive.

These tests prove the reconciled ProductionMedia restore path honours all of
that, through the real 40-station-content.sh entry point, while
production/root_policy.py's own accepted semantics stay intact (its tests
live in production/tests/test_root_policy.py).
"""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
import time
from pathlib import Path

from django.test import SimpleTestCase

from isadoraair.tests.test_restore_content_root_safety import (
    HELPER,
    OWNER,
    REPO_ROOT,
    REPORTS_ARCHIVE,
    SELECTIVE_SUDO,
    STAGED_ENV,
    SWAP_SUDO,
    _archive,
    _fingerprint,
    _Stage40Harness,
    _TmpCase,
    crs,
)

POLICY = REPO_ROOT / "production" / "root_policy.py"
PM_FILE = "./srv-content/production-media/media/ab/ab" + "0" * 30
PM_ARCHIVE = {
    **REPORTS_ARCHIVE,
    "./srv-content": None,
    "./srv-content/production-media": None,
    "./srv-content/production-media/media": None,
    "./srv-content/production-media/media/ab": None,
    PM_FILE: b"BYTES",
}
PM_STAGED_ENV = STAGED_ENV + "PRODUCTION_MEDIA_ROOT=/mnt/stationdata/production-media\n"


def _pm_archive(path: Path, *, symlinks: dict[str, str] | None = None) -> Path:
    """Mirror a real backup: deploy/stage_production_media.sh `cp -a`s the
    0750 store, so media/ directories are 0750 and media files 0440 (as in
    Phase A's own fixture); reports/ entries as in r0106's fixture."""
    with tarfile.open(path, "w:gz") as tf:
        for name, data in PM_ARCHIVE.items():
            info = tarfile.TarInfo(name)
            info.mtime = int(time.time())
            production_media = name.startswith("./srv-content")
            if data is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o750 if production_media else 0o755
                tf.addfile(info)
            else:
                info.size = len(data)
                info.mode = 0o440 if production_media else 0o644
                tf.addfile(info, io.BytesIO(data))
        for name, target in (symlinks or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tf.addfile(info)
    return path


def _policy_accepts(root: str, env_file: Path) -> bool:
    result = subprocess.run(
        ["python3", str(POLICY), "check", "--root", root, "--app-root", str(REPO_ROOT)],
        capture_output=True, text=True, timeout=30,
    )
    return result.returncode == 0


class ProductionMediaRootSafetyTests(_TmpCase):
    """Root-safety cross-check (helper level): both layers agree."""

    def _check(self, env_text: str, staging: Path | None = None) -> str:
        self.env_file.write_text(env_text, encoding="utf-8")
        return crs.check_root(
            key="PRODUCTION_MEDIA_ROOT", default=crs.MANAGED_ROOT_DEFAULTS["PRODUCTION_MEDIA_ROOT"],
            env_file=self.env_file, target_root=str(self.app), tooling_root=str(REPO_ROOT),
            staging_root=str(staging) if staging else None, home=os.environ.get("HOME"),
        )

    def test_safe_and_dedicated_alternate_volume_roots_are_accepted_by_both_layers(self):
        for value in ("/srv/isadoraair/production-media",
                      "/mnt/station-data/isadoraair-production-media",
                      "/media/disk2/isadoraair-production-media"):
            with self.subTest(value=value):
                self.assertEqual(self._check(f"PRODUCTION_MEDIA_ROOT={value}\n"), value)
                self.assertTrue(_policy_accepts(value, self.env_file))
        self.assertEqual(self._check(""), "/srv/isadoraair/production-media")

    def test_dangerous_roots_are_refused_by_both_layers(self):
        for value in ("/", "/etc", "/srv", "/srv/isadoraair", "/var", "/mnt", "/media",
                      str(REPO_ROOT), f"{REPO_ROOT}/production-media"):
            with self.subTest(value=value):
                with self.assertRaises(crs.UnsafeRootError):
                    self._check(f"PRODUCTION_MEDIA_ROOT={value}\n")
                self.assertFalse(_policy_accepts(value, self.env_file))

    def test_production_media_cannot_alias_reports_or_weather(self):
        cases = {
            "reports default": "PRODUCTION_MEDIA_ROOT=/var/lib/isadoraair/reports\n",
            "inside reports": "PRODUCTION_MEDIA_ROOT=/var/lib/isadoraair/reports/pm\n",
            "weather default": "PRODUCTION_MEDIA_ROOT=/var/lib/isadoraair/weather\n",
            "configured reports": "REPORTS_ROOT=/mnt/sd/reports\nPRODUCTION_MEDIA_ROOT=/mnt/sd/reports\n",
            "ancestor of configured weather": "WEATHER_DATA_DIR=/mnt/sd/pm/weather\nPRODUCTION_MEDIA_ROOT=/mnt/sd/pm\n",
        }
        for label, env_text in cases.items():
            with self.subTest(label):
                with self.assertRaises(crs.UnsafeRootError):
                    self._check(env_text)

    def test_reports_and_weather_cannot_alias_production_media_either(self):
        for key in ("REPORTS_ROOT", "WEATHER_DATA_DIR"):
            for env_text in (f"{key}=/srv/isadoraair/production-media\n",
                             f"{key}=/srv/isadoraair/production-media/media\n",
                             f"PRODUCTION_MEDIA_ROOT=/mnt/sd/pm\n{key}=/mnt/sd/pm/x\n"):
                with self.subTest(key=key, env=env_text):
                    self.env_file.write_text(env_text, encoding="utf-8")
                    with self.assertRaises(crs.UnsafeRootError):
                        crs.check_root(key=key, default=crs.MANAGED_ROOT_DEFAULTS[key], env_file=self.env_file,
                                       target_root=str(self.app), tooling_root=str(REPO_ROOT),
                                       staging_root=None, home=os.environ.get("HOME"))

    def test_explicit_empty_assignment_fails_closed(self):
        with self.assertRaises(crs.UnsafeRootError):
            self._check("PRODUCTION_MEDIA_ROOT=\n")

    def test_staged_production_media_symlink_onto_another_managed_root_is_refused(self):
        staging = self.tmp / "staging"
        base = staging / "mnt" / "stationdata"
        (base / "reports").mkdir(parents=True)
        (base / "production-media").symlink_to(base / "reports")
        with self.assertRaises(crs.UnsafeRootError):
            self._check(PM_STAGED_ENV, staging=staging)

    def test_nested_member_prefix_is_supported_and_validated(self):
        archive = _archive(self.tmp / "pm.tar.gz", PM_ARCHIVE)
        self.assertEqual(crs.validate_members(archive, "srv-content/production-media/media"),
                         ["ab", "ab/ab" + "0" * 30])
        for prefix in ("", "/srv-content", "srv-content//media", "srv-content/../etc", "a/./b"):
            with self.subTest(prefix=prefix):
                with self.assertRaises(crs.UnsafeRootError):
                    crs.validate_members(archive, prefix)


class ProductionMediaStagedAliasEntryPointTests(_Stage40Harness):
    """Real stage-40 runs, --staging-root --apply, real tools (only sudo shimmed)."""

    def setUp(self):
        super().setUp()
        self.archive = _pm_archive(self.tmp / "backup.tar.gz")

    def _tree(self, name: str) -> tuple[Path, Path]:
        staging = self.tmp / name
        base = staging / "mnt" / "stationdata"
        library = base / "library"
        library.mkdir(parents=True)
        (library / "track.flac").write_bytes(b"audio")
        library.chmod(0o750)
        return staging, base

    def _run_staged(self, staging: Path):
        self.env_file.write_text(PM_STAGED_ENV, encoding="utf-8")
        return self._run(staging=staging, sudo_script=SELECTIVE_SUDO)

    def test_staged_production_media_aliases_onto_managed_roots_are_refused(self):
        for name, target in (("reports", "reports"), ("weather", "weather"), ("library", "library"),
                             ("carts", None)):
            with self.subTest(target=name):
                staging, base = self._tree(f"s-pm-{name}")
                for real in ("reports", "weather"):
                    (base / real).mkdir(exist_ok=True)
                victim = (staging / "srv" / "isadoraair" / "carts") if target is None else base / target
                victim.mkdir(parents=True, exist_ok=True)
                (base / "production-media").symlink_to(victim)
                before = _fingerprint(victim)
                self.log.write_text("")
                time.sleep(0.02)
                result = self._run_staged(staging)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("PRODUCTION_MEDIA_ROOT", result.stderr)
                self.assertEqual(_fingerprint(victim), before)
                for managed in ("media", "incoming", "work", "locks"):
                    self.assertFalse((victim / managed).exists(), managed)
                self.assertEqual(self.log.read_text(), "")

    def test_safe_staged_distinct_roots_restore_production_media_and_reports(self):
        staging, base = self._tree("s-pm-safe")
        for real in ("reports", "weather"):
            (base / real).mkdir()
        before = _fingerprint(base / "library")
        result = self._run_staged(staging)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        pm = base / "production-media"
        self.assertEqual((pm / "media" / "ab" / ("ab" + "0" * 30)).read_bytes(), b"BYTES")
        for path in (pm, pm / "media", pm / "incoming", pm / "work", pm / "locks"):
            self.assertEqual(path.stat().st_mode & 0o7777, 0o750, path)
        self.assertTrue((base / "reports" / "royalty-2026-09.csv").is_file())
        self.assertEqual(_fingerprint(base / "library"), before)


class ProductionMediaLiveRestoreTests(_Stage40Harness):
    """LIVE mode (explicit temporary --target-root, PATH shims)."""

    def setUp(self):
        super().setUp()
        self.archive = _pm_archive(self.tmp / "backup.tar.gz")

    def test_explicit_empty_assignment_refuses_before_any_mutation(self):
        self.env_file.write_text("PRODUCTION_MEDIA_ROOT=\n", encoding="utf-8")
        result = self._run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("PRODUCTION_MEDIA_ROOT", result.stderr)
        self.assertEqual(self.log.read_text(), "")

    def test_ancestor_swapped_after_validation_creates_nothing_in_the_victim(self):
        parent = self.tmp / "stationdata" / "parent"
        parent.mkdir(parents=True)
        root = parent / "production-media"
        victim = self.tmp / "victim"
        victim.mkdir()
        victim.chmod(0o700)
        before = (victim.stat().st_mode, victim.stat().st_uid, victim.stat().st_ctime_ns)
        self.env_file.write_text(f"PRODUCTION_MEDIA_ROOT={root}\n", encoding="utf-8")
        marker = self.tmp / "swap.done"
        time.sleep(0.02)
        result = self._run(sudo_script=SWAP_SUDO, extra_env={
            "SWAP_TRIGGER": str(root), "SWAP_PARENT": str(parent),
            "SWAP_VICTIM": str(victim), "SWAP_MARKER": str(marker),
        })
        self.assertIn("accepted by production/root_policy.py", result.stdout)  # validation DID pass
        self.assertTrue(marker.exists(), "the adversarial swap was never exercised")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(list(victim.iterdir()), [])
        self.assertEqual((victim.stat().st_mode, victim.stat().st_uid, victim.stat().st_ctime_ns), before)
        self.assertFalse(any(self.tmp.rglob("ab" + "0" * 30)), "media was extracted")
        self.assertNotIn("chown-members", self.log.read_text())

    def test_hostile_media_member_is_refused_before_extraction(self):
        root = self.tmp / "stationdisk" / "production-media"
        self.archive = _pm_archive(self.tmp / "evil.tar.gz",
                                   symlinks={"./srv-content/production-media/media/evil": "/etc"})
        self.env_file.write_text(f"PRODUCTION_MEDIA_ROOT={root}\n", encoding="utf-8")
        result = self._run(sudo_script=SELECTIVE_SUDO)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("not a regular file or directory", result.stderr)
        self.assertFalse((root / "media" / "evil").is_symlink() or (root / "media" / "evil").exists())
        self.assertFalse((root / "media" / "ab").exists())
        self.assertNotIn("chown-members", self.log.read_text())

    def test_safe_live_restore_is_descriptor_established_and_member_scoped(self):
        root = self.tmp / "stationdisk" / "production-media"
        (root / "media").mkdir(parents=True)
        unrelated = root / "media" / "zz"
        unrelated.mkdir()
        (unrelated / "pre-existing").write_bytes(b"old")
        before = (unrelated / "pre-existing").stat().st_ctime_ns
        self.env_file.write_text(f"PRODUCTION_MEDIA_ROOT={root}\n", encoding="utf-8")
        time.sleep(0.02)
        result = self._run(sudo_script=SELECTIVE_SUDO)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((root / "media" / "ab" / ("ab" + "0" * 30)).read_bytes(), b"BYTES")
        lines = [line for line in self.log.read_text().splitlines() if str(root) in line]
        for directory in (root, *(root / name for name in ("media", "incoming", "work", "locks"))):
            self.assertIn(f"sudo python3 -I {HELPER} establish --root {directory} --owner {OWNER} --mode 0750",
                          lines)
        self.assertTrue(any(f"chown-members --root {root}/media --owner {OWNER}" in line for line in lines), lines)
        self.assertFalse(any("chown -R" in line or " mkdir " in f" {line} " or "chmod" in line for line in lines),
                         lines)
        # Ownership touched only the restored members: unrelated media untouched.
        self.assertEqual((unrelated / "pre-existing").stat().st_ctime_ns, before)

    def test_plan_mode_previews_the_reconciled_steps(self):
        root = self.tmp / "stationdisk" / "production-media"
        self.env_file.write_text(f"PRODUCTION_MEDIA_ROOT={root}\n", encoding="utf-8")
        result = self._run(mode="--plan", sudo_script=SELECTIVE_SUDO)  # plan still lists the archive with tar
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"establish --root {root}/media --owner {OWNER} --mode 0750", result.stdout)
        self.assertIn("validate archive srv-content/production-media/media/ members", result.stdout)
        self.assertIn(f"exact restored members only) {OWNER} under {root}/media", result.stdout)
        self.assertFalse(root.exists())
        self.assertEqual(self.log.read_text(), "")
