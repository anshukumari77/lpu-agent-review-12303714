"""PR-03: truthful portable reports over real, isolated *_test PostgreSQL."""
import json

import pytest
from psycopg.types.json import Jsonb

import test_store
from beep_agent.domain import Report, Snapshot
from beep_agent.reports import build_report, render_report_html
from test_reports import synthetic_snapshot


@pytest.fixture
def store():
    yield from test_store.store.__wrapped__()


def finished_snapshot(store, *, recording_status="failed"):
    session = test_store.activate(store)
    sid = session["id"]
    snap = synthetic_snapshot(revision=1)
    for event in snap.evidence:
        store.append_event("t1", sid, event.model_dump(mode="json"))
    assert store.save_snapshot("t1", sid, snap.model_dump(mode="json"), expected_revision=0, consent_epoch=0)
    store.control("t1", sid, "client", "finish")
    store.set_recording("t1", sid, recording_status)
    return sid, Snapshot.model_validate(store.load_snapshot("t1", sid))


def test_failed_recording_cannot_publish_complete_report(store):
    """The audit's sufficient-coverage + failed-recorder counterexample."""
    sid, snap = finished_snapshot(store)
    report = build_report(snap, sid)
    assert store.put_report("t1", sid, report.model_dump(mode="json"), expected_revision=snap.revision)
    delivered = store.get_report("t1", sid)
    assert delivered["status"] == "partial"
    assert store.get_session("t1", sid)["status"] == "partial"
    assert delivered["recording"]["artifact_status"] == "unavailable"
    assert delivered["recording"]["media_validation"] == "not_performed"
    assert delivered["evidence"] == snap.model_dump(mode="json")["evidence"]
    assert "Recording outcome and provenance" in render_report_html(Report.model_validate(delivered))


def test_missing_manifest_legacy_report_json_html_are_explicitly_unverified():
    data = build_report(synthetic_snapshot(), "synthetic-legacy").model_dump(mode="json")
    data.pop("recording", None)
    data["status"] = "complete"
    data["summary"] = "This completed review documents source-linked text findings."
    report = Report.model_validate(data)
    assert report.status == "partial"
    exported = json.loads(report.model_dump_json(exclude={"internal_opportunity"}))
    assert exported["recording"]["artifact_status"] == "unverified"
    assert exported["recording"]["provenance"] == "missing_manifest"
    assert exported["recording"]["media_validation"] == "not_performed"
    assert exported["recording"]["evidence_epochs"] == [0]
    html = render_report_html(report)
    assert "Recording outcome and provenance" in html
    assert "Playback has not been validated" in html
    assert "This completed review" not in html
    assert "Internal opportunity" not in html


def test_text_only_build_never_claims_recorded_review_complete():
    report = build_report(synthetic_snapshot(), "synthetic-unverified")
    assert report.status == "partial"
    assert report.recording.artifact_status == "unverified"
    assert report.evidence == synthetic_snapshot().evidence
    assert any("recording" in gap.lower() for gap in report.unknowns)


def reserve_epoch(store):
    session = test_store.activate(store)
    job = store.claim_job("synthetic-start")
    with store.job_lease(job["id"], job["lease_token"]):
        session = store.set_recording("t1", session["id"], "starting")
        store.set_recording("t1", session["id"], "recording", "EG-synthetic")
    binding = {"reservation_id": session["recording_reservation"]["id"], "bucket": "synthetic-private"}
    store.finish_job(job["id"], job["lease_token"], {"recording_storage_binding": binding})
    store.control("t1", session["id"], "client", "pause")
    cleanup = store.claim_job("synthetic-cleanup")
    return session, cleanup


def test_id_only_cleanup_is_not_artifact_confirmation(store):
    session, job = reserve_epoch(store)
    with store.job_lease(job["id"], job["lease_token"]):
        store.complete_recording_cleanup("t1", session["id"], session["recording_reservation"], ["EG-synthetic"])
    final = store.get_session("t1", session["id"])
    assert final["recording_cleanup_complete"]
    assert final["recording_artifact_status"] == "unverified"


@pytest.mark.parametrize("field,value", [("consent_epoch", 9), ("key", "beep/unrelated/file.mp4"),
    ("bucket", "unrelated-bucket"), ("room_name", "beep-other"), ("reservation_id", "other"),
    ("egress_id", "EG-other")])
def test_cleanup_rejects_mismatched_manifest_without_settling(store, monkeypatch, field, value):
    from beep_agent.recording import CleanupResult
    from beep_agent.store import StoreError
    from test_production_recording_identity import completed_info, service_with_head
    session, job = reserve_epoch(store)
    service, _ = service_with_head(monkeypatch)
    manifest = service._verify_files(completed_info(session), session).model_copy(update={field: value})
    with store.job_lease(job["id"], job["lease_token"]), pytest.raises(StoreError):
        store.complete_recording_cleanup("t1", session["id"], session["recording_reservation"],
            CleanupResult(["EG-synthetic"], manifests=[manifest]))
    with store._connect() as conn:
        assert not conn.execute("SELECT settled FROM beep_recording_reservations WHERE id=%s",
            (session["recording_reservation"]["id"],)).fetchone()["settled"]


