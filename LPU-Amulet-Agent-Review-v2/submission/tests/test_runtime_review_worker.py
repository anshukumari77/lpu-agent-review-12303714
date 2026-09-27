"""Review regressions: real PostgreSQL and SDK, synthetic network boundaries only."""
import asyncio

import httpx
import pytest

from beep_agent.store import Store
from beep_agent.worker import DurableWorker
from test_worker import active_session, database as worker_database, response, settings


@pytest.fixture
def database():
    yield from worker_database.__wrapped__()


async def test_pause_before_recording_publication_keeps_exact_cleanup(database, monkeypatch):
    store = Store(database)
    store.initialize()
    state = active_session(store)
    sid = state['id']
    original = store.set_recording
    calls = []

    def pause_before_activation(tenant, sid, status, eid=None):
        if status == 'recording':
            store.control(tenant, sid, 'client', 'pause')
        return original(tenant, sid, status, eid)

    class Egress:
        async def start(self, state):
            calls.append(('start', 'EG-race'))
            return 'EG-race'

        async def reconcile_stop(self, state, reservation):
            calls.append(('stop', 'EG-race'))
            return ['EG-race']

    monkeypatch.setattr(store, 'set_recording', pause_before_activation)
    store.enqueue_job('tenant', sid, 'recording_start', {'consent_epoch': 0})
    async with DurableWorker(settings().model_copy(update={'database_url': database}),
                             store=store, recording=Egress()) as worker:
        await worker.run_once()
        with store._connect() as conn:
            payloads = [r['payload'] for r in conn.execute(
                "SELECT payload FROM beep_jobs WHERE kind='recording_stop' AND session_id=%s", (sid,))]
        assert any(p.get('egress_id') == 'EG-race' for p in payloads)
        for _ in range(4):
            await worker.run_once()
        assert ('stop', 'EG-race') in calls
        assert store.get_session('tenant', sid)['recording_status'] == 'stopped'


async def test_pause_during_snapshot_load_never_dispatches_sdk(database, monkeypatch):
    store = Store(database)
    store.initialize()
    state = active_session(store, recording=True)
    sid = state['id']
    store.append_event('tenant', sid, dict(id='race', kind='transcript', actor='client',
        text='Manager approves.', at_ms=960001, consent_epoch=0))
    original = store.load_snapshot
    requests = []

    def pause_on_load(*args):
        snapshot = original(*args)
        store.control('tenant', sid, 'client', 'pause')
        return snapshot

    def boundary(request):
        requests.append(request)
        return response(request)

    monkeypatch.setattr(store, 'load_snapshot', pause_on_load)
    async with DurableWorker(settings().model_copy(update={'database_url': database}),
                             store=store, provider_transport=httpx.MockTransport(boundary)) as worker:
        await worker.run_once()
        assert requests == [], 'Pause acknowledged before admission must send no HTTP request'


async def test_independent_recording_health_fences_long_graph(database):
    from beep_agent import worker as module
    store = Store(database)
    store.initialize()
    state = active_session(store)
    sid = state['id']
    store.set_recording('tenant', sid, 'recording', 'EG-health')
    store.append_event('tenant', sid, dict(id='slow', kind='transcript', actor='client',
        text='Manager approves.', at_ms=960001, consent_epoch=0))
    entered, cancelled, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def boundary(request):
        entered.set()
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()

    class Egress:
        async def check_active(self, current):
            assert current['egress_id'] == 'EG-health'
            raise ConnectionError('synthetic terminated recorder')

    async with DurableWorker(settings().model_copy(update={'database_url': database}),
                             store=store, provider_transport=httpx.MockTransport(boundary)) as owner:
        running = asyncio.create_task(owner.run_once())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            monitor = asyncio.create_task(module.recording_health_sweep(
                settings(), store, stop, recording=Egress(), interval=.01, timeout=.1))
            try:
                await asyncio.wait_for(cancelled.wait(), 2)
                await running
                current = store.get_session('tenant', sid)
                assert current['status'] == 'paused'
                assert current['recording_status'] == 'failed'
                assert current['egress_id'] == 'EG-health'
            finally:
                stop.set()
                await monitor
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize('failure', ['cancel', 'lease_lost'])
async def test_late_start_retains_exact_id_after_owner_failure(database, failure):
    store = Store(database)
    store.initialize()
    state = active_session(store)
    sid = state['id']
    entered, release = asyncio.Event(), asyncio.Event()

    class Egress:
        async def start(self, current):
            assert current['recording_reservation']['consent_epoch'] == 0
            entered.set()
            await release.wait()
            return 'EG-late'

    store.enqueue_job('tenant', sid, 'recording_start', {'consent_epoch': 0})
    async with DurableWorker(settings().model_copy(update={'database_url': database}),
                             store=store, recording=Egress()) as owner:
        running = asyncio.create_task(owner.run_once())
        await entered.wait()
        if failure == 'cancel':
            running.cancel()
        else:
            with store._connect() as conn:
                conn.execute("UPDATE beep_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE session_id=%s AND status='running'", (sid,))
            await asyncio.sleep(.6)
        release.set()
        await asyncio.gather(running, return_exceptions=True)
        with store._connect() as conn:
            jobs = conn.execute("SELECT payload FROM beep_jobs WHERE kind='recording_stop' AND session_id=%s", (sid,)).fetchall()
        assert any(r['payload'].get('egress_id') == 'EG-late' for r in jobs)
        assert store.get_session('tenant', sid)['recording_status'] != 'recording'


async def test_stop_failure_publishes_failed_and_keeps_exact_identity(database):
    store = Store(database)
    store.initialize()
    state = active_session(store)
    sid = state['id']
    store.enqueue_job('tenant', sid, 'recording_start', {'consent_epoch': 0})

    class Egress:
        async def start(self, state):
            return 'EG-stop'

        async def reconcile_stop(self, state, reservation):
            assert state['egress_id'] == 'EG-stop'
            assert store.get_session('tenant', sid)['recording_status'] == 'stopping'
            raise ConnectionError('synthetic unconfirmed storage')

    async with DurableWorker(settings().model_copy(update={'database_url': database}),
                             store=store, recording=Egress()) as owner:
        await owner.run_once()
        store.control('tenant', sid, 'client', 'pause')
        await owner.run_once()
        current = store.get_session('tenant', sid)
        assert current['recording_status'] == 'failed'
        assert current['egress_id'] == 'EG-stop'
