"""r0075 attempt-aware Remote DJ signaling ownership regressions."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from library.services import remote_dj_signaling as signaling
from library.services.remote_dj_connection import mint_remote_dj_token


ATTEMPT_A = "r0075_attempt_A_123456789"
ATTEMPT_B = "r0075_attempt_B_987654321"


class _LifecycleWebSocket:
    """Fake whose close handshake and handler exit are independently visible."""

    def __init__(self, token):
        self.request = SimpleNamespace(path=f"/ws/remote-dj/?token={token}")
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.iteration_done = asyncio.Event()
        self.close_calls = 0
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self.iteration_done.wait()
        raise StopAsyncIteration

    async def close(self, *, code=None, reason=None):
        self.close_calls += 1
        self.close_started.set()
        await self.allow_close.wait()
        self.iteration_done.set()

    async def send(self, payload):
        self.sent.append(payload)

    def finish_naturally(self):
        self.iteration_done.set()


class RemoteDJSignalingLifecycleTests(SimpleTestCase):
    def _server(self):
        engine = SimpleNamespace(
            _remote_dj_session_start=MagicMock(),
            _remote_dj_session_stop=MagicMock(),
            _remote_dj_handle_answer=MagicMock(),
            _remote_dj_handle_ice=MagicMock(),
            _remote_dj_record_browser_milestone=MagicMock(),
            _remote_dj_record_browser_stats=MagicMock(),
            _remote_dj_record_signaling_failure=MagicMock(),
        )
        return signaling.RemoteDJSignalingServer(engine), engine

    @staticmethod
    async def _wait_until(predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(0)
        raise AssertionError("async signaling condition was not reached")

    def test_retired_a_close_may_stall_while_b_is_admitted_and_survives_a_finally(self):
        """Production race: logical release does not await physical close."""

        async def scenario():
            token_a, _ = mint_remote_dj_token(42, attempt_id=ATTEMPT_A)
            token_b, _ = mint_remote_dj_token(42, attempt_id=ATTEMPT_B)
            ws_a = _LifecycleWebSocket(token_a)
            ws_b = _LifecycleWebSocket(token_b)
            server, engine = self._server()
            server._loop = asyncio.get_running_loop()

            with patch.object(signaling.GLib, "idle_add") as idle_add:
                handler_a = asyncio.create_task(server._handler(ws_a))
                await self._wait_until(
                    lambda: server._ws_attempt_id == ATTEMPT_A
                )

                server.retire_attempt_threadsafe(ATTEMPT_A)
                await asyncio.wait_for(ws_a.close_started.wait(), timeout=1)
                self.assertIsNone(server._ws_attempt_id)
                self.assertIsNone(server._ws)

                # A.close() is deliberately still blocked here.  B must
                # nevertheless acquire logical admission immediately.
                handler_b = asyncio.create_task(server._handler(ws_b))
                await self._wait_until(
                    lambda: server._ws_attempt_id == ATTEMPT_B
                )
                self.assertIs(server._ws, ws_b)
                self.assertFalse(handler_a.done())

                start_attempts = [
                    call.args[1]
                    for call in idle_add.call_args_list
                    if call.args and call.args[0] is engine._remote_dj_session_start
                ]
                self.assertEqual(start_attempts, [ATTEMPT_A, ATTEMPT_B])

                # Complete A's physical close and therefore A's handler
                # finally.  Neither may clear B or request engine teardown.
                ws_a.allow_close.set()
                await asyncio.wait_for(handler_a, timeout=1)
                await self._wait_until(lambda: ws_a.close_calls == 1)
                self.assertEqual(server._ws_attempt_id, ATTEMPT_B)
                self.assertIs(server._ws, ws_b)
                a_stop_calls = [
                    call
                    for call in idle_add.call_args_list
                    if call.args == (engine._remote_dj_session_stop, ATTEMPT_A)
                ]
                self.assertEqual(a_stop_calls, [])

                ws_b.finish_naturally()
                await asyncio.wait_for(handler_b, timeout=1)

        asyncio.run(scenario())

    def test_stale_retire_cannot_release_or_close_b(self):
        async def scenario():
            token_b, _ = mint_remote_dj_token(42, attempt_id=ATTEMPT_B)
            ws_b = _LifecycleWebSocket(token_b)
            server, _engine = self._server()
            server._ws = ws_b
            server._ws_attempt_id = ATTEMPT_B

            retired = await server._retire_attempt(ATTEMPT_A)

            self.assertFalse(retired)
            self.assertEqual(server._ws_attempt_id, ATTEMPT_B)
            self.assertIs(server._ws, ws_b)
            self.assertEqual(ws_b.close_calls, 0)

        asyncio.run(scenario())

    def test_repeated_retire_of_a_is_idempotent_after_b_takes_ownership(self):
        async def scenario():
            token_a, _ = mint_remote_dj_token(42, attempt_id=ATTEMPT_A)
            token_b, _ = mint_remote_dj_token(42, attempt_id=ATTEMPT_B)
            ws_a = _LifecycleWebSocket(token_a)
            ws_b = _LifecycleWebSocket(token_b)
            ws_a.allow_close.set()
            server, _engine = self._server()
            server._ws = ws_a
            server._ws_attempt_id = ATTEMPT_A

            self.assertTrue(await server._retire_attempt(ATTEMPT_A))
            server._ws = ws_b
            server._ws_attempt_id = ATTEMPT_B
            self.assertFalse(await server._retire_attempt(ATTEMPT_A))

            self.assertEqual(ws_a.close_calls, 1)
            self.assertEqual(ws_b.close_calls, 0)
            self.assertEqual(server._ws_attempt_id, ATTEMPT_B)
            self.assertIs(server._ws, ws_b)

        asyncio.run(scenario())

    def test_natural_close_releases_owner_and_stops_matching_engine_attempt(self):
        async def scenario():
            token_a, _ = mint_remote_dj_token(42, attempt_id=ATTEMPT_A)
            ws_a = _LifecycleWebSocket(token_a)
            server, engine = self._server()

            with patch.object(signaling.GLib, "idle_add") as idle_add:
                handler = asyncio.create_task(server._handler(ws_a))
                await self._wait_until(
                    lambda: server._ws_attempt_id == ATTEMPT_A
                )
                ws_a.finish_naturally()
                await asyncio.wait_for(handler, timeout=1)

            self.assertIsNone(server._ws_attempt_id)
            self.assertIsNone(server._ws)
            self.assertEqual(
                idle_add.call_args_list[-1].args,
                (engine._remote_dj_session_stop, ATTEMPT_A),
            )

        asyncio.run(scenario())

    def test_attempt_correlated_send_never_crosses_to_new_owner(self):
        async def scenario():
            token_b, _ = mint_remote_dj_token(42, attempt_id=ATTEMPT_B)
            ws_b = _LifecycleWebSocket(token_b)
            server, _engine = self._server()
            server._ws = ws_b
            server._ws_attempt_id = ATTEMPT_B
            payload = {"type": "offer", "sdp": "attempt-specific"}

            await server._send(ATTEMPT_A, payload)
            self.assertEqual(ws_b.sent, [])

            await server._send(ATTEMPT_B, payload)
            self.assertEqual(ws_b.sent, [json.dumps(payload)])

        asyncio.run(scenario())
