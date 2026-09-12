"""Bounded Remote DJ connection-attempt identity and telemetry helpers."""

import re
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from django.core.signing import BadSignature, TimestampSigner


TOKEN_SALT = "isadoraair.remote-dj.attempt.v1"
ATTEMPT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,48}$")
MAX_REASON_LENGTH = 240
MAX_BROWSER_ELAPSED_MS = 60 * 60 * 1000

FAILURE_AUTHORIZATION_TOKEN = "authorization_token"
FAILURE_SIGNALING_SESSION_BUSY = "signaling_session_busy"
FAILURE_DEPENDENCY_SESSION_BUILD = "dependency_session_build"
FAILURE_ICE_STATE = "ice_state"
FAILURE_MEDIA_ROUTING = "media_routing"
FAILURE_CLASSES = frozenset({
    FAILURE_AUTHORIZATION_TOKEN,
    FAILURE_SIGNALING_SESSION_BUSY,
    FAILURE_DEPENDENCY_SESSION_BUILD,
    FAILURE_ICE_STATE,
    FAILURE_MEDIA_ROUTING,
})

MILESTONE_ORDER = (
    "browser_connect_start",
    "token_issued",
    "browser_token_received",
    "websocket_admitted",
    "browser_websocket_open",
    "glib_session_start",
    "session_build_started",
    "session_build_completed",
    "offer_created",
    "offer_queued",
    "answer_received",
    "answer_submitted",
    "first_server_ice_candidate",
    "first_browser_ice_candidate",
    "peer_connecting",
    "ice_checking",
    "ice_connected",
    "peer_connected",
    "inbound_source_pad",
    "first_decoded_remote_mic_buffer",
    "session_stopped",
)
MILESTONE_RANK = {name: rank for rank, name in enumerate(MILESTONE_ORDER)}
BROWSER_MILESTONES = frozenset({
    "browser_connect_start",
    "browser_token_received",
    "browser_websocket_open",
    "first_browser_ice_candidate",
    "ice_checking",
    "ice_connected",
})


def new_attempt_id():
    """Return a bounded 24-character, cryptographically random identity."""
    return secrets.token_urlsafe(18)


def mint_remote_dj_token(user_id, *, attempt_id=None, issued_at_ms=None):
    attempt_id = attempt_id or new_attempt_id()
    if not ATTEMPT_ID_PATTERN.fullmatch(attempt_id):
        raise ValueError("invalid Remote DJ attempt identity")
    payload = {
        "version": 1,
        "user_id": int(user_id),
        "attempt_id": attempt_id,
        "issued_at_ms": int(issued_at_ms if issued_at_ms is not None else time.time() * 1000),
    }
    token = TimestampSigner(salt=TOKEN_SALT).sign_object(payload, compress=True)
    return token, payload


