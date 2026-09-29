"""Protected-runtime generation 7 -- heterogeneous current/target probe.

This is the regression test for the real production defect behind r0090's
PROBE_INVALID failure (job f09ccba1-2fdf-4050-b901-57fed15628f9): generation
6's executor._strict_probe() unconditionally required the reviewed-approval
13-key schema, but the current-schema probe always runs against whatever
application source is CURRENTLY installed -- which, for any station that
has not yet had an ordinary release deploy the new updatecenter_probe.py,
is still the pre-693ea94 script emitting the legacy 7-key shape. Protected-
runtime generation and ordinary application-source deployment are on
independent cadences; the "current" probe can never assume the "target"
probe's schema is already live.

Every generation-6 unit test that exercised this boundary handed BOTH the
current and target probe calls a hand-authored fixture dict that already
contained all 13 keys -- so none of them ever modeled the real asymmetry.
This test does not repeat that mistake: the "current" side runs the ACTUAL
r0088 updatecenter_probe.py, extracted byte-for-byte from git history via
`git show`, as a genuine subprocess against a disposable checkout -- not a
hand-typed approximation of what an old script "would" emit. The "target"
side uses the real r0089 migration graph via the same DB-rollback technique
already established in test_r0089_migration_approval_acceptance.py.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from .phase_b_helpers import PROJECT_ROOT
from isadoraair_updater.executor import (
    _LEGACY_PROBE_KEYS, _REVIEW_PROBE_KEYS, ExecutionError, Executor, _strict_probe,
)
from updatecenter.management.commands.updatecenter_probe import build_probe_payload

# The real, still-installed-in-production commit whose application source
# (and therefore whose updatecenter_probe.py) predates the reviewed
# migration approval feature entirely. See PROJECT_NOTES.md.
R0088_COMMIT = "edc5d5c8f345ba18646db66a9a050093d1a076b0"
R0089_TARGET_COMMIT = "1" * 40  # placeholder -- this test never touches git for the target side


def _run_real_legacy_probe() -> bytes:
    """Materialize the ACTUAL r0088 updatecenter_probe.py in a disposable
    Django project and run it as a genuine subprocess. Returns its raw
    stdout bytes -- exactly what executor.py's _probe() would have piped
    into _strict_probe() in production."""
    legacy_source = subprocess.run(
        ["git", "show", f"{R0088_COMMIT}:updatecenter/management/commands/updatecenter_probe.py"],
        cwd=PROJECT_ROOT, check=True, capture_output=True, text=True,
    ).stdout
    if "release_id" in legacy_source or "release-id" in legacy_source:
        raise AssertionError(
            "r0088's updatecenter_probe.py unexpectedly mentions release_id -- "
            "this test would no longer be exercising a genuinely pre-approval script"
        )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "sample" / "migrations").mkdir(parents=True)
        (root / "updatecenter" / "management" / "commands").mkdir(parents=True)
        for package in (
            root / "sample", root / "sample" / "migrations",
            root / "updatecenter", root / "updatecenter" / "management",
            root / "updatecenter" / "management" / "commands",
        ):
            (package / "__init__.py").touch()
        (root / "manage.py").write_text(
            "import os,sys\nos.environ.setdefault('DJANGO_SETTINGS_MODULE','settings')\n"
            "from django.core.management import execute_from_command_line\nexecute_from_command_line(sys.argv)\n",
            encoding="utf-8",
        )
        database = root / "db.sqlite3"
        (root / "settings.py").write_text(
            "SECRET_KEY='test'\nINSTALLED_APPS=['django.contrib.contenttypes','sample','updatecenter']\n"
            f"DATABASES={{'default':{{'ENGINE':'django.db.backends.sqlite3','NAME':r'{database}'}}}}\n"
            "DEFAULT_AUTO_FIELD='django.db.models.BigAutoField'\n",
            encoding="utf-8",
        )
        (root / "sample" / "models.py").write_text(
            "from django.db import models\nclass Item(models.Model):\n    name=models.CharField(max_length=20)\n",
            encoding="utf-8",
        )
        (root / "sample" / "migrations" / "0001_initial.py").write_text(
            "from django.db import migrations,models\nclass Migration(migrations.Migration):\n"
            "    initial=True\n    dependencies=[]\n    operations=[migrations.CreateModel("
            "name='Item',fields=[('id',models.BigAutoField(primary_key=True,serialize=False)),"
            "('name',models.CharField(max_length=20))])]\n",
            encoding="utf-8",
        )
        (root / "updatecenter" / "management" / "commands" / "updatecenter_probe.py").write_text(
            legacy_source, encoding="utf-8",
        )
        env = {
            "PATH": __import__("os").environ.get("PATH", ""), "PYTHONPATH": str(root),
            "DJANGO_SETTINGS_MODULE": "settings", "PYTHONDONTWRITEBYTECODE": "1",
        }
        subprocess.run(
            [sys.executable, str(root / "manage.py"), "migrate", "--noinput"],
            cwd=root, env=env, check=True, capture_output=True,
        )
        probe = subprocess.run(
            [sys.executable, str(root / "manage.py"), "updatecenter_probe", "--skip-checks"],
            cwd=root, env=env, check=True, capture_output=True,
        )
        return probe.stdout.strip()


class Gen7HeterogeneousProbeCompatibilityTests(TransactionTestCase):
    def setUp(self):
        self.migration_executor = MigrationExecutor(connection)
        self.leaf_targets = self.migration_executor.loader.graph.leaf_nodes()
        # Roll the real DB back to exactly pre-r0089, same technique as
        # test_r0089_migration_approval_acceptance.py.
        self.migration_executor.migrate([
            ("authz", None),
            ("library", "0084_alter_uitheme_logo_alter_uitheme_station_logo"),
        ])

    def tearDown(self):
        MigrationExecutor(connection).migrate(self.leaf_targets)

    def test_legacy_current_probe_accepted_then_real_target_probe_stops_at_manual_review(self):
        # 1-2-3: current-schema probe genuinely runs the OLD r0088 script,
        # which correctly emits only the legacy 7-key shape.
        current_raw = _run_real_legacy_probe()
        current_payload = _strict_probe(current_raw, review_context=False)
        self.assertEqual(set(current_payload), _LEGACY_PROBE_KEYS)
        self.assertEqual(current_payload["status"], "ok")
        self.assertEqual(current_payload["plan"], [])

        # 4-5-6: target-schema probe runs the NEW script (this checkout, in
        # process, same DB) WITH real release/target review context, over
        # the real r0089 migration graph, and emits the full 13-key shape.
        target_raw = json.dumps(
            build_probe_payload(release_id="r0089", target_commit=R0089_TARGET_COMMIT)
        ).encode("utf-8")
        target_payload = _strict_probe(target_raw, review_context=True)
        self.assertEqual(set(target_payload), _REVIEW_PROBE_KEYS)
        self.assertEqual(target_payload["release_id"], "r0089")
        self.assertEqual(target_payload["target_commit"], R0089_TARGET_COMMIT)
        self.assertTrue(target_payload["manual_operations"], "r0089's real manual operations must be discovered")
        self.assertIsNone(target_payload["approval"])

        # 7-8: execution progresses into migration-plan review and reaches
        # manual_intervention_required (MIGRATION_OPERATION_MANUAL) because
        # no matching MigrationPlanApproval exists for this fresh digest.
        plan = types.SimpleNamespace(
            installed_release_id="r0088", target_release_id="r0089",
            releases_in_plan=("r0089",),
            migrations_required=tuple(sorted(item["ref"] for item in target_payload["plan"])),
            migration_compatibility="additive",
            fingerprint="f" * 64,
        )
        validator = object.__new__(Executor)
        validator.approval_store = types.SimpleNamespace(find=lambda identity: None)
        with self.assertRaises(ExecutionError) as caught:
            validator._validate_target_schema(
                plan, target_payload, {"applied": current_payload["applied"]},
                "gen7-heterogeneous-probe-test", migration_already_started=False,
            )
        self.assertEqual(caught.exception.classification, "MIGRATION_OPERATION_MANUAL")
        self.assertTrue(caught.exception.manual)
        self.assertIsNotNone(caught.exception.migration_plan_review)
        self.assertEqual(caught.exception.migration_plan_review["release_id"], "r0089")

        # 9-10: no migration was applied and no source was advanced before
        # this stop -- this test never called `manage.py migrate` against
        # the real DB beyond setUp's deliberate rollback, and never touched
        # _advance_source/git at all.
        still_applied = build_probe_payload()["applied"]
        self.assertNotIn("authz.0001_initial", still_applied)
        self.assertNotIn("library.0085_remote_dj_queue_set_next_access", still_applied)

    def test_legacy_current_probe_polluted_with_real_review_data_is_rejected(self):
        """A context-less probe call reporting NON-default review fields
        (i.e. lying about being asked for review context) must be rejected
        exactly like any other schema violation -- proves the compatibility
        fix did not become permissive."""
        current_raw = _run_real_legacy_probe()
        current_payload = json.loads(current_raw)
        current_payload.update({
            "release_id": "r0089", "target_commit": R0089_TARGET_COMMIT,
            "manifest_sha256": "a" * 64, "migration_plan_digest": "b" * 64,
            "manual_operations": [], "approval": None,
        })
        with self.assertRaisesRegex(ExecutionError, "review evidence without review context"):
            _strict_probe(json.dumps(current_payload).encode("utf-8"), review_context=False)

    def test_target_probe_returning_only_legacy_shape_is_rejected(self):
        current_raw = _run_real_legacy_probe()
        with self.assertRaisesRegex(ExecutionError, "schema/status mismatch"):
            _strict_probe(current_raw, review_context=True)
