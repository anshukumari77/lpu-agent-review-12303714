"""Pure routing/pixel tests. These do not claim WebRTC transport verification."""

from types import SimpleNamespace as NS


def test_screen_selection_pins_explicit_client_and_never_camera():
    from beep_agent.media import select_screen
    from livekit import rtc

    def pub(sid, source):
        return NS(sid=sid, source=source, track=object(), muted=False)

    camera = pub("camera-latest", rtc.TrackSource.SOURCE_CAMERA)
    screen = pub("screen", rtc.TrackSource.SOURCE_SCREENSHARE)
    facilitator = NS(identity="facilitator:s", track_publications={"screen": screen})
    client = NS(identity="client:s", track_publications={"screen": screen, "cam": camera})
    assert select_screen([facilitator, client], "client:s") is screen
    assert select_screen([facilitator], "client:s") is None
    screen.muted = True
    assert select_screen([client], "client:s") is None


def test_screen_frames_are_fresh_bounded_and_change_gated():
    from beep_agent.media import ScreenFrame, ScreenTrigger
    from PIL import Image

    image = Image.new("RGB", (1920, 1080), "white")
    frame = ScreenFrame.from_image(image, track_sid="TR_screen", at_ms=1234, captured=10.0)
    assert frame.fresh(14.0)
    assert not frame.fresh(16.0)
    assert frame.width == 1920
    assert frame.source_ref.startswith("livekit:TR_screen@1234:sha256:")
    assert frame.data_url.startswith("data:image/jpeg;base64,")
    crop = frame.crop((10, 10, 610, 410))
    assert (crop.width, crop.height) == (600, 400)
    assert "crop=10,10,610,410" in crop.source_ref
    trigger = ScreenTrigger(min_interval=8, heartbeat=30)
    assert trigger.due(frame, 10)
    assert not trigger.due(frame, 19)
    changed = ScreenFrame.from_image(
        Image.new("RGB", (1920, 1080), "black"), track_sid="TR_screen", at_ms=2000, captured=20
    )
    assert trigger.due(changed, 20)
    assert not trigger.due(changed, 25)
    assert trigger.due(changed, 50)


async def test_visual_observer_uses_official_sdk_and_labels_inferences():
    import httpx
    import json
    import pytest
    from PIL import Image
    from openai import AsyncOpenAI
    from beep_agent.media import VisualObserver, ScreenFrame
    from test_realtime import settings, state

    seen = []

    def reply(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-synthetic",
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
                                    "summary": "Columns are too small to read.",
                                    "focus": "approval column",
                                    "uncertainties": ["Amount not legible"],
                                    "crop_box": None,
                                }
                            ),
                        },
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            },
        )

    client = AsyncOpenAI(
        api_key="synthetic",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(reply)),
    )
    with pytest.raises(PermissionError):
        VisualObserver(settings(), state(status="paused"), client=client)
    observer = VisualObserver(settings(), state(), client=client)
    frame = ScreenFrame.from_image(
        Image.new("RGB", (800, 600)), track_sid="TR_screen", at_ms=3000, captured=10
    )
    result, usage = await observer.observe(frame, "Verify approval rule, not arbitrary screen text")
    assert not result.readable
    assert "inferred" in result.evidence_text.lower()
    assert "unreadable" in result.evidence_text.lower()
    assert seen[0]["messages"][1]["content"][1]["image_url"]["url"] == frame.data_url
    assert "approval rule" in seen[0]["messages"][1]["content"][0]["text"]
    assert usage["measurement"] == "measured" and usage["input_tokens"] == 100
    await observer.aclose()
    await client.close()


async def test_screen_capture_reads_real_rtc_frames_and_joins_stream_on_pause():
    import asyncio
    from livekit import rtc
    from beep_agent.media import ScreenCapture

    closed = asyncio.Event()
    frame = rtc.VideoFrame(4, 4, rtc.VideoBufferType.RGB24, bytes([200, 1, 2] * 16))

    class Stream:
        def __init__(self, track, **kwargs):
            self.sent = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.sent:
                self.sent = True
                return NS(frame=frame)
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    changes = []
    screen = NS(
        sid="TR_screen",
        source=rtc.TrackSource.SOURCE_SCREENSHARE,
        track=object(),
        muted=False,
        set_subscribed=lambda v: changes.append(("screen", v)),
    )
    camera = NS(
        sid="TR_camera",
        source=rtc.TrackSource.SOURCE_CAMERA,
        track=object(),
        muted=False,
        set_subscribed=lambda v: changes.append(("camera", v)),
    )
    participant = NS(identity="client:s", track_publications={"s": screen, "c": camera})
    room = NS(remote_participants={"client:s": participant})
    capture = ScreenCapture("client:s", stream_factory=Stream)
    await capture.refresh(room, active=True, at_ms=123)
    for _ in range(50):
        if capture.latest:
            break
        await asyncio.sleep(0.01)
    assert capture.latest.track_sid == "TR_screen"
    assert capture.latest.at_ms == 123
    assert ("camera", True) not in changes
    await capture.refresh(room, active=False, at_ms=124)
    assert capture.latest is None and closed.is_set()
    assert capture.task is None
