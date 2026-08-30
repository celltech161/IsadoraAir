"""D1-B: the runtime bundle descriptor -- a strict, provider-neutral,
pure-data inventory of exactly what files a protected-runtime generation
consists of, their exact bytes (by SHA-256), and their exact filesystem
mode. Parseable without importing ANY worker implementation code (no
isadoraair_updater.release/systemd/daemon/executor import here) -- a
future supervisor verifies a candidate bundle's shape and content before
it ever imports/executes a single line of that bundle.

This is pure data: no commands, no hooks, no environment, no arbitrary
destination path, no systemd capability directive, no station-specific
path, no symlink/special-file semantics. A file entry can only assert
"a plain regular file with this exact relative path has this exact
content and this exact mode" -- nothing else is expressible."""
from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path
import re

SCHEMA_VERSION = 1

# Closed set -- a bundle file is either the one executable entrypoint
# (0755) or a plain read-only source/data file (0644). No other mode is
# ever legitimate for a protected-runtime bundle; anything else is
# refused rather than passed through to a future chmod call.
ALLOWED_MODES = frozenset({"0755", "0644"})

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MODE_RE = re.compile(r"^0[0-7]{3}$")

# Generous but genuinely finite -- a protected-runtime bundle is a small
# Python package plus a handful of data files, not an arbitrary payload.
MAX_FILES = 256
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_GENERATION = 1_000_000
MAX_WIRE_PROTOCOLS = 8
MAX_ENTRYPOINT_LEN = 255
MAX_PATH_LEN = 255

# Every relative-path field in this package (a descriptor file path, a
# manifest's descriptor_path/attestation path) uses this exact safety
# rule -- see D1-A's manifest_field.py, which imports this function
# directly rather than re-deriving an equivalent regex, so the two
# contracts can never silently drift apart.
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


class DescriptorError(ValueError):
    """Raised for any structurally-invalid descriptor. Always a specific,
    human-readable, non-secret message -- never a generic 'invalid'."""


def validate_relative_path(value, *, field: str, max_len: int = MAX_PATH_LEN) -> str:
    """Strict POSIX-relative-path safety, shared by every path-shaped
    field in this package. Rejects: non-string, empty, absolute
    (leading '/'), backslashes, control characters, NUL, '..' segments
    (anywhere, not just a bare leading one), '.' segments, empty
    segments (a literal '//'), leading/trailing '/', and anything over
    max_len bytes. What remains is always a plain, unambiguous,
    same-tree-relative POSIX path with no possible escape or alias."""
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


def generation_advances(generation: int, previous_generation: int | None) -> bool:
    """The one shared rule for whether a candidate generation is
    legitimate relative to whatever came before -- used identically by
    verification.py (candidate-bundle verification) and cross_check.py
    (manifest-level predecessor-diff checking), so the two can never
    silently define "legitimate" differently. previous_generation=None
    means no protected-runtime generation has EVER existed on this
    station's trusted history -- the only legitimate first value is
    exactly 1. Otherwise: strictly greater than previous_generation,
    with no other constraint -- a SKIP (e.g. 3 -> 7) is legitimate (a
    station may fast-forward past intermediate generations it never
    installed); a REPLAY (3 -> 3) or ROLLBACK (3 -> 2) is not."""
    if previous_generation is None:
        return generation == 1
    return generation > previous_generation


def compute_bundle_sha256(files: tuple[FileEntry, ...]) -> str:
    """Deterministic aggregate digest over the EXACT sorted file list --
    never "trust json.dumps," computed mechanically here and only here,
    with a fixed, unambiguous field order/separator/terminator so two
    independent implementations (worker, future supervisor) can never
    disagree about what bytes were hashed. `files` must already be in
    canonical (ascending path) order -- callers that don't yet know
    that is true should sort first; this function does not re-sort, so
    it can also be used to positively PROVE a given order matches (or
    does not) the descriptor's own declared bundle_sha256."""
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


