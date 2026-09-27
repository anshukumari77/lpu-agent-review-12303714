"""Final runtime/core contract regressions against real isolated PostgreSQL."""
from datetime import datetime, timezone, timedelta

import pytest
from psycopg.types.json import Jsonb

from beep_agent.store import Store
from test_worker import active_session, database as worker_database


@pytest.fixture
def database():
    yield from worker_database.__wrapped__()


def test_successful_monitor_refresh_and_stale_admission_gate(database):
    store = Store(database)
    store.initialize()
    try:
        state = active_session(store)
        sid = state['id']
        store.set_recording('tenant', sid, 'recording', 'EG-fresh')
        store.enqueue_job('tenant', sid, 'discovery', {})
        job = store.claim_job('owner')
        with store._connect() as conn:
            conn.execute("UPDATE beep_sessions SET data=jsonb_set(data,'{recording_verified_at}',%s) WHERE id=%s",
                (Jsonb((datetime.now(timezone.utc)-timedelta(seconds=6)).isoformat()), sid))
        assert not store.admit_inference(job['id'], job['lease_token'], 'tenant', sid, 0)
        assert store.observe_recording('tenant', sid, 'recording', 'EG-fresh', 0)
        assert store.admit_inference(job['id'], job['lease_token'], 'tenant', sid, 0)
        assert not store.observe_recording('tenant', sid, 'failed', 'EG-wrong', 0)
        assert store.get_session('tenant', sid)['status'] == 'active'
    finally:
        store.close()


@pytest.mark.parametrize('terminal', ['EGRESS_FAILED', 'EGRESS_ABORTED'])
async def test_terminal_failed_recording_cleanup_does_not_claim_file(terminal):
    from livekit import api
    from beep_agent.recording import RecordingService
    from test_recording import provider_http, protobuf, recording_settings, synthetic_session
    session = synthetic_session()
    reservation = dict(id='reserve1', consent_epoch=0, room_name=session['room_name'],
                       output_prefix='beep/tenant-1/session-1/epoch-0-reserve1')
    def responder(method, path, body):
        assert method == 'POST' and path.endswith('ListEgress')
        return protobuf(api.ListEgressResponse(items=[api.EgressInfo(
            egress_id='EG-failed', room_name=session['room_name'], status=getattr(api, terminal),
            room_composite=api.RoomCompositeEgressRequest(file_outputs=[api.EncodedFileOutput(
                filepath=reservation['output_prefix']+'.mp4', s3=api.S3Upload(bucket='synthetic-private'))]))]))
    with provider_http(responder) as (url, calls):
        result = await RecordingService(recording_settings(url)).reconcile_stop(session, reservation)
        assert result == ['EG-failed']
        assert result.failed_ids == ['EG-failed']
        assert len(calls) >= 2
