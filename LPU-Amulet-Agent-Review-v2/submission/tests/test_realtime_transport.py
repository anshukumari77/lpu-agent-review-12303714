"""Actual local RTC + actual SDK + synthetic provider WebSocket, never paid inference."""

import asyncio
import base64
import json
import os
from pathlib import Path

import aiohttp
from aiohttp import web
import pytest
from livekit import api, rtc

from test_worker import database as worker_database, active_session


@pytest.fixture
def database():
    yield from worker_database.__wrapped__()


class SyntheticRealtime:
    """Protocol fixture only; explicitly NOT an inference implementation."""

    def __init__(self):
        self.events = []
        self.connected = asyncio.Event()
        self.socket = None
        self.count = 0

    async def handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.socket = ws
        self.connected.set()
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(message.data)
            self.events.append(data)
            kind = data["type"]
            if kind == "conversation.item.create":
                await ws.send_json(
                    {
                        "type": "conversation.item.added",
                        "event_id": "ack",
                        "previous_item_id": data.get("previous_item_id"),
                        "item": data["item"],
                    }
                )
            elif kind == "conversation.item.delete":
                await ws.send_json(
                    {
                        "type": "conversation.item.deleted",
                        "event_id": "del",
                        "item_id": data["item_id"],
                    }
                )
            elif kind == "response.create":
                self.count += 1
                rid, iid = f"resp-{self.count}", f"assistant-{self.count}"
                response = {
                    "id": rid,
                    "object": "realtime.response",
                    "status": "in_progress",
                    "metadata": data.get("response", {}).get("metadata"),
                    "output": [],
                }
                await ws.send_json(
                    {"type": "response.created", "event_id": "r", "response": response}
                )
                item = {
                    "id": iid,
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                }
                await ws.send_json(
                    {
                        "type": "response.output_item.added",
                        "event_id": "o",
                        "response_id": rid,
                        "output_index": 0,
                        "item": item,
                    }
                )
                await ws.send_json(
                    {
                        "type": "response.content_part.added",
                        "event_id": "p",
                        "response_id": rid,
                        "item_id": iid,
                        "output_index": 0,
                        "content_index": 0,
                        "part": {"type": "audio", "transcript": ""},
                    }
                )
                await ws.send_json(
                    {
                        "type": "response.output_audio_transcript.delta",
                        "event_id": "t",
                        "response_id": rid,
                        "item_id": iid,
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "Who approves the quote?",
                    }
                )
                await ws.send_json(
                    {
                        "type": "response.output_audio.delta",
                        "event_id": "a",
                        "response_id": rid,
                        "item_id": iid,
                        "output_index": 0,
                        "content_index": 0,
                        "delta": base64.b64encode(bytes(24000)).decode(),
                    }
                )
                item.update(
                    status="completed",
                    content=[{"type": "audio", "transcript": "Who approves the quote?"}],
                )
                await ws.send_json(
                    {
                        "type": "response.output_item.done",
                        "event_id": "d",
                        "response_id": rid,
                        "output_index": 0,
                        "item": item,
                    }
                )
                response.update(
                    status="completed",
                    output=[item],
                    usage={
                        "input_tokens": 10,
                        "output_tokens": 10,
                        "total_tokens": 20,
                        "input_token_details": {},
                        "output_token_details": {},
                    },
                )
                await ws.send_json(
                    {"type": "response.done", "event_id": "end", "response": response}
                )
        return ws

    async def client_transcript(self, text):
        await self.socket.send_json(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "event_id": "client-transcript",
                "item_id": "client-turn",
                "content_index": 0,
                "transcript": text,
                "logprobs": None,
            }
        )


async def connect_room(cfg, room_name, identity, *, auto_subscribe=True):
    token = api.AccessToken(
        cfg.livekit_api_key.get_secret_value(), cfg.livekit_api_secret.get_secret_value()
    )
    token.with_identity(identity).with_grants(api.VideoGrants(room_join=True, room=room_name))
    room = rtc.Room()
    await room.connect(
        cfg.livekit_url, token.to_jwt(), options=rtc.RoomOptions(auto_subscribe=auto_subscribe)
    )
    return room


async def wait_until(predicate, timeout=10):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.05)


