"""Read-only readiness of ``isadoraair-render`` for Monitoring (2.22C2A).

Monitoring OBSERVES the render service; it never renders, never changes its
admission configuration, never touches its cgroup subtree and never starts,
stops or restarts it. Evidence, all through the service's public surfaces:

1. the socket file -- present and a socket;
2. the status file the service keeps beside it (``status.json``): its
   admission limits, how many renders are running and waiting right now, and
   the last failure code it saw (with a timestamp);
3. a handshake: connect, send nothing, close the writing half. An empty
   request is the protocol's ``invalid_request`` -- answered without reading
   a descriptor or running anything; at capacity the accept loop answers
   ``busy`` without admitting the connection. Either proves the accept loop,
   admission and the request path are alive. Bounded by a short deadline.

Every failure is reported, never raised.
"""
from __future__ import annotations

import errno
import json
import os
import socket
import stat
import time

from . import render_service

HANDSHAKE_TIMEOUT_SECONDS = 2.0
REPLY_LIMIT_BYTES = 4096

SOCKET_MISSING = "socket_missing"
SOCKET_INVALID = "socket_invalid"
NOT_LISTENING = "not_listening"
UNREACHABLE = "socket_unreachable"
UNRESPONSIVE = "unresponsive"
UNEXPECTED_REPLY = "unexpected_reply"
STATUS_MISSING = "status_missing"
STATUS_MALFORMED = "status_malformed"
DEGRADED_REASONS = frozenset({STATUS_MISSING, STATUS_MALFORMED})


def observe(socket_path: str | None = None, *, timeout: float = HANDSHAKE_TIMEOUT_SECONDS) -> dict:
    path = socket_path or render_service.socket_path()
    result = {"ready": False, "reason": None, "max_active": None, "max_pending": None, "active": None,
              "pending": None, "at_capacity": False, "last_failure": None}
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
    snapshot = render_service.status_snapshot(path)
    if snapshot is None:
        status = os.path.join(os.path.dirname(path), render_service.STATUS_FILENAME)
        result["reason"] = STATUS_MISSING if not os.path.lexists(status) else STATUS_MALFORMED
        return result
    result.update(snapshot)
    result["ready"] = True
    return result


def handshake(path: str, *, timeout: float = HANDSHAKE_TIMEOUT_SECONDS):
    """(answer, None) for a live service -- answer is "error" (the empty
    request) or "busy" -- else (None, reason)."""
    deadline = time.monotonic() + timeout
    data = b""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
    try:
        sock.settimeout(timeout)
        try:
            sock.connect(path)
        except ConnectionRefusedError:
            return None, NOT_LISTENING
        except BlockingIOError:
            return None, UNRESPONSIVE
        sock.shutdown(socket.SHUT_WR)
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
        if exc.errno in (errno.ECONNREFUSED, errno.ENOENT, errno.ECONNRESET, errno.EPIPE):
            return None, NOT_LISTENING
        return None, UNREACHABLE
    finally:
        sock.close()
    try:
        reply = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, UNEXPECTED_REPLY
    if not isinstance(reply, dict) or (reply.get("status"), reply.get("code")) not in {
            ("error", "invalid_request"), ("busy", "busy")}:
        return None, UNEXPECTED_REPLY
    return reply["status"], None
