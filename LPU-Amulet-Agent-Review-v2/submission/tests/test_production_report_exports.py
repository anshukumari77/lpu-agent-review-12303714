"""PR-03 delivery route regression, including persisted pre-manifest reports."""
from psycopg.types.json import Jsonb

import test_api
from test_api import new_session, exchange
from test_reports import synthetic_snapshot
from beep_agent.reports import build_report

client = test_api.client
settings = test_api.settings
store = test_api.store


def test_legacy_report_and_export_routes_disclose_missing_recording(client, store):
    made = new_session(client)
    sid = made["session"]["id"]
    legacy = build_report(synthetic_snapshot(), sid).model_dump(mode="json")
    legacy.pop("recording", None)
    legacy["status"] = "complete"
    legacy["internal_opportunity"]["rationale"] = "SYNTHETIC INTERNAL DO NOT EXPORT"
    with store._connect() as conn:
        conn.execute("UPDATE beep_sessions SET report=%s WHERE id=%s", (Jsonb(legacy), sid))
    assert exchange(client, made["invitations"]["client"]).status_code == 200
    for suffix in ("report", "export"):
        response = client.get(f"/api/sessions/{sid}/{suffix}")
        assert response.status_code == 200
        report = response.json()["report"]
        assert report["status"] == "partial"
        assert report["recording"]["artifact_status"] == "unverified"
        assert report["recording"]["provenance"] == "missing_manifest"
        assert report["recording"]["evidence_epochs"] == [0]
        assert report["evidence"] == legacy["evidence"]
        assert "internal_opportunity" not in report
        assert "SYNTHETIC INTERNAL" not in response.text
    html = client.get(f"/api/sessions/{sid}/report.html")
    assert html.status_code == 200
    assert "Playback has not been validated" in html.text
    assert "SYNTHETIC INTERNAL" not in html.text
    # Read-time truthfulness must not rewrite original retained legacy content.
    assert store.get_report("local", sid) == legacy
