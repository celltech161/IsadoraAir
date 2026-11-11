"""Roadmap 1.22 -- the trusted migration probe refuses an inconsistent recorder history.

`updatecenter_probe` runs as the application user, from the TARGET source, on
behalf of the protected updater. Its plan was `migration_plan(leaf_nodes())`,
which skips a leaf Django already believes applied without looking at that
leaf's dependencies, so a missing recorder row for an interior migration was
invisible: the probe happily emitted a plan and a migration-plan digest. Now it
asks Django's own `check_consistent_history` first and FAILS (no payload, no
digest, no manual-operation list), which the protected updater already
classifies as PROBE_FAILED.

Every recorder manipulation here is bookkeeping inside a disposable test
database transaction (Django's MigrationRecorder), never a production edit."""
import io
import json
import re
import traceback
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.test import SimpleTestCase, TestCase

from . import phase_b_helpers  # noqa: F401 -- sys.path side effect, see that module's docstring
from isadoraair_updater.executor import ExecutionError, Executor
from isadoraair_updater.process import ProcessResult
from updatecenter.management.commands import updatecenter_probe as probe
from updatecenter.management.commands.updatecenter_probe import build_probe_payload
from .test_schema_health import _interior

HEALTHY_PAYLOAD_KEYS = {
    "schema_version", "status", "plan", "nodes", "applied", "conflicts", "replacements", "release_id",
    "target_commit", "manifest_sha256", "migration_plan_digest", "manual_operations", "approval",
}
TARGET_COMMIT = "a" * 40
RELEASE_ID = "r0132"          # a real manifest at deploy/releases/ (the probe reads it relative to the repo root)


class _MissingInterior:
    """Context manager: mark the parent of library's leaf unapplied (an interior record), restore after."""

    def __init__(self, app_label="library"):
        (self.app, self.leaf), (_, self.parent) = _interior(app_label)
        self.recorder = MigrationRecorder(connection)

    def __enter__(self):
        self.recorder.record_unapplied(self.app, self.parent)
        return self

    def __exit__(self, *exc):
        self.recorder.record_applied(self.app, self.parent)


class HealthyProbeUnchangedTests(TestCase):
    def test_consistent_database_emits_the_established_payload(self):
        payload = build_probe_payload()
        self.assertEqual(set(payload), HEALTHY_PAYLOAD_KEYS)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["plan"], [])                      # the test database is fully migrated
        self.assertIsNone(payload["migration_plan_digest"])
        self.assertEqual(payload["manual_operations"], [])
        self.assertIsNone(payload["approval"])
        json.dumps(payload)                                         # still wire-serialisable

    def test_command_writes_exactly_one_json_document(self):
        out = io.StringIO()
        call_command("updatecenter_probe", skip_checks=True, stdout=out)
        self.assertEqual(set(json.loads(out.getvalue())), HEALTHY_PAYLOAD_KEYS)

    def test_legitimately_pending_leaf_still_appears_in_the_plan(self):
        """A consistent applied prefix (only the leaf unapplied) is ordinary pending work."""
        (app, leaf), _ = _interior("library")
        recorder = MigrationRecorder(connection)
        recorder.record_unapplied(app, leaf)
        try:
            payload = build_probe_payload()
        finally:
            recorder.record_applied(app, leaf)
        self.assertEqual([item["ref"] for item in payload["plan"]], [f"{app}.{leaf}"])
        self.assertNotIn(f"{app}.{leaf}", payload["applied"])

    def test_consistent_prefix_of_two_pending_migrations_keeps_plan_order(self):
        (app, leaf), (_, parent) = _interior("library")
        recorder = MigrationRecorder(connection)
        recorder.record_unapplied(app, leaf)
        recorder.record_unapplied(app, parent)
        try:
            payload = build_probe_payload()
        finally:
            recorder.record_applied(app, parent)
            recorder.record_applied(app, leaf)
        self.assertEqual([item["ref"] for item in payload["plan"]], [f"{app}.{parent}", f"{app}.{leaf}"])

    def test_healthy_review_digest_is_recomputed_identically(self):
        """The digest for a healthy (pending-leaf) plan is the established canonical value: recomputing it from
        the emitted plan, the manifest bytes and the context gives the same digest, and the manual-operation
        list equals extract_manual_operations(plan). (Compared against the pre-change code in the evidence.)"""
        (app, leaf), _ = _interior("library")
        recorder = MigrationRecorder(connection)
        recorder.record_unapplied(app, leaf)
        try:
            payload = build_probe_payload(release_id=RELEASE_ID, target_commit=TARGET_COMMIT)
        finally:
            recorder.record_applied(app, leaf)
        manifest_path = Path("deploy") / "releases" / f"{RELEASE_ID}.json"
        import hashlib
        self.assertEqual(payload["manifest_sha256"], hashlib.sha256(manifest_path.read_bytes()).hexdigest())
        self.assertEqual(payload["migration_plan_digest"], probe.compute_migration_plan_digest(
            release_id=RELEASE_ID, target_commit=TARGET_COMMIT, manifest_sha256=payload["manifest_sha256"], plan=payload["plan"]))
        self.assertEqual(payload["manual_operations"], probe.extract_manual_operations(payload["plan"]))
        print(f"EVIDENCE healthy-digest {RELEASE_ID} pending={app}.{leaf} digest={payload['migration_plan_digest']}")


