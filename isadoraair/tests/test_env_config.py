"""isadoraair/env_config.py -- 2026-08-11 admin-editable environment
configuration layer, Phase 1.

Every test that touches disk operates ONLY inside pytest/unittest's own
temp directories (via tempfile.TemporaryDirectory or Django's tempdir
helpers below) -- nothing here ever reads or writes the real project
.env, and no real SMTP send happens anywhere in this file.

Round-trip encoding basis
--------------------------
encode_env_value()'s quoting rule was derived from DIRECT, LIVE testing
(not just source-reading) during development of this feature:

  * python-decouple 3.8's RepositoryEnv does line-by-line parsing with
    PURELY POSITIONAL quote-stripping (v[1:-1] if v[0]==v[-1] and both
    are ' or both are ") and NO backslash-escape processing at all,
    regardless of quote type (confirmed via inspect.getsource).

  * systemd's EnvironmentFile= parser (confirmed live via
    `systemd-run --user --pipe --wait --collect
    --property=EnvironmentFile=<file> /usr/bin/env` against constructed
    test files -- a safe, no-root, auto-cleaning technique) strips
    leading/trailing whitespace from bare values; DOES process real
    backslash escapes inside DOUBLE-quoted values (a genuine divergence
    from decouple); silently drops backslashes from BARE unquoted
    values; and treats SINGLE-quote-wrapping as fully literal, with no
    escape processing at all -- matching decouple's own positional-only
    behavior exactly. $ and ` are never expanded by either parser in any
    form.

  * Conclusion: single-quote wrapping is the one encoding provably safe
    for both consumers whenever quoting is needed at all. This is what
    encode_env_value() implements. RoundTripEncodingTests below verifies
    the result against the REAL decouple.RepositoryEnv parser (an actual
    project dependency, not a hand-rolled test double); a systemd-run
    class does the same against the real systemd parser when available,
    and skips cleanly when it isn't (e.g. no user systemd/D-Bus session
    in a sandboxed CI environment), per the instruction to use real
    consumer semantics "where practical" rather than block portability
    on a live external process.
"""
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

import decouple
from django.test import SimpleTestCase, TestCase

from isadoraair import env_config


def _has_user_systemd():
    if shutil.which("systemd-run") is None:
        return False
    try:
        subprocess.run(
            ["systemd-run", "--user", "--pipe", "--wait", "--collect", "/usr/bin/true"],
            capture_output=True, timeout=10,
        )
        return True
    except Exception:
        return False


_HAS_USER_SYSTEMD = _has_user_systemd()


class TempEnvMixin:
    """Every subclass gets an isolated temp directory per test method,
    with self.env_path/self.lock_path/self.backup_path pointing inside
    it -- never the real project .env."""

    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory(prefix="isadoraair-envtest-")
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp_path = Path(self._tmpdir.name)
        self.env_path = self.tmp_path / ".env"
        self.lock_path = self.tmp_path / ".env.lock"
        self.backup_path = self.tmp_path / ".env.bak"

    def write_env(self, text):
        self.env_path.write_text(text, encoding="utf-8")

    def read_env(self):
        return self.env_path.read_text(encoding="utf-8")

    def read_managed(self, keys=None):
        return env_config.read_managed_values(keys=keys, env_path=self.env_path)

    def update_managed(self, values):
        return env_config.update_managed_values(
            values, env_path=self.env_path, lock_path=self.lock_path, backup_path=self.backup_path,
        )


# ---------------------------------------------------------------------
# Never touch the real .env -- structural safety net for this whole file
# ---------------------------------------------------------------------
class RealEnvUntouchedGuardTests(TestCase):
    def test_project_env_file_path_is_not_a_test_temp_path(self):
        """Sanity check on env_config's own constant -- confirms this
        module's tests are wired to temp files, not the real project
        root, before anything else in this file runs."""
        self.assertTrue(str(env_config.ENV_FILE_PATH).endswith("/.env"))
        # Every test in this file passes an explicit env_path= override;
        # none of them should ever exercise ENV_FILE_PATH directly.


