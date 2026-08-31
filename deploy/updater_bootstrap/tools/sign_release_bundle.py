#!/usr/bin/env python3
"""Deterministic, unprivileged Phase-D release bundle CLI.

The workflow is intentionally explicit: build, inspect, emit statement, sign
with a named key, and validate. Nothing here installs or activates a runtime.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys

from protected_runtime_release import (
    ReleaseAuthoringError,
    build_descriptor,
    build_statement,
    generation_one_policy_bytes,
    sign_statement,
    validate_descriptor_inventory,
)


def _write(path: str, content: bytes) -> None:
    destination = Path(path)
    destination.write_bytes(content)
    print(f"wrote {destination} ({len(content)} bytes)")


def _build(args: argparse.Namespace) -> int:
    descriptor = build_descriptor(
        runtime_root=Path(args.runtime_root), generation=args.generation,
        runtime_version=args.runtime_version,
        manifest_protocol_version=args.manifest_protocol_version,
        supported_wire_protocols=tuple(args.wire_protocol),
    )
    _write(args.output, descriptor)
    print(f"descriptor_sha256={hashlib.sha256(descriptor).hexdigest()}")
    return 0


def _statement(args: argparse.Namespace) -> int:
    descriptor = Path(args.descriptor).read_bytes()
    statement = build_statement(
        descriptor_bytes=descriptor, release_id=args.release_id,
        previous_release_id=args.previous_release_id, generation=args.generation,
    )
    _write(args.output, statement)
    return 0


def _sign(args: argparse.Namespace) -> int:
    signature = sign_statement(
        statement=Path(args.statement).read_bytes(),
        private_key_path=Path(args.private_key), public_key_path=Path(args.public_key),
    )
    _write(args.output, signature)
    print(f"signature verified against explicit public key {args.public_key}")
    return 0


def _validate(args: argparse.Namespace) -> int:
    descriptor_bytes = Path(args.descriptor).read_bytes()
    descriptor = validate_descriptor_inventory(
        descriptor_bytes=descriptor_bytes, runtime_root=Path(args.runtime_root),
    )
    if descriptor.generation != args.generation:
        raise ReleaseAuthoringError(
            f"descriptor generation {descriptor.generation} does not equal expected {args.generation}"
        )
    print(
        f"protected runtime descriptor valid: generation={descriptor.generation} "
        f"files={len(descriptor.files)} bundle_sha256={descriptor.bundle_sha256}"
    )
    return 0


def _generation_one_policy(args: argparse.Namespace) -> int:
    _write(args.output, generation_one_policy_bytes())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build-descriptor", allow_abbrev=False)
    build.add_argument("--runtime-root", required=True)
    build.add_argument("--generation", required=True, type=int)
    build.add_argument("--runtime-version", required=True, type=int)
    build.add_argument("--manifest-protocol-version", required=True, type=int)
    build.add_argument("--wire-protocol", required=True, type=int, action="append")
    build.add_argument("--output", required=True)
    build.set_defaults(func=_build)

    statement = commands.add_parser("statement", allow_abbrev=False)
    statement.add_argument("--descriptor", required=True)
    statement.add_argument("--release-id", required=True)
    statement.add_argument("--previous-release-id")
    statement.add_argument("--generation", required=True, type=int)
    statement.add_argument("--output", required=True)
    statement.set_defaults(func=_statement)

    sign = commands.add_parser("sign", allow_abbrev=False)
    sign.add_argument("--statement", required=True)
    sign.add_argument("--private-key", required=True)
    sign.add_argument("--public-key", required=True)
    sign.add_argument("--output", required=True)
    sign.set_defaults(func=_sign)

    validate = commands.add_parser("validate-descriptor", allow_abbrev=False)
    validate.add_argument("--runtime-root", required=True)
    validate.add_argument("--descriptor", required=True)
    validate.add_argument("--generation", required=True, type=int)
    validate.set_defaults(func=_validate)

    generation_one = commands.add_parser("generation-one-policy", allow_abbrev=False)
    generation_one.add_argument("--output", required=True)
    generation_one.set_defaults(func=_generation_one_policy)
    return parser


def main(argv=None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return args.func(args)
    except (OSError, ReleaseAuthoringError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