class InconsistentHistoryProbeTests(TestCase):
    def test_probe_refuses_and_names_the_dependency_violation(self):
        with _MissingInterior() as missing:
            with self.assertRaises(RuntimeError) as caught:
                build_probe_payload()
        message = str(caught.exception)
        self.assertIn("inconsistent", message.lower())
        self.assertIn(f"{missing.app}.{missing.leaf}", message)
        self.assertIn(f"{missing.app}.{missing.parent}", message)

    def test_refusal_is_identical_in_another_app(self):
        with _MissingInterior("webrequests") as missing:
            with self.assertRaisesRegex(RuntimeError, re.escape(f"{missing.app}.{missing.parent}")):
                build_probe_payload()

    def test_no_digest_no_manual_operations_no_plan_work_for_invalid_history(self):
        """The failure precedes plan construction: nothing is classified, hashed or digested."""
        with _MissingInterior():
            with mock.patch.object(probe, "compute_migration_plan_digest") as digest, \
                 mock.patch.object(probe, "extract_manual_operations") as manual, \
                 mock.patch.object(probe, "_classify_migration_operation") as classify, \
                 mock.patch.object(probe, "_migration_file_sha256") as filehash:
                with self.assertRaises(RuntimeError):
                    build_probe_payload(release_id=RELEASE_ID, target_commit=TARGET_COMMIT)
        digest.assert_not_called()
        manual.assert_not_called()
        classify.assert_not_called()
        filehash.assert_not_called()

    def test_recovery_reconstruction_is_refused_too(self):
        with _MissingInterior() as missing:
            with self.assertRaises(RuntimeError):
                build_probe_payload(release_id=RELEASE_ID, target_commit=TARGET_COMMIT,
                                    recovery_plan_refs=[f"{missing.app}.{missing.leaf}"])

    def test_command_fails_and_emits_no_payload(self):
        out, err = io.StringIO(), io.StringIO()
        with _MissingInterior():
            # The command's handle() lets the error propagate (no CommandError wrapper, no stdout write):
            # an uncaught exception ends the manage.py process with a nonzero status, exactly like the probe's
            # other RuntimeError refusals. (run_from_argv itself is not used here: it closes every database
            # connection in its `finally`, which would destroy this test's transaction.)
            with self.assertRaises(RuntimeError):
                call_command("updatecenter_probe", skip_checks=True, stdout=out, stderr=err)
        self.assertEqual(out.getvalue(), "")                      # no successful JSON, no digest

    def test_ordinary_probe_never_modifies_the_recorder(self):
        recorder = MigrationRecorder(connection)
        before = set(recorder.applied_migrations())
        build_probe_payload()
        with _MissingInterior():
            degraded = set(recorder.applied_migrations())
            with self.assertRaises(RuntimeError):
                build_probe_payload()
            self.assertEqual(set(recorder.applied_migrations()), degraded)
        self.assertEqual(set(recorder.applied_migrations()), before)


