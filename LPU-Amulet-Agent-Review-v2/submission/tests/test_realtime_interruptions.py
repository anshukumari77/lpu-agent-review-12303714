"""Real SDK/WebSocket with an instrumented playback-device boundary (not human audibility)."""

import asyncio
import time

import aiohttp
from aiohttp import web
from livekit.agents.voice import io

from test_realtime import settings, state
from test_realtime_transport import SyntheticRealtime


class ControlledSpeaker(io.AudioOutput):
    def __init__(self):
        super().__init__(
            label="synthetic-playback-device", capabilities=io.AudioOutputCapabilities(pause=False)
        )
        self.ready = asyncio.Event()
        self.frames = 0

    async def capture_frame(self, frame):
        await super().capture_frame(frame)
        self.frames += 1
        self.on_playback_started(created_at=time.time())
        self.ready.set()

    def flush(self):
        super().flush()  # the test device has NOT completed playback yet

    def clear_buffer(self):
        if self._pending_playback_count:
            self.on_playback_finished(
                playback_position=0.2, interrupted=True, synchronized_transcript="Who approves"
            )


async def test_real_sdk_interruption_persists_only_device_delivered_prefix():
    from beep_agent.realtime import assemble_native, close_native, transcript_evidence

    fixture = SyntheticRealtime()
    app = web.Application()
    app.router.add_get("/v1/realtime", fixture.handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as http:
            assembly = assemble_native(
                settings(), state(), http_session=http, base_url=f"http://127.0.0.1:{port}/v1"
            )
            sink = ControlledSpeaker()
            assembly.session.output.audio = sink
            delivered = []
            assembly.session.on("conversation_item_added", lambda ev: delivered.append(ev.item))
            try:
                await assembly.session.start(assembly.agent, record=False, session_host=False)
                handle = assembly.session.generate_reply(instructions="Synthetic question")
                await asyncio.wait_for(sink.ready.wait(), 10)
                await asyncio.wait_for(assembly.session.interrupt(force=True), 10)
                await asyncio.wait_for(handle, 10)
                assert sink.frames > 0
                message = next(
                    item for item in delivered if getattr(item, "role", None) == "assistant"
                )
                assert message.interrupted
                evidence = transcript_evidence(message, state(), 1500)
                assert evidence["text"] == "Who approves"
                assert "the quote" not in evidence["text"]
            finally:
                await close_native(assembly)
    finally:
        await runner.cleanup()
