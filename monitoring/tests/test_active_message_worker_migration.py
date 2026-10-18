"""D3E-B correction B3: monitoring.0014 provisions the active-message worker's
Monitoring check during an unattended release, and its one RunPython is
installable only through an exact trusted migration-authorization companion.

* the migration itself: a migrated database has exactly one enabled check;
  re-running never duplicates; an existing check of the exact unit (any name,
  customized, disabled) is never changed; the name-collision policy never
  touches an unrelated row; reverse is a no-op; no systemd / Active Messages
  side effect;
* Update Center: the real classifier finds exactly one manual RunPython (never
  automatic); a synthetic release whose commit carries THESE migration bytes
  and its metadata-only companion is accepted by both the protected runtime and
  Django's mirror and covers the station's exact plan; altered bytes, operation
  index, operation, manifest hash, target commit, an incomplete or missing
  companion are refused / not covered;
* the executor applies migrations before unit reconciliation and restarts, so
  the check exists before the new worker is started and Monitoring restarted.

The real r0116 companion cannot exist yet (it binds r0116's not-yet-created
release commit and manifest hash); docs/ACTIVE_MESSAGES.md gives the authoring
procedure.
"""
import hashlib
import importlib
import inspect
import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TestCase

from active_messages.models import ActiveBroadcastExecution, ActiveBroadcastPolicy
from monitoring.models import MonitorCheck
from updatecenter import migration_authorization as django_side
from updatecenter.management.commands import updatecenter_probe as probe
from updatecenter.tests.phase_b_helpers import RUNTIME_ROOT, git, manifest  # noqa: F401

from isadoraair_updater import executor as executor_module
from isadoraair_updater.migration_authorization import (
    central_authorization_covers, manual_operation_identity, validate_release_companion,
)
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import ReleaseError, TrustedRepository, load_chain

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NAME = "0014_provision_active_message_worker_check"
REF = f"monitoring.{NAME}"
PATH = f"monitoring/migrations/{NAME}.py"
UNIT = "isadoraair-active-message-worker.service"
MIGRATION_BYTES = (PROJECT_ROOT / PATH).read_bytes()
MIGRATION_SHA256 = hashlib.sha256(MIGRATION_BYTES).hexdigest()
COMPANION = "deploy/migration_authorizations/r0003.json"
module = importlib.import_module(f"monitoring.migrations.{NAME}")


def historical_apps():
    return MigrationLoader(connection).project_state(("monitoring", NAME)).apps


def provision():
    module.provision_active_message_worker_check(historical_apps(), SimpleNamespace(connection=connection))


def worker_checks():
    return MonitorCheck.objects.filter(kind="systemd", systemd_unit=UNIT)


def snapshot():
    return sorted(MonitorCheck.objects.values_list(*[f.attname for f in MonitorCheck._meta.concrete_fields]))


class ProvisioningTests(TestCase):
    def test_a_migrated_database_has_exactly_one_enabled_check(self):
        [check] = worker_checks()
        self.assertEqual((check.name, check.enabled, check.show_as_card, check.consecutive_failures_required,
                          check.notify_on_warning, check.notify_on_critical),
                         ("Active Message Worker", True, True, 2, True, True))
        others = MonitorCheck.objects.filter(kind="systemd").exclude(pk=check.pk)
        self.assertGreater(check.sort_order, max(others.values_list("sort_order", flat=True), default=-1))

    def test_from_empty_it_creates_one_and_never_a_duplicate(self):
        worker_checks().delete()
        provision()
        self.assertEqual(worker_checks().count(), 1)
        before = snapshot()
        provision()
        provision()
        self.assertEqual(snapshot(), before)

    def test_an_existing_check_of_the_unit_is_never_changed(self):
        check = worker_checks().get()
        check.name, check.enabled, check.sort_order = "AM worker (ops)", False, 3
        check.consecutive_failures_required, check.notify_on_warning, check.show_as_card = 5, False, False
        check.warning_threshold, check.critical_threshold = 1.0, 2.0
        check.save()
        before = snapshot()
        provision()
        self.assertEqual(snapshot(), before)                     # byte-for-byte: disabled stays disabled
        self.assertFalse(worker_checks().get().enabled)

    def test_name_collisions_never_touch_an_unrelated_row(self):
        worker_checks().delete()
        unrelated = MonitorCheck.objects.create(name="Active Message Worker", kind="disk", disk_path="/",
                                                critical_threshold=90)
        provision()
        self.assertEqual(list(worker_checks().values_list("name", flat=True)), [UNIT])     # the fallback name
        self.assertEqual(MonitorCheck.objects.get(pk=unrelated.pk).kind, "disk")
        worker_checks().delete()
        MonitorCheck.objects.create(name=UNIT, kind="disk", disk_path="/tmp", critical_threshold=90)
        before = snapshot()
        provision()                                              # both names held: nothing is created or changed
        self.assertEqual(snapshot(), before)
        self.assertFalse(worker_checks().exists())

    def test_reverse_never_deletes_operator_state(self):
        migration = MigrationLoader(None, ignore_no_migrations=True).disk_migrations[("monitoring", NAME)]
        [operation] = migration.operations
        self.assertIs(operation.reverse_code, operation.noop)
        before = snapshot()
        operation.reverse_code(historical_apps(), SimpleNamespace(connection=connection))
        self.assertEqual(snapshot(), before)
        self.assertEqual(worker_checks().count(), 1)

    def test_no_service_or_active_messages_side_effect(self):
        worker_checks().delete()
        refuse = AssertionError("a migration must not touch services")
        with mock.patch.object(subprocess, "run", side_effect=refuse), \
                mock.patch.object(subprocess, "Popen", side_effect=refuse):
            provision()
        self.assertEqual(worker_checks().count(), 1)
        self.assertFalse(ActiveBroadcastPolicy.objects.exists())
        self.assertFalse(ActiveBroadcastExecution.objects.exists())
        source = MIGRATION_BYTES.decode()
        for word in ("subprocess", "systemctl", "import monitoring", "from monitoring", "active_messages"):
            self.assertNotIn(word, source.split("from django.db import migrations", 1)[1])

    def test_schema_is_unchanged_and_the_operation_is_one_atomic_runpython(self):
        migration = MigrationLoader(None, ignore_no_migrations=True).disk_migrations[("monitoring", NAME)]
        self.assertEqual(migration.dependencies, [("monitoring", "0013_backup_recovery_assurance_check")])
        self.assertTrue(migration.atomic)
        self.assertEqual([type(op).__name__ for op in migration.operations], ["RunPython"])