def _require_int(value, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise DescriptorError(f"{field}: must be an integer")
    return value


def parse_descriptor_dict(data: dict, *, label: str = "<descriptor>") -> RuntimeDescriptor:
    """The one entry point. Strict: unknown top-level keys, unknown file-
    entry keys, wrong types, out-of-bound counts/sizes, unsorted/
    duplicate file paths, a disallowed mode, an entrypoint absent from
    `files`, or a bundle_sha256 that does not match the recomputed
    digest of the declared files all raise DescriptorError. There is no
    partial/best-effort acceptance."""
    if not isinstance(data, dict):
        raise DescriptorError(f"{label}: descriptor must be a JSON object")

    known_top = {
        "schema_version", "generation", "runtime_version", "manifest_protocol_version",
        "supported_wire_protocols", "entrypoint", "files", "bundle_sha256",
    }
    unknown_top = set(data) - known_top
    if unknown_top:
        raise DescriptorError(f"{label}: unrecognized field(s) {sorted(unknown_top)!r}")
    missing_top = known_top - set(data)
    if missing_top:
        raise DescriptorError(f"{label}: missing required field(s) {sorted(missing_top)!r}")

    schema_version = _require_int(data["schema_version"], "schema_version")
    if schema_version != SCHEMA_VERSION:
        raise DescriptorError(f"{label}: unsupported schema_version {schema_version} (expected {SCHEMA_VERSION})")

    generation = _require_int(data["generation"], "generation")
    if not (1 <= generation <= MAX_GENERATION):
        raise DescriptorError(f"{label}: generation must be between 1 and {MAX_GENERATION}")

    runtime_version = _require_int(data["runtime_version"], "runtime_version")
    if runtime_version < 1:
        raise DescriptorError(f"{label}: runtime_version must be a positive integer")

    manifest_protocol_version = _require_int(data["manifest_protocol_version"], "manifest_protocol_version")
    if manifest_protocol_version < 1:
        raise DescriptorError(f"{label}: manifest_protocol_version must be a positive integer")

    wire = data["supported_wire_protocols"]
    if not isinstance(wire, list) or not wire:
        raise DescriptorError(f"{label}: supported_wire_protocols must be a non-empty list")
    if len(wire) > MAX_WIRE_PROTOCOLS:
        raise DescriptorError(f"{label}: supported_wire_protocols exceeds {MAX_WIRE_PROTOCOLS} entries")
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in wire):
        raise DescriptorError(f"{label}: supported_wire_protocols must contain only positive integers")
    if len(set(wire)) != len(wire):
        raise DescriptorError(f"{label}: supported_wire_protocols contains a duplicate")
    if list(wire) != sorted(wire):
        raise DescriptorError(f"{label}: supported_wire_protocols must be in canonical ascending order")
    supported_wire_protocols = tuple(wire)

    entrypoint = validate_relative_path(data["entrypoint"], field="entrypoint", max_len=MAX_ENTRYPOINT_LEN)

    raw_files = data["files"]
    if not isinstance(raw_files, list):
        raise DescriptorError(f"{label}: files must be a list")
    if not raw_files:
        raise DescriptorError(f"{label}: files must not be empty")
    if len(raw_files) > MAX_FILES:
        raise DescriptorError(f"{label}: files exceeds {MAX_FILES} entries")

    known_file_keys = {"path", "sha256", "mode", "size_bytes"}
    entries: list[FileEntry] = []
    seen_paths: set[str] = set()
    total_bytes = 0
    for index, raw in enumerate(raw_files):
        item_label = f"{label}: files[{index}]"
        if not isinstance(raw, dict):
            raise DescriptorError(f"{item_label}: must be a JSON object")
        unknown_keys = set(raw) - known_file_keys
        if unknown_keys:
            raise DescriptorError(f"{item_label}: unrecognized field(s) {sorted(unknown_keys)!r}")
        missing_keys = known_file_keys - set(raw)
        if missing_keys:
            raise DescriptorError(f"{item_label}: missing field(s) {sorted(missing_keys)!r}")

        path = validate_relative_path(raw["path"], field=f"{item_label}.path")
        if path in seen_paths:
            raise DescriptorError(f"{item_label}: duplicate path {path!r}")
        seen_paths.add(path)

        sha = raw["sha256"]
        if not isinstance(sha, str) or not SHA256_RE.match(sha):
            raise DescriptorError(f"{item_label}: sha256 must be exactly 64 lowercase hex characters")

        mode = raw["mode"]
        if not isinstance(mode, str) or not MODE_RE.match(mode) or mode not in ALLOWED_MODES:
            raise DescriptorError(f"{item_label}: mode must be one of {sorted(ALLOWED_MODES)!r}")

        size_bytes = _require_int(raw["size_bytes"], f"{item_label}.size_bytes")
        if not (0 <= size_bytes <= MAX_FILE_BYTES):
            raise DescriptorError(f"{item_label}: size_bytes must be between 0 and {MAX_FILE_BYTES}")
        total_bytes += size_bytes

        entries.append(FileEntry(path=path, sha256=sha, mode=mode, size_bytes=size_bytes))

    if total_bytes > MAX_TOTAL_BYTES:
        raise DescriptorError(f"{label}: total declared file size exceeds {MAX_TOTAL_BYTES} bytes")

    paths_in_order = [entry.path for entry in entries]
    if paths_in_order != sorted(paths_in_order):
        raise DescriptorError(f"{label}: files must be listed in canonical ascending path order")

    if entrypoint not in seen_paths:
        raise DescriptorError(f"{label}: entrypoint {entrypoint!r} is not present in files")
    entrypoint_mode = next(entry.mode for entry in entries if entry.path == entrypoint)
    if entrypoint_mode != "0755":
        raise DescriptorError(f"{label}: entrypoint {entrypoint!r} must have mode 0755, got {entrypoint_mode}")

    declared_bundle_sha256 = data["bundle_sha256"]
    if not isinstance(declared_bundle_sha256, str) or not SHA256_RE.match(declared_bundle_sha256):
        raise DescriptorError(f"{label}: bundle_sha256 must be exactly 64 lowercase hex characters")
    recomputed = compute_bundle_sha256(tuple(entries))
    if declared_bundle_sha256 != recomputed:
        raise DescriptorError(
            f"{label}: bundle_sha256 does not match the recomputed digest of the declared files"
        )

    return RuntimeDescriptor(
        schema_version=schema_version,
        generation=generation,
        runtime_version=runtime_version,
        manifest_protocol_version=manifest_protocol_version,
        supported_wire_protocols=supported_wire_protocols,
        entrypoint=entrypoint,
        files=tuple(entries),
        bundle_sha256=declared_bundle_sha256,
    )


