#!/usr/bin/env python3
"""Collect and validate non-secret Stereo Tool disaster-recovery provenance.

This helper NEVER executes the proprietary Stereo Tool binary and NEVER
serializes .stereo_tool.rc contents.  The binary and runtime-state file are
identified only by path/presence/size/SHA-256 plus an embedded product-version
string when one can be found safely in the binary bytes.

The proprietary binary and plaintext runtime state remain external to the
ordinary DR archive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import re
import shlex
import sys
import tarfile
from pathlib import Path, PurePosixPath


SCHEMA_VERSION = 1
PRODUCT = "Thimeo Stereo Tool"
CLASSIFICATION = "external_proprietary"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_PATTERNS = (
    re.compile(rb"Thimeo Stereo Tool ([0-9]+\.[0-9]+) \(for Linux\)"),
    re.compile(rb"Audio processed by Stereo Tool ([0-9]+\.[0-9]+)"),
)


class ProvenanceError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_identity(path: Path) -> dict:
    if not path.is_file():
        return {
            "present": False,
            "size_bytes": None,
            "sha256": None,
        }
    stat_result = path.stat()
    return {
        "present": True,
        "size_bytes": stat_result.st_size,
        "sha256": sha256_file(path),
    }


def embedded_version(path: Path) -> tuple[str | None, str | None]:
    if not path.is_file() or path.stat().st_size == 0:
        return None, None

    with path.open("rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            for pattern in VERSION_PATTERNS:
                match = pattern.search(mapped)
                if match:
                    return (
                        match.group(1).decode("ascii"),
                        "embedded_product_string",
                    )

    return None, None


def execstart_binary(service_unit: Path) -> Path | None:
    if not service_unit.is_file():
        return None

    for raw_line in service_unit.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines():
        line = raw_line.strip()
        if not line.startswith("ExecStart="):
            continue

        value = line.split("=", 1)[1].strip()
        if not value:
            continue

        try:
            argv = shlex.split(value)
        except ValueError:
            continue

        if not argv:
            continue

        executable = argv[0]

        # systemd ExecStart supports command prefixes.  Strip only the
        # recognized prefix characters from the command token; this does not
        # execute or otherwise interpret the unit.
        executable = executable.lstrip("-+!@:")

        if executable.startswith("/"):
            return Path(executable)

    return None


def source_record(path: Path, *, bundled: bool) -> dict:
    record = file_identity(path)
    record.update(
        {
            "source_path": str(path),
            "bundled": bundled,
        }
    )
    return record


def collect(args: argparse.Namespace) -> int:
    service_file = Path(args.service_unit_file)
    fallback_binary = Path(args.fallback_binary)
    profile_dir = Path(args.profile_dir)
    profile_source_root = Path(args.profile_source_root)
    runtime_state = Path(args.runtime_state)
    output = Path(args.output)

    service_binary = execstart_binary(service_file)
    binary_path = service_binary or fallback_binary
    binary_path_source = (
        "service_execstart" if service_binary is not None else "fallback_path"
    )

    binary = source_record(binary_path, bundled=False)
    version, version_source = embedded_version(binary_path)
    binary.update(
        {
            "configured_path": str(binary_path),
            "path_source": binary_path_source,
            "reported_version": version,
            "reported_version_source": version_source,
        }
    )

    service = file_identity(service_file)
    service.update(
        {
            "source_path": args.service_unit_source_path,
            "archive_path": (
                "etc-live/stereotool.service"
                if service["present"]
                else None
            ),
            "bundled": bool(service["present"]),
        }
    )

    profiles = []
    if profile_dir.is_dir():
        for profile in sorted(
            (
                candidate
                for candidate in profile_dir.iterdir()
                if candidate.is_file()
                and candidate.suffix.lower() == ".sts"
            ),
            key=lambda candidate: candidate.name.casefold(),
        ):
            identity = file_identity(profile)
            profiles.append(
                {
                    "source_path": str(profile_source_root / profile.name),
                    "archive_path": f"stereotool/{profile.name}",
                    "present": True,
                    "size_bytes": identity["size_bytes"],
                    "sha256": identity["sha256"],
                    "bundled": True,
                }
            )

    runtime = source_record(runtime_state, bundled=False)
    runtime.update(
        {
            "secret_bearing": True,
            "contents_recorded": False,
        }
    )

    document = {
        "schema_version": SCHEMA_VERSION,
        "product": PRODUCT,
        "classification": CLASSIFICATION,
        "binary_bundled": False,
        "binary": binary,
        "service_unit": service,
        "profiles": profiles,
        "runtime_state": runtime,
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "binary_present": binary["present"],
                "output": str(output),
                "profiles": len(profiles),
                "reported_version": version,
                "runtime_state_present": runtime["present"],
                "schema_version": SCHEMA_VERSION,
                "service_unit_present": service["present"],
            },
            sort_keys=True,
        )
    )
    return 0


def normalize_member_name(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]

    if name in ("", "."):
        return ""

    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts:
        raise ProvenanceError(
            f"unsafe archive member path encountered: {name!r}"
        )
    return str(pure)


def member_map(tf: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    result: dict[str, tarfile.TarInfo] = {}

    for member in tf.getmembers():
        normalized = normalize_member_name(member.name)
        if not normalized:
            continue

        if normalized in result:
            raise ProvenanceError(
                f"duplicate normalized archive member: {normalized}"
            )
        result[normalized] = member

    return result


def read_member(
    tf: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    name: str,
    *,
    max_bytes: int | None = None,
) -> bytes:
    member = members.get(name)
    if member is None:
        raise ProvenanceError(f"archive member missing: {name}")
    if not member.isfile():
        raise ProvenanceError(
            f"archive member is not a regular file: {name}"
        )
    if max_bytes is not None and member.size > max_bytes:
        raise ProvenanceError(
            f"archive member exceeds size limit: {name}"
        )

    extracted = tf.extractfile(member)
    if extracted is None:
        raise ProvenanceError(f"cannot read archive member: {name}")
    return extracted.read()


def member_identity(
    tf: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    name: str,
) -> tuple[int, str]:
    data = read_member(tf, members, name)
    return len(data), hashlib.sha256(data).hexdigest()


def require_dict(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise ProvenanceError(f"{label} must be an object")
    return value


def validate_identity_record(record: dict, label: str) -> None:
    present = record.get("present")
    if not isinstance(present, bool):
        raise ProvenanceError(f"{label}.present must be boolean")

    if present:
        size = record.get("size_bytes")
        digest = record.get("sha256")

        if not isinstance(size, int) or size < 0:
            raise ProvenanceError(
                f"{label}.size_bytes must be a non-negative integer"
            )
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise ProvenanceError(
                f"{label}.sha256 must be a lowercase SHA-256 digest"
            )
    else:
        if record.get("size_bytes") is not None:
            raise ProvenanceError(
                f"{label}.size_bytes must be null when absent"
            )
        if record.get("sha256") is not None:
            raise ProvenanceError(
                f"{label}.sha256 must be null when absent"
            )


def assert_member_matches(
    tf: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    archive_path: str,
    record: dict,
    label: str,
) -> None:
    actual_size, actual_sha = member_identity(
        tf,
        members,
        archive_path,
    )

    if actual_size != record["size_bytes"]:
        raise ProvenanceError(
            f"{label} size mismatch: provenance={record['size_bytes']} "
            f"archive={actual_size}"
        )

    if actual_sha != record["sha256"]:
        raise ProvenanceError(
            f"{label} SHA-256 mismatch"
        )


def verify_archive(args: argparse.Namespace) -> int:
    archive = Path(args.archive)

    with tarfile.open(archive, "r:gz") as tf:
        members = member_map(tf)

        provenance_name = "stereotool/provenance.json"
        raw = read_member(
            tf,
            members,
            provenance_name,
            max_bytes=1024 * 1024,
        )

        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProvenanceError(
                f"invalid {provenance_name}: {exc}"
            ) from exc

        root = require_dict(document, "provenance")

        if root.get("schema_version") != SCHEMA_VERSION:
            raise ProvenanceError(
                "unsupported Stereo Tool provenance schema_version"
            )
        if root.get("product") != PRODUCT:
            raise ProvenanceError("unexpected Stereo Tool product identity")
        if root.get("classification") != CLASSIFICATION:
            raise ProvenanceError(
                "unexpected Stereo Tool provenance classification"
            )
        if root.get("binary_bundled") is not False:
            raise ProvenanceError(
                "binary_bundled must be false for proprietary Stereo Tool"
            )

        binary = require_dict(root.get("binary"), "binary")
        validate_identity_record(binary, "binary")

        if binary.get("bundled") is not False:
            raise ProvenanceError("binary.bundled must be false")

        configured_path = binary.get("configured_path")
        if not isinstance(configured_path, str) or not configured_path:
            raise ProvenanceError(
                "binary.configured_path must be a non-empty string"
            )

        version = binary.get("reported_version")
        if version is not None and not re.fullmatch(
            r"[0-9]+\.[0-9]+",
            version,
        ):
            raise ProvenanceError(
                "binary.reported_version has invalid format"
            )

        binary_archive_candidate = (
            f"stereotool/{Path(configured_path).name}"
        )
        if binary_archive_candidate in members:
            raise ProvenanceError(
                "proprietary Stereo Tool binary is present in archive "
                "despite binary_bundled=false"
            )

        service = require_dict(
            root.get("service_unit"),
            "service_unit",
        )
        validate_identity_record(service, "service_unit")

        expected_service_path = "etc-live/stereotool.service"
        if service["present"]:
            if service.get("bundled") is not True:
                raise ProvenanceError(
                    "present service_unit must be marked bundled"
                )
            if service.get("archive_path") != expected_service_path:
                raise ProvenanceError(
                    "service_unit archive_path must be "
                    "etc-live/stereotool.service"
                )
            assert_member_matches(
                tf,
                members,
                expected_service_path,
                service,
                "service_unit",
            )
        else:
            if expected_service_path in members:
                raise ProvenanceError(
                    "archive contains stereotool.service but provenance "
                    "claims service_unit absent"
                )

        profiles = root.get("profiles")
        if not isinstance(profiles, list):
            raise ProvenanceError("profiles must be an array")

        expected_profiles: dict[str, dict] = {}
        for index, profile in enumerate(profiles):
            record = require_dict(
                profile,
                f"profiles[{index}]",
            )
            validate_identity_record(
                record,
                f"profiles[{index}]",
            )
            if record["present"] is not True:
                raise ProvenanceError(
                    "profile records must describe present files"
                )
            if record.get("bundled") is not True:
                raise ProvenanceError(
                    "profile records must be marked bundled"
                )

            archive_path = record.get("archive_path")
            if (
                not isinstance(archive_path, str)
                or not re.fullmatch(
                    r"stereotool/[^/]+[.]sts",
                    archive_path,
                    re.IGNORECASE,
                )
            ):
                raise ProvenanceError(
                    f"invalid profile archive_path: {archive_path!r}"
                )
            if archive_path in expected_profiles:
                raise ProvenanceError(
                    f"duplicate profile record: {archive_path}"
                )
            expected_profiles[archive_path] = record

        actual_profiles = {
            name
            for name, member in members.items()
            if member.isfile()
            and re.fullmatch(
                r"stereotool/[^/]+[.]sts",
                name,
                re.IGNORECASE,
            )
        }

        if set(expected_profiles) != actual_profiles:
            raise ProvenanceError(
                "profile set mismatch between provenance and archive"
            )

        for archive_path, record in expected_profiles.items():
            assert_member_matches(
                tf,
                members,
                archive_path,
                record,
                archive_path,
            )

        runtime = require_dict(
            root.get("runtime_state"),
            "runtime_state",
        )
        validate_identity_record(runtime, "runtime_state")

        if runtime.get("bundled") is not False:
            raise ProvenanceError(
                "runtime_state.bundled must be false"
            )
        if runtime.get("secret_bearing") is not True:
            raise ProvenanceError(
                "runtime_state.secret_bearing must be true"
            )
        if runtime.get("contents_recorded") is not False:
            raise ProvenanceError(
                "runtime_state.contents_recorded must be false"
            )

        plaintext_runtime_members = [
            name
            for name in members
            if name == ".stereo_tool.rc"
            or name.endswith("/.stereo_tool.rc")
        ]
        if plaintext_runtime_members:
            raise ProvenanceError(
                "plaintext .stereo_tool.rc is present in archive"
            )

        print(
            json.dumps(
                {
                    "binary_present": binary["present"],
                    "profiles": len(expected_profiles),
                    "reported_version": version,
                    "runtime_state_present": runtime["present"],
                    "schema_version": root["schema_version"],
                    "service_unit_present": service["present"],
                    "verified": True,
                },
                sort_keys=True,
            )
        )

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser("collect")
    collect_parser.add_argument(
        "--service-unit-file",
        required=True,
    )
    collect_parser.add_argument(
        "--service-unit-source-path",
        required=True,
    )
    collect_parser.add_argument(
        "--profile-dir",
        required=True,
    )
    collect_parser.add_argument(
        "--profile-source-root",
        required=True,
    )
    collect_parser.add_argument(
        "--runtime-state",
        required=True,
    )
    collect_parser.add_argument(
        "--fallback-binary",
        required=True,
    )
    collect_parser.add_argument(
        "--output",
        required=True,
    )
    collect_parser.set_defaults(func=collect)

    verify_parser = subparsers.add_parser("verify-archive")
    verify_parser.add_argument("--archive", required=True)
    verify_parser.set_defaults(func=verify_archive)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        return args.func(args)
    except (
        OSError,
        ProvenanceError,
        tarfile.TarError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
