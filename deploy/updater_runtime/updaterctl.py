#!/usr/bin/python3
"""Fixed, read-only readiness probe for the installed protected daemon."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket


SOCKET_PATH = Path("/run/isadoraair-updater/updater.sock")
PROTOCOL_VERSION = 3
MAX_RESPONSE_BYTES = 131072


def ping() -> dict:
    request = json.dumps(
        {"protocol_version": PROTOCOL_VERSION, "action": "PING"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    response = bytearray()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(SOCKET_PATH))
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        while len(response) <= MAX_RESPONSE_BYTES:
            chunk = connection.recv(min(4096, MAX_RESPONSE_BYTES + 1 - len(response)))
            if not chunk:
                break
            response.extend(chunk)
    if len(response) > MAX_RESPONSE_BYTES:
        raise RuntimeError("response exceeds protocol limit")
    result = json.loads(bytes(response).decode("utf-8"))
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("protected updater PING failed")
    if result.get("protocol_version") != PROTOCOL_VERSION:
        raise RuntimeError("protected updater protocol is incompatible")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("action", choices=("ping",))
    args = parser.parse_args()
    if args.action == "ping":
        print(json.dumps(ping(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
