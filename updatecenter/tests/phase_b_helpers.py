from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import grp
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = PROJECT_ROOT / "deploy" / "updater_runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "Phase B Test", "GIT_AUTHOR_EMAIL": "phase-b@example.invalid",
             "GIT_COMMITTER_NAME": "Phase B Test", "GIT_COMMITTER_EMAIL": "phase-b@example.invalid"},
    )
    return result.stdout.strip()


def manifest(release_id: str, previous: str | None, *, bootstrap: str | None = None, **changes) -> dict:
    data = {
        "schema_version": 1,
        "release_id": release_id,
        "previous_release_id": previous,
        "minimum_updater_protocol_version": 1,
        "summary": "test release",
        "migrations_required": [],
        "migration_compatibility": None,
        "python_requirements_changed": False,
        "requirements_sha256": None,
        "apt_packages_new": [],
        "systemd_units_changed": [],
        "systemd_units_new_required": [],
        "systemd_units_new_optional": [],
        "systemd_units_removed_or_renamed": [],
        "collectstatic_required": False,
        "services_requiring_restart": [],
        "nginx_changed": False,
        "runtime_components_changed": False,
        "minimum_supported_release_id": None,
    }
    if bootstrap is not None:
        data["bootstrap_commit"] = bootstrap
    data.update(changes)
    return data


def create_release_repository(root: Path, *, third_release_changes: dict | None = None,
                              third_release_files: dict[str, str] | None = None):
    root.mkdir(parents=True, exist_ok=True)
    author = root / "author"
    upstream = root / "upstream.git"
    author.mkdir()
    git(author, "init", "-b", "main")
    (author / "README").write_text("baseline\n", encoding="utf-8")
    git(author, "add", "README")
    git(author, "commit", "-m", "baseline")
    bootstrap = git(author, "rev-parse", "HEAD")
    releases = author / "deploy" / "releases"
    releases.mkdir(parents=True)
    (releases / "r0001.json").write_text(json.dumps(manifest("r0001", None, bootstrap=bootstrap)), encoding="utf-8")
    git(author, "add", "deploy/releases/r0001.json")
    git(author, "commit", "-m", "introduce bootstrap manifest")
    (releases / "r0002.json").write_text(json.dumps(manifest("r0002", "r0001", minimum_supported_release_id="r0001")), encoding="utf-8")
    git(author, "add", "deploy/releases/r0002.json")
    git(author, "commit", "-m", "release r0002")
    r0002 = git(author, "rev-parse", "HEAD")
    changes = dict(third_release_changes or {})
    for relative, content in (third_release_files or {}).items():
        destination = author / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    migrations = changes.get("migrations_required", [])
    for ref in migrations:
        app, name = ref.split(".", 1)
        migration_root = author / app / "migrations"
        migration_root.mkdir(parents=True, exist_ok=True)
        (migration_root / "__init__.py").touch()
        (migration_root / f"{name}.py").write_text("# test migration\n", encoding="utf-8")
    for unit in (*changes.get("systemd_units_changed", []), *changes.get("systemd_units_new_required", []), *changes.get("systemd_units_new_optional", [])):
        deploy = author / "deploy"
        deploy.mkdir(exist_ok=True)
        (deploy / unit).write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")
    (releases / "r0003.json").write_text(json.dumps(manifest("r0003", "r0002", minimum_supported_release_id="r0002", **changes)), encoding="utf-8")
    git(author, "add", ".")
    git(author, "commit", "-m", "release r0003")
    r0003 = git(author, "rev-parse", "HEAD")
    subprocess.run(["git", "init", "--bare", str(upstream)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    git(author, "remote", "add", "origin", str(upstream))
    git(author, "push", "-u", "origin", "main")
    return author, upstream, bootstrap, r0002, r0003


def config_dict(root: Path, upstream: str) -> dict:
    app = root / "app"
    app.mkdir(exist_ok=True)
    env = app / ".env"
    env.write_text("SECRET_KEY=test-secret\nDB_NAME=test\nDB_USER=test\nDB_PASSWORD=test-password\n", encoding="utf-8")
    account = pwd.getpwuid(os.getuid())
    group = grp.getgrgid(os.getgid())
    return {
        "schema_version": 1,
        "trusted_repository_url": upstream,
        "trusted_branch": "main",
        "application_root": str(app),
        "application_user": account.pw_name,
        "application_group": group.gr_name,
        "application_environment_file": str(env),
        "trusted_repository": str(root / "protected" / "repository.git"),
        "jobs_root": str(root / "protected" / "jobs"),
        "logs_root": str(root / "protected" / "logs"),
        "staging_root": str(root / "protected" / "staging"),
        "checkpoint_root": str(root / "protected" / "checkpoints"),
        "socket_path": str(root / "run" / "updater.sock"),
        "systemd_unit_root": str(root / "protected" / "systemd"),
        "render_values": {
            "isa_user": account.pw_name,
            "isa_root": str(app),
            "isa_home": str(root / "home"),
            "syndicated_root": str(root / "home" / "syndicated"),
            "weather_root": str(root / "home" / "weather"),
            "ogremote_root": str(root / "home" / "ogremote"),
        },
        "database": {"name": "test", "user": "test", "host": "localhost", "port": 5432, "pgpass_file": None},
        "gunicorn_health_url": "http://127.0.0.1:8000/login/",
    }