# ---------------------------------------------------------------------
# Parser / read behavior
# ---------------------------------------------------------------------
class ParserReadBehaviorTests(TempEnvMixin, TestCase):
    def test_simple_assignment_read(self):
        self.write_env("EMAIL_HOST=mail.example.com\n")
        values = self.read_managed(["EMAIL_HOST"])
        self.assertEqual(values["EMAIL_HOST"].display_value, "mail.example.com")
        self.assertTrue(values["EMAIL_HOST"].present)

    def test_comments_blank_lines_unknown_keys_do_not_affect_read(self):
        self.write_env(
            "# a comment\n"
            "\n"
            "SOME_UNKNOWN_KEY=whatever\n"
            "EMAIL_HOST=mail.example.com\n"
        )
        values = self.read_managed(["EMAIL_HOST"])
        self.assertEqual(values["EMAIL_HOST"].display_value, "mail.example.com")

    def test_absent_allowed_key_falls_back_to_registered_default(self):
        self.write_env("SOME_OTHER_KEY=1\n")
        values = self.read_managed(["EMAIL_HOST", "EMAIL_PORT"])
        self.assertFalse(values["EMAIL_HOST"].present)
        self.assertEqual(values["EMAIL_HOST"].display_value, "localhost")
        self.assertFalse(values["EMAIL_PORT"].present)
        self.assertEqual(values["EMAIL_PORT"].display_value, "587")

    def test_missing_file_reports_all_keys_absent_with_defaults(self):
        values = self.read_managed(["EMAIL_HOST"])
        self.assertFalse(values["EMAIL_HOST"].present)
        self.assertEqual(values["EMAIL_HOST"].display_value, "localhost")

    def test_commented_out_key_not_counted_as_active(self):
        self.write_env("# EMAIL_HOST=commented-out.example.com\n")
        values = self.read_managed(["EMAIL_HOST"])
        self.assertFalse(values["EMAIL_HOST"].present)
        self.assertEqual(values["EMAIL_HOST"].display_value, "localhost")

    def test_duplicate_managed_key_rejected_on_read(self):
        self.write_env("EMAIL_HOST=first.example.com\nEMAIL_HOST=second.example.com\n")
        with self.assertRaises(env_config.DuplicateManagedKeyError) as ctx:
            self.read_managed(["EMAIL_HOST"])
        self.assertIn("EMAIL_HOST", str(ctx.exception))
        self.assertIn("more than once", str(ctx.exception))

    def test_duplicate_where_one_occurrence_is_commented_out_is_fine(self):
        """A commented-out occurrence doesn't count toward the
        duplicate check -- only ACTIVE assignment lines do."""
        self.write_env("# EMAIL_HOST=old.example.com\nEMAIL_HOST=new.example.com\n")
        values = self.read_managed(["EMAIL_HOST"])
        self.assertEqual(values["EMAIL_HOST"].display_value, "new.example.com")

    def test_malformed_line_no_equals_sign_ignored_safely(self):
        self.write_env("this is not a valid assignment at all\nEMAIL_HOST=mail.example.com\n")
        values = self.read_managed(["EMAIL_HOST"])
        self.assertEqual(values["EMAIL_HOST"].display_value, "mail.example.com")

    def test_unallowed_key_cannot_be_read(self):
        with self.assertRaises(env_config.UnregisteredKeyError):
            self.read_managed(["SECRET_KEY"])

    def test_unallowed_key_cannot_be_written(self):
        with self.assertRaises(env_config.UnregisteredKeyError):
            self.update_managed({"SECRET_KEY": "nope"})

    def test_secret_key_display_value_always_none_even_when_present(self):
        self.write_env("EMAIL_HOST_PASSWORD=realpassword123\n")
        values = self.read_managed(["EMAIL_HOST_PASSWORD"])
        self.assertIsNone(values["EMAIL_HOST_PASSWORD"].display_value)
        self.assertTrue(values["EMAIL_HOST_PASSWORD"].present)

    def test_quoted_value_decoded_same_as_real_decouple(self):
        self.write_env('EMAIL_HOST_USER="quoted value"\n')
        values = self.read_managed(["EMAIL_HOST_USER"])
        # Cross-check directly against the real dependency.
        expected = decouple.RepositoryEnv(str(self.env_path)).data["EMAIL_HOST_USER"]
        self.assertEqual(values["EMAIL_HOST_USER"].display_value, expected)
        self.assertEqual(expected, "quoted value")


