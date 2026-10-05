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
NESTED_FILE = "./srv-content/production-media/media/cd/cd" + "1" * 30
NESTED_MEDIA = {"./srv-content/production-media/media/cd": None, NESTED_FILE: b"NESTED"}


def _pm_archive(path: Path, *, symlinks: dict[str, str] | None = None,
                extra: list[tuple] | None = None, nested: bool = False) -> Path:
    """Mirror a real backup: deploy/stage_production_media.sh `cp -a`s the
    0750 store, so media/ directories are 0750 and media files 0440 (as in
    Phase A's own fixture); reports/ entries as in r0106's fixture.

    ``extra``: (name, kind, payload) members appended after the legitimate
    ones -- kind is file/dir/symlink/hardlink/chr/fifo (payload = bytes or a
    link target). ``nested``: add a second, deeper legitimate media path."""
    entries = dict(PM_ARCHIVE)
    if nested:
        entries.update(NESTED_MEDIA)
    with tarfile.open(path, "w:gz") as tf:
        for name, data in entries.items():
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
        for name, kind, payload in (extra or []):
            info = tarfile.TarInfo(name)
            info.mtime = int(time.time())
            if kind == "file":
                info.size = len(payload)
                tf.addfile(info, io.BytesIO(payload))
                continue
            info.type = {"dir": tarfile.DIRTYPE, "symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE,
                         "chr": tarfile.CHRTYPE, "fifo": tarfile.FIFOTYPE}[kind]
            if kind in ("symlink", "hardlink"):
                info.linkname = payload
            if kind == "chr":
                info.devmajor, info.devminor = 1, 3
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


# ---------------------------------------------------------------------------
# Hermetic stage-40 harness (Codex blocker 2 on 43c6a5f).
#
# Every configurable root stage 40 validates or writes is redirected beneath
# the test's temporary directory, and every mutating tool is confined: an
# unprivileged chown/chmod/mkdir/tar/install/cp/mv/ln/rm/touch naming an
# absolute path outside the test root is refused AND recorded, failing the
# test. (The earlier fixture left REPORTS_ROOT at its default, so stage 40's
# unprivileged report extraction reached the host's /var/lib/isadoraair/reports.)
# The only privileged attempts allowed outside the test root are stage 40's
# hard-coded, non-configurable live /srv/isadoraair/{carts,...} directories,
# which the sudo shim records and never executes.
# ---------------------------------------------------------------------------

CONFINE_TOOL = """#!/bin/bash
name="@NAME@"
for arg in "$@"; do
  case "$arg" in
    /*)
      case "$arg" in
        "$SHIM_ALLOW_PREFIX"|"$SHIM_ALLOW_PREFIX"/*) ;;
        *) printf 'OUTSIDE %s %s\\n' "$name" "$*" >> "$SHIM_LOG"
           echo "confine-shim: refusing $name outside the test root: $arg" >&2
           exit 97 ;;
      esac
      ;;
  esac
done
PATH="${PATH#*:}" exec "$name" "$@"
"""
CONFINED_TOOLS = ("chown", "chmod", "mkdir", "tar", "install", "cp", "mv", "ln", "rm", "touch")
FIXED_LIVE_SRV_DIRS = frozenset(
    f"/srv/isadoraair/{name}" for name in ("carts", "voicetracks", "waveforms", "aircheck", "rip_staging", "music")
)
LIVE_PREFIXES_FORBIDDEN = ("/var", "/srv", "/opt", "/etc", "/home", "/usr", "/root", "/mnt", "/media")