async def test_worker_binds_bucket_before_start_retains_private_cleanup_manifest(store, monkeypatch):
    from beep_agent.worker import DurableWorker
    from test_production_recording_identity import completed_info, EgressReadback, service_with_head
    service, _ = service_with_head(monkeypatch)
    session = test_store.activate(store)
    sid = session["id"]
    job = store.claim_job("synthetic-start")
    async def start(state):
        with store._connect() as conn:
            receipt = conn.execute("SELECT result FROM beep_jobs WHERE id=%s", (job["id"],)).fetchone()["result"]
        assert receipt["recording_storage_binding"] == {
            "reservation_id": state["recording_reservation"]["id"], "bucket": "synthetic-private"}
        return "EG-synthetic"
    monkeypatch.setattr(service, "start", start)
    worker = DurableWorker(service.settings, store=store, recording=service)
    with store.job_lease(job["id"], job["lease_token"]):
        result = await worker._recording(job, session)
    assert store.finish_job(job["id"], job["lease_token"], result)
    session = store.get_session("t1", sid)
    reservation = dict(session["recording_reservation"])
    assert "recording_storage_binding" not in session
    fake = EgressReadback([completed_info(session)])
    monkeypatch.setattr(service, "_api", lambda: fake)
    store.control("t1", sid, "client", "pause")
    cleanup = store.claim_job("synthetic-cleanup")
    with store.job_lease(cleanup["id"], cleanup["lease_token"]):
        result = await worker._recording(cleanup, store.get_session("t1", sid))
    assert store.finish_job(cleanup["id"], cleanup["lease_token"], result)
    with store._connect() as conn:
        saved = conn.execute("SELECT result FROM beep_jobs WHERE id=%s", (cleanup["id"],)).fetchone()["result"]
        reserved = conn.execute("SELECT data,settled FROM beep_recording_reservations WHERE id=%s",
            (reservation["id"],)).fetchone()
    assert saved["recording_manifests"][0]["key"] == reservation["output_prefix"] + ".mp4"
    assert reserved == {"data": reservation, "settled": True}
    final = store.get_session("t1", sid)
    assert final["recording_artifact_status"] == "metadata_verified"
    assert "recording_manifests" not in final


def test_old_epoch_cleanup_retains_private_receipt_without_relabelling_new_recorder(store, monkeypatch):
    from beep_agent.recording import CleanupResult
    from test_production_recording_identity import completed_info, service_with_head
    session, job = reserve_epoch(store)
    sid = session["id"]
    service, _ = service_with_head(monkeypatch)
    manifest = service._verify_files(completed_info(session), session)
    with store._connect() as conn:
        current = {**store._row(conn, "t1", sid)["data"], "recording_reservation": {"id": "new"},
            "recording_epoch": 1, "consent_epoch": 1, "egress_id": "EG-new", "recording_status": "recording"}
        conn.execute("UPDATE beep_sessions SET data=%s WHERE id=%s", (Jsonb(current), sid))
    with store.job_lease(job["id"], job["lease_token"]):
        store.complete_recording_cleanup("t1", sid, session["recording_reservation"],
            CleanupResult(["EG-synthetic"], manifests=[manifest]))
    assert store.get_session("t1", sid) == current
    with store._connect() as conn:
        assert conn.execute("SELECT result FROM beep_jobs WHERE id=%s", (job["id"],)).fetchone()["result"][
            "recording_manifests"][0]["consent_epoch"] == 0


