#!/usr/bin/env python3
"""Restore management authority: run a Django management command with THIS
checkout's code, never the restored backup's own copy.

Stages 50 (native fdkaac), 70 (TTS), 75 (protected updater), and 90 (E5
system surfaces) all REPAIR or PROVISION runtime state by invoking a
`manage.py` management command -- but the application tree being repaired
(`$RESTORE_TARGET_ROOT`, reconstructed from the backup's exact recorded Git
SHA, see 20-application.sh) can be an intentionally OLDER, compatible
release than the restore tooling currently performing the repair. A newer
restore checkout must be able to fix an older backup's broken
runtime-recovery code without ever delegating the repair itself back to
that same broken code -- see docs/DISASTER_RECOVERY_RESTORE.md.

This stdlib-only helper (matching runtime_recovery_archive.py's own
"stdlib-only, orchestration glue" convention) is the one place that split
is enforced, so it is enforced identically everywhere rather than
re-implemented per stage script:

  * Executable source authority -- always THIS checkout (--repo-root),
    specifically <repo-root>/manage.py and everything it imports. The
    restored target's own manage.py/isadoraair package is NEVER executed
    by this helper, no matter how it is invoked.

  * Python interpreter -- the RESTORED target's venv
    (<target-root>/venv/bin/python), which already has Django and every
    other runtime dependency 60-python.sh installed for it there. This is
    reasonable ONLY if that venv actually satisfies THIS checkout's own
    requirements.txt -- so that is verified, exactly (pinned versions
    only), before anything Django-related is imported. On any
    missing/mismatched pin this exits nonzero and NEVER falls back to
    <target-root>/manage.py -- fail closed, no silent downgrade to the
    backup's own recovery code.

  * Configuration/secrets -- relayed from <target-root>/.env into the
    real OS environment before exec, because python-decouple's config()
    always checks os.environ before any file it discovers (see
    isadoraair/settings.py's own DB_NAME comment, and
    deploy/restore/lib.sh's restore_parse_common_args). That makes the
    RESTORED station's own configuration authoritative for the command
    about to run without ever copying .env into this checkout, and
    without decouple's own caller-path file search (which walks upward
    from wherever isadoraair/settings.py physically lives -- i.e. THIS
    checkout, once manage.py is executed from --repo-root) ever getting a
    chance to wander off and find an unrelated developer/sandbox .env
    instead. Any variable the caller's shell already exported (e.g.
    lib.sh's own DB_NAME staging override) is left untouched -- exactly
    the same "os.environ wins over any file" precedence decouple itself
    uses, applied one layer earlier.

  * Target filesystem -- never touched by this helper itself. --target-
    root is consulted only to read its venv interpreter and its .env; the
    actual stage-specific target/fake-root/publish-root options are the
    calling stage script's own concern, forwarded verbatim as manage.py
    arguments.

Usage:
  restore_manage.py --repo-root PATH --target-root PATH -- CMD [ARGS...]
  restore_manage.py --repo-root PATH --target-root PATH --print-exec-argv -- CMD [ARGS...]

--print-exec-argv performs every check and the full .env relay, but
prints the resolved argv (and the set of relayed variable NAMES -- never
values) instead of exec'ing. Testing/debugging only.
"""

from __future__ import annotations

import argparse
import importlib.metadata as importlib_metadata
import os
import re
import shlex
import sys
from pathlib import Path

_NAME_NORMALIZE_RE = re.compile(r"[-_.]+")
_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;]+)")


def _normalize_name(name: str) -> str:
    return _NAME_NORMALIZE_RE.sub("-", name).lower()


def parse_dotenv(path: Path) -> dict[str, str]:
    """Parse a dotenv file with EXACTLY python-decouple's own RepositoryEnv
    semantics (see decouple.RepositoryEnv.__init__) -- byte-for-byte the
    same rules, so relaying the result into os.environ produces identical
    config() results to decouple discovering this exact file itself."""

    data: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and (
                (value[0] == "'" and value[-1] == "'") or (value[0] == '"' and value[-1] == '"')
            ):
                value = value[1:-1]
            data[key] = value
    return data


