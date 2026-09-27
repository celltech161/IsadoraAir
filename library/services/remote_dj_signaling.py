"""
Remote DJ over WebRTC -- signaling server.

Runs a `websockets.serve()` server on its own dedicated daemon thread with
its own asyncio event loop, started from PlaybackEngine.start() (only when
RemoteDJConfig.enabled). Lives in the same process/systemd unit as the
rest of the engine -- no new service. Listens on 127.0.0.1:8001, matching
the nginx `/ws/remote-dj/` location that proxies real browser connections
to it; validates the token minted by `api_remote_dj_token` with the same
`TimestampSigner`/SECRET_KEY Django uses.

Only ever one active session at a time (a confirmed design decision, not
a limitation to lift later) -- a second connection attempt while one is
already active is rejected outright.

Marshaling in both directions mirrors the pattern already established by
_create_deck's EOS probe (GLib.idle_add(self._on_deck_eos_probed, ...)),
validated end-to-end in Stage 2/3 of the offline harness work:
  - websocket thread -> GLib thread: GLib.idle_add(...) for session
    start/stop and incoming SDP answers/ICE candidates.
  - GLib/GStreamer thread -> websocket thread: send_json_threadsafe()
    below, via asyncio.run_coroutine_threadsafe against the loop captured
    once the server coroutine starts running.

Roadmap 2.5C -- this server is a SEPARATE trust boundary from Django's
request pipeline: it runs its own raw websockets.serve(), inside this
same process (see RemoteDJSignalingServer.start()), never through
GroupBasedAccessMiddleware or any Django view. A valid token signature
proves the token was genuinely minted by api_remote_dj_token at some
point in the last TOKEN_MAX_AGE_SECONDS -- it does NOT prove the
authorization that was true at mint time is STILL true now (the Role
could have been edited, the account disabled, the TalentAssignment
disabled/deleted, or scheduled enforcement flipped on, all in the
interim). _handler() therefore re-runs the full authz.evaluator.authorize()
check for remote_dj.connect against a freshly-fetched User row before
admitting -- see the call below. bypass_cache=True is used because this
process (`manage.py run_engine`) never receives the Django signals a Role
edit made through the web/gunicorn process fires -- see authorize()'s own
docstring for why that specific staleness only matters here.
"""
import asyncio
import json
import threading
from urllib.parse import parse_qs, urlparse

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.core.signing import BadSignature, SignatureExpired
from gi.repository import GLib
import websockets

from authz.evaluator import authorize
from library.services.remote_dj_connection import (
    BROWSER_MILESTONES,
    FAILURE_AUTHORIZATION_TOKEN,
    FAILURE_SIGNALING_SESSION_BUSY,
    verify_remote_dj_token,
)
from library.services.remote_dj_stats import sanitize_browser_stats_payload

TOKEN_MAX_AGE_SECONDS = 60
# One session already active; a second connection attempt is rejected
# outright with this close code (see the Context section's "one remote DJ
# at a time" decision).
CLOSE_CODE_SESSION_BUSY = 4002
CLOSE_CODE_INVALID_TOKEN = 4001
CLOSE_CODE_NOT_AUTHORIZED = 4003