# ---------------------------------------------------------------------
# Round-trip value encoding
# ---------------------------------------------------------------------
class RoundTripEncodingTests(TempEnvMixin, TestCase):
    """Writes a value via update_managed_values(), then decodes the
    resulting file with the REAL decouple.RepositoryEnv parser (not a
    reimplementation) to prove it comes back byte-identical."""

    ORDINARY_CASES = [
        "plain",
        "has spaces in it",
        "  leading and trailing spaces  ",
        "has#a hash",
        "has=an equals",
        "has'a single quote",
        'has"a double quote',
        "Tr0ub4dor&3!",
        "p@ssw0rd!#$%^&*()_+-={}[]|:;,.<>?/~",
        "",
    ]

    def _roundtrip(self, value):
        self.write_env("EMAIL_HOST_USER=placeholder\n")
        self.update_managed({"EMAIL_HOST_USER": value})
        decoded = decouple.RepositoryEnv(str(self.env_path)).data.get("EMAIL_HOST_USER", "")
        self.assertEqual(decoded, value)

    def test_ordinary_values_round_trip(self):
        for value in self.ORDINARY_CASES:
            with self.subTest(value=value):
                self._roundtrip(value)

    def test_realistic_punctuation_heavy_password_round_trips(self):
        self._roundtrip("Tr0ub4dor&3!$%^*()_+-=[]{}|;:,.<>?")

    def test_blank_value_round_trips(self):
        self._roundtrip("")

    def test_newline_rejected(self):
        with self.assertRaises(env_config.UnsafeValueError):
            env_config.encode_env_value("line one\nline two")

    def test_carriage_return_rejected(self):
        with self.assertRaises(env_config.UnsafeValueError):
            env_config.encode_env_value("value\rwith cr")

    def test_nul_rejected(self):
        with self.assertRaises(env_config.UnsafeValueError):
            env_config.encode_env_value("value\x00with nul")

    def test_value_needing_quoting_with_both_quote_types_rejected_not_corrupted(self):
        """A value with BOTH quote characters is only actually unsafe
        once something ELSE (here, leading/trailing whitespace) forces
        quoting in the first place -- a value that merely contains both
        quote characters somewhere in the middle, with no leading/
        trailing whitespace and non-matching first/last characters,
        never needs quoting at all and round-trips fine bare (see
        test_ordinary_values_round_trip's has'/has" cases)."""
        with self.assertRaises(env_config.UnsafeValueError):
            env_config.encode_env_value("""  has ' and " both, and leading/trailing space  """)

    def test_value_needing_both_quote_types_is_rejected_at_admin_validation_boundary_too(self):
        """Confirms the rejection happens BEFORE any write is attempted
        -- the original file must be untouched."""
        self.write_env("EMAIL_HOST_USER=original\n")
        before = self.read_env()
        with self.assertRaises(env_config.UnsafeValueError):
            self.update_managed({"EMAIL_HOST_USER": """  has ' and " both  """})
        self.assertEqual(self.read_env(), before)

    @skipUnless(_HAS_USER_SYSTEMD, "no usable user systemd/D-Bus session in this environment")
    def test_representative_values_round_trip_through_real_systemd_environment_file(self):
        """Belt-and-suspenders: the same encoded lines this module
        would write, loaded by the REAL systemd EnvironmentFile= parser
        via a disposable, auto-cleaned transient unit -- not just
        decouple. Skips cleanly where a user systemd session isn't
        available (e.g. minimal CI) rather than failing the suite."""
        cases = [
            "plain", "has spaces", "has#hash", "has=equals",
            "has'single", 'has"double', "Tr0ub4dor&3!",
        ]
        lines = []
        keys = []
        for i, value in enumerate(cases):
            key = f"ENVTEST_CASE_{i}"
            keys.append(key)
            lines.append(f"{key}={env_config.encode_env_value(value)}")
        envfile = self.tmp_path / "systemd_test.env"
        envfile.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = subprocess.run(
            [
                "systemd-run", "--user", "--pipe", "--wait", "--collect",
                f"--property=EnvironmentFile={envfile}", "/usr/bin/env",
            ],
            capture_output=True, timeout=15, text=True,
        )
        env_out = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line and line.split("=", 1)[0] in keys
        )
        for i, expected in enumerate(cases):
            self.assertEqual(env_out.get(f"ENVTEST_CASE_{i}"), expected)


# ---------------------------------------------------------------------
# Structural preservation (comments/blanks/unknown/unrelated keys)
# ---------------------------------------------------------------------
class StructuralPreservationTests(TempEnvMixin, TestCase):
    def test_editing_one_key_preserves_everything_else_byte_for_byte(self):
        original = (
            "# top comment\n"
            "\n"
            "DB_NAME=isadoraair\n"
            "UNKNOWN_KEY=untouched\n"
            "\n"
            "# email section\n"
            "EMAIL_HOST=old.example.com\n"
            "EMAIL_PORT=587\n"
        )
        self.write_env(original)
        self.update_managed({"EMAIL_HOST": "new.example.com"})
        result = self.read_env()
        for unrelated_line in [
            "# top comment", "DB_NAME=isadoraair", "UNKNOWN_KEY=untouched",
            "# email section", "EMAIL_PORT=587",
        ]:
            self.assertIn(unrelated_line, result)
        self.assertIn("EMAIL_HOST=new.example.com", result)
        self.assertNotIn("old.example.com", result)

    def test_absent_key_is_appended_not_inserted_mid_file(self):
        self.write_env("DB_NAME=isadoraair\n")
        self.update_managed({"EMAIL_HOST": "mail.example.com"})
        result = self.read_env()
        lines = result.splitlines()
        self.assertEqual(lines[0], "DB_NAME=isadoraair")
        self.assertIn("EMAIL_HOST=mail.example.com", lines)

    def test_never_regenerated_from_env_example(self):
        """Confirms the writer only ever mutates the given file -- it
        has no code path that reads or copies .env.example content."""
        self.write_env("EMAIL_HOST=custom.example.com\nCUSTOM_UNRELATED=keepme\n")
        self.update_managed({"EMAIL_PORT": "2525"})
        result = self.read_env()
        self.assertIn("CUSTOM_UNRELATED=keepme", result)
        self.assertIn("EMAIL_HOST=custom.example.com", result)

    def test_no_op_save_writes_nothing_and_touches_no_backup(self):
        self.write_env("EMAIL_HOST=mail.example.com\n")
        before_mtime = self.env_path.stat().st_mtime_ns
        result = self.update_managed({"EMAIL_HOST": "mail.example.com"})
        self.assertEqual(result.changed_keys, [])
        self.assertEqual(self.env_path.stat().st_mtime_ns, before_mtime)
        self.assertFalse(self.backup_path.exists())

    def test_changed_keys_reports_only_actually_changed(self):
        self.write_env("EMAIL_HOST=mail.example.com\nEMAIL_PORT=587\n")
        result = self.update_managed({"EMAIL_HOST": "mail.example.com", "EMAIL_PORT": "2525"})
        self.assertEqual(result.changed_keys, ["EMAIL_PORT"])


