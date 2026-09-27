"""Durable lease owner for discovery/report and independent recording jobs."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import hashlib
import json
import uuid

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from beep_agent.discovery import DiscoveryEngine
from beep_agent.domain import Evidence, Snapshot
from beep_agent.providers import OpenAIProvider
from beep_agent.realtime import consented
from beep_agent.store import Store


async def store_call(method, *args, **kwargs):
    """Join DB threads even on cancellation; no write escapes the owning lifecycle."""
    task = asyncio.create_task(asyncio.to_thread(method, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class AdmittedProvider:
    """Recheck authority at every provider entry, after graph checkpoint IO."""

    def __init__(self, provider, admit):
        self.provider, self.admit = provider, admit

    async def extract(self, *args, **kwargs):
        await self.admit()
        return await self.provider.extract(*args, **kwargs)

    async def synthesize_report(self, *args, **kwargs):
        await self.admit()
        return await self.provider.synthesize_report(*args, **kwargs)


class DurableWorker:
    def __init__(
        self,
        settings,
        *,
        store=None,
        provider=None,
        recording=None,
        lease_seconds=120,
        job_timeout=90,
        provider_transport=None,
    ):
        self.settings = settings
        self.store = store or Store(settings.database_url)
        self.provider = AdmittedProvider(provider, self._admit) if provider is not None else None
        self._admission = None
        self.provider_transport = provider_transport
        self._openai_client = None
        self._usage = []
        self.recording = recording
        self.worker_id = "worker-" + uuid.uuid4().hex
        self.lease_seconds = lease_seconds
        self.job_timeout = job_timeout
        self.checkpointer = None
        self.engine = None
        self.stack = AsyncExitStack()

    @staticmethod
    def thread_id(tenant_id: str, session_id: str) -> str:
        # Unambiguous tenant/session binding, bounded independently of opaque ID length.
        return "beep:" + hashlib.sha256(json.dumps([tenant_id, session_id]).encode()).hexdigest()

    async def __aenter__(self):
        await store_call(self.store.initialize)
        self.checkpointer = await self.stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(self.settings.database_url)
        )
        await self.checkpointer.setup()
        if self.provider is not None:
            self.engine = DiscoveryEngine(self.provider, checkpointer=self.checkpointer)
        return self

    async def __aexit__(self, *exc):
        if self._openai_client:
            await self._openai_client.close()
        await self.stack.aclose()
        await store_call(self.store.close)

    async def run_once(self) -> bool:
        job = await store_call(self.store.claim_job, self.worker_id, self.lease_seconds)
        if not job:
            return False
        execution = asyncio.create_task(self.execute(job), name="beep.durable-job")
        watcher = asyncio.create_task(self._watch_lease(job), name="beep.lease-owner")
        try:
            timeout = (
                max(self.job_timeout, 180) if job["kind"] == "recording_stop" else self.job_timeout
            )
            async with asyncio.timeout(timeout):
                done, _ = await asyncio.wait(
                    [execution, watcher], return_when=asyncio.FIRST_COMPLETED
                )
                if watcher in done:
                    watcher.result()
                result = await execution
            if job["kind"] == "report" and result.get("status") == "superseded":
                # Consent/publication denial is not successful finalisation. Store
                # preserves a newer correction/report owner before terminalising.
                committed = await store_call(self.store.fail_job, job["id"], job["lease_token"],
                                             "report_not_publishable", retry=False)
            else:
                committed = await store_call(self.store.finish_job, job["id"], job["lease_token"], result)
            if not committed:
                raise RuntimeError("Lease lost before job commit")
        except asyncio.CancelledError:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            await store_call(
                self.store.fail_job,
                job["id"],
                job["lease_token"],
                "cancelled_ambiguous",
                retry=False,
            )
            raise
        except Exception:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            await store_call(
                self.store.fail_job,
                job["id"],
                job["lease_token"],
                "job_failed_reconciliation_required",
                retry=False,
            )
        finally:
            execution.cancel()
            watcher.cancel()
            await asyncio.gather(execution, watcher, return_exceptions=True)
        return True

    async def _watch_lease(self, job):
        state = await store_call(self.store.get_session, job["tenant_id"], job["session_id"])
        epoch = state["consent_epoch"]
        interval = min(self.lease_seconds / 3, 0.5)
        while True:
            await asyncio.sleep(interval)
            if not await store_call(
                self.store.renew_job, job["id"], job["lease_token"], self.lease_seconds
            ):
                raise RuntimeError("Lease lost")
            if job["kind"] in {"discovery", "report"}:
                state = await store_call(
                    self.store.get_session, job["tenant_id"], job["session_id"]
                )
                if (
                    state["consent_epoch"] != epoch
                    or not consented(state)
                    or state["status"] == "paused"
                ):
                    raise RuntimeError("Consent changed during job")

    async def _recording(self, job, state):
        tenant, sid = job["tenant_id"], job["session_id"]
        if job["kind"] == "recording_start":
            if (
                not consented(state)
                or state["status"] not in {"introduction", "active"}
                or job["payload"].get("consent_epoch") != state["consent_epoch"]
            ):
                return {"status": "superseded"}
            if state["recording_status"] == "recording" and state.get("egress_id"):
                return {"status": "already_recording"}
        # Recording cleanup remains available without an inference credential.
        # RecordingService performs its own room/S3 prerequisite gate.
        if self.recording is None:
            from beep_agent.recording import RecordingService

            self.recording = RecordingService(self.settings)
        if job["kind"] == "recording_start":
            state = await store_call(self.store.set_recording, tenant, sid, "starting")
            reservation = state["recording_reservation"]
            binding = {"reservation_id": reservation["id"], "bucket": self.settings.s3_bucket}

            def bind_storage():
                from psycopg.types.json import Jsonb
                from beep_agent.store import StoreError
                with self.store._connect() as c:
                    current = self.store._row(c, tenant, sid, lock=True)["data"]
                    if (not self.store._lease_valid(c, sid) or current.get("recording_reservation") != reservation
                            or current["consent_epoch"] != reservation["consent_epoch"]):
                        raise StoreError("Recording storage binding fence lost")
                    c.execute("""UPDATE beep_jobs SET result=COALESCE(result,'{}'::jsonb) || %s
                        WHERE id=%s AND lease_token=%s AND status='running'""",
                        (Jsonb({"recording_storage_binding": binding}), job["id"], job["lease_token"]))

            async def start_and_retain():
                # Shield the bounded SDK start AND retention as one lifecycle. Cancellation
                # cannot discard a successfully returned ID between these two operations.
                eid = None
                try:
                    await store_call(bind_storage)
                    eid = await self.recording.start({**state, "recording_storage_binding": binding})
                    return eid
                except BaseException as exc:
                    eid = getattr(exc, "egress_id", None)
                    raise
                finally:
                    await store_call(
                        self.store.request_recording_cleanup, tenant, sid, reservation, eid
                    )

            start = asyncio.create_task(start_and_retain(), name="beep.start-and-retain")
            try:
                eid = await asyncio.shield(start)
                await store_call(self.store.set_recording, tenant, sid, "recording", eid)
                return {"status": "recording", "egress_id": eid, "recording_storage_binding": binding}
            except BaseException:
                # Joining preserves exact ID even if timeout/lease cancellation wins.
                result = await asyncio.gather(start, return_exceptions=True)
                eid = result[0] if isinstance(result[0], str) else getattr(result[0], "egress_id", None)
                from beep_agent.store import StoreError
                from contextlib import suppress

                with suppress(StoreError):
                    await store_call(self.store.set_recording, tenant, sid, "failed", eid)
                raise
        payload = job["payload"]
        eid = payload.get("egress_id") or state.get("egress_id")
        reservation = payload.get("reservation")
        if not eid and not reservation:
            return {"status": "no_recording"}
        from beep_agent.store import StoreError
        from contextlib import suppress

        # The store compares epoch/identity atomically; stale cleanup still stops its
        # exact external target but must never relabel a newer current recorder.
        with suppress(StoreError):
            await store_call(self.store.set_recording, tenant, sid, "stopping", eid)
        try:
            if reservation:
                def storage_binding():
                    with self.store._connect() as c:
                        row = c.execute("""SELECT result->'recording_storage_binding' AS binding
                            FROM beep_jobs WHERE session_id=%s AND kind='recording_start'
                            AND result->'recording_storage_binding'->>'reservation_id'=%s""",
                            (sid, reservation["id"])).fetchone()
                        return row["binding"] if row else None
                binding = await store_call(storage_binding)
                cleanup_state = {**state, **({"recording_storage_binding": binding} if binding else {})}
                ids = await self.recording.reconcile_stop(cleanup_state, payload)
                if not binding:
                    # Legacy reservations did not pin a bucket before start. A later
                    # configured bucket cannot retroactively certify artifact identity.
                    from beep_agent.recording import CleanupResult
                    ids = CleanupResult(ids, getattr(ids, "failed_ids", []))
                await store_call(
                    self.store.complete_recording_cleanup, tenant, sid, reservation, ids
                )
                failed_ids = getattr(ids, "failed_ids", [])
                manifests = [m.model_dump(mode="json") for m in getattr(ids, "manifests", [])]
                return {"status": "cleanup_complete_artifact_unavailable" if failed_ids else
                        "cleanup_complete_decode_verified" if manifests and all(
                            m["media_validation"] == "verified" for m in manifests) else
                        "cleanup_complete_decode_failed" if any(m["media_validation"] == "failed" for m in manifests) else
                        "cleanup_complete_metadata_verified" if manifests else "cleanup_complete_artifact_unverified",
                        "egress_ids": list(ids), "failed_ids": failed_ids, "recording_manifests": manifests}
            else:
                await self.recording.stop(eid)
                with suppress(StoreError):
                    await store_call(self.store.set_recording, tenant, sid, "stopped", eid)
        except BaseException:
            with suppress(StoreError):
                await store_call(self.store.set_recording, tenant, sid, "failed", eid)
            raise
        return {"status": "stopped", "egress_id": eid}

    def _ensure_provider(self):
        if self.provider is not None:
            return
        if self.settings.inference_provider == "codex":
            from beep_agent.telemetry import source_versions, budget_capability
            self.settings.require_inference()

            def meter_codex(usage):
                self._usage.append({
                    **usage,
                    **budget_capability(getattr(self, '_configured_budget_aud', None)),
                    "versions": source_versions(),
                })

            self.provider = AdmittedProvider(
                OpenAIProvider(self.settings, usage_callback=meter_codex, before_send=self._admit), self._admit
            )
            self.engine = DiscoveryEngine(self.provider, checkpointer=self.checkpointer)
            return
        import httpx
        from openai import AsyncOpenAI
        from beep_agent.telemetry import source_versions, budget_capability

        self.settings.require_runtime()

        async def meter(response):
            if response.is_success:
                await response.aread()
                body = response.json()
                usage = body.get("usage")
                if isinstance(usage, dict):
                    self._usage.append(
                        {
                            key: usage[key]
                            for key in (
                                "input_tokens",
                                "output_tokens",
                                "total_tokens",
                                "input_tokens_details",
                                "output_tokens_details",
                            )
                            if key in usage
                        }
                        | {
                            "measurement": "measured",
                            **budget_capability(getattr(self, '_configured_budget_aud', None)),
                            "source": "openai.responses.usage",
                            "model": body.get("model", self.settings.planner_model),
                            "request_id": body.get("id"),
                            "versions": source_versions(),
                        }
                    )

        http = httpx.AsyncClient(
            transport=self.provider_transport, event_hooks={"response": [meter]}, timeout=35
        )
        self._openai_client = AsyncOpenAI(
            api_key=self.settings.openai_api_key.get_secret_value(),
            base_url="https://api.openai.com/v1",
            max_retries=0,
            timeout=35,
            http_client=http,
        )
        self.provider = AdmittedProvider(
            OpenAIProvider(self.settings, client=self._openai_client), self._admit
        )
        self.engine = DiscoveryEngine(self.provider, checkpointer=self.checkpointer)

    async def _admit(self):
        if self._admission is None or not await store_call(
            self.store.admit_inference, *self._admission
        ):
            raise PermissionError("Inference admission revoked")

    async def _once_inference(self, thread_id, operation_key, call):
        """Durable ambiguity marker written BEFORE network IO, result BEFORE publication.

        Lost acknowledgement never causes another queued job to replay a paid call.
        A retained result can be republished without inference after a worker crash.
        """
        from langgraph.checkpoint.base import empty_checkpoint

        op_id = hashlib.sha256(operation_key.encode()).hexdigest()
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": "operation:" + op_id}}
        previous = await self.checkpointer.aget_tuple(config)
        if previous:
            values = previous.checkpoint["channel_values"]
            if values.get("status") == "complete":
                return json.loads(values["result_json"])
            raise RuntimeError("Ambiguous inference requires explicit operator reconciliation")
        started = empty_checkpoint()
        started["channel_values"] = {"status": "started"}
        started["channel_versions"] = {"status": 1}
        config = await self.checkpointer.aput(
            config, started, {"source": "input", "step": 0}, {"status": 1}
        )
        await self._admit()
        result = (await call()).model_dump(mode="json")
        completed = empty_checkpoint()
        completed["channel_values"] = {"status": "complete", "result_json": json.dumps(result)}
        completed["channel_versions"] = {"status": 2, "result_json": 1}
        await self.checkpointer.aput(
            config, completed, {"source": "update", "step": 1}, completed["channel_versions"]
        )
        return result

    async def execute(self, job: dict) -> dict:
        self._usage = []
        result = None
        with self.store.job_lease(job["id"], job["lease_token"]):
            try:
                result = await self._execute(job)
                return result
            finally:
                receipts = self._usage
                self._usage = []
                for usage in receipts:
                    attributed = {**usage, "stage": job["kind"], "job_id": job["id"]}
                    try:
                        await store_call(self.store.add_usage, job["tenant_id"], job["session_id"], attributed)
                    except Exception:
                        if result is None:
                            raise
                        # Do not replay inference or call a committed result unpublished.
                        # Retain the receipt, not exception detail, for exact-job accounting.
                        result["usage_reconciliation_required"] = True
                        result.setdefault("pending_usage", []).append(attributed)

    async def _execute(self, job: dict) -> dict:
        tenant, sid = job["tenant_id"], job["session_id"]
        state = await store_call(self.store.get_session, tenant, sid)
        self._configured_budget_aud = state.get('budget_aud')
        if job["kind"] in {"recording_start", "recording_stop"}:
            return await self._recording(job, state)
        if job["kind"] not in {"discovery", "report"}:
            raise ValueError("Unsupported job kind")
        if state["status"] not in {"active", "finalising"} or not consented(state):
            return {"status": "superseded"}
        self._admission = (
            job["id"], job["lease_token"], tenant, sid, state["consent_epoch"]
        )
        if self.settings.inference_provider == "codex":
            self.settings.require_inference()
        else:
            self.settings.require_runtime()
        self._ensure_provider()
        snap = Snapshot.model_validate(await store_call(self.store.load_snapshot, tenant, sid))
        events = await store_call(self.store.list_events, tenant, sid, snap.last_event_seq)
        if not events:
            if job["kind"] == "report" and state["status"] == "finalising":
                from beep_agent.domain import Report

                report = Report.model_validate(
                    await self._once_inference(
                        self.thread_id(tenant, sid),
                        f"report:{snap.revision}:{state['consent_epoch']}",
                        lambda: self.provider.synthesize_report(
                            snap, sid, partial=bool(job["payload"].get("partial"))
                        ),
                    )
                )
                await store_call(
                    self.store.commit_report,
                    job["id"],
                    job["lease_token"],
                    tenant,
                    sid,
                    {
                        "report": report.model_dump(mode="json"),
                        "expected_revision": snap.revision,
                        "consent_epoch": state["consent_epoch"],
                    },
                )
                return {"status": "reported", "revision": snap.revision}
            return {"status": "already_current", "revision": snap.revision}
        event = Evidence.model_validate(events[0])
        # Retained evidence keeps its ORIGINAL capture epoch. Replay with an epoch-bound
        # graph view, then restore current publication epoch; never revive old probes.
        view = Snapshot.model_validate(
            {**snap.model_dump(mode="json"), "consent_epoch": event.consent_epoch, "probe": None}
        )
        if event.actor in {"agent", "system", "facilitator"}:
            result = Snapshot.model_validate(
                {
                    **view.model_dump(mode="json"),
                    "evidence": [*view.evidence, event],
                    "last_event_seq": event.seq,
                    "revision": view.revision + 1,
                    "probe": None,
                }
            )
        else:
            result = Snapshot.model_validate(
                await self._once_inference(
                    self.thread_id(tenant, sid),
                    f"discovery:{event.id}:{snap.revision}",
                    lambda: self.engine.process(view, event, thread_id=self.thread_id(tenant, sid)),
                )
            )
        data = result.model_dump(mode="json")
        if event.consent_epoch != state["consent_epoch"] or state["status"] != "active":
            data["probe"] = None
        data["consent_epoch"] = state["consent_epoch"]
        saved = await store_call(
            self.store.commit_snapshot,
            job["id"],
            job["lease_token"],
            tenant,
            sid,
            data,
            snap.revision,
            state["consent_epoch"],
        )
        if saved and (len(events) > 1 or job["kind"] == "report"):
            await store_call(self.store.enqueue_job, tenant, sid, job["kind"], job["payload"])
        return {"status": "saved" if saved else "superseded", "revision": result.revision}


async def ensure_agent_dispatch(settings, store, state) -> str | None:
    """Handover dispatch does not depend on a browser requesting a new room token."""
    from livekit import api
    import aiohttp

    if state["status"] != "active" or not consented(state):
        return None
    settings.require_runtime()
    tenant, sid, room = state["tenant_id"], state["id"], state["room_name"]
    metadata = json.dumps({"tenant_id": tenant, "session_id": sid}, sort_keys=True)
    async with api.LiveKitAPI(
        settings.livekit_url,
        settings.livekit_api_key.get_secret_value(),
        settings.livekit_api_secret.get_secret_value(),
        timeout=aiohttp.ClientTimeout(total=10),
        failover=False,
    ) as client:
        rooms = await client.room.list_rooms(api.ListRoomsRequest(names=[room]))
        if not rooms.rooms:
            await client.room.create_room(api.CreateRoomRequest(name=room, empty_timeout=600))
        dispatches = await client.agent_dispatch.list_dispatch(room)
        matching = [
            d
            for d in dispatches
            if d.room == room
            and d.agent_name == settings.agent_name
            and d.metadata == metadata
            and not d.state.deleted_at
        ]
        if matching:
            return matching[0].id
        if not await store_call(store.reserve_dispatch, tenant, sid, state["consent_epoch"]):
            raise RuntimeError("Dispatch outcome needs reconciliation")
        created = await client.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                room=room, agent_name=settings.agent_name, metadata=metadata
            )
        )
        verified = await client.agent_dispatch.get_dispatch(created.id, room)
        if (
            not verified
            or verified.id != created.id
            or verified.metadata != metadata
            or verified.room != room
            or verified.agent_name != settings.agent_name
        ):
            raise RuntimeError("Dispatch could not be confirmed")
        await store_call(store.confirm_dispatch, tenant, sid, state["consent_epoch"], verified.id)
        return verified.id


async def recording_health_sweep(settings, store, stop, *, recording=None, interval=1, timeout=3):
    """Independent exact-ID readback; every scan is bounded, never behind a graph."""
    from beep_agent.recording import RecordingService

    recording = recording or RecordingService(settings)

    async def check(state):
        try:
            async with asyncio.timeout(timeout):
                await recording.check_active(state)
        except Exception:
            status = 'failed'
        else:
            status = 'recording'
        await store_call(
            store.observe_recording, state['tenant_id'], state['id'], status,
            state.get('egress_id'), state['consent_epoch']
        )

    while not stop.is_set():
        states = await store_call(store.list_active_sessions)
        await asyncio.gather(*(check(s) for s in states if s['recording_status'] == 'recording'
                               and s['status'] in {'active', 'introduction'}))
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except TimeoutError:
            pass


async def control_sweep(settings, store, stop):
    """Independent control loop: no long graph/recording call delays handover or cap."""
    import logging

    confirmed = set()
    while not stop.is_set():
        states = await store_call(store.list_active_sessions)
        for state in states:
            key = (state["id"], state["consent_epoch"])
            if state["status"] == "active" and key not in confirmed:
                try:
                    await ensure_agent_dispatch(settings, store, state)
                    confirmed.add(key)
                except Exception:
                    logging.getLogger("beep.worker").warning(
                        "Agent dispatch unavailable; no inference fallback"
                    )
        try:
            await asyncio.wait_for(stop.wait(), 2)
        except TimeoutError:
            pass


async def run_worker(settings, *, once=False, stop=None) -> int:
    """Run one serialized job at a time; scale with independent lease-owning processes."""
    import signal

    if not settings.database_url:
        raise ValueError("Explicit database configuration required")
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    if not once:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
    count = 0
    try:
        async with DurableWorker(settings) as worker:
            health = None if once else asyncio.create_task(
                recording_health_sweep(settings, worker.store, stop), name="beep.recording-health"
            )
            sweep = (
                None
                if once
                else asyncio.create_task(
                    control_sweep(settings, worker.store, stop), name="beep.handover-and-cap"
                )
            )
            try:
                while not stop.is_set():
                    if sweep and sweep.done():
                        sweep.result()
                    await store_call(worker.store.list_active_sessions)
                    job_task = asyncio.create_task(worker.run_once())
                    try:
                        if health:
                            done, _ = await asyncio.wait(
                                [job_task, health, sweep], return_when=asyncio.FIRST_COMPLETED
                            )
                            for task in done:
                                task.result()
                        worked = await job_task
                    finally:
                        job_task.cancel()
                        await asyncio.gather(job_task, return_exceptions=True)
                    count += int(worked)
                    if once:
                        return count
                    if not worked:
                        try:
                            await asyncio.wait_for(stop.wait(), settings.worker_poll_seconds)
                        except TimeoutError:
                            pass
            finally:
                if health:
                    health.cancel()
                    await asyncio.gather(health, return_exceptions=True)
                if sweep:
                    sweep.cancel()
                    await asyncio.gather(sweep, return_exceptions=True)
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
    return count


def main():
    import argparse
    from beep_agent.config import Settings
    from beep_agent.telemetry import configure_logging

    parser = argparse.ArgumentParser(description="BEEP durable PostgreSQL worker")
    parser.add_argument("--once", action="store_true", help="Attempt one eligible job then exit")
    args = parser.parse_args()
    configure_logging()
    asyncio.run(run_worker(Settings(), once=args.once))


if __name__ == "__main__":
    main()
