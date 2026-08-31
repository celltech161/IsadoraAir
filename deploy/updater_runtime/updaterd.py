#!/usr/bin/python3
"""Installed entry point; production runs this file only from protected storage.

Update Center Phase D, D4-B/D5.1: the SAME file serves three launch shapes --
`--config` alone (the ordinary pre-Phase-D worker), `--config` plus the
three active A/B generation identity arguments, or `--config` plus all four
candidate identity arguments
(`--expected-slot`, `--expected-generation`, `--expected-descriptor-
sha256`, `--expected-job-uuid`), which is exactly what the supervisor's
own launch.launch_worker() invokes for a real Phase-D handoff -- fixed
argument NAMES only, never an arbitrary path/command (see launch.py's
own docstring: `/usr/bin/python3 -I <slot>/updaterd.py --config <path>
[--expected-slot ... --expected-generation ... --expected-descriptor-
sha256 ... [--expected-job-uuid ...]]`, nothing else is ever on this
argv)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

# ``-I`` deliberately removes the script directory from sys.path.  Re-add only
# this entry point's own canonical directory; production verifies below that it
# is the exact root-owned installation directory, never an application path.
_ENTRY_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ENTRY_ROOT))


_PROTECTED_INSTALL_ROOT = Path("/usr/local/libexec/isadoraair-updater")
_SLOT_RE = re.compile(r"^[AB]$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _assert_root_owned_unwritable_ancestry(path: Path) -> None:
    current = Path("/")
    for part in path.parts[1:]:
        current = current / part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError(f"protected updater parent path is unsafe: {current}")


def _assert_protected_install(*, expected_slot: str | None):
    """D4-B step 1: verify it is executing from the expected slot --
    two-phase, since WHICH slots_root the supervisor actually
    configured is not yet knowable (reading it requires trusting root-
    owned config, which itself requires this check to already have
    passed). Phase one (always): every ancestor of _ENTRY_ROOT is
    root-owned, non-symlink, non-group/world-writable, and _ENTRY_ROOT
    itself is either the FIXED pre-Phase-D install path, or (candidate
    launch only) a directory whose own basename is exactly the
    expected slot letter. Phase two (candidate launch only, in main()
    after config loads) cross-checks _ENTRY_ROOT.parent against the
    supervisor's own configured phase_d_supervisor_slots_root."""
    if expected_slot is None:
        if _ENTRY_ROOT != _PROTECTED_INSTALL_ROOT:
            raise RuntimeError("updater daemon refuses to execute outside its protected installation path")
    elif _ENTRY_ROOT.name != expected_slot:
        raise RuntimeError(
            f"updater daemon refuses to execute: its own directory name {_ENTRY_ROOT.name!r} "
            f"does not match the expected slot {expected_slot!r}"
        )
    _assert_root_owned_unwritable_ancestry(_ENTRY_ROOT)
    checked = [_ENTRY_ROOT, _ENTRY_ROOT / "updaterd.py", _ENTRY_ROOT / "isadoraair_updater"]
    checked.extend((_ENTRY_ROOT / "isadoraair_updater").glob("*.py"))
    for path in checked:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError(f"protected updater installation ownership/mode is unsafe: {path.name}")
        if path.suffix == ".py" and not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"protected updater module is not a regular file: {path.name}")