def test_pause_resume_reports_both_epochs_without_private_ids_or_fabricated_playback(store, monkeypatch):
    from beep_agent.recording import CleanupResult
    from test_production_recording_identity import completed_info, service_with_head
    service, _ = service_with_head(monkeypatch)
    old, cleanup = reserve_epoch(store)
    sid = old["id"]
    manifest0 = service._verify_files(completed_info(old), old)
    with store.job_lease(cleanup["id"], cleanup["lease_token"]):
        store.complete_recording_cleanup("t1", sid, old["recording_reservation"],
            CleanupResult(["EG-synthetic"], manifests=[manifest0]))
    store.finish_job(cleanup["id"], cleanup["lease_token"], {
        "recording_manifests": [manifest0.model_dump(mode="json")]})
    store.control("t1", sid, "client", "resume")
    start = store.claim_job("synthetic-resume")
    with store.job_lease(start["id"], start["lease_token"]):
        current = store.set_recording("t1", sid, "starting")
        store.set_recording("t1", sid, "recording", "EG-synthetic-new")
    reservation1 = current["recording_reservation"]
    store.finish_job(start["id"], start["lease_token"], {"recording_storage_binding": {
        "reservation_id": reservation1["id"], "bucket": "synthetic-private"}})
    snapshot = synthetic_snapshot(revision=1, consent_epoch=1)
    events = [e.model_copy(update={"consent_epoch": 1}) for e in snapshot.evidence]
    for event in events:
        store.append_event("t1", sid, event.model_dump(mode="json"))
    snapshot = Snapshot.model_validate({**snapshot.model_dump(mode="json"), "evidence": events})
    assert store.save_snapshot("t1", sid, snapshot.model_dump(mode="json"), expected_revision=0, consent_epoch=1)
    store.control("t1", sid, "client", "finish")
    # Claim only the cleanup; synthetic discovery jobs have not contacted a provider.
    while True:
        job = store.claim_job("synthetic-final-cleanup")
        if job["kind"] == "recording_stop":
            break
        store.finish_job(job["id"], job["lease_token"], {"status": "synthetic-no-provider"})
    info = completed_info(current, egress_id="EG-synthetic-new")
    info.started_at += 10_000_000_000
    info.ended_at += 10_000_000_000
    manifest1 = service._verify_files(info, current)
    with store.job_lease(job["id"], job["lease_token"]):
        store.complete_recording_cleanup("t1", sid, reservation1,
            CleanupResult(["EG-synthetic-new"], manifests=[manifest1]))
    store.finish_job(job["id"], job["lease_token"], {"recording_manifests": [manifest1.model_dump(mode="json")]})
    report = build_report(snapshot, sid)
    report.internal_opportunity["rationale"] = "SYNTHETIC INTERNAL SENTINEL"
    assert store.put_report("t1", sid, report.model_dump(mode="json"), expected_revision=1)
    saved = store.get_report("t1", sid)
    assert saved["status"] == "partial"
    assert saved["recording"]["artifact_status"] == "metadata_verified"
    assert saved["recording"]["cleanup_status"] == "confirmed"
    assert [segment["consent_epoch"] for segment in saved["recording"]["segments"]] == [0, 1]
    assert [segment["started_at_ns"] for segment in saved["recording"]["segments"]] == [
        manifest0.started_at_ns, manifest1.started_at_ns]
    assert saved["evidence"] == snapshot.model_dump(mode="json")["evidence"]
    portable = Report.model_validate(saved).model_dump_json(exclude={"internal_opportunity"})
    html = render_report_html(Report.model_validate(saved))
    for private in (manifest0.key, manifest1.key, manifest0.egress_id, manifest1.egress_id,
                    "synthetic-private", "SYNTHETIC INTERNAL SENTINEL"):
        assert private not in portable and private not in html
    assert "not_performed" in portable and "Playback has not been validated" in html


def test_unsettled_cleanup_is_reported_pending_and_not_erased_by_partial_delivery(store):
    session, cleanup = reserve_epoch(store)
    store.finish_job(cleanup["id"], cleanup["lease_token"], {"status": "synthetic-unresolved"})
    sid = session["id"]
    store.control("t1", sid, "client", "finish")
    snapshot = Snapshot.model_validate(store.load_snapshot("t1", sid))
    assert store.put_report("t1", sid, build_report(snapshot, sid).model_dump(mode="json"), expected_revision=0)
    report = store.get_report("t1", sid)
    assert report["recording"]["cleanup_status"] == "pending"
    assert report["recording"]["artifact_status"] == "unverified"
    assert store.get_session("t1", sid)["recording_cleanup_pending"]


def test_failed_new_epoch_does_not_hide_retained_older_artifact(monkeypatch):
    from beep_agent.reports import recording_provenance
    from test_production_recording_identity import completed_info, reserved_session, service_with_head
    session = reserved_session()
    service, _ = service_with_head(monkeypatch)
    manifest = service._verify_files(completed_info(), session)
    projection = recording_provenance({**session, "recording_status": "failed"}, [],
        [{"data": session["recording_reservation"], "settled": True}], [manifest])
    assert projection.artifact_status == "incomplete"


def test_store_ignores_model_supplied_recording_success(store):
    sid, snapshot = finished_snapshot(store)
    report = build_report(snapshot, sid).model_dump(mode="json")
    report["recording"].update(artifact_status="metadata_verified", provenance="persisted_manifest",
        cleanup_status="confirmed", segments=[{"consent_epoch": 0, "artifact_status": "metadata_verified",
        "terminal_status": "complete", "started_at_ns": 123}])
    assert store.put_report("t1", sid, report, expected_revision=1)
    saved = store.get_report("t1", sid)
    assert saved["recording"]["artifact_status"] == "unavailable"
    assert saved["recording"]["segments"] == []


def test_delivery_helper_projects_legacy_without_mutating_original():
    from beep_agent.reports import report_for_delivery
    original = build_report(synthetic_snapshot(), "synthetic-legacy").model_dump(mode="json")
    original.pop("recording")
    original["status"] = "complete"
    copy = json.loads(json.dumps(original))
    delivered = report_for_delivery(original)
    assert delivered["status"] == "partial" and delivered["recording"]["provenance"] == "missing_manifest"
    assert original == copy


def test_source_supported_no_project_remains_valid_in_recording_partial():
    report = build_report(synthetic_snapshot(), "synthetic-no-project")
    assert report.status == "partial"
    assert report.internal_opportunity["status"] == "no_project"
    assert report.internal_opportunity["candidate_claim_ids"] == []
