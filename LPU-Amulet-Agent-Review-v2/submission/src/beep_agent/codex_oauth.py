"""Opt-in ChatGPT subscription inference through official Codex managed OAuth.

The app-server owns managed refresh. The explicitly selected file store supplies
only the access token and account ID to the official SDK, in memory. No copies.
Account metadata is not proof of a successful inference or voice entitlement.
"""
import asyncio
import base64
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tomllib
from uuid import uuid4

import httpx
from pydantic import BaseModel, ValidationError
from openai import APIStatusError, AsyncOpenAI

from .codex_rpc import CodexError, CodexRPC


CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
MAX_AUTH_BYTES = 65_536
MAX_INPUT_BYTES = 4_000_000
MAX_WIRE_BYTES = 2_000_000
MAX_OUTPUT_BYTES = 131_072
CALL_TIMEOUT_SECONDS = 40
WORK_TIMEOUT_SECONDS = 36  # Reserve four seconds for cancellation/transport/RPC cleanup.


class _Rejected(Exception):
    """Internal fixed failure codes, never upstream data."""


@contextmanager
def _selected_home(settings):
    if not settings.codex_home:
        raise _Rejected("codex_not_configured")
    home = Path(settings.codex_home).expanduser()
    if not home.is_absolute() or ".." in home.parts:
        raise _Rejected("codex_unsafe_auth_store")
    # Do not canonicalize away a symlink before validating it.
    if any(p.is_symlink() for p in (home, *home.parents)):
        raise _Rejected("codex_unsafe_auth_store")
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise _Rejected("codex_unsafe_auth_store")
        yield home, fd
    finally:
        os.close(fd)