def parse_pinned_requirements(path: Path) -> dict[str, str]:
    """Extract exact `Name==Version` pins. Unpinned/other requirement
    forms are intentionally ignored -- this checkout's requirements.txt
    only ever uses exact pins (see 60-python.sh's own docstring)."""

    pins: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = _PIN_RE.match(line)
            if match:
                pins[match.group(1)] = match.group(2)
    return pins


def check_compatibility(pinned: dict[str, str]) -> list[str]:
    """Compare `pinned` against what is importable in the CURRENTLY
    RUNNING interpreter (i.e. whichever python actually executed this
    script) via importlib.metadata. Returns a list of human-readable
    problem descriptions -- package names and version numbers only,
    never anything from .env -- empty means fully compatible."""

    problems: list[str] = []
    for name, expected in sorted(pinned.items()):
        actual: str | None = None
        for candidate in {name, _normalize_name(name)}:
            try:
                actual = importlib_metadata.version(candidate)
                break
            except importlib_metadata.PackageNotFoundError:
                continue
        if actual is None:
            problems.append(f"{name}: this checkout requires =={expected}, not installed in the restored venv")
        elif actual != expected:
            problems.append(f"{name}: this checkout requires =={expected}, restored venv has =={actual}")
    return problems


def merge_env_from_dotenv(dotenv: dict[str, str], environ: "os._Environ[str]") -> list[str]:
    """Inject every dotenv key NOT already present in `environ`, exactly
    mirroring decouple's own os.environ-first precedence one layer
    earlier -- an already-exported override (e.g. lib.sh's DB_NAME
    staging override) always wins. Returns the list of keys actually
    injected (names only, for non-secret diagnostics)."""

    injected = []
    for key, value in dotenv.items():
        if key not in environ:
            environ[key] = value
            injected.append(key)
    return injected


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="restore_manage.py", allow_abbrev=False)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--target-root", required=True, type=Path)
    parser.add_argument(
        "--print-exec-argv",
        action="store_true",
        help="Print the resolved argv instead of executing it. Testing/debugging only.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    if "--" in raw_argv:
        separator = raw_argv.index("--")
        own_args, forwarded = raw_argv[:separator], raw_argv[separator + 1 :]
    else:
        own_args, forwarded = raw_argv, []

    args = _parser().parse_args(own_args)

    if not forwarded:
        print("restore_manage.py: no manage.py command given after '--'.", file=sys.stderr)
        return 2

    repo_root = args.repo_root.resolve()
    target_root = args.target_root.resolve()

    manage_py = repo_root / "manage.py"
    if not manage_py.is_file():
        print(
            f"restore_manage.py: {manage_py} not found -- --repo-root must be a real IsadoraAir checkout.",
            file=sys.stderr,
        )
        return 2

    requirements_path = repo_root / "requirements.txt"
    if not requirements_path.is_file():
        print(f"restore_manage.py: {requirements_path} not found.", file=sys.stderr)
        return 2

    env_path = target_root / ".env"
    if not env_path.is_file():
        print(
            f"restore_manage.py: {env_path} not found -- run 20-application.sh first.",
            file=sys.stderr,
        )
        return 2

    pinned = parse_pinned_requirements(requirements_path)
    problems = check_compatibility(pinned)
    if problems:
        print(
            "restore_manage.py: the restored target's Python environment is not compatible "
            f"with this checkout's requirements.txt -- refusing to fall back to {target_root}/manage.py:",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 3

    dotenv = parse_dotenv(env_path)
    injected = merge_env_from_dotenv(dotenv, os.environ)

    exec_argv = [sys.executable, str(manage_py), *forwarded]

    if args.print_exec_argv:
        print(" ".join(shlex.quote(part) for part in exec_argv))
        print(f"env keys relayed from {env_path}: {', '.join(sorted(injected)) or '(none)'}")
        return 0

    os.execv(sys.executable, exec_argv)
    return 1  # unreachable if execv succeeds -- kept only to satisfy static analysis


if __name__ == "__main__":
    raise SystemExit(main())
