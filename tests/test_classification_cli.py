"""Owner operational commands expose only aggregate, non-private results."""
import pytest
from extensions import db
import models as m
from tests.test_classification_tasks import worker
from tests.test_classification_service import env, add_item


def invoke(worker, *args):
    return worker.app.test_cli_runner().invoke(args=["classification", *args])


def test_status_only_aggregates_without_storage(worker):
    worker.env.item.title = "PRIVATE TITLE"
    db.session.commit()
    result = invoke(worker, "status")
    assert result.exit_code == 0, result.output
    assert "missing_current_policy=1" in result.output
    assert "PRIVATE" not in result.output and "https:" not in result.output and "items/" not in result.output
    assert not worker.env.storage.reads


@pytest.mark.parametrize("limit", ["0", "101", "private-value"])
def test_backlog_rejects_invalid_limit_without_echoing_input(worker, limit):
    result = invoke(worker, "enqueue-backlog", "--limit", limit)
    assert result.exit_code != 0 and "private-value" not in result.output


def test_backlog_disabled_still_creates_bounded_work(worker):
    worker.app.config["CLASSIFICATION_ENABLED"] = False
    add_item(worker.env, external_id="other")
    result = invoke(worker, "enqueue-backlog", "--limit", "1")
    assert result.exit_code == 0, result.output
    assert "created=1" in result.output and m.ClassificationRevision.query.count() == 1
    assert not worker.sent and not worker.env.classifier.calls


def test_retry_appends_idempotently_and_can_retry_later_failure(worker):
    original = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    row = db.session.get(m.ClassificationRevision, original)
    row.state, row.error_category, row.attempts = "failed", "claude_auth", 5
    db.session.commit()
    worker.app.config["CLASSIFICATION_ENABLED"] = False
    for _ in range(2):
        result = invoke(worker, "retry", "--item-id", str(worker.env.item.id))
        assert result.exit_code == 0, result.output
    assert m.ClassificationRevision.query.count() == 2 and not worker.sent
    latest = m.ClassificationRevision.query.order_by(m.ClassificationRevision.id.desc()).first()
    assert latest.idempotency_key == f"manual_retry:{original}" and latest.attempts == 0
    latest.state, latest.error_category = "failed", "claude_config"
    db.session.commit()
    result = invoke(worker, "retry", "--item-id", str(worker.env.item.id))
    assert result.exit_code == 0 and m.ClassificationRevision.query.count() == 3
    assert row.attempts == 5 and row.state == "failed"


@pytest.mark.parametrize("arguments", [("smoke",), ("smoke", "--item-id", "1", "--oldest-completed")])
def test_smoke_requires_exactly_one_selection(worker, arguments):
    result = invoke(worker, *arguments)
    assert result.exit_code != 0 and not worker.env.classifier.calls


@pytest.mark.parametrize("selector", ["item", "oldest"])
def test_one_item_smoke_runs_synchronously_while_disabled(worker, selector):
    add_item(worker.env, external_id="unselected")
    worker.app.config["CLASSIFICATION_ENABLED"] = False
    arguments = ["--item-id", str(worker.env.item.id)] if selector == "item" else ["--oldest-completed"]
    result = invoke(worker, "smoke", *arguments)
    assert result.exit_code == 0, result.output
    assert "state=completed" in result.output and "cost" not in result.output
    assert m.ClassificationRevision.query.count() == 1
    assert not worker.sent and len(worker.env.classifier.calls) == 1
    assert worker.env.transcript.segments[0].text not in result.output


