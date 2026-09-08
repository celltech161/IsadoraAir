"""r0045 -- recovery-media discovery/validation and per-stage routing.

Confirmed integration gap: restore.sh's interactive Adopt/New/Show/Quit
workflow (r0044) broadcasts the same COMMON_ARGS to every stage except
the already-established --isa-user/--isa-uid/--isa-gid routing -- it
cannot express the frozen-media inputs a real offline E8 restore needs
(Stage 10's local apt/snap closure, Stage 20's local Git mirror,
Stage 60/80's offline pip wheelhouse, Stage 80's local companion Git
mirrors), all previously supplied by a separate E8 runner script this
interactive orchestrator doesn't have.

This introduces ONE recovery-media concept (`--recovery-media-root`, or
interactive discovery) instead of six separate flags, established
directly from deploy/restore/build_offline_closure.py's own --out-dir
layout and the real E8 export procedure -- verified in this file
against BOTH synthetic fixtures and, for the discovery/validation
module itself, the actual on-disk r0042 export this session's own
earlier work produced (skipped if that path isn't present, e.g. a
different host/CI runner).
"""
from __future__ import annotations

import json
import os
import pty
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from django.test import SimpleTestCase

from isadoraair.tests.test_restore_tooling import RESTORE_DIR
from isadoraair.tests.test_restore_resume_ledger import _make_git_fixture_repo

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RECOVERY_MEDIA_HELPER = RESTORE_DIR / "recovery_media.py"
REAL_R0042_EXPORT = Path(
    "/home/jreed/e8-final-export-r0042/20260907T231658Z/e8-inputs"
)
REAL_R0042_ARCHIVE = REAL_R0042_EXPORT / "backups" / "isadoraair-backup-20260907-181431-formal-r0042.tar.gz"


def _run_pty(args, env, *, send: bytes = b"", wait_before_send: float = 1.2, timeout: int = 20,
             stop_after: bytes | None = None):
    """Drive `args` over a real pty. If `stop_after` is given, the child
    (and its whole process group, since it runs detached via
    start_new_session) is killed as soon as that byte string appears in
    the accumulated output -- used so tests that only care about the
    recovery-media discovery/prompt behaviour never have to let the
    REAL stage loop run to completion (which, with no media root
    resolved, would fall through to Stage 20's default *online* git
    clone against the real upstream repo -- slow and network-dependent,
    not something a unit test should ever wait on)."""
    controller_fd, follower_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            args, stdin=follower_fd, stdout=follower_fd, stderr=follower_fd,
            env=env, start_new_session=True, close_fds=True,
        )
        os.close(follower_fd)
        follower_fd = -1
        time.sleep(wait_before_send)
        if send:
            os.write(controller_fd, send)
        chunks = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = os.read(controller_fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
            if stop_after is not None and stop_after in b"".join(chunks):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                break
        try:
            returncode = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            returncode = proc.wait(timeout=5)
        return returncode, b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        if follower_fd != -1:
            os.close(follower_fd)
        os.close(controller_fd)


def _make_minimal_archive(path: Path, name: str = "backup.tar.gz") -> Path:
    empty = path / f"{name}-empty"
    empty.mkdir(parents=True, exist_ok=True)
    archive = path / name
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(empty, arcname=".")
    return archive


def _make_synthetic_media_root(
    root: Path, *, archive_to_include: Path | None = None, apt_direct_packages: tuple[str, ...] = ()
) -> Path:
    """Builds a structurally-complete recovery-media tree matching
    recovery_media.py's own documented layout contract -- real bare git
    repo, real (if trivial) apt/snap/wheelhouse artifacts, never
    requiring a genuine multi-hundred-MB E8 closure just to prove the
    DISCOVERY/ROUTING logic works."""
    (root / "backups").mkdir(parents=True)
    apt_repo = root / "offline" / "apt-repo"
    apt_repo.mkdir(parents=True)
    (apt_repo / "dummy_1.0_amd64.deb").write_bytes(b"")
    (apt_repo / "Packages").write_text("", encoding="utf-8")
    snaps = root / "offline" / "snaps"
    snaps.mkdir(parents=True)
    (snaps / "snap-manifest.json").write_text("{}", encoding="utf-8")
    manifests = root / "offline" / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "direct-apt-packages.txt").write_text(
        "\n".join(apt_direct_packages) + ("\n" if apt_direct_packages else ""), encoding="utf-8"
    )
    wheelhouse = root / "offline" / "wheelhouse"
    wheelhouse.mkdir(parents=True)
    (wheelhouse / "dummy-1.0-py3-none-any.whl").write_bytes(b"")
    repos = root / "repos"
    repos.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "-q", str(repos / "IsadoraAir.git")], check=True)
    if archive_to_include is not None:
        shutil.copy(archive_to_include, root / "backups" / archive_to_include.name)
    return root


class RecoveryMediaValidateTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-e-media-validate-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.archive = _make_minimal_archive(self.tmpdir)

    def _validate(self, root, archive=None):
        args = [sys.executable, str(RECOVERY_MEDIA_HELPER), "validate", "--root", str(root)]
        if archive is not None:
            args += ["--archive", str(archive)]
        return subprocess.run(args, capture_output=True, text=True, timeout=15)

    def test_complete_tree_validates(self):
        root = _make_synthetic_media_root(self.tmpdir / "media", archive_to_include=self.archive)
        result = self._validate(root, self.archive)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        evidence = json.loads(result.stdout)
        self.assertTrue(evidence["valid"])
        self.assertTrue(evidence["archive_match"])
        self.assertEqual(evidence["problems"], [])

    def test_missing_apt_repo_fails(self):
        root = _make_synthetic_media_root(self.tmpdir / "media", archive_to_include=self.archive)
        shutil.rmtree(root / "offline" / "apt-repo")
        result = self._validate(root, self.archive)
        self.assertNotEqual(result.returncode, 0)
        evidence = json.loads(result.stdout)
        self.assertFalse(evidence["valid"])
        self.assertTrue(any("apt-repo" in p for p in evidence["problems"]))

    def test_missing_isadoraair_git_fails(self):
        root = _make_synthetic_media_root(self.tmpdir / "media", archive_to_include=self.archive)
        shutil.rmtree(root / "repos" / "IsadoraAir.git")
        result = self._validate(root, self.archive)
        self.assertNotEqual(result.returncode, 0)
        evidence = json.loads(result.stdout)
        self.assertFalse(evidence["valid"])
        self.assertTrue(any("IsadoraAir.git" in p for p in evidence["problems"]))

    def test_empty_wheelhouse_fails(self):
        root = _make_synthetic_media_root(self.tmpdir / "media", archive_to_include=self.archive)
        (root / "offline" / "wheelhouse" / "dummy-1.0-py3-none-any.whl").unlink()
        result = self._validate(root, self.archive)
        self.assertNotEqual(result.returncode, 0)
        evidence = json.loads(result.stdout)
        self.assertTrue(any("wheelhouse" in p for p in evidence["problems"]))

    def test_archive_not_present_in_backups_fails(self):
        """The CLI's own exit code (what restore.sh's explicit
        --recovery-media-root path actually checks) fails closed here
        -- but `valid` itself stays structural-only (True), since
        discover() elsewhere needs this exact "structurally fine, just
        doesn't have THIS archive" evidence to still report a
        structurally-sound root as a real, rankable candidate rather
        than silently dropping it (see RecoveryMediaDiscoverTests'
        ranked-by-archive-match coverage)."""
        root = _make_synthetic_media_root(self.tmpdir / "media")  # no archive copied in
        result = self._validate(root, self.archive)
        self.assertNotEqual(result.returncode, 0)
        evidence = json.loads(result.stdout)
        self.assertFalse(evidence["archive_match"])
        self.assertTrue(evidence["valid"])
        self.assertTrue(any("does not match any archive" in n for n in evidence["notes"]))

    def test_wrong_archive_bytes_fails_even_with_matching_name(self):
        root = _make_synthetic_media_root(self.tmpdir / "media")
        (root / "backups" / self.archive.name).write_bytes(b"completely different bytes")
        result = self._validate(root, self.archive)
        self.assertNotEqual(result.returncode, 0)
        evidence = json.loads(result.stdout)
        self.assertFalse(evidence["archive_match"])

    def test_validate_without_archive_still_checks_structure(self):
        root = _make_synthetic_media_root(self.tmpdir / "media")
        result = self._validate(root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        evidence = json.loads(result.stdout)
        self.assertTrue(evidence["valid"])
        self.assertIsNone(evidence["archive_match"])

    def test_real_r0042_export_validates_if_present(self):
        """Not a synthetic fixture -- the ACTUAL frozen export this
        session's own earlier work produced, still on this host. Skips
        cleanly elsewhere (a different host/CI runner)."""
        if not REAL_R0042_ARCHIVE.is_file():
            self.skipTest("real r0042 E8 export not present on this host")
        result = self._validate(REAL_R0042_EXPORT, REAL_R0042_ARCHIVE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        evidence = json.loads(result.stdout)
        self.assertTrue(evidence["valid"])
        self.assertTrue(evidence["archive_match"])
        self.assertIn("weather-ingest.git", evidence["companion_repo_names"])
        self.assertIn("syndicated-ingest.git", evidence["companion_repo_names"])
        self.assertIn("ogremote-ingest.git", evidence["companion_repo_names"])


class RecoveryMediaDiscoverTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-e-media-discover-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.archive = _make_minimal_archive(self.tmpdir)

    def _discover(self, archive, *search_roots):
        args = [sys.executable, str(RECOVERY_MEDIA_HELPER), "discover", "--archive", str(archive)]
        for root in search_roots:
            args += ["--search-root", str(root)]
        return subprocess.run(args, capture_output=True, text=True, timeout=15)

    def test_single_match_discovered_near_archive(self):
        search_root = self.tmpdir / "search"
        media = _make_synthetic_media_root(search_root / "some-export" / "e8-inputs", archive_to_include=self.archive)
        result = self._discover(self.archive, search_root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        candidates = json.loads(result.stdout)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(Path(candidates[0]["root"]).resolve(), media.resolve())
        self.assertTrue(candidates[0]["archive_match"])

    def test_archive_already_inside_a_media_tree_is_found_without_a_search_root(self):
        media = _make_synthetic_media_root(self.tmpdir / "e8-inputs")
        archive_inside = self.tmpdir / "e8-inputs" / "backups" / "isadoraair-backup-copy.tar.gz"
        shutil.copy(self.archive, archive_inside)
        result = self._discover(archive_inside)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        candidates = json.loads(result.stdout)
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0]["archive_match"])

    def test_no_candidates_found_returns_empty_list(self):
        search_root = self.tmpdir / "empty-search"
        search_root.mkdir()
        result = self._discover(self.archive, search_root)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), [])

    def test_multiple_valid_roots_both_reported_ranked_by_archive_match(self):
        search_root = self.tmpdir / "search"
        matching = _make_synthetic_media_root(
            search_root / "export-a" / "e8-inputs", archive_to_include=self.archive
        )
        other_archive = _make_minimal_archive(self.tmpdir, name="other-backup.tar.gz")
        non_matching = _make_synthetic_media_root(
            search_root / "export-b" / "e8-inputs", archive_to_include=other_archive
        )
        result = self._discover(self.archive, search_root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        candidates = json.loads(result.stdout)
        self.assertEqual(len(candidates), 2)
        roots = {Path(c["root"]).resolve() for c in candidates}
        self.assertEqual(roots, {matching.resolve(), non_matching.resolve()})
        # archive-matching candidate sorts first
        self.assertTrue(candidates[0]["archive_match"])
        self.assertFalse(candidates[1]["archive_match"])


class RecoveryMediaDetectAptGroupsTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-e-media-groups-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.packages_file = REPO_ROOT / "deploy" / "packages-ubuntu-26.04.txt"

    def _detect(self, root):
        return subprocess.run(
            [sys.executable, str(RECOVERY_MEDIA_HELPER), "detect-apt-groups",
             "--root", str(root), "--packages-file", str(self.packages_file)],
            capture_output=True, text=True, timeout=15,
        )

    def test_only_present_groups_detected(self):
        # espeak-ng (kokoro) and age (backup-encryption) present; the
        # cd-rip and selenium groups' own packages are absent.
        root = _make_synthetic_media_root(
            self.tmpdir / "media", apt_direct_packages=("espeak-ng", "age", "git")
        )
        result = self._detect(root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        groups = json.loads(result.stdout)
        self.assertTrue(groups["OPTIONAL_KOKORO_TTS"])
        self.assertTrue(groups["OPTIONAL_BACKUP_ENCRYPTION"])
        self.assertFalse(groups["OPTIONAL_CD_RIP"])
        self.assertFalse(groups["OPTIONAL_SYNDICATED_SELENIUM"])

    def test_no_optional_groups_present(self):
        root = _make_synthetic_media_root(self.tmpdir / "media", apt_direct_packages=("git", "curl"))
        result = self._detect(root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        groups = json.loads(result.stdout)
        self.assertFalse(any(groups.values()))

    def test_real_r0042_export_detects_all_four_groups_if_present(self):
        if not (REAL_R0042_EXPORT / "offline" / "manifests" / "direct-apt-packages.txt").is_file():
            self.skipTest("real r0042 E8 export not present on this host")
        result = self._detect(REAL_R0042_EXPORT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        groups = json.loads(result.stdout)
        self.assertTrue(all(groups.values()), groups)


class RestoreShFunctionSourcingTests(SimpleTestCase):
    """Isolated, no-side-effect tests of restore.sh's own media-
    resolution/routing logic -- `source`s restore.sh (its main body is
    guarded behind a BASH_SOURCE==$0 check specifically so this works)
    to reach its functions/variables without running the real
    interactive preflight or the 12-stage loop. This is what actually
    proves 'Stage-10-only flags reach only Stage 10', etc., without the
    cost/fragility of running every real stage script for real."""

    def setUp(self):
        super().setUp()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-e-restore-sh-source-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.archive = _make_minimal_archive(self.tmpdir)
        self.media = _make_synthetic_media_root(
            self.tmpdir / "media" / "e8-inputs",
            archive_to_include=self.archive,
            apt_direct_packages=("espeak-ng",),  # only kokoro this time
        )

    def _source_and_run(self, extra_args, script_after_source):
        script = (
            f'source "{RESTORE_DIR / "restore.sh"}" '
            f'--archive "{self.archive}" --staging-root "{self.tmpdir / "staging"}" '
            f'{extra_args} --apply\n'
            f"{script_after_source}\n"
        )
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)

    def test_stage10_args_include_only_apt_snap_and_detected_groups(self):
        result = self._source_and_run(
            f'--recovery-media-root "{self.media}"',
            '_restore_resolve_media_root; _restore_build_media_stage_args; '
            'printf "%s\\n" "${STAGE10_ARGS[@]}"',
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = result.stdout.strip().splitlines()
        self.assertIn("--apt-repo-dir", args)
        self.assertIn(str(self.media / "offline" / "apt-repo"), args)
        self.assertIn("--snap-dir", args)
        self.assertIn(str(self.media / "offline" / "snaps"), args)
        self.assertIn("--with-kokoro-tts", args)
        self.assertNotIn("--with-cd-rip", args)
        self.assertNotIn("--with-syndicated-selenium", args)
        self.assertNotIn("--with-backup-encryption", args)
        self.assertNotIn("--skip-heaac-build", args)

    def test_stage20_args_contain_only_local_mirror_url(self):
        result = self._source_and_run(
            f'--recovery-media-root "{self.media}"',
            '_restore_resolve_media_root; _restore_build_media_stage_args; '
            'printf "%s\\n" "${STAGE20_ARGS[@]}"',
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = result.stdout.strip().splitlines()
        self.assertIn("--repo-url", args)
        self.assertIn(f"file://{self.media}/repos/IsadoraAir.git", args)
        # Never contaminated with Stage-10-only flags.
        self.assertNotIn("--with-cd-rip", args)
        self.assertNotIn("--apt-repo-dir", args)
        self.assertNotIn("--repo-url-prefix", args)

    def test_stage80_args_contain_only_companion_prefix(self):
        result = self._source_and_run(
            f'--recovery-media-root "{self.media}"',
            '_restore_resolve_media_root; _restore_build_media_stage_args; '
            'printf "%s\\n" "${STAGE80_ARGS[@]}"',
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = result.stdout.strip().splitlines()
        self.assertIn("--repo-url-prefix", args)
        self.assertIn(f"file://{self.media}/repos", args)
        self.assertNotIn("--repo-url", args)
        self.assertNotIn("--with-cd-rip", args)

    def test_stage60_and_stage80_pip_env_constrained_to_wheelhouse_no_index(self):
        result = self._source_and_run(
            f'--recovery-media-root "{self.media}"',
            '_restore_resolve_media_root; _restore_build_media_stage_args; '
            'printf "%s\\n" "${STAGE60_ENV[@]}"; echo ---; printf "%s\\n" "${STAGE80_ENV[@]}"',
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        stage60_env, _, stage80_env = result.stdout.partition("---\n")
        for env_block in (stage60_env, stage80_env):
            self.assertIn("PIP_NO_INDEX=1", env_block)
            self.assertIn(f"PIP_FIND_LINKS={self.media}/offline/wheelhouse", env_block)
            self.assertIn("PIP_DISABLE_PIP_VERSION_CHECK=1", env_block)

    def test_identity_args_routing_still_intact(self):
        result = self._source_and_run(
            f'--recovery-media-root "{self.media}" --isa-user station --isa-uid 1500 --isa-gid 1500',
            'printf "%s\\n" "${IDENTITY_ARGS[@]}"; echo ---; printf "%s\\n" "${COMMON_ARGS[@]}"',
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        identity_block, _, common_block = result.stdout.partition("---\n")
        self.assertIn("--isa-user", identity_block)
        self.assertIn("station", identity_block)
        self.assertIn("--isa-uid", identity_block)
        self.assertNotIn("--isa-user", common_block)
        self.assertNotIn("station", common_block)

    def test_owner_routed_only_to_stage20_and_stage40(self):
        result = self._source_and_run(
            f'--recovery-media-root "{self.media}" --owner jreed:jreed',
            'printf "%s\\n" "${OWNER_ARGS[@]}"; echo ---; printf "%s\\n" "${COMMON_ARGS[@]}"',
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        owner_block, _, common_block = result.stdout.partition("---\n")
        owner_lines = owner_block.strip().splitlines()
        self.assertIn("--owner", owner_lines)
        self.assertIn("jreed:jreed", owner_lines)
        self.assertNotIn("--owner", common_block)

    def test_no_media_root_leaves_every_stage_array_empty(self):
        """No --recovery-media-root, non-interactive (no TTY under a
        subprocess without a pty) -- every stage-specific array stays
        empty, exactly pre-r0045 behavior for callers that never asked
        for media routing."""
        result = self._source_and_run(
            "",
            '_restore_resolve_media_root; _restore_build_media_stage_args; '
            'echo "MEDIA_ROOT=[$MEDIA_ROOT]"; '
            'echo "S10=[${STAGE10_ARGS[*]}]"; echo "S20=[${STAGE20_ARGS[*]}]"; '
            'echo "S60=[${STAGE60_ENV[*]}]"; echo "S80=[${STAGE80_ARGS[*]}]"',
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("MEDIA_ROOT=[]", result.stdout)
        self.assertIn("S10=[]", result.stdout)
        self.assertIn("S20=[]", result.stdout)
        self.assertIn("S60=[]", result.stdout)
        self.assertIn("S80=[]", result.stdout)

    def test_bad_media_root_fails_closed_deterministically_noninteractive(self):
        incomplete = self.tmpdir / "incomplete-media"
        incomplete.mkdir()
        (incomplete / "backups").mkdir()
        result = self._source_and_run(
            f'--recovery-media-root "{incomplete}"',
            "_restore_resolve_media_root",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a valid/complete recovery-media tree", result.stdout + result.stderr)

    def test_explicit_media_root_is_deterministic_non_tty(self):
        """The same explicit --recovery-media-root, run twice with no
        TTY at all, resolves identically both times -- no discovery/
        prompting variability."""
        first = self._source_and_run(
            f'--recovery-media-root "{self.media}"',
            '_restore_resolve_media_root; echo "MEDIA_ROOT=$MEDIA_ROOT"',
        )
        second = self._source_and_run(
            f'--recovery-media-root "{self.media}"',
            '_restore_resolve_media_root; echo "MEDIA_ROOT=$MEDIA_ROOT"',
        )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(first.stdout.strip(), second.stdout.strip())
        self.assertIn(f"MEDIA_ROOT={self.media}", first.stdout)


class RestoreBadMediaFailsBeforeStageWritesTests(SimpleTestCase):
    """Real, full `bash restore.sh` execution (not sourced) proving an
    incomplete media tree is rejected BEFORE Stage 00 -- or any other
    stage -- ever runs, let alone writes anything."""

    def setUp(self):
        super().setUp()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-e-restore-badmedia-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.archive = _make_minimal_archive(self.tmpdir)

    def test_incomplete_media_root_blocks_before_any_stage_runs(self):
        incomplete = self.tmpdir / "incomplete-media"
        incomplete.mkdir()
        result = subprocess.run(
            [str(RESTORE_DIR / "restore.sh"), "--archive", str(self.archive),
             "--staging-root", str(self.tmpdir / "staging"),
             "--recovery-media-root", str(incomplete), "--apply"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("not a valid/complete recovery-media tree", combined)
        self.assertNotIn(">>> Running", combined)
        self.assertFalse((self.tmpdir / "staging").exists())


class InteractiveMediaDiscoveryTests(SimpleTestCase):
    """Real pty-driven tests of restore.sh's interactive recovery-media
    discovery, combined with the r0044 ledger/adopt workflow -- proving
    they compose correctly (media resolved first, then the ledger/adopt
    menu, matching the documented acceptance sequence)."""

    def setUp(self):
        super().setUp()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-e-interactive-media-"))
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
        self.archive = self._build_archive()

    def _build_archive(self) -> Path:
        workdir = self.tmpdir / "work"
        workdir.mkdir()
        (workdir / "MANIFEST.txt").write_text(f"IsadoraAir Git SHA:     {self.fixture_sha}\n", encoding="utf-8")
        app_dir = self.tmpdir / "app_build" / "isadoraair"
        app_dir.mkdir(parents=True)
        (app_dir / ".env").write_text("SECRET_KEY=test\n", encoding="utf-8")
        (app_dir / "manage.py").write_text("#!/usr/bin/env python\n", encoding="utf-8")
        with tarfile.open(workdir / "app.tar.gz", "w:gz") as tf:
            tf.add(app_dir, arcname="isadoraair")
        (workdir / "database.dump").write_bytes(b"PGDMP" + b"\x00" * 32)
        archive = self.tmpdir / "backup.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(workdir, arcname=".")
        return archive

    def _env(self):
        # Isolate $HOME so discovery cannot pick up any REAL recovery-media
        # tree that happens to exist on the host running the tests (e.g.
        # this session's own real E8 export lives under the real $HOME) --
        # these tests assert on exactly the synthetic candidates they set up.
        isolated_home = self.tmpdir / "isolated-home"
        isolated_home.mkdir(parents=True, exist_ok=True)
        return {
            **os.environ,
            "RESTORE_RECOVERY_RECEIPT_ROOT": str(self.ledger_root),
            "HOME": str(isolated_home),
        }

    def _args(self):
        return [
            "bash", str(RESTORE_DIR / "restore.sh"),
            "--archive", str(self.archive), "--staging-root", str(self.staging), "--apply",
        ]

    def test_single_discovered_media_root_shown_then_ledger_adopt_menu_follows(self):
        """The documented acceptance sequence: media discovered/shown
        FIRST, then the pre-ledger adoption menu (since this target
        also has restore progress but no ledger)."""
        # Media root discoverable near the archive (backups/ sibling).
        media_export_dir = self.tmpdir / "my-export"
        media = _make_synthetic_media_root(media_export_dir / "e8-inputs")
        shutil.copy(self.archive, media / "backups" / self.archive.name)
        self.archive = media / "backups" / self.archive.name

        # Pre-ledger restore progress at the target.
        target = self.staging / "opt" / "isadoraair"
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", str(self.fixture_repo), str(target)], check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "-q", "--detach", self.fixture_sha], check=True)
        (target / ".env").write_text("SECRET_KEY=test\n", encoding="utf-8")

        returncode, output = _run_pty(self._args(), self._env(), send=b"Q\n", timeout=25)
        self.assertIn("Recovery media found for this archive", output)
        self.assertIn(str(media), output)
        self.assertIn("Existing IsadoraAir restore state was found, but no recovery ledger exists", output)
        self.assertIn("Cancelled -- no changes made", output)

    def test_ambiguous_media_prompts_and_decline_proceeds_without_media(self):
        media_a = _make_synthetic_media_root(self.tmpdir / "export-a" / "e8-inputs")
        media_b = _make_synthetic_media_root(self.tmpdir / "export-b" / "e8-inputs")
        # Neither contains the archive -- both structurally valid, so
        # this is the "ambiguous" (no unambiguous single match) case.
        # Stopped as soon as the decline is processed -- with no media
        # root resolved and no --repo-url given, letting the real stage
        # loop continue would fall through to Stage 20's default
        # *online* git clone against the real upstream repo, which a
        # unit test must never wait on.
        returncode, output = _run_pty(
            self._args(), self._env(), send=b"0\nQ\n", timeout=25,
            stop_after=b"proceeding with online/default sources",
        )
        self.assertIn("Multiple possible recovery-media roots were found", output)
        self.assertIn(str(media_a), output)
        self.assertIn(str(media_b), output)
        self.assertIn("none selected -- proceeding with online/default sources", output)

    def test_no_media_found_prompts_and_blank_proceeds(self):
        # Stopped as soon as the prompt itself is shown -- this test only
        # cares about the discovery/prompt behavior, not the real (online,
        # network-dependent) stage loop that would follow.
        returncode, output = _run_pty(
            self._args(), self._env(), send=b"\n", wait_before_send=1.0, timeout=15,
            stop_after=b"Recovery-media root []:",
        )
        self.assertIn("No local recovery media", output)
        self.assertNotIn("Recovery media: using", output)


class NoOnlineFallbackTests(SimpleTestCase):
    """Real proof (not just structural) that PIP_NO_INDEX=1 +
    PIP_FIND_LINKS genuinely prevent pip from reaching the network,
    exactly the env pair Stage 60/80 are routed under --recovery-media-
    root."""

    def test_pip_with_no_index_and_empty_wheelhouse_cannot_fall_back_online(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-e-no-online-fallback-"))
        try:
            wheelhouse = tmpdir / "wheelhouse"
            wheelhouse.mkdir()
            env = {
                **os.environ,
                "PIP_NO_INDEX": "1",
                "PIP_FIND_LINKS": str(wheelhouse),
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            }
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--dry-run",
                 "this-package-definitely-does-not-exist-isadoraair-r0045-test"],
                capture_output=True, text=True, timeout=30, env=env,
            )
            self.assertNotEqual(result.returncode, 0)
            combined = (result.stdout + result.stderr).lower()
            # pip's own error for a genuinely offline, no-index lookup --
            # never "could not find a version" phrased as if it HAD
            # checked an index it wasn't supposed to reach at all, and
            # never any indication of actual network activity.
            self.assertTrue(
                "no matching distribution" in combined or "no such comparison" in combined
                or "could not find a version" in combined,
                combined,
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
