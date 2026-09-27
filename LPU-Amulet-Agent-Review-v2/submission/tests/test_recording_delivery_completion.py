"""Decode delivery regressions: synthetic sources and isolated *_test PostgreSQL."""
import json

import pytest

import test_recording_decode as decoding
import test_store
from beep_agent.domain import Report, Snapshot
from beep_agent.reports import build_report, render_report_html
from beep_agent.worker import DurableWorker
from test_production_recording_identity import EgressReadback, completed_info
from test_recording_decode import bound_service
from test_reports import synthetic_snapshot


def test_sufficient_content_eligibility_survives_recording_gate_roundtrip():
    report = build_report(synthetic_snapshot(), "synthetic-complete-content")
    assert report.status == "partial"  # No media receipt yet.
    assert getattr(report, "content_status", report.status) == "complete"
    restored = Report.model_validate_json(report.model_dump_json())
    assert restored.content_status == "complete"
    interrupted = build_report(synthetic_snapshot(), "synthetic-interrupted", partial=True)
    assert interrupted.content_status == "partial"
    assert Report.model_validate_json(interrupted.model_dump_json()).content_status == "partial"


@pytest.fixture(scope="module")
def media_files(tmp_path_factory):
    return decoding.media_files.__wrapped__(tmp_path_factory)


@pytest.fixture
def store():
    yield from test_store.store.__wrapped__()


def reserved_delivery(store, *, partial=False):
    session = test_store.activate(store)
    sid = session["id"]
    start = store.claim_job("synthetic-decode-start")
    with store.job_lease(start["id"], start["lease_token"]):
        session = store.set_recording("t1", sid, "starting")
        store.set_recording("t1", sid, "recording", "EG-synthetic")
    store.finish_job(start["id"], start["lease_token"], {"recording_storage_binding": {
        "reservation_id": session["recording_reservation"]["id"], "bucket": "synthetic-private"}})
    snap = synthetic_snapshot(revision=1, **({"coverage": ["workflow"]} if partial else {}))
    for event in snap.evidence:
        store.append_event("t1", sid, event.model_dump(mode="json"))
    assert store.save_snapshot("t1", sid, snap.model_dump(mode="json"), expected_revision=0, consent_epoch=0)
    store.control("t1", sid, "client", "finish")
    for _ in range(10):
        job = store.claim_job("synthetic-decode-cleanup")
        assert job is not None
        if job["kind"] == "recording_stop":
            return session, job, Snapshot.model_validate(store.load_snapshot("t1", sid))
        store.finish_job(job["id"], job["lease_token"], {"status": "synthetic-no-inference"})
    raise AssertionError("cleanup was not claimable")


async def decode_cleanup(store, monkeypatch, path, *, partial=False):
    session, job, snapshot = reserved_delivery(store, partial=partial)
    service, boundary, manifest = bound_service(monkeypatch, path, session=session)
    info = completed_info(session)
    info.file_results[0].size = manifest.size_bytes
    fake = EgressReadback([info])
    monkeypatch.setattr(service, "_api", lambda: fake)
    worker = DurableWorker(service.settings, store=store, recording=service)
    with store.job_lease(job["id"], job["lease_token"]):
        result = await worker._recording(job, store.get_session("t1", session["id"]))
    assert store.finish_job(job["id"], job["lease_token"], result)
    assert not fake.stops
    return session, job, snapshot, boundary, result