def _assert_descriptor_identity(*, expected_slot: str, expected_descriptor_sha256: str) -> None:
    """D4-B step 2: verify its own descriptor identity -- reads the
    descriptor bytes the OLD worker staged at the fixed, convention-
    based sibling path (runtime_handoff.descriptor_staging_path's own
    convention: slots_root/.staging/descriptor-<slot>.json, where
    slots_root is _ENTRY_ROOT.parent -- this candidate's own directory
    IS slots_root/<expected_slot> by construction, proven by
    _assert_protected_install() immediately above) and compares its
    sha256 against what THIS process was launched expecting. Pure
    stdlib, no isadoraair_updater import yet -- this check, like the
    installation-safety check above it, must be trustworthy before
    trusting anything the protected package itself would do."""
    slots_root = _ENTRY_ROOT.parent
    descriptor_path = slots_root / ".staging" / f"descriptor-{expected_slot}.json"
    try:
        descriptor_bytes = descriptor_path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"updater daemon cannot read its own expected descriptor: {exc}") from exc
    actual = hashlib.sha256(descriptor_bytes).hexdigest()
    if actual != expected_descriptor_sha256:
        raise RuntimeError(
            "updater daemon refuses to execute: its own staged descriptor does not match "
            "the descriptor SHA-256 it was launched expecting"
        )


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--config", default="/etc/isadoraair/station.json")
    # D4-B's own minimum supervisor-controlled candidate-handoff
    # startup identity fields. All four, or none -- never a partial
    # set (checked below). Fixed, closed shapes only: a slot letter,
    # a positive integer, a hex digest, a canonical UUID -- there is
    # no path/command/argv field here, and there never will be one.
    parser.add_argument("--expected-slot", choices=("A", "B"), default=None)
    parser.add_argument("--expected-generation", type=int, default=None)
    parser.add_argument("--expected-descriptor-sha256", default=None)
    parser.add_argument("--expected-job-uuid", default=None)
    arguments = parser.parse_args(argv)

    slot_identity = (
        arguments.expected_slot, arguments.expected_generation,
        arguments.expected_descriptor_sha256,
    )
    if not (all(value is None for value in slot_identity)
            or all(value is not None for value in slot_identity)):
        parser.error(
            "--expected-slot/--expected-generation/--expected-descriptor-sha256 "
            "must be given all together or not at all"
        )
    if arguments.expected_job_uuid is not None and arguments.expected_slot is None:
        parser.error("--expected-job-uuid requires a complete slot identity")
    if arguments.expected_generation is not None:
        if arguments.expected_generation < 1:
            parser.error("--expected-generation must be a positive integer")
        if not _SHA256_RE.fullmatch(arguments.expected_descriptor_sha256 or ""):
            parser.error("--expected-descriptor-sha256 must be exactly 64 lowercase hex characters")
        if (arguments.expected_job_uuid is not None
                and not _UUID_RE.fullmatch(arguments.expected_job_uuid)):
            parser.error("--expected-job-uuid must be a canonical lowercase UUID")
    return arguments


def main() -> int:
    arguments = parse_args()
    if os.geteuid() != 0:
        sys.stderr.write("updaterd: the protected updater daemon must run as root\n")
        return 1
    _assert_protected_install(expected_slot=arguments.expected_slot)
    is_slot_worker = arguments.expected_slot is not None
    is_candidate = arguments.expected_job_uuid is not None
    if is_slot_worker:
        _assert_descriptor_identity(
            expected_slot=arguments.expected_slot,
            expected_descriptor_sha256=arguments.expected_descriptor_sha256,
        )
    # Import privileged implementation only after the installation path and
    # every package file have been proven root-owned and application-unwritable.
    from isadoraair_updater.config import load_config
    from isadoraair_updater.daemon import UpdaterDaemon
    from protected_bootstrap.policy import parse_policy_dict

    config = load_config(Path(arguments.config), enforce_protection=True)
    active_policy = None
    if is_slot_worker:
        # Phase two of D4-B step 1: now that root-owned config is
        # trusted, cross-check the supervisor's OWN configured
        # slots_root against where this process is ACTUALLY running
        # from -- _ENTRY_ROOT.parent must be exactly that path, never
        # merely "a directory named A or B somewhere."
        if config.phase_d_supervisor_slots_root is None or _ENTRY_ROOT.parent != config.phase_d_supervisor_slots_root:
            sys.stderr.write(
                "updaterd: refuses to execute as a Phase-D candidate: its own directory is not "
                "inside this station's configured phase_d_supervisor_slots_root\n"
            )
            return 1
        try:
            policy_data = json.loads((_ENTRY_ROOT / "protected-policy.json").read_text(encoding="utf-8"))
            active_policy = parse_policy_dict(policy_data, label="active protected-policy.json")
        except (OSError, UnicodeError, ValueError) as exc:
            sys.stderr.write(f"updaterd: invalid active protected policy: {exc}\n")
            return 1
    if is_candidate:
        daemon = UpdaterDaemon(
            config, expected_slot=arguments.expected_slot,
            expected_handoff_generation=arguments.expected_generation,
            expected_handoff_descriptor_sha256=arguments.expected_descriptor_sha256,
            expected_resumable_job_uuid=arguments.expected_job_uuid,
            active_policy=active_policy,
        )
    else:
        daemon = UpdaterDaemon(config, active_policy=active_policy)
    try:
        daemon.serve_forever()
    finally:
        daemon.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
