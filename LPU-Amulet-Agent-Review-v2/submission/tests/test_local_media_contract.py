"""Isolate the real-media harness SDK-client lifetime without RTC or S3 I/O."""
import ast
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from beep_agent.config import Settings
from beep_agent.recording import RecordingService


def test_real_media_download_closes_real_botocore_client(monkeypatch):
    tree = ast.parse(Path(__file__).with_name("test_local_media.py").read_text())
    scopes = [node for node in ast.walk(tree) if isinstance(node, ast.With)
              and any("service._s3()" in ast.unparse(item.context_expr) for item in node.items)]
    assert scopes, "Harness must explicitly own and close its S3 client"
    service = RecordingService(Settings(s3_endpoint="http://127.0.0.1:1", s3_region="us-east-1",
        s3_access_key="synthetic-only", s3_secret_key="synthetic-only"))
    client = service._s3()  # Actual boto3/botocore object, no external call.
    assert not hasattr(client, "__enter__"), "SDK contract changed; review this regression"
    closed = []
    original_close = client.close

    def close():
        original_close()
        closed.append(True)

    monkeypatch.setattr(client, "close", close)
    for scope in scopes:
        # Run the exact lifetime clause; skip its network body in this focused test.
        scope.body = [ast.Pass()]
        module = ast.fix_missing_locations(ast.Module(body=[scope], type_ignores=[]))
        try:
            exec(compile(module, "real_media_s3_scope", "exec"),
                 {"service": SimpleNamespace(_s3=lambda: client), "closing": closing})
            assert closed, "Real-media acceptance leaked its boto3 client"
        finally:
            original_close()