def verify_remote_dj_token(token, *, max_age):
    payload = TimestampSigner(salt=TOKEN_SALT).unsign_object(token, max_age=max_age)
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise BadSignature("unsupported Remote DJ token payload")
    attempt_id = payload.get("attempt_id")
    if not isinstance(attempt_id, str) or not ATTEMPT_ID_PATTERN.fullmatch(attempt_id):
        raise BadSignature("invalid Remote DJ attempt identity")
    try:
        user_id = int(payload["user_id"])
        issued_at_ms = int(payload["issued_at_ms"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BadSignature("invalid Remote DJ token payload") from exc
    if user_id <= 0 or issued_at_ms <= 0:
        raise BadSignature("invalid Remote DJ token payload")
    return {
        "user_id": user_id,
        "attempt_id": attempt_id,
        "issued_at_ms": issued_at_ms,
    }


def normalize_browser_stun_server(configured_value):
    """Convert GStreamer's ``stun://host:port`` into browser syntax."""
    if not isinstance(configured_value, str):
        raise ValueError("STUN server must be a string")
    value = configured_value.strip()
    if not value or any(ord(ch) < 33 for ch in value):
        raise ValueError("STUN server is empty or contains whitespace")
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "stun" or not parsed.hostname:
        raise ValueError("STUN server must use stun://host[:port]")
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("STUN server must not contain credentials, paths, query, or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("STUN server port is invalid") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("STUN server port is invalid")
    host = parsed.hostname
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise ValueError("STUN server host is invalid")
    authority = f"{host}:{port}" if port is not None else host
    return f"stun:{authority}"


def browser_ice_servers(configured_value):
    return [{"urls": normalize_browser_stun_server(configured_value)}]


class RemoteDJConnectionAttempt:
    """One sparse, bounded attempt timeline owned by the engine GLib thread."""

    def __init__(self, attempt_id, issued_at_ms, *, monotonic=None, wall_time=None):
        if not ATTEMPT_ID_PATTERN.fullmatch(attempt_id or ""):
            raise ValueError("invalid Remote DJ attempt identity")
        self.attempt_id = attempt_id
        self.issued_at_ms = max(1, int(issued_at_ms))
        self._monotonic = monotonic or time.monotonic
        self._wall_time = wall_time or time.time
        self._admitted_monotonic = self._monotonic()
        self._admission_elapsed_ms = max(
            0.0, self._wall_time() * 1000.0 - self.issued_at_ms
        )
        self.stage = "token_issued"
        self.status = "connecting"
        # The browser and server clocks are deliberately independent.
        # Duplicate suppression therefore happens within a timing
        # domain, never globally by milestone name.
        self.milestones = {
            "server": {"token_issued": 0.0},
            "browser": {},
        }
        self.failure = None
        self._frozen_elapsed_ms = None

    def _server_elapsed_ms(self):
        return max(
            self._admission_elapsed_ms,
            self._admission_elapsed_ms
            + (self._monotonic() - self._admitted_monotonic) * 1000.0,
        )

    def record(self, milestone, *, browser_elapsed_ms=None):
        if milestone not in MILESTONE_RANK:
            return False
        source = "browser" if browser_elapsed_ms is not None else "server"
        domain = self.milestones[source]
        if milestone in domain:
            return False
        if browser_elapsed_ms is None:
            elapsed_ms = self._server_elapsed_ms()
        else:
            try:
                elapsed_ms = float(browser_elapsed_ms)
            except (TypeError, ValueError):
                return False
            if not 0 <= elapsed_ms <= MAX_BROWSER_ELAPSED_MS:
                return False
        domain[milestone] = round(elapsed_ms, 1)
        current_rank = MILESTONE_RANK.get(self.stage, -1)
        new_rank = MILESTONE_RANK.get(milestone, current_rank)
        if self.status != "failed" and new_rank >= current_rank:
            self.stage = milestone
        if milestone in {"ice_connected", "peer_connected"}:
            self.status = "connected"
        return True

    def fail(self, failure_class, reason):
        if failure_class not in FAILURE_CLASSES:
            failure_class = FAILURE_MEDIA_ROUTING
        clean_reason = " ".join(str(reason).split())[:MAX_REASON_LENGTH]
        self.failure = {"class": failure_class, "reason": clean_reason}
        self.stage = "failed"
        self.status = "failed"
        self._frozen_elapsed_ms = round(self._server_elapsed_ms(), 1)

    def end(self):
        self.record("session_stopped")
        if self.status != "failed":
            self.status = "ended"
            self.stage = "session_stopped"
        self._frozen_elapsed_ms = round(self._server_elapsed_ms(), 1)

    def snapshot(self):
        elapsed = (
            self._frozen_elapsed_ms
            if self._frozen_elapsed_ms is not None
            else round(self._server_elapsed_ms(), 1)
        )
        started_at = datetime.fromtimestamp(
            self.issued_at_ms / 1000.0, tz=timezone.utc
        ).isoformat()
        milestone_summary = {
            source: dict(values)
            for source, values in self.milestones.items()
        }
        return {
            "attempt_id": self.attempt_id,
            "status": self.status,
            "stage": self.stage,
            "started_at": started_at,
            "elapsed_ms": elapsed,
            # Never compare these two domains directly: server values
            # are monotonic elapsed time from token issuance/admission;
            # browser values are performance.now()-relative durations
            # from the local Connect click.
            "milestones_ms": milestone_summary,
            "failure": dict(self.failure) if self.failure is not None else None,
        }
