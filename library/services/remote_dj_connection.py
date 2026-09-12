"""Bounded Remote DJ connection-attempt identity and telemetry helpers."""

import re
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from django.core.signing import BadSignature, TimestampSigner

from library.services.remote_dj_stats import (
    ICE_GATHERING_STATES,
    ICE_STATES,
    empty_media_stats_snapshot,
    empty_transport_snapshot,
)


TOKEN_SALT = "isadoraair.remote-dj.attempt.v1"
ATTEMPT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,48}$")
MAX_REASON_LENGTH = 240
MAX_BROWSER_ELAPSED_MS = 60 * 60 * 1000
MAX_TRANSPORT_TRANSITIONS = 12

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
    "server_ice_checking",
    "peer_connecting",
    "ice_checking",
    "server_ice_connected",
    "ice_connected",
    "server_ice_completed",
    "selected_candidate_pair",
    # P1 1.5 Pass B1 -- the SERVER-side get-stats snapshot can expose a
    # transport's selected-candidate-pair-id before the remote answer/
    # browser candidates even exist (proven in r0070 production: the
    # snapshot only means "candidate-pair stats were present in this
    # promise reply," never "a usable pair was actually nominated").
    # Renamed rather than reusing "selected_candidate_pair" (which stays
    # exactly as before for the BROWSER's own, separately-verified,
    # selected/nominated-pair report) so a truthful name can never be
    # confused with the misleading server-side one it replaces. See
    # _remote_dj_on_stats_ready.
    "candidate_pair_stats_present",
    "dtls_connecting",
    "dtls_connected",
    "peer_connected",
    "first_monitor_rtp",
    "first_monitor_audio_energy",
    "inbound_source_pad",
    "first_remote_mic_rtp_observed",
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
    "selected_candidate_pair",
    "first_monitor_rtp",
    "first_monitor_audio_energy",
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
        # Fixed-key, JSON-safe observational state. Updates overwrite the
        # current snapshot; no unbounded history or database telemetry exists.
        self.transport = empty_transport_snapshot()
        self.media_stats = empty_media_stats_snapshot()
        self._frozen_elapsed_ms = None

    def record_server_ice_state(self, state):
        if state not in ICE_STATES:
            return False
        previous = self.transport["server"]["ice_state"]
        self.transport["server"]["ice_state"] = state
        if state != previous:
            transitions = self.transport["server"]["ice_transitions"]
            if len(transitions) < MAX_TRANSPORT_TRANSITIONS:
                transitions.append({
                    "state": state,
                    "elapsed_ms": round(self._server_elapsed_ms(), 1),
                })
        return True

    def record_server_ice_gathering_state(self, state):
        if state not in ICE_GATHERING_STATES:
            return False
        previous = self.transport["server"]["ice_gathering_state"]
        self.transport["server"]["ice_gathering_state"] = state
        if state != previous:
            transitions = self.transport["server"]["ice_gathering_transitions"]
            if len(transitions) < MAX_TRANSPORT_TRANSITIONS:
                transitions.append({
                    "state": state,
                    "elapsed_ms": round(self._server_elapsed_ms(), 1),
                })
        return True

    def record_server_dtls_state(self, state):
        if state is None:
            return False
        previous = self.transport["server"]["dtls_state"]
        self.transport["server"]["dtls_state"] = state
        if state != previous:
            observations = self.transport["server"]["dtls_state_observations"]
            if len(observations) < MAX_TRANSPORT_TRANSITIONS:
                observations.append({
                    "state": state,
                    "elapsed_ms": round(self._server_elapsed_ms(), 1),
                })
        return True

    def apply_server_stats(self, parsed):
        if not isinstance(parsed, dict):
            return False
        transport = parsed.get("transport", {})
        if transport.get("dtls_state") is not None:
            self.record_server_dtls_state(transport["dtls_state"])
        if transport.get("dtls_role") is not None:
            self.transport["server"]["dtls_role"] = transport["dtls_role"]
        if isinstance(transport.get("selected_pair"), dict):
            self.transport["server"]["selected_pair"] = dict(
                transport["selected_pair"]
            )
        for section in ("remote_mic", "monitor_return"):
            values = parsed.get(section)
            if isinstance(values, dict):
                for key in self.media_stats[section]:
                    if values.get(key) is not None:
                        self.media_stats[section][key] = values[key]
        return True

    def apply_browser_stats(self, sanitized):
        if not isinstance(sanitized, dict):
            return False
        if sanitized.get("ice_state") is not None:
            self.transport["browser"]["ice_state"] = sanitized["ice_state"]
        if sanitized.get("rtt_ms") is not None:
            self.transport["browser"]["rtt_ms"] = sanitized["rtt_ms"]
        pair = sanitized.get("selected_pair")
        if isinstance(pair, dict):
            self.transport["browser"]["selected_pair"] = dict(pair)
        inbound = sanitized.get("inbound")
        if isinstance(inbound, dict):
            for key in self.media_stats["browser_monitor"]:
                if inbound.get(key) is not None:
                    self.media_stats["browser_monitor"][key] = inbound[key]
        return True

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

    def mark_reconnecting(self):
        """P1 1.5 Pass B1 -- product-level lifecycle transition: an
        established session's transport became recoverably disconnected
        (WebRTC PeerConnectionState DISCONNECTED, or equivalent). Returns
        True only if this actually changed status -- i.e. the attempt was
        genuinely "connected" immediately before. A DISCONNECTED signal
        arriving before the session ever reached "connected" (still
        mid-negotiation) is not a recoverable-established-session event
        and must not start a recovery-grace window; the caller uses this
        return value to decide whether to do so."""
        if self.status != "connected":
            return False
        self.status = "reconnecting"
        return True

    def mark_recovered(self):
        """The mirror of mark_reconnecting(): the SAME session's transport
        recovered before its recovery grace expired. Returns True only if
        a transition actually happened (i.e. the attempt was genuinely
        "reconnecting")."""
        if self.status != "reconnecting":
            return False
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
        transport_summary = {
            domain: {
                **values,
                "selected_pair": dict(values["selected_pair"]),
            }
            for domain, values in self.transport.items()
        }
        for key in (
            "ice_transitions",
            "ice_gathering_transitions",
            "dtls_state_observations",
        ):
            transport_summary["server"][key] = [
                dict(item) for item in self.transport["server"][key]
            ]
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
            "transport": transport_summary,
            "media_stats": {
                section: dict(values)
                for section, values in self.media_stats.items()
            },
        }