async def test_real_sdk_local_transport_final_transcripts_and_screen_images(database):
    from beep_agent.config import Settings
    from beep_agent.store import Store
    from beep_agent.realtime import InterviewRuntime, assemble_native
    from beep_agent.media import VisualObserver
    from openai import AsyncOpenAI
    import httpx

    path = os.environ.get("BEEP_TEST_LIVEKIT_CONFIG")
    if not path:
        pytest.skip("BEEP_TEST_LIVEKIT_CONFIG required for actual RTC test")
    cfg = Settings(
        **{**json.loads(Path(path).read_text()), "openai_api_key": "synthetic-never-paid"}
    )
    store = Store(database)
    store.initialize()
    state = active_session(store)
    sid = state["id"]
    store.set_recording("tenant", sid, "recording", "EG-synthetic-boundary")
    from beep_agent.worker import ensure_agent_dispatch

    cfg = cfg.model_copy(update={"agent_name": "beep-synthetic-unregistered"})
    first_dispatch = await ensure_agent_dispatch(cfg, store, state)
    second_dispatch = await ensure_agent_dispatch(cfg, store, state)
    assert first_dispatch and first_dispatch == second_dispatch
    fixture = SyntheticRealtime()
    app = web.Application()
    app.router.add_get("/v1/realtime", fixture.handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    rooms = []
    runtime = None
    task = None
    publisher = None
    audio_publisher = None
    audio_sources = []
    send_audio = True

    async def send_microphones():
        import math
        from array import array

        pcm = array("h", [int(3000 * math.sin(i * 2 * math.pi * 440 / 48000)) for i in range(960)])
        while True:
            if send_audio:
                for source in audio_sources:
                    await source.capture_frame(rtc.AudioFrame(pcm.tobytes(), 48000, 1, 960))
            await asyncio.sleep(0.02)

    def visual_response(request):
        return httpx.Response(
            200,
            json={
                "id": "vision-fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-5.4-mini",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "readable": False,
                                    "summary": "Approval column is too small to read",
                                    "focus": "approval",
                                    "uncertainties": ["Rule not legible"],
                                    "crop_box": None,
                                }
                            ),
                        },
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            },
        )

    vision_client = AsyncOpenAI(
        api_key="synthetic",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(visual_response)),
    )
    try:
        # The facilitator deliberately arrives first; they must never become the linked client.
        facilitator = await connect_room(cfg, state["room_name"], "facilitator:" + sid)
        rooms.append(facilitator)
        client = await connect_room(cfg, state["room_name"], "client:" + sid)
        rooms.append(client)
        agent_room = await connect_room(
            cfg, state["room_name"], "agent:" + sid, auto_subscribe=False
        )
        rooms.append(agent_room)
        async with aiohttp.ClientSession() as http:

            def factory(settings, session, **kwargs):
                return assemble_native(
                    settings,
                    session,
                    **kwargs,
                    http_session=http,
                    base_url=f"http://127.0.0.1:{port}/v1",
                )

            runtime = InterviewRuntime(
                cfg,
                store,
                "tenant",
                sid,
                agent_room,
                assembly_factory=factory,
                observer_factory=lambda settings, session: VisualObserver(
                    settings, session, client=vision_client
                ),
            )
            task = asyncio.create_task(runtime.run())
            await asyncio.wait_for(fixture.connected.wait(), 10)
            facilitator_source = rtc.AudioSource(48000, 1, queue_size_ms=100)
            audio_sources.append(facilitator_source)
            await facilitator.local_participant.publish_track(
                rtc.LocalAudioTrack.create_audio_track("facilitator-mic", facilitator_source),
                rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
            )
            audio_publisher = asyncio.create_task(send_microphones())
            await asyncio.sleep(1)
            assert not any(e["type"] == "input_audio_buffer.append" for e in fixture.events)
            client_source = rtc.AudioSource(48000, 1, queue_size_ms=100)
            audio_sources.append(client_source)
            await client.local_participant.publish_track(
                rtc.LocalAudioTrack.create_audio_track("client-mic", client_source),
                rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
            )
            await wait_until(
                lambda: any(e["type"] == "input_audio_buffer.append" for e in fixture.events)
            )
            send_audio = False
            await fixture.client_transcript("The manager approves the quote.")
            await wait_until(
                lambda: any(e["actor"] == "client" for e in store.list_events("tenant", sid))
            )
            evidence = store.list_events("tenant", sid)
            assert any(
                e["actor"] == "client" and e["text"] == "The manager approves the quote."
                for e in evidence
            )
            await wait_until(
                lambda: any(e["actor"] == "agent" for e in store.list_events("tenant", sid))
            )
            delivered = [e for e in store.list_events("tenant", sid) if e["actor"] == "agent"]
            assert delivered[0]["text"] == "Who approves the quote?"
            screen_source, camera_source = rtc.VideoSource(640, 480), rtc.VideoSource(640, 480)
            screen_track = rtc.LocalVideoTrack.create_video_track("selected-screen", screen_source)
            camera_track = rtc.LocalVideoTrack.create_video_track("webcam-last", camera_source)
            screen_pub = await client.local_participant.publish_track(
                screen_track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_SCREENSHARE)
            )
            await client.local_participant.publish_track(
                camera_track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA)
            )

            async def send_frames():
                while True:
                    screen_source.capture_frame(
                        rtc.VideoFrame(
                            640,
                            480,
                            rtc.VideoBufferType.RGBA,
                            bytes([40, 80, 120, 255]) * (640 * 480),
                        )
                    )
                    camera_source.capture_frame(
                        rtc.VideoFrame(
                            640,
                            480,
                            rtc.VideoBufferType.RGBA,
                            bytes([255, 0, 0, 255]) * (640 * 480),
                        )
                    )
                    await asyncio.sleep(0.2)

            publisher = asyncio.create_task(send_frames())
            await wait_until(
                lambda: any(
                    e["kind"] == "screen_observation" for e in store.list_events("tenant", sid)
                ),
                15,
            )
            screen_event = next(
                e for e in store.list_events("tenant", sid) if e["kind"] == "screen_observation"
            )
            assert screen_event["source_ref"].startswith("livekit:" + screen_pub.sid)
            assert "UNREADABLE" in screen_event["text"]
            assert any(
                e["type"] == "conversation.item.create"
                and any(c["type"] == "input_image" for c in e["item"].get("content", []))
                for e in fixture.events
            )
            # A graph-produced directive is synthesized here as test data from actual stored events.
            # No new client audio is sent: the silent-work controller must trigger response.create.
            persisted = store.list_events("tenant", sid)
            snapshot = store.load_snapshot("tenant", sid)
            snapshot.update(
                revision=1,
                last_event_seq=persisted[-1]["seq"],
                evidence=persisted,
                probe={
                    "id": "silent-approval",
                    "text": "Please zoom the approval field.",
                    "reason": "Unreadable selected screen",
                    "evidence_ids": [screen_event["id"]],
                    "consent_epoch": 0,
                    "based_on_revision": 1,
                },
            )
            assert store.save_snapshot(
                "tenant", sid, snapshot, expected_revision=0, consent_epoch=0
            )
            import time

            runtime.directives.last_activity = time.monotonic() - 30
            runtime.directives.last_spoken = time.monotonic() - 30
            await wait_until(
                lambda: any(
                    e["type"] == "response.create"
                    and "zoom the approval" in e.get("response", {}).get("instructions", "")
                    for e in fixture.events
                )
            )
            await facilitator.disconnect()
            assert (
                runtime.assembly is not None
            )  # facilitator departure does not close client session
            send_audio = True
            store.control("tenant", sid, "client", "pause")
            await wait_until(lambda: runtime.model_task is None)
            assert runtime.assembly is None
            assert any(e["type"] == "input_audio_buffer.clear" for e in fixture.events)
            stopped_count = sum(e["type"] == "input_audio_buffer.append" for e in fixture.events)
            await asyncio.sleep(0.4)
            assert (
                sum(e["type"] == "input_audio_buffer.append" for e in fixture.events)
                == stopped_count
            )
    finally:
        if audio_publisher:
            audio_publisher.cancel()
            await asyncio.gather(audio_publisher, return_exceptions=True)
        for source in audio_sources:
            await source.aclose()
        if publisher:
            publisher.cancel()
            await asyncio.gather(publisher, return_exceptions=True)
        if runtime:
            await runtime.aclose()
        if task:
            await asyncio.wait_for(task, 15)
        for room in reversed(rooms):
            await room.disconnect()
        async with api.LiveKitAPI(
            cfg.livekit_url,
            cfg.livekit_api_key.get_secret_value(),
            cfg.livekit_api_secret.get_secret_value(),
        ) as lk:
            await lk.room.delete_room(api.DeleteRoomRequest(room=state["room_name"]))
        await runner.cleanup()
        await vision_client.close()
