#!/usr/bin/env python3
"""Machine-readable backup-v3 classification and safe recovery extraction.

This stdlib-only helper is orchestration glue.  It does not validate or
provision E3/E4 payloads; Django's Runtime Foundation E authorities retain
those responsibilities.  It owns the outer archive class, extraction safety,
and the small durable receipt consumed by final restore acceptance.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


METADATA_NAME = "runtime-recovery-archive.json"
SCHEMA_VERSION = 1
SELF_CONTAINED_CLASS = "self_contained_v3"
LEGACY_CLASS = "legacy_non_self_contained"
V3_FORMAT = "3.0.0"
LEGACY_FORMAT = "2.1.0"
COMPONENTS = frozenset({"kokoro", "piper", "native_fdkaac"})
MAX_METADATA_BYTES = 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PAYLOAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ArchiveContractError(ValueError):
    pass


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _strict_metadata(value: object) -> dict:
    if not isinstance(value, dict):
        raise ArchiveContractError("archive recovery metadata must be a JSON object")
    required = {
        "schema_version",
        "backup_script_version",
        "archive_format_version",
        "recovery_class",
        "payload_included",
        "payload_id",
        "payload_schema_version",
        "product_contract_sha256",
        "included_components",
        "required_components",
        "policy_satisfied",
        "piper_freshness",
    }
    if set(value) != required:
        raise ArchiveContractError("archive recovery metadata fields do not match schema 1")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ArchiveContractError("unsupported archive recovery metadata schema")
    if not isinstance(value["backup_script_version"], str) or not value["backup_script_version"]:
        raise ArchiveContractError("backup script version must be a non-empty string")
    if not isinstance(value["payload_included"], bool):
        raise ArchiveContractError("payload_included must be boolean")
    if value["policy_satisfied"] not in (True, False, None):
        raise ArchiveContractError("policy_satisfied must be boolean or null")
    included = value["included_components"]
    required_components = value["required_components"]
    if not isinstance(included, list) or not isinstance(required_components, list):
        raise ArchiveContractError("archive component fields must be lists")
    if not all(isinstance(item, str) for item in (*included, *required_components)):
        raise ArchiveContractError("archive component names must be strings")
    if included != sorted(set(included)) or required_components != sorted(set(required_components)):
        raise ArchiveContractError("archive component fields must be sorted and unique")
    unknown = (set(included) | set(required_components)) - COMPONENTS
    if unknown:
        raise ArchiveContractError(f"unknown archive recovery component(s): {', '.join(sorted(unknown))}")
    if value["payload_included"]:
        if not isinstance(value["payload_id"], str) or not _PAYLOAD_ID_RE.fullmatch(value["payload_id"]):
            raise ArchiveContractError("included payload has invalid identity")
        if value["payload_schema_version"] != 1:
            raise ArchiveContractError("included payload has unsupported schema version")
        if not isinstance(value["product_contract_sha256"], str) or not _SHA256_RE.fullmatch(value["product_contract_sha256"]):
            raise ArchiveContractError("included payload has invalid product-contract digest")
        if value["piper_freshness"] not in ("current", "stale", "not_checked"):
            raise ArchiveContractError("included payload has invalid Piper freshness")
    elif any(
        value[field] is not None
        for field in ("payload_id", "payload_schema_version", "product_contract_sha256", "piper_freshness")
    ) or included:
        raise ArchiveContractError("absent payload cannot declare payload identity/components")
    if value["recovery_class"] == SELF_CONTAINED_CLASS:
        if value["archive_format_version"] != V3_FORMAT:
            raise ArchiveContractError("self-contained recovery class must use archive format 3.0.0")
        if not value["payload_included"] or not required_components or value["policy_satisfied"] is not True:
            raise ArchiveContractError("self-contained v3 requires a payload and a satisfied non-empty policy")
        if not set(required_components) <= set(included):
            raise ArchiveContractError("self-contained v3 does not include every required component")
    elif value["recovery_class"] == LEGACY_CLASS:
        if value["archive_format_version"] != LEGACY_FORMAT:
            raise ArchiveContractError("legacy recovery class must use archive format 2.1.0")
    else:
        raise ArchiveContractError("unknown archive recovery class")
    return value


def _load_status(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArchiveContractError("backup recovery validation output is not JSON") from exc
    if not isinstance(value, dict):
        raise ArchiveContractError("backup recovery validation output must be an object")
    return value


def write_metadata(args: argparse.Namespace) -> int:
    status = _load_status(args.status_json)
    payload_included = bool(status.get("payload_id"))
    policy = status.get("policy") if payload_included else None
    required = policy.get("required", []) if isinstance(policy, dict) else []
    policy_satisfied = policy.get("satisfied") if isinstance(policy, dict) else None
    included: set[str] = set(status.get("tts_components") or [])
    native = (status.get("components") or {}).get("native_fdkaac") or {}
    if native.get("state") == "present":
        included.add("native_fdkaac")
    self_contained = (
        payload_included
        and bool(required)
        and policy_satisfied is True
        and set(required) <= included
    )
    metadata = _strict_metadata(
        {
            "schema_version": SCHEMA_VERSION,
            "backup_script_version": args.script_version,
            "archive_format_version": V3_FORMAT if self_contained else LEGACY_FORMAT,
            "recovery_class": SELF_CONTAINED_CLASS if self_contained else LEGACY_CLASS,
            "payload_included": payload_included,
            "payload_id": status.get("payload_id") if payload_included else None,
            "payload_schema_version": status.get("schema_version") if payload_included else None,
            "product_contract_sha256": status.get("product_contract_sha256") if payload_included else None,
            "included_components": sorted(included),
            "required_components": sorted(required),
            "policy_satisfied": policy_satisfied,
            "piper_freshness": ((status.get("piper_freshness") or {}).get("state") if payload_included else None),
        }
    )
    _atomic_json(args.output, metadata)
    print(json.dumps(metadata, sort_keys=True, separators=(",", ":")))
    return 0


def _normalized_name(name: str) -> PurePosixPath:
    while name.startswith("./"):
        name = name[2:]
    candidate = PurePosixPath(name)
    if not name or name.startswith("/") or "\\" in name or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ArchiveContractError(f"unsafe archive member path: {name!r}")
    return candidate


def _metadata_from_tar(archive: Path) -> dict | None:
    matches: list[tarfile.TarInfo] = []
    with tarfile.open(archive, "r:gz") as source:
        for member in source.getmembers():
            try:
                name = _normalized_name(member.name)
            except ArchiveContractError:
                continue
            if name == PurePosixPath(METADATA_NAME):
                matches.append(member)
        if not matches:
            return None
        if len(matches) != 1 or not matches[0].isreg() or matches[0].size > MAX_METADATA_BYTES:
            raise ArchiveContractError("archive recovery metadata member is duplicate, non-regular, or too large")
        stream = source.extractfile(matches[0])
        if stream is None:
            raise ArchiveContractError("archive recovery metadata could not be read")
        try:
            value = json.loads(stream.read(MAX_METADATA_BYTES + 1).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ArchiveContractError("archive recovery metadata is invalid JSON") from exc
    return _strict_metadata(value)


def inspect_archive(args: argparse.Namespace) -> int:
    metadata = _metadata_from_tar(args.archive)
    if metadata is None:
        print("LEGACY ARCHIVE -- no machine-readable Runtime Foundation E recovery metadata", file=sys.stderr)
        return 2
    print(json.dumps(metadata, sort_keys=True, separators=(",", ":")))
    return 0


def extract_payload(args: argparse.Namespace) -> int:
    metadata = _metadata_from_tar(args.archive)
    if metadata is None:
        print("LEGACY ARCHIVE -- NOT SELF-CONTAINED FOR FOUNDATION E", file=sys.stderr)
        return 2
    if metadata["recovery_class"] != SELF_CONTAINED_CLASS:
        print("LEGACY/NON-SELF-CONTAINED ARCHIVE -- NOT SELF-CONTAINED FOR FOUNDATION E", file=sys.stderr)
        return 3
    if args.destination.exists():
        raise ArchiveContractError(f"extraction destination already exists: {args.destination}")
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{args.destination.name}.extract-", dir=args.destination.parent))
    try:
        seen: set[PurePosixPath] = set()
        extracted = 0
        with tarfile.open(args.archive, "r:gz") as source:
            for member in source.getmembers():
                name = _normalized_name(member.name)
                if not name.parts or name.parts[0] != "runtime-recovery":
                    continue
                relative = PurePosixPath(*name.parts[1:])
                if not relative.parts:
                    if not member.isdir():
                        raise ArchiveContractError("runtime-recovery archive root must be a directory")
                    continue
                if relative in seen:
                    raise ArchiveContractError(f"duplicate runtime recovery archive member: {relative}")
                seen.add(relative)
                destination = temporary.joinpath(*relative.parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=False)
                    os.chmod(destination, 0o755)
                elif member.isreg():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    descriptor = os.open(
                        destination,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                        0o644,
                    )
                    stream = source.extractfile(member)
                    if stream is None:
                        os.close(descriptor)
                        raise ArchiveContractError(f"could not read archive member: {name}")
                    with os.fdopen(descriptor, "wb") as output:
                        shutil.copyfileobj(stream, output, length=1024 * 1024)
                    os.chmod(destination, 0o644)
                    extracted += 1
                else:
                    raise ArchiveContractError(
                        f"runtime recovery archive member is not a directory or regular file: {name}"
                    )
        if not extracted or not (temporary / "runtime-recovery.json").is_file():
            raise ArchiveContractError("runtime recovery extraction did not produce its manifest")
        os.replace(temporary, args.destination)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    print(json.dumps(metadata, sort_keys=True, separators=(",", ":")))
    return 0


def record_components(args: argparse.Namespace) -> int:
    metadata = _metadata_from_tar(args.archive)
    if metadata is None or metadata["recovery_class"] != SELF_CONTAINED_CLASS:
        raise ArchiveContractError("cannot record recovery against a non-self-contained archive")
    requested = set(args.component)
    if not requested or requested - COMPONENTS or not requested <= set(metadata["included_components"]):
        raise ArchiveContractError("receipt component is absent from archive recovery metadata")
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "archive_format_version": metadata["archive_format_version"],
        "payload_id": metadata["payload_id"],
        "recovered_components": [],
    }
    if args.receipt.exists():
        try:
            existing = json.loads(args.receipt.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArchiveContractError("existing runtime recovery receipt is invalid") from exc
        for key in ("schema_version", "archive_format_version", "payload_id"):
            if existing.get(key) != receipt[key]:
                raise ArchiveContractError("runtime recovery receipt belongs to a different archive/payload")
        existing_components = existing.get("recovered_components", [])
        if (
            not isinstance(existing_components, list)
            or not all(isinstance(item, str) for item in existing_components)
            or set(existing_components) - COMPONENTS
        ):
            raise ArchiveContractError("existing runtime recovery receipt has invalid components")
        receipt["recovered_components"] = existing_components
    receipt["recovered_components"] = sorted(set(receipt["recovered_components"]) | requested)
    _atomic_json(args.receipt, receipt)
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


def accept_restore(args: argparse.Namespace) -> int:
    metadata = _metadata_from_tar(args.archive)
    if metadata is None or metadata["recovery_class"] != SELF_CONTAINED_CLASS:
        print("LEGACY ARCHIVE -- NOT SELF-CONTAINED FOR FOUNDATION E", file=sys.stderr)
        return 2
    try:
        receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        print("runtime recovery receipt is missing or invalid", file=sys.stderr)
        return 1
    if (
        receipt.get("schema_version") != SCHEMA_VERSION
        or receipt.get("archive_format_version") != metadata["archive_format_version"]
        or receipt.get("payload_id") != metadata["payload_id"]
    ):
        print("runtime recovery receipt does not match this archive/payload", file=sys.stderr)
        return 1
    recovered_value = receipt.get("recovered_components")
    if not isinstance(recovered_value, list) or not all(isinstance(item, str) for item in recovered_value):
        print("runtime recovery receipt has invalid components", file=sys.stderr)
        return 1
    recovered = set(recovered_value)
    missing = set(metadata["required_components"]) - recovered
    if missing:
        print(f"runtime recovery is incomplete; missing: {', '.join(sorted(missing))}", file=sys.stderr)
        return 1
    print(json.dumps({"accepted": True, "payload_id": metadata["payload_id"], "recovered_components": sorted(recovered)}, sort_keys=True, separators=(",", ":")))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(allow_abbrev=False)
    commands = root.add_subparsers(dest="command", required=True)
    write = commands.add_parser("write-metadata", allow_abbrev=False)
    write.add_argument("--status-json", required=True)
    write.add_argument("--script-version", required=True)
    write.add_argument("--output", required=True, type=Path)
    write.set_defaults(handler=write_metadata)
    inspect = commands.add_parser("inspect", allow_abbrev=False)
    inspect.add_argument("--archive", required=True, type=Path)
    inspect.set_defaults(handler=inspect_archive)
    extract = commands.add_parser("extract", allow_abbrev=False)
    extract.add_argument("--archive", required=True, type=Path)
    extract.add_argument("--destination", required=True, type=Path)
    extract.set_defaults(handler=extract_payload)
    record = commands.add_parser("record", allow_abbrev=False)
    record.add_argument("--archive", required=True, type=Path)
    record.add_argument("--receipt", required=True, type=Path)
    record.add_argument("--component", action="append", required=True, choices=sorted(COMPONENTS))
    record.set_defaults(handler=record_components)
    accept = commands.add_parser("accept", allow_abbrev=False)
    accept.add_argument("--archive", required=True, type=Path)
    accept.add_argument("--receipt", required=True, type=Path)
    accept.set_defaults(handler=accept_restore)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return args.handler(args)
    except (ArchiveContractError, OSError, tarfile.TarError) as exc:
        print(f"runtime recovery archive error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
