"""Bounded, content-free native acceptance diagnostics. Never business evidence.

No logging, exception formatting, frame locals, source lines or provider bodies.
Only fixed labels and bounded integers leave this module, apart from the caller's
owned artifact path. This module never constructs a provider or reads settings.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import json
import os
from pathlib import Path
import sysconfig


MAX_FAILURES = 8
MAX_FRAMES = 8
MAX_TRACEBACK_SCAN = 64
MAX_ARTIFACT_BYTES = 65536
MAX_EVENTS = 64
MAX_ERRORS = 16
MAX_IDS = 64  # per event/item/response namespace; digests retained only in memory
MAX_ID_CHARS = 128
MAX_COUNTER = 65535
SERVER_TYPES = frozenset(
    {
        "error",
        "session.created",
        "session.updated",
        "conversation.created",
        "conversation.item.created",
        "conversation.item.added",
        "conversation.item.done",
        "conversation.item.deleted",
        "conversation.item.truncated",
        "conversation.item.retrieved",
        "conversation.item.input_audio_transcription.delta",
        "conversation.item.input_audio_transcription.completed",
        "conversation.item.input_audio_transcription.failed",
        "input_audio_buffer.committed",
        "input_audio_buffer.cleared",
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
        "response.created",
        "response.done",
        "response.output_item.added",
        "response.output_item.done",
        "response.content_part.added",
        "response.content_part.done",
        "response.output_text.delta",
        "response.output_text.done",
        "response.output_audio.delta",
        "response.output_audio.done",
        "response.output_audio_transcript.delta",
        "response.output_audio_transcript.done",
        "response.audio.delta",
        "response.audio.done",
        "response.audio_transcript.delta",
        "response.audio_transcript.done",
        "response.text.delta",
        "response.text.done",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "rate_limits.updated",
    }
)
CLIENT_TYPES = frozenset(
    {
        "session.update",
        "input_audio_buffer.append",
        "input_audio_buffer.commit",
        "input_audio_buffer.clear",
        "conversation.item.create",
        "conversation.item.delete",
        "conversation.item.retrieve",
        "conversation.item.truncate",
        "response.create",
        "response.cancel",
        "output_audio_buffer.clear",
    }
)
COUNT_ONLY = frozenset(
    {name for name in SERVER_TYPES if name.endswith(".delta")} | {"input_audio_buffer.append"}
)
ERROR_TYPES = frozenset(
    {
        "invalid_request_error",
        "server_error",
        "authentication_error",
        "permission_error",
        "rate_limit_error",
        "tokens",
        "invalid_request",
    }
)
ERROR_CODES = frozenset(
    {
        "invalid_value",
        "invalid_type",
        "invalid_json",
        "invalid_event",
        "missing_required_parameter",
        "unknown_parameter",
        "unsupported_value",
        "server_error",
        "rate_limit_exceeded",
        "insufficient_quota",
        "invalid_api_key",
        "account_deactivated",
        "billing_hard_limit_reached",
        "conversation_already_has_active_response",
        "response_cancel_not_active",
        "item_not_found",
        "invalid_previous_item_id",
        "input_audio_buffer_commit_empty",
        "audio_unintelligible",
        "context_length_exceeded",
    }
)
ERROR_PARAMS = frozenset(
    {
        "event_id",
        "item_id",
        "previous_item_id",
        "item",
        "item.type",
        "item.role",
        "item.content",
        "item.content[0].type",
        "item.content[0].image_url",
        "item.content[0].text",
        "response",
        "response.modalities",
        "response.output_modalities",
        "session",
        "session.type",
        "session.modalities",
        "session.audio",
        "session.audio.input",
        "session.audio.output",
        "session.tools",
        "tools",
        "model",
        "audio",
        "content_index",
        "audio_end_ms",
    }
)
STATUSES = frozenset({"in_progress", "completed", "cancelled", "incomplete", "failed"})
ITEM_TYPES = frozenset({"message", "function_call", "function_call_output"})
ROLES = frozenset({"system", "developer", "user", "assistant"})
STAGES = frozenset({"prepare", "voice", "finish", "cleanup", "export", "native_task"})
EXCEPTION_TYPES = frozenset(
    {
        "Exception",
        "RuntimeError",
        "ValueError",
        "TypeError",
        "AttributeError",
        "KeyError",
        "IndexError",
        "AssertionError",
        "PermissionError",
        "OSError",
        "ConnectionError",
        "ConnectionResetError",
        "BrokenPipeError",
        "TimeoutError",
        "CancelledError",
        "Rejected",
        "APIError",
        "APIConnectionError",
        "APIStatusError",
        "APITimeoutError",
        "RealtimeError",
        "ClientError",
        "ClientConnectionError",
        "ClientConnectorError",
        "ServerDisconnectedError",
        "WSServerHandshakeError",
        "ClientConnectionResetError",
        "ServerTimeoutError",
    }
)
# Exact known source paths, not arbitrary basename/module/function names.
_ROOT = Path(__file__).resolve().parents[2]
_SDK = Path(sysconfig.get_path("purelib"))
_SOURCE_PATHS = {
    str(_ROOT / "scripts/verify_live_session.py"): "scripts/verify_live_session.py",
    **{
        str(_ROOT / "src/beep_agent" / name): "src/beep_agent/" + name
        for name in (
            "realtime.py",
            "native_diagnostics.py",
            "media.py",
            "worker.py",
            "store.py",
        )
    },
    **{
        str(_SDK / name): name
        for name in (
            "livekit/plugins/openai/realtime/realtime_model.py",
            "livekit/agents/voice/agent.py",
            "livekit/agents/voice/agent_session.py",
            "livekit/agents/voice/agent_activity.py",
            "livekit/agents/llm/realtime.py",
            "livekit/agents/utils/aio/channel.py",
            "livekit/agents/utils/log.py",
            "livekit/rtc/event_emitter.py",
            "aiohttp/client.py",
            "aiohttp/connector.py",
            "aiohttp/client_ws.py",
            "aiohttp/streams.py",
        )
    },
}


def _label(value, allowed):
    # No coercion, custom hashing, repr/str or substring redaction.
    return value if type(value) is str and len(value) <= 128 and value in allowed else "other"


class NativeDiagnostics:
    def __init__(self):
        self._failures = []
        self._failures_dropped = 0
        self._events = []
        self._errors = []
        self._counts = {"server": {}, "client": {}}
        self._ids = {"event": {}, "item": {}, "response": {}}
        self._sequence = 0
        self._dropped = 0
        self._formatter_errors = 0
        self.observer = {
            "attached": False,
            "stopped": False,
            "polls": 0,
            "detach_failed": False,
            "coverage": "best_effort_no_replay",
        }

    def _handle(self, kind, value):
        if type(value) is not str or not 0 < len(value) <= MAX_ID_CHARS or not value.isascii():
            return "other"
        # Even the bounded in-memory correlation table retains no raw IDs.
        digest = hashlib.sha256(value.encode("utf-8")).digest()
        table = self._ids[kind]
        if digest not in table:
            if len(table) >= MAX_IDS:
                return "other"
            table[digest] = kind + "-" + str(len(table) + 1)
        return table[digest]

    def server_event(self, event=None):
        self._safe_event("server", event)

    def client_event(self, event=None):
        self._safe_event("client", event)

    def _safe_event(self, direction, event):
        try:
            self._record_event(direction, event)
        except BaseException:
            # EventEmitter propagates TypeError; no formatter may reach that path.
            self._formatter_errors = min(MAX_COUNTER, self._formatter_errors + 1)

    def _record_event(self, direction, event):
        event = event if type(event) is dict else {}
        kind = _label(event.get("type"), SERVER_TYPES if direction == "server" else CLIENT_TYPES)
        counts = self._counts[direction]
        counts[kind] = min(MAX_COUNTER, counts.get(kind, 0) + 1)
        self._sequence = min(MAX_COUNTER, self._sequence + 1)
        if kind in COUNT_ONLY:
            return  # No ID map growth, byte/length inspection or per-delta sample.
        response = event.get("response") if kind in {"response.created", "response.done"} else None
        response = response if type(response) is dict else {}
        details = response.get("status_details")
        details = details if type(details) is dict else {}
        error = (
            event.get("error")
            if kind in {"error", "conversation.item.input_audio_transcription.failed"}
            else details.get("error")
        )
        is_error = kind in {"error", "conversation.item.input_audio_transcription.failed"} or bool(
            details
        )
        target, limit = (self._errors, MAX_ERRORS) if is_error else (self._events, MAX_EVENTS)
        if len(target) >= limit:
            self._dropped = min(MAX_COUNTER, self._dropped + 1)
            return
        record = {"sequence": self._sequence, "direction": direction, "type": kind}
        if kind != "other":
            if "event_id" in event:
                record["event"] = self._handle("event", event["event_id"])
            item = event.get("item")
            item = item if type(item) is dict else {}
            if "item_id" in event or "id" in item:
                record["item"] = self._handle("item", event.get("item_id", item.get("id")))
            if "previous_item_id" in event:
                record["previous_item"] = self._handle("item", event["previous_item_id"])
            if item:
                record["item_type"] = _label(item.get("type"), ITEM_TYPES)
                record["role"] = _label(item.get("role"), ROLES)
            if "response_id" in event or "id" in response:
                record["response"] = self._handle(
                    "response", event.get("response_id", response.get("id"))
                )
            if response:
                record["status"] = _label(response.get("status"), STATUSES)
            if is_error:
                error = error if type(error) is dict else {}
                record["error"] = {
                    "type": _label(error.get("type"), ERROR_TYPES),
                    "code": _label(error.get("code"), ERROR_CODES),
                    "param": _label(error.get("param"), ERROR_PARAMS),
                }
                if "event_id" in error:
                    record["error"]["event"] = self._handle("event", error["event_id"])
        target.append(record)

    def failure(self, stage, exc):
        """Consume only the primary exception type and trusted line references."""
        try:
            if len(self._failures) >= MAX_FAILURES:
                self._failures_dropped = min(65535, self._failures_dropped + 1)
                return
            frames = []
            tb = BaseException.__getattribute__(exc, "__traceback__")
            for _ in range(MAX_TRACEBACK_SCAN):
                if tb is None:
                    break
                source = _SOURCE_PATHS.get(tb.tb_frame.f_code.co_filename)
                if source is not None and type(tb.tb_lineno) is int and 0 < tb.tb_lineno < 10**7:
                    frames.append({"source": source, "line": tb.tb_lineno})
                    frames = frames[-MAX_FRAMES:]
                tb = tb.tb_next
            name = type.__getattribute__(type(exc), "__name__")
            self._failures.append(
                {
                    "stage": _label(stage, STAGES),
                    "type": _label(name, EXCEPTION_TYPES),
                    "frames": frames,
                }
            )
        except BaseException:
            # A diagnostics formatter must not replace the original outcome.
            pass

    def snapshot(self):
        return {
            "schema_version": 1,
            "purpose": "native_diagnostics_not_business_evidence",
            "failures": self._failures,
            "failures_dropped": self._failures_dropped,
            "events": self._events,
            "errors": self._errors,
            "counts": self._counts,
            "samples_dropped": self._dropped,
            "formatter_errors": self._formatter_errors,
            "id_handles": {kind: len(ids) for kind, ids in self._ids.items()},
            "observer": self.observer,
            "native_close_elapsed": "not_measured_by_diagnostics",
        }

    def export(self, backend, result):
        """Best effort, independent of business export; never changes acceptance."""
        summary = {
            "purpose": "native_diagnostics_not_business_evidence",
            "failure_count": len(self._failures),
            "failures": self._failures,
            "observer": dict(self.observer),
            "counts": self._counts,
            "error_count": len(self._errors),
            "samples_dropped": self._dropped,
            "exported": False,
        }
        result["native_diagnostics"] = summary
        temporary = None
        try:
            directory = getattr(backend, "directory", None)
            if not getattr(backend, "owns_directory", False) or not isinstance(directory, Path):
                summary["export_status"] = "directory_unavailable"
                return
            data = (
                json.dumps(
                    self.snapshot(), ensure_ascii=True, allow_nan=False, separators=(",", ":")
                )
                + "\n"
            ).encode("ascii")
            if len(data) > MAX_ARTIFACT_BYTES:
                summary["export_status"] = "size_limit"
                return
            path = directory / "native-diagnostics.json"
            candidate = directory / "native-diagnostics.json.tmp"
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            temporary = candidate  # Only unlink a temporary this invocation created.
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(temporary, path)
            temporary = None
            summary.update(exported=True, export_status="written", bytes=len(data))
            result.setdefault("artifacts", {})["native_diagnostics"] = str(path)
        except BaseException:
            summary["export_status"] = "write_failed"
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except BaseException:
                    pass


POLL_SECONDS = 0.005
MAX_POLLS = 12000
MAX_OBSERVER_SECONDS = 60.0
CALLBACKS = ("openai_server_event_received", "openai_client_event_queued")


class NativeObserver:
    """One passive attachment; no model/session/request/reply construction.

    Synchronous close removes listeners before any await, including when the
    observer task has not started. No join is added to the socket-close path.
    """

    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        self.task = None
        self._closed = False
        self._listeners = []
        self._native_task = None
        self._native_task_seen = False

    def start(self, runtime, seconds):
        if self.task is not None or self._closed:
            return
        coroutine = None
        try:
            budget = (
                min(seconds, MAX_OBSERVER_SECONDS)
                if type(seconds) in (int, float) and math.isfinite(seconds) and seconds > 0
                else 0
            )
            deadline = asyncio.get_running_loop().time() + budget
            coroutine = self._watch(runtime, deadline)
            self.task = asyncio.create_task(coroutine, name="acceptance.native-diagnostics")
            self.task.add_done_callback(self._finished)
        except BaseException:
            if self.task is None and coroutine is not None:
                coroutine.close()
            self.close()

    def _finished(self, task):
        self.close()

    def _task_finished(self, task):
        try:
            if not self._native_task_seen and task.done():
                self._native_task_seen = True
                if not task.cancelled() and (exc := task.exception()) is not None:
                    self.diagnostics.failure("native_task", exc)
        except BaseException:
            pass

    def _callback(self, direction):
        def observed(event=None):
            if self._closed:
                return
            try:
                self.diagnostics._safe_event(direction, event)
            except BaseException:
                pass

        return observed

    async def _watch(self, runtime, deadline):
        try:
            loop = asyncio.get_running_loop()
            for _ in range(MAX_POLLS):
                if self._closed or loop.time() >= deadline:
                    break
                self.diagnostics.observer["polls"] += 1
                if self._native_task is None:
                    task = getattr(runtime, "model_task", None)
                    if isinstance(task, asyncio.Task):
                        self._native_task = task
                        task.add_done_callback(self._task_finished)
                try:
                    assembly = runtime.assembly
                    native = assembly.agent.realtime_llm_session if assembly is not None else None
                except (AttributeError, RuntimeError):
                    native = None  # Agent's documented property raises before activity start.
                if native is not None:
                    for event, direction in zip(CALLBACKS, ("server", "client")):
                        callback = self._callback(direction)
                        self._listeners.append((native, event, callback))  # own a partial on()
                        native.on(event, callback)
                    self.diagnostics.observer["attached"] = True
                    await asyncio.sleep(max(0, deadline - loop.time()))
                    break  # Never follow a replacement/reconnected session.
                await asyncio.sleep(min(POLL_SECONDS, max(0, deadline - loop.time())))
        except BaseException:
            pass
        finally:
            self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True  # Queued/failed-to-remove callbacks become inert first.
        self.diagnostics.observer["stopped"] = True
        for native, event, callback in self._listeners:
            try:
                native.off(event, callback)
            except BaseException:
                self.diagnostics.observer["detach_failed"] = True
        self._listeners.clear()
        if self._native_task is not None:
            self._task_finished(self._native_task)
            self._native_task.remove_done_callback(self._task_finished)
            self._native_task = None
        if self.task is not None and not self.task.done():
            self.task.cancel()