class _HermeticStage40Harness(_Stage40Harness):
    def setUp(self):
        super().setUp()
        self.station = self.tmp / "station"
        self.roots = {
            "LIBRARY_ROOT": self.station / "library",
            "WAVEFORMS_DIR": self.station / "waveforms",
            "REPORTS_ROOT": self.station / "reports",
            "WEATHER_DATA_DIR": self.station / "weather",
            "ENCODER_STATE_ROOT": self.station / "encoders",
            "PRODUCTION_MEDIA_ROOT": self.station / "production-media",
        }

    def write_env(self, **overrides):
        """Every managed root explicitly inside the test tree (overridable)."""
        values = {key: str(path) for key, path in self.roots.items()}
        values.update(overrides)
        self.env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")

    def _shims(self, sudo_script):
        shims = super()._shims(sudo_script)
        if sudo_script is not None:  # a "real tools" run: confine every mutating tool
            for name in CONFINED_TOOLS:
                path = shims / name
                path.write_text(CONFINE_TOOL.replace("@NAME@", name))
                path.chmod(0o755)
        return shims

    def assert_hermetic(self):
        log = self.log.read_text().splitlines()
        self.assertEqual([line for line in log if line.startswith("OUTSIDE ")], [],
                         "an unprivileged mutation targeted a path outside the test root")
        skipped = {line[len("SKIPPED "):] for line in log if line.startswith("SKIPPED ")}
        allowed = (str(self.tmp), str(HELPER))
        for line in log:
            if not line.startswith("sudo "):
                continue
            outside = [arg for arg in line.split()[1:]
                       if arg.startswith("/") and not arg.startswith(allowed)]
            if not outside:
                continue
            self.assertIn(line, skipped, f"privileged command outside the test root was EXECUTED: {line}")
            self.assertTrue(set(outside) <= FIXED_LIVE_SRV_DIRS,
                            f"privileged attempt outside the test root beyond stage 40's fixed /srv tree: {line}")
        for key, path in self.roots.items():
            self.assertTrue(str(path).startswith(str(self.tmp)), key)

    def station_fingerprint(self):
        return {str(p.relative_to(self.tmp)): (p.lstat().st_mode, p.lstat().st_ctime_ns, p.lstat().st_size)
                for p in sorted(self.station.rglob("*"))} if self.station.exists() else {}


class ProductionMediaStagedAliasEntryPointTests(_HermeticStage40Harness):
    """Real stage-40 runs, --staging-root --apply, real (confined) tools."""

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
        # Staged runs judge these LIVE values and write only beneath staging.
        self.env_file.write_text(PM_STAGED_ENV + f"ENCODER_STATE_ROOT={self.station}/encoders\n", encoding="utf-8")
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
        self.assert_hermetic()