async def test_real_verified_media_and_sufficient_coverage_deliver_complete(store, monkeypatch, media_files):
    session, job, snapshot, boundary, result = await decode_cleanup(store, monkeypatch, media_files / "av.mp4")
    assert result["recording_manifests"][0]["media_validation"] == "verified"
    assert len(boundary.gets) == 1  # Full decoding only once, after final Egress readback.
    with store._connect() as conn:
        saved = conn.execute("SELECT result FROM beep_jobs WHERE id=%s", (job["id"],)).fetchone()["result"]
    assert saved["recording_manifests"] == result["recording_manifests"]
    report = build_report(snapshot, session["id"])
    assert report.status == "partial" and report.content_status == "complete"
    assert store.put_report("t1", session["id"], report.model_dump(mode="json"), expected_revision=1)
    delivered = store.get_report("t1", session["id"])
    assert delivered["status"] == "complete"
    assert store.get_session("t1", session["id"])["status"] == "completed"
    assert delivered["recording"]["media_validation"] == "verified"
    assert delivered["recording"]["segments"][0]["media_validation"] == "verified"
    portable = Report.model_validate(delivered)
    assert Report.model_validate_json(portable.model_dump_json()).status == "complete"
    html = render_report_html(portable)
    assert "Full audio/video decode verified" in html
    assert "correspondence" in html and "unverified" in html
    assert "Playback has not been validated" not in html
    assert not any("recording-incomplete" in gap for gap in portable.unknowns)
    client_json = portable.model_dump_json(exclude={"internal_opportunity"})
    for private in ("synthetic-private", "EG-synthetic", session["recording_reservation"]["id"],
            "synthetic-v1", result["recording_manifests"][0]["validation_receipt"]["sha256"]):
        assert private not in html and private not in client_json
    assert delivered["evidence"] == snapshot.model_dump(mode="json")["evidence"]


@pytest.mark.parametrize("name", ["wrong", "truncated", "silent", "video", "audio"])
async def test_decode_failure_is_durable_cleanup_not_provider_replay(store, monkeypatch, media_files, name):
    session, job, snapshot, boundary, result = await decode_cleanup(store, monkeypatch, media_files / (name + ".mp4"))
    assert result["status"] == "cleanup_complete_decode_failed"
    assert result["recording_manifests"][0]["media_validation"] == "failed"
    assert len(boundary.gets) == 1
    with store._connect() as conn:
        reserved = conn.execute("SELECT settled,data FROM beep_recording_reservations WHERE id=%s",
            (session["recording_reservation"]["id"],)).fetchone()
        saved = conn.execute("SELECT result FROM beep_jobs WHERE id=%s", (job["id"],)).fetchone()["result"]
    assert reserved == {"data": session["recording_reservation"], "settled": True}
    assert saved["recording_manifests"][0]["validation_receipt"]["outcome"] == "failed"
    assert not store.get_session("t1", session["id"])["recording_cleanup_pending"]
    assert store.put_report("t1", session["id"], build_report(snapshot, session["id"]).model_dump(mode="json"), expected_revision=1)
    delivered = store.get_report("t1", session["id"])
    assert delivered["status"] == "partial" and delivered["recording"]["media_validation"] == "failed"
    assert delivered["recording"]["segments"][0]["validation_failure"]


@pytest.mark.parametrize("partial_source", ["coverage", "interrupted", "legacy"])
async def test_verified_media_never_promotes_partial_discovery(store, monkeypatch, media_files, partial_source):
    session, job, snapshot, _, result = await decode_cleanup(store, monkeypatch, media_files / "av.mp4",
        partial=partial_source == "coverage")
    assert result["recording_manifests"][0]["media_validation"] == "verified"
    report = build_report(snapshot, session["id"], partial=partial_source == "interrupted").model_dump(mode="json")
    if partial_source == "coverage":
        # Even a forged complete flag and erased gaps cannot overrule stored coverage.
        report.update(content_status="complete", status="complete", unknowns=[])
    if partial_source == "legacy":
        report.pop("content_status")
        report["status"] = "complete"
    assert store.put_report("t1", session["id"], report, expected_revision=1)
    delivered = store.get_report("t1", session["id"])
    assert delivered["status"] == "partial"
    assert delivered["recording"]["media_validation"] == "verified"


async def test_corrupt_private_receipt_is_partial_not_error(store, monkeypatch, media_files):
    from psycopg.types.json import Jsonb
    session, job, snapshot, _, result = await decode_cleanup(store, monkeypatch, media_files / "av.mp4")
    result["recording_manifests"][0]["validation_receipt"]["sha256"] = "broken"
    with store._connect() as conn:
        conn.execute("UPDATE beep_jobs SET result=%s WHERE id=%s", (Jsonb(result), job["id"]))
    assert store.put_report("t1", session["id"], build_report(snapshot, session["id"]).model_dump(mode="json"), expected_revision=1)
    delivered = store.get_report("t1", session["id"])
    assert delivered["status"] == "partial"
    assert delivered["recording"]["media_validation"] != "verified"


