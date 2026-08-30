#!/usr/bin/env python3
"""Immutable Update Center Phase-D bootstrap supervisor entrypoint.

NOT installed or run anywhere by this workorder (D2 is implementation
+ tests only -- see docs/UPDATE_CENTER_PHASE_D.md). This is the exact
shape the future systemd unit (deploy/updater-bootstrapd.service, a
draft, also not installed) would invoke: `/usr/bin/python3 -I
updater_bootstrapd.py --config /etc/isadoraair/updater-bootstrap.json`.

Deliberately thin: argument parsing, a root check, config loading, and
handing off to isadoraair_updater_bootstrap's own modules -- no
business logic lives in this file itself, so the file a systemd unit
actually execs stays trivially reviewable even as the supervisor
package underneath it grows."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from isadoraair_updater_bootstrap.config import ConfigError, validate_config_dict  # noqa: E402


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="updater_bootstrapd.py", allow_abbrev=False)
    parser.add_argument("--config", required=True, help="Path to the bootstrap config JSON file")
    parser.add_argument(
        "--application-root", required=True,
        help="The application checkout root, for the config's own overlap check",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if os.geteuid() != 0:
        sys.stderr.write("updater_bootstrapd: refuses to run as a non-root effective UID\n")
        return 1
    import json
    try:
        raw = Path(args.config).read_text(encoding="utf-8")
        config = validate_config_dict(json.loads(raw), application_root=Path(args.application_root))
    except (OSError, ValueError, ConfigError) as exc:
        sys.stderr.write(f"updater_bootstrapd: invalid bootstrap configuration: {exc}\n")
        return 1
    # The real event loop (accept connections on config.activation_socket,
    # load/recover RuntimeState, launch/monitor the active slot's worker)
    # is D2's state-machine/protocol work made runnable end-to-end -- an
    # actual integration slice, not exercised by this entrypoint script
    # in this workorder. Every piece it would call is independently
    # implemented and tested under isadoraair_updater_bootstrap/.
    sys.stderr.write(
        f"updater_bootstrapd: configuration valid (slots_root={config.slots_root}); "
        "event loop not implemented in this workorder\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
