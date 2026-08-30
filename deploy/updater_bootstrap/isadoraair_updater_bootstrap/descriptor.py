"""Supervisor-side runtime descriptor validation -- an INDEPENDENT
implementation of the same contract as
deploy/updater_runtime/protected_bootstrap/descriptor.py (D1, the
worker-side copy). Deliberately not imported from there: Correction 1
requires the immutable supervisor never execute or import replaceable-
worker-tree code, even for something as seemingly inert as a schema
validator -- a compromised worker tree could otherwise smuggle a
malicious "validator" the supervisor would unwittingly trust. Kept in
agreement with the worker-side copy by
updatecenter/tests/test_phase_d2_parity.py, which feeds an identical
fixture corpus to both and asserts identical accept/reject outcomes --
never by one importing the other."""
from __future__ import annotations

import dataclasses
import hashlib
import re

SCHEMA_VERSION = 1
ALLOWED_MODES = frozenset({"0755", "0644"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MODE_RE = re.compile(r"^0[0-7]{3}$")
MAX_FILES = 256
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_GENERATION = 1_000_000
MAX_WIRE_PROTOCOLS = 8
MAX_ENTRYPOINT_LEN = 255
MAX_PATH_LEN = 255
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


class DescriptorError(ValueError):
    pass


def validate_relative_path(value, *, field: str, max_len: int = MAX_PATH_LEN) -> str:
    if not isinstance(value, str) or not value:
        raise DescriptorError(f"{field}: must be a non-empty string")
    if len(value) > max_len:
        raise DescriptorError(f"{field}: exceeds {max_len} characters")
    if "\\" in value:
        raise DescriptorError(f"{field}: backslashes are not a supported path separator")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value):
        raise DescriptorError(f"{field}: contains a control character")
    if value.startswith("/") or value.endswith("/"):
        raise DescriptorError(f"{field}: must be a relative path with no leading/trailing slash")
    segments = value.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise DescriptorError(f"{field}: contains an empty, '.', or '..' path segment")
    for segment in segments:
        if not _PATH_SEGMENT_RE.match(segment):
            raise DescriptorError(f"{field}: path segment {segment!r} contains an unsupported character")
    return value


@dataclasses.dataclass(frozen=True)
class FileEntry:
    path: str
    sha256: str
    mode: str
    size_bytes: int


@dataclasses.dataclass(frozen=True)
class RuntimeDescriptor:
    schema_version: int
    generation: int
    runtime_version: int
    manifest_protocol_version: int
    supported_wire_protocols: tuple[int, ...]
    entrypoint: str
    files: tuple[FileEntry, ...]
    bundle_sha256: str

    def file_by_path(self) -> dict[str, FileEntry]:
        return {entry.path: entry for entry in self.files}


def compute_bundle_sha256(files: tuple[FileEntry, ...]) -> str:
    hasher = hashlib.sha256()
    for entry in files:
        hasher.update(entry.path.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(entry.sha256.encode("ascii"))
        hasher.update(b"\x00")
        hasher.update(entry.mode.encode("ascii"))
        hasher.update(b"\x00")
        hasher.update(str(entry.size_bytes).encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def generation_advances(generation: int, previous_generation: int | None) -> bool:
    if previous_generation is None:
        return generation == 1
    return generation > previous_generation


def _positive_int(data, field, *, maximum=None):
    value = data[field]
    if not isinstance(value, int) or isinstance(value, bool):
        raise DescriptorError(f"{field}: must be an integer")
    if value < 1:
        raise DescriptorError(f"{field}: must be positive")
    if maximum is not None and value > maximum:
        raise DescriptorError(f"{field}: exceeds maximum {maximum}")
    return value


def parse_descriptor_dict(data: dict, *, label: str = "<descriptor>") -> RuntimeDescriptor:
    if not isinstance(data, dict):
        raise DescriptorError(f"{label}: descriptor must be a JSON object")

    known_top = {
        "schema_version", "generation", "runtime_version", "manifest_protocol_version",
        "supported_wire_protocols", "entrypoint", "files", "bundle_sha256",
    }
    if set(data) - known_top:
        raise DescriptorError(f"{label}: unrecognized field(s) {sorted(set(data) - known_top)!r}")
    if known_top - set(data):
        raise DescriptorError(f"{label}: missing required field(s) {sorted(known_top - set(data))!r}")

    schema_version = data["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != SCHEMA_VERSION:
        raise DescriptorError(f"{label}: unsupported schema_version")

    generation = _positive_int(data, "generation", maximum=MAX_GENERATION)
    runtime_version = _positive_int(data, "runtime_version")
    manifest_protocol_version = _positive_int(data, "manifest_protocol_version")

    wire = data["supported_wire_protocols"]
    if not isinstance(wire, list) or not wire or len(wire) > MAX_WIRE_PROTOCOLS:
        raise DescriptorError(f"{label}: supported_wire_protocols must be a non-empty, bounded list")
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in wire):
        raise DescriptorError(f"{label}: supported_wire_protocols must contain only positive integers")
    if len(set(wire)) != len(wire) or list(wire) != sorted(wire):
        raise DescriptorError(f"{label}: supported_wire_protocols must be unique and canonically sorted")

    entrypoint = validate_relative_path(data["entrypoint"], field="entrypoint", max_len=MAX_ENTRYPOINT_LEN)

    raw_files = data["files"]
    if not isinstance(raw_files, list) or not raw_files:
        raise DescriptorError(f"{label}: files must be a non-empty list")
    if len(raw_files) > MAX_FILES:
        raise DescriptorError(f"{label}: files exceeds {MAX_FILES} entries")

    known_file_keys = {"path", "sha256", "mode", "size_bytes"}
    entries: list[FileEntry] = []
    seen: set[str] = set()
    total_bytes = 0
    for index, raw in enumerate(raw_files):
        item = f"{label}: files[{index}]"
        if not isinstance(raw, dict) or set(raw) != known_file_keys:
            raise DescriptorError(f"{item}: must be an object with exactly {sorted(known_file_keys)!r}")
        path = validate_relative_path(raw["path"], field=f"{item}.path")
        if path in seen:
            raise DescriptorError(f"{item}: duplicate path {path!r}")
        seen.add(path)
        sha = raw["sha256"]
        if not isinstance(sha, str) or not SHA256_RE.match(sha):
            raise DescriptorError(f"{item}: sha256 must be exactly 64 lowercase hex characters")
        mode = raw["mode"]
        if mode not in ALLOWED_MODES:
            raise DescriptorError(f"{item}: mode must be one of {sorted(ALLOWED_MODES)!r}")
        size_bytes = raw["size_bytes"]
        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or not (0 <= size_bytes <= MAX_FILE_BYTES):
            raise DescriptorError(f"{item}: size_bytes out of range")
        total_bytes += size_bytes
        entries.append(FileEntry(path, sha, mode, size_bytes))

    if total_bytes > MAX_TOTAL_BYTES:
        raise DescriptorError(f"{label}: total declared size exceeds {MAX_TOTAL_BYTES} bytes")
    if [e.path for e in entries] != sorted(e.path for e in entries):
        raise DescriptorError(f"{label}: files must be in canonical ascending path order")
    if entrypoint not in seen:
        raise DescriptorError(f"{label}: entrypoint {entrypoint!r} is not present in files")
    if next(e.mode for e in entries if e.path == entrypoint) != "0755":
        raise DescriptorError(f"{label}: entrypoint must have mode 0755")

    declared = data["bundle_sha256"]
    if not isinstance(declared, str) or not SHA256_RE.match(declared):
        raise DescriptorError(f"{label}: bundle_sha256 must be exactly 64 lowercase hex characters")
    if declared != compute_bundle_sha256(tuple(entries)):
        raise DescriptorError(f"{label}: bundle_sha256 does not match the recomputed digest")

    return RuntimeDescriptor(
        schema_version=schema_version, generation=generation, runtime_version=runtime_version,
        manifest_protocol_version=manifest_protocol_version, supported_wire_protocols=tuple(wire),
        entrypoint=entrypoint, files=tuple(entries), bundle_sha256=declared,
    )


def hash_file(path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_descriptor_against_directory(descriptor: RuntimeDescriptor, root) -> tuple[str, ...]:
    from pathlib import Path
    root = Path(root)
    if not root.is_dir():
        raise DescriptorError(f"verification root {root} is not a directory")
    reasons: list[str] = []
    declared = descriptor.file_by_path()
    for entry in descriptor.files:
        candidate = root / entry.path
        if candidate.is_symlink():
            reasons.append(f"{entry.path}: is a symlink")
            continue
        if not candidate.is_file():
            reasons.append(f"{entry.path}: missing")
            continue
        info = candidate.stat()
        actual_mode = "0" + oct(info.st_mode & 0o777)[2:].zfill(4)[-3:]
        if actual_mode != entry.mode:
            reasons.append(f"{entry.path}: mode {actual_mode} != {entry.mode}")
        if info.st_size != entry.size_bytes:
            reasons.append(f"{entry.path}: size {info.st_size} != {entry.size_bytes}")
        elif hash_file(candidate) != entry.sha256:
            reasons.append(f"{entry.path}: sha256 mismatch")
    actual_paths = set()
    for candidate in root.rglob("*"):
        if not candidate.is_dir():
            actual_paths.add(candidate.relative_to(root).as_posix())
    for path in sorted(actual_paths - set(declared)):
        reasons.append(f"{path}: present on disk but not declared")
    return tuple(reasons)
