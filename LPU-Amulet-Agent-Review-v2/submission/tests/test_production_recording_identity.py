"""PR-03: synthetic Egress/S3 boundaries; no provider or media calls."""
from types import SimpleNamespace

import pytest
from livekit import api

from beep_agent.recording import RecordingError, RecordingService
from test_recording import recording_settings, synthetic_session


def reserved_session():
    session = synthetic_session()
    session["recording_reservation"] = {
        "id": "reserved1", "consent_epoch": 0, "room_name": session["room_name"],
        "output_prefix": "beep/tenant-1/session-1/epoch-0-reserved1",
    }
    return session


def completed_info(session=None, **changes):
    session = session or reserved_session()
    key = session["recording_reservation"]["output_prefix"] + ".mp4"
    values = dict(
        egress_id="EG-synthetic", room_name=session["room_name"], status=api.EGRESS_COMPLETE,
        started_at=1_700_000_000_000_000_000, ended_at=1_700_000_002_000_000_000,
        room_composite=api.RoomCompositeEgressRequest(room_name=session["room_name"],
            file_outputs=[api.EncodedFileOutput(filepath=key,
                s3=api.S3Upload(bucket="synthetic-private"))]),
        file_results=[api.FileInfo(filename=key, size=128, duration=2_000_000_000)],
    )
    return api.EgressInfo(**{**values, **changes})


def service_with_head(monkeypatch, *, size=128):
    service = RecordingService(recording_settings("http://127.0.0.1:1"))
    calls = []
    def head(**kwargs):
        calls.append(kwargs)
        return {"ContentLength": size, "ContentType": "video/mp4", "ETag": '"synthetic-etag"'}
    monkeypatch.setattr(service, "_s3", lambda: SimpleNamespace(head_object=head, close=lambda: None))
    return service, calls


def test_unrelated_file_is_not_verified_without_exact_reservation(monkeypatch):
    """Persistent version of the audit's broad-beep-prefix counterexample."""
    service, calls = service_with_head(monkeypatch)
    info = completed_info(file_results=[api.FileInfo(
        filename="beep/other-tenant/other-session/not-the-reservation.mp4", size=128)])
    with pytest.raises(RecordingError):
        service._verify_files(info)
    assert calls == []


@pytest.mark.parametrize("fault", ["wrong_prefix", "wrong_epoch", "empty", "zero_size",
    "wrong_size", "multiple_results", "multiple_outputs", "wrong_room", "wrong_bucket",
    "wrong_request_key", "wrong_request_room", "not_complete"])
def test_final_file_requires_exact_scope_and_size(monkeypatch, fault):
    session = reserved_session()
    service, calls = service_with_head(monkeypatch, size=127 if fault == "wrong_size" else 128)
    info = completed_info()
    if fault == "wrong_prefix":
        info.file_results[0].filename = "beep/other-tenant/other-session/file.mp4"
    elif fault == "wrong_epoch":
        info.file_results[0].filename = info.file_results[0].filename.replace("epoch-0", "epoch-1")
    elif fault == "empty":
        del info.file_results[:]
    elif fault == "zero_size":
        info.file_results[0].size = 0
    elif fault == "multiple_results":
        info.file_results.append(info.file_results[0])
    elif fault == "multiple_outputs":
        info.room_composite.file_outputs.append(info.room_composite.file_outputs[0])
    elif fault == "wrong_room":
        info.room_name = "beep-other-session"
    elif fault == "wrong_bucket":
        info.room_composite.file_outputs[0].s3.bucket = "other-private"
    elif fault == "wrong_request_key":
        info.room_composite.file_outputs[0].filepath += "-not-reserved"
    elif fault == "wrong_request_room":
        info.room_composite.room_name = "beep-other-session"
    elif fault == "not_complete":
        info.status = api.EGRESS_ACTIVE
    with pytest.raises(RecordingError):
        service._verify_files(info, session, session["recording_reservation"])
    assert len(calls) == (1 if fault == "wrong_size" else 0)


def test_exact_head_is_metadata_not_playability_and_keeps_observed_timing(monkeypatch):
    session = reserved_session()
    service, calls = service_with_head(monkeypatch)
    manifest = service._verify_files(completed_info(), session).model_dump(mode="json")
    assert manifest["consent_epoch"] == 0
    assert manifest["reservation_id"] == "reserved1"
    assert manifest["bucket"] == "synthetic-private"
    assert manifest["key"] == session["recording_reservation"]["output_prefix"] + ".mp4"
    assert manifest["artifact_status"] == "metadata_verified"
    assert manifest["media_validation"] == "not_performed"
    assert manifest["size_bytes"] == manifest["head_size_bytes"] == 128
    assert manifest["started_at_ns"] == 1_700_000_000_000_000_000
    assert manifest["ended_at_ns"] == 1_700_000_002_000_000_000
    assert manifest["duration_ns"] == 2_000_000_000
    assert manifest["file_started_at_ns"] is None
    assert calls == [{"Bucket": manifest["bucket"], "Key": manifest["key"]}]


