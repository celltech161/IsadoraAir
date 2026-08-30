#!/usr/bin/env python3
"""D2-Q: unprivileged development/release-side helper for producing a
signed protected-runtime bundle. Never runs on station root runtime --
this lives under deploy/updater_bootstrap/tools/, entirely separate
from both the worker tree and the supervisor tree, and nothing in
either of those imports it. No web UI, no private-key storage: signing
itself is delegated to a user-selected LOCAL command the operator
supplies on the command line (e.g. an `openssl pkeyutl -sign -inkey
...` invocation pointed at a key on their own machine, or a hardware-
token wrapper) -- this script never reads, generates, or embeds a
private key anywhere.

Reuses deploy/updater_runtime/protected_bootstrap's own descriptor/
attestation contracts directly (this is a development tool building a
bundle FOR the worker to eventually consume, not the immutable
supervisor -- Correction 1's independence requirement applies only to
the supervisor tree, not here; importing D1's already-reviewed schema
avoids a third independent reimplementation of the identical contract).

Three subcommands, meant to be run in sequence:
  build-descriptor  -- hash every file under a source tree, write descriptor.json
  statement          -- print/write the exact bytes a signer must sign
  sign               -- run the operator's own signing command, then
                         VERIFY the resulting signature against a
                         supplied public key before accepting it
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

_TOOLS_DIR = Path(__file__).resolve().parent
_RUNTIME_ROOT = _TOOLS_DIR.parents[1] / "updater_runtime"
sys.path.insert(0, str(_RUNTIME_ROOT))

from protected_bootstrap.attestation import build_attestation_statement, verify_ed25519  # noqa: E402
from protected_bootstrap.descriptor import FileEntry, compute_bundle_sha256, hash_file  # noqa: E402


def _build_descriptor(args: argparse.Namespace) -> int:
    source_dir = Path(args.source_dir).resolve()
    entries = []
    for path in sorted(source_dir.rglob("*")):
        if path.is_dir():
            continue
        if path.is_symlink():
            print(f"error: {path} is a symlink -- a signed bundle cannot contain one", file=sys.stderr)
            return 1
        relative = path.relative_to(source_dir).as_posix()
        mode = "0755" if relative == args.entrypoint else "0644"
        entries.append({
            "path": relative, "sha256": hash_file(path), "mode": mode, "size_bytes": path.stat().st_size,
        })
    entries.sort(key=lambda e: e["path"])
    file_objects = tuple(FileEntry(e["path"], e["sha256"], e["mode"], e["size_bytes"]) for e in entries)
    descriptor = {
        "schema_version": 1,
        "generation": args.generation,
        "runtime_version": args.runtime_version,
        "manifest_protocol_version": args.manifest_protocol_version,
        "supported_wire_protocols": sorted(args.wire_protocol),
        "entrypoint": args.entrypoint,
        "files": entries,
        "bundle_sha256": compute_bundle_sha256(file_objects),
    }
    Path(args.output).write_text(json.dumps(descriptor, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.output} ({len(entries)} files, bundle_sha256={descriptor['bundle_sha256']})")
    return 0


def _statement(args: argparse.Namespace) -> int:
    descriptor_bytes = Path(args.descriptor).read_bytes()
    import hashlib
    descriptor_sha256 = hashlib.sha256(descriptor_bytes).hexdigest()
    statement = build_attestation_statement(
        release_id=args.release_id, previous_release_id=args.previous_release_id,
        generation=args.generation, descriptor_sha256=descriptor_sha256,
    )
    Path(args.output).write_bytes(statement)
    print(f"wrote {args.output} ({len(statement)} bytes); descriptor_sha256={descriptor_sha256}")
    return 0


def _sign(args: argparse.Namespace) -> int:
    """Ed25519 oneshot signing (openssl pkeyutl -sign -rawin, or an
    equivalent tool) needs a real seekable file, not a stdin pipe --
    openssl itself cannot determine the input size from a pipe for a
    oneshot operation. The statement is therefore always written to a
    real temporary file; a `{statement}` placeholder in --sign-command
    is substituted with that path (e.g. `openssl pkeyutl -sign -inkey
    key.pem -rawin -in {statement}`). If the command has no such
    placeholder, the path is appended as a trailing argument instead,
    for a signing tool that takes its input path positionally."""
    statement = Path(args.statement).read_bytes()
    with tempfile.NamedTemporaryFile(prefix="isadoraair-sign-statement-") as statement_file:
        statement_file.write(statement)
        statement_file.flush()
        command_text = args.sign_command
        if "{statement}" in command_text:
            command = shlex.split(command_text.replace("{statement}", statement_file.name))
        else:
            command = [*shlex.split(command_text), statement_file.name]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        print(f"error: sign command exited {result.returncode}: {result.stderr.decode(errors='replace')}", file=sys.stderr)
        return 1
    signature = result.stdout
    outcome = verify_ed25519(public_key_path=Path(args.public_key), statement=statement, signature=signature)
    if not outcome.verified:
        print(f"error: produced signature does not verify against {args.public_key}: {outcome.detail}", file=sys.stderr)
        return 1
    Path(args.output).write_bytes(signature)
    print(f"wrote {args.output} -- verified against {args.public_key}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sign_release_bundle.py")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build-descriptor")
    build.add_argument("--source-dir", required=True)
    build.add_argument("--entrypoint", required=True)
    build.add_argument("--generation", type=int, required=True)
    build.add_argument("--runtime-version", type=int, required=True)
    build.add_argument("--manifest-protocol-version", type=int, required=True)
    build.add_argument("--wire-protocol", type=int, action="append", required=True)
    build.add_argument("--output", required=True)
    build.set_defaults(func=_build_descriptor)

    statement = subparsers.add_parser("statement")
    statement.add_argument("--descriptor", required=True)
    statement.add_argument("--release-id", required=True)
    statement.add_argument("--previous-release-id", default=None)
    statement.add_argument("--generation", type=int, required=True)
    statement.add_argument("--output", required=True)
    statement.set_defaults(func=_statement)

    sign = subparsers.add_parser("sign")
    sign.add_argument("--statement", required=True)
    sign.add_argument(
        "--sign-command", required=True,
        help="A local command that signs the statement and writes a raw 64-byte Ed25519 signature "
             "to stdout. Use {statement} as a placeholder for the statement's temp file path, e.g. "
             "'openssl pkeyutl -sign -inkey /path/to/private.pem -rawin -in {statement}' -- if no "
             "placeholder is present, the path is appended as a trailing argument instead. "
             "This script never sees or stores the private key itself.",
    )
    sign.add_argument("--public-key", required=True, help="Verify the produced signature before accepting it")
    sign.add_argument("--output", required=True)
    sign.set_defaults(func=_sign)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
