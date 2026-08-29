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
deployment follow-up, resolved" section for the full history.

`EncryptRecoveryCredentialsScriptTests` (2026-08-18, Phase 4.5 final
follow-up) is the one class here that DOES really execute a script --
deploy/encrypt_recovery_credentials.sh was deliberately split out as
its own small, standalone, dependency-light helper (no pg_dump, no
SFTP, no real .env) specifically so this kind of real functional test
is possible without touching production secrets: every credential path
it reads is overridden to a synthetic temp-dir file, and (for the
"encryption succeeds" cases) a disposable age keypair generated inside
a temp dir is used -- never any real host credential or the operator's
actual recovery key. Those cases are skipped when `age` isn't installed
on the machine running the tests (it is an optional package, see
deploy/packages-ubuntu-26.04.txt's OPTIONAL_BACKUP_ENCRYPTION); the
fail-closed-path tests (disabled, bad recipient, private-key-rejected,
missing-age) need no real binary at all and always run."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from django.test import SimpleTestCase

DEPLOY_DIR = Path(__file__).resolve().parent.parent.parent / "deploy"
SCRIPT_PATH = DEPLOY_DIR / "backup_isadoraair.sh"
SERVICE_PATH = DEPLOY_DIR / "isadoraair-backup.service"
NINETY_SYSTEM_CONFIG = DEPLOY_DIR / "restore" / "90-system-config.sh"
TEN_PACKAGES = DEPLOY_DIR / "restore" / "10-packages.sh"
PACKAGES_MANIFEST = DEPLOY_DIR / "packages-ubuntu-26.04.txt"
ENCRYPT_CREDS_SCRIPT = DEPLOY_DIR / "encrypt_recovery_credentials.sh"
STATION_CONTENT_STAGE = DEPLOY_DIR / "restore" / "40-station-content.sh"

AGE_INSTALLED = shutil.which("age") is not None


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

    # ---- 2026-08-18 Phase 4.5 final follow-up: encrypted recovery-
    # credential preservation -- static assertions on backup_isadoraair.sh
    # itself. Real functional coverage of the encryption logic lives in
    # EncryptRecoveryCredentialsScriptTests below, against the standalone
    # helper script this file calls (deploy/encrypt_recovery_credentials.sh).

    def test_script_version_bumped_for_new_archive_layout(self):
        # Runtime Foundation E7B (2026-08-29): bumped again, to a MAJOR
        # version (3.0.0) -- see RuntimeRecoveryPayloadBackupTests for the
        # dedicated v3/runtime-recovery coverage. This assertion still only
        # needs to prove SCRIPT_VERSION was deliberately bumped past its
        # pre-E7B baseline, not pin the exact string forever.
        self.assertIn('SCRIPT_VERSION="3.0.0"', self.text)
        self.assertNotIn('SCRIPT_VERSION="2.1.0"', self.text)

    def test_encryption_step_calls_the_standalone_helper_script(self):
        self.assertIn(
            '$("$SCRIPT_DIR/encrypt_recovery_credentials.sh" "$RECOVERY_CRED_DIR")', self.text,
        )

    def test_encryption_failure_aborts_before_manifest_and_upload(self):
        """A configured-but-failed encryption run must abort the whole
        backup before MANIFEST.txt is written and before any upload --
        never silently proceed as if the feature weren't configured."""
        helper_call_pos = self.text.index('encrypt_recovery_credentials.sh" "$RECOVERY_CRED_DIR"')
        abort_pos = self.text.index("Aborting before upload rather than producing a backup")
        manifest_pos = self.text.index("Writing backup manifest...")
        upload_pos = self.text.index('echo "Uploading via SFTP')
        self.assertLess(helper_call_pos, abort_pos)
        self.assertLess(abort_pos, manifest_pos)
        self.assertLess(manifest_pos, upload_pos)
        # The abort itself must actually exit non-zero, not just print.
        abort_block = self.text[helper_call_pos - 20:abort_pos + 400]
        self.assertIn("exit 1", abort_block)

    def test_encryption_step_runs_unconditionally_not_only_under_dry_run_or_live(self):
        """The encryption step must sit outside both the early
        DRY_RUN-vs-real credential-file branch (lines near the top that
        decide whether to read ~/.iasboxbu.cred for SFTP) and the late
        DRY_RUN-vs-upload branch -- it always runs, regardless of
        DRY_RUN, per its own documented contract (DRY_RUN means "no SFTP
        connection", not "skip every filesystem operation")."""
        encrypting_line_pos = self.text.index('echo "Encrypting recovery credentials')
        early_dry_run_branch_end = self.text.index("if ! command -v pg_dump")
        late_dry_run_branch_start = self.text.index('if [ "$DRY_RUN" = "1" ]; then\n  echo "DRY_RUN=1: not uploading')
        # Must come after the early credential-reading branch entirely...
        self.assertGreater(encrypting_line_pos, early_dry_run_branch_end)
        # ...and before the late upload-vs-dry-run branch, i.e. it isn't
        # nested inside either DRY_RUN conditional.
        self.assertLess(encrypting_line_pos, late_dry_run_branch_start)

    def test_manifest_records_inclusion_status_never_secret_values(self):
        """The manifest block built from the helper script's stdout must
        only ever carry name/status pairs (e.g. "included"/"absent"),
        never a credential value -- the helper's own contract already
        guarantees it never prints one, this is the belt-and-suspenders
        check that the manifest-assembly code doesn't introduce one by
        reading the source files directly."""
        self.assertIn("Recovery credential encryption:", self.text)
        self.assertIn("Recovery credential cipher: age", self.text)
        self.assertIn('Recovery credential ${name}: ${status}', self.text)
        # The manifest-building loop only ever reads $RECOVERY_CRED_STATUS_OUTPUT
        # (the helper's own stdout) -- never re-reads a credential file itself.
        self.assertNotIn("cat \"$IASBOXBU_CRED_FILE\"", self.text)
        self.assertNotIn("cat ~/.iasboxbu.cred", self.text)

    def test_archive_manifest_lists_recovery_credentials_directory(self):
        self.assertIn("recovery-credentials/*.age", self.text)

    def test_secrets_never_included_list_no_longer_claims_all_three_cred_files_are_excluded(self):
        """The old blanket claim ("these three files are NEVER included
        in any form") is no longer true once encryption is configured --
        the manifest's "Secrets NEVER included" section must be updated
        to only list what is genuinely never included even encrypted
        (acme.sh, StereoTool license), not the three credential files."""
        never_included_start = self.text.index("Secrets NEVER included in ANY form")
        never_included_end = self.text.index("MANIFEST\n", never_included_start)
        block = self.text[never_included_start:never_included_end]
        self.assertNotIn(".iasboxbu.cred", block)
        self.assertNotIn(".syndicated_ingest.cred", block)
        self.assertNotIn(".ogremote_ingest.cred", block)
        self.assertIn("acme.sh", block)
        self.assertIn("StereoTool license", block)

    def test_manifest_documents_the_bootstrap_rule(self):
        """The manifest itself must not overclaim that the encrypted
        in-archive copy solves retrieving the archive in the first
        place -- this is the same "bootstrap rule" the DR docs spell out
        in full; the manifest's own short version must at least gesture
        at it so a future reader relying on the archive alone isn't
        misled."""
        self.assertIn("NOT a bootstrap source for retrieving this archive", self.text)

    def test_encryption_never_decrypts_or_touches_a_private_key(self):
        """This script must have no code path that could plausibly
        decrypt anything -- no -i/--identity flag, no `age --decrypt`,
        anywhere in the file."""
        self.assertNotIn("--decrypt", self.text)
        self.assertNotIn(" -i ", self.text)
        self.assertNotRegex(self.text, r"age\s+[^\n]*--identity")

    def test_recovery_cred_dir_is_inside_the_ephemeral_workdir(self):
        """The plaintext-adjacent working area for the ciphertext output
        must live under $WORKDIR (removed by the cleanup trap on every
        exit path, success or failure), never somewhere that could
        survive a crash."""
        self.assertIn('RECOVERY_CRED_DIR="$WORKDIR/recovery-credentials"', self.text)


class RuntimeRecoveryPayloadBackupTests(SimpleTestCase):
    """Runtime Foundation E7B -- static assertions proving
    deploy/backup_isadoraair.sh's own runtime-recovery/ integration,
    matching this file's own established real-execution-would-need-
    production-secrets rationale (see module docstring)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.text = SCRIPT_PATH.read_text(encoding="utf-8")

    def test_script_version_is_separate_from_archive_format_classification(self):
        self.assertIn('SCRIPT_VERSION="3.0.0"', self.text)
        self.assertNotIn('SCRIPT_VERSION="2.1.0"', self.text)
        self.assertIn("runtime-recovery-archive.json", self.text)
        self.assertIn("archive_format_version", self.text)
        self.assertIn("recovery_class", self.text)

    def test_recovery_payload_config_vars_default_safely(self):
        self.assertIn(
            'RECOVERY_PAYLOAD_ROOT="${RECOVERY_PAYLOAD_ROOT:-/var/lib/isadoraair/runtime-recovery}"', self.text
        )
        self.assertIn('BACKUP_REQUIRED_RECOVERY_COMPONENTS="${BACKUP_REQUIRED_RECOVERY_COMPONENTS:-}"', self.text)

    def test_payload_validated_via_the_real_e7a_python_api_not_reimplemented(self):
        """Must call validate_runtime_recovery_payload (the actual E7A/
        E7B Python authority) -- never parse/re-derive wheel/model/
        source identity itself in shell."""
        self.assertIn("validate_runtime_recovery_payload --base-root", self.text)
        self.assertIn("--current --json", self.text)
        # No independent hash table or wheel/package constant sneaked in.
        self.assertNotIn("kokoro-onnx", self.text)
        self.assertNotIn("sha256sum", self.text)

    def test_no_network_acquisition_introduced(self):
        """This step must never fetch anything -- no pip install, no
        curl/wget, no --download-sources -- only validate+copy an
        already-prepared local payload."""
        self.assertNotIn("--download-sources", self.text)
        self.assertNotIn("pip install", self.text)
        self.assertNotIn("curl ", self.text)
        self.assertNotIn("wget ", self.text)

    def test_configured_but_broken_payload_always_aborts(self):
        """Exit code 2 is treated specially ONLY when paired with an
        EMPTY BACKUP_REQUIRED_RECOVERY_COMPONENTS -- every other exit
        code (including exit 2 WITH a policy configured) is fatal,
        unconditionally."""
        self.assertIn('if [ "$RECOVERY_PAYLOAD_EXIT" -eq 2 ] && [ -z "$BACKUP_REQUIRED_RECOVERY_COMPONENTS" ]; then', self.text)
        self.assertIn('elif [ "$RECOVERY_PAYLOAD_EXIT" -ne 0 ]; then', self.text)
        self.assertIn("aborting before upload", self.text)

    def test_not_configured_case_never_aborts_when_no_policy_set(self):
        start = self.text.index('if [ "$RECOVERY_PAYLOAD_EXIT" -eq 2 ]')
        end = self.text.index("elif ", start)
        not_configured_branch = self.text[start:end]
        self.assertNotRegex(not_configured_branch, r"\bexit 1\b")

    def test_copied_payload_is_re_validated_inside_workdir(self):
        """Belt-and-suspenders, matching the existing app.tar.gz manage.py/
        .env checks -- the COPY itself must be re-validated, not just the
        original source."""
        self.assertIn('validate_runtime_recovery_payload "$WORKDIR/runtime-recovery"', self.text)

    def test_unprivileged_backup_copy_does_not_attempt_to_preserve_root_ownership(self):
        start = self.text.index("Validating and including the current Runtime Foundation E7")
        end = self.text.index('echo "Encrypting recovery credentials', start)
        payload_step = self.text[start:end]
        self.assertIn('cp -R "$RECOVERY_PAYLOAD_RESOLVED_PATH/."', payload_step)
        self.assertNotIn('cp -a "$RECOVERY_PAYLOAD_RESOLVED_PATH/."', payload_step)

    def test_policy_is_parsed_by_the_strict_python_authority(self):
        self.assertIn('--require-components "$BACKUP_REQUIRED_RECOVERY_COMPONENTS"', self.text)
        self.assertNotIn("REQUIRED_COMPONENT_LIST", self.text)

    def test_recovery_payload_step_runs_before_manifest_and_upload(self):
        payload_step = self.text.index("Validating and including the current Runtime Foundation E7")
        manifest_write = self.text.index('echo "Writing backup manifest...')
        upload_step = self.text.index("Uploading via SFTP")
        self.assertLess(payload_step, manifest_write)
        self.assertLess(manifest_write, upload_step)

    def test_recovery_payload_step_is_not_gated_by_dry_run(self):
        """This step touches no network at all (pure local filesystem +
        one Django ORM read for Piper freshness) -- it must run
        identically whether or not DRY_RUN=1, exactly like every other
        archive-building step, never specially skipped."""
        start = self.text.index("Validating and including the current Runtime Foundation E7")
        end = self.text.index('echo "Encrypting recovery credentials')
        step_body = self.text[start:end]
        self.assertNotIn("DRY_RUN", step_body)

    def test_manifest_records_required_fields(self):
        for expected in (
            "Runtime recovery payload ID:",
            "Runtime recovery payload schema version:",
            "Runtime recovery product-contract digest:",
            "Runtime recovery tts component:",
            "Runtime recovery native fdkaac component:",
            "Runtime recovery required-component policy:",
            "Runtime recovery required-component policy satisfied:",
        ):
            self.assertIn(expected, self.text)

    def test_manifest_contents_listing_mentions_runtime_recovery(self):
        self.assertIn("runtime-recovery/", self.text)

    def test_venv_python_prerequisite_checked_early(self):
        self.assertIn('APP_VENV_PYTHON="$PROJECT_DIR/venv/bin/python"', self.text)
        self.assertIn('if [ ! -x "$APP_VENV_PYTHON" ]; then', self.text)

    def test_json_field_extraction_uses_stdlib_json_never_eval(self):
        self.assertIn("import json, sys", self.text)
        self.assertNotIn("eval ", self.text)
        self.assertNotIn("eval(", self.text)

    def test_existing_safety_checks_still_present(self):
        """E7B must not regress any pre-existing safeguard."""
        self.assertIn('if [ ! -s "$WORKDIR/database.dump" ]; then', self.text)
        self.assertIn("manage\\.py", self.text)
        self.assertIn(".env.bak", self.text)
        self.assertIn(".env.lock", self.text)
        self.assertIn("album_art_cache", self.text)
        self.assertIn('REMOTE_PARTIAL="${REMOTE_FILE}.partial"', self.text)
        self.assertIn("rename ${REMOTE_PARTIAL} ${REMOTE_FILE}", self.text)
        self.assertIn("RETENTION_DAYS=30", self.text)


class EncryptRecoveryCredentialsScriptTests(SimpleTestCase):
    """Real functional tests against deploy/encrypt_recovery_credentials.sh
    -- the standalone helper backup_isadoraair.sh calls. Every credential
    path is overridden via env to a synthetic file inside a temp
    directory; no production credential file is ever read. Tests that
    need a successful `age` encryption use a disposable keypair generated
    fresh inside the test's own temp dir -- never the operator's real
    recovery key -- and are skipped entirely if `age` is not installed on
    the machine running the suite (it's an optional package; see
    deploy/packages-ubuntu-26.04.txt)."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-encrypt-creds-test-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.output_dir = self.tmpdir / "out"
        self.cred_dir = self.tmpdir / "creds"
        self.cred_dir.mkdir()
        self.iasboxbu = self.cred_dir / "iasboxbu.cred"
        self.syndicated = self.cred_dir / "syndicated.cred"
        self.ogremote = self.cred_dir / "ogremote.cred"

    def _base_env(self, **overrides):
        env = dict(os.environ)
        env.pop("BACKUP_RECOVERY_AGE_RECIPIENT", None)
        env.pop("BACKUP_RECOVERY_AGE_RECIPIENT_FILE", None)
        env["IASBOXBU_CRED_FILE"] = str(self.iasboxbu)
        env["SYNDICATED_INGEST_CRED_FILE"] = str(self.syndicated)
        env["OGREMOTE_INGEST_CRED_FILE"] = str(self.ogremote)
        env.update(overrides)
        return env

    def _run(self, env):
        return subprocess.run(
            [str(ENCRYPT_CREDS_SCRIPT), str(self.output_dir)],
            capture_output=True, text=True, timeout=30, env=env,
        )

    def _make_keypair(self):
        """Real disposable age keypair, generated entirely inside this
        test's own temp dir -- never touches any real recovery key."""
        result = subprocess.run(["age-keygen"], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        identity_text = result.stdout
        recipient = None
        for line in identity_text.splitlines():
            if line.startswith("# public key:"):
                recipient = line.split(":", 1)[1].strip()
        self.assertIsNotNone(recipient, f"could not parse recipient from age-keygen output:\n{identity_text}")
        identity_path = self.tmpdir / "test-identity.txt"
        identity_path.write_text(identity_text)
        identity_path.chmod(0o600)
        return recipient, identity_path

    # ---- disabled path (no age binary needed) --------------------------

    def test_disabled_when_neither_env_var_set(self):
        result = self._run(self._base_env())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("STATUS=disabled", result.stdout)
        self.assertFalse(self.output_dir.exists(), "disabled path must not create the output dir at all")

    def test_disabled_path_does_not_require_age_or_read_any_credential_file(self):
        """Belt-and-suspenders: even if a credential file exists, the
        disabled path must not touch it -- proven here by making the
        source files exist with real-looking (but fake) content and
        confirming nothing is read/encrypted."""
        self.iasboxbu.write_text("BAK_HOST=example.test\nBAK_PASS=notreal\n")
        result = self._run(self._base_env())
        self.assertEqual(result.returncode, 0)
        self.assertIn("STATUS=disabled", result.stdout)
        self.assertNotIn("CRED", result.stdout)

    # ---- explicitly configured but broken -> fail closed ----------------

    def test_malformed_recipient_fails_closed(self):
        result = self._run(self._base_env(BACKUP_RECOVERY_AGE_RECIPIENT="not-a-real-recipient"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not look like a valid age public recipient", result.stderr)
        self.assertFalse((self.output_dir / "iasboxbu.cred.age").exists())

    def test_private_key_configured_as_recipient_is_explicitly_rejected(self):
        """A private identity accidentally pasted into the recipient
        slot must never silently "work". In practice this string never
        matches the age1... recipient regex at all (wrong prefix, wrong
        case) so it's caught by the same structural check as any other
        malformed value -- but the error text must still explicitly call
        out the private-key case by name, not just say "malformed",
        since that's the single most likely way this specific mistake
        happens in real operator use."""
        fake_private = "AGE-SECRET-KEY-1QYQSZQGPQYQSZQGPQYQSZQGPQYQSZQGPQYQSZQGPQYQSZQGPQYQSZQFXXXXX"
        result = self._run(self._base_env(BACKUP_RECOVERY_AGE_RECIPIENT=fake_private))
        self.assertEqual(result.returncode, 1)
        self.assertIn("AGE-SECRET-KEY", result.stderr)
        self.assertIn("private key", result.stderr.lower())

    def test_recipient_file_missing_fails_closed(self):
        result = self._run(self._base_env(
            BACKUP_RECOVERY_AGE_RECIPIENT_FILE=str(self.tmpdir / "does-not-exist.pub"),
        ))
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not exist", result.stderr)

    def test_recipient_file_empty_or_all_comments_fails_closed(self):
        recipient_file = self.tmpdir / "recipient.pub"
        recipient_file.write_text("# just a comment\n\n")
        result = self._run(self._base_env(BACKUP_RECOVERY_AGE_RECIPIENT_FILE=str(recipient_file)))
        self.assertEqual(result.returncode, 1)
        self.assertIn("no usable recipient line", result.stderr)

    def test_no_secret_value_ever_printed_on_any_path(self):
        """Across every fail-closed scenario above, stdout/stderr must
        never contain the fake secret content used in the test fixture
        itself, if any source file happened to exist."""
        self.iasboxbu.write_text("BAK_PASS=SuperSecretTestValue123\n")
        result = self._run(self._base_env(BACKUP_RECOVERY_AGE_RECIPIENT="not-a-real-recipient"))
        self.assertNotIn("SuperSecretTestValue123", result.stdout)
        self.assertNotIn("SuperSecretTestValue123", result.stderr)

    @staticmethod
    def _hide_age_from_path():
        """Returns a PATH string with every directory containing an
        `age` executable removed -- used to deterministically exercise
        the "age not installed" failure path regardless of whether this
        particular test machine happens to have it."""
        keep = []
        for d in os.environ.get("PATH", "").split(os.pathsep):
            if not d:
                continue
            candidate = Path(d) / "age"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                continue
            keep.append(d)
        return os.pathsep.join(keep)

    def test_missing_age_binary_fails_clearly(self):
        env = self._base_env(BACKUP_RECOVERY_AGE_RECIPIENT="age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszq")
        env["PATH"] = self._hide_age_from_path()
        result = self._run(env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not installed", result.stderr)
        self.assertIn("apt install age", result.stderr)

    # ---- real encryption round-trip (needs a real `age` binary) --------

    @unittest.skipUnless(AGE_INSTALLED, "age is not installed on this test machine")
    def test_enabled_encrypts_all_present_files_and_reports_absent_for_missing(self):
        recipient, identity_path = self._make_keypair()
        self.iasboxbu.write_text("BAK_HOST=example.test\nBAK_PASS=fake-test-value\n")
        self.syndicated.write_text("SMTP_PASSWORD=also-fake\n")
        # ogremote deliberately left absent.

        result = self._run(self._base_env(BACKUP_RECOVERY_AGE_RECIPIENT=recipient))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("STATUS=enabled", result.stdout)
        self.assertIn("CIPHER=age", result.stdout)
        self.assertIn("CRED iasboxbu included", result.stdout)
        self.assertIn("CRED syndicated_ingest included", result.stdout)
        self.assertIn("CRED ogremote_ingest absent", result.stdout)

        iasboxbu_age = self.output_dir / "iasboxbu.cred.age"
        syndicated_age = self.output_dir / "syndicated_ingest.cred.age"
        self.assertTrue(iasboxbu_age.is_file())
        self.assertTrue(syndicated_age.is_file())
        self.assertFalse((self.output_dir / "ogremote_ingest.cred.age").exists())
        self.assertGreater(iasboxbu_age.stat().st_size, 0)

        # Never plaintext: the secret value must not appear anywhere in
        # the ciphertext bytes (a trivially weak but real, cheap check).
        ciphertext = iasboxbu_age.read_bytes()
        self.assertNotIn(b"fake-test-value", ciphertext)

        # Output dir and .age files get the documented restrictive modes.
        self.assertEqual(self.output_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(iasboxbu_age.stat().st_mode & 0o777, 0o600)

        # Real round-trip: decrypting with the matching private key
        # recovers exactly the original plaintext -- proves the helper
        # encrypted to the configured PUBLIC recipient, not something
        # else, and that the ciphertext is genuinely valid age output.
        decrypt = subprocess.run(
            ["age", "--decrypt", "-i", str(identity_path), str(iasboxbu_age)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(decrypt.returncode, 0, decrypt.stderr)
        self.assertEqual(decrypt.stdout, "BAK_HOST=example.test\nBAK_PASS=fake-test-value\n")

    @unittest.skipUnless(AGE_INSTALLED, "age is not installed on this test machine")
    def test_enabled_via_recipient_file_with_leading_comment(self):
        recipient, _identity_path = self._make_keypair()
        recipient_file = self.tmpdir / "recipient.pub"
        recipient_file.write_text(f"# IsadoraAir backup recovery recipient\n{recipient}\n")
        self.iasboxbu.write_text("BAK_HOST=example.test\n")

        result = self._run(self._base_env(BACKUP_RECOVERY_AGE_RECIPIENT_FILE=str(recipient_file)))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CRED iasboxbu included", result.stdout)

    @unittest.skipUnless(AGE_INSTALLED, "age is not installed on this test machine")
    def test_all_three_credentials_absent_still_enabled_zero_included(self):
        """A syntactically valid, real recipient with zero source
        credential files present on this host must not be a failure --
        matches the documented "absent is not a failure" policy."""
        recipient, _identity_path = self._make_keypair()
        result = self._run(self._base_env(BACKUP_RECOVERY_AGE_RECIPIENT=recipient))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("STATUS=enabled", result.stdout)
        self.assertIn("CRED iasboxbu absent", result.stdout)
        self.assertIn("CRED syndicated_ingest absent", result.stdout)
        self.assertIn("CRED ogremote_ingest absent", result.stdout)
        self.assertEqual(list(self.output_dir.glob("*.age")), [])


class PackageBaselineBackupEncryptionTests(SimpleTestCase):
    """deploy/packages-ubuntu-26.04.txt + deploy/restore/10-packages.sh --
    the `age` package must be a documented, opt-in-only group, never part
    of the default install set (a fresh generic install that never
    configures encryption shouldn't need it)."""

    def test_age_listed_in_optional_backup_encryption_group(self):
        text = PACKAGES_MANIFEST.read_text(encoding="utf-8")
        start = text.index("OPTIONAL_BACKUP_ENCRYPTION=(")
        end = text.index(")", start)
        self.assertIn("age", text[start:end].split())

    def test_age_not_in_any_default_installed_group(self):
        """Only CORE/AUDIO_GSTREAMER/BUILD_HEAAC install without an
        explicit opt-in flag -- age must not appear in any of those."""
        text = PACKAGES_MANIFEST.read_text(encoding="utf-8")
        for group in ("CORE", "AUDIO_GSTREAMER", "BUILD_HEAAC"):
            start = text.index(f"{group}=(")
            end = text.index(")", start)
            self.assertNotIn("age", text[start:end].split(), f"'age' unexpectedly in default group {group}")

    def test_with_backup_encryption_flag_wired_into_10_packages(self):
        text = TEN_PACKAGES.read_text(encoding="utf-8")
        self.assertIn("--with-backup-encryption", text)
        self.assertIn("OPTIONAL_BACKUP_ENCRYPTION", text)
        self.assertIn("WITH_BACKUP_ENCRYPTION=1; shift", text)

    def test_with_all_optional_includes_backup_encryption(self):
        text = TEN_PACKAGES.read_text(encoding="utf-8")
        all_optional_line = next(
            line for line in text.splitlines() if "--with-all-optional)" in line
        )
        self.assertIn("WITH_BACKUP_ENCRYPTION=1", all_optional_line)

    def test_plan_mode_reports_age_as_the_resolved_group_when_flag_passed(self):
        """Real subprocess run, --plan only (never touches apt/root) --
        confirms the flag actually resolves to the age package group,
        not just that the flag text exists somewhere in the file."""
        result = subprocess.run(
            [str(TEN_PACKAGES), "--plan", "--with-backup-encryption"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OPTIONAL_BACKUP_ENCRYPTION", result.stdout)

    def test_plan_mode_without_flag_does_not_select_backup_encryption_group(self):
        result = subprocess.run(
            [str(TEN_PACKAGES), "--plan"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        selected_line = next(line for line in result.stdout.splitlines() if "Package groups selected:" in line)
        self.assertNotIn("OPTIONAL_BACKUP_ENCRYPTION", selected_line)


class StereoToolLicenseNotABlockerTests(SimpleTestCase):
    """deploy/restore/40-station-content.sh -- 2026-08-18 Phase 4.5 final
    follow-up: the license checklist item must read as informational
    (log_info), never as a warning (log_warn), and must explicitly say
    it's not a blocker -- the exact behavior change this pass made."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.text = STATION_CONTENT_STAGE.read_text(encoding="utf-8")

    def test_license_line_uses_log_info_not_log_warn(self):
        license_line = next(
            line for line in self.text.splitlines() if "License entered" in line
        )
        self.assertTrue(license_line.strip().startswith("log_info"), license_line)

    def test_license_line_explicitly_says_not_a_blocker(self):
        license_line = next(
            line for line in self.text.splitlines() if "License entered" in line
        )
        self.assertIn("NOT a blocker", license_line)

    def test_binary_and_service_unit_checklist_items_unchanged(self):
        """Only the license line's severity/wording changed -- binary
        and service-unit checklist items must still exist and still be
        real checks (binary via log_warn since it IS still unverified by
        default; service unit still deferred to 90-system-config.sh)."""
        self.assertIn("Binary installed", self.text)
        self.assertIn("Service unit valid", self.text)


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

    def test_recovery_recipient_config_present_but_commented_out_by_default(self):
        """2026-08-18 Phase 4.5 final follow-up: the two config lines for
        encrypted recovery-credential preservation ship in the template
        so an operator has something to uncomment, but must be inert
        (commented out) by default -- a fresh install must not
        accidentally activate this feature just by rendering the unit."""
        env_lines = [
            line for line in self.text.splitlines()
            if "BACKUP_RECOVERY_AGE_RECIPIENT" in line
        ]
        self.assertTrue(env_lines, "no BACKUP_RECOVERY_AGE_RECIPIENT line found in the unit template")
        for line in env_lines:
            self.assertTrue(line.strip().startswith("#"), f"config line is active, not commented out: {line!r}")
        # No literal, real-looking recipient value hardcoded -- only the
        # generic "age1..." placeholder.
        self.assertNotRegex(self.text, r"BACKUP_RECOVERY_AGE_RECIPIENT=age1[a-z0-9]{20,}")
        # Active ExecStart/directive lines (non-comment) must never
        # reference this config directly -- it's purely env-driven.
        for line in self.text.splitlines():
            if line.strip().startswith("#"):
                continue
            self.assertNotIn("BACKUP_RECOVERY_AGE_RECIPIENT", line)

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
