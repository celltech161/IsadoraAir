"""P0 1.2 transactional database-credential rotation tests."""
from __future__ import annotations

import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import decouple
import psycopg2
from psycopg2 import sql
from django.test import SimpleTestCase

from deploy.updater_runtime.isadoraair_updater.config import DatabaseConfig, StationConfig
from isadoraair import db_credentials, env_config
from isadoraair.maintenance_lock import (
    MaintenanceLockError,
    database_maintenance_lock,
    database_rotation_is_pending,
    database_rotation_pending_path,
)
from monitoring.management.commands import rotate_database_credentials as command_mod


OLD = "old-password-OnlyForTests-32-characters"
NEW = "new-password_OnlyForTests-32-characters"
ESCAPED = r"new:password\\OnlyForTests-32-characters"


def _identity():
    return db_credentials.DatabaseIdentity("127.0.0.1", 5432, "isadoraair_test", "isadoraair_test")


class RenderingTests(SimpleTestCase):
    def test_env_targeted_update_preserves_every_unrelated_byte(self):
        original = b"# comment\nDB_NAME=x\nUNCHANGED = ' exact '  # tail\nDB_PASSWORD=old\nLAST=yes"
        rendered = env_config.render_database_password_update(original, "new-safe-password")
        self.assertEqual(
            rendered,
            b"# comment\nDB_NAME=x\nUNCHANGED = ' exact '  # tail\nDB_PASSWORD=new-safe-password\nLAST=yes",
        )

    def test_env_duplicate_and_missing_password_fail_closed(self):
        with self.assertRaises(env_config.DuplicateManagedKeyError):
            env_config.render_database_password_update(b"DB_PASSWORD=a\nDB_PASSWORD=b\n", "new")
        with self.assertRaises(env_config.EnvWriteError):
            env_config.render_database_password_update(b"DB_NAME=x\n", "new")

    def test_pgpass_password_escaping_and_unrelated_entries(self):
        identity = _identity()
        original = (
            b"# retained\nother:5432:db:user:untouched\n"
            b"127.0.0.1:5432:isadoraair_test:isadoraair_test:old\n"
        )
        rendered = db_credentials.render_pgpass_password_update(original, identity, ESCAPED)
        self.assertIn(b"other:5432:db:user:untouched\n", rendered)
        self.assertEqual(db_credentials.read_pgpass_password(rendered, identity), ESCAPED)

    def test_pgpass_duplicate_exact_or_wildcard_matches_fail_closed(self):
        identity = _identity()
        raw = (
            b"*:5432:isadoraair_test:isadoraair_test:first\n"
            b"127.0.0.1:5432:isadoraair_test:isadoraair_test:second\n"
        )
        with self.assertRaises(db_credentials.CredentialPreflightError):
            db_credentials.read_pgpass_password(raw, identity)
        with self.assertRaises(db_credentials.CredentialPreflightError):
            db_credentials.render_pgpass_password_update(raw, identity, "new")

    def test_generated_password_is_high_entropy_and_format_safe(self):
        value = db_credentials.generate_password()
        self.assertEqual(len(value), 48)
        self.assertTrue(set(value) <= set(db_credentials.SAFE_PASSWORD_ALPHABET))
        self.assertEqual(env_config.encode_env_value(value), value)
        self.assertEqual(db_credentials._escape_pgpass_password(value), value)

    def test_operator_password_policy_fails_before_mutation(self):
        with self.assertRaises(db_credentials.CredentialPreflightError):
            db_credentials.validate_new_password("too-short")
        with self.assertRaises(db_credentials.CredentialPreflightError):
            db_credentials.validate_new_password(" " + "x" * 32 + "'\"\\")
        with self.assertRaises(db_credentials.CredentialPreflightError):
            db_credentials.validate_new_password(ESCAPED)

    def test_every_supported_character_is_raw_env_and_backup_safe(self):
        for character in db_credentials.SAFE_PASSWORD_ALPHABET:
            with self.subTest(character=character):
                value = character * 32
                db_credentials.validate_new_password(value)
                self.assertEqual(env_config.encode_env_value(value), value)
                self.assertEqual(db_credentials._escape_pgpass_password(value), value)


