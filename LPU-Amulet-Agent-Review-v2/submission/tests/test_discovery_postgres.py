"""Real PostgreSQL persistence + official SDK; HTTP responses are SYNTHETIC fixtures."""
import json
import os
import uuid

import httpx
import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from openai import AsyncOpenAI
from psycopg.conninfo import make_conninfo

from beep_agent.discovery import DiscoveryEngine
from beep_agent.domain import Snapshot
from beep_agent.providers import OpenAIProvider
from beep_agent.reports import build_report, render_report_html
from test_discovery import extraction, source
from test_providers import response_envelope, synthetic_settings


@pytest.fixture
def isolated_checkpoint_database():
    dsn = os.environ.get("BEEP_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("BEEP_TEST_DATABASE_URL required for real PostgreSQL checkpoints")
    schema = "domain_test_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
    try:
        yield make_conninfo(dsn, options=f"-c search_path={schema}")
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema)))


@pytest.mark.asyncio
async def test_postgres_restart_recovers_real_sdk_graph_then_preserves_correction_in_report(isolated_checkpoint_database, tmp_path):
    requests = []
    corrected = extraction(
        claims=[{"id": "c2", "text": "SYNTHETIC correction: Rae drafts the quotation.", "evidence_ids": ["e2"], "status": "reported"}],
        steps=[{"id": "s1", "title": "Draft quotation", "actor": "Rae", "system": "Spreadsheet", "description": "Rae drafts", "evidence_ids": ["e2"]}],
        coverage=[], superseded_claim_ids=["c1"],
    )
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        event = json.loads(payload["input"][0]["content"][0]["text"])["untrusted_data"]["event"]
        return httpx.Response(200, json=response_envelope(extraction() if event["id"] == "e1" else corrected))
    before = Snapshot(title="SYNTHETIC persisted quotation", pack_id="quotation")
    thread_id = "synthetic-tenant/" + uuid.uuid4().hex
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with AsyncOpenAI(api_key="synthetic-test-not-a-credential", http_client=http) as sdk:
            provider = OpenAIProvider(synthetic_settings(), sdk)
            async with AsyncPostgresSaver.from_conn_string(isolated_checkpoint_database) as saver:
                await saver.setup()
                first = await DiscoveryEngine(provider, saver).process(before, source(), thread_id=thread_id)
                assert await saver.aget_tuple({"configurable": {"thread_id": thread_id}}) is not None
            async with AsyncPostgresSaver.from_conn_string(isolated_checkpoint_database) as restarted_saver:
                engine = DiscoveryEngine(provider, restarted_saver)
                recovered = await engine.process(before, source(), thread_id=thread_id)
                assert recovered == first and len(requests) == 1
                final = await engine.process(recovered, source(2, kind="correction", text="SYNTHETIC: Rae, not Alex, drafts."), thread_id=thread_id)
                saved = await engine.graph.aget_state({"configurable": {"thread_id": thread_id}})
                assert Snapshot.model_validate(saved.values["result"]) == final
    assert len(requests) == 2
    assert all(request["text"]["format"]["strict"] for request in requests)
    report = build_report(final, "synthetic-persisted-report")
    assert report.revision == 2 and report.status == "partial"
    assert report.claims[0].status == "contradicted"
    assert report.evidence[0] == first.evidence[0]
    assert report.steps[0].actor == "Rae"
    output = tmp_path / "synthetic-client-report.html"
    html = render_report_html(report)
    output.write_text(html)
    assert 'href="#evidence-2"' in html and "SYNTHETIC" in html
    assert "Internal opportunity" not in html
