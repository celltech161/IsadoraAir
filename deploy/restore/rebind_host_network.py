#!/usr/bin/env python3
"""Rebind host-local Django network identity after bare-metal recovery.

A backup's .env legitimately carries station configuration and secrets, but its
private LAN address is machine-specific.  Restoring that address verbatim onto
replacement hardware leaves Django rejecting requests to the replacement host
until ALLOWED_HOSTS/CSRF_TRUSTED_ORIGINS are edited manually.

This helper performs one narrow, deterministic rewrite:
  * preserve localhost/127.0.0.1 and all non-IP hostnames/domains;
  * remove stale private/link-local IPv4/IPv6 literals inherited from backup;
  * add the replacement machine's detected primary IP and hostname;
  * for CSRF origins, preserve each stale private-IP origin's scheme/port shape
    while substituting the new IP, and add equivalent hostname origins;
  * preserve every unrelated .env line byte-for-byte apart from newline
    normalization on the two rewritten keys.

No secret values are printed.  The write is atomic and preserves the existing
file mode.  --plan reports only the detected hostname/IP and whether each key
would change.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from urllib.parse import urlsplit, urlunsplit


KEY_ALLOWED = "ALLOWED_HOSTS"
KEY_CSRF = "CSRF_TRUSTED_ORIGINS"


def _parse_ip(value: str):
    try:
        return ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        return None


def _is_stale_local_ip(value: str) -> bool:
    address = _parse_ip(value)
    if address is None:
        return False
    if address.is_loopback:
        return False
    return bool(address.is_private or address.is_link_local)


def detect_primary_ip() -> str:
    """Return the kernel-selected primary global address, preferring IPv4."""
    commands = (
        ["ip", "-4", "route", "get", "1.1.1.1"],
        ["ip", "-4", "-o", "addr", "show", "scope", "global"],
        ["ip", "-6", "-o", "addr", "show", "scope", "global"],
    )
    for command in commands:
        try:
            completed = subprocess.run(command, check=True, text=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            continue
        tokens = completed.stdout.replace("\n", " ").split()
        if command[1:4] == ["-4", "route", "get"]:
            if "src" in tokens:
                candidate = tokens[tokens.index("src") + 1]
                if _parse_ip(candidate) is not None:
                    return candidate
        else:
            for index, token in enumerate(tokens[:-1]):
                if token in {"inet", "inet6"}:
                    candidate = tokens[index + 1].split("/", 1)[0]
                    address = _parse_ip(candidate)
                    if address is not None and not address.is_loopback and not address.is_link_local:
                        return candidate
    raise RuntimeError("could not detect a primary non-loopback IP address")


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def rebind_allowed_hosts(raw: str, *, hostname: str, primary_ip: str) -> str:
    values = [item.strip() for item in raw.split(",") if item.strip()]
    kept = [item for item in values if not _is_stale_local_ip(item)]
    for required in ("localhost", "127.0.0.1", hostname, primary_ip):
        if required:
            kept.append(required)
    return ",".join(_dedupe(kept))


def _origin_with_host(origin: str, new_host: str) -> str | None:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = f"[{new_host}]" if ":" in new_host and not new_host.startswith("[") else new_host
    netloc = host
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def rebind_csrf_origins(raw: str, *, hostname: str, primary_ip: str) -> str:
    values = [item.strip() for item in raw.split(",") if item.strip()]
    kept: list[str] = []
    inherited_shapes: list[str] = []
    for origin in values:
        try:
            parsed = urlsplit(origin)
            origin_host = parsed.hostname
        except ValueError:
            origin_host = None
        if origin_host and _is_stale_local_ip(origin_host):
            inherited_shapes.append(origin)
            continue
        kept.append(origin)

    # Always provide direct HTTPS access on the recovered host.
    kept.extend((f"https://{hostname}", f"https://{primary_ip}"))

    # Preserve any explicitly-configured scheme/port patterns that belonged to
    # the old private address (for example http://192.168.1.125:8000).
    for origin in inherited_shapes:
        replacement = _origin_with_host(origin, primary_ip)
        if replacement:
            kept.append(replacement)
        replacement = _origin_with_host(origin, hostname)
        if replacement:
            kept.append(replacement)

    return ",".join(_dedupe(kept))


def _read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    for line in lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {KEY_ALLOWED, KEY_CSRF} and key not in values:
            values[key] = value
    return lines, values


def _rewrite(lines: list[str], replacements: dict[str, str]) -> str:
    output: list[str] = []
    written: set[str] = set()
    for line in lines:
        if line and not line.lstrip().startswith("#") and "=" in line:
            key = line.split("=", 1)[0]
            if key in replacements and key not in written:
                output.append(f"{key}={replacements[key]}")
                written.add(key)
                continue
        output.append(line)
    for key in (KEY_ALLOWED, KEY_CSRF):
        if key in replacements and key not in written:
            output.append(f"{key}={replacements[key]}")
    return "\n".join(output) + "\n"


def atomic_write(path: Path, content: str) -> None:
    mode = path.stat().st_mode & 0o777
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.rebind-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--hostname", help="Test/operator override; defaults to socket.gethostname().")
    parser.add_argument("--primary-ip", help="Test/operator override; defaults to kernel route detection.")
    args = parser.parse_args(argv)

    if not args.env.is_file():
        parser.error(f"environment file not found: {args.env}")

    hostname = (args.hostname or socket.gethostname()).strip()
    primary_ip = (args.primary_ip or detect_primary_ip()).strip()
    if not hostname or any(char.isspace() for char in hostname):
        parser.error("detected hostname is empty or contains whitespace")
    if _parse_ip(primary_ip) is None:
        parser.error(f"detected primary address is not a valid IP: {primary_ip}")

    lines, existing = _read_env(args.env)
    allowed = rebind_allowed_hosts(existing.get(KEY_ALLOWED, ""), hostname=hostname, primary_ip=primary_ip)
    csrf = rebind_csrf_origins(existing.get(KEY_CSRF, ""), hostname=hostname, primary_ip=primary_ip)
    replacements = {KEY_ALLOWED: allowed, KEY_CSRF: csrf}
    content = _rewrite(lines, replacements)
    changed = content != args.env.read_text(encoding="utf-8")

    print(f"Detected recovery hostname: {hostname}")
    print(f"Detected recovery primary IP: {primary_ip}")
    print(f"{KEY_ALLOWED}: {'would change' if args.plan and changed else 'updated' if args.apply and changed else 'unchanged'}")
    print(f"{KEY_CSRF}: {'would change' if args.plan and changed else 'updated' if args.apply and changed else 'unchanged'}")

    if args.apply and changed:
        atomic_write(args.env, content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
