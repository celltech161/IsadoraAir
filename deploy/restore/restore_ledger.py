#!/usr/bin/env python3
"""Restore-session ledger -- IsadoraAir 1.2 Phase 4, r0043.

The FIRST implementation step toward a genuinely resumable/convergent
disaster-recovery restore. This stdlib-only helper owns one small,
narrowly-scoped durable JSON file (by default
``/var/lib/isadoraair/restore/ledger.json`` -- the SAME directory
``runtime_recovery_archive.py``'s own component receipt already lives
in, established by the same ``lib.sh`` helper) that binds a restore run
against a given ``--target-root`` to exactly one backup archive, and
records which numbered stages have durably completed against it.

This is deliberately NOT a general-purpose state machine or a second
copy of what each stage already verifies about its own output -- it is
the minimum identity/provenance ledger needed so that:

  - a stage can ask "did *I* already complete, for *this exact*
    archive, against *this exact* target root?" and get a precise,
    fail-closed answer (never a guess), and
  - a later stage (80-companions.sh) can ask "does durable evidence
    already prove an earlier stage's normalization already ran here,
    for this exact archive?" before treating any pre-existing content
    as safe-to-repair scaffold rather than fail-closed contamination.

Never stores secrets: only an archive's own SHA256 digest, its
basename, the recorded application Git SHA, an optional Runtime
Foundation E7B payload identity, and per-stage completion metadata
supplied by the calling stage script (itself never a secret -- see
each stage's own ``restore_ledger_record`` call).

Identity is exactly the pair (archive_sha256, target_root). Any
existing ledger whose identity does not match the CURRENT invocation's
--archive/--target-root is a hard, fail-closed error on every
operation here -- a different archive (or a different target root)
must never silently inherit a prior restore session's ledger. A
corrupt/unparseable/schema-invalid existing ledger is the same: fail
closed, never silently discarded and overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
_SHA256_RE_LEN = 64


class LedgerError(ValueError):
    """Any identity mismatch, corruption, or contract violation. Every
    caller treats this as fail-closed -- never a fallback to a fresh
    ledger, never a guess."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"), indent=None)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_existing(ledger_path: Path) -> dict | None:
    if not ledger_path.exists():
        return None
    try:
        raw = ledger_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LedgerError(f"existing restore ledger at {ledger_path} is unreadable/corrupt: {exc}") from None
    if not isinstance(data, dict):
        raise LedgerError(f"existing restore ledger at {ledger_path} is not a JSON object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise LedgerError(
            f"existing restore ledger at {ledger_path} has schema_version={data.get('schema_version')!r}, "
            f"expected {SCHEMA_VERSION} -- refusing to guess how to interpret it"
        )
    for required in ("archive_sha256", "target_root", "started_at", "updated_at", "stages"):
        if required not in data:
            raise LedgerError(f"existing restore ledger at {ledger_path} is missing required field {required!r}")
    if not isinstance(data["stages"], dict):
        raise LedgerError(f"existing restore ledger at {ledger_path} has a non-object 'stages' field")
    for stage_name, stage_value in data["stages"].items():
        if not isinstance(stage_value, dict) or stage_value.get("state") not in ("complete",):
            raise LedgerError(
                f"existing restore ledger at {ledger_path} has an invalid entry for stage {stage_name!r}"
            )
    return data


def _verify_identity(existing: dict, *, archive_sha256: str, target_root: str, ledger_path: Path) -> None:
    if existing["archive_sha256"] != archive_sha256:
        raise LedgerError(
            f"restore ledger at {ledger_path} belongs to a DIFFERENT archive "
            f"(recorded {existing['archive_sha256']}, this run is {archive_sha256}) -- "
            "a different archive must never silently inherit a prior restore session. "
            "If this is genuinely intentional (a fresh restore of this target root from a "
            "different backup), remove the stale ledger explicitly first -- never done "
            "automatically by this tool."
        )
    if existing["target_root"] != target_root:
        raise LedgerError(
            f"restore ledger at {ledger_path} belongs to a DIFFERENT target root "
            f"(recorded {existing['target_root']!r}, this run is {target_root!r})"
        )


