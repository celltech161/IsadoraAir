import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from .phase_b_helpers import PROJECT_ROOT, config_dict
from isadoraair_updater.checkpoint import create_checkpoint, verify_checkpoint
from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.executor import Executor, _strict_probe
from isadoraair_updater.process import CommandRunner
from updatecenter.management.commands.updatecenter_probe import build_probe_payload


class TargetSourceRealGraphTests(SimpleTestCase):
    def test_target_only_migration_is_discovered_from_target_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "current"
            target = root / "target"
            (current / "sample" / "migrations").mkdir(parents=True)
            (current / "updatecenter" / "management" / "commands").mkdir(parents=True)
            for package in (
                current / "sample", current / "sample" / "migrations",
                current / "updatecenter", current / "updatecenter" / "management",
                current / "updatecenter" / "management" / "commands",
            ):
                (package / "__init__.py").touch()
            (current / "manage.py").write_text(
                "import os,sys\nos.environ.setdefault('DJANGO_SETTINGS_MODULE','settings')\n"
                "from django.core.management import execute_from_command_line\nexecute_from_command_line(sys.argv)\n",
                encoding="utf-8",
            )
            database = root / "db.sqlite3"
            (current / "settings.py").write_text(
                "SECRET_KEY='test'\nINSTALLED_APPS=['django.contrib.contenttypes','sample','updatecenter']\n"
                f"DATABASES={{'default':{{'ENGINE':'django.db.backends.sqlite3','NAME':r'{database}'}}}}\n"
                "DEFAULT_AUTO_FIELD='django.db.models.BigAutoField'\n",
                encoding="utf-8",
            )
            (current / "sample" / "models.py").write_text("from django.db import models\nclass Item(models.Model):\n    name=models.CharField(max_length=20)\n", encoding="utf-8")
            (current / "sample" / "migrations" / "0001_initial.py").write_text(
                "from django.db import migrations,models\nclass Migration(migrations.Migration):\n"
                "    initial=True\n    dependencies=[]\n    operations=[migrations.CreateModel(name='Item',fields=[('id',models.BigAutoField(primary_key=True,serialize=False)),('name',models.CharField(max_length=20))])]\n",
                encoding="utf-8",
            )
            shutil.copy2(
                PROJECT_ROOT / "updatecenter" / "management" / "commands" / "updatecenter_probe.py",
                current / "updatecenter" / "management" / "commands" / "updatecenter_probe.py",
            )
            env = {
                "PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(current),
                "DJANGO_SETTINGS_MODULE": "settings", "PYTHONDONTWRITEBYTECODE": "1",
            }
            subprocess.run([sys.executable, str(current / "manage.py"), "migrate", "--noinput"], cwd=current, env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            current_probe = subprocess.run(
                [sys.executable, str(current / "manage.py"), "updatecenter_probe", "--skip-checks"],
                cwd=current, env=env, check=True, stdout=subprocess.PIPE, text=True,
            )
            self.assertEqual(json.loads(current_probe.stdout)["plan"], [])

            shutil.copytree(current, target)
            (target / "sample" / "migrations" / "0002_add_note.py").write_text(
                "from django.db import migrations,models\nclass Migration(migrations.Migration):\n"
                "    dependencies=[('sample','0001_initial')]\n"
                "    operations=[migrations.AddField(model_name='item',name='note',field=models.TextField(null=True))]\n",
                encoding="utf-8",
            )
            target_env = dict(env, PYTHONPATH=str(target))
            target_probe = subprocess.run(
                [sys.executable, str(target / "manage.py"), "updatecenter_probe", "--skip-checks"],
                cwd=target, env=target_env, check=True, stdout=subprocess.PIPE, text=True,
            )
            payload = json.loads(target_probe.stdout)
            self.assertEqual([item["ref"] for item in payload["plan"]], ["sample.0002_add_note"])
            self.assertEqual(payload["plan"][0]["operations"][0]["classification"], "additive")


class R0011ProspectiveR0012TargetSchemaTests(TransactionTestCase):
    """Exercise the real incident migration through the staged-target contract."""

    migration_ref = "monitoring.0011_transmitter_vendor_and_password"

    def test_r0012_probe_accepts_and_applies_r0011_from_supported_baselines(self):
        executor = MigrationExecutor(connection)
        latest_targets = executor.loader.graph.leaf_nodes()
        r0010_targets = [
            ("monitoring", "0010_alter_listenerpeak_tlh_since_at")
            if app_label == "monitoring"
            else (app_label, migration_name)
            for app_label, migration_name in latest_targets
        ]

        try:
            # Represent the schema still installed at r0010. The corrected
            # probe is imported from this staged post-r0011/r0012 source.
            executor.migrate(r0010_targets)
            target_payload = _strict_probe(
                json.dumps(build_probe_payload()).encode("utf-8")
            )
            migration = next(
                item
                for item in target_payload["plan"]
                if item["ref"] == self.migration_ref
            )
            self.assertEqual(len(migration["operations"]), 7)
            self.assertEqual(
                {item["classification"] for item in migration["operations"]},
                {"additive"},
            )

            executor_validator = object.__new__(Executor)
            aggregate_paths = (
                ("r0010", ("r0011", "r0012")),
                ("r0009", ("r0010", "r0011", "r0012")),
                ("r0007", ("r0008", "r0009", "r0010", "r0011", "r0012")),
            )
            for installed_release, releases_in_plan in aggregate_paths:
                with self.subTest(installed_release=installed_release):
                    plan = SimpleNamespace(
                        installed_release_id=installed_release,
                        target_release_id="r0012",
                        releases_in_plan=releases_in_plan,
                        migrations_required=(self.migration_ref,),
                        migration_compatibility="additive",
                    )
                    actual = executor_validator._validate_target_schema(
                        plan,
                        target_payload,
                        {"applied": target_payload["applied"]},
                        migration_already_started=False,
                    )
                    self.assertEqual(actual, (self.migration_ref,))

            # Complete the isolated transition and prove the real migration
            # becomes applied with no target-schema work left pending.
            MigrationExecutor(connection).migrate(latest_targets)
            after_payload = build_probe_payload()
            self.assertIn(self.migration_ref, after_payload["applied"])
            self.assertNotIn(
                self.migration_ref,
                [item["ref"] for item in after_payload["plan"]],
            )
        finally:
            # A failed assertion must not leave the shared test database at an
            # old schema for later test classes.
            MigrationExecutor(connection).migrate(latest_targets)


class RealPostgresCheckpointTests(TestCase):
    def test_pg_dump_checkpoint_against_isolated_django_test_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = connection.settings_dict
            raw = config_dict(root, str(root / "upstream.git"))
            raw["database"] = {
                "name": settings["NAME"], "user": settings["USER"],
                "host": settings["HOST"], "port": int(settings["PORT"]),
                "pgpass_file": None,
            }
            Path(raw["application_environment_file"]).write_text(
                f"SECRET_KEY=test\nDB_NAME={settings['NAME']}\nDB_USER={settings['USER']}\n"
                f"DB_PASSWORD={settings['PASSWORD']}\nDB_HOST={settings['HOST']}\nDB_PORT={settings['PORT']}\n",
                encoding="utf-8",
            )
            config = validate_config_dict(raw, allow_local_repository=True)
            metadata = create_checkpoint(
                config, CommandRunner(), job_id="123e4567-e89b-42d3-a456-426614174000",
                installed_release="r0002", installed_commit="a" * 40,
                target_release="r0003", target_commit="b" * 40,
            )
            self.assertTrue(verify_checkpoint(config.checkpoint_root, metadata))
            self.assertGreater(metadata["size_bytes"], 100)