def test_corrupt_legacy_delivery_media_projection_fails_closed():
    from beep_agent.reports import report_for_delivery
    report = build_report(synthetic_snapshot(), "synthetic-corrupt").model_dump(mode="json")
    report.update(status="complete", recording={"media_validation": "verified", "segments": "corrupt"})
    original = json.dumps(report, sort_keys=True)
    delivered = report_for_delivery(report)
    assert delivered["status"] == "partial"
    assert delivered["recording"]["media_validation"] == "not_performed"
    assert json.dumps(report, sort_keys=True) == original


async def test_decode_io_releases_db_lock_and_heartbeat_keeps_lease(store, monkeypatch, media_files):
    import asyncio
    import threading
    session, job, snapshot = reserved_delivery(store)
    sid = session["id"]
    service, boundary, manifest = bound_service(monkeypatch, media_files / "av.mp4", session=session)
    info = completed_info(session)
    info.file_results[0].size = manifest.size_bytes
    monkeypatch.setattr(service, "_api", lambda: EgressReadback([info]))
    entered, release = threading.Event(), threading.Event()
    def during_download(_):
        # Independent real connection: failure to acquire NOWAIT proves an IO lock leak.
        with store._connect() as conn:
            assert conn.execute("SELECT id FROM beep_sessions WHERE id=%s FOR UPDATE NOWAIT", (sid,)).fetchone()
        entered.set()
        assert release.wait(5)
    boundary.on_get = during_download
    worker = DurableWorker(service.settings, store=store, recording=service, lease_seconds=2)
    renewals = []
    renew = store.renew_job
    def observed_renew(*args, **kwargs):
        result = renew(*args, **kwargs)
        renewals.append(result)
        return result
    monkeypatch.setattr(store, "renew_job", observed_renew)
    with store.job_lease(job["id"], job["lease_token"]):
        watcher = asyncio.create_task(worker._watch_lease(job))
        task = asyncio.create_task(worker._recording(job, store.get_session("t1", sid)))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            async with asyncio.timeout(4):
                while not renewals:
                    await asyncio.sleep(0.05)
            assert all(renewals)
            release.set()
            result = await task
        finally:
            release.set()
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
    assert result["recording_manifests"][0]["media_validation"] == "verified"


async def test_lease_loss_after_download_cannot_settle_or_publish(store, monkeypatch, media_files):
    from beep_agent.store import StoreError
    session, job, _ = reserved_delivery(store)
    sid = session["id"]
    service, boundary, manifest = bound_service(monkeypatch, media_files / "av.mp4", session=session)
    info = completed_info(session)
    info.file_results[0].size = manifest.size_bytes
    fake = EgressReadback([info])
    monkeypatch.setattr(service, "_api", lambda: fake)
    def lose_lease(_):
        with store._connect() as conn:
            conn.execute("UPDATE beep_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s", (job["id"],))
    boundary.on_get = lose_lease
    worker = DurableWorker(service.settings, store=store, recording=service)
    with store.job_lease(job["id"], job["lease_token"]), pytest.raises(StoreError, match="lease lost"):
        await worker._recording(job, store.get_session("t1", sid))
    with store._connect() as conn:
        assert not conn.execute("SELECT settled FROM beep_recording_reservations WHERE id=%s",
            (session["recording_reservation"]["id"],)).fetchone()["settled"]
        result = conn.execute("SELECT result FROM beep_jobs WHERE id=%s", (job["id"],)).fetchone()["result"]
        assert not result or not result.get("recording_manifests")
    assert not fake.stops
    assert store.get_report("t1", sid) is None
    assert store.get_session("t1", sid)["recording_cleanup_pending"]


async def test_old_epoch_real_decode_retains_receipt_without_relabelling_new_recorder(store, monkeypatch, media_files):
    from psycopg.types.json import Jsonb
    session, job, _ = reserved_delivery(store)
    sid = session["id"]
    service, boundary, manifest = bound_service(monkeypatch, media_files / "av.mp4", session=session)
    info = completed_info(session)
    info.file_results[0].size = manifest.size_bytes
    monkeypatch.setattr(service, "_api", lambda: EgressReadback([info]))
    newer = {}
    def advance_epoch(_):
        with store._connect() as conn:
            row = store._row(conn, "t1", sid, lock=True)["data"]
            newer.update({**row, "consent_epoch": 2, "recording_epoch": 2,
                "recording_reservation": {"id": "new-reservation"}, "egress_id": "EG-new", "recording_status": "recording"})
            conn.execute("UPDATE beep_sessions SET data=%s WHERE id=%s", (Jsonb(newer), sid))
    boundary.on_get = advance_epoch
    worker = DurableWorker(service.settings, store=store, recording=service)
    with store.job_lease(job["id"], job["lease_token"]):
        result = await worker._recording(job, store.get_session("t1", sid))
    assert store.get_session("t1", sid) == newer
    with store._connect() as conn:
        saved = conn.execute("SELECT result FROM beep_jobs WHERE id=%s", (job["id"],)).fetchone()["result"]
    assert saved["recording_manifests"][0]["media_validation"] == "verified"
    assert result["recording_manifests"][0]["consent_epoch"] == 0


