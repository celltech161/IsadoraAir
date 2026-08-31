#!/usr/bin/env python3
"""Immutable Update Center Phase-D bootstrap supervisor entrypoint.

NOT installed or run anywhere by this workorder (implementation +
tests only -- see docs/UPDATE_CENTER_PHASE_D.md, D4-T's own explicit
scope exclusions). This is the exact shape the future systemd unit
(deploy/updater-bootstrapd.service, a draft, also not installed) would
invoke: `/usr/bin/python3 -I updater_bootstrapd.py --config
/etc/isadoraair/updater-bootstrap.json --application-root ... --worker-
config /etc/isadoraair/station.json`.

Deliberately thin: argument parsing, a root check, config/trust-policy
loading, and handing off to isadoraair_updater_bootstrap.supervisor_
daemon.SupervisorDaemon -- no business logic lives in this file itself,
so the file a systemd unit actually execs stays trivially reviewable
even as the supervisor package underneath it grows."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from isadoraair_updater_bootstrap.config import ConfigError, validate_config_dict  # noqa: E402
from isadoraair_updater_bootstrap.trust import TrustPolicyError, parse_trust_policy_dict  # noqa: E402


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="updater_bootstrapd.py", allow_abbrev=False)
    parser.add_argument("--config", required=True, help="Path to the bootstrap config JSON file")
    parser.add_argument(
        "--application-root", required=True,
        help="The application checkout root, for the config's own overlap check",
    )
    parser.add_argument(
        "--worker-config", required=True,
        help="Path to the WORKER's own station config JSON -- passed through unread/"
             "unparsed to every worker this supervisor launches (--config on the "
             "worker's own argv); this supervisor itself never parses application/"
             "station-policy concerns (D4-A's own explicit boundary).",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if os.geteuid() != 0:
        sys.stderr.write("updater_bootstrapd: refuses to run as a non-root effective UID\n")
        return 1
    try:
        raw = Path(args.config).read_text(encoding="utf-8")
        config = validate_config_dict(json.loads(raw), application_root=Path(args.application_root))
    except (OSError, ValueError, ConfigError) as exc:
        sys.stderr.write(f"updater_bootstrapd: invalid bootstrap configuration: {exc}\n")
        return 1
    try:
        trust_raw = json.loads(config.trust_policy_path.read_text(encoding="utf-8"))
        trust_policy = parse_trust_policy_dict(trust_raw, signer_directory=config.signer_root)
    except (OSError, ValueError, TrustPolicyError) as exc:
        sys.stderr.write(f"updater_bootstrapd: invalid trust policy: {exc}\n")
        return 1

    from isadoraair_updater_bootstrap.supervisor_daemon import SupervisorDaemon

    daemon = SupervisorDaemon(config, trust_policy, worker_config_path=Path(args.worker_config))
    try:
        daemon.start()
    finally:
        daemon.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
