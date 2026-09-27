"""Local provisioning tests operate only inside pytest temporary directories."""
import importlib.util
import json
import stat


def test_local_setup_generates_isolated_private_credentials_without_printing(tmp_path, capsys):
    assert importlib.util.find_spec("beep_agent.localdev") is not None, "Local setup not implemented"
    from beep_agent.localdev import prepare_local
    path = prepare_local(tmp_path, rtc_ip="127.0.0.1")
    data = json.loads(path.read_text())
    assert len(data["admin_token"]) >= 32
    assert data["admin_token"] != data["signing_secret"]
    assert data["openai_api_key"] == ""
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert capsys.readouterr().out == ""
    assert data["admin_token"] not in (path.parent / "infra" / "livekit.yaml").read_text()
    again = prepare_local(tmp_path, rtc_ip="127.0.0.1")
    assert again.read_bytes() == path.read_bytes()


def test_secure_model_key_setup_preserves_config_and_never_echoes(tmp_path, monkeypatch, capsys):
    import getpass
    from beep_agent import localdev
    assert hasattr(localdev, "configure_openai"), "Secure product-key setup not implemented"
    path = localdev.prepare_local(tmp_path)
    original = json.loads(path.read_text())
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "synthetic-test-key")
    localdev.configure_openai(path)
    saved = json.loads(path.read_text())
    assert saved["openai_api_key"] == "synthetic-test-key"
    assert saved["admin_token"] == original["admin_token"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert capsys.readouterr().out == ""


def test_local_bucket_bootstrap_creates_and_reads_back_using_official_sdk():
    from beep_agent import localdev
    assert hasattr(localdev, "ensure_local_bucket")
    import boto3
    from botocore.stub import Stubber
    from beep_agent.config import Settings
    client = boto3.client("s3", endpoint_url="http://127.0.0.1:8334", region_name="us-east-1",
                          aws_access_key_id="synthetic", aws_secret_access_key="synthetic")
    settings = Settings(s3_endpoint="http://127.0.0.1:8334", s3_bucket="test-bucket")
    with Stubber(client) as stub:
        stub.add_client_error("head_bucket", service_error_code="404", http_status_code=404,
                              expected_params={"Bucket": "test-bucket"})
        stub.add_response("create_bucket", {}, {"Bucket": "test-bucket"})
        stub.add_response("head_bucket", {}, {"Bucket": "test-bucket"})
        localdev.ensure_local_bucket(settings, client=client)
        stub.assert_no_pending_responses()
