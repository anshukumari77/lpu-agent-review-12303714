"""Private stdio bridge to the official Codex app-server. Never exports tokens."""
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import signal


class CodexError(RuntimeError):
    """Allowlisted failure only: upstream payloads and stderr are never surfaced."""

    def __init__(self, code="codex_unavailable"):
        self.code = code
        super().__init__(code)


class CodexRPC:
    def __init__(self, home: Path, *, executable="codex", command=None, timeout=35):
        self.home = Path(home).expanduser().resolve()
        self.command = command or [
            executable, "-c", 'forced_login_method="chatgpt"',
            "-c", 'model_provider="openai"', "app-server", "--listen", "stdio://",
        ]
        self.timeout = timeout
        self.process = None
        self._pump_task = None
        self._close_task = None
        self._pending = {}
        self._counter = 0
        self._write_lock = asyncio.Lock()
        self.events = asyncio.Queue(maxsize=512)

    async def __aenter__(self):
        # No API keys, proxy overrides, inherited model configuration or trace flags.
        env = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR", "LANG") if key in os.environ}
        env.update(CODEX_HOME=str(self.home), RUST_LOG="off", NO_COLOR="1")
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.command, env=env, cwd=str(self.home),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=4_000_000, start_new_session=True,
            )
            self._pump_task = asyncio.create_task(self._pump())
            await self.request("initialize", {
                "clientInfo": {"name": "beep_review", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            }, timeout=10)
            await self._send({"method": "initialized"})
            return self
        except asyncio.CancelledError:
            await self.aclose()
            raise
        except Exception:
            await self.aclose()
            raise CodexError("codex_start_failed") from None

    async def __aexit__(self, *_):
        await self.aclose()

    async def _send(self, message):
        data = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        if len(data) > 3_000_000:
            raise CodexError("codex_input_too_large")
        async with self._write_lock:
            if not self.process or self.process.returncode is not None:
                raise CodexError("codex_closed")
            self.process.stdin.write(data)
            await self.process.stdin.drain()

    async def request(self, method, params, *, timeout=None):
        self._counter += 1
        request_id = self._counter
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(timeout or self.timeout):
                await self._send({"id": request_id, "method": method, "params": params})
                return await future
        except (TimeoutError, ConnectionError, BrokenPipeError):
            await self.aclose()
            raise CodexError("codex_request_interrupted") from None
        finally:
            self._pending.pop(request_id, None)

    async def _pump(self):
        failure = "codex_closed"
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError()
                if "id" in message and "method" in message:
                    # BEEP has no approval, tool execution or token-refresh dispatcher.
                    await self._send({"id": message["id"], "error": {
                        "code": -32601, "message": "BEEP does not execute tools",
                    }})
                    failure = "codex_tool_request_rejected"
                    break
                if "id" in message:
                    future = self._pending.get(message["id"])
                    if future is not None and not future.done():
                        if "error" in message:
                            future.set_exception(CodexError("codex_rpc_rejected"))
                        else:
                            future.set_result(message.get("result"))
                elif "method" in message:
                    self.events.put_nowait(message)
        except (ValueError, asyncio.QueueFull):
            failure = "codex_protocol_error"
        except asyncio.CancelledError:
            pass
        except Exception:
            failure = "codex_protocol_error"
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CodexError(failure))
            with suppress(asyncio.QueueFull):
                self.events.put_nowait({"method": "beep/error", "params": {"code": failure}})

    async def aclose(self):
        if self._close_task is None or self._close_task.done():
            self._close_task = asyncio.create_task(self._close())
        cleanup = self._close_task
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # Every wait must be shielded: another cancel must not abort reaping.
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
            raise

    async def _close(self):
        if self.process and self.process.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
        if self._pump_task and not self._pump_task.done():
            self._pump_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._pump_task
