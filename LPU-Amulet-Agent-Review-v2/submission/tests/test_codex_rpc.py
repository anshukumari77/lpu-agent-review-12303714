"""Synthetic subprocess protocol fixtures, not live OAuth/model acceptance."""
import asyncio
from contextlib import suppress
import json
import os
import signal
import sys

import pytest

from beep_agent import codex_rpc


@pytest.fixture
async def stubborn_broker(tmp_path, monkeypatch):
    script = tmp_path / "stubborn_broker.py"
    script.write_text('''import json, signal, sys
signal.signal(signal.SIGTERM, signal.SIG_IGN)
for line in sys.stdin:
    message = json.loads(line)
    if "id" in message:
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
''')
    client = codex_rpc.CodexRPC(tmp_path, command=[sys.executable, str(script)])
    terminating = asyncio.Event()
    signals = []
    killpg = os.killpg

    def observe_signal(pid, sig):
        result = killpg(pid, sig)
        signals.append(sig)
        if sig == signal.SIGTERM:
            terminating.set()
        return result

    monkeypatch.setattr(codex_rpc.os, "killpg", observe_signal)
    try:
        await client.__aenter__()
        yield client, terminating, signals
    finally:
        # Failed regressions must not leak their deliberately stubborn child.
        if client.process is not None and client.process.returncode is None:
            with suppress(ProcessLookupError):
                killpg(client.process.pid, signal.SIGKILL)
            await client.process.wait()
        await client.aclose()


@pytest.mark.asyncio
async def test_repeated_shutdown_cancellation_waits_for_reaping(stubborn_broker):
    client, terminating, signals = stubborn_broker
    task = asyncio.create_task(client.aclose())
    try:
        await asyncio.wait_for(terminating.wait(), 1)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert task.cancelled()
        assert client.process.returncode == -signal.SIGKILL
        assert client._pump_task.done()
        assert signals == [signal.SIGTERM, signal.SIGKILL]
        with pytest.raises(ProcessLookupError):
            os.kill(client.process.pid, 0)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_concurrent_close_is_idempotent_and_cancellation_is_local(stubborn_broker):
    client, terminating, signals = stubborn_broker
    first = asyncio.create_task(client.aclose())
    tasks = [first]
    try:
        await asyncio.wait_for(terminating.wait(), 1)
        second = asyncio.create_task(client.aclose())
        tasks.append(second)
        await asyncio.sleep(0)
        first.cancel()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1] is None
        assert client.process.returncode == -signal.SIGKILL
        assert client._pump_task.done()
        await client.aclose()
        assert signals == [signal.SIGTERM, signal.SIGKILL]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_managed_account_roundtrip_has_no_inherited_api_key(tmp_path, monkeypatch):
    from beep_agent import codex_rpc

    script = tmp_path / "server.py"
    script.write_text('''import json, os, sys
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    assert "OPENAI_API_KEY" not in os.environ
    assert "ANTHROPIC_API_KEY" not in os.environ
    result = {"userAgent": "synthetic-codex"} if msg["method"] == "initialize" else {"account": {"type": "chatgpt", "planType": "plus"}, "requiresOpenaiAuth": True}
    print(json.dumps({"id": msg["id"], "result": result}), flush=True)
''')
    monkeypatch.setenv("OPENAI_API_KEY", "test-inherited-key-must-not-cross")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-other-provider-key")
    async with codex_rpc.CodexRPC(tmp_path, command=[sys.executable, str(script)]) as client:
        result = await client.request("account/read", {"refreshToken": False})
        pid = client.process.pid
        assert result["account"]["type"] == "chatgpt"
    assert pid > 0
    assert client.process.returncode is not None
    assert "test-inherited" not in json.dumps(result)


@pytest.mark.asyncio
async def test_cancelled_start_propagates_and_reaps_child(tmp_path):
    import asyncio
    from beep_agent.codex_rpc import CodexRPC

    script = tmp_path / "stalled.py"
    script.write_text("import time; time.sleep(30)\n")
    client = CodexRPC(tmp_path, command=[sys.executable, str(script)])
    task = asyncio.create_task(client.__aenter__())
    for _ in range(100):
        if client.process is not None:
            break
        await asyncio.sleep(.01)
    assert client.process is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.process.returncode is not None
