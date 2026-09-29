#!/usr/bin/python3
"""Protected-runtime control utility, including bootstrap plan approval."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import socket
import sys
import uuid


SOCKET_PATH = Path("/run/isadoraair-updater/updater.sock")
PROTOCOL_VERSION = 4
MAX_RESPONSE_BYTES = 131072


def request(payload: dict) -> dict:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(raw) > 8192:
        raise RuntimeError("request exceeds protocol limit")
    response = bytearray()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(SOCKET_PATH))
        connection.sendall(raw)
        connection.shutdown(socket.SHUT_WR)
        while len(response) <= MAX_RESPONSE_BYTES:
            chunk = connection.recv(min(4096, MAX_RESPONSE_BYTES + 1 - len(response)))
            if not chunk:
                break
            response.extend(chunk)
    if len(response) > MAX_RESPONSE_BYTES:
        raise RuntimeError("response exceeds protocol limit")
    try:
        result = json.loads(bytes(response).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError("protected updater returned invalid JSON") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        detail = result.get("detail", "protected updater rejected the request") if isinstance(result, dict) else "invalid response"
        raise RuntimeError(str(detail)[:500])
    return result


def ping() -> dict:
    result = request({"protocol_version": PROTOCOL_VERSION, "action": "PING"})
    if result.get("protocol_version") != PROTOCOL_VERSION:
        raise RuntimeError("protected updater protocol is incompatible")
    return result


def _require_root() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("migration review and approval require OS root")


def _job_status(job_id: str) -> dict:
    result = request({
        "protocol_version": PROTOCOL_VERSION, "action": "GET_JOB_STATUS", "job_id": job_id,
    })
    job = result.get("job")
    if not isinstance(job, dict):
        raise RuntimeError("protected updater returned invalid job evidence")
    return job


def _review(job_id: str) -> tuple[dict, dict]:
    job = _job_status(job_id)
    review = job.get("migration_plan_review")
    plan = job.get("trusted_plan")
    if (job.get("state") != "manual_intervention_required"
            or job.get("failure_classification") != "MIGRATION_OPERATION_MANUAL"
            or not isinstance(review, dict) or not isinstance(plan, dict)):
        raise RuntimeError("job is not a terminal reviewed-migration decision point")
    required = {
        "release_id", "target_commit", "manifest_sha256",
        "migration_plan_digest", "trusted_plan_fingerprint", "manual_operations",
    }
    if set(review) != required or not isinstance(review["manual_operations"], list):
        raise RuntimeError("job review evidence is incomplete")
    return job, review


def print_review(job_id: str) -> dict:
    _job, review = _review(job_id)
    print("WARNING: this migration plan contains operations outside the automatic mechanical allowlist.")
    print("Approval applies only to this exact protected plan identity and does not resume the discovery job.")
    print(f"Discovery job: {job_id}")
    print(f"Target release: {review['release_id']}")
    print(f"Target commit: {review['target_commit']}")
    print(f"Manifest SHA-256: {review['manifest_sha256']}")
    print(f"Trusted-plan fingerprint: {review['trusted_plan_fingerprint']}")
    print(f"Migration-plan digest: {review['migration_plan_digest']}")
    print("Manual operations:")
    for entry in review["manual_operations"]:
        print(
            f"  - {entry.get('ref')} operation[{entry.get('operation_index')}]: "
            f"{entry.get('operation')} — {entry.get('detail')}"
        )
    return review


def approve(job_id: str, operator: str) -> dict:
    review = print_review(job_id)
    print()
    confirmation = input("Type the full migration-plan digest to approve: ").strip()
    if confirmation != review["migration_plan_digest"]:
        raise RuntimeError("digest confirmation does not match; no approval was created")
    reason = input("Written approval reason (required): ").strip()
    if not reason:
        raise RuntimeError("approval reason is required; no approval was created")
    result = request({
        "protocol_version": PROTOCOL_VERSION,
        "action": "APPROVE_MIGRATION_PLAN",
        "job_id": job_id,
        "confirmed_migration_plan_digest": confirmation,
        "approved_by_username": operator,
        "reason": reason,
    })
    if not isinstance(result.get("approval"), dict):
        raise RuntimeError("protected updater returned invalid approval evidence")
    return result


def canonical_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise argparse.ArgumentTypeError("job id must be a canonical lowercase UUID") from exc
    if str(parsed) != value:
        raise argparse.ArgumentTypeError("job id must be a canonical lowercase UUID")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("ping", allow_abbrev=False)
    review_parser = subparsers.add_parser("migration-review", allow_abbrev=False)
    review_parser.add_argument("job_id", type=canonical_uuid)
    approve_parser = subparsers.add_parser("approve-migration-plan", allow_abbrev=False)
    approve_parser.add_argument("job_id", type=canonical_uuid)
    approve_parser.add_argument("--operator", default=getpass.getuser())
    args = parser.parse_args()
    try:
        if args.action == "ping":
            print(json.dumps(ping(), sort_keys=True))
        elif args.action == "migration-review":
            _require_root()
            print_review(args.job_id)
        else:
            _require_root()
            print(json.dumps(approve(args.job_id, args.operator), sort_keys=True))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"updaterctl: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