class AtomicAndLockTests(SimpleTestCase):
    def test_atomic_writer_preserves_requested_policy_and_leaves_no_temp(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / ".env"
            target.write_bytes(b"old")
            account = pwd.getpwuid(os.geteuid())
            env_config._atomic_write_bytes(
                target, b"new", 0o600, uid=account.pw_uid, gid=account.pw_gid,
            )
            info = target.stat()
            self.assertEqual(target.read_bytes(), b"new")
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
            self.assertEqual((info.st_uid, info.st_gid), (account.pw_uid, account.pw_gid))
            self.assertEqual(list(root.glob(".*.tmp.*")), [])

    def test_exclusive_lock_rejects_contending_shared_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = Path(temporary) / ".env"
            env.write_text("DB_PASSWORD=x\n", encoding="utf-8")
            os.chmod(env, 0o600)
            with database_maintenance_lock(env, shared=False, timeout=0.1):
                with self.assertRaises(MaintenanceLockError):
                    with database_maintenance_lock(env, shared=True, timeout=0.05):
                        pass

    def test_lock_file_is_0600_and_matches_environment_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = Path(temporary) / ".env"
            env.write_text("DB_PASSWORD=x\n", encoding="utf-8")
            os.chmod(env, 0o600)
            with database_maintenance_lock(env, shared=True, timeout=0.1) as lock:
                env_info, lock_info = env.stat(), lock.stat()
                self.assertEqual(stat.S_IMODE(lock_info.st_mode), 0o600)
                self.assertEqual((lock_info.st_uid, lock_info.st_gid), (env_info.st_uid, env_info.st_gid))

    def test_nonsecret_pending_marker_is_a_fail_closed_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = Path(temporary) / ".env"
            env.write_text("DB_PASSWORD=x\n", encoding="utf-8")
            self.assertFalse(database_rotation_is_pending(env))
            marker = database_rotation_pending_path(env)
            marker.write_text("database credential recovery pending\n", encoding="utf-8")
            self.assertTrue(database_rotation_is_pending(env))

    def test_pending_marker_blocks_ordinary_environment_admin_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = root / ".env"
            env.write_text("EMAIL_HOST=smtp.old.example\n", encoding="utf-8")
            database_rotation_pending_path(env).write_text("pending\n", encoding="utf-8")
            with self.assertRaisesMessage(env_config.EnvWriteError, "recovery is pending"):
                env_config.update_managed_values(
                    {"EMAIL_HOST": "smtp.new.example"}, env_path=env,
                )
            self.assertEqual(env.read_text(encoding="utf-8"), "EMAIL_HOST=smtp.old.example\n")


class CommandSecretInputTests(SimpleTestCase):
    def test_prompt_fails_closed_without_interactive_terminal(self):
        stream = mock.Mock()
        stream.isatty.return_value = False
        with mock.patch.object(command_mod.sys, "stdin", stream):
            with self.assertRaisesMessage(command_mod.CommandError, "interactive terminal"):
                command_mod.Command._prompt_password()

    def test_incomplete_rollback_event_is_static_redacted_and_names_phase(self):
        error = db_credentials.CredentialRollbackError(
            "pg_dump_validation", ["PostgreSQL role"],
        )
        with mock.patch("monitoring.models.emit_event") as emit:
            command_mod.Command._emit_incomplete_rollback_event(error)
        detail = emit.call_args.kwargs["detail"]
        self.assertIn("pg_dump_validation", detail)
        self.assertIn("PostgreSQL role", detail)
        self.assertNotIn(OLD, detail)
        self.assertNotIn(NEW, detail)


class _FakeCursor:
    def __init__(self, authority, user):
        self.authority = authority
        self.user = user
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        if params:
            self.authority["password"] = params[0]
        else:
            self.result = (self.user,)

    def fetchone(self):
        return self.result


class _FakeConnection:
    def __init__(self, authority, user):
        self.authority = authority
        self.user = user
        self.closed = False

    def cursor(self):
        return _FakeCursor(self.authority, self.user)

    def close(self):
        self.closed = True


class _LoggingFakeCursor:
    """Minimal cursor double that answers exactly the SET/RESET and
    current_setting()/pg_settings-existence statements
    _guarded_mutation_session() issues, the way a real, correctly
    suppressed PostgreSQL session would -- used by tests that exercise the
    *real* _alter_role() (and therefore its real logging-suppression guard)
    without a live server. `deny` names GUCs whose SET must fail (modeling
    a role lacking `GRANT SET ON PARAMETER ...`); `bad_readback` maps a GUC
    to a wrong current_setting() value it should return instead of the
    safe one.

    _alter_role()'s verifier-bearing ALTER ROLE is issued as a
    psycopg2.sql.Composed object (from sql.SQL(...).format(sql.Identifier(...))),
    not a plain string -- it can only be rendered into real SQL text against
    a genuine psycopg2 connection, so this fake does not attempt to parse
    it. Any non-string `query` is recorded verbatim as `self.altered`
    (query, params) instead, for structural assertions (see
    MutationLoggingGuardTests) that the role name arrived as a
    sql.Identifier and the params carried only the (mocked) verifier, never
    the cleartext password."""

    _SAFE = {
        "log_statement": "none",
        "log_min_error_statement": "panic",
        "log_min_duration_statement": "-1",
        "log_min_duration_sample": "-1",
        "log_transaction_sample_rate": "0",
        db_credentials._PGAUDIT_LOG_GUC: "none",
    }

    def __init__(self, *, deny=frozenset(), bad_readback=None, pgaudit_present=False):
        self.deny = set(deny)
        self.bad_readback = dict(bad_readback or {})
        self.pgaudit_present = pgaudit_present
        self.set_calls = []
        self.reset_calls = []
        self.altered = None
        self._pending_setting = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        if not isinstance(query, str):
            self.altered = (query, params)
            return
        text = " ".join(query.split())
        upper = text.upper()
        if upper.startswith("SET "):
            guc = text.split()[1]
            if guc in self.deny:
                raise RuntimeError(f"permission denied to set parameter {guc!r}")
            self.set_calls.append(guc)
        elif upper.startswith("RESET "):
            self.reset_calls.append(text.split()[1])
        elif "current_setting" in text:
            self._pending_setting = params[0]
        elif "pg_settings" in text:
            self._pending_setting = "__pgaudit_probe__"
        else:
            raise AssertionError(f"unexpected statement issued by the logging guard: {query!r}")

    def fetchone(self):
        guc = self._pending_setting
        if guc == "__pgaudit_probe__":
            return (1,) if self.pgaudit_present else None
        if guc in self.bad_readback:
            return (self.bad_readback[guc],)
        return (self._SAFE.get(guc, "none"),)

    def close(self):
        pass


class _LoggingFakeConnection:
    def __init__(self, **cursor_kwargs):
        self.fake_cursor = _LoggingFakeCursor(**cursor_kwargs)

    def cursor(self):
        return self.fake_cursor


FAKE_VERIFIER = "SCRAM-SHA-256$4096:ZmFrZS1zYWx0$ZmFrZS1zdG9yZWQ6ZmFrZS1zZXJ2ZXI="


class MutationLoggingGuardTests(SimpleTestCase):
    """_guarded_mutation_session() / _alter_role() -- the r0059 Pass G
    re-review's remaining release blocker: PostgreSQL statement/error
    logging (and pgAudit, where present) must never be able to render the
    client-generated SCRAM verifier -- or the timeout/permission failure
    of the attempt to suppress it -- into server-side logs. These are
    real-server-free contract tests against _LoggingFakeConnection, with
    psycopg2.extensions.encrypt_password() itself mocked out (it is a C
    function backed by the real libpq and genuinely requires a real
    connection as `scope` -- these tests exist specifically to prove the
    *guard sequencing and cleartext-handling contract* around it without a
    live server; the real disposable-PostgreSQL proof, exercising the real
    encrypt_password() end to end, lives in
    RealPostgreSQLMutationLoggingIntegrationTests below)."""

    def _rotator(self, verifier=FAKE_VERIFIER):
        rotator = db_credentials.CredentialRotator(
            require_root=False, enforce_config_protection=False, enforce_live_root=False,
        )
        self._patcher = mock.patch.object(db_credentials.extensions, "encrypt_password", return_value=verifier)
        self._encrypt_password = self._patcher.start()
        self.addCleanup(self._patcher.stop)
        return rotator

    def test_happy_path_suppresses_verifies_and_resets_every_guc(self):
        rotator = self._rotator()
        connection = _LoggingFakeConnection()
        rotator._alter_role(connection, "isadoraair_test", NEW)
        self._encrypt_password.assert_called_once_with(
            NEW, "isadoraair_test", scope=connection, algorithm="scram-sha-256",
        )
        cursor = connection.fake_cursor
        for guc, _literal in db_credentials._SENSITIVE_LOGGING_GUCS:
            self.assertIn(guc, cursor.set_calls)
            self.assertIn(guc, cursor.reset_calls)
        self.assertIn("statement_timeout", cursor.set_calls)
        self.assertIn("lock_timeout", cursor.set_calls)
        # No pgAudit GUC on this fake server -- nothing suppressed, nothing reset.
        self.assertNotIn(db_credentials._PGAUDIT_LOG_GUC, cursor.set_calls)

    def test_verifier_bearing_sql_uses_identifier_and_never_the_cleartext_password(self):
        rotator = self._rotator()
        connection = _LoggingFakeConnection()
        rotator._alter_role(connection, "isadoraair_test", NEW)
        composed, params = connection.fake_cursor.altered
        # The role name must be a real SQL identifier, never interpolated
        # into the statement text.
        identifiers = [part for part in composed.seq if isinstance(part, db_credentials.sql.Identifier)]
        self.assertEqual(len(identifiers), 1)
        self.assertEqual(identifiers[0].strings, ("isadoraair_test",))
        # The statement text itself (the SQL literal parts) must never
        # contain the cleartext password -- only the mocked verifier may
        # travel as a bound parameter.
        literal_text = "".join(part.string for part in composed.seq if isinstance(part, db_credentials.sql.SQL))
        self.assertNotIn(NEW, literal_text)
        self.assertEqual(params, (FAKE_VERIFIER,))
        self.assertNotIn(NEW, params)

    def test_denied_set_on_any_sensitive_guc_aborts_before_mutation(self):
        for guc, _literal in db_credentials._SENSITIVE_LOGGING_GUCS:
            with self.subTest(guc=guc):
                rotator = self._rotator()
                connection = _LoggingFakeConnection(deny={guc})
                with self.assertRaises(db_credentials.CredentialLoggingSuppressionError) as caught:
                    rotator._alter_role(connection, "isadoraair_test", NEW)
                self.assertIn(guc, str(caught.exception))
                self._encrypt_password.assert_not_called()
                self.assertIsNone(connection.fake_cursor.altered)
                self.assertNotIn(NEW, str(caught.exception))

    def test_bad_readback_on_any_sensitive_guc_aborts_before_mutation(self):
        for guc, _literal in db_credentials._SENSITIVE_LOGGING_GUCS:
            with self.subTest(guc=guc):
                rotator = self._rotator()
                connection = _LoggingFakeConnection(bad_readback={guc: "all"})
                with self.assertRaises(db_credentials.CredentialLoggingSuppressionError) as caught:
                    rotator._alter_role(connection, "isadoraair_test", NEW)
                self.assertIn(guc, str(caught.exception))
                self._encrypt_password.assert_not_called()
                self.assertIsNone(connection.fake_cursor.altered)

    def test_active_pgaudit_is_suppressed_and_verified_when_settable(self):
        rotator = self._rotator()
        connection = _LoggingFakeConnection(pgaudit_present=True)
        rotator._alter_role(connection, "isadoraair_test", NEW)
        self._encrypt_password.assert_called_once()
        self.assertIn(db_credentials._PGAUDIT_LOG_GUC, connection.fake_cursor.set_calls)
        self.assertIn(db_credentials._PGAUDIT_LOG_GUC, connection.fake_cursor.reset_calls)

    def test_active_pgaudit_that_cannot_be_suppressed_aborts_before_mutation(self):
        rotator = self._rotator()
        connection = _LoggingFakeConnection(pgaudit_present=True, deny={db_credentials._PGAUDIT_LOG_GUC})
        with self.assertRaises(db_credentials.CredentialLoggingSuppressionError) as caught:
            rotator._alter_role(connection, "isadoraair_test", NEW)
        self.assertIn("pgAudit", str(caught.exception))
        self._encrypt_password.assert_not_called()
        self.assertIsNone(connection.fake_cursor.altered)

    def test_active_pgaudit_with_bad_readback_aborts_before_mutation(self):
        rotator = self._rotator()
        connection = _LoggingFakeConnection(
            pgaudit_present=True, bad_readback={db_credentials._PGAUDIT_LOG_GUC: "role, ddl"},
        )
        with self.assertRaises(db_credentials.CredentialLoggingSuppressionError) as caught:
            rotator._alter_role(connection, "isadoraair_test", NEW)
        self.assertIn("pgAudit", str(caught.exception))
        self._encrypt_password.assert_not_called()
        self.assertIsNone(connection.fake_cursor.altered)

    def test_only_parameters_actually_set_are_reset_on_early_failure(self):
        # log_min_duration_statement is the third GUC in the fixed order --
        # confirms cleanup is exactly "what was applied", not "everything".
        rotator = self._rotator()
        connection = _LoggingFakeConnection(deny={"log_min_duration_statement"})
        with self.assertRaises(db_credentials.CredentialLoggingSuppressionError):
            rotator._alter_role(connection, "isadoraair_test", NEW)
        cursor = connection.fake_cursor
        self.assertEqual(set(cursor.set_calls), {"statement_timeout", "lock_timeout", "log_statement", "log_min_error_statement"})
        self.assertEqual(set(cursor.reset_calls), set(cursor.set_calls))

    def test_operation_timeout_is_best_effort_never_fail_closed(self):
        rotator = self._rotator()
        connection = _LoggingFakeConnection(deny={"statement_timeout", "lock_timeout"})
        # Must not raise -- these are not security-sensitive.
        rotator._alter_role(connection, "isadoraair_test", NEW)
        self._encrypt_password.assert_called_once()
        self.assertNotIn("statement_timeout", connection.fake_cursor.reset_calls)
        self.assertNotIn("lock_timeout", connection.fake_cursor.reset_calls)

    def test_no_cleartext_or_verifier_in_any_raised_message(self):
        rotator = self._rotator()
        connection = _LoggingFakeConnection(deny={"log_statement"})
        with self.assertRaises(db_credentials.CredentialLoggingSuppressionError) as caught:
            rotator._alter_role(connection, "isadoraair_test", NEW)
        self.assertNotIn(NEW, str(caught.exception))
        self.assertNotIn("SCRAM-SHA-256$", str(caught.exception))

    def test_forward_and_rollback_directions_use_the_same_guarded_path(self):
        """The previous review specifically required both directions to be
        safe through one shared implementation -- exercise _alter_role()
        twice on the same connection, forward then back, exactly as
        rotate() and _rollback() each independently do."""
        rotator = self._rotator()
        connection = _LoggingFakeConnection()
        rotator._alter_role(connection, "isadoraair_test", NEW)
        forward_composed, forward_params = connection.fake_cursor.altered
        rotator._alter_role(connection, "isadoraair_test", OLD)
        rollback_composed, rollback_params = connection.fake_cursor.altered
        self.assertEqual(self._encrypt_password.call_count, 2)
        self.assertEqual(
            self._encrypt_password.call_args_list[0].args[0], NEW,
        )
        self.assertEqual(
            self._encrypt_password.call_args_list[1].args[0], OLD,
        )
        # Both calls produced verifier-bearing SQL (not cleartext), on the
        # exact same guarded connection.
        for params in (forward_params, rollback_params):
            self.assertNotIn(NEW, params)
            self.assertNotIn(OLD, params)


class TransactionTests(SimpleTestCase):
    phases = (
        "before_database_change",
        "after_database_change",
        "environment_staging",
        "environment_atomic_replace",
        "pgpass_staging",
        "pgpass_atomic_replace",
        "django_validation",
        "psql_validation",
        "pg_dump_validation",
    )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env = self.root / ".env"
        self.pgpass = self.root / ".pgpass"
        self.jobs = self.root / "jobs"
        self.jobs.mkdir()
        self.identity = _identity()
        self.env.write_text(
            "SECRET_KEY=test-only\nDEBUG=True\n"
            f"DB_HOST={self.identity.host}\nDB_PORT={self.identity.port}\n"
            f"DB_NAME={self.identity.name}\nDB_USER={self.identity.user}\nDB_PASSWORD={OLD}\n",
            encoding="utf-8",
        )
        self.pgpass.write_text(
            f"{self.identity.host}:{self.identity.port}:{self.identity.name}:{self.identity.user}:{OLD}\n",
            encoding="utf-8",
        )
        os.chmod(self.env, 0o600)
        os.chmod(self.pgpass, 0o600)
        account = pwd.getpwuid(os.geteuid())
        self.station = StationConfig(
            trusted_repository_url="https://example.invalid/repo.git",
            trusted_branch="main",
            application_root=self.root,
            application_user=account.pw_name,
            application_group=account.pw_name,
            application_environment_file=self.env,
            trusted_repository=self.root / "trusted",
            jobs_root=self.jobs,
            logs_root=self.root / "logs",
            staging_root=self.root / "staging",
            checkpoint_root=self.root / "checkpoints",
            socket_path=self.root / "run/updater.sock",
            systemd_unit_root=self.root / "systemd",
            render_values={},
            database=DatabaseConfig(
                self.identity.name, self.identity.user, self.identity.host,
                self.identity.port, self.pgpass,
            ),
            gunicorn_health_url="http://127.0.0.1:8000/",
            update_execution_enabled=True,
            operator_restart_units=(),
            phase_d_supervisor_activation_socket=None,
            phase_d_supervisor_slots_root=None,
            phase_d_trust_policy_path=None,
            phase_d_signer_root=None,
        )
        self.authority = {"password": OLD}
        self.progress = []

    def tearDown(self):
        self.temporary.cleanup()

    def _disk_passwords(self):
        env_values = decouple.RepositoryEnv(str(self.env)).data
        pgpass = db_credentials.read_pgpass_password(self.pgpass.read_bytes(), self.identity)
        return env_values["DB_PASSWORD"], pgpass

    def _rotator(self, failure_phase=None):
        def inject(phase):
            if phase == failure_phase:
                raise RuntimeError("injected secret-like detail must never escape")

        rotator = db_credentials.CredentialRotator(
            require_root=False,
            enforce_config_protection=False,
            enforce_live_root=False,
            failure_injector=inject,
            progress=self.progress.append,
        )
        rotator._station = lambda: self.station
        # Fake transaction authority stores the plain test value. The real
        # integration test below exercises libpq SCRAM verifier generation.
        rotator._password_verifier = lambda _connection, _user, password: password

        def connect(identity, password):
            if password != self.authority["password"]:
                raise db_credentials.CredentialPreflightError("PostgreSQL authentication failed")
            return _FakeConnection(self.authority, identity.user)

        rotator._connect = connect

        def alter_role(connection, _user, password):
            connection.authority["password"] = password

        # Transaction unit tests model the role authority in memory. The real
        # PostgreSQL integration below exercises psycopg2.extensions.
        # encrypt_password() and the guarded verifier-bearing ALTER ROLE.
        rotator._alter_role = alter_role

        def validate_paths(station, identity):
            env_password, pgpass_password = self._disk_passwords()
            if not (env_password == pgpass_password == self.authority["password"]):
                raise db_credentials.CredentialRotationError("credential path mismatch")

        rotator._validate_paths = validate_paths
        rotator._django_probe = lambda station: self._disk_passwords()[0] == self.authority["password"]
        rotator._psql_probe = lambda station, identity: self._disk_passwords()[1] == self.authority["password"]
        rotator._pg_dump_probe = lambda station, identity: self._disk_passwords()[1] == self.authority["password"]
        return rotator

    def _settings(self):
        return {
            "default": {
                "HOST": self.identity.host,
                "PORT": str(self.identity.port),
                "NAME": self.identity.name,
                "USER": self.identity.user,
                "PASSWORD": OLD,
            }
        }

    def test_success_commits_all_three_authorities(self):
        rotator = self._rotator()
        with mock.patch.object(db_credentials.settings, "DATABASES", self._settings()):
            rotator.rotate(NEW)
        self.assertEqual((self.authority["password"], *self._disk_passwords()), (NEW, NEW, NEW))
        self.assertIn("rotation committed", self.progress)
        self.assertFalse(any(self.root.glob(".db-credential-pgdump-*")))
        self.assertFalse((self.jobs / db_credentials.RECOVERY_RECORD_NAME).exists())
        self.assertFalse(database_rotation_pending_path(self.env).exists())

    def test_every_failure_boundary_is_fully_old_not_mixed(self):
        for phase in self.phases:
            with self.subTest(phase=phase):
                self.authority["password"] = OLD
                self.env.write_text(
                    "SECRET_KEY=test-only\nDEBUG=True\n"
                    f"DB_HOST={self.identity.host}\nDB_PORT={self.identity.port}\n"
                    f"DB_NAME={self.identity.name}\nDB_USER={self.identity.user}\nDB_PASSWORD={OLD}\n",
                    encoding="utf-8",
                )
                self.pgpass.write_text(
                    f"{self.identity.host}:{self.identity.port}:{self.identity.name}:{self.identity.user}:{OLD}\n",
                    encoding="utf-8",
                )
                os.chmod(self.env, 0o600)
                os.chmod(self.pgpass, 0o600)
                rotator = self._rotator(phase)
                with mock.patch.object(db_credentials.settings, "DATABASES", self._settings()):
                    with self.assertRaises(db_credentials.CredentialRotationError) as caught:
                        rotator.rotate(NEW)
                self.assertNotIn(OLD, str(caught.exception))
                self.assertNotIn(NEW, str(caught.exception))
                self.assertNotIn("secret-like", str(caught.exception))
                self.assertEqual((self.authority["password"], *self._disk_passwords()), (OLD, OLD, OLD))
                self.assertFalse((self.jobs / db_credentials.RECOVERY_RECORD_NAME).exists())
                self.assertFalse(database_rotation_pending_path(self.env).exists())

    def test_inconsistent_preflight_never_mutates(self):
        self.pgpass.write_text(
            f"{self.identity.host}:{self.identity.port}:{self.identity.name}:{self.identity.user}:wrong\n",
            encoding="utf-8",
        )
        os.chmod(self.pgpass, 0o600)
        rotator = self._rotator()
        with mock.patch.object(db_credentials.settings, "DATABASES", self._settings()):
            with self.assertRaises(db_credentials.CredentialPreflightError):
                rotator.rotate(NEW)
        self.assertEqual(self.authority["password"], OLD)

    def test_active_protected_updater_job_blocks_preflight(self):
        job_id = "00000000-0000-0000-0000-000000000001"
        job = self.jobs / f"{job_id}.json"
        job.write_text(
            '{"schema_version":1,"job_id":"' + job_id + '","state":"running"}',
            encoding="utf-8",
        )
        os.chmod(job, 0o600)
        rotator = self._rotator()
        with mock.patch.object(db_credentials.settings, "DATABASES", self._settings()):
            with self.assertRaisesMessage(
                db_credentials.CredentialPreflightError, "Update Center installation is active",
            ):
                rotator.rotate(NEW)

    def test_actual_atomic_writer_failure_rolls_back(self):
        rotator = self._rotator()
        real_write = rotator._write_snapshot
        calls = {"count": 0}

        def fail_pgpass(snapshot, data):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("injected write error")
            return real_write(snapshot, data)

        rotator._write_snapshot = fail_pgpass
        with mock.patch.object(db_credentials.settings, "DATABASES", self._settings()):
            with self.assertRaises(db_credentials.CredentialRotationError):
                rotator.rotate(NEW)
        self.assertEqual((self.authority["password"], *self._disk_passwords()), (OLD, OLD, OLD))

    def test_strict_directory_fsync_failure_prevents_database_mutation(self):
        rotator = self._rotator()
        with mock.patch.object(db_credentials.settings, "DATABASES", self._settings()):
            with mock.patch.object(
                db_credentials, "_fsync_directory_strict", side_effect=OSError("fsync failed"),
            ):
                with self.assertRaises(db_credentials.CredentialRotationError):
                    rotator.rotate(NEW)
        self.assertEqual((self.authority["password"], *self._disk_passwords()), (OLD, OLD, OLD))
        self.assertFalse((self.jobs / db_credentials.RECOVERY_RECORD_NAME).exists())
        self.assertFalse(database_rotation_pending_path(self.env).exists())

    def test_credential_rename_fsync_failure_enters_full_rollback(self):
        rotator = self._rotator()
        real_fsync = db_credentials._fsync_directory_strict
        env_directory_calls = {"count": 0}

        def fail_first_post_mutation_env_fsync(directory):
            if Path(directory) == self.env.parent:
                env_directory_calls["count"] += 1
                # Call 1 persists the public marker. Call 2 follows the
                # application .env replace and must abort the transaction.
                if env_directory_calls["count"] == 2:
                    raise OSError("credential directory fsync failed")
            return real_fsync(directory)

        with mock.patch.object(db_credentials.settings, "DATABASES", self._settings()):
            with mock.patch.object(
                db_credentials, "_fsync_directory_strict", side_effect=fail_first_post_mutation_env_fsync,
            ):
                with self.assertRaises(db_credentials.CredentialRotationError):
                    rotator.rotate(NEW)
        self.assertEqual((self.authority["password"], *self._disk_passwords()), (OLD, OLD, OLD))
        self.assertFalse((self.jobs / db_credentials.RECOVERY_RECORD_NAME).exists())
        self.assertFalse(database_rotation_pending_path(self.env).exists())

    def test_persistent_pgpass_write_failure_does_not_rewrite_unchanged_old_pgpass(self):
        rotator = self._rotator()
        real_write = rotator._write_snapshot

        def always_fail_pgpass(snapshot, data):
            if snapshot.path == self.pgpass:
                raise OSError("persistent pgpass failure")
            return real_write(snapshot, data)

        rotator._write_snapshot = always_fail_pgpass
        with mock.patch.object(db_credentials.settings, "DATABASES", self._settings()):
            with self.assertRaises(db_credentials.CredentialRotationError):
                rotator.rotate(NEW)
        self.assertEqual((self.authority["password"], *self._disk_passwords()), (OLD, OLD, OLD))
        self.assertFalse((self.jobs / db_credentials.RECOVERY_RECORD_NAME).exists())
        self.assertFalse(database_rotation_pending_path(self.env).exists())

    def test_commit_marker_cleanup_failure_keeps_valid_new_state_gated(self):
        rotator = self._rotator()
        real_remove_marker = rotator._remove_pending_marker
        rotator._remove_pending_marker = mock.Mock(side_effect=OSError("marker unlink failed"))
        with mock.patch.object(db_credentials.settings, "DATABASES", self._settings()):
            with self.assertRaisesMessage(
                db_credentials.CredentialRotationError, "durable cleanup is incomplete",
            ):
                rotator.rotate(NEW)
        self.assertEqual((self.authority["password"], *self._disk_passwords()), (NEW, NEW, NEW))
        self.assertFalse((self.jobs / db_credentials.RECOVERY_RECORD_NAME).exists())
        self.assertTrue(database_rotation_pending_path(self.env).exists())

        rotator._remove_pending_marker = real_remove_marker
        new_settings = self._settings()
        new_settings["default"]["PASSWORD"] = NEW
        with mock.patch.object(db_credentials.settings, "DATABASES", new_settings):
            rotator.preflight()
        self.assertFalse(database_rotation_pending_path(self.env).exists())

    def test_commit_record_cleanup_failure_recovers_to_old_state(self):
        rotator = self._rotator()
        real_remove_record = rotator._remove_recovery_record
        rotator._remove_recovery_record = mock.Mock(side_effect=OSError("record unlink failed"))
        with mock.patch.object(db_credentials.settings, "DATABASES", self._settings()):
            with self.assertRaisesMessage(
                db_credentials.CredentialRotationError, "durable cleanup is incomplete",
            ):
                rotator.rotate(NEW)
        self.assertEqual((self.authority["password"], *self._disk_passwords()), (NEW, NEW, NEW))
        self.assertTrue((self.jobs / db_credentials.RECOVERY_RECORD_NAME).exists())
        self.assertTrue(database_rotation_pending_path(self.env).exists())

        rotator._remove_recovery_record = real_remove_record
        new_settings = self._settings()
        new_settings["default"]["PASSWORD"] = NEW
        with mock.patch.object(db_credentials.settings, "DATABASES", new_settings):
            with self.assertRaisesMessage(
                db_credentials.CredentialPreflightError, "pending credential transaction was restored",
            ):
                rotator.preflight()
        self.assertEqual((self.authority["password"], *self._disk_passwords()), (OLD, OLD, OLD))
        self.assertFalse((self.jobs / db_credentials.RECOVERY_RECORD_NAME).exists())
        self.assertFalse(database_rotation_pending_path(self.env).exists())

    def test_inconsistent_marker_only_state_remains_gated(self):
        database_rotation_pending_path(self.env).write_text("pending\n", encoding="utf-8")
        self.pgpass.write_text(
            f"{self.identity.host}:{self.identity.port}:{self.identity.name}:{self.identity.user}:wrong\n",
            encoding="utf-8",
        )
        os.chmod(self.pgpass, 0o600)
        rotator = self._rotator()
        with mock.patch.object(db_credentials.settings, "DATABASES", self._settings()):
            with self.assertRaises(db_credentials.CredentialPreflightError):
                rotator.preflight()
        self.assertTrue(database_rotation_pending_path(self.env).exists())

    def test_keyboard_interrupt_after_database_change_rolls_back_under_guard(self):
        rotator = self._rotator()

        def interrupt(phase):
            if phase == "after_database_change":
                raise KeyboardInterrupt()

        rotator.failure_injector = interrupt
        with mock.patch.object(db_credentials.settings, "DATABASES", self._settings()):
            with self.assertRaises(db_credentials.CredentialRotationError):
                rotator.rotate(NEW)
        self.assertEqual((self.authority["password"], *self._disk_passwords()), (OLD, OLD, OLD))
        self.assertFalse((self.jobs / db_credentials.RECOVERY_RECORD_NAME).exists())
        self.assertFalse(database_rotation_pending_path(self.env).exists())

    def test_signal_arriving_during_nonsignal_rollback_is_deferred(self):
        rotator = self._rotator("after_database_change")
        real_rollback = rotator._rollback

        def signal_then_rollback(state, password, phase):
            os.kill(os.getpid(), db_credentials.signal.SIGTERM)
            return real_rollback(state, password, phase)

        rotator._rollback = signal_then_rollback
        with mock.patch.object(db_credentials.settings, "DATABASES", self._settings()):
            with self.assertRaises(db_credentials.CredentialRotationError):
                rotator.rotate(NEW)
        self.assertEqual((self.authority["password"], *self._disk_passwords()), (OLD, OLD, OLD))
        self.assertFalse((self.jobs / db_credentials.RECOVERY_RECORD_NAME).exists())
        self.assertFalse(database_rotation_pending_path(self.env).exists())

    def test_durable_record_recovers_split_state_after_process_loss(self):
        rotator = self._rotator()
        with mock.patch.object(db_credentials.settings, "DATABASES", self._settings()):
            state = rotator._preflight_under_lock(self.station)
            rotator._write_recovery_record(state, NEW)
            state.rollback_connection.close()
            recovery_path = self.jobs / db_credentials.RECOVERY_RECORD_NAME
            self.assertEqual(stat.S_IMODE(recovery_path.stat().st_mode), 0o600)

            self.authority["password"] = NEW
            self.env.write_bytes(env_config.render_database_password_update(self.env.read_bytes(), NEW))

            self.assertTrue(rotator._recover_pending_under_lock(self.station))

        self.assertEqual((self.authority["password"], *self._disk_passwords()), (OLD, OLD, OLD))
        self.assertFalse((self.jobs / db_credentials.RECOVERY_RECORD_NAME).exists())
        self.assertFalse(database_rotation_pending_path(self.env).exists())

    def test_role_change_uses_psycopg2_encrypt_password_not_rendered_cleartext_sql(self):
        rotator = db_credentials.CredentialRotator(
            require_root=False, enforce_config_protection=False, enforce_live_root=False,
        )
        # A real connection double, not a bare Mock: _alter_role's logging-
        # suppression guard (see MutationLoggingGuardTests) issues genuine
        # SET/current_setting statements on it before a verifier is ever
        # generated. psycopg2.extensions.encrypt_password() is itself a C
        # function requiring a real connection as `scope`, so it is mocked
        # here -- the real disposable-PostgreSQL tests exercise the actual
        # function end to end.
        connection = _LoggingFakeConnection()

        with mock.patch.object(
            db_credentials.extensions, "encrypt_password", return_value=FAKE_VERIFIER,
        ) as encrypt_password:
            rotator._alter_role(connection, self.identity.user, NEW)

        encrypt_password.assert_called_once_with(
            NEW, self.identity.user, scope=connection, algorithm="scram-sha-256",
        )
        composed, params = connection.fake_cursor.altered
        identifiers = [part for part in composed.seq if isinstance(part, db_credentials.sql.Identifier)]
        self.assertEqual(identifiers, [db_credentials.sql.Identifier(self.identity.user)])
        self.assertEqual(params, (FAKE_VERIFIER,))
        self.assertNotIn(NEW, params)
        # The guard ran (and cleaned up after itself) around the real call.
        for guc, _literal in db_credentials._SENSITIVE_LOGGING_GUCS:
            self.assertIn(guc, connection.fake_cursor.set_calls)
            self.assertIn(guc, connection.fake_cursor.reset_calls)

    def test_ambiguous_alter_role_response_is_treated_as_mutated_and_rolled_back(self):
        rotator = self._rotator()
        real_alter = rotator._alter_role
        calls = {"count": 0}

        def apply_then_lose_response(connection, user, password):
            calls["count"] += 1
            real_alter(connection, user, password)
            if calls["count"] == 1:
                raise db_credentials.CredentialRotationError("indeterminate transport boundary")

        rotator._alter_role = apply_then_lose_response
        with mock.patch.object(db_credentials.settings, "DATABASES", self._settings()):
            with self.assertRaises(db_credentials.CredentialRotationError):
                rotator.rotate(NEW)
        self.assertEqual((self.authority["password"], *self._disk_passwords()), (OLD, OLD, OLD))

    def test_incomplete_rollback_names_authority_without_secrets(self):
        rotator = self._rotator("after_database_change")
        real_alter = rotator._alter_role
        calls = {"count": 0}

        def fail_both_rollback_paths(connection, user, password):
            calls["count"] += 1
            if calls["count"] >= 2:
                raise db_credentials.CredentialRotationError("static failure")
            return real_alter(connection, user, password)

        rotator._alter_role = fail_both_rollback_paths
        with mock.patch.object(db_credentials.settings, "DATABASES", self._settings()):
            with self.assertRaises(db_credentials.CredentialRollbackError) as caught:
                rotator.rotate(NEW)
        self.assertIn("PostgreSQL role", caught.exception.failed_authorities)
        self.assertIn("after_database_change", str(caught.exception))
        self.assertNotIn(OLD, str(caught.exception))
        self.assertNotIn(NEW, str(caught.exception))

    def test_live_rotation_requires_all_persistent_consumers_inactive(self):
        rotator = self._rotator()
        rotator.enforce_live_root = True
        loaded = mock.Mock(returncode=0, stdout="loaded\n")
        active = mock.Mock(returncode=0, stdout="active\n")
        with mock.patch("isadoraair.db_credentials.subprocess.run", side_effect=[loaded, active] * 5):
            with self.assertRaisesMessage(
                db_credentials.CredentialPreflightError, "stop the persistent database consumers",
            ):
                rotator._assert_persistent_consumers_stopped()

    def test_interrupted_child_is_killed_and_reaped(self):
        rotator = self._rotator()
        process = mock.Mock(pid=424242)
        process.poll.return_value = None
        with mock.patch.object(db_credentials.subprocess, "Popen", return_value=process):
            with mock.patch.object(db_credentials.time, "sleep", side_effect=KeyboardInterrupt):
                with mock.patch.object(db_credentials.os, "killpg") as killpg:
                    with self.assertRaises(KeyboardInterrupt):
                        rotator._run(self.station, ["/test/child"])
        killpg.assert_called_once_with(424242, db_credentials.signal.SIGKILL)
        process.wait.assert_called_once_with()


class RealPostgreSQLIntegrationTests(SimpleTestCase):
    """Opt-in test against a disposable SCRAM-authenticated PostgreSQL role."""

    def test_real_rotation_and_post_dump_failure_rollback(self):
        socket_root = os.environ.get("ISADORAAIR_ROTATION_TEST_SOCKET")
        if not socket_root:
            self.skipTest("set ISADORAAIR_ROTATION_TEST_SOCKET for disposable PostgreSQL integration")
        port = int(os.environ.get("ISADORAAIR_ROTATION_TEST_PORT", "55441"))
        identity = db_credentials.DatabaseIdentity(socket_root, port, "rotation_test", "rotation_test")
        initial = "rotation-old-test-password-32-chars"
        committed = "rotation-new-test-password-32-chars"
        rejected = "rotation-rejected-password-32-chars"

        admin = psycopg2.connect(host=socket_root, port=port, dbname="postgres", user=pwd.getpwuid(os.geteuid()).pw_name)
        admin.autocommit = True
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("ALTER ROLE {} PASSWORD %s").format(sql.Identifier(identity.user)),
                (initial,),
            )
        admin.close()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = root / ".env"
            pgpass = root / ".pgpass"
            jobs = root / "jobs"
            jobs.mkdir()
            env.write_text(
                "SECRET_KEY=test-only\nDEBUG=True\n"
                f"DB_HOST={identity.host}\nDB_PORT={identity.port}\nDB_NAME={identity.name}\n"
                f"DB_USER={identity.user}\nDB_PASSWORD={initial}\n",
                encoding="utf-8",
            )
            pgpass.write_text(
                f"{identity.host}:{identity.port}:{identity.name}:{identity.user}:{initial}\n",
                encoding="utf-8",
            )
            os.chmod(env, 0o600)
            os.chmod(pgpass, 0o600)
            account = pwd.getpwuid(os.geteuid())
            station = StationConfig(
                trusted_repository_url="https://example.invalid/repo.git", trusted_branch="main",
                application_root=root, application_user=account.pw_name, application_group=account.pw_name,
                application_environment_file=env, trusted_repository=root / "trusted", jobs_root=jobs,
                logs_root=root / "logs", staging_root=root / "staging", checkpoint_root=root / "checkpoints",
                socket_path=root / "run/updater.sock", systemd_unit_root=root / "systemd", render_values={},
                database=DatabaseConfig(identity.name, identity.user, identity.host, identity.port, pgpass),
                gunicorn_health_url="http://127.0.0.1:8000/", update_execution_enabled=True,
                operator_restart_units=(), phase_d_supervisor_activation_socket=None,
                phase_d_supervisor_slots_root=None, phase_d_trust_policy_path=None, phase_d_signer_root=None,
            )

            def configure(injector=lambda _phase: None):
                rotator = db_credentials.CredentialRotator(
                    require_root=False, enforce_config_protection=False, enforce_live_root=False,
                    failure_injector=injector, command_timeout=30,
                )
                rotator._station = lambda: station

                def django_probe(_station):
                    values = decouple.RepositoryEnv(str(env)).data
                    try:
                        connection = psycopg2.connect(
                            host=values["DB_HOST"], port=int(values["DB_PORT"]), dbname=values["DB_NAME"],
                            user=values["DB_USER"], password=values["DB_PASSWORD"], connect_timeout=5,
                        )
                        connection.close()
                        return True
                    except psycopg2.Error:
                        return False

                rotator._django_probe = django_probe
                return rotator

            first_settings = {"default": {
                "HOST": identity.host, "PORT": str(identity.port), "NAME": identity.name,
                "USER": identity.user, "PASSWORD": initial,
            }}
            with mock.patch.object(db_credentials.settings, "DATABASES", first_settings):
                configure().rotate(committed)
            self.assertTrue(configure()._django_probe(station))
            with self.assertRaises(psycopg2.Error):
                psycopg2.connect(
                    host=identity.host, port=identity.port, dbname=identity.name,
                    user=identity.user, password=initial, connect_timeout=5,
                )

            second_settings = {"default": {**first_settings["default"], "PASSWORD": committed}}

            failing = configure()
            real_dump = failing._pg_dump_probe
            dump_calls = {"count": 0}

            def fail_after_real_dump(station_arg, identity_arg):
                dump_calls["count"] += 1
                result = real_dump(station_arg, identity_arg)
                # Call 1 is preflight. Call 2 is the post-change validator:
                # execute the real custom dump, then inject rejection. Call 3
                # is rollback validation and must be allowed to pass.
                return result and dump_calls["count"] != 2

            failing._pg_dump_probe = fail_after_real_dump
            with mock.patch.object(db_credentials.settings, "DATABASES", second_settings):
                with self.assertRaises(db_credentials.CredentialRotationError):
                    failing.rotate(rejected)
            self.assertGreaterEqual(dump_calls["count"], 3)
            self.assertTrue(configure()._django_probe(station))
            self.assertEqual(decouple.RepositoryEnv(str(env)).data["DB_PASSWORD"], committed)
            self.assertEqual(db_credentials.read_pgpass_password(pgpass.read_bytes(), identity), committed)


