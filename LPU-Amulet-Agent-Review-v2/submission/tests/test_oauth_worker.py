"""Real PostgreSQL and graph; synthetic Codex inference at the SDK boundary."""
import json

import psycopg
import pytest
from pydantic import SecretStr

from test_worker import active_session, database as database, settings as base_settings


@pytest.mark.asyncio
async def test_worker_routes_discovery_and_report_to_oauth_and_persists_usage(database, tmp_path, monkeypatch):
    from beep_agent import codex_oauth
    from beep_agent.store import Store
    from beep_agent.worker import DurableWorker

    calls = []
    class Inference:
        def __init__(self, settings, *, before_send):
            assert not settings.openai_api_key.get_secret_value()
            self.before_send = before_send
        async def structured(self, schema, instructions, content):
            await self.before_send()
            data = json.loads(content[0]["text"])["untrusted_data"]
            calls.append(data)
            event = data["event"]
            value = {"claims": [{"id": "c-" + event["id"], "text": event["text"], "status": "reported", "evidence_ids": [event["id"]]}]} if event else {"summary_claim_ids": ["c-e1"], "opportunity_claim_ids": [], "priority_unknowns": []}
            return schema.model_validate(value), {"provider": "codex", "model": "gpt-5.5", "input_tokens": 100, "output_tokens": 40, "measurement": "measured", "source": "codex.responses.usage"}

    monkeypatch.setattr(codex_oauth, "CodexInference", Inference, raising=False)
    settings = base_settings().model_copy(update={
        "database_url": database, "inference_provider": "codex", "codex_home": tmp_path,
        "codex_model": "gpt-5.5", "openai_api_key": SecretStr(""),
    })
    store = Store(database)
    store.initialize()
    session = active_session(store, recording=True)
    sid = session["id"]
    store.append_event("tenant", sid, {"id": "e1", "kind": "transcript", "actor": "client", "text": "SYNTHETIC: manager approves the quote.", "at_ms": 960001, "consent_epoch": 0})
    class RecordingFixture:
        async def stop(self, egress_id):
            assert egress_id == "EG-synthetic-fixture"
    async with DurableWorker(settings, store=store, recording=RecordingFixture()) as worker:
        assert await worker.run_once()
        saved = store.load_snapshot("tenant", sid)
        assert saved["claims"][0]["evidence_ids"] == ["e1"]
        store.control("tenant", sid, "client", "finish")
        for _ in range(8):
            if not await worker.run_once():
                break
        report = store.get_report("tenant", sid)
        assert report["summary"].startswith("Source-linked review synthesis.")
        with psycopg.connect(database) as conn:
            rows = conn.execute("SELECT data FROM beep_usage WHERE session_id=%s", (sid,)).fetchall()
        assert len(rows) == 2
        assert all(row[0]["provider"] == "codex" for row in rows)
    assert len(calls) == 2
