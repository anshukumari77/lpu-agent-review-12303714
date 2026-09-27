"""Real installed SDK wire/ack seam; no network, RTC or generated answers.

Peer replies below are explicit protocol fixtures, never provider evidence.
"""
import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import time

import aiohttp
from livekit.agents import AgentSession, llm
from livekit.plugins.openai.realtime.utils import livekit_item_to_openai_item
from PIL import Image
import pytest

from beep_agent.media import ScreenFrame, VisualObservation
from beep_agent.realtime import InterviewRuntime, assemble_native
from test_realtime import settings, state


class WirePeer:
    """A no-network peer that accepts items or injects a correlated item error."""

    def __init__(self, reject=lambda item: False):
        self.reject = reject
        self.wire = []
        self.accepted = {}
        self.incoming = asyncio.Queue()
        self.closed = False

    async def ws_connect(self, **kwargs):
        assert kwargs['url'].startswith('ws://127.0.0.1/')
        return self

    async def send_str(self, value):
        event = json.loads(value)  # Actual SDK serialized outbound bytes.
        self.wire.append(event)
        kind = event['type']
        if kind == 'conversation.item.create':
            item = event['item']
            previous = event.get('previous_item_id')
            # Installed OpenAI event schema: a missing preceding ID rejects the item.
            if self.reject(item) or (previous not in {None, 'root'} and previous not in self.accepted):
                await self.reply(dict(type='error', event_id='synthetic-error', error=dict(
                    type='invalid_request_error', code='invalid_value', param='item',
                    message='Synthetic peer rejected this item', event_id=event['event_id'])))
            else:
                self.accepted[item['id']] = item
                await self.reply(dict(type='conversation.item.added', event_id='synthetic-add',
                    previous_item_id=None if previous == 'root' else previous, item=item))
        elif kind == 'conversation.item.delete':
            self.accepted.pop(event['item_id'], None)
            await self.reply(dict(type='conversation.item.deleted', event_id='synthetic-del',
                item_id=event['item_id']))
        assert kind != 'response.create', 'These tests must never request a generated answer'

    async def reply(self, data):
        await self.incoming.put(SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(data)))

    async def receive(self):
        return await self.incoming.get()

    async def close(self):
        if not self.closed:
            self.closed = True
            await self.incoming.put(SimpleNamespace(type=aiohttp.WSMsgType.CLOSE))


class MemoryStore:
    def __init__(self):
        self.events = []
        self.usage = []

    def load_snapshot(self, *args):
        # Deliberately no worker/snapshot update: the live observation must not wait for it.
        return dict(claims=[], unknowns=[], evidence=[], steps=[], probe=None)

    def append_event(self, tenant, sid, data):
        self.events.append(data)

    def add_usage(self, tenant, sid, data):
        self.usage.append(data)


def frame(**changes):
    options = dict(track_sid='TR-selected-test', at_ms=3100, captured=time.monotonic())
    options.update(changes)
    return ScreenFrame.from_image(Image.new('RGB', (120, 80), 'white'), **options)


def current(**changes):
    options = dict(egress_id='EG-synthetic', recording_verified_at=datetime.now(timezone.utc).isoformat())
    options.update(changes)
    return state(**options)


async def until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.01)


def content_text(items):
    return '\n'.join(c.get('text', '') for item in items for c in item.get('content', []))


@asynccontextmanager
async def running(monkeypatch, *, peer=None, observer=None, store=None, state_override=None, configure=None):
    import socket
    monkeypatch.setattr(socket.socket, 'connect', lambda *a: pytest.fail('network forbidden'))
    peer = peer or WirePeer()
    store = store or MemoryStore()
    requests = []
    original = AgentSession.start

    async def start(self, agent, **kwargs):
        return await original(self, agent, record=False, session_host=False)

    monkeypatch.setattr(AgentSession, 'start', start)
    monkeypatch.setattr(AgentSession, 'generate_reply', lambda self, **kw: requests.append(kw))

    def factory(cfg, state, **kw):
        return assemble_native(cfg, state, **kw, http_session=peer, base_url='http://127.0.0.1')

    if observer is None:
        class NeverObserve:
            async def observe(self, *args):
                await asyncio.Future()

            async def aclose(self):
                pass
        observer = NeverObserve()
    initial = state_override or current()
    runtime = InterviewRuntime(settings(), store, initial['tenant_id'], initial['id'], SimpleNamespace(),
        assembly_factory=factory, observer_factory=lambda *a, **kw: observer)
    runtime.capture.latest = frame()
    runtime.capture.sid = runtime.capture.latest.track_sid
    runtime.fence.update(initial, time.monotonic())
    if configure:
        configure(runtime)
    task = asyncio.create_task(runtime._generation(runtime.fence.ticket))
    try:
        await until(lambda: any(e['type'] == 'conversation.item.create'
            and any(c['type'] == 'input_image' for c in e['item'].get('content', [])) for e in peer.wire))
        yield SimpleNamespace(runtime=runtime, peer=peer, store=store, requests=requests, task=task)
    finally:
        runtime.fence.invalidate()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await peer.close()