class RealPostgreSQLMutationLoggingIntegrationTests(SimpleTestCase):
    """Required integration proof for the r0059 Pass G re-review's remaining
    release blocker: a real, fully self-managed, disposable PostgreSQL
    cluster (never the station's or any external database) configured with
    statement/error logging aggressive enough that an *unsuppressed* ALTER
    ROLE would unquestionably be captured -- log_statement='all',
    log_min_error_statement='error', log_min_duration_statement=0 -- proves
    that _alter_role()'s real logging-suppression guard actually keeps the
    SCRAM verifier (and, on the failure path, the attempted new password)
    out of the server's real log file, for forward rotation, a failing
    mutation, and a rollback mutation alike.

    A dedicated positive-control statement (issued deliberately unsuppressed
    on the plain superuser connection) proves this cluster's logging
    configuration -- and this test's log-inspection methodology -- would
    actually have caught a real leak, so the negative assertions below mean
    something.

    Self-contained: uses the isadoraair-django venv host's installed
    PostgreSQL 18 server binaries to initdb/start/stop its own throwaway
    cluster in a private temporary directory; skips cleanly if those
    binaries are unavailable. Never touches the station's or any other
    running PostgreSQL instance.

    pgAudit is not installed in this environment (confirmed: no
    ``pgaudit.log`` row in ``pg_settings`` once the cluster is up -- see
    ``test_cluster_confirms_no_pgaudit_is_loaded``), so pgAudit's
    suppression path is exercised only by MutationLoggingGuardTests' mocked
    contract tests above, never empirically against a real pgAudit-enabled
    server.
    """

    PG_BIN = Path("/usr/lib/postgresql/18/bin")

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not (cls.PG_BIN / "initdb").exists() or not (cls.PG_BIN / "pg_ctl").exists():
            raise unittest.SkipTest("local PostgreSQL 18 server binaries are not installed")
        cls._tempdir = tempfile.mkdtemp(prefix="isadoraair-logit-")
        cls.root = Path(cls._tempdir)
        cls.data_dir = cls.root / "data"
        cls.socket_dir = cls.root
        cls.log_path = cls.root / "server.log"
        cls.port = 55555
        cls.superuser = pwd.getpwuid(os.geteuid()).pw_name
        try:
            subprocess.run(
                [str(cls.PG_BIN / "initdb"), "-D", str(cls.data_dir), "-U", cls.superuser,
                 "--auth=trust", "--no-sync", "-E", "UTF8"],
                check=True, capture_output=True, text=True, timeout=60,
            )
            with open(cls.data_dir / "postgresql.conf", "a") as handle:
                handle.write(
                    "\n# isadoraair r0059 mutation-logging integration test -- deliberately\n"
                    "# aggressive so an unsuppressed sensitive statement would be captured.\n"
                    "listen_addresses = ''\n"
                    "log_destination = 'stderr'\n"
                    "logging_collector = off\n"
                    "log_statement = 'all'\n"
                    "log_min_error_statement = 'error'\n"
                    "log_min_duration_statement = 0\n"
                    "log_min_duration_sample = 0\n"
                    "log_transaction_sample_rate = 1\n"
                    "log_connections = off\n"
                    "log_disconnections = off\n"
                )
            subprocess.run(
                [str(cls.PG_BIN / "pg_ctl"), "-D", str(cls.data_dir),
                 "-o", f"-k {cls.socket_dir} -p {cls.port} -h ''",
                 "-l", str(cls.log_path), "-w", "-t", "30", "start"],
                check=True, capture_output=True, text=True, timeout=60,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            shutil.rmtree(cls._tempdir, ignore_errors=True)
            raise unittest.SkipTest(f"could not start a disposable PostgreSQL 18 cluster: {exc}")
        cls._started = True
        admin = psycopg2.connect(host=str(cls.socket_dir), port=cls.port, dbname="postgres", user=cls.superuser)
        admin.autocommit = True
        try:
            with admin.cursor() as cursor:
                cursor.execute("CREATE ROLE logtest_ok LOGIN PASSWORD 'initial-ok-password-32-characters'")
                cursor.execute("CREATE ROLE logtest_denied LOGIN PASSWORD 'initial-denied-password-32-chars'")
                cursor.execute(
                    "GRANT SET ON PARAMETER log_statement, log_min_error_statement, "
                    "log_min_duration_statement, log_min_duration_sample, "
                    "log_transaction_sample_rate TO logtest_ok"
                )
        finally:
            admin.close()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "_started", False):
            subprocess.run(
                [str(cls.PG_BIN / "pg_ctl"), "-D", str(cls.data_dir), "-m", "immediate", "-w", "stop"],
                capture_output=True, text=True, timeout=30,
            )
            shutil.rmtree(cls._tempdir, ignore_errors=True)
        super().tearDownClass()

    def _admin_connection(self):
        connection = psycopg2.connect(host=str(self.socket_dir), port=self.port, dbname="postgres", user=self.superuser)
        connection.autocommit = True
        return connection

    def _role_connection(self, user, password):
        connection = psycopg2.connect(
            host=str(self.socket_dir), port=self.port, dbname="postgres", user=user, password=password,
        )
        connection.autocommit = True
        return connection

    def _log_text(self):
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def _log_mark(self) -> int:
        """Byte offset marking 'now' in the (cumulative, ever-growing)
        server log -- setUpClass's own plain, unsuppressed admin
        CREATE ROLE .../GRANT statements legitimately mention the fixture's
        own well-known initial passwords, so negative assertions about a
        *specific* guarded mutation attempt must only inspect log content
        appended since that attempt began, never the whole file."""
        return self.log_path.stat().st_size

    def _log_since(self, mark: int) -> str:
        with open(self.log_path, "rb") as handle:
            handle.seek(mark)
            return handle.read().decode("utf-8", errors="replace")

    def _rotator(self):
        return db_credentials.CredentialRotator(
            require_root=False, enforce_config_protection=False, enforce_live_root=False,
        )

    def test_cluster_confirms_no_pgaudit_is_loaded(self):
        admin = self._admin_connection()
        try:
            with admin.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_settings WHERE name = 'pgaudit.log'")
                self.assertIsNone(cursor.fetchone())
        finally:
            admin.close()

    def test_unsuppressed_control_statement_is_actually_logged(self):
        """Positive control: proves this cluster's aggressive logging
        configuration, and this test module's log-inspection methodology,
        would genuinely catch a real leak -- without it, the negative
        assertions in the tests below would be meaningless."""
        admin = self._admin_connection()
        try:
            with admin.cursor() as cursor:
                cursor.execute(
                    sql.SQL("ALTER ROLE logtest_ok PASSWORD %s"),
                    ("CANARY-POSITIVE-CONTROL-VALUE-32CH",),
                )
                # Restore, so later tests connecting as logtest_ok as part of
                # the class fixture still authenticate with the known value.
                cursor.execute(
                    sql.SQL("ALTER ROLE logtest_ok PASSWORD %s"),
                    ("initial-ok-password-32-characters",),
                )
        finally:
            admin.close()
        self.assertIn("CANARY-POSITIVE-CONTROL-VALUE-32CH", self._log_text())

    def test_successful_mutation_leaves_no_verifier_or_password_in_server_log(self):
        new_password = "rotation-success-new-pw-32-characters"
        mark = self._log_mark()
        connection = self._role_connection("logtest_ok", "initial-ok-password-32-characters")
        try:
            self._rotator()._alter_role(connection, "logtest_ok", new_password)
        finally:
            connection.close()
        log_slice = self._log_since(mark)
        self.assertNotIn(new_password, log_slice)
        self.assertNotIn("initial-ok-password-32-characters", log_slice)
        self.assertNotIn("SCRAM-SHA-256$", log_slice)
        # Restore the well-known password for the rollback-mutation test below.
        mark = self._log_mark()
        restore = self._role_connection("logtest_ok", new_password)
        try:
            self._rotator()._alter_role(restore, "logtest_ok", "initial-ok-password-32-characters")
        finally:
            restore.close()
        log_slice = self._log_since(mark)
        self.assertNotIn(new_password, log_slice)
        self.assertNotIn("initial-ok-password-32-characters", log_slice)
        self.assertNotIn("SCRAM-SHA-256$", log_slice)

    def test_failing_mutation_still_leaves_no_verifier_or_password_in_server_log(self):
        """log_min_error_statement='error' means a FAILING ALTER ROLE would
        normally still be captured even with log_statement suppressed --
        this proves the guard's stronger log_min_error_statement='panic'
        override holds even when the sensitive statement itself fails.

        The failure is a genuine server-side rejection (logtest_ok has no
        CREATEROLE/admin privilege over a role that is not itself, so
        PostgreSQL rejects the ALTER ROLE with "permission denied") rather
        than a nonexistent target role. The earlier direct
        PQchangePassword/ctypes implementation exposed a native-libpq crash
        hazard during this correction; the current implementation instead
        generates the SCRAM verifier through psycopg2.extensions.encrypt_password()
        and issues ALTER ROLE separately. This test therefore focuses only
        on proving suppression of a genuine server-side ALTER ROLE failure."""
        rejected_password = "rotation-rejected-attempt-pw-32-chars"
        mark = self._log_mark()
        connection = self._role_connection("logtest_ok", "initial-ok-password-32-characters")
        try:
            with self.assertRaises(db_credentials.CredentialRotationError):
                self._rotator()._alter_role(connection, "logtest_denied", rejected_password)
        finally:
            connection.close()
        log_slice = self._log_since(mark)
        self.assertNotIn(rejected_password, log_slice)
        self.assertNotIn("SCRAM-SHA-256$", log_slice)
        # The ALTER ROLE was genuinely attempted (and rejected) -- confirms
        # this is testing log_min_error_statement's suppression, not merely
        # a guard failure that prevented the verifier-bearing ALTER ROLE.
        self.assertIn("permission denied to alter role", log_slice)
        # The failed attempt targeted a different role entirely -- both
        # logtest_ok and logtest_denied must be provably unchanged.
        still_old = self._role_connection("logtest_ok", "initial-ok-password-32-characters")
        still_old.close()
        still_old_denied = self._role_connection("logtest_denied", "initial-denied-password-32-chars")
        still_old_denied.close()

    def test_rollback_mutation_leaves_no_verifier_or_password_in_server_log(self):
        rolled_new_password = "rotation-rollback-forward-pw-32-chars"
        mark = self._log_mark()
        connection = self._role_connection("logtest_ok", "initial-ok-password-32-characters")
        try:
            self._rotator()._alter_role(connection, "logtest_ok", rolled_new_password)
            # Same live session _rollback() would reuse -- restore the old
            # password exactly as the real rollback path does.
            self._rotator()._alter_role(connection, "logtest_ok", "initial-ok-password-32-characters")
        finally:
            connection.close()
        log_slice = self._log_since(mark)
        self.assertNotIn(rolled_new_password, log_slice)
        self.assertNotIn("initial-ok-password-32-characters", log_slice)
        self.assertNotIn("SCRAM-SHA-256$", log_slice)
        restored = self._role_connection("logtest_ok", "initial-ok-password-32-characters")
        restored.close()

    def test_role_without_set_privilege_aborts_before_any_mutation_and_reports_actionable_reason(self):
        """Under trust auth (this disposable cluster's simplification --
        see setUpClass), connecting with ANY password succeeds, so
        "unchanged" cannot be proven by a rejected reconnect attempt.
        Instead, the real server log itself is the proof: since the guard
        fails before verifier generation or ALTER ROLE execution, no ALTER ROLE
        statement of any kind -- suppressed or not -- reaches the server at
        all for this attempt."""
        mark = self._log_mark()
        connection = self._role_connection("logtest_denied", "initial-denied-password-32-chars")
        try:
            with self.assertRaises(db_credentials.CredentialLoggingSuppressionError) as caught:
                self._rotator()._alter_role(connection, "logtest_denied", "should-never-be-applied-32-chars")
        finally:
            connection.close()
        self.assertIn("log_statement", str(caught.exception))
        self.assertNotIn("should-never-be-applied-32-chars", str(caught.exception))
        log_slice = self._log_since(mark)
        self.assertNotIn("should-never-be-applied-32-chars", log_slice)
        self.assertNotIn("ALTER ROLE", log_slice.upper())
