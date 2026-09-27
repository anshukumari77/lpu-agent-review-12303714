"""Unpaid installed-SDK protocol fixtures; never provider/model evidence.

The peer has no sockets. Outbound strings run through the installed SDK's real
_run_ws; inbound GA fixtures are validated against the installed OpenAPI-generated
ConversationItemAdded schema. No response generation, audio, or business answers.
"""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import socket
import time

from livekit.agents import llm
from openai.types.realtime import ConversationItemAdded, RealtimeErrorEvent
import pytest
from pydantic import ValidationError

from beep_agent.realtime import assemble_native
from test_native_screen_grounding_fences import revoke
from test_native_screen_grounding_sdk import (
    WirePeer, content_text, current, frame, running, settings, until,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('No sockets are permitted in native protocol fixtures')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)


@asynccontextmanager
async def installed_session(peer):
    assembly = assemble_native(settings(), current(), http_session=peer, base_url='http://127.0.0.1')
    try:
        await assembly.session.start(assembly.agent, record=False, session_host=False)
        yield assembly
    finally:
        await assembly.session.aclose()
        await assembly.model.aclose()
        await peer.close()


class ProtocolPeer(WirePeer):
    """GA acknowledgements with optional media omitted, and an explicit IO gate."""

    def __init__(self, *, hold_image=False, omit_image_url=False):
        super().__init__()
        self.hold_image = hold_image
        self.omit_image_url = omit_image_url
        self.image_entered = asyncio.Event()
        self.image_release = asyncio.Event()
        self.image_event = None
        self.acks = []

    async def send_str(self, value):
        event = json.loads(value)
        if event['type'] != 'conversation.item.create':
            return await super().send_str(value)
        self.wire.append(event)
        item = deepcopy(event['item'])
        if any(c['type'] == 'input_image' for c in item.get('content', [])):
            self.image_event = event
            self.image_entered.set()
            if self.hold_image:
                await self.image_release.wait()
        previous = event.get('previous_item_id')
        assert previous in {None, 'root'} or previous in self.accepted
        assert item['id'] not in self.accepted, 'Unexpected item recreation'
        self.accepted[item['id']] = deepcopy(item)
        item.update(object='realtime.item', status='completed')
        for part in item.get('content', []):
            part.pop('audio', None)  # GA added explicitly excludes audio bytes.
            if self.omit_image_url:
                part.pop('image_url', None)  # Optional in generated user content schema.
        await self.added(item, None if previous == 'root' else previous)

    async def added(self, item, previous=None):
        event = dict(type='conversation.item.added', event_id='synthetic-ga-added',
                     previous_item_id=previous, item=item)
        ConversationItemAdded.model_validate(event)  # strict required-field check, not construct
        self.acks.append(deepcopy(event))
        await self.reply(event)

    async def pending_assistant(self, previous):
        item = dict(id='item_synthetic_pending_assistant', object='realtime.item',
                    type='message', role='assistant', status='in_progress', content=[])
        self.accepted[item['id']] = item
        await self.added(item, previous)
        return item['id']


async def test_pixel_withdrawal_preserves_assistant_added_during_image_ack(monkeypatch):
    peer = ProtocolPeer(hold_image=True, omit_image_url=True)
    async with running(monkeypatch, peer=peer) as run:
        await peer.image_entered.wait()
        sdk = run.runtime.assembly.agent.realtime_llm_session
        assistant_id = await peer.pending_assistant(peer.image_event['previous_item_id'])
        await until(lambda: sdk.chat_ctx.get_by_id(assistant_id) is not None)
        # The server's prior tail differs from the optimistic local screen tail.
        # AgentActivity._on_remote_item_added currently drops this placeholder.
        assert run.runtime.assembly.agent.chat_ctx.get_by_id(assistant_id) is None
        revoke(run.runtime, 'pixels')
        peer.image_release.set()
        await until(lambda: 'context is unavailable or stale' in content_text(peer.accepted.values()))
        deleted = {event['item_id'] for event in peer.wire if event['type'] == 'conversation.item.delete'}
        assert assistant_id not in deleted, 'Screen withdrawal deleted an unrelated in-progress assistant item'
        assert assistant_id in peer.accepted
        assert peer.image_event['item']['id'] in deleted
        assert len(run.requests) == 1  # intercepted existing introduction; no generated replies
        assert not any(event['type'] == 'response.create' for event in peer.wire)


