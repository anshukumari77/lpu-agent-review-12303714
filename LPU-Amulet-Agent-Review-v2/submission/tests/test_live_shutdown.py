"""Unpaid deadline regressions using the installed SDK and synthetic IO."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from livekit.agents import APIConnectOptions
from livekit.plugins.openai import realtime


def load_harness():
    path = Path(__file__).resolve().parents[1] / "scripts" / "verify_live_session.py"
    spec = importlib.util.spec_from_file_location("live_shutdown_harness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_real_sdk_external_transport_cannot_report_an_early_socket_close(monkeypatch):
    import socket

    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("network forbidden"))
    harness = load_harness()

    class SyntheticSocket:
        closed = False

        def __init__(self):
            self.end = asyncio.Event()

        async def send_str(self, value):
            pass

        async def receive(self):
            await self.end.wait()
            return SimpleNamespace(type=aiohttp.WSMsgType.CLOSE)

        async def close(self):
            if not self.closed:
                await asyncio.sleep(1.1)
                self.closed = True
                self.end.set()

    ws = SyntheticSocket()

    class SyntheticHTTP:
        async def ws_connect(self, **kwargs):
            return ws

    model = realtime.RealtimeModel(
        api_key="synthetic-no-provider", base_url="http://127.0.0.1/never-requested",
        http_session=SyntheticHTTP(), max_session_duration=None,
        conn_options=APIConnectOptions(max_retry=0, timeout=1),
    )

    class Runtime:
        failures = 0
        model_task = None
        assembly = SimpleNamespace(model=model)
        native = None

        async def run(self):
            self.native = model.session()
            await asyncio.Future()

        async def aclose(self):
            if self.native:
                await self.native.aclose()

    runtime = Runtime()
    try:
        # The old harness reports ~0.5s, despite actual transport closure after 1.6s.
        with pytest.raises((harness.Rejected, TimeoutError)):
            await harness.bounded_voice(runtime, 1, cut=lambda: None)
    finally:
        await runtime.aclose()
        await model.aclose()


async def test_owned_http_closes_real_loopback_peer_before_slow_runtime_cleanup():
    from aiohttp import web
    from contextlib import suppress

    harness = load_harness()
    peer_closed = asyncio.Event()
    times = {}

    async def websocket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        try:
            async for message in ws:
                pass  # Real loopback transport; no provider responses are fabricated.
        finally:
            times["peer_closed"] = asyncio.get_running_loop().time()
            peer_closed.set()
        return ws

    app = web.Application()
    app.router.add_get("/realtime", websocket)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0]
    try:
        async with aiohttp.ClientSession(trust_env=False) as http:
            model = realtime.RealtimeModel(
                api_key="synthetic-loopback-only", base_url=f"http://{host}:{port}",
                http_session=http, max_session_duration=None,
                conn_options=APIConnectOptions(max_retry=0, timeout=1),
            )

            class Runtime:
                failures = 0
                model_task = None
                assembly = SimpleNamespace(model=model)
                native = None

                async def run(self):
                    self.native = model.session()
                    await asyncio.Future()

                async def aclose(self):
                    # Simulate slow observer/runtime join preceding native close.
                    await asyncio.sleep(1.1)
                    if self.native:
                        with suppress(Exception, asyncio.CancelledError):
                            await self.native.aclose()

            runtime = Runtime()
            started = asyncio.get_running_loop().time()
            with pytest.raises(harness.Rejected):
                await harness.bounded_voice(
                    runtime, 1, cut=lambda: None,
                    close_transport=http.close, transport_closed=lambda: http.closed,
                )
            await asyncio.wait_for(peer_closed.wait(), 1)
            assert http.closed
            assert 0 < times["peer_closed"] - started < 1
            await model.aclose()
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("question,question_seq,client_text,expected", [
    ("Thank you. What happens next?", 4, "The owner approves the quotation.", False),
    ("Who approves this $14,500 quotation?", 2, "The owner approves the quotation.", False),
    ("Who approves this $14,500 quotation?", 4, "The owner approves the quotation.", True),
    ("How is fourteen thousand five hundred approved?", 4, "The owner approves the quotation.", True),
    ("The total is 14,500.", 4, "The owner approves the quotation.", False),
    ("I understand how the owner approves the 14,500 quotation.", 4,
     "The owner approves the quotation.", False),
    ("The quotation is 14,500. What happens next?", 4,
     "The owner approves the quotation.", False),
    ("I understand how the owner approves the 14,500 quotation?", 4,
     "The owner approves the quotation.", False),
    ("Who approves this $14,500 quotation?", 4,
     "The owner approves fourteen thousand and five hundred dollars.", False),
    ("Who approves this $14,500 quotation?", 4,
     "The owner approves fourteen-thousand-and-five-hundred dollars.", False),
    ("How is fourteen thousand and five hundred approved?", 4,
     "The owner approves the quotation.", True),
    ("Who approves this $14,500 quotation?", 4, "The owner approves the quotation for 14,500.", False),
    ("Who approves this $16,000 quotation?", 4, "The owner approves the quotation.", False),
])
def test_screen_question_requires_later_question_with_screen_only_fact(
    tmp_path, question, question_seq, client_text, expected,
):
    from beep_agent.config import Settings

    harness = load_harness()
    backend = harness.RealSession(Settings(openai_api_key=""), SimpleNamespace(seconds=60), directory=tmp_path)
    backend.screen_publication = SimpleNamespace(sid="synthetic-screen")
    backend.observed_source = "livekit:synthetic-screen@900300:sha256:synthetic"
    backend.measurements["synthetic_speech_samples_sent"] = 48000
    backend.events = [
        {"kind": "transcript", "actor": "client", "seq": 1, "text": client_text,
         "source_ref": "livekit-agents@unit:conversation_item_added:client"},
        {"kind": "screen_observation", "actor": "observer", "seq": 3,
         "text": "Synthetic selected screen shows AUD 14,500 approval pending.",
         "source_ref": backend.observed_source},
        {"kind": "transcript", "actor": "agent", "seq": question_seq, "text": question,
         "source_ref": "livekit-agents@unit:conversation_item_added:agent"},
    ]
    backend.capture_measurements()
    assert backend.measurements.get("screen_aware_question_after_observation") is expected


async def test_owned_transport_still_closes_when_synchronous_mute_fails():
    harness = load_harness()

    class Runtime:
        failures = 0
        model_task = None

        def __init__(self):
            self.stop = asyncio.Event()
            self.task = None

        async def run(self):
            self.task = asyncio.current_task()
            await self.stop.wait()

        async def aclose(self):
            self.stop.set()

    def cut():
        raise RuntimeError("synthetic mute failure")

    runtime = Runtime()
    async with aiohttp.ClientSession(trust_env=False) as http:
        try:
            with pytest.raises(RuntimeError, match="synthetic mute failure"):
                await harness.bounded_voice(
                    runtime, 0.03, cut=cut,
                    close_transport=http.close, transport_closed=lambda: http.closed,
                )
            assert http.closed
        finally:
            await runtime.aclose()
            if runtime.task:
                runtime.task.cancel()
                await asyncio.gather(runtime.task, return_exceptions=True)
