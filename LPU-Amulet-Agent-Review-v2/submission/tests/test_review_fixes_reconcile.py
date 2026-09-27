"""Official SDK HTTP-loopback cleanup probes (synthetic media only)."""
import pytest
from livekit import api
from test_recording import provider_http, protobuf, recording_settings, synthetic_session
from beep_agent.recording import RecordingService, RecordingError


@pytest.mark.parametrize("present", [True, False])
async def test_reconcile_unknown_id_stops_only_exact_reserved_output(present):
    session = synthetic_session()
    reservation = dict(id="reserve1", consent_epoch=0, room_name=session["room_name"],
                       output_prefix="beep/tenant-1/session-1/epoch-0-reserve1")
    stopped = []
    media = b"synthetic-media"
    def info(eid, prefix, status=api.EGRESS_ACTIVE):
        return api.EgressInfo(egress_id=eid, room_name=session["room_name"], status=status,
            room_composite=api.RoomCompositeEgressRequest(room_name=session["room_name"], file_outputs=[api.EncodedFileOutput(
                filepath=prefix + ".mp4", s3=api.S3Upload(bucket="synthetic-private"))]),
            file_results=[api.FileInfo(filename=prefix + ".mp4", size=len(media))])
    def responder(method, path, body):
        if method == "HEAD":
            return 200, "video/mp4", media
        if path.endswith("ListEgress"):
            req = api.ListEgressRequest.FromString(body)
            items = [info("EG-other", "beep/tenant-1/session-1/epoch-1-other")]
            if present:
                items.append(info("EG-lost", reservation["output_prefix"], api.EGRESS_COMPLETE if stopped else api.EGRESS_ACTIVE))
            if req.egress_id:
                items = [i for i in items if i.egress_id == req.egress_id]
            return protobuf(api.ListEgressResponse(items=items))
        assert path.endswith("StopEgress"), "Reconciliation must never start recording"
        eid = api.StopEgressRequest.FromString(body).egress_id
        assert eid == "EG-lost"
        stopped.append(eid)
        return protobuf(info(eid, reservation["output_prefix"], api.EGRESS_COMPLETE))
    with provider_http(responder) as (url, calls):
        service = RecordingService(recording_settings(url))
        if present:
            result = await service.reconcile_stop(session, reservation)
            assert result == ["EG-lost"]
            assert result.manifests[0].media_validation == "failed"
            assert result.manifests[0].validation_receipt.failure_code == "identity_unbound"
            assert stopped == ["EG-lost"]
        else:
            with pytest.raises(RecordingError, match="not confirmed"):
                await service.reconcile_stop(session, reservation)
        assert not any(path.endswith("StartRoomCompositeEgress") for _, path, _ in calls)


async def test_start_uses_durable_reservation_output_not_new_random_path():
    session = synthetic_session()
    prefix = "beep/tenant-1/session-1/epoch-0-reserve1"
    session["recording_reservation"] = dict(id="reserve1", consent_epoch=0,
        room_name=session["room_name"], output_prefix=prefix)
    def responder(method, path, body):
        if path.endswith("CreateRoom"):
            return protobuf(api.Room(name=session["room_name"]))
        if path.endswith("ListRooms"):
            return protobuf(api.ListRoomsResponse(rooms=[api.Room(name=session["room_name"])]))
        if path.endswith("ListEgress"):
            return protobuf(api.ListEgressResponse())
        req = api.RoomCompositeEgressRequest.FromString(body)
        return protobuf(api.EgressInfo(egress_id="EG-new", room_name=req.room_name, status=api.EGRESS_ACTIVE))
    with provider_http(responder) as (url, calls):
        assert await RecordingService(recording_settings(url)).start(session) == "EG-new"
        starts = [api.RoomCompositeEgressRequest.FromString(body) for _, path, body in calls
                  if path.endswith("StartRoomCompositeEgress")]
        assert len(starts) == 1 and starts[0].file_outputs[0].filepath == prefix + ".mp4"


@pytest.mark.parametrize("case", ["active", "wrong-id", "wrong-room", "wrong-prefix", "failed", "missing"])
async def test_active_monitor_requires_exact_sdk_evidence(case):
    session = synthetic_session()
    prefix = "beep/tenant-1/session-1/epoch-0-reserve1"
    session.update(egress_id="EG-current", recording_status="recording", recording_reservation=dict(
        id="reserve1", consent_epoch=0, room_name=session["room_name"], output_prefix=prefix))
    def responder(method, path, body):
        assert path.endswith("ListEgress")
        assert api.ListEgressRequest.FromString(body).egress_id == "EG-current"
        item = api.EgressInfo(egress_id="EG-wrong" if case == "wrong-id" else "EG-current",
            room_name="wrong" if case == "wrong-room" else session["room_name"],
            status=api.EGRESS_FAILED if case == "failed" else api.EGRESS_ACTIVE,
            room_composite=api.RoomCompositeEgressRequest(file_outputs=[api.EncodedFileOutput(
                filepath=(prefix + "wrong" if case == "wrong-prefix" else prefix) + ".mp4",
                s3=api.S3Upload(bucket="synthetic-private"))]))
        return protobuf(api.ListEgressResponse(items=[] if case == "missing" else [item]))
    with provider_http(responder) as (url, calls):
        service = RecordingService(recording_settings(url))
        if case == "active":
            assert await service.check_active(session) is None
        else:
            with pytest.raises(RecordingError):
                await service.check_active(session)
        assert len(calls) == 1
