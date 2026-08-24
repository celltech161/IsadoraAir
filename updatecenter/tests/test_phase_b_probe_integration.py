import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from django.db import connection
from django.test import SimpleTestCase, TestCase

from .phase_b_helpers import PROJECT_ROOT, config_dict
from isadoraair_updater.checkpoint import create_checkpoint, verify_checkpoint
from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.process import CommandRunner


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
