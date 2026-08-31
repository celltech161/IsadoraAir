"""Unprivileged Phase-D protected-runtime release authoring primitives.

This module is deliberately release-side only.  It constructs and validates
the exact immutable bundle format consumed by the protected updater, but it
does not install files, inspect station configuration, or infer a signing key.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile


OPENSSL_BINARY = "/usr/bin/openssl"
POLICY_PATH = "protected-policy.json"
ENTRYPOINT = "updaterd.py"
DESCRIPTOR_FILENAME = "protected-runtime-descriptor.json"

_TOP_LEVEL_FILES = frozenset({"README.md", "updaterctl.py", ENTRYPOINT, POLICY_PATH})
_PACKAGE_DIRECTORIES = frozenset({"isadoraair_updater", "protected_bootstrap"})


class ReleaseAuthoringError(ValueError):
    """A deterministic, operator-safe release-authoring refusal."""


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _runtime_imports():
    # This tool lives below deploy/updater_bootstrap/tools.  Its sibling
    # deploy/updater_runtime is reviewed source, never a station-selected path.
    import sys

    runtime_root = Path(__file__).resolve().parents[2] / "updater_runtime"
    if str(runtime_root) not in sys.path:
        sys.path.insert(0, str(runtime_root))
    from protected_bootstrap.attestation import build_attestation_statement, verify_ed25519
    from protected_bootstrap.descriptor import (
        FileEntry,
        compute_bundle_sha256,
        parse_descriptor_dict,
        verify_descriptor_against_directory,
    )
    from protected_bootstrap.policy import parse_policy_dict

    return {
        "build_statement": build_attestation_statement,
        "verify_ed25519": verify_ed25519,
        "FileEntry": FileEntry,
        "compute_bundle_sha256": compute_bundle_sha256,
        "parse_descriptor_dict": parse_descriptor_dict,
        "verify_descriptor": verify_descriptor_against_directory,
        "parse_policy": parse_policy_dict,
    }


def _plain_file(path: Path, *, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReleaseAuthoringError(f"{label} cannot be inspected: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ReleaseAuthoringError(f"{label} must be a non-symlink regular file: {path}")
    if info.st_nlink != 1:
        raise ReleaseAuthoringError(f"{label} must not be hard-linked: {path}")
    return info


def enumerate_runtime_files(runtime_root: Path) -> tuple[Path, ...]:
    """Return the closed, canonical protected-runtime inventory.

    The bundle is Python source/data, not an arbitrary directory archive.  The
    only allowed package payload is direct ``*.py`` children of the two known
    packages, plus the four explicitly named top-level files.  This rejects
    cache directories, wheels, keys, fixtures, symlinks and special files.
    """

    root = Path(runtime_root)
    if root.is_symlink() or not root.is_dir():
        raise ReleaseAuthoringError("runtime root must be a non-symlink directory")
    paths: list[Path] = []
    seen_directories: set[str] = set()
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        relative_directory = current_path.relative_to(root)
        if relative_directory.parts:
            if len(relative_directory.parts) != 1 or relative_directory.as_posix() not in _PACKAGE_DIRECTORIES:
                raise ReleaseAuthoringError(
                    f"unexpected directory in protected runtime: {relative_directory.as_posix()}"
                )
            seen_directories.add(relative_directory.as_posix())
        for directory in directories:
            candidate = current_path / directory
            if candidate.is_symlink():
                raise ReleaseAuthoringError(f"symlink directory refused: {candidate}")
            relative = candidate.relative_to(root)
            if len(relative.parts) != 1 or relative.as_posix() not in _PACKAGE_DIRECTORIES:
                raise ReleaseAuthoringError(f"unexpected directory in protected runtime: {relative.as_posix()}")
        for filename in filenames:
            candidate = current_path / filename
            relative = candidate.relative_to(root)
            relative_text = relative.as_posix()
            _plain_file(candidate, label="runtime file")
            # The descriptor is release metadata adjacent to (but never part
            # of) the runtime tree.  Its one fixed name is the only ignored
            # file; arbitrary adjacent JSON remains an error.
            if len(relative.parts) == 1 and filename == DESCRIPTOR_FILENAME:
                continue
            allowed = (
                (len(relative.parts) == 1 and relative_text in _TOP_LEVEL_FILES)
                or (
                    len(relative.parts) == 2
                    and relative.parts[0] in _PACKAGE_DIRECTORIES
                    and filename.endswith(".py")
                )
            )
            if not allowed:
                raise ReleaseAuthoringError(f"unexpected file in protected runtime: {relative_text}")
            paths.append(candidate)
    missing_top = _TOP_LEVEL_FILES - {path.relative_to(root).as_posix() for path in paths}
    if missing_top:
        raise ReleaseAuthoringError(f"protected runtime is missing required file(s): {sorted(missing_top)!r}")
    missing_packages = _PACKAGE_DIRECTORIES - seen_directories
    if missing_packages:
        raise ReleaseAuthoringError(f"protected runtime is missing package directory(s): {sorted(missing_packages)!r}")
    return tuple(sorted(paths, key=lambda path: path.relative_to(root).as_posix()))


def build_descriptor(
    *, runtime_root: Path, generation: int, runtime_version: int,
    manifest_protocol_version: int, supported_wire_protocols: tuple[int, ...],
) -> bytes:
    contracts = _runtime_imports()
    root = Path(runtime_root)
    paths = enumerate_runtime_files(root)
    policy_path = root / POLICY_PATH
    try:
        policy_value = json.loads(policy_path.read_text(encoding="utf-8"))
        contracts["parse_policy"](policy_value, label=POLICY_PATH)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReleaseAuthoringError(f"{POLICY_PATH} is not a valid protected managed-unit policy: {exc}") from exc

    entries = []
    file_objects = []
    for path in paths:
        relative = path.relative_to(root).as_posix()
        content = path.read_bytes()
        mode = "0755" if relative == ENTRYPOINT else "0644"
        entry = {
            "path": relative,
            "sha256": hashlib.sha256(content).hexdigest(),
            "mode": mode,
            "size_bytes": len(content),
        }
        entries.append(entry)
        file_objects.append(contracts["FileEntry"](**entry))
    descriptor = {
        "schema_version": 1,
        "generation": generation,
        "runtime_version": runtime_version,
        "manifest_protocol_version": manifest_protocol_version,
        "supported_wire_protocols": sorted(supported_wire_protocols),
        "entrypoint": ENTRYPOINT,
        "files": entries,
        "bundle_sha256": contracts["compute_bundle_sha256"](tuple(file_objects)),
    }
    # Round-trip through the production parser before publishing bytes.
    contracts["parse_descriptor_dict"](descriptor, label="authored descriptor")
    return canonical_json(descriptor)


def build_statement(
    *, descriptor_bytes: bytes, release_id: str, previous_release_id: str | None,
    generation: int,
) -> bytes:
    contracts = _runtime_imports()
    return contracts["build_statement"](
        release_id=release_id,
        previous_release_id=previous_release_id,
        generation=generation,
        descriptor_sha256=hashlib.sha256(descriptor_bytes).hexdigest(),
    )


def sign_statement(
    *, statement: bytes, private_key_path: Path, public_key_path: Path,
) -> bytes:
    """Sign with one explicit private key and fixed OpenSSL argv.

    No shell, PATH lookup, default key location or caller-supplied executable is
    expressible.  The key must be a private, single-link regular file.
    """

    private_key = Path(private_key_path)
    public_key = Path(public_key_path)
    private_info = _plain_file(private_key, label="private key")
    _plain_file(public_key, label="public key")
    if private_info.st_mode & 0o077:
        raise ReleaseAuthoringError("private key must not be group/world accessible")
    with tempfile.TemporaryDirectory(prefix="isadoraair-release-sign-") as scratch:
        statement_path = Path(scratch) / "statement"
        statement_path.write_bytes(statement)
        result = subprocess.run(
            [
                OPENSSL_BINARY, "pkeyutl", "-sign", "-inkey", str(private_key),
                "-rawin", "-in", str(statement_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
        )
    if result.returncode != 0:
        raise ReleaseAuthoringError(f"fixed OpenSSL signer exited {result.returncode}")
    signature = result.stdout
    outcome = _runtime_imports()["verify_ed25519"](
        public_key_path=public_key, statement=statement, signature=signature,
    )
    if not outcome.verified:
        raise ReleaseAuthoringError(f"generated signature did not verify: {outcome.detail}")
    return signature


def validate_descriptor_inventory(*, descriptor_bytes: bytes, runtime_root: Path):
    contracts = _runtime_imports()
    try:
        descriptor = contracts["parse_descriptor_dict"](
            json.loads(descriptor_bytes.decode("utf-8")), label="release descriptor"
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReleaseAuthoringError(f"release descriptor is invalid: {exc}") from exc
    expected = {path.relative_to(runtime_root).as_posix() for path in enumerate_runtime_files(runtime_root)}
    actual = {entry.path for entry in descriptor.files}
    if actual != expected:
        raise ReleaseAuthoringError("descriptor inventory does not equal the closed runtime inventory")
    by_path = descriptor.file_by_path()
    for relative in sorted(expected):
        source = Path(runtime_root) / relative
        content = source.read_bytes()
        entry = by_path[relative]
        if len(content) != entry.size_bytes or hashlib.sha256(content).hexdigest() != entry.sha256:
            raise ReleaseAuthoringError(f"runtime source bytes do not match descriptor: {relative}")
        expected_mode = "0755" if relative == ENTRYPOINT else "0644"
        if entry.mode != expected_mode:
            raise ReleaseAuthoringError(
                f"descriptor publication mode for {relative} must be {expected_mode}"
            )
    return descriptor


def generation_one_policy_bytes() -> bytes:
    """Canonical D0 policy, derived from the existing compiled authority."""
    import sys

    runtime_root = Path(__file__).resolve().parents[2] / "updater_runtime"
    if str(runtime_root) not in sys.path:
        sys.path.insert(0, str(runtime_root))
    from isadoraair_updater.release import GENERATION_1_POLICY_DOCUMENT

    value = {
        "schema_version": GENERATION_1_POLICY_DOCUMENT.schema_version,
        "managed_units": [
            {"unit": entry.unit, "policy": entry.policy}
            for entry in GENERATION_1_POLICY_DOCUMENT.entries
        ],
    }
    _runtime_imports()["parse_policy"](value, label="generation-1 policy")
    return canonical_json(value)