@pytest.mark.parametrize("fence", ["lease", "epoch"])
async def test_complete_report_still_requires_current_publication_fences(store, monkeypatch, media_files, fence):
    from beep_agent.store import StoreError
    session, _, snapshot, _, _ = await decode_cleanup(store, monkeypatch, media_files / "av.mp4")
    sid = session["id"]
    for _ in range(10):
        job = store.claim_job("synthetic-report-owner")
        assert job is not None
        if job["kind"] == "report":
            break
        store.finish_job(job["id"], job["lease_token"], {})
    else:
        raise AssertionError("report not claimable")
    epoch = store.get_session("t1", sid)["consent_epoch"]
    if fence == "lease":
        with store._connect() as conn:
            conn.execute("UPDATE beep_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s", (job["id"],))
    bundle = {"report": build_report(snapshot, sid).model_dump(mode="json"), "expected_revision": 1,
              "consent_epoch": epoch - 1 if fence == "epoch" else epoch}
    with pytest.raises(StoreError, match="fence lost"):
        store.commit_report(job["id"], job["lease_token"], "t1", sid, bundle)
    assert store.get_report("t1", sid) is None


@pytest.mark.parametrize("gap", ["none", "pending", "missing", "unverified", "duplicate", "evidence_epoch"])
async def test_every_reserved_segment_and_evidence_epoch_required(monkeypatch, media_files, gap):
    from beep_agent.reports import recording_provenance
    from test_production_recording_identity import reserved_session
    session = reserved_session()
    service, _, manifest = bound_service(monkeypatch, media_files / "av.mp4", session=session)
    first = await service._verify_media(manifest)
    second_session = {**session, "recording_reservation": {**session["recording_reservation"],
        "id": "reserved2", "consent_epoch": 1,
        "output_prefix": "beep/tenant-1/session-1/epoch-1-reserved2"}}
    service2, _, manifest2 = bound_service(monkeypatch, media_files / "av.mp4", session=second_session)
    second = await service2._verify_media(manifest2)
    receipts = [first, second]
    records = [{"data": s["recording_reservation"], "settled": True} for s in (session, second_session)]
    snapshot = synthetic_snapshot()
    if gap == "pending":
        records[1]["settled"] = False
    if gap == "missing":
        receipts.pop()
    if gap == "unverified":
        receipts[1] = manifest2
    if gap == "duplicate":
        receipts.append(first)
    if gap == "evidence_epoch":
        snapshot.evidence[0].consent_epoch = 3
    projection = recording_provenance(session, snapshot.evidence, records, receipts)
    assert projection.decode_verified == (gap == "none")
    assert projection.required_segments == 2


async def test_portable_report_uses_actual_evidence_epochs_not_supplied_projection(monkeypatch, media_files):
    from beep_agent.reports import recording_provenance
    from test_production_recording_identity import reserved_session
    session = reserved_session()
    service, _, manifest = bound_service(monkeypatch, media_files / "av.mp4", session=session)
    verified = await service._verify_media(manifest)
    snapshot = synthetic_snapshot()
    projection = recording_provenance(session, snapshot.evidence,
        [{"data": session["recording_reservation"], "settled": True}], [verified])
    report = build_report(snapshot, "synthetic-epoch-mismatch").model_dump(mode="json")
    report["recording"] = projection.model_dump(mode="json")
    report["evidence"][0]["consent_epoch"] = 3
    delivered = Report.model_validate(report)
    assert delivered.status == "partial"
    assert 3 in delivered.recording.unverified_evidence_epochs