class ProductionMediaLiveRestoreTests(_HermeticStage40Harness):
    """LIVE mode (explicit temporary --target-root, every managed root in the
    test tree, confined tools)."""

    def setUp(self):
        super().setUp()
        self.archive = _pm_archive(self.tmp / "backup.tar.gz")

    def test_explicit_empty_assignment_refuses_before_any_mutation(self):
        self.write_env(PRODUCTION_MEDIA_ROOT="")
        result = self._run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("PRODUCTION_MEDIA_ROOT", result.stderr)
        self.assertEqual(self.log.read_text(), "")

    def test_ancestor_swapped_after_validation_creates_nothing_in_the_victim(self):
        parent = self.station / "parent"
        parent.mkdir(parents=True)
        root = parent / "production-media"
        self.roots["PRODUCTION_MEDIA_ROOT"] = root
        victim = self.tmp / "victim"
        victim.mkdir()
        victim.chmod(0o700)
        before = (victim.stat().st_mode, victim.stat().st_uid, victim.stat().st_ctime_ns)
        self.write_env()
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
        self.assert_hermetic()

    def test_safe_live_restore_is_descriptor_established_and_member_scoped(self):
        root = self.roots["PRODUCTION_MEDIA_ROOT"]
        (root / "media").mkdir(parents=True)
        unrelated = root / "media" / "zz"
        unrelated.mkdir()
        (unrelated / "pre-existing").write_bytes(b"old")
        before = (unrelated / "pre-existing").stat().st_ctime_ns
        self.write_env()
        time.sleep(0.02)
        result = self._run(sudo_script=SELECTIVE_SUDO)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((root / "media" / "ab" / ("ab" + "0" * 30)).read_bytes(), b"BYTES")
        # Reports landed in the TEST reports root -- never the host default.
        self.assertTrue((self.roots["REPORTS_ROOT"] / "royalty-2026-09.csv").is_file())
        lines = [line for line in self.log.read_text().splitlines() if str(root) in line]
        for directory in (root, *(root / name for name in ("media", "incoming", "work", "locks"))):
            self.assertIn(f"sudo python3 -I {HELPER} establish --root {directory} --owner {OWNER} --mode 0750",
                          lines)
        self.assertTrue(any(f"chown-members --root {root}/media --owner {OWNER}" in line for line in lines), lines)
        self.assertFalse(any("chown -R" in line or " mkdir " in f" {line} " or "chmod" in line for line in lines),
                         lines)
        # Ownership touched only the restored members: unrelated media untouched.
        self.assertEqual((unrelated / "pre-existing").stat().st_ctime_ns, before)
        self.assert_hermetic()

    def test_safe_live_restore_with_nested_media_paths(self):
        self.archive = _pm_archive(self.tmp / "nested.tar.gz", nested=True)
        root = self.roots["PRODUCTION_MEDIA_ROOT"]
        self.write_env()
        result = self._run(sudo_script=SELECTIVE_SUDO)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((root / "media" / "ab" / ("ab" + "0" * 30)).read_bytes(), b"BYTES")
        self.assertEqual((root / "media" / "cd" / ("cd" + "1" * 30)).read_bytes(), b"NESTED")
        self.assert_hermetic()

    def test_plan_mode_previews_the_reconciled_steps(self):
        root = self.roots["PRODUCTION_MEDIA_ROOT"]
        self.write_env()
        result = self._run(mode="--plan", sudo_script=SELECTIVE_SUDO)  # plan still lists the archive with tar
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"establish --root {root}/media --owner {OWNER} --mode 0750", result.stdout)
        self.assertIn(f"exact restored members only) {OWNER} under {root}/media", result.stdout)
        self.assertFalse(root.exists())
        self.assertEqual([l for l in self.log.read_text().splitlines() if not l.startswith("SKIPPED")
                          and not l.startswith("sudo")], [])
        self.assert_hermetic()


# ---------------------------------------------------------------------------
# Codex blocker 1 on 43c6a5f: members reaching srv-content/production-media/
# outside media/ (or through a non-canonical spelling) were silently skipped
# and stage 40 succeeded. The ProductionMedia namespace is now strict.
# ---------------------------------------------------------------------------

VALID = b"x"
HOSTILE_MEMBERS = {
    "absolute": [("/srv-content/production-media/media/absolute", "file", VALID)],
    "absolute namespace sibling": [("/srv-content/production-media/sibling", "file", VALID)],
    "sibling outside prefix": [("./srv-content/production-media/sibling", "file", VALID)],
    "transient sibling tree": [("./srv-content/production-media/incoming/x.part", "file", VALID)],
    "traversal out of media": [("./srv-content/production-media/media/../../../etc/evil", "file", VALID)],
    "traversal to sibling": [("./srv-content/production-media/media/../sibling", "file", VALID)],
    "dot-dot back into media": [("./srv-content/production-media/../production-media/media/x", "file", VALID)],
    "double ./ spelling": [("././srv-content/production-media/media/x", "file", VALID)],
    "empty component spelling": [("srv-content//production-media/media/x", "file", VALID)],
    "dot component spelling": [("./srv-content/./production-media/media/x", "file", VALID)],
    "symlink in media": [("./srv-content/production-media/media/evil", "symlink", "/etc")],
    "symlinked sibling": [("./srv-content/production-media/work", "symlink", "/etc")],
    "hardlink in media": [("./srv-content/production-media/media/hl", "hardlink", PM_FILE)],
    "character device": [("./srv-content/production-media/media/dev", "chr", None)],
    "fifo": [("./srv-content/production-media/media/fifo", "fifo", None)],
}