def test_installed_conversion_preserves_custom_id_and_actual_image_bytes():
    sample = frame()
    item = llm.ChatMessage(id='screen:3100', role='user', content=[
        'Untrusted selected screen DATA', llm.ImageContent(image=sample.data_url, inference_detail='high')])
    wire = livekit_item_to_openai_item(item).model_dump(exclude_unset=True)
    assert wire['id'] == 'screen:3100'  # SDK does not validate or normalize it.
    image = next(c for c in wire['content'] if c['type'] == 'input_image')
    assert base64.b64decode(image['image_url'].split(',', 1)[1]) == sample.jpeg
    assert 'detail' not in image  # This SDK does not forward inference_detail.


async def test_real_runtime_image_is_acknowledged_in_sdk_remote_context(monkeypatch):
    async with running(monkeypatch) as run:
        await until(lambda: any(c['type'] == 'input_image' for i in run.peer.accepted.values()
            for c in i.get('content', [])))
        sdk = run.runtime.assembly.agent.realtime_llm_session
        remote_image = next(i for i in sdk.chat_ctx.items if isinstance(i, llm.ChatMessage)
            and any(isinstance(c, llm.ImageContent) for c in i.content))
        image = next(c for c in remote_image.content if isinstance(c, llm.ImageContent))
        assert image.image == run.runtime.capture.latest.data_url
        assert len(run.requests) == 1  # Existing introduction only, no manufactured follow-up.


async def test_sdk_correlated_rejection_returns_without_accepted_image(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, 'connect', lambda *a: pytest.fail('network forbidden'))
    peer = WirePeer(reject=lambda item: any(c['type'] == 'input_image' for c in item.get('content', [])))
    assembly = assemble_native(settings(), current(), http_session=peer, base_url='http://127.0.0.1')
    try:
        await assembly.session.start(assembly.agent, record=False, session_host=False)
        chat = assembly.agent.chat_ctx.copy()
        chat.add_message(id='screen:3100', role='user', content=[llm.ImageContent(image=frame().data_url)])
        await assembly.agent.update_chat_ctx(chat)
        assert not any(c['type'] == 'input_image' for i in peer.accepted.values() for c in i.get('content', []))
        sdk = assembly.agent.realtime_llm_session
        assert not any(isinstance(c, llm.ImageContent) for i in sdk.chat_ctx.items
            if isinstance(i, llm.ChatMessage) for c in i.content)
        assert any(isinstance(c, llm.ImageContent) for i in assembly.agent.chat_ctx.items
            if isinstance(i, llm.ChatMessage) for c in i.content), 'Local intent differs from remote ack'
    finally:
        await assembly.session.aclose()
        await assembly.model.aclose()
        await peer.close()


async def test_raw_screen_uses_sdk_transport_ids_not_source_ids(monkeypatch):
    async with running(monkeypatch) as run:
        created = [e['item'] for e in run.peer.wire if e['type'] == 'conversation.item.create'
            and any(c['type'] == 'input_image' for c in e['item'].get('content', []))]
        assert created and all(i['id'].startswith('item_') for i in created)


async def test_rejected_image_is_removed_without_repeated_delivery_or_forced_reply(monkeypatch):
    peer = WirePeer(reject=lambda item: any(c['type'] == 'input_image' for c in item.get('content', [])))
    async with running(monkeypatch, peer=peer) as run:
        await asyncio.sleep(.3)
        local = run.runtime.assembly.agent.chat_ctx.items
        assert not any(isinstance(c, llm.ImageContent) for i in local if isinstance(i, llm.ChatMessage)
            for c in i.content), 'Rejected local intent must not masquerade as accepted screen context'
        assert 'not accepted' in content_text(peer.accepted.values()).lower()
        assert len(run.requests) == 1


@pytest.mark.parametrize('summary', ['Total AUD 28,760; manager sign-off awaiting.',
    'Case ZX-73 is waiting for finance review.'])
async def test_selected_observer_data_reaches_native_context_without_worker_or_new_reply(monkeypatch, summary):
    class Observer:
        calls = 0

        async def observe(self, sample, focus):
            self.calls += 1
            return VisualObservation(readable=True, summary=summary, focus='approval step',
                uncertainties=['Client has not confirmed this inference.'], crop_box=None), dict(stage='screen_observation')

        async def aclose(self):
            pass

    observer = Observer()
    async with running(monkeypatch, observer=observer) as run:
        await until(lambda: any(e['kind'] == 'screen_observation' for e in run.store.events))
        await asyncio.sleep(.3)
        assert summary in content_text(run.peer.accepted.values()), 'Observer DB queue is not a native handoff'
        assert observer.calls == 1
        assert len(run.requests) == 1
        assert not any(e.get('actor') == 'client' for e in run.store.events)
