"""Actual installed AgentSession fatal transport failure, without RTC or model IO."""
import asyncio
import time
from types import SimpleNamespace

import aiohttp
import pytest
from livekit.agents import AgentSession

from beep_agent.realtime import InterviewRuntime, assemble_native
from test_realtime import settings, state


async def test_fatal_sdk_error_without_close_mutes_and_counts_retry(monkeypatch):
    release = asyncio.Event()
    started = asyncio.Event()
    assemblies = []

    class HTTP:
        async def ws_connect(self, *args, **kwargs):
            await release.wait()
            raise aiohttp.ClientConnectionError('synthetic disconnected transport')

    class Store:
        def load_snapshot(self, *args):
            return dict(claims=[], unknowns=[], evidence=[], steps=[], probe=None)

    original = AgentSession.start
    async def local_start(self, agent, **kwargs):
        result = await original(self, agent, record=False, session_host=kwargs.get('session_host', False))
        started.set()
        return result

    monkeypatch.setattr(AgentSession, 'start', local_start)
    def factory(cfg, current, **kwargs):
        assembly = assemble_native(cfg, current, http_session=HTTP())
        assemblies.append(assembly)
        return assembly

    runtime = InterviewRuntime(settings(), Store(), 't', 's', SimpleNamespace(), assembly_factory=factory)
    runtime.fence.update(state(), time.monotonic())
    ticket = runtime.fence.ticket
    task = asyncio.create_task(runtime._generation(ticket))
    await asyncio.wait_for(started.wait(), 3)
    release.set()
    await asyncio.sleep(.15)
    try:
        assert not runtime.fence.valid(ticket, time.monotonic()), 'Fatal error must fence without close'
        assert not assemblies[0].session.input.audio_enabled
        assert runtime.failures == 1 and runtime.next_attempt > time.monotonic()
        with pytest.raises(Exception):
            await asyncio.wait_for(asyncio.shield(task), 3)
        runtime.model_task = task
        # Poll's independent accounting must see a failed task, not wait for close.
        assert task.done() and task.exception() is not None
        assert runtime.failures == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_financial_budget_capability_is_explicitly_unavailable():
    from beep_agent import telemetry
    capability = telemetry.budget_capability(60)
    assert capability['monetary_enforcement'] == 'UNAVAILABLE'
    assert capability['configured_budget_aud'] == 60
    assert capability['effective_hard_budget_aud'] is None


def test_readback_is_not_acknowledged_before_delivered_speech():
    from datetime import datetime, timezone, timedelta
    from beep_agent.realtime import DirectivePolicy
    policy = DirectivePolicy(last_activity=0, last_spoken=0)
    current = state(started_at=(datetime.now(timezone.utc)-timedelta(seconds=5300)).isoformat())
    assert policy.choose(dict(steps=[], unknowns=[]), current, time.monotonic(), idle=True)
    assert policy.readback_sent is False


def test_recording_freshness_fails_closed_on_missing_or_stale_identity():
    from datetime import datetime, timezone, timedelta
    from beep_agent.realtime import recording_is_fresh
    now = datetime.now(timezone.utc)
    assert not recording_is_fresh(state())
    assert not recording_is_fresh(state(egress_id='EG', recording_verified_at=(now-timedelta(seconds=6)).isoformat()))
    assert recording_is_fresh(state(egress_id='EG', recording_verified_at=now.isoformat()))
