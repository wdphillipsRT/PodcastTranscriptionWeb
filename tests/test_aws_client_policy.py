"""Readiness and runtime use SDK endpoints and normal credentials, offline."""

import logging
from types import SimpleNamespace

import boto3
from botocore.httpsession import URLLib3Session
import pytest

import s3_utils
import system_checks
import transcribe


@pytest.mark.parametrize("consumer", ["readiness_s3", "readiness_transcribe", "runtime_s3", "runtime_transcribe"])
@pytest.mark.parametrize("override", ["global_environment", "service_environment", "global_shared_config", "service_shared_config"])
def test_all_aws_clients_ignore_external_endpoint_overrides(
        monkeypatch, tmp_path, capsys, caplog, consumer, override):
    service = "s3" if consumer.endswith("s3") else "transcribe"
    marker = "secret-endpoint-override"
    hostile = "https://" + marker + ".invalid"
    caplog.set_level(logging.DEBUG, logger="botocore")
    for name in ("AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3", "AWS_ENDPOINT_URL_TRANSCRIBE",
                 "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_USE_FIPS_ENDPOINT", "AWS_USE_DUALSTACK_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        "AWS_ACCESS_KEY_ID": "synthetic-key", "AWS_SECRET_ACCESS_KEY": "synthetic-private-secret",
        "AWS_SESSION_TOKEN": "synthetic-session-secret", "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "false",
        "AWS_CONFIG_FILE": str(tmp_path / "aws-config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "absent-credentials"),
    }.items():
        monkeypatch.setenv(name, value)
    shared = "[default]\nregion = us-east-1\nignore_configured_endpoint_urls = false\n"
    if override == "global_environment":
        monkeypatch.setenv("AWS_ENDPOINT_URL", hostile)
    elif override == "service_environment":
        monkeypatch.setenv("AWS_ENDPOINT_URL_" + service.upper(), hostile)
    elif override == "global_shared_config":
        shared += "endpoint_url = " + hostile + "\n"
    else:
        shared += "services = hostile\n\n[services hostile]\n" + service + " =\n  endpoint_url = " + hostile + "\n"
    (tmp_path / "aws-config").write_text(shared)
    def no_network(*args, **kwargs):
        pytest.fail("AWS endpoint tests must not contact a provider")
    monkeypatch.setattr(URLLib3Session, "send", no_network)
    session = boto3.Session()
    supplied = []
    def create_client(service_name, **kwargs):
        supplied.append(kwargs)
        return session.client(service_name, **kwargs)
    monkeypatch.setattr(boto3, "client", create_client)
    settings = SimpleNamespace(AWS_REGION="us-west-2", AWS_S3_BUCKET_NAME=s3_utils.BUCKET_NAME)
    if consumer.startswith("readiness"):
        client = system_checks._aws_client(service, vars(settings))
    elif service == "s3":
        monkeypatch.setattr(s3_utils.S3Manager, "probe", lambda self: None)
        client = s3_utils.S3Manager(settings).s3_client
    else:
        client = transcribe.get_transcribe_client(settings)
    try:
        assert client.meta.endpoint_url == f"https://{service}.us-west-2.amazonaws.com"
        assert "endpoint_url" not in supplied[0]
        assert "signature_version" not in supplied[0]["config"]._user_provided_options
        assert supplied[0]["config"].ignore_configured_endpoint_urls is True
        assert 0 < client.meta.config.connect_timeout <= 10
        assert 0 < client.meta.config.read_timeout <= 60
        assert client.meta.config.retries["total_max_attempts"] == 1
        credentials = client._request_signer._credentials.get_frozen_credentials()
        assert (credentials.access_key, credentials.secret_key, credentials.token) == (
            "synthetic-key", "synthetic-private-secret", "synthetic-session-secret")
        assert client.meta.config.signature_version in {"v4", "s3v4"}
    finally:
        client.close()
    output = capsys.readouterr()
    assert not output.out and not output.err
    assert all(value not in caplog.text for value in (
        marker, "synthetic-private-secret", "synthetic-session-secret"))