def hash_file(path: Path) -> str:
    """SHA-256 of one real file's bytes -- the same primitive both
    descriptor authoring tooling and disk-verification below must use,
    so they can never silently disagree about what "the file's hash"
    means (e.g. line-ending normalization)."""
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_descriptor_against_directory(descriptor: RuntimeDescriptor, root: Path) -> tuple[str, ...]:
    """Compares a validated descriptor against REAL files under `root`.
    Returns a tuple of human-readable mismatch reasons (empty = exact
    match). Never raises for a mismatch -- only for a filesystem error
    inspecting `root` itself. Checks, per declared file: present,
    regular file (no symlink), exact size, exact mode, exact hash --
    and separately reports any REAL file under root that the descriptor
    does not declare (an "extra file" the descriptor's inventory must
    also be exact about, not just a subset)."""
    if not root.is_dir():
        raise DescriptorError(f"verification root {root} is not a directory")
    reasons: list[str] = []
    declared = descriptor.file_by_path()
    for entry in descriptor.files:
        candidate = root / entry.path
        if candidate.is_symlink():
            reasons.append(f"{entry.path}: is a symlink, not a plain regular file")
            continue
        if not candidate.is_file():
            reasons.append(f"{entry.path}: missing from bundle")
            continue
        stat_result = candidate.stat()
        actual_mode = oct(stat_result.st_mode & 0o777)[2:].zfill(4)
        # oct() above yields e.g. "0o755" -> stripped to "755"; re-add
        # the leading 0 to match the descriptor's own "0NNN" convention.
        actual_mode = "0" + actual_mode[-3:]
        if actual_mode != entry.mode:
            reasons.append(f"{entry.path}: mode is {actual_mode}, descriptor declares {entry.mode}")
        if stat_result.st_size != entry.size_bytes:
            reasons.append(
                f"{entry.path}: size is {stat_result.st_size} bytes, descriptor declares {entry.size_bytes}"
            )
        else:
            actual_hash = hash_file(candidate)
            if actual_hash != entry.sha256:
                reasons.append(f"{entry.path}: sha256 does not match the descriptor")

    declared_paths = set(declared)
    actual_paths: set[str] = set()
    for candidate in root.rglob("*"):
        if candidate.is_dir():
            continue
        relative = candidate.relative_to(root).as_posix()
        actual_paths.add(relative)
    extra = actual_paths - declared_paths
    for path in sorted(extra):
        reasons.append(f"{path}: present on disk but not declared in the descriptor")

    return tuple(reasons)
