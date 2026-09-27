"""Pinned LiveKit storage location formatting; exact object reads remain unchanged."""
import pytest

from beep_agent.recording import RecordingError
from test_production_recording_identity import completed_info, reserved_session, service_with_head


@pytest.mark.parametrize("base", [
    "http://seaweed:8333/synthetic-private",
    "https://seaweed:8333/synthetic-private",
    "https://127.0.0.1:1/synthetic-private",
    "https://s3.amazonaws.com/synthetic-private",
])
def test_documented_location_forms_still_read_only_exact_reserved_object(monkeypatch, base):
    service, calls = service_with_head(monkeypatch)
    service.settings = service.settings.model_copy(update={"s3_egress_endpoint": "http://seaweed:8333"})
    if base == "https://s3.amazonaws.com/synthetic-private":
        service.settings = service.settings.model_copy(update={"s3_endpoint": "", "s3_egress_endpoint": ""})
    original_endpoints = (service.settings.s3_endpoint, service.settings.s3_egress_endpoint)
    session = reserved_session()
    info = completed_info(session)
    key = info.file_results[0].filename
    info.file_results[0].location = base + "/" + key
    manifest = service._verify_files(info, session)
    assert calls == [{"Bucket": "synthetic-private", "Key": key}]
    assert manifest.artifact_status == "metadata_verified"
    assert manifest.media_validation == "not_performed"
    assert (service.settings.s3_endpoint, service.settings.s3_egress_endpoint) == original_endpoints


@pytest.mark.parametrize("bad", ["host", "port", "bucket", "key", "userinfo", "query", "fragment"])
def test_location_compatibility_does_not_accept_contradictory_identity(monkeypatch, bad):
    service, calls = service_with_head(monkeypatch)
    service.settings = service.settings.model_copy(update={"s3_egress_endpoint": "http://seaweed:8333"})
    session = reserved_session()
    info = completed_info(session)
    key = info.file_results[0].filename
    location = "https://seaweed:8333/synthetic-private/" + key
    if bad == "host":
        location = location.replace("seaweed:", "seaweed.example.invalid:")
    elif bad == "port":
        location = location.replace(":8333/", ":8334/")
    elif bad == "bucket":
        location = location.replace("synthetic-private/", "other-private/")
    elif bad == "key":
        location += ".different"
    elif bad == "userinfo":
        location = location.replace("https://", "https://synthetic@")
    elif bad == "query":
        location += "?extra=synthetic"
    else:
        location += "#synthetic"
    info.file_results[0].location = location
    with pytest.raises(RecordingError, match="contradicts"):
        service._verify_files(info, session)
    assert calls == []
