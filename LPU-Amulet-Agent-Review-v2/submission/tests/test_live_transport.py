"""Actual SDK / harness HTTP owner against a loopback-only protocol peer."""
import asyncio
import importlib.util
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest
from livekit.agents import APIConnectOptions
from livekit.plugins.openai import realtime

from beep_agent.config import Settings


def load_harness():
    path = Path(__file__).resolve().parents[1] / "scripts" / "verify_live_session.py"
    spec = importlib.util.spec_from_file_location("live_transport_harness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("response", ["ambiguous_close", "redirect"])
async def test_actual_voice_http_owner_sends_only_one_upgrade_request(tmp_path, response):
    harness = load_harness()
    observed = []
    handlers = set()

    async def peer(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            assert header.startswith(b"GET ")
            assert b"upgrade: websocket" in header.lower()
            observed.append(header.split(b"\r\n", 1)[0])
            if response == "redirect" and len(observed) == 1:
                writer.write(b"HTTP/1.1 307 Temporary Redirect\r\n"
                             b"Location: /redirected\r\nContent-Length: 0\r\n"
                             b"Connection: close\r\n\r\n")
                await writer.drain()
            # Deliberately no upgrade acknowledgement or fabricated model output.
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    backend = harness.RealSession(Settings(openai_api_key=""),
                                  SimpleNamespace(seconds=1), directory=tmp_path)
    owners = []
    outcomes = []

    def factory(*, http_session):
        owners.append(http_session)
        return realtime.RealtimeModel(
            api_key="synthetic-loopback-only", base_url=f"http://127.0.0.1:{port}",
            http_session=http_session, max_session_duration=None,
            conn_options=APIConnectOptions(max_retry=0, timeout=2),
        )

    backend.gate.factory = factory

    async def drive(**options):
        model = backend.gate()
        native = model.session()
        try:
            await asyncio.wait_for(asyncio.shield(native._main_atask), 3)
        except Exception as exc:
            outcomes.append(type(exc).__name__)
        finally:
            with suppress(Exception, asyncio.CancelledError):
                await native.aclose()
            await model.aclose()

    backend._drive_voice = drive  # Protocol-only boundary; actual voice() owns HTTP.
    try:
        await backend.voice()
        assert backend.gate.attempts == 1
        assert outcomes  # Failed/ambiguous opening is not a successful voice call.
        assert len(observed) == 1, "Ambiguous opening or redirect must not replay"
        assert len(owners) == 1 and owners[0].closed
        assert backend.measurements["native_http_requests_dispatched"] == 1
        assert backend.measurements["native_http_replays_blocked"] == 1
        assert backend.measurements["native_transport_single_request"] is False
    finally:
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)


async def test_guarded_owner_permits_one_real_loopback_websocket(tmp_path):
    from aiohttp import web

    harness = load_harness()
    opened = asyncio.Event()
    closed = asyncio.Event()
    observed = []

    async def peer(request):
        observed.append(request.path)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        opened.set()
        try:
            async for message in ws:
                pass  # Only real transport; no synthetic model/session response.
        finally:
            closed.set()
        return ws

    app = web.Application()
    app.router.add_get("/realtime", peer)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    _, port = runner.addresses[0]
    backend = harness.RealSession(Settings(openai_api_key=""),
                                  SimpleNamespace(seconds=1), directory=tmp_path)
    owners = []

    def factory(*, http_session):
        owners.append(http_session)
        return realtime.RealtimeModel(
            api_key="synthetic-loopback-only", base_url=f"http://127.0.0.1:{port}",
            http_session=http_session, max_session_duration=None,
            conn_options=APIConnectOptions(max_retry=0, timeout=2),
        )

    backend.gate.factory = factory

    async def drive(**options):
        model = backend.gate()
        native = model.session()
        try:
            await asyncio.wait_for(opened.wait(), 2)
        finally:
            await native.aclose()
            await model.aclose()

    backend._drive_voice = drive
    try:
        await backend.voice()
        await asyncio.wait_for(closed.wait(), 2)
        assert observed == ["/realtime"]
        assert backend.gate.attempts == 1
        assert len(owners) == 1 and owners[0].closed
        assert backend.measurements["native_http_requests_dispatched"] == 1
        assert backend.measurements["native_http_replays_blocked"] == 0
        assert backend.measurements["native_transport_single_request"] is True
    finally:
        await runner.cleanup()