def _read_file(directory_fd, name, *, secret=True):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                 dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        forbidden = 0o177 if secret else 0o022
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & forbidden or info.st_nlink != 1
                or info.st_size > MAX_AUTH_BYTES):
            raise _Rejected("codex_unsafe_auth_store")
        with os.fdopen(fd, "rb", closefd=False) as file:
            data = file.read(MAX_AUTH_BYTES + 1)
        after = os.fstat(fd)
        if len(data) > MAX_AUTH_BYTES or (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise _Rejected("codex_unsafe_auth_store")
        return data
    finally:
        os.close(fd)


def _check_file_store(directory_fd):
    try:
        config = tomllib.loads(_read_file(directory_fd, "config.toml", secret=False).decode())
    except FileNotFoundError:
        config = {}
    if config.get("cli_auth_credentials_store", "file") != "file":
        raise _Rejected("codex_file_store_required_keychain_unsupported")


def _load_auth(directory_fd):
    try:
        data = json.loads(_read_file(directory_fd, "auth.json"))
    except FileNotFoundError:
        raise _Rejected("codex_file_store_required_keychain_unsupported") from None
    if (data.get("auth_mode") not in (None, "chatgpt")
            or data.get("OPENAI_API_KEY") is not None):
        raise _Rejected("codex_managed_chatgpt_required")
    tokens = data["tokens"]
    token, account_id = tokens["access_token"], tokens["account_id"]
    if (not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,16384}", token)
            or not isinstance(account_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", account_id)):
        raise _Rejected("codex_invalid_auth_store")
    return token, account_id, _refresh_due(token, data.get("last_refresh"))


def _refresh_due(token, last_refresh):
    # Scheduling hint only, NOT JWT signature verification or authentication.
    # Match the pinned official managed-auth policy (5 minutes / fallback 8 days).
    now = datetime.now(timezone.utc)
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        expiry = claims.get("exp")
        if isinstance(expiry, (int, float)) and not isinstance(expiry, bool):
            return expiry <= now.timestamp() + 300
    except (ValueError, IndexError, AttributeError):
        pass
    if isinstance(last_refresh, str):
        refreshed = datetime.fromisoformat(last_refresh.replace("Z", "+00:00"))
        if refreshed.tzinfo is not None:
            return (now - refreshed).total_seconds() > 8 * 86400
    raise _Rejected("codex_invalid_auth_store")


@asynccontextmanager
async def _refresh_lock(directory_fd):
    # Codex's pinned source has an in-process Semaphore, not an OS file lock.
    # Keep this empty inode: unlinking it would break cross-process exclusion.
    fd = os.open(".beep-oauth.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                 | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size):
            raise _Rejected("codex_unsafe_auth_store")
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.025)
        yield
    finally:
        os.close(fd)  # Also releases flock, including on cancellation.


class _CodexSDK(AsyncOpenAI):
    @property
    def default_headers(self):
        # SDK 2.54 also reads OPENAI_CUSTOM_HEADERS. Do not let those, organization,
        # project, or SDK telemetry defaults become request/logging data.
        return {"Accept": "text/event-stream", "Content-Type": "application/json",
                **self.auth_headers}


def _http_error_code(error):
    status = error.status_code
    if status == 401:
        return "codex_auth_rejected"
    if status == 403:
        return "codex_access_denied"
    if status == 429:
        return "codex_rate_limited"
    if 300 <= status < 400:
        return "codex_redirect_rejected"
    if status >= 500:
        return "codex_service_unavailable"
    # Compare fixed protocol codes only; never return an upstream message/body.
    if isinstance(error.code, str) and error.code in {"model_not_found", "unsupported_model", "model_not_supported"}:
        return "codex_model_unsupported"
    return "codex_request_rejected"


class _LimitedStream(httpx.AsyncByteStream):
    def __init__(self, response):
        self.response = response

    async def __aiter__(self):
        size = 0
        async for chunk in self.response.stream:
            size += len(chunk)
            if size > MAX_WIRE_BYTES:
                raise _Rejected("codex_output_too_large")
            yield chunk

    async def aclose(self):
        await self.response.aclose()


class _LimitedTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner):
        self.inner = inner

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            await response.aclose()
            raise _Rejected("codex_encoding_rejected")
        # Bound bytes BEFORE the SDK buffers an SSE line or error response body.
        return httpx.Response(response.status_code, headers=response.headers,
                              stream=_LimitedStream(response), extensions=response.extensions)

    async def aclose(self):
        await self.inner.aclose()


def _validate_input(schema, instructions, content):
    if not isinstance(schema, type) or not issubclass(schema, BaseModel):
        raise _Rejected("codex_input_invalid_schema")
    if not isinstance(instructions, str) or not isinstance(content, list):
        raise _Rejected("codex_input_invalid")
    for part in content:
        if not isinstance(part, dict):
            raise _Rejected("codex_input_invalid")
        kind = part.get("type")
        if kind == "input_text":
            if set(part) != {"type", "text"} or not isinstance(part["text"], str):
                raise _Rejected("codex_input_invalid")
        elif kind == "input_image":
            if (not set(part) <= {"type", "image_url", "detail"}
                    or not isinstance(part.get("image_url"), str)
                    or part.get("detail", "auto") not in {"auto", "low", "high", "original"}):
                raise _Rejected("codex_input_invalid")
        else:
            raise _Rejected("codex_input_invalid")
    if len(json.dumps([instructions, content, schema.model_json_schema()]).encode()) > MAX_INPUT_BYTES:
        raise _Rejected("codex_input_too_large")
    # Freeze caller-owned content before any auth await; it never becomes instructions.
    return json.loads(json.dumps(content))


def _check_items(items):
    for item in items:
        if item.type not in {"message", "reasoning"}:
            raise _Rejected("codex_tool_output_rejected")
        if item.type == "message":
            for part in item.content:
                if part.type == "refusal":
                    raise _Rejected("codex_refusal")
                if part.type != "output_text":
                    raise _Rejected("codex_invalid_output")


def _check_event(event):
    kind = event.type
    if "refusal" in kind:
        raise _Rejected("codex_refusal")
    if "call" in kind:
        raise _Rejected("codex_tool_output_rejected")
    if kind == "response.incomplete":
        raise _Rejected("codex_incomplete")
    if kind in {"error", "response.failed"}:
        raise _Rejected("codex_response_failed")
    if getattr(event, "item", None) is not None:
        _check_items([event.item])
    if getattr(event, "response", None) is not None:
        _check_items(event.response.output)


def _reconcile_output(response, done_items, done_texts):
    if done_items:
        if sorted(done_items) != list(range(len(done_items))):
            raise _Rejected("codex_invalid_output")
        ordered = [done_items[index] for index in sorted(done_items)]
        for item in ordered:
            if item.type == "message" and item.status != "completed":
                raise _Rejected("codex_invalid_output")
        if response.output:
            def payload(item):
                # Exclude SDK-only parsed fields BEFORE serializing: otherwise
                # Pydantic warnings could contain output data from generic models.
                return item.model_dump(mode="json", exclude_none=True,
                                       exclude={"content": {"__all__": {"parsed"}}})
            if [payload(item) for item in ordered] != [payload(item) for item in response.output]:
                raise _Rejected("codex_invalid_output")
        else:
            # Metadata/usage/ID stay exactly as returned by response.completed.
            response = response.model_copy(update={"output": ordered})
    for (output_index, content_index), text in done_texts.items():
        if not 0 <= output_index < len(response.output):
            raise _Rejected("codex_invalid_output")
        item = response.output[output_index]
        if (item.type != "message" or not 0 <= content_index < len(item.content)
                or item.content[content_index].type != "output_text"
                or item.content[content_index].text != text):
            raise _Rejected("codex_invalid_output")
    return response


def _reject_defaults(value):
    # The SDK's strict JSON Schema makes even Pydantic defaulted fields required.
    # Never silently substitute defaults for absent provider evidence.
    if isinstance(value, BaseModel):
        if set(type(value).model_fields) - value.model_fields_set:
            raise _Rejected("codex_invalid_output")
        for name in type(value).model_fields:
            _reject_defaults(getattr(value, name))
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_defaults(item)
    elif isinstance(value, dict):
        for item in value.values():
            _reject_defaults(item)


def _validated_output(response, schema):
    if response.status != "completed" or response.error or response.incomplete_details:
        raise _Rejected("codex_incomplete")
    _check_items(response.output)
    messages = [item for item in response.output if item.type == "message"]
    if (len(messages) != 1 or messages[0].status != "completed"
            or messages[0].role != "assistant" or len(messages[0].content) != 1):
        raise _Rejected("codex_invalid_output")
    text = messages[0].content[0].text
    if len(text.encode()) > MAX_OUTPUT_BYTES:
        raise _Rejected("codex_output_too_large")
    value = schema.model_validate_json(text, strict=True, extra="forbid")
    _reject_defaults(value)
    return value


def _usage(response):
    def count(value):
        return value if type(value) is int and value >= 0 else None
    raw = response.usage
    input_tokens = count(raw.input_tokens) if raw else None
    output_tokens = count(raw.output_tokens) if raw else None
    response_id = response.id if isinstance(response.id, str) and response.id else None
    return {
        "provider": "codex", "billing": "subscription", "response_id": response_id,
        "model": getattr(response, "model", None),
        "correlation_id": uuid4().hex, "id_kind": "response" if response_id else "local_correlation",
        "hard_budget_available": False, "cost_aud": None,
        "measurement": "measured" if input_tokens is not None and output_tokens is not None else "unknown",
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "total_tokens": count(raw.total_tokens) if raw else None,
        "cached_input_tokens": count(getattr(raw.input_tokens_details, "cached_tokens", None)) if raw else None,
        "reasoning_tokens": count(getattr(raw.output_tokens_details, "reasoning_tokens", None)) if raw else None,
        "source": "codex.responses.usage",
    }


class CodexInference:
    def __init__(self, settings, *, rpc_factory=None, http_transport=None, before_send=None):
        self.settings = settings
        self.rpc_factory = rpc_factory or CodexRPC
        self.http_transport = http_transport
        self.before_send = before_send

    async def structured(self, schema, instructions: str, content: list[dict]) -> tuple[BaseModel, dict]:
        try:
            async with asyncio.timeout(CALL_TIMEOUT_SECONDS):
                async with asyncio.timeout(WORK_TIMEOUT_SECONDS):
                    return await self._structured(schema, instructions, content)
        except TimeoutError:
            raise CodexError("codex_deadline_exceeded") from None
        except _Rejected as error:
            raise CodexError(str(error)) from None
        except APIStatusError as error:
            raise CodexError(_http_error_code(error)) from None
        except ValidationError:
            raise CodexError("codex_invalid_output") from None
        except Exception:
            raise CodexError("codex_inference_unavailable") from None

    async def _structured(self, schema, instructions, content):
        content = _validate_input(schema, instructions, content)
        with _selected_home(self.settings) as (home, directory_fd):
            async with _refresh_lock(directory_fd):
                _check_file_store(directory_fd)
                _, expected_account, _ = _load_auth(directory_fd)
                async with self.rpc_factory(home, executable=self.settings.codex_executable) as rpc:
                    config = await rpc.request("config/read", {"includeLayers": False})
                    if config["config"].get("cli_auth_credentials_store", "file") != "file":
                        raise _Rejected("codex_file_store_required_keychain_unsupported")
                    del config
                    account = await rpc.request("account/read", {"refreshToken": False})
                    if (account.get("account") or {}).get("type") != "chatgpt":
                        raise _Rejected("codex_sign_in_required")
                    token, account_id, due = _load_auth(directory_fd)
                    if account_id != expected_account:
                        raise _Rejected("codex_account_changed")
                    if due:
                        account = await rpc.request("account/read", {"refreshToken": True})
                        if (account.get("account") or {}).get("type") != "chatgpt":
                            raise _Rejected("codex_sign_in_required")
                        token, account_id, due = _load_auth(directory_fd)
                        if account_id != expected_account or due:
                            raise _Rejected("codex_refresh_failed")
        async def pin_request(request):
            if request.method != "POST" or str(request.url) != CODEX_BASE_URL + "/responses":
                raise _Rejected("codex_endpoint_rejected")
            request.headers.clear()
            request.headers.update({
                "Host": "chatgpt.com", "Authorization": f"Bearer {token}",
                "ChatGPT-Account-Id": account_id, "Content-Type": "application/json",
                "Accept": "text/event-stream", "Accept-Encoding": "identity",
                "Content-Length": str(len(request.content)),
            })
            # Last await before transport egress, after the managed broker closed.
            if self.before_send is not None:
                await self.before_send()
        transport = self.http_transport or httpx.AsyncHTTPTransport(trust_env=False, retries=0)
        async with httpx.AsyncClient(
            transport=_LimitedTransport(transport), trust_env=False, follow_redirects=False,
            event_hooks={"request": [pin_request]},
        ) as http:
            async with _CodexSDK(
                api_key=token, base_url=CODEX_BASE_URL, max_retries=0, http_client=http,
                organization="", project="", admin_api_key="", webhook_secret="",
            ) as sdk:
                async with sdk.responses.stream(
                    model=self.settings.codex_model, instructions=instructions,
                    input=[{"role": "user", "content": content}], text_format=schema,
                    tools=[], store=False, reasoning={"effort": "low"},
                ) as stream:
                    completed = False
                    done_items = {}
                    done_texts = {}
                    async for event in stream:
                        _check_event(event)
                        if event.type == "response.output_text.done":
                            key = (event.output_index, event.content_index)
                            if completed or key in done_texts:
                                raise _Rejected("codex_invalid_output")
                            done_texts[key] = event.text
                        if event.type == "response.output_item.done":
                            index = event.output_index
                            if completed or index in done_items or type(index) is not int or not 0 <= index < 64:
                                raise _Rejected("codex_invalid_output")
                            done_items[index] = event.item
                        completed = completed or event.type == "response.completed"
                    if not completed:
                        raise _Rejected("codex_incomplete")
                    response = await stream.get_final_response()
                    response = _reconcile_output(response, done_items, done_texts)
                return _validated_output(response, schema), _usage(response)


async def oauth_status(settings) -> dict:
    result = {
        "provider": "codex", "configured": bool(settings.codex_home),
        "authenticated": False, "state": "not_configured", "model": settings.codex_model,
    }
    if not settings.codex_home:
        return result
    try:
        async with asyncio.timeout(12):
            async with CodexRPC(settings.codex_home, executable=settings.codex_executable) as rpc:
                data = await rpc.request("account/read", {"refreshToken": False})
                authenticated = (data.get("account") or {}).get("type") == "chatgpt"
                result.update(authenticated=authenticated, state="connected" if authenticated else "sign_in_required")
    except Exception:
        result["state"] = "unavailable"
    return result