def cmd_record(args: argparse.Namespace) -> int:
    archive_sha256 = sha256_of(args.archive)
    target_root = str(args.target_root)
    existing = _load_existing(args.ledger)
    if existing is None:
        ledger: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "archive_sha256": archive_sha256,
            "archive_basename": args.archive.name,
            "target_root": target_root,
            "git_sha": None,
            "payload_id": None,
            "product_contract_sha256": None,
            "started_at": _now(),
            "updated_at": _now(),
            "stages": {},
        }
    else:
        _verify_identity(existing, archive_sha256=archive_sha256, target_root=target_root, ledger_path=args.ledger)
        ledger = existing

    # Optional identity-adjacent fields (git_sha, payload_id,
    # product_contract_sha256) -- never secrets. Once recorded, a LATER
    # call supplying a DIFFERENT value for the same field is itself a
    # hard error: these must be consistent for the whole life of one
    # restore session, exactly like archive_sha256/target_root above.
    for field, value in (
        ("git_sha", args.git_sha),
        ("payload_id", args.payload_id),
        ("product_contract_sha256", args.product_contract_sha256),
    ):
        if value is None:
            continue
        if ledger.get(field) not in (None, value):
            raise LedgerError(
                f"restore ledger {field} mismatch: already recorded {ledger[field]!r}, this call supplied {value!r}"
            )
        ledger[field] = value

    detail: dict[str, Any] = {}
    if args.detail is not None:
        try:
            detail = json.loads(args.detail)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"--detail is not valid JSON: {exc}") from None
        if not isinstance(detail, dict):
            raise LedgerError("--detail must be a JSON object")

    ledger["stages"][args.stage] = {"state": "complete", "completed_at": _now(), "detail": detail}
    ledger["updated_at"] = _now()
    _atomic_json(args.ledger, ledger)
    print(json.dumps(ledger, sort_keys=True, separators=(",", ":")))
    return 0


def cmd_stage_state(args: argparse.Namespace) -> int:
    archive_sha256 = sha256_of(args.archive)
    target_root = str(args.target_root)
    existing = _load_existing(args.ledger)
    if existing is None:
        print("absent")
        return 0
    _verify_identity(existing, archive_sha256=archive_sha256, target_root=target_root, ledger_path=args.ledger)
    stage_value = existing["stages"].get(args.stage)
    print(stage_value["state"] if stage_value else "absent")
    return 0


def cmd_describe(args: argparse.Namespace) -> int:
    existing = _load_existing(args.ledger)
    if existing is None:
        print(json.dumps({"present": False}))
        return 0
    print(json.dumps({"present": True, **existing}, sort_keys=True, separators=(",", ":")))
    return 0


def cmd_verify_identity(args: argparse.Namespace) -> int:
    """Exit 0 if no ledger exists yet, OR an existing ledger's identity
    matches this exact archive/target-root. Exit 1 (clear stderr
    message) on any mismatch/corruption -- used by a stage that wants
    to fail fast before doing ANY work, without itself needing to know
    or care about any individual stage's completion state."""
    archive_sha256 = sha256_of(args.archive)
    target_root = str(args.target_root)
    existing = _load_existing(args.ledger)
    if existing is not None:
        _verify_identity(existing, archive_sha256=archive_sha256, target_root=target_root, ledger_path=args.ledger)
    return 0


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(allow_abbrev=False)
    commands = root.add_subparsers(dest="command", required=True)

    def _common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--ledger", required=True, type=Path)
        sub.add_argument("--archive", required=True, type=Path)
        sub.add_argument("--target-root", required=True)

    record = commands.add_parser("record", allow_abbrev=False)
    _common(record)
    record.add_argument("--stage", required=True)
    record.add_argument("--git-sha")
    record.add_argument("--payload-id")
    record.add_argument("--product-contract-sha256")
    record.add_argument("--detail")
    record.set_defaults(handler=cmd_record)

    stage_state = commands.add_parser("stage-state", allow_abbrev=False)
    _common(stage_state)
    stage_state.add_argument("--stage", required=True)
    stage_state.set_defaults(handler=cmd_stage_state)

    verify_identity = commands.add_parser("verify-identity", allow_abbrev=False)
    _common(verify_identity)
    verify_identity.set_defaults(handler=cmd_verify_identity)

    describe = commands.add_parser("describe", allow_abbrev=False)
    describe.add_argument("--ledger", required=True, type=Path)
    describe.set_defaults(handler=cmd_describe)

    return root


def main() -> int:
    args = build_parser().parse_args()
    if hasattr(args, "archive") and not args.archive.is_file():
        print(f"restore ledger: --archive does not exist: {args.archive}", file=sys.stderr)
        return 1
    try:
        return args.handler(args)
    except LedgerError as exc:
        print(f"restore ledger error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"restore ledger I/O error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
