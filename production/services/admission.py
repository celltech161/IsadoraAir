"""The validation service's admission limits: ONE canonical domain.

How many validations ``isadoraair-validation`` runs at once, and how many
more admitted requests may wait for a run slot, protect the on-air host from
validation resource exhaustion (each run may use a full CPU and 512 MiB). An
operator may choose values inside the hard domain below -- through Django
admin (Production media -> Validation limits, stored in ``.env`` as
PRODUCTION_VALIDATION_MAX_ACTIVE / PRODUCTION_VALIDATION_MAX_PENDING), or the
service's ``--max-active`` / ``--max-pending`` -- but never outside it.

Every route converges here: the admin form's validator, the service's
settings/environment reading, its command line, and the ValidationService
constructor itself (authoritative: the service refuses to start on any value
outside the domain -- it never clamps or reinterprets one).

Accepted: a real ``int`` (never ``bool``) or a string of plain ASCII decimal
digits without sign, whitespace or leading zeros, inside the range. Rejected:
booleans, floats, fractional/scientific/hex/signed/padded strings, anything
longer than three digits, values outside the range, everything else.
"""
from __future__ import annotations

import json
import os
import re

ACTIVE_DEFAULT, ACTIVE_MIN, ACTIVE_MAX = 2, 1, 4
PENDING_DEFAULT, PENDING_MIN, PENDING_MAX = 4, 0, 8
ACTIVE_LABEL = "Concurrent validations"
PENDING_LABEL = "Pending validation requests"
# The running service reports the limits it actually enforces here (beside its
# socket), so the admin page can say whether a saved change is in effect yet.
STATUS_FILENAME = "admission.json"

_PLAIN_DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,2})", re.ASCII)


class AdmissionConfigError(ValueError):
    """A configured admission limit is outside its hard domain."""


def _parse(value, *, label, minimum, maximum) -> int:
    domain = f"{label} must be between {minimum} and {maximum}."
    if isinstance(value, bool):
        raise AdmissionConfigError(f"{domain} A true/false value is not a number.")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        if not _PLAIN_DECIMAL.fullmatch(value):
            raise AdmissionConfigError(f"{domain} Enter a whole number using digits only.")
        number = int(value)
    else:
        raise AdmissionConfigError(f"{domain} Enter a whole number using digits only.")
    if not minimum <= number <= maximum:
        raise AdmissionConfigError(domain)
    return number


def parse_active(value) -> int:
    return _parse(value, label=ACTIVE_LABEL, minimum=ACTIVE_MIN, maximum=ACTIVE_MAX)


def parse_pending(value) -> int:
    return _parse(value, label=PENDING_LABEL, minimum=PENDING_MIN, maximum=PENDING_MAX)


# -- what the running service enforces (display only) ----------------------------

def status_path(socket_path: str) -> str:
    return os.path.join(os.path.dirname(socket_path), STATUS_FILENAME)


def write_status(socket_path: str, max_active: int, max_pending: int) -> None:
    """Called by the service once it listens: its effective limits, 0600."""
    path = status_path(socket_path)
    temporary = f"{path}.{os.getpid()}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, json.dumps({"max_active": max_active, "max_pending": max_pending}).encode())
    finally:
        os.close(fd)
    os.replace(temporary, path)


def remove_status(socket_path: str) -> None:
    try:
        os.unlink(status_path(socket_path))
    except OSError:
        pass


def running_limits(socket_path: str) -> dict | None:
    """{"max_active": n, "max_pending": m} as reported by the running service,
    or None when it is not running / not reporting / reporting nonsense."""
    try:
        with open(status_path(socket_path), "rb") as handle:
            data = json.loads(handle.read(4096))
        return {"max_active": parse_active(data["max_active"]), "max_pending": parse_pending(data["max_pending"])}
    except (OSError, ValueError, KeyError, TypeError):
        return None