async def test_screen_replacement_does_not_truncate_unmanaged_conversation(monkeypatch):
    peer = ProtocolPeer(omit_image_url=True)
    async with running(monkeypatch, peer=peer) as run:
        sdk = run.runtime.assembly.agent.realtime_llm_session
        old_image_id = peer.image_event['item']['id']
        await until(lambda: sdk.chat_ctx.get_by_id(old_image_id) is not None)
        previous = old_image_id
        conversation_ids = set()
        for index in range(38):
            role = 'user' if index % 2 == 0 else 'assistant'
            item = dict(id=f'item_synthetic_conversation_{index}', object='realtime.item',
                        type='message', role=role, status='completed', content=[{
                            'type': 'input_text' if role == 'user' else 'output_text',
                            'text': f'Synthetic {role} protocol marker {index}',
                        }])
            peer.accepted[item['id']] = deepcopy(item)
            await peer.added(item, previous)
            previous = item['id']
            conversation_ids.add(item['id'])
        await until(lambda: sdk.chat_ctx.get_by_id(previous) is not None)
        start = len(peer.wire)
        revoke(run.runtime, 'track')
        await until(lambda: old_image_id not in peer.accepted)
        deleted = {event['item_id'] for event in peer.wire[start:]
                   if event['type'] == 'conversation.item.delete'}
        assert deleted == {old_image_id}, 'A screen refresh may replace only managed screen items'
        assert conversation_ids <= set(peer.accepted)
        assert conversation_ids <= {item.id for item in run.runtime.assembly.agent.chat_ctx.items}


@pytest.mark.parametrize('omit_image_url', [False, True])
async def test_ga_added_without_optional_image_fields_resolves_real_update(omit_image_url):
    peer = ProtocolPeer(omit_image_url=omit_image_url)
    async with installed_session(peer) as assembly:
        sample = frame()
        chat = assembly.agent.chat_ctx.copy()
        image = chat.add_message(role='user', content=[
            'Synthetic image protocol marker', llm.ImageContent(image=sample.data_url),
        ])
        await assembly.agent.update_chat_ctx(chat)
        ack = next(e for e in peer.acks if e['item']['id'] == image.id)
        ack_image = next(part for part in ack['item']['content'] if part['type'] == 'input_image')
        assert ('image_url' in ack_image) is not omit_image_url
        remote = assembly.agent.realtime_llm_session.chat_ctx.get_by_id(image.id)
        assert remote is not None  # A synthetic received item acknowledgement, not local intent.
        assert any(isinstance(c, llm.ImageContent) for c in remote.content) is not omit_image_url
        assert peer.image_event['item']['content'][1]['image_url'] == sample.data_url
        await assembly.agent.update_chat_ctx(llm.ChatContext())
        assert assembly.agent.realtime_llm_session.chat_ctx.get_by_id(image.id) is None
        assert image.id not in peer.accepted


@pytest.mark.parametrize('role,part', [
    ('user', {'type': 'input_audio'}),
    ('user', {'type': 'input_audio', 'transcript': 'Synthetic client protocol marker'}),
    ('assistant', {'type': 'output_audio', 'transcript': ''}),
])
async def test_ga_audio_item_with_omitted_audio_is_preserved_on_screen_replacement(monkeypatch, role, part):
    peer = ProtocolPeer(hold_image=True, omit_image_url=True)
    async with running(monkeypatch, peer=peer) as run:
        await peer.image_entered.wait()
        sdk = run.runtime.assembly.agent.realtime_llm_session
        item = dict(id='item_synthetic_audio', object='realtime.item', type='message',
                    role=role, status='completed', content=[deepcopy(part)])
        peer.accepted[item['id']] = deepcopy(item)
        await peer.added(item, peer.image_event['previous_item_id'])
        await until(lambda: sdk.chat_ctx.get_by_id(item['id']) is not None)
        peer.image_release.set()
        await until(lambda: sdk.chat_ctx.get_by_id(peer.image_event['item']['id']) is not None)
        revoke(run.runtime, 'track')
        old_image_id = peer.image_event['item']['id']
        await until(lambda: old_image_id not in peer.accepted)
        assert peer.accepted[item['id']] == item
        assert sdk.chat_ctx.get_by_id(item['id']) is not None
        assert run.runtime.assembly.agent.chat_ctx.get_by_id(item['id']) is not None
        assert not any(event['type'] == 'conversation.item.delete' and event['item_id'] == item['id']
                       for event in peer.wire)
        assert len(run.requests) == 1


class FaultPeer(ProtocolPeer):
    """Explicit acknowledgement faults; none are claimed historical events."""

    def __init__(self, fault):
        super().__init__()
        self.fault = fault
        self.fault_event = None

    async def added(self, item, previous=None):
        if not any(c['type'] == 'input_image' for c in item.get('content', [])):
            return await super().added(item, previous)
        original = dict(type='conversation.item.added', event_id='synthetic-ga-added',
                        previous_item_id=previous, item=item)
        ConversationItemAdded.model_validate(original)
        event = deepcopy(original)
        if self.fault == 'drop':
            self.fault_event = event
            return
        if self.fault in {'rejected', 'uncorrelated_rejection'}:
            self.accepted.pop(item['id'])
            error = dict(type='invalid_request_error', code='invalid_value', param='item',
                         message='Synthetic protocol rejection')
            if self.fault == 'rejected':
                error['event_id'] = self.image_event['event_id']
            event = dict(type='error', event_id='synthetic-reject', error=error)
            RealtimeErrorEvent.model_validate(event)
        elif self.fault == 'missing_content':
            event['item'].pop('content')
            with pytest.raises(ValidationError):
                ConversationItemAdded.model_validate(event)
        elif self.fault == 'wrong_item_id':
            event['item']['id'] = 'item_synthetic_wrong_correlation'
            ConversationItemAdded.model_validate(event)
        elif self.fault == 'legacy_created':
            event['type'] = 'conversation.item.created'
            with pytest.raises(ValidationError):
                ConversationItemAdded.model_validate(event)
        else:
            raise AssertionError(self.fault)
        self.fault_event = event
        await self.reply(event)


