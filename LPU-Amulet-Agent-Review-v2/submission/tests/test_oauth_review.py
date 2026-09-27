"""Independent OAuth boundary review: synthetic stores/HTTP/subprocesses only.

Regression assertions describe required behavior; a failure is review evidence.
Never launch the real Codex executable or inspect an existing credential store.
"""
import asyncio
from contextlib import suppress
import json
import os
import signal
import sys
from types import SimpleNamespace

import httpx
from pydantic import BaseModel
import pytest

from beep_agent import codex_oauth, codex_rpc
from test_codex_oauth import Answer, FakeRPC, response_events, synthetic_store


@pytest.fixture
def review_adapter(tmp_path):
    synthetic_store(tmp_path)
    settings = SimpleNamespace(codex_home=tmp_path, codex_executable="codex", codex_model="selected-synthetic-model")
    rpc = FakeRPC(tmp_path)

    def make(handler, **kwargs):
        return codex_oauth.CodexInference(
            settings, rpc_factory=lambda *a, **k: rpc,
            http_transport=httpx.MockTransport(handler), **kwargs,
        )

    return settings, rpc, make


@pytest.mark.parametrize("code", [{"private": "synthetic-private"}, ["synthetic-private"]])
async def test_malformed_http_error_code_remains_allowlisted(review_adapter, code):
    _, rpc, make = review_adapter
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(400, json={"error": {"code": code, "message": "synthetic-private"}})

    with pytest.raises(codex_oauth.CodexError, match="^codex_request_rejected$"):
        await make(handler).structured(Answer, "trusted", [])
    assert len(requests) == 1 and rpc.closed


@pytest.mark.parametrize("text", ['{"answer":"ok","unexpected":"ignored"}', '{}'])
async def test_local_output_enforces_requested_strict_schema(review_adapter, text):
    _, _, make = review_adapter

    class DefaultAnswer(BaseModel):
        answer: str = "invented-default"

    def handler(request):
        schema = json.loads(request.content)["text"]["format"]["schema"]
        assert schema["additionalProperties"] is False
        assert schema["required"] == ["answer"]
        output = [{"id": "msg_synthetic", "type": "message", "status": "completed",
                   "role": "assistant", "content": [{"type": "output_text", "text": text, "annotations": []}]}]
        return httpx.Response(200, content=response_events(output=output))

    with pytest.raises(codex_oauth.CodexError, match="^codex_invalid_output$"):
        await make(handler).structured(DefaultAnswer, "trusted", [])


async def test_cancellation_during_rpc_shutdown_reaps_child(tmp_path, monkeypatch):
    script = tmp_path / "synthetic_broker.py"
    script.write_text('''import json, signal, sys
signal.signal(signal.SIGTERM, signal.SIG_IGN)
for line in sys.stdin:
    message = json.loads(line)
    if "id" in message:
        result = {} if message["method"] == "initialize" else {"account": {"type": "chatgpt"}}
        print(json.dumps({"id": message["id"], "result": result}), flush=True)
''')
    client = codex_rpc.CodexRPC(tmp_path, command=[sys.executable, str(script)])
    terminating = asyncio.Event()
    killpg = os.killpg

    def observe_term(pid, sig):
        result = killpg(pid, sig)
        if sig == signal.SIGTERM:
            terminating.set()
        return result

    monkeypatch.setattr(codex_rpc.os, "killpg", observe_term)

    async def run():
        async with client:
            await client.request("account/read", {"refreshToken": False})

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(terminating.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert client.process.returncode is not None, "RPC child survives cancellation during __aexit__"
        assert client._pump_task.done()
    finally:
        # The deliberately failing review must never leave its synthetic child.
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if client.process is not None and client.process.returncode is None:
            with suppress(ProcessLookupError):
                killpg(client.process.pid, signal.SIGKILL)
            await client.process.wait()
        await client.aclose()


async def test_cancelled_lock_waiter_and_holder_release_lock(tmp_path):
    home_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    holding, waiting = asyncio.Event(), asyncio.Event()

    async def holder():
        async with codex_oauth._refresh_lock(home_fd):
            holding.set()
            await asyncio.Event().wait()

    async def waiter():
        waiting.set()
        async with codex_oauth._refresh_lock(home_fd):
            pytest.fail("waiter should not enter an occupied lock")

    first = asyncio.create_task(holder())
    second = None
    try:
        await asyncio.wait_for(holding.wait(), 1)
        second = asyncio.create_task(waiter())
        await asyncio.wait_for(waiting.wait(), 1)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        async with asyncio.timeout(1):
            async with codex_oauth._refresh_lock(home_fd):
                pass
    finally:
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        os.close(home_fd)


@pytest.mark.parametrize("failure", ["eof", "read_error", "connect_error", "oversize_error", "encoding"])
async def test_transport_failure_closes_without_retry_or_partial_result(review_adapter, failure):
    _, rpc, make = review_adapter
    requests = []

    class Wire(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield response_events().split(b"\n\n", 1)[0] + b"\n\n"
            if failure == "read_error":
                raise httpx.ReadError("synthetic-private-wire-error")

        async def aclose(self):
            self.closed = True

    wire = Wire()

    def handler(request):
        requests.append(request)
        if failure == "connect_error":
            raise httpx.ConnectError("synthetic-private-connect-error")
        if failure == "oversize_error":
            return httpx.Response(400, content=b"x" * (codex_oauth.MAX_WIRE_BYTES + 1))
        if failure == "encoding":
            return httpx.Response(200, stream=wire, headers={"content-encoding": "gzip"})
        return httpx.Response(200, stream=wire)

    with pytest.raises(codex_oauth.CodexError, match="^codex_") as exc:
        await make(handler).structured(Answer, "trusted", [])
    assert "synthetic-private" not in str(exc.value)
    assert exc.value.__suppress_context__
    assert len(requests) == 1 and rpc.closed
    if failure in {"eof", "read_error", "encoding"}:
        assert wire.closed


async def test_selected_model_and_only_allowlisted_rpc_methods(review_adapter):
    settings, rpc, make = review_adapter

    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == settings.codex_model
        assert body["tools"] == [] and body["store"] is False
        assert not {"previous_response_id", "conversation", "background"} & body.keys()
        assert str(request.url) == codex_oauth.CODEX_BASE_URL + "/responses"
        return httpx.Response(200, content=response_events())

    await make(handler).structured(Answer, "trusted", [])
    assert rpc.calls == [("account/read", {"refreshToken": False})]