class RemoteDJSignalingServer:
    def __init__(self, engine, host="127.0.0.1", port=8001):
        self.engine = engine
        self.host = host
        self.port = port
        self._loop = None
        self._ws = None
        # Logical admission ownership is attempt-correlated and distinct
        # from the lifetime of the physical WebSocket.  A retired socket
        # may still be completing its close handshake after these fields
        # have moved on to a newly admitted attempt.
        self._ws_attempt_id = None

    def start(self):
        threading.Thread(target=self._run_thread, daemon=True).start()

    def _run_thread(self):
        try:
            asyncio.run(self._main())
        except Exception as exc:
            print(f"  Remote DJ signaling server crashed: {exc}")

    async def _main(self):
        self._loop = asyncio.get_running_loop()
        async with websockets.serve(self._handler, self.host, self.port):
            print(f"  Remote DJ signaling server listening on {self.host}:{self.port}")
            await asyncio.Future()  # run forever

    async def _handler(self, ws):
        query = parse_qs(urlparse(ws.request.path).query)
        token = query.get("token", [None])[0]
        try:
            identity = verify_remote_dj_token(
                token, max_age=TOKEN_MAX_AGE_SECONDS
            )
        except (BadSignature, SignatureExpired, TypeError):
            print("  Remote DJ connection refused: failure=authorization_token")
            await ws.close(code=CLOSE_CODE_INVALID_TOKEN, reason="invalid token")
            return

        attempt_id = identity["attempt_id"]

        # Roadmap 2.5C -- a valid signature only proves the token was
        # minted at some point in the last TOKEN_MAX_AGE_SECONDS; it does
        # NOT prove the authorization that was true at mint time still
        # holds now. Re-run the real check against a freshly-fetched
        # User row (never trust the signed payload's claims about
        # capability/schedule -- only its user_id) before admitting.
        # Django refuses synchronous ORM access from a running asyncio
        # event loop regardless of which OS thread is running it (this
        # handler is a plain `async def` coroutine even though it lives
        # on its own dedicated thread) -- sync_to_async hands the actual
        # DB work to a worker thread the same way Django's own docs
        # prescribe for exactly this situation.
        auth_result = await sync_to_async(self._check_remote_dj_authorization)(
            identity["user_id"], attempt_id
        )
        if not auth_result.allowed:
            print(
                f"  Remote DJ connection refused: attempt={attempt_id} "
                f"failure={FAILURE_AUTHORIZATION_TOKEN} reason={auth_result.code}"
            )
            GLib.idle_add(
                self.engine._remote_dj_record_signaling_failure,
                attempt_id,
                identity["issued_at_ms"],
                FAILURE_AUTHORIZATION_TOKEN,
                auth_result.reason,
            )
            await ws.close(code=CLOSE_CODE_NOT_AUTHORIZED, reason="not authorized")
            return

        if self._ws_attempt_id is not None:
            reason = "a session is already active"
            print(
                f"  Remote DJ connection refused: attempt={attempt_id} "
                f"failure={FAILURE_SIGNALING_SESSION_BUSY}"
            )
            GLib.idle_add(
                self.engine._remote_dj_record_signaling_failure,
                attempt_id,
                identity["issued_at_ms"],
                FAILURE_SIGNALING_SESSION_BUSY,
                reason,
            )
            await ws.close(code=CLOSE_CODE_SESSION_BUSY, reason="a session is already active")
            return

        self._ws = ws
        self._ws_attempt_id = attempt_id
        print(
            f"  Remote DJ WebSocket admitted: attempt={attempt_id} "
            f"user_id={identity['user_id']}"
        )
        GLib.idle_add(
            self.engine._remote_dj_session_start,
            attempt_id,
            identity["issued_at_ms"],
            identity["user_id"],
        )

        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                msg_type = data.get("type")
                if msg_type == "answer":
                    GLib.idle_add(
                        self.engine._remote_dj_handle_answer,
                        attempt_id,
                        data.get("sdp"),
                    )
                elif msg_type == "ice":
                    GLib.idle_add(
                        self.engine._remote_dj_handle_ice,
                        attempt_id,
                        data.get("sdpMLineIndex"), data.get("candidate"),
                    )
                elif msg_type == "milestone":
                    milestone = data.get("milestone")
                    if milestone in BROWSER_MILESTONES:
                        GLib.idle_add(
                            self.engine._remote_dj_record_browser_milestone,
                            attempt_id,
                            milestone,
                            data.get("elapsed_ms"),
                        )
                elif msg_type == "stats":
                    sanitized = sanitize_browser_stats_payload(data.get("stats"))
                    if sanitized is not None:
                        GLib.idle_add(
                            self.engine._remote_dj_record_browser_stats,
                            attempt_id,
                            sanitized,
                            data.get("elapsed_ms"),
                        )
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            # Only the handler which still owns logical admission may
            # release it and ask the engine to stop.  An engine-retired A
            # can finish physically closing after B is admitted; A's
            # delayed finally must be completely inert toward B.
            if self._ws_attempt_id == attempt_id and self._ws is ws:
                self._ws = None
                self._ws_attempt_id = None
                print(f"  Remote DJ disconnected: attempt={attempt_id}")
                GLib.idle_add(
                    self.engine._remote_dj_session_stop, attempt_id
                )

    @staticmethod
    def _check_remote_dj_authorization(user_id, attempt_id):
        """Synchronous DB work for the admission check above, run via
        sync_to_async (Django refuses direct synchronous ORM access from
        a running asyncio event loop, regardless of OS thread -- see the
        _handler call site's own comment). Fetches a fresh User row
        every call (no per-connection caching) and asks the real
        evaluator -- see this module's own docstring for why
        bypass_cache=True matters here. Also emits the denial audit
        event, in this SAME sync_to_async-wrapped call, when denied --
        emit_event is itself a DB write, so it must run on this same
        safe thread rather than back in the coroutine.

        Closes its own DB connection before returning: sync_to_async
        runs this on a throwaway thread-pool worker thread that Django's
        normal per-request connection cleanup never sees (there's no
        request here), so an idle connection would otherwise accumulate
        on that thread for as long as the process runs."""
        from django.db import connections

        User = get_user_model()
        try:
            try:
                user = User.objects.get(pk=user_id)
            except User.DoesNotExist:
                user = None
            result = authorize(user, "remote_dj.connect", bypass_cache=True)
            if not result.allowed:
                try:
                    from monitoring.models import emit_event
                    emit_event(
                        category="authz",
                        level="warning",
                        title="Remote DJ signaling admission denied",
                        detail={"user_id": user_id, "attempt_id": attempt_id, "code": result.code},
                        dedupe_key=f"authz|signaling_denied|{user_id}|{result.code}",
                    )
                except Exception as exc:
                    print(f"  Remote DJ: failed to emit admission-denied audit event: {exc}")
            return result
        finally:
            connections.close_all()

    def send_json_threadsafe(self, attempt_id, obj):
        """Called from the GLib/GStreamer thread to deliver a message
        (offer/ICE candidate) only to the browser which owns that signed
        attempt.  The owner check runs on the signaling loop, not here,
        so a queued A send can never be redirected to a later owner B."""
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._send(attempt_id, obj), self._loop
        )

    def retire_attempt_threadsafe(self, attempt_id):
        """Relinquish an engine-finalized attempt's logical signaling
        ownership, then close its detached physical socket asynchronously.

        The attempt comparison and both ownership mutations run on the
        signaling asyncio loop.  A stale/repeated retirement can therefore
        neither release nor close a newer attempt's socket.
        """
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._retire_attempt(attempt_id), self._loop
        )

    async def _retire_attempt(self, attempt_id):
        active_attempt = self._ws_attempt_id
        if active_attempt != attempt_id:
            if active_attempt is not None:
                print(
                    "  Remote DJ stale_signaling_retire_ignored "
                    f"attempt={attempt_id} active_attempt={active_attempt}"
                )
            return False

        detached_ws = self._ws
        self._ws = None
        self._ws_attempt_id = None
        print(f"  Remote DJ signaling_owner_retired attempt={attempt_id}")
        if detached_ws is None:
            return True

        print(f"  Remote DJ signaling_socket_close_started attempt={attempt_id}")
        try:
            await detached_ws.close()
        except Exception as exc:
            print(
                "  Remote DJ signaling_socket_close_failed "
                f"attempt={attempt_id} error={type(exc).__name__}"
            )
            return False
        print(f"  Remote DJ signaling_socket_close_completed attempt={attempt_id}")
        return True

    async def _send(self, attempt_id, obj):
        if self._ws_attempt_id != attempt_id or self._ws is None:
            return
        # Capture the matching socket before send() yields.  Even if this
        # attempt is retired while the send awaits, it remains bound to
        # A's detached socket and can never be redirected onto owner B.
        ws = self._ws
        try:
            await ws.send(json.dumps(obj))
        except Exception as exc:
            print(f"  Remote DJ send failed: {exc}")