class StrictNamespaceMemberTests(_TmpCase):
    """Helper level: the strict contract, and that it stays opt-in."""

    PREFIX = "srv-content/production-media/media"
    NAMESPACE = "srv-content/production-media"

    def test_every_hostile_member_fails_the_whole_archive(self):
        for label, extra in HOSTILE_MEMBERS.items():
            with self.subTest(label):
                archive = _pm_archive(self.tmp / "h.tar.gz", extra=extra)
                with self.assertRaises(crs.UnsafeRootError):
                    crs.validate_members(archive, self.PREFIX, strict_namespace=self.NAMESPACE)

    def test_other_domains_and_legitimate_nested_media_are_accepted(self):
        archive = _pm_archive(self.tmp / "ok.tar.gz", nested=True, extra=[
            ("./etc-live", "dir", None), ("./etc-live/nginx.conf", "file", VALID),
            ("./srv-content/carts", "dir", None), ("./srv-content/carts/a.mp3", "file", VALID),
            ("./MANIFEST.txt", "file", VALID),
        ])
        self.assertEqual(
            crs.validate_members(archive, self.PREFIX, strict_namespace=self.NAMESPACE),
            ["ab", "ab/ab" + "0" * 30, "cd", "cd/cd" + "1" * 30],
        )

    def test_namespace_must_contain_the_prefix(self):
        archive = _pm_archive(self.tmp / "ok.tar.gz")
        with self.assertRaises(crs.UnsafeRootError):
            crs.validate_members(archive, self.PREFIX, strict_namespace="srv-content/carts")

    def test_non_strict_callers_keep_their_contract(self):
        """REPORTS_ROOT's caller (no --strict-namespace) is unchanged: members of
        other domains, even oddly spelled ones, are not its business."""
        archive = _pm_archive(self.tmp / "h.tar.gz", extra=HOSTILE_MEMBERS["absolute"])
        self.assertEqual(crs.validate_members(archive, "reports"),
                         ["q3", "q3/soundexchange.csv", "royalty-2026-09.csv"])


class StrictNamespaceStage40Tests(_HermeticStage40Harness):
    """Real stage-40 entry point, LIVE mode: a valid ProductionMedia member plus
    ONE hostile member must fail the stage before any change at all."""

    def _prepare(self):
        root = self.roots["PRODUCTION_MEDIA_ROOT"]
        (root / "media" / "ab").mkdir(parents=True)
        (root / "media" / "ab" / "existing").write_bytes(b"kept")
        reports = self.roots["REPORTS_ROOT"]
        reports.mkdir(parents=True)
        (reports / "unrelated.csv").write_bytes(b"kept")
        self.write_env()
        return root

    def test_each_hostile_member_fails_stage_40_before_any_mutation(self):
        root = self._prepare()
        for label, extra in HOSTILE_MEMBERS.items():
            with self.subTest(label):
                self.archive = _pm_archive(self.tmp / "hostile.tar.gz", extra=extra)
                self.log.write_text("")
                before = self.station_fingerprint()
                time.sleep(0.01)
                result = self._run(sudo_script=SELECTIVE_SUDO)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("failed member validation", result.stderr)
                # Nothing at all: no extraction, no establishment, no ownership/mode change.
                self.assertEqual(self.station_fingerprint(), before)
                self.assertFalse((root / "media" / "ab" / ("ab" + "0" * 30)).exists())
                self.assertEqual(self.log.read_text(), "", "a command ran before the archive was refused")
                self.assertFalse(Path("/etc/evil").exists())

    def test_the_two_codex_probes_fail_through_the_cli(self):
        """Codex's exact probes, through stage 40 itself."""
        self._prepare()
        for member in ("/srv-content/production-media/media/absolute", "./srv-content/production-media/sibling"):
            with self.subTest(member=member):
                self.archive = _pm_archive(self.tmp / "probe.tar.gz", extra=[(member, "file", VALID)])
                self.log.write_text("")
                result = self._run(sudo_script=SELECTIVE_SUDO)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(repr(member), result.stderr)
                self.assertNotIn("restoring srv-content/production-media/media", result.stdout)