@pytest.mark.parametrize('fault', ['drop', 'missing_content', 'wrong_item_id',
                                  'legacy_created', 'uncorrelated_rejection'])
async def test_real_sdk_five_second_timeout_for_unsettled_image_ack(fault, record_property):
    peer = FaultPeer(fault)
    async with installed_session(peer) as assembly:
        chat = assembly.agent.chat_ctx.copy()
        image = chat.add_message(role='user', content=[llm.ImageContent(image=frame().data_url)])
        started = time.monotonic()
        with pytest.raises(llm.RealtimeError, match=r'^update_chat_ctx timed out\.$'):
            async with asyncio.timeout(8):
                await assembly.agent.update_chat_ctx(chat)
        elapsed = time.monotonic() - started
        record_property('actual_update_elapsed_seconds', round(elapsed, 3))
        assert 4.8 <= elapsed < 8  # Real installed 5.0s timer; never patched or shortened.
        sdk = assembly.agent.realtime_llm_session
        assert assembly.agent.chat_ctx.get_by_id(image.id) is not None
        assert sdk.chat_ctx.get_by_id(image.id) is None
        assert len([e for e in peer.wire if e['type'] == 'conversation.item.create'
                    and e['item']['id'] == image.id]) == 1
        assert not any(e['type'] == 'response.create' for e in peer.wire)
        assert not sdk._chat_ctx_event_futures and not sdk._item_create_future


async def test_correlated_rejection_returns_normally_but_has_no_remote_item(record_property):
    peer = FaultPeer('rejected')
    async with installed_session(peer) as assembly:
        chat = assembly.agent.chat_ctx.copy()
        image = chat.add_message(role='user', content=[llm.ImageContent(image=frame().data_url)])
        started = time.monotonic()
        await assembly.agent.update_chat_ctx(chat)
        elapsed = time.monotonic() - started
        record_property('actual_update_elapsed_seconds', round(elapsed, 3))
        assert elapsed < 1
        assert assembly.agent.chat_ctx.get_by_id(image.id) is not None
        assert assembly.agent.realtime_llm_session.chat_ctx.get_by_id(image.id) is None
        assert image.id not in peer.accepted


async def test_revocation_during_rejection_notice_preserves_interleaved_assistant(monkeypatch):
    class NoticePeer(FaultPeer):
        def __init__(self):
            super().__init__('rejected')
            self.notice_entered = asyncio.Event()
            self.notice_release = asyncio.Event()
            self.notice_previous = None

        async def added(self, item, previous=None):
            if 'Some selected-screen context was not accepted' in content_text([item]):
                self.notice_previous = previous
                self.notice_entered.set()
                await self.notice_release.wait()
            await super().added(item, previous)

    peer = NoticePeer()
    async with running(monkeypatch, peer=peer) as run:
        await asyncio.wait_for(peer.notice_entered.wait(), 1)
        sdk = run.runtime.assembly.agent.realtime_llm_session
        assistant_id = await peer.pending_assistant(peer.notice_previous)
        await until(lambda: sdk.chat_ctx.get_by_id(assistant_id) is not None)
        revoke(run.runtime, 'epoch')
        peer.notice_release.set()
        await until(lambda: run.task.done())
        assert run.task.exception() is None
        assert assistant_id in peer.accepted
        assert not any(event['type'] == 'conversation.item.delete' and event['item_id'] == assistant_id
                       for event in peer.wire)
        assert not any(c['type'] == 'input_image' for item in peer.accepted.values()
                       for c in item.get('content', []))
        assert len(run.requests) == 1


async def test_runtime_propagates_update_timeout_and_closes_without_replay(monkeypatch):
    peer = FaultPeer('drop')
    async with running(monkeypatch, peer=peer) as run:
        # Exercise _generation's actual exception/cleanup path, not a fabricated
        # harness category. The outer poll/watchdog is not started by this fixture.
        with pytest.raises(llm.RealtimeError, match=r'^update_chat_ctx timed out\.$'):
            async with asyncio.timeout(8):
                await asyncio.shield(run.task)
        assert peer.closed
        assert run.runtime.assembly is None
        assert len([e for e in peer.wire if e['type'] == 'conversation.item.create'
                    and any(c['type'] == 'input_image' for c in e['item']['content'])]) == 1
        assert len(run.requests) == 1