class ProtectedCallerClassificationTests(TestCase):
    """The protected updater (deploy/updater_runtime) must treat that failure as PROBE_FAILED, unchanged."""

    def _failed_result(self, *, release_context):
        """A ProcessResult shaped like the real command's failure: nonzero exit, empty stdout, the real
        traceback text on stderr."""
        with _MissingInterior():
            try:
                kwargs = {"release_id": RELEASE_ID, "target_commit": TARGET_COMMIT} if release_context else {}
                build_probe_payload(**kwargs)
            except RuntimeError:
                stderr = traceback.format_exc().encode("utf-8")
            else:                                           # pragma: no cover -- the probe must have failed
                self.fail("the probe did not fail on an inconsistent history")
        return ProcessResult(("manage.py", "updatecenter_probe"), 1, b"", stderr)

    def _executor(self, result):
        executor = object.__new__(Executor)
        executor._run_app = mock.Mock(return_value=(result, {}))
        return executor

    def test_current_schema_probe_failure_is_probe_failed(self):
        executor = self._executor(self._failed_result(release_context=False))
        executor.config = mock.Mock(application_root=Path("."))
        with self.assertRaises(ExecutionError) as caught:
            executor._validate_current_schema()
        self.assertEqual(caught.exception.classification, "PROBE_FAILED")

    def test_target_schema_probe_failure_is_probe_failed(self):
        executor = self._executor(self._failed_result(release_context=True))
        with self.assertRaises(ExecutionError) as caught:
            executor._probe(Path("."), release_id=RELEASE_ID, target_commit=TARGET_COMMIT)
        self.assertEqual(caught.exception.classification, "PROBE_FAILED")
        self.assertIn("inconsistent", str(caught.exception).lower())

    def test_only_the_read_only_probe_command_is_run(self):
        """Classifying the failure runs nothing else: no migrate, no git, no source or database mutation."""
        executor = self._executor(self._failed_result(release_context=True))
        with self.assertRaises(ExecutionError):
            executor._probe(Path("."), release_id=RELEASE_ID, target_commit=TARGET_COMMIT)
        (call,) = executor._run_app.call_args_list
        arguments = call.args[1]
        self.assertEqual(arguments[0], "updatecenter_probe")
        self.assertNotIn("migrate", arguments)


class PreAdvanceGateOrderTests(SimpleTestCase):
    """Both probes that can see the history run BEFORE the source is advanced or any migration is applied, so
    PROBE_FAILED stops the job at the pre-advance gate with nothing mutated. The target probe uses the STAGED
    target commit's own copy of this command, so a resumed job (source already advanced) is still protected
    by the target's code, not the possibly stale installed code."""

    def test_probes_precede_source_advance_and_migration(self):
        source = (Path(__file__).resolve().parents[2] / "deploy" / "updater_runtime" / "isadoraair_updater" / "executor.py").read_text()
        run_body = source[source.index("current_payload = self._validate_current_schema()"):]
        current = run_body.index("self._validate_current_schema()")
        target = run_body.index("target_payload = self._probe(")
        advance = run_body.index("self._advance_source(plan)")
        migrate_marker = run_body.index("self._run_migration_preflights(staged.source_root")
        self.assertLess(current, target)
        self.assertLess(target, advance)
        self.assertLess(target, migrate_marker)
        self.assertIn("staged.source_root, release_id=plan.target_release_id", run_body[target:target + 200])