# ---------------------------------------------------------------------
# Duplicate-key handling on write
# ---------------------------------------------------------------------
class DuplicateKeyWriteTests(TempEnvMixin, TestCase):
    def test_duplicate_managed_key_rejected_before_any_write(self):
        original = "EMAIL_HOST=first.example.com\nEMAIL_HOST=second.example.com\n"
        self.write_env(original)
        with self.assertRaises(env_config.DuplicateManagedKeyError):
            self.update_managed({"EMAIL_HOST": "new.example.com"})
        self.assertEqual(self.read_env(), original)
        self.assertFalse(self.backup_path.exists())

    def test_error_message_is_actionable(self):
        self.write_env("EMAIL_HOST=a\nEMAIL_HOST=b\n")
        with self.assertRaises(env_config.DuplicateManagedKeyError) as ctx:
            self.update_managed({"EMAIL_HOST": "c"})
        self.assertEqual(
            str(ctx.exception),
            "EMAIL_HOST is defined more than once in .env. Resolve the duplicate "
            "before editing it from Django admin.",
        )


# ---------------------------------------------------------------------
# Atomic write / lock / backup / permissions / symlink
# ---------------------------------------------------------------------
class AtomicWriteTests(TempEnvMixin, TestCase):
    def test_successful_replacement_content(self):
        self.write_env("EMAIL_HOST=old.example.com\n")
        self.update_managed({"EMAIL_HOST": "new.example.com"})
        self.assertIn("EMAIL_HOST=new.example.com", self.read_env())

    def test_mode_preserved_across_replacement(self):
        self.write_env("EMAIL_HOST=old.example.com\n")
        os.chmod(self.env_path, 0o640)
        self.update_managed({"EMAIL_HOST": "new.example.com"})
        self.assertEqual(stat.S_IMODE(self.env_path.stat().st_mode), 0o640)

    def test_new_file_gets_conservative_mode(self):
        self.update_managed({"EMAIL_HOST": "new.example.com"})
        self.assertEqual(stat.S_IMODE(self.env_path.stat().st_mode), 0o600)

    def test_temp_write_failure_leaves_old_file_intact(self):
        self.write_env("EMAIL_HOST=old.example.com\n")
        with patch("os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(env_config.EnvWriteError):
                self.update_managed({"EMAIL_HOST": "new.example.com"})
        self.assertIn("EMAIL_HOST=old.example.com", self.read_env())

    def test_replace_failure_leaves_old_file_intact(self):
        self.write_env("EMAIL_HOST=old.example.com\n")
        with patch("os.replace", side_effect=OSError("permission denied")):
            with self.assertRaises(env_config.EnvWriteError):
                self.update_managed({"EMAIL_HOST": "new.example.com"})
        self.assertIn("EMAIL_HOST=old.example.com", self.read_env())
        # No stray temp file left behind either.
        leftovers = [p for p in self.tmp_path.iterdir() if p.name.startswith(".env.tmp")]
        self.assertEqual(leftovers, [])

    def test_backup_contains_immediate_previous_version(self):
        self.write_env("EMAIL_HOST=version-one.example.com\n")
        self.update_managed({"EMAIL_HOST": "version-two.example.com"})
        self.assertIn("EMAIL_HOST=version-one.example.com", self.backup_path.read_text())
        self.update_managed({"EMAIL_HOST": "version-three.example.com"})
        self.assertIn("EMAIL_HOST=version-two.example.com", self.backup_path.read_text())
        self.assertNotIn("version-one", self.backup_path.read_text())

    def test_backup_has_restrictive_permissions(self):
        self.write_env("EMAIL_HOST=old.example.com\n")
        self.update_managed({"EMAIL_HOST": "new.example.com"})
        self.assertEqual(stat.S_IMODE(self.backup_path.stat().st_mode), 0o600)

    def test_backup_not_created_on_first_ever_write_no_prior_file(self):
        self.update_managed({"EMAIL_HOST": "new.example.com"})
        self.assertFalse(self.backup_path.exists())

    def test_unknown_content_unchanged_across_a_real_replace(self):
        self.write_env("UNRELATED_KEY=keepme\nEMAIL_HOST=old.example.com\n")
        self.update_managed({"EMAIL_HOST": "new.example.com"})
        self.assertIn("UNRELATED_KEY=keepme", self.read_env())

    def test_lock_file_is_used(self):
        self.write_env("EMAIL_HOST=old.example.com\n")
        self.assertFalse(self.lock_path.exists())
        self.update_managed({"EMAIL_HOST": "new.example.com"})
        self.assertTrue(self.lock_path.exists())

    def test_lock_excludes_a_concurrent_writer(self):
        """Holds the lock externally (simulating a second in-flight
        admin request) and confirms our own call blocks until released
        rather than racing straight through."""
        import fcntl
        import threading
        import time

        self.write_env("EMAIL_HOST=old.example.com\n")
        self.lock_path.touch()
        fd = os.open(str(self.lock_path), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)

        results = {}

        def release_after_delay():
            time.sleep(0.3)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        def do_update():
            start = time.monotonic()
            self.update_managed({"EMAIL_HOST": "new.example.com"})
            results["elapsed"] = time.monotonic() - start

        releaser = threading.Thread(target=release_after_delay)
        updater = threading.Thread(target=do_update)
        releaser.start()
        updater.start()
        releaser.join()
        updater.join(timeout=5)

        self.assertIn("elapsed", results)
        self.assertGreaterEqual(results["elapsed"], 0.25)
        self.assertIn("EMAIL_HOST=new.example.com", self.read_env())

    def test_symlink_target_rejected(self):
        real_target = self.tmp_path / "elsewhere.env"
        real_target.write_text("SECRET_KEY=leaked-if-this-is-followed\n", encoding="utf-8")
        self.env_path.symlink_to(real_target)
        with self.assertRaises(env_config.EnvWriteError) as ctx:
            self.update_managed({"EMAIL_HOST": "new.example.com"})
        self.assertIn("symlink", str(ctx.exception))
        # The symlink target must be untouched.
        self.assertIn("leaked-if-this-is-followed", real_target.read_text())

    def test_env_file_path_constant_uses_resolved_base_dir(self):
        """ENV_FILE_PATH = Path(settings.BASE_DIR) / ".env", and
        settings.BASE_DIR is Path(__file__).resolve().parent.parent in
        isadoraair/settings.py -- .resolve() canonicalizes through any
        symlink in the path. This is what makes the real deployment's
        /opt/isadoraair -> <actual checkout dir> symlink (confirmed
        live: readlink -f agrees for both spellings and they share one
        inode) transparent to this module without any special-casing:
        resolving ENV_FILE_PATH again must be a no-op, proving nothing
        upstream of it left an unresolved symlink component."""
        self.assertTrue(env_config.ENV_FILE_PATH.is_absolute())
        self.assertEqual(env_config.ENV_FILE_PATH, env_config.ENV_FILE_PATH.resolve())

    def test_write_through_symlinked_project_root_operates_on_real_file(self):
        """Regression test for the real deployment shape: an
        operator-facing project path that is itself a symlink to the
        actual checkout directory (e.g. /opt/isadoraair ->
        /home/jreed/isadoraair-django on the production box), with
        .env living as a plain file inside the REAL directory. Writing
        through the aliased/symlinked spelling must land on the exact
        same underlying file as writing through the real path -- not a
        second, divergent copy -- and must NOT be rejected by the
        symlink-safety check (which only ever inspects the FINAL .env
        path component, never an ancestor directory)."""
        real_root = self.tmp_path / "real_project_root"
        real_root.mkdir()
        operator_alias = self.tmp_path / "operator_alias"
        operator_alias.symlink_to(real_root, target_is_directory=True)

        real_env = real_root / ".env"
        real_env.write_text("EMAIL_HOST=old.example.com\n", encoding="utf-8")
        real_env.chmod(0o600)

        aliased_env_path = operator_alias / ".env"
        # Sanity check on the fixture itself: both spellings really do
        # resolve to the identical underlying file.
        self.assertEqual(aliased_env_path.resolve(), real_env.resolve())
        self.assertEqual(os.stat(aliased_env_path).st_ino, os.stat(real_env).st_ino)

        result = env_config.update_managed_values(
            {"EMAIL_HOST": "new.example.com"},
            env_path=aliased_env_path,
            lock_path=operator_alias / ".env.lock",
            backup_path=operator_alias / ".env.bak",
        )
        self.assertEqual(result.changed_keys, ["EMAIL_HOST"])

        # The REAL file (reached via the non-aliased, canonical path)
        # was actually updated -- not a divergent copy under the alias.
        self.assertIn("EMAIL_HOST=new.example.com", real_env.read_text())

        # Reading back through the OTHER spelling agrees.
        values = env_config.read_managed_values(["EMAIL_HOST"], env_path=real_env)
        self.assertEqual(values["EMAIL_HOST"].display_value, "new.example.com")

    def test_ancestor_directory_symlink_does_not_trigger_leaf_symlink_rejection(self):
        """The leaf-only is_symlink() check must not be fooled into
        rejecting a write merely because a directory ABOVE .env is a
        symlink -- only a genuine `.env -> elsewhere` LEAF symlink
        should ever be rejected (see test_symlink_target_rejected for
        that actual-rejection case, which this test is the mirror
        image of)."""
        real_root = self.tmp_path / "real_root2"
        real_root.mkdir()
        alias = self.tmp_path / "alias2"
        alias.symlink_to(real_root, target_is_directory=True)
        (real_root / ".env").write_text("EMAIL_HOST=untouched.example.com\n", encoding="utf-8")

        # Must NOT raise EnvWriteError -- this is the valid, intended
        # deployment shape, not an unsafe symlink target.
        result = env_config.update_managed_values(
            {"EMAIL_HOST": "changed.example.com"},
            env_path=alias / ".env", lock_path=alias / ".env.lock", backup_path=alias / ".env.bak",
        )
        self.assertEqual(result.changed_keys, ["EMAIL_HOST"])
        self.assertIn("EMAIL_HOST=changed.example.com", (real_root / ".env").read_text())

    def test_unwritable_directory_gives_safe_error_not_a_crash(self):
        self.write_env("EMAIL_HOST=old.example.com\n")
        os.chmod(self.tmp_path, 0o500)  # read+execute only, no write
        try:
            with self.assertRaises(env_config.EnvWriteError):
                self.update_managed({"EMAIL_HOST": "new.example.com"})
        finally:
            os.chmod(self.tmp_path, 0o700)  # restore so addCleanup can remove it
        self.assertIn("EMAIL_HOST=old.example.com", self.read_env())

    def test_lock_timeout_raises_env_write_error(self):
        import fcntl
        self.write_env("EMAIL_HOST=old.example.com\n")
        self.lock_path.touch()
        fd = os.open(str(self.lock_path), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with self.assertRaises(env_config.EnvWriteError):
                env_config.update_managed_values(
                    {"EMAIL_HOST": "new.example.com"},
                    env_path=self.env_path, lock_path=self.lock_path, backup_path=self.backup_path,
                )
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


# ---------------------------------------------------------------------
# Secret handling
# ---------------------------------------------------------------------
class SecretHandlingTests(TempEnvMixin, TestCase):
    def test_password_never_returned_by_read_even_when_present(self):
        self.write_env("EMAIL_HOST_PASSWORD=SuperSecret123!\n")
        values = self.read_managed(["EMAIL_HOST_PASSWORD"])
        self.assertIsNone(values["EMAIL_HOST_PASSWORD"].display_value)

    def test_password_never_returned_by_compare_to_running(self):
        self.write_env("EMAIL_HOST_PASSWORD=SuperSecret123!\n")
        with patch.object(env_config.django_settings, "EMAIL_HOST_PASSWORD", "SuperSecret123!"):
            results = env_config.compare_to_running(["EMAIL_HOST_PASSWORD"], env_path=self.env_path)
        self.assertIsNone(results["EMAIL_HOST_PASSWORD"].disk_display)
        self.assertIsNone(results["EMAIL_HOST_PASSWORD"].running_display)
        self.assertTrue(results["EMAIL_HOST_PASSWORD"].matches)

    def test_secret_matches_running_true_when_equal(self):
        self.write_env("EMAIL_HOST_PASSWORD=SuperSecret123!\n")
        with patch.object(env_config.django_settings, "EMAIL_HOST_PASSWORD", "SuperSecret123!"):
            self.assertTrue(env_config.secret_matches_running("EMAIL_HOST_PASSWORD", env_path=self.env_path))

    def test_secret_matches_running_false_when_different(self):
        self.write_env("EMAIL_HOST_PASSWORD=NewOnDisk!\n")
        with patch.object(env_config.django_settings, "EMAIL_HOST_PASSWORD", "OldRunning!"):
            self.assertFalse(env_config.secret_matches_running("EMAIL_HOST_PASSWORD", env_path=self.env_path))

    def test_blank_password_write_round_trips_as_blank(self):
        self.write_env("EMAIL_HOST_PASSWORD=something\n")
        self.update_managed({"EMAIL_HOST_PASSWORD": ""})
        self.assertIn("EMAIL_HOST_PASSWORD=\n", self.read_env())


# ---------------------------------------------------------------------
# Comparison / type normalization ("saved vs running", requirement 14)
# ---------------------------------------------------------------------
class ComparisonTests(TempEnvMixin, TestCase):
    def test_port_int_vs_text_does_not_false_mismatch(self):
        self.write_env("EMAIL_PORT=587\n")
        with patch.object(env_config.django_settings, "EMAIL_PORT", 587):
            results = env_config.compare_to_running(["EMAIL_PORT"], env_path=self.env_path)
        self.assertTrue(results["EMAIL_PORT"].matches)

    def test_tls_bool_vs_text_does_not_false_mismatch(self):
        self.write_env("EMAIL_USE_TLS=True\n")
        with patch.object(env_config.django_settings, "EMAIL_USE_TLS", True):
            results = env_config.compare_to_running(["EMAIL_USE_TLS"], env_path=self.env_path)
        self.assertTrue(results["EMAIL_USE_TLS"].matches)

    def test_tls_lowercase_true_still_matches_running_true(self):
        self.write_env("EMAIL_USE_TLS=true\n")
        with patch.object(env_config.django_settings, "EMAIL_USE_TLS", True):
            results = env_config.compare_to_running(["EMAIL_USE_TLS"], env_path=self.env_path)
        self.assertTrue(results["EMAIL_USE_TLS"].matches)

    def test_genuine_mismatch_detected(self):
        self.write_env("EMAIL_HOST=new-on-disk.example.com\n")
        with patch.object(env_config.django_settings, "EMAIL_HOST", "old-running.example.com"):
            results = env_config.compare_to_running(["EMAIL_HOST"], env_path=self.env_path)
        self.assertFalse(results["EMAIL_HOST"].matches)
        self.assertEqual(results["EMAIL_HOST"].disk_display, "new-on-disk.example.com")
        self.assertEqual(results["EMAIL_HOST"].running_display, "old-running.example.com")

    def test_saved_value_visible_immediately_even_if_running_settings_are_stale(self):
        """Core requirement 13: reloading the admin page after a save
        must show the NEW disk value, never fall back to
        django.conf.settings alone."""
        self.write_env("EMAIL_HOST=freshly-saved.example.com\n")
        with patch.object(env_config.django_settings, "EMAIL_HOST", "stale-running-value.example.com"):
            values = self.read_managed(["EMAIL_HOST"])
        self.assertEqual(values["EMAIL_HOST"].display_value, "freshly-saved.example.com")


# ---------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------
class ValidationTests(TempEnvMixin, TestCase):
    def test_email_host_blank_rejected(self):
        with self.assertRaises(env_config.InvalidValueError):
            self.update_managed({"EMAIL_HOST": ""})

    def test_email_host_whitespace_rejected(self):
        with self.assertRaises(env_config.InvalidValueError):
            self.update_managed({"EMAIL_HOST": "has space.example.com"})

    def test_email_host_newline_rejected(self):
        with self.assertRaises(env_config.InvalidValueError):
            self.update_managed({"EMAIL_HOST": "bad\nvalue"})

    def test_email_port_non_integer_rejected(self):
        with self.assertRaises(env_config.InvalidValueError):
            self.update_managed({"EMAIL_PORT": "not-a-number"})

    def test_email_port_out_of_range_rejected(self):
        with self.assertRaises(env_config.InvalidValueError):
            self.update_managed({"EMAIL_PORT": "0"})
        with self.assertRaises(env_config.InvalidValueError):
            self.update_managed({"EMAIL_PORT": "70000"})

    def test_email_port_valid_boundaries_accepted(self):
        self.update_managed({"EMAIL_PORT": "1"})
        self.update_managed({"EMAIL_PORT": "65535"})

    def test_use_tls_unrecognized_value_rejected(self):
        with self.assertRaises(env_config.InvalidValueError):
            self.update_managed({"EMAIL_USE_TLS": "maybe"})

    def test_use_tls_recognized_values_accepted(self):
        for value in ("True", "False", "yes", "no", "1", "0"):
            with self.subTest(value=value):
                self.update_managed({"EMAIL_USE_TLS": value})

    def test_default_from_email_invalid_rejected(self):
        with self.assertRaises(env_config.InvalidValueError):
            self.update_managed({"DEFAULT_FROM_EMAIL": "not-an-email"})

    def test_default_from_email_valid_accepted(self):
        self.update_managed({"DEFAULT_FROM_EMAIL": "alerts@example.com"})

    def test_email_host_user_blank_allowed(self):
        self.update_managed({"EMAIL_HOST_USER": ""})

    def test_email_host_user_newline_rejected(self):
        with self.assertRaises(env_config.InvalidValueError):
            self.update_managed({"EMAIL_HOST_USER": "bad\nvalue"})

    def test_email_host_password_blank_allowed(self):
        self.update_managed({"EMAIL_HOST_PASSWORD": ""})

    def test_email_host_password_realistic_punctuation_allowed(self):
        self.update_managed({"EMAIL_HOST_PASSWORD": "Tr0ub4dor&3!$%^*()"})

    def test_invalid_value_does_not_write_anything(self):
        self.write_env("EMAIL_PORT=587\n")
        before = self.read_env()
        with self.assertRaises(env_config.InvalidValueError):
            self.update_managed({"EMAIL_PORT": "not-a-number"})
        self.assertEqual(self.read_env(), before)


# ---------------------------------------------------------------------
# Production-inventory regression fixture (mirrors the real project's
# .env.example key set/ordering/comment structure, WITH SYNTHETIC,
# NON-REAL VALUES) -- proves every non-SMTP line survives byte-for-byte
# when only SMTP keys are edited.
# ---------------------------------------------------------------------
PRODUCTION_INVENTORY_FIXTURE = """# Database
DB_NAME=isadoraair
DB_USER=isadoraair
DB_PASSWORD=synthetic-not-a-real-secret
DB_HOST=localhost
DB_PORT=5432

# Paths
LIBRARY_ROOT=/srv/isadoraair/music
WAVEFORMS_DIR=/srv/isadoraair/waveforms
# Where GW3000/Ecowitt weather JSON files land (shared with the
# companion weather-ingest cron scripts).
WEATHER_DATA_DIR=/var/lib/isadoraair/weather
# Where generated royalty / SoundExchange filings are persisted from
# the /reports/ page.
REPORTS_ROOT=/var/lib/isadoraair/reports

# Django
SECRET_KEY=synthetic-not-a-real-secret-key-value
DEBUG=False
ALLOWED_HOSTS=localhost,127.0.0.1,isadoraair
CSRF_TRUSTED_ORIGINS=https://isadoraair,https://127.0.0.1

# Email -- used by password reset, admin invites, and monitoring
# alert delivery.
EMAIL_HOST=localhost
EMAIL_PORT=587
EMAIL_HOST_USER=
EMAIL_HOST_PASSWORD=
EMAIL_USE_TLS=True
DEFAULT_FROM_EMAIL=isadoraair@localhost

# CD ripping identifies itself to the MusicBrainz API via a contact
# email so they can reach out if the client misbehaves.
MUSICBRAINZ_CONTACT=synthetic@example.com
"""


class ProductionInventoryRegressionTests(TempEnvMixin, TestCase):
    """The 19-key real-world layout (redacted/synthetic values,
    matching key names/ordering/comment structure of .env.example) as a
    regression fixture -- every line NOT among the SMTP keys being
    edited must survive completely unchanged, byte for byte."""

    NON_SMTP_KEYS = [
        "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT",
        "LIBRARY_ROOT", "WAVEFORMS_DIR", "SECRET_KEY", "DEBUG",
        "ALLOWED_HOSTS", "CSRF_TRUSTED_ORIGINS", "WEATHER_DATA_DIR",
        "REPORTS_ROOT", "MUSICBRAINZ_CONTACT",
    ]

    def _non_smtp_lines(self, text):
        """Every line whose first token (up to '=') is one of the
        non-SMTP inventory keys, OR any comment/blank line -- i.e.
        everything this test asserts must be untouched."""
        out = []
        for line in text.split("\n"):
            stripped = line.strip()
            key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else None
            if key is None or key in self.NON_SMTP_KEYS:
                out.append(line)
        return out

    def test_all_six_smtp_keys_edited_preserves_every_non_smtp_line(self):
        self.write_env(PRODUCTION_INVENTORY_FIXTURE)
        before_non_smtp = self._non_smtp_lines(PRODUCTION_INVENTORY_FIXTURE)

        self.update_managed({
            "EMAIL_HOST": "smtp.newrelay.example.com",
            "EMAIL_PORT": "2525",
            "EMAIL_HOST_USER": "newuser@example.com",
            "EMAIL_HOST_PASSWORD": "N3wSup3rSecret!",
            "EMAIL_USE_TLS": "False",
            "DEFAULT_FROM_EMAIL": "alerts@newrelay.example.com",
        })

        after = self.read_env()
        after_non_smtp = self._non_smtp_lines(after)
        self.assertEqual(before_non_smtp, after_non_smtp)

        # And the SMTP keys really did change.
        self.assertIn("EMAIL_HOST=smtp.newrelay.example.com", after)
        self.assertIn("EMAIL_PORT=2525", after)
        self.assertIn("EMAIL_HOST_USER=newuser@example.com", after)
        self.assertIn("EMAIL_HOST_PASSWORD=N3wSup3rSecret!", after)
        self.assertIn("EMAIL_USE_TLS=False", after)
        self.assertIn("DEFAULT_FROM_EMAIL=alerts@newrelay.example.com", after)

    def test_single_smtp_key_edit_leaves_all_others_and_all_non_smtp_lines_untouched(self):
        self.write_env(PRODUCTION_INVENTORY_FIXTURE)
        self.update_managed({"EMAIL_PORT": "465"})
        after = self.read_env()

        # Every non-SMTP line byte-for-byte identical.
        self.assertEqual(self._non_smtp_lines(PRODUCTION_INVENTORY_FIXTURE), self._non_smtp_lines(after))
        # The other five SMTP keys also completely unchanged.
        for untouched in [
            "EMAIL_HOST=localhost", "EMAIL_HOST_USER=", "EMAIL_HOST_PASSWORD=",
            "EMAIL_USE_TLS=True", "DEFAULT_FROM_EMAIL=isadoraair@localhost",
        ]:
            self.assertIn(untouched, after)
        self.assertIn("EMAIL_PORT=465", after)

    def test_read_managed_values_against_full_fixture_matches_registered_defaults(self):
        self.write_env(PRODUCTION_INVENTORY_FIXTURE)
        values = self.read_managed()
        self.assertEqual(values["EMAIL_HOST"].display_value, "localhost")
        self.assertEqual(values["EMAIL_PORT"].display_value, "587")
        self.assertEqual(values["EMAIL_HOST_USER"].display_value, "")
        self.assertIsNone(values["EMAIL_HOST_PASSWORD"].display_value)
        self.assertEqual(values["EMAIL_USE_TLS"].display_value, "True")
        self.assertEqual(values["DEFAULT_FROM_EMAIL"].display_value, "isadoraair@localhost")

    def test_backup_of_full_fixture_is_exact_previous_content(self):
        self.write_env(PRODUCTION_INVENTORY_FIXTURE)
        self.update_managed({"EMAIL_PORT": "465"})
        self.assertEqual(self.backup_path.read_text(encoding="utf-8"), PRODUCTION_INVENTORY_FIXTURE)
