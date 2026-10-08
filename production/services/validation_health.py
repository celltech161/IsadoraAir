"""Read-only readiness of ``isadoraair-validation`` for Monitoring (r0108).

Monitoring OBSERVES the validation service; it is never a second lifecycle
controller. Nothing here sends a validation request, runs a validator, reads
or changes admission configuration, touches the service's cgroup subtree, or
starts/stops/restarts anything. Three pieces of evidence, all through the
service's public surfaces:

1. the socket file -- present and a socket;
2. the status file the service writes once it is listening (``admission.json``,
   production.services.admission) -- present and inside the admission domain;
3. a handshake through the socket: connect, send nothing, close the writing
   half. An empty request is the protocol's ``incomplete request``: the
   service answers ``unavailable`` without reading a descriptor, running or
   logging anything; at capacity it answers ``busy`` from its accept loop
   without admitting the connection. Either answer proves the accept loop,
   admission and the request path are alive. Bounded by a short deadline.

Every failure is reported, never raised: a malformed or hostile reply, a
missing socket, a hung service -- Monitoring keeps running either way.
"""
from __future__ import annotations

import errno
import json
import os
import socket
import stat
import time

from . import admission

HANDSHAKE_TIMEOUT_SECONDS = 2.0
REPLY_LIMIT_BYTES = 4096
# The only answers an empty request can get from a live service.
HANDSHAKE_REPLIES = frozenset({"unavailable", "busy"})

# Reasons, most to least severe. Status-file problems while the handshake
# succeeds leave validation working (only the reported limits are untrustworthy).
SOCKET_MISSING = "socket_missing"
SOCKET_INVALID = "socket_invalid"
NOT_LISTENING = "not_listening"
UNREACHABLE = "socket_unreachable"
UNRESPONSIVE = "unresponsive"
UNEXPECTED_REPLY = "unexpected_reply"
STATUS_MISSING = "status_missing"
STATUS_MALFORMED = "status_malformed"
DEGRADED_REASONS = frozenset({STATUS_MISSING, STATUS_MALFORMED})


def default_socket_path() -> str:
    from django.conf import settings
    return settings.PRODUCTION_VALIDATION_SOCKET


def observe(socket_path: str | None = None, *, timeout: float = HANDSHAKE_TIMEOUT_SECONDS) -> dict:
    """``{"ready": bool, "reason": str | None, "max_active", "max_pending",
    "at_capacity"}`` -- ``ready`` only when the socket answered the handshake
    AND the service reports valid limits."""
    path = socket_path or default_socket_path()
    result = {"ready": False, "reason": None, "max_active": None, "max_pending": None, "at_capacity": False}
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        result["reason"] = SOCKET_MISSING
        return result
    except OSError:
        result["reason"] = UNREACHABLE
        return result
    if not stat.S_ISSOCK(info.st_mode):
        result["reason"] = SOCKET_INVALID
        return result

    answer, reason = handshake(path, timeout=timeout)
    if reason is not None:
        result["reason"] = reason
        return result
    result["at_capacity"] = answer == "busy"

    limits = admission.running_limits(path)
    if limits is None:
        result["reason"] = STATUS_MISSING if not os.path.lexists(admission.status_path(path)) else STATUS_MALFORMED
        return result
    result.update(limits)
    result["ready"] = True
    return result


def handshake(path: str, *, timeout: float = HANDSHAKE_TIMEOUT_SECONDS) -> tuple[str | None, str | None]:
    """(answer, None) for a live service, (None, reason) otherwise."""
    deadline = time.monotonic() + timeout
    data = b""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
    try:
        sock.settimeout(timeout)
        try:
            sock.connect(path)
        except ConnectionRefusedError:
            return None, NOT_LISTENING                    # a stale socket file: nobody listens
        except BlockingIOError:
            return None, UNRESPONSIVE                     # backlog full: the accept loop is stuck
        sock.shutdown(socket.SHUT_WR)                     # the empty request
        while not data.endswith(b"\n") and len(data) < REPLY_LIMIT_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, UNRESPONSIVE
            sock.settimeout(remaining)
            chunk = sock.recv(REPLY_LIMIT_BYTES - len(data))
            if not chunk:
                break
            data += chunk
    except (socket.timeout, TimeoutError):
        return None, UNRESPONSIVE
    except OSError as exc:
        # ECONNRESET/EPIPE: queued on a listener that then went away (the
        # service died or is restarting) -- nobody is listening any more.
        if exc.errno in (errno.ECONNREFUSED, errno.ENOENT, errno.ECONNRESET, errno.EPIPE):
            return None, NOT_LISTENING
        return None, UNREACHABLE
    finally:
        sock.close()
    try:
        reply = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, UNEXPECTED_REPLY
    if not isinstance(reply, dict) or reply.get("status") not in HANDSHAKE_REPLIES:
        return None, UNEXPECTED_REPLY
    return reply["status"], None
