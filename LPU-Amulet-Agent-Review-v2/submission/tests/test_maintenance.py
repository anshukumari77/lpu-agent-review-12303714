"""Retention is explicit administrative maintenance, isolated synthetic tests only."""
import importlib.util
import os
from pathlib import Path
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest


@pytest.fixture
def maintenance_db():
    dsn = os.environ.get("BEEP_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Explicit isolated PostgreSQL test DSN required")
    parsed = conninfo_to_dict(dsn)
    assert parsed["dbname"].endswith("_test")
    assert parsed["host"] == str(Path(__file__).resolve().parents[1] / ".local/pgsocket")
    schema = "maintenance_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield make_conninfo(dsn, options=f"-c search_path={schema}")
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


async def test_retention_lists_only_due_terminal_records_and_requires_apply(maintenance_db):
    assert importlib.util.find_spec("beep_agent.maintenance") is not None
    from beep_agent.maintenance import Maintenance
    from beep_agent.config import Settings
    from beep_agent.store import Store
    store = Store(maintenance_db)
    store.initialize()
    session = store.create_session("synthetic", title="Synthetic retention", pack_id="general", offer="paid")
    with psycopg.connect(maintenance_db) as connection:
        connection.execute("UPDATE beep_sessions SET data=jsonb_set(data, '{created_at}', to_jsonb((now()-interval '31 days')::text)) WHERE id=%s", (session["id"],))
    maintenance = Maintenance(Settings(database_url=maintenance_db), store=store)
    assert [s["id"] for s in maintenance.due()] == [session["id"]]
    result = await maintenance.purge("synthetic", session["id"], apply=False)
    assert result["status"] == "dry_run"
    assert store.get_session("synthetic", session["id"])["status"] == "awaiting_consent"


async def test_purge_fences_then_deletes_checkpoint_and_all_local_records(maintenance_db):
    from beep_agent.maintenance import Maintenance
    from beep_agent.config import Settings
    from beep_agent.store import Store, StoreError
    from beep_agent.worker import DurableWorker
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import StateGraph, START, END
    store = Store(maintenance_db)
    store.initialize()
    session = store.create_session("synthetic", title="Synthetic purge", pack_id="general", offer="paid")
    sid = session["id"]
    called = []
    class StorageBoundary:
        async def delete(self, state):
            called.append(state["id"])
            # Remote deletion runs after a durable fail-closed local fence.
            current = store.get_session("synthetic", sid)
            assert current["status"] == "failed" and current["deletion_pending"] is True
    thread_id = DurableWorker.thread_id("synthetic", sid)
    config = {"configurable": {"thread_id": thread_id}}
    async with AsyncPostgresSaver.from_conn_string(maintenance_db) as saver:
        await saver.setup()
        graph = StateGraph(dict)
        graph.add_node("keep", lambda state: state)
        graph.add_edge(START, "keep")
        graph.add_edge("keep", END)
        await graph.compile(checkpointer=saver).ainvoke({"synthetic": "retained source"}, config)
        assert await saver.aget_tuple(config) is not None
        maintenance = Maintenance(Settings(database_url=maintenance_db), store=store, recording=StorageBoundary())
        assert (await maintenance.purge("synthetic", sid, apply=True))["status"] == "deleted"
        assert called == [sid]
        assert await saver.aget_tuple(config) is None
    with pytest.raises(StoreError):
        store.get_session("synthetic", sid)
    with psycopg.connect(maintenance_db) as connection:
        assert connection.execute("SELECT session_id FROM beep_deletion_tombstones WHERE tenant_id=%s AND session_id=%s", ("synthetic", sid)).fetchone()[0] == sid


async def test_failed_remote_delete_keeps_tombstone_fence_and_local_record(maintenance_db):
    from beep_agent.maintenance import Maintenance
    from beep_agent.config import Settings
    from beep_agent.store import Store
    store = Store(maintenance_db)
    store.initialize()
    session = store.create_session("synthetic", title="Deletion failure", pack_id="general", offer="paid")
    class StorageBoundary:
        async def delete(self, state):
            raise RuntimeError("Synthetic remote delete denied")
    maintenance = Maintenance(Settings(database_url=maintenance_db), store=store, recording=StorageBoundary())
    with pytest.raises(RuntimeError):
        await maintenance.purge("synthetic", session["id"], apply=True)
    state = store.get_session("synthetic", session["id"])
    assert state["deletion_pending"] is True
    assert state["status"] == "failed"
