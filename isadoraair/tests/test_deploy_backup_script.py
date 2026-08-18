"""1.2 disaster-recovery Phase 2 -- deploy/backup_isadoraair.sh.

The script itself needs real production secrets (~/.iasboxbu.cred) and
network access to a remote SFTP target to actually run, so these tests
never execute it. Instead they assert the deterministic, static
properties that matter most for disaster-recovery correctness: the
exact set of things it does/doesn't touch, its own safety properties,
and that no secret value has been hardcoded into a file this repo
commits. `bash -n` (syntax only) is exercised separately by this same
test as a subprocess check -- cheap, safe, no side effects.

`BackupServiceUnitTests` and `NinetySystemConfigRendersBackupUnitTests`
below cover the sibling regression this same disaster-recovery work
found in Phase 4.5 (2026-08-17): `deploy/isadoraair-backup.service`'s
own `ExecStart` had drifted stale, still pointing at a historical
`@@ISA_HOME@@/bin/backup_isadoraair.sh` host-local copy that production
itself had already stopped using -- a fresh restore rendering that
template would have installed a unit pointing at a script the restore
tooling never creates. See docs/DISASTER_RECOVERY.md's "Known
deployment follow-up, resolved" section for the full history."""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

DEPLOY_DIR = Path(__file__).resolve().parent.parent.parent / "deploy"
SCRIPT_PATH = DEPLOY_DIR / "backup_isadoraair.sh"
SERVICE_PATH = DEPLOY_DIR / "isadoraair-backup.service"
NINETY_SYSTEM_CONFIG = DEPLOY_DIR / "restore" / "90-system-config.sh"


class BackupScriptExistsAndParsesTests(SimpleTestCase):
    def test_script_exists_and_is_executable(self):
        self.assertTrue(SCRIPT_PATH.is_file(), f"{SCRIPT_PATH} does not exist")
        mode = SCRIPT_PATH.stat().st_mode
        self.assertTrue(mode & 0o111, "backup_isadoraair.sh is not executable (chmod +x)")

    def test_bash_syntax_is_valid(self):
        """`bash -n` parses the script without executing any of it --
        the same check run manually during this feature's own
        validation pass, captured here as a permanent regression
        guard."""
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT_PATH)], capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, f"bash -n failed:\n{result.stderr}")