def test_cli_failure_suppresses_private_exception(worker, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE transcript https://secret.example/key")
    monkeypatch.setattr(worker.env.service, "ensure_revision", fail)
    result = invoke(worker, "smoke", "--oldest-completed")
    assert result.exit_code != 0 and "PRIVATE" not in result.output and "https:" not in result.output


from tests.router_fake import FakeRouter, fake_router  # noqa: E402,F401


def test_router_smoke_names_an_unspecified_reason(worker, fake_router, monkeypatch):  # noqa: F811
    import socket
    import router_classifier
    monkeypatch.delattr(socket.socket, "connect")
    worker.app.config.update(ROUTER_URL=fake_router.url, ROUTER_TOKEN_FILE=fake_router.token_file,
                             ROUTER_CA_FILE="", ROUTER_CLIENT_CERT_FILE="", ROUTER_CLIENT_KEY_FILE="")

    def fail(self, window, **keywords):
        raise router_classifier.ClassifierError("model_config")
    monkeypatch.setattr(router_classifier.RouterClassifier, "classify", fail)
    result = invoke(worker, "router-smoke")
    assert result.exit_code != 0
    assert "Router smoke failed: model_config reason=unspecified" in result.output


@pytest.mark.parametrize("failure", [None, "auth", "model", "not_ready", "wrong_cert"])
def test_router_smoke_sends_one_synthetic_request_through_the_production_adapter(worker, fake_router, monkeypatch,
                                                                                 failure):  # noqa: F811
    import json
    import socket
    # The worker fixture forbids socket connections; the fake router listens on loopback only.
    monkeypatch.delattr(socket.socket, "connect")
    worker.app.config.update(ROUTER_URL=fake_router.url, ROUTER_TOKEN_FILE=fake_router.token_file,
                             ROUTER_CA_FILE="", ROUTER_CLIENT_CERT_FILE="", ROUTER_CLIENT_KEY_FILE="")
    identity = "seg-000001-" + "a" * 16
    fake_router.output = {"ranges": [{"first_segment_id": identity, "last_segment_id": identity, "label": "editorial",
                                      "confidence": 1, "reason_codes": ["substantive_content"]}]}
    arguments = ("router-smoke",)
    if failure == "auth":
        fake_router.reply(401, FakeRouter.error("MODEL_UNAUTHORIZED"))
    elif failure == "model":
        fake_router.model = "other:1b"
    elif failure == "not_ready":
        fake_router.ready = False
    elif failure == "wrong_cert":
        import router_client
        used = []
        def refuse(self, *args, **kwargs):
            used.append(self.cert)
            raise router_client.RouterError("model_auth", reason="tls")
        monkeypatch.setattr(router_client.RouterClient, "run", refuse)
        arguments = ("router-smoke", "--wrong-client-cert")
    result = invoke(worker, *arguments)
    posts = [request for request in fake_router.requests if request[0] == "POST"]
    assert m.ClassificationRevision.query.count() == 0
    assert not worker.env.storage.reads and not worker.env.storage.puts
    if failure == "not_ready":
        assert result.exit_code != 0 and "Router is not ready" in result.output and not posts
    elif failure == "wrong_cert":
        assert result.exit_code == 0, result.output
        assert result.output == "wrong-client-cert=model_auth reason=tls\n"
        # A throwaway pair was used in place of the configured one, then removed.
        import os
        assert len(used) == 1 and used[0] is not None and not any(os.path.exists(path) for path in used[0])
    elif failure:
        assert result.exit_code != 0
        assert ("Router smoke failed: model_auth reason=unauthorized" if failure == "auth"
                else "Router smoke failed: model_mismatch reason=mismatch") in result.output
    else:
        assert result.exit_code == 0, result.output
        assert result.output.startswith("ready=1 model=bonsai2:27b-pq2_0 label=editorial seconds=")
        assert len(posts) == 1 and posts[0][3]["route"] == "podcast.classification"
        assert worker.env.transcript.segments[0].text not in json.dumps(posts[0][3])


@pytest.mark.parametrize("category,reason", [(None, None), ("model_auth", "token"), ("router_busy", "transport")])
def test_wrong_client_cert_smoke_fails_unless_refused_as_tls_auth(worker, fake_router, monkeypatch, category,
                                                                    reason):  # noqa: F811
    import socket
    import router_client
    monkeypatch.delattr(socket.socket, "connect")
    worker.app.config.update(ROUTER_URL=fake_router.url, ROUTER_TOKEN_FILE=fake_router.token_file,
                             ROUTER_CA_FILE="", ROUTER_CLIENT_CERT_FILE="", ROUTER_CLIENT_KEY_FILE="")
    identity = "seg-000001-" + "a" * 16
    fake_router.output = {"ranges": [{"first_segment_id": identity, "last_segment_id": identity, "label": "editorial",
                                      "confidence": 1, "reason_codes": ["substantive_content"]}]}
    if category is not None:
        def refuse(self, *args, **kwargs):
            raise router_client.RouterError(category, reason=reason)
        monkeypatch.setattr(router_client.RouterClient, "run", refuse)
    result = invoke(worker, "router-smoke", "--wrong-client-cert")
    assert result.exit_code != 0
    assert "wrong-client-cert" in result.output and "BEGIN" not in result.output


def test_throwaway_client_certificate_is_a_matching_self_signed_pair(tmp_path):
    import shutil
    import ssl
    import subprocess
    from classification_cli import _write_throwaway_client_certificate
    certificate, key = tmp_path / "client.crt", tmp_path / "client.key"
    _write_throwaway_client_certificate(certificate, key)
    assert oct(key.stat().st_mode & 0o777) == "0o600"
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_cert_chain(str(certificate), str(key))  # Raises unless the key matches the certificate.
    if shutil.which("openssl"):
        verified = subprocess.run(["openssl", "verify", "-CAfile", str(certificate), str(certificate)],
                                  capture_output=True, text=True, timeout=30)
        assert verified.returncode == 0, verified.stdout + verified.stderr


def test_status_shows_busy_revisions_and_the_oldest_busy_block(worker):
    from router_classifier import ClassifierError
    from tests.test_classification_tasks import task
    result = invoke(worker, "status")
    assert "router_busy=0 oldest_busy_key_sent=none oldest_busy_updated=none" in result.output
    identity = worker.env.service.ensure_revision(worker.env.item.id, "backfill")
    task().run(identity, "prepare")
    worker.env.classifier.error = ClassifierError("router_busy", reason="in_progress", advance_key=False)
    task().run(identity, "block", 1)
    block = m.ClassificationBlock.query.filter_by(revision_id=identity, ordinal=1).one()
    sent, updated = block.request_generation_started_at, block.updated_at
    db.session.commit()
    result = invoke(worker, "status")
    assert result.exit_code == 0, result.output
    assert (f"router_busy=1 oldest_busy_key_sent={sent.isoformat()} oldest_busy_updated={updated.isoformat()}"
            in result.output)


def test_reclassify_cancel_prints_the_number_of_cancelled_revisions(worker):
    worker.app.config["CLASSIFICATION_ENABLED"] = False
    add_item(worker.env, external_id="second")
    assert invoke(worker, "reclassify-all", "--limit", "1").exit_code == 0
    result = invoke(worker, "reclassify-cancel")
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "cancelled=0"
    assert invoke(worker, "reclassify-all").exit_code == 0
    assert invoke(worker, "reclassify-cancel").output.strip() == "cancelled=1"