def exact_station_plan():
    plan = probe._serialize_exact_plan(MigrationLoader(None, ignore_no_migrations=True), [REF])
    return {"plan_items": [{"ref": item["ref"], "migration_file_sha256": item["migration_file_sha256"]}
                           for item in plan],
            "manual_operations": probe.extract_manual_operations(plan)}


class ClassificationTests(SimpleTestCase):
    def test_the_real_classifier_finds_exactly_one_manual_runpython(self):
        station = exact_station_plan()
        self.assertEqual(station["plan_items"], [{"ref": REF, "migration_file_sha256": MIGRATION_SHA256}])
        self.assertEqual([(op["ref"], op["operation_index"], op["operation"], op["classification"])
                          for op in station["manual_operations"]], [(REF, 0, "RunPython", "manual")])

    def test_nothing_covers_it_without_a_companion(self):
        self.assertFalse(central_authorization_covers({}, **exact_station_plan()))


class SyntheticRelease:
    """r0001 -> r0002 -> r0003, where r0003's commit introduces THESE exact
    migration bytes and declares them; companions are later commits."""

    def __init__(self, root: Path, *, migration_bytes=MIGRATION_BYTES):
        self.author = root
        root.mkdir(parents=True)
        git(root, "init", "-q", "-b", "main")
        (root / "README").write_text("baseline\n")
        git(root, "add", "README")
        git(root, "commit", "-q", "-m", "baseline")
        bootstrap = git(root, "rev-parse", "HEAD")
        releases = root / "deploy" / "releases"
        releases.mkdir(parents=True)
        self._release(manifest("r0001", None, bootstrap=bootstrap))
        self._release(manifest("r0002", "r0001", minimum_supported_release_id="r0001"))
        self.r0002 = git(root, "rev-parse", "HEAD")
        (root / "monitoring" / "migrations").mkdir(parents=True)
        (root / "monitoring" / "migrations" / "__init__.py").touch()
        (root / PATH).write_bytes(migration_bytes)
        self._release(manifest("r0003", "r0002", minimum_supported_release_id="r0002", migrations_required=[REF],
                               migration_compatibility="additive", services_requiring_restart=["isadoraair-monitoring"],
                               systemd_units_new_required=[UNIT]), extra=[PATH, "monitoring/migrations/__init__.py"],
                      unit=True)
        self.r0003 = git(root, "rev-parse", "HEAD")
        self.manifest_sha = hashlib.sha256((releases / "r0003.json").read_bytes()).hexdigest()

    def _release(self, data, *, extra=(), unit=False):
        path = self.author / "deploy" / "releases" / f"{data['release_id']}.json"
        path.write_text(json.dumps(data))
        paths = [str(path.relative_to(self.author)), *extra]
        if unit:
            (self.author / "deploy" / UNIT).write_bytes((PROJECT_ROOT / "deploy" / UNIT).read_bytes())
            paths.append(f"deploy/{UNIT}")
        git(self.author, "add", *paths)
        git(self.author, "commit", "-q", "-m", f"release {data['release_id']}")

    def record(self, **operation_changes):
        operation = {"ref": REF, "migration_file_sha256": MIGRATION_SHA256, "operation_index": 0,
                     "operation": "RunPython", "classification": "manual"}
        operation.update(operation_changes)
        return {"schema_version": 1, "release_id": "r0003", "target_commit": self.r0003,
                "manifest_sha256": self.manifest_sha, "authorized_manual_operations": [operation]}

    def add_companion(self, record):
        destination = self.author / COMPANION
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(record, indent=2) + "\n")
        git(self.author, "add", COMPANION)
        git(self.author, "commit", "-q", "-m", "companion r0003 (metadata only)")

    def verdicts(self):
        tip = git(self.author, "rev-parse", "HEAD")
        repository = TrustedRepository(self.author / ".git", "unused", "main", CommandRunner())
        entry = {item.manifest.release_id: item for item in load_chain(repository, tip)}["r0003"]
        try:
            runtime = ("ok", validate_release_companion(repository, tip, entry))
        except ReleaseError as exc:
            runtime = ("rejected", str(exc))
        try:
            django = ("ok", django_side.validate_release_companion(
                self.author, tip, release_id="r0003", release_commit=self.r0003, migrations_required=[REF]))
        except django_side.CompanionError as exc:
            django = ("rejected", str(exc))
        return runtime, django


class CompanionContractTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.count = 0

    def release(self, **kwargs):
        self.count += 1
        return SyntheticRelease(Path(self.temp.name) / f"repo{self.count}", **kwargs)

    def test_the_exact_companion_authorizes_the_station_plan_automatically(self):
        release = self.release()
        release.add_companion(release.record())
        runtime, django = release.verdicts()
        identity = (REF, MIGRATION_SHA256, 0, "RunPython", "manual")
        self.assertEqual((runtime, django), (("ok", frozenset({identity})), ("ok", frozenset({identity}))))
        self.assertTrue(central_authorization_covers({"r0003": runtime[1]}, **exact_station_plan()))

    def test_binding_errors_are_refused_by_both_validators(self):
        cases = {
            "altered migration bytes": lambda r: r.record(migration_file_sha256="0" * 64),
            "wrong manifest hash": lambda r: {**r.record(), "manifest_sha256": "1" * 64},
            "wrong target commit": lambda r: {**r.record(), "target_commit": r.r0002},
            "incomplete (no operations)": lambda r: {**r.record(), "authorized_manual_operations": []},
            "not manual": lambda r: r.record(classification="additive"),
            "another migration": lambda r: r.record(ref="monitoring.0013_backup_recovery_assurance_check"),
        }
        for label, build in cases.items():
            with self.subTest(label):
                release = self.release()
                release.add_companion(build(release))
                runtime, django = release.verdicts()
                self.assertEqual((runtime[0], django[0]), ("rejected", "rejected"), (runtime, django))

    def test_a_valid_companion_for_another_operation_does_not_cover_this_one(self):
        for label, changes in {"operation index 1": {"operation_index": 1},
                               "another operation": {"operation": "RunSQL"}}.items():
            with self.subTest(label):
                release = self.release()
                release.add_companion(release.record(**changes))
                runtime, django = release.verdicts()
                self.assertEqual((runtime[0], django[0]), ("ok", "ok"))
                self.assertFalse(central_authorization_covers({"r0003": runtime[1]}, **exact_station_plan()))

    def test_a_release_carrying_different_migration_bytes_is_not_covered(self):
        altered = MIGRATION_BYTES.replace(b'DEFAULT_NAME = "Active Message Worker"', b'DEFAULT_NAME = "Other"')
        self.assertNotEqual(altered, MIGRATION_BYTES)
        release = self.release(migration_bytes=altered)
        release.add_companion(release.record(migration_file_sha256=hashlib.sha256(altered).hexdigest()))
        runtime, _django = release.verdicts()
        self.assertEqual(runtime[0], "ok")                       # valid for ITS bytes...
        self.assertFalse(central_authorization_covers({"r0003": runtime[1]}, **exact_station_plan()))  # ...not ours

    def test_a_missing_companion_authorizes_nothing(self):
        runtime, django = self.release().verdicts()
        self.assertEqual((runtime, django), (("ok", None), ("ok", None)))

    def test_a_companion_inside_the_release_commit_is_refused(self):
        release = self.release()
        destination = release.author / COMPANION
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(release.record()))
        git(release.author, "add", COMPANION)
        git(release.author, "commit", "-q", "--amend", "--no-edit")      # folded into the release commit
        release.r0003 = git(release.author, "rev-parse", "HEAD")
        runtime, django = release.verdicts()
        self.assertEqual((runtime[0], django[0]), ("rejected", "rejected"))


class ExecutionOrderTests(SimpleTestCase):
    def test_migrations_run_before_unit_activation_and_restarts(self):
        """Executor.execute: guarded migrations -> database_verified -> source ->
        systemd reconcile (enable --now of the new worker) -> declared restarts
        (Monitoring). The check therefore exists before the worker is started and
        before the restarted Monitoring first polls it."""
        source = inspect.getsource(executor_module.Executor.execute)
        order = [source.index(marker) for marker in (
            'self.store.milestone(job_id, "migration_started")', 'self.store.milestone(job_id, "database_verified")',
            'self.store.milestone(job_id, "source_advanced")', "self.systemd.reconcile(staged.source_root, plan)",
            "self.systemd.restart_declared((service,))")]
        self.assertEqual(order, sorted(order))