class BackupScriptContentTests(SimpleTestCase):
    """Static assertions against the script's own text -- deliberately
    NOT executing it (would need real production secrets + network)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.text = SCRIPT_PATH.read_text(encoding="utf-8")

    def test_strict_failure_handling_enabled(self):
        self.assertIn("set -euo pipefail", self.text)

    def test_music_library_is_never_included_only_documented_as_excluded(self):
        """The single highest-stakes property this backup script has:
        it must never accidentally start including the 717+ GB music
        library. Every real mention of the path must be documentation
        (a `#` comment, or plain text inside the MANIFEST.txt heredoc
        this script writes into its own archive) -- never an argument
        to an actual cp/tar/rsync command."""
        self.assertIn("/srv/isadoraair/music", self.text)
        for lineno, line in enumerate(self.text.splitlines(), start=1):
            if "/srv/isadoraair/music" not in line:
                continue
            # The structural guarantee that actually matters: no
            # cp/tar/rsync invocation anywhere in the file takes the
            # music library as a source argument -- this holds
            # regardless of whether the mention is a `#` comment or
            # plain text inside the MANIFEST heredoc.
            self.assertNotRegex(
                line, r"(cp|tar|rsync)\s+.*/srv/isadoraair/music",
                f"line {lineno} passes the music library to cp/tar/rsync: {line!r}",
            )

    def test_no_hardcoded_secret_assignments(self):
        """Every secret value must come from the external cred file
        (~/.iasboxbu.cred) or .env, never a literal assignment in this
        committed script. The one exception is DRY_RUN=1's own obvious,
        human-readable placeholder text ("(dry-run, not used)") for
        BAK_HOST/BAK_PATH -- not a secret, just a stand-in so the rest of
        the script has something non-empty to print/reference when the
        cred file was never even read."""
        for var in ("BAK_PASS", "DB_PASSWORD", "BAK_HOST", "BAK_USER"):
            # A literal `VAR="something"` assignment (not `VAR=$(...)`,
            # not `VAR="${VAR:-...}"`, not reading from a file) would
            # indicate a hardcoded value slipped in.
            for match in re.finditer(rf'^\s*{var}="[^$][^"]*"\s*$', self.text, re.MULTILINE):
                if "dry-run" in match.group(0).lower():
                    continue
                self.fail(f"{var} appears to be hardcoded: {match.group(0)!r}")

    def test_config_file_is_external_not_repo_relative(self):
        self.assertIn('CONFIG_FILE="$HOME/.iasboxbu.cred"', self.text)

    def test_project_dir_is_overridable_not_hardcoded_to_this_session_path(self):
        """Must default to the portable /opt/isadoraair convention
        (matching every other deploy/ file's @@ISA_ROOT@@ placeholder),
        not a literal /home/jreed/... path -- this repo copy has to work
        for a generic install, not just this one checkout. The header
        comment's historical mention of the OLD dead script's own
        /home/jreed/isadoraair-dev path is fine (documentation, not a
        functional default) -- what must never happen is PROJECT_DIR
        itself defaulting to a session-specific path."""
        self.assertIn('PROJECT_DIR="${PROJECT_DIR:-/opt/isadoraair}"', self.text)
        self.assertNotRegex(self.text, r'PROJECT_DIR="\$\{PROJECT_DIR:-/home/')
        self.assertNotRegex(self.text, r'^PROJECT_DIR="/home/', re.MULTILINE)

    def test_nginx_authoritative_source_is_sites_available(self):
        self.assertIn("/etc/nginx/sites-available/isadoraair", self.text)
        self.assertIn("/etc/nginx/snippets/isadoraair-locations.conf", self.text)

    def test_five_core_units_and_stereotool_covered(self):
        for unit in ("isadoraair-gunicorn", "isadoraair-engine", "isadoraair-encoders",
                     "isadoraair-monitoring", "isadoraair-rbds", "stereotool"):
            self.assertIn(unit, self.text)

    def test_sts_profile_covered_by_glob_not_whole_stereotool_tree(self):
        self.assertIn("*.sts", self.text)
        # Must never blindly tar/cp the entire StereoTool directory
        # (binaries, license file, other versions) -- only the .sts
        # profile(s) via the explicit find/glob.
        self.assertNotRegex(self.text, r"(cp -a|tar\s)[^\n]*\$STEREOTOOL_DIR\b")

    def test_reports_and_srv_content_covered(self):
        self.assertIn("REPORTS_ROOT", self.text)
        self.assertIn("for sub in carts voicetracks", self.text)

    def test_waveforms_aircheck_rip_staging_and_mitd_are_not_included(self):
        """Explicitly regenerable/transient/oversized subtrees must
        never appear as an actual copy source, only in exclusion
        documentation (manifest/comments)."""
        for excluded in ("waveforms", "aircheck", "rip_staging", "mitd_artbell"):
            for lineno, line in enumerate(self.text.splitlines(), start=1):
                if excluded not in line:
                    continue
                self.assertTrue(
                    line.strip().startswith("#") or "regenerable" in line.lower()
                    or "transient" in line.lower() or "future" in line.lower()
                    or "own retention" in line.lower() or "MANIFEST" in line or excluded.upper() in line,
                    f"line {lineno} mentions {excluded!r} outside documentation context: {line!r}",
                )

    def test_atomic_remote_promotion_pattern_present(self):
        """A connection drop mid-upload must never leave a file sitting
        under the FINAL backup name that a later restore would mistake
        for a complete, valid archive."""
        self.assertIn("REMOTE_PARTIAL", self.text)
        self.assertIn("rename ${REMOTE_PARTIAL} ${REMOTE_FILE}", self.text)
        # The put must target the partial name, not the final name directly.
        self.assertIn("put ${TMP_TAR} ${REMOTE_PARTIAL}", self.text)

    def test_local_archive_integrity_check_before_upload(self):
        self.assertIn("tar -tzf \"$TMP_TAR\"", self.text)

    def test_empty_database_dump_is_rejected(self):
        self.assertIn('if [ ! -s "$WORKDIR/database.dump" ]', self.text)

    def test_manifest_is_generated_and_lists_git_sha(self):
        self.assertIn("MANIFEST.txt", self.text)
        self.assertIn("GIT_SHA", self.text)
        self.assertIn("SCRIPT_VERSION", self.text)

    def test_retention_and_cleanup_trap_preserved(self):
        self.assertIn("RETENTION_DAYS=30", self.text)
        self.assertIn("trap cleanup EXIT", self.text)

    def test_dry_run_mode_skips_credentials_and_network(self):
        """DRY_RUN=1 must never read the credential file and must never
        call sftp_run -- the whole point is exercising the real archive-
        building path with zero network/remote access."""
        self.assertIn('DRY_RUN="${DRY_RUN:-0}"', self.text)
        self.assertIn('if [ "$DRY_RUN" = "1" ]; then', self.text)
        self.assertIn("skipping ${CONFIG_FILE} entirely", self.text)
        self.assertIn("not uploading, not pruning", self.text)

    def test_dry_run_archive_is_preserved_not_deleted(self):
        """cleanup() must only skip deleting $TMP_TAR when DRY_RUN=1 --
        the whole archive-inspection use case depends on the file still
        existing after the script exits."""
        self.assertIn('if [ "$DRY_RUN" != "1" ]; then', self.text)
        self.assertIn('rm -f "$TMP_TAR"', self.text)

    def test_app_archive_dereferences_project_dir_symlink(self):
        """PROJECT_DIR (/opt/isadoraair by convention) is itself a symlink
        to the real checkout. Real bug found and fixed 2026-08-12 via a
        DRY_RUN validation pass: without -h/--dereference, tar does not
        follow a symlink passed as its own named path argument -- it
        archives the symlink record itself (one tiny entry, ~4KB
        compressed) and silently stops there. `set -e` can't catch this;
        tar itself exits 0, having "successfully" archived the wrong
        thing. This is the regression guard for that fix."""
        match = re.search(r'tar\s+[^\n]*"\$WORKDIR/app\.tar\.gz"', self.text)
        self.assertIsNotNone(match, "could not find the app.tar.gz tar invocation")
        self.assertIn(
            "-h", match.group(0).split(),
            f"app.tar.gz tar invocation is missing -h/--dereference: {match.group(0)!r}",
        )

    def test_app_archive_content_is_verified_after_creation(self):
        """Belt-and-suspenders beyond the -h flag itself: even if some
        future change reintroduces a symlink-stub regression by another
        path, the script must positively confirm app.tar.gz actually
        contains the application (manage.py) and .env before proceeding
        to upload -- not just trust that the tar command it ran was
        correct."""
        self.assertIn("manage\\.py", self.text)
        self.assertIn(r'grep -qE "^$(basename "$PROJECT_DIR")/manage\.py$"', self.text)
        self.assertIn(r'grep -qE "^$(basename "$PROJECT_DIR")/\.env$"', self.text)
        self.assertIn("app.tar.gz is missing manage.py", self.text)
        self.assertIn("app.tar.gz is missing .env", self.text)

    def test_app_archive_content_check_avoids_pipefail_sigpipe_false_failure(self):
        """Real second bug found in the same DRY_RUN validation pass that
        found the symlink issue: `tar -tzf ... | grep -q ...` under
        `set -o pipefail` spuriously "fails" even when grep matches --
        `grep -q` exits the instant it finds a match, SIGPIPEs tar, and
        pipefail reports tar's SIGPIPE exit status for the whole pipeline
        regardless of grep's own (successful) result. The listing must be
        captured to a variable first so grep never reads from a live pipe
        tar could get SIGPIPE'd out from under."""
        self.assertIn('APP_TAR_LISTING=$(tar -tzf "$WORKDIR/app.tar.gz")', self.text)
        self.assertNotIn('tar -tzf "$WORKDIR/app.tar.gz" | grep', self.text)
        self.assertIn('<<< "$APP_TAR_LISTING"', self.text)

    def test_env_included_but_env_bak_and_env_lock_excluded(self):
        """Real bug found by the 2026-08-12 DRY_RUN validation pass: the
        app-tree tar invocation swept up two pre-existing, untracked
        project-root files alongside the intended .env --
        .env.bak (a stale hand-made copy, itself holding real secrets)
        and .env.lock (a lock file, not configuration). Neither is
        anything a restore needs, and .env.bak in particular is exactly
        the kind of stray secrets-bearing file this backup should not be
        multiplying copies of. .env itself must still be fully included."""
        self.assertIn('--exclude="$(basename "$PROJECT_DIR")/.env.bak"', self.text)
        self.assertIn('--exclude="$(basename "$PROJECT_DIR")/.env.lock"', self.text)
        # The exclude flags must sit inside the actual app.tar.gz tar
        # invocation, not just exist somewhere in the file as text.
        match = re.search(r'tar\s+[^\n]*"\$WORKDIR/app\.tar\.gz"[\s\S]*?-C\s', self.text)
        self.assertIsNotNone(match, "could not find the app.tar.gz tar invocation")
        self.assertIn(".env.bak", match.group(0))
        self.assertIn(".env.lock", match.group(0))
        # .env itself must never be excluded -- it's the whole point of
        # "application tree: code, .env, media/". A bare `.env"` exclude
        # (closing quote immediately after .env, not .env.bak/.env.lock's
        # own longer names) would defeat that -- confirm no such line
        # exists anywhere in the script.
        self.assertNotIn('--exclude="$(basename "$PROJECT_DIR")/.env"', self.text)

    def test_app_archive_exclusion_is_verified_after_creation(self):
        """Belt-and-suspenders mirror of the manage.py/.env inclusion
        check: even if a future change reintroduces .env.bak/.env.lock by
        another path (e.g. a rename that bypasses the --exclude glob),
        the script must positively confirm they did NOT end up in the
        finished app.tar.gz before proceeding to upload."""
        self.assertIn(r'grep -qE "^$(basename "$PROJECT_DIR")/\.env\.(bak|lock)$"', self.text)
        self.assertIn("app.tar.gz contains .env.bak or .env.lock", self.text)

    def test_no_reload_or_restart_commands(self):
        """This script's job is to back up configuration, never to
        apply it -- it must never restart/reload any service."""
        for forbidden in ("systemctl restart", "systemctl reload", "nginx -s reload"):
            self.assertNotIn(forbidden, self.text)


class BackupServiceUnitTests(SimpleTestCase):
    """deploy/isadoraair-backup.service -- the systemd unit template.
    Phase 4.5 regression coverage: the unit must run the repo-managed
    script directly (@@ISA_ROOT@@/deploy/backup_isadoraair.sh), never
    the historical @@ISA_HOME@@/bin/ host-local copy, and the
    credential file stays external to both the script (already covered
    above by test_config_file_is_external_not_repo_relative) and this
    unit itself."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.text = SERVICE_PATH.read_text(encoding="utf-8")

    def test_service_file_exists(self):
        self.assertTrue(SERVICE_PATH.is_file(), f"{SERVICE_PATH} does not exist")

    def test_execstart_runs_the_repo_managed_script(self):
        self.assertIn(
            "ExecStart=@@ISA_ROOT@@/deploy/backup_isadoraair.sh", self.text,
        )

    def test_execstart_does_not_reference_historical_host_local_copy(self):
        """The exact regression this pass fixed: ExecStart must never
        point at the old @@ISA_HOME@@/bin/ path again."""
        self.assertNotIn("@@ISA_HOME@@/bin/backup_isadoraair.sh", self.text)
        for line in self.text.splitlines():
            if line.strip().startswith("ExecStart="):
                self.assertNotIn("ISA_HOME", line, f"ExecStart still references ISA_HOME: {line!r}")

    def test_credential_file_path_is_documentation_only_not_a_directive(self):
        """@@ISA_HOME@@/.iasboxbu.cred may be MENTIONED (in the comment
        explaining the script/credential split), but the unit itself
        must never read it directly -- the script does that (see
        CONFIG_FILE="$HOME/.iasboxbu.cred" in backup_isadoraair.sh)."""
        self.assertNotIn("EnvironmentFile", self.text)
        self.assertNotIn("iasboxbu.cred", " ".join(
            line for line in self.text.splitlines()
            if line.strip().startswith(("ExecStart=", "ExecStartPre=", "ExecStartPost="))
        ))

    def test_no_secret_value_embedded(self):
        for forbidden in ("BAK_PASS=", "BAK_HOST=", "BAK_USER=", "PGPASSWORD"):
            self.assertNotIn(forbidden, self.text)

    def test_renders_cleanly_with_the_documented_placeholder_substitution(self):
        """Same sed loop deploy/README.md documents as the canonical
        install procedure -- confirms the file has no leftover/unknown
        placeholder tokens after substitution."""
        rendered = self.text
        for token, value in (
            ("@@ISA_USER@@", "isadoraair"),
            ("@@ISA_ROOT@@", "/opt/isadoraair"),
            ("@@ISA_HOME@@", "/home/isadoraair"),
        ):
            rendered = rendered.replace(token, value)
        self.assertNotIn("@@", rendered, f"unrendered placeholder left in output:\n{rendered}")
        self.assertIn("ExecStart=/opt/isadoraair/deploy/backup_isadoraair.sh", rendered)


class NinetySystemConfigRendersBackupUnitTests(SimpleTestCase):
    """Real functional test (subprocess, not text matching): exercises
    deploy/restore/90-system-config.sh's own generic deploy/*.service
    render+install loop -- the actual code path a real restore uses --
    against an isolated --staging-root, and confirms the FINAL unit
    landing on disk points at the repo-managed script. Proves the
    restore tooling never needs to manufacture an extra host-local
    backup-script copy; production is never touched (staging root
    only)."""

    def setUp(self):
        self.staging_root = Path(tempfile.mkdtemp(prefix="isadoraair-backup-unit-test-"))
        self.addCleanup(shutil.rmtree, self.staging_root, ignore_errors=True)

    def test_rendered_backup_service_execstart_matches_repo_script(self):
        result = subprocess.run(
            [str(NINETY_SYSTEM_CONFIG), "--staging-root", str(self.staging_root), "--apply"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        rendered_path = self.staging_root / "etc" / "systemd" / "system" / "isadoraair-backup.service"
        self.assertTrue(rendered_path.is_file(), f"{rendered_path} was not rendered")
        rendered_text = rendered_path.read_text(encoding="utf-8")

        expected_execstart = f"ExecStart={self.staging_root}/opt/isadoraair/deploy/backup_isadoraair.sh"
        self.assertIn(expected_execstart, rendered_text)
        self.assertNotIn("ISA_HOME", "\n".join(
            line for line in rendered_text.splitlines() if line.startswith("ExecStart=")
        ))
        self.assertNotIn("@@", rendered_text, "unrendered placeholder left in the installed unit")