class EgressReadback:
    def __init__(self, items):
        self.items = items
        self.stops = []
        self.egress = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def list_egress(self, request):
        return api.ListEgressResponse(items=self.items)

    async def stop_egress(self, request):
        self.stops.append(request.egress_id)
        info = next(i for i in self.items if i.egress_id == request.egress_id)
        info.status = api.EGRESS_COMPLETE
        return info


async def test_reconcile_carries_exact_manifest_after_final_readback(monkeypatch):
    service, _ = service_with_head(monkeypatch)
    fake = EgressReadback([completed_info()])
    monkeypatch.setattr(service, "_api", lambda: fake)
    result = await service.reconcile_stop(reserved_session())
    assert result == ["EG-synthetic"]
    assert len(result.manifests) == 1
    assert result.manifests[0].artifact_status == "metadata_verified"


@pytest.mark.parametrize("status", [api.EGRESS_FAILED, api.EGRESS_ABORTED, api.EGRESS_LIMIT_REACHED])
async def test_terminal_failure_is_cleanup_not_artifact_success(monkeypatch, status):
    service, calls = service_with_head(monkeypatch)
    fake = EgressReadback([completed_info(status=status, file_results=[])])
    monkeypatch.setattr(service, "_api", lambda: fake)
    result = await service.reconcile_stop(reserved_session())
    assert result.failed_ids == ["EG-synthetic"]
    assert result.manifests[0].artifact_status == "unavailable"
    assert result.manifests[0].size_bytes is None
    assert not fake.stops and not calls


async def test_unknown_listing_retains_uncertainty(monkeypatch):
    service, calls = service_with_head(monkeypatch)
    monkeypatch.setattr(service, "_api", lambda: EgressReadback([]))
    with pytest.raises(RecordingError, match="shutdown is not confirmed"):
        await service.reconcile_stop(reserved_session())
    assert not calls


async def test_reconcile_refuses_mismatched_known_id(monkeypatch):
    service, _ = service_with_head(monkeypatch)
    monkeypatch.setattr(service, "_api", lambda: EgressReadback([completed_info()]))
    session = reserved_session()
    with pytest.raises(RecordingError):
        await service.reconcile_stop(session, {"reservation": session["recording_reservation"],
                                              "egress_id": "EG-missing"})


async def test_legacy_stop_does_not_certify_unreserved_artifact(monkeypatch):
    service, calls = service_with_head(monkeypatch)
    monkeypatch.setattr(service, "_api", lambda: EgressReadback([completed_info()]))
    assert await service.stop("EG-synthetic") is None
    assert not calls


@pytest.mark.parametrize("location", ["s3://other-bucket/beep/tenant-1/session-1/epoch-0-reserved1.mp4",
    "http://127.0.0.1:1/synthetic-private/beep/other/file.mp4", "https://example.invalid/wrong.mp4"])
def test_final_location_cannot_contradict_reserved_object(monkeypatch, location):
    service, calls = service_with_head(monkeypatch)
    info = completed_info()
    info.file_results[0].location = location
    with pytest.raises(RecordingError):
        service._verify_files(info, reserved_session())
    assert not calls


def test_reserved_bucket_survives_config_change(monkeypatch):
    service, calls = service_with_head(monkeypatch)
    session = reserved_session()
    session["recording_storage_binding"] = {"reservation_id": "reserved1", "bucket": "synthetic-private"}
    service.settings.s3_bucket = "newly-configured-bucket"
    assert service._verify_files(completed_info(), session).bucket == "synthetic-private"
    assert calls[0]["Bucket"] == "synthetic-private"


async def test_changed_final_readback_cannot_publish_manifest(monkeypatch):
    service, _ = service_with_head(monkeypatch)
    fake = EgressReadback([completed_info()])
    count = 0
    async def listing(request):
        nonlocal count
        count += 1
        info = completed_info()
        if count >= 3:
            info.file_results[0].filename = "beep/other/file.mp4"
        return api.ListEgressResponse(items=[info])
    monkeypatch.setattr(fake, "list_egress", listing)
    monkeypatch.setattr(service, "_api", lambda: fake)
    with pytest.raises(RecordingError):
        await service.reconcile_stop(reserved_session())


async def test_scoped_stop_checks_room_before_mutation(monkeypatch):
    service, _ = service_with_head(monkeypatch)
    fake = EgressReadback([completed_info(status=api.EGRESS_ACTIVE, room_name="beep-wrong-room")])
    monkeypatch.setattr(service, "_api", lambda: fake)
    with pytest.raises(RecordingError):
        await service.stop("EG-synthetic", session=reserved_session())
    assert not fake.stops
