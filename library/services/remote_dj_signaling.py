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
"""
import asyncio
import json
import threading
from urllib.parse import parse_qs, urlparse

from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from gi.repository import GLib
import websockets

TOKEN_MAX_AGE_SECONDS = 60
# One session already active; a second connection attempt is rejected
# outright with this close code (see the Context section's "one remote DJ
# at a time" decision).
CLOSE_CODE_SESSION_BUSY = 4002
CLOSE_CODE_INVALID_TOKEN = 4001


class RemoteDJSignalingServer:
    def __init__(self, engine, host="127.0.0.1", port=8001):
        self.engine = engine
        self.host = host
        self.port = port
        self._loop = None
        self._ws = None

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
        signer = TimestampSigner()
        try:
            user_id = signer.unsign(token, max_age=TOKEN_MAX_AGE_SECONDS)
        except (BadSignature, SignatureExpired, TypeError):
            await ws.close(code=CLOSE_CODE_INVALID_TOKEN, reason="invalid token")
            return

        if self._ws is not None:
            await ws.close(code=CLOSE_CODE_SESSION_BUSY, reason="a session is already active")
            return

        self._ws = ws
        print(f"  Remote DJ connected: user_id={user_id}")
        GLib.idle_add(self.engine._remote_dj_session_start)

        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                msg_type = data.get("type")
                if msg_type == "answer":
                    GLib.idle_add(self.engine._remote_dj_handle_answer, data.get("sdp"))
                elif msg_type == "ice":
                    GLib.idle_add(
                        self.engine._remote_dj_handle_ice,
                        data.get("sdpMLineIndex"), data.get("candidate"),
                    )
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._ws = None
            print("  Remote DJ disconnected")
            GLib.idle_add(self.engine._remote_dj_session_stop)

    def send_json_threadsafe(self, obj):
        """Called from the GLib/GStreamer thread to deliver a message
        (offer/ICE candidate) to the currently-connected browser, if any."""
        if self._loop is None or self._ws is None:
            return
        asyncio.run_coroutine_threadsafe(self._send(obj), self._loop)

    def disconnect_threadsafe(self):
        """Operator-triggered force-disconnect (or a reaction to the
        webrtcbin connection itself failing) -- closes the actual
        websocket if it's still open. A no-op if the browser already
        disconnected on its own (the ordinary case, which drives session
        teardown the other way: _handler's own `finally` block, not this
        method)."""
        if self._loop is None or self._ws is None:
            return
        asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)

    async def _send(self, obj):
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps(obj))
        except Exception as exc:
            print(f"  Remote DJ send failed: {exc}")
