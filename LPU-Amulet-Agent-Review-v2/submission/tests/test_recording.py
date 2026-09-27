"""Synthetic HTTP transport fixtures ONLY; official LiveKit/boto3 SDKs run unchanged."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import time
import pytest
from livekit import api
from beep_agent.config import Settings
from beep_agent.recording import RecordingService, RecordingError


@contextmanager
def provider_http(responder):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            calls.append((self.command, self.path, body))
            status, content_type, data = responder(self.command, self.path, body)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        do_GET = do_POST
        do_HEAD = do_POST
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def recording_settings(url):
    return Settings(database_url="postgresql:///synthetic_test", admin_token="synthetic-operator-0123456789012345",
        signing_secret="synthetic-signing-0123456789012345", openai_api_key="",
        livekit_url=url, livekit_api_key="synthetic-key", livekit_api_secret="synthetic-secret-01234567890123456789",
        s3_bucket="synthetic-private", s3_endpoint=url, s3_region="us-east-1",
        s3_access_key="synthetic-s3-key", s3_secret_key="synthetic-s3-secret")


def synthetic_session():
    return dict(id="session-1", tenant_id="tenant-1", room_name="beep-session-1", consent_epoch=0,
                status="active", client_consent={"ai":True,"recording":True},
                facilitator_consent={"ai":True,"recording":True})


def protobuf(message):
    return 200, "application/protobuf", message.SerializeToString()


@pytest.mark.asyncio
async def test_recording_waits_for_active_not_start_ack_using_synthetic_http():
    acknowledged=[]
    def responder(method, path, body):
        if path.endswith("CreateRoom"):
            return protobuf(api.Room(name="beep-session-1"))
        if path.endswith("ListRooms"):
            return protobuf(api.ListRoomsResponse(rooms=[api.Room(name="beep-session-1")]))
        if path.endswith("ListEgress") and not acknowledged:
            return protobuf(api.ListEgressResponse())
        if path.endswith("StartRoomCompositeEgress"):
            request = api.RoomCompositeEgressRequest.FromString(body)
            assert request.room_name == "beep-session-1"
            output = request.file_outputs[0]
            assert output.filepath.startswith("beep/tenant-1/session-1/")
            assert output.s3.bucket == "synthetic-private" and output.s3.force_path_style
            acknowledged.append(request)
            return protobuf(api.EgressInfo(egress_id="EG_test", room_name=request.room_name, status=api.EGRESS_STARTING))
        assert path.endswith("ListEgress")
        return protobuf(api.ListEgressResponse(items=[api.EgressInfo(egress_id="EG_test", room_name="beep-session-1", status=api.EGRESS_ACTIVE, started_at=time.time_ns())]))
    with provider_http(responder) as (url, calls):
        service = RecordingService(recording_settings(url))
        service.POLL_INTERVAL = 0.001
        assert await service.start(synthetic_session()) == "EG_test"
        assert any(path.endswith("ListEgress") for _,path,_ in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("object_case", ["matching", "empty", "changed_size"])
async def test_stop_requires_complete_and_nonempty_s3_object_using_synthetic_http(object_case):
    polls = []
    media = b"synthetic-media"
    session = synthetic_session()
    reservation = dict(id="reserve1", consent_epoch=0, room_name=session["room_name"],
                       output_prefix="beep/tenant-1/session-1/epoch-0-reserve1")
    session["recording_reservation"] = reservation
    key = reservation["output_prefix"] + ".mp4"
    def responder(method, path, body):
        if method == "HEAD":
            assert path == "/synthetic-private/" + key
            data = b"" if object_case == "empty" else media + b"x" if object_case == "changed_size" else media
            return 200, "video/mp4", data
        if path.endswith("StopEgress"):
            return protobuf(api.EgressInfo(egress_id="EG_test",status=api.EGRESS_ENDING))
        polls.append(path)
        return protobuf(api.ListEgressResponse(items=[api.EgressInfo(egress_id="EG_test",
            room_name=session["room_name"], status=api.EGRESS_COMPLETE,
            room_composite=api.RoomCompositeEgressRequest(room_name=session["room_name"],
                file_outputs=[api.EncodedFileOutput(filepath=key,
                    s3=api.S3Upload(bucket="synthetic-private"))]),
            file_results=[api.FileInfo(filename=key, size=len(media))])]))
    with provider_http(responder) as (url,calls):
        service = RecordingService(recording_settings(url))
        service.POLL_INTERVAL = 0.001
        if object_case == "matching":
            manifest = await service.stop("EG_test", session=session)
            assert manifest.artifact_status == "metadata_verified"
            assert manifest.media_validation == "not_performed"  # Never label fake bytes playable.
        else:
            with pytest.raises(RecordingError, match="size does not match"):
                await service.stop("EG_test", session=session)
        assert polls and any(method == "HEAD" for method,_,_ in calls)


@pytest.mark.asyncio
async def test_delete_checks_remote_errors_and_private_prefix_using_synthetic_http():
    def responder(method, path, body):
        if "versioning" in path:
            return 200,"application/xml",b'<VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"/>'
        if method == "GET":
            assert "prefix=beep%2Ftenant-1%2Fsession-1%2F" in path
            return 200,"application/xml",b'<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>beep/tenant-1/session-1/test.mp4</Key></Contents></ListBucketResult>'
        return 200,"application/xml",b'<DeleteResult><Error><Key>beep/tenant-1/session-1/test.mp4</Key><Code>AccessDenied</Code></Error></DeleteResult>'
    with provider_http(responder) as (url,calls):
        session = {**synthetic_session(), "status":"completed"}
        with pytest.raises(RecordingError):
            await RecordingService(recording_settings(url)).delete(session)
        assert any(method == "POST" and "delete" in path for method,path,_ in calls)


@pytest.mark.asyncio
async def test_start_ensures_room_and_reconciles_lost_ack_without_duplicate_using_synthetic_http():
    recordings=[]
    rooms=[]
    starts=[]
    def responder(method,path,body):
        if path.endswith("CreateRoom"):
            req=api.CreateRoomRequest.FromString(body)
            rooms.append(api.Room(name=req.name))
            return protobuf(rooms[-1])
        if path.endswith("ListRooms"):
            return protobuf(api.ListRoomsResponse(rooms=rooms))
        if path.endswith("ListEgress"):
            return protobuf(api.ListEgressResponse(items=recordings))
        assert path.endswith("StartRoomCompositeEgress") and rooms
        req=api.RoomCompositeEgressRequest.FromString(body)
        starts.append(req)
        recordings.append(api.EgressInfo(egress_id="EG_lost_ack",room_name=req.room_name,
            room_composite=req,status=api.EGRESS_ACTIVE))
        return 500,"application/json",b'{"code":"unavailable","msg":"synthetic lost acknowledgement"}'
    with provider_http(responder) as (url,calls):
        service=RecordingService(recording_settings(url))
        assert await service.start(synthetic_session())=="EG_lost_ack"
        assert await service.start(synthetic_session())=="EG_lost_ack"
        assert len(starts)==1 and any(path.endswith("ListRooms") for _,path,_ in calls)


@pytest.mark.asyncio
async def test_failed_egress_never_becomes_recording_using_synthetic_http():
    def responder(method, path, body):
        if path.endswith("CreateRoom"):
            return protobuf(api.Room(name="beep-session-1"))
        if path.endswith("ListRooms"):
            return protobuf(api.ListRoomsResponse(rooms=[api.Room(name="beep-session-1")]))
        if path.endswith("ListEgress"):
            return protobuf(api.ListEgressResponse())
        return protobuf(api.EgressInfo(egress_id="EG_failed", room_name="beep-session-1", status=api.EGRESS_FAILED, error="synthetic private diagnostic"))
    with provider_http(responder) as (url, calls):
        with pytest.raises(RecordingError) as exc:
            await RecordingService(recording_settings(url)).start(synthetic_session())
        assert "synthetic private diagnostic" not in str(exc.value)
        assert exc.value.egress_id == "EG_failed"
