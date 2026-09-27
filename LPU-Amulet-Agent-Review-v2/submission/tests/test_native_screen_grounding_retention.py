"""Actual isolated PostgreSQL retention + native SDK; no provider/RTC/service calls."""
import asyncio
import json
import time

import pytest

from beep_agent.store import Store
from test_worker import active_session, database as worker_database
from test_native_screen_grounding_sdk import WirePeer, content_text, running, until
from test_native_screen_grounding_fences import DelayedObserver


@pytest.fixture
def database():
    yield from worker_database.__wrapped__()


def unlocked(store, sid):
    # Independent connection proves provider/SDK IO isn't inside the control mutex.
    with store._connect() as conn:
        row = conn.execute('SELECT id FROM beep_sessions WHERE id=%s FOR UPDATE NOWAIT', (sid,)).fetchone()
        assert row['id'] == sid


async def test_real_postgres_retained_observation_enters_native_without_holding_control_lock(database, monkeypatch):
    store = Store(database)
    store.initialize()
    current = active_session(store, recording=True)
    sid = current['id']
    boundaries = []

    class Observer(DelayedObserver):
        async def observe(self, sample, focus):
            unlocked(store, sid)
            boundaries.append('observer_io')
            return await super().observe(sample, focus)

    class Peer(WirePeer):
        async def send_str(self, value):
            event = json.loads(value)
            if event['type'] == 'conversation.item.create':
                unlocked(store, sid)
                boundaries.append('native_context_io')
            await super().send_str(value)

    observer = Observer()
    try:
        async with running(monkeypatch, store=store, state_override=current, observer=observer, peer=Peer()) as run:
            observer.release.set()
            await until(lambda: observer.summary in content_text(run.peer.accepted.values()))
            # Exact persisted readback, not a mock append acknowledgement.
            reader = Store(database)
            try:
                events = reader.list_events('tenant', sid)
                snapshot = reader.load_snapshot('tenant', sid)
            finally:
                reader.close()
            assert len(events) == 1 and events[0]['kind'] == 'screen_observation'
            assert events[0]['actor'] == 'observer'
            assert events[0]['source_ref'] in content_text(run.peer.accepted.values())
            assert snapshot['evidence'] == [] and snapshot['claims'] == []
            assert {'observer_io', 'native_context_io'} <= set(boundaries)
            assert len(run.requests) == 1
    finally:
        store.close()


@pytest.mark.parametrize('revoke', ['epoch', 'frame_loss'])
async def test_rechecks_after_real_retention_await_before_native_handoff(database, monkeypatch, revoke):
    store = Store(database)
    store.initialize()
    current = active_session(store, recording=True)
    sid = current['id']
    observer = DelayedObserver()
    persisted = asyncio.Event()
    release = asyncio.Event()

    def configure(runtime):
        original = runtime.db

        async def delayed_db(method, *args, **kwargs):
            result = await original(method, *args, **kwargs)
            if method == store.append_event:
                unlocked(store, sid)
                persisted.set()
                await release.wait()
            return result

        runtime.db = delayed_db

    try:
        async with running(monkeypatch, store=store, state_override=current,
                           observer=observer, configure=configure) as run:
            observer.release.set()
            await asyncio.wait_for(persisted.wait(), 1)
            assert len(store.list_events('tenant', sid)) == 1
            if revoke == 'epoch':
                paused = store.control('tenant', sid, 'client', 'pause')
                run.runtime.fence.update(paused, time.monotonic())
            else:
                run.runtime.capture.latest = None
            release.set()
            await asyncio.sleep(.3)
            assert observer.summary not in content_text(run.peer.accepted.values())
            assert len(run.requests) == 1
    finally:
        release.set()
        store.close()
