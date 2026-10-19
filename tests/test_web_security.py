"""Workspace trust boundaries: authentication, CSRF, private artifacts, errors."""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from extensions import db
from models import ContentItem
from tests.test_web_sources import workspace


def test_private_surface_has_no_public_product_claims(workspace):
    for path in ("/", "/dashboard", "/login", "/sources/1", "/items/1"):
        response = workspace.client.get(path)
        assert response.status_code == 200
        for claim in ("register", "stripe", "subscription", "mailgun", "heroku", "email notification", "sign up"):
            assert claim not in response.text.lower()
    for path in ("/register", "/subscription", "/pricing"):
        assert workspace.client.get(path).status_code == 404
    for filename in ("register.html", "subscription.html", "episodes.html", "emails/welcome.html",
                     "emails/summary_ready.html", "emails/transcript_ready.html"):
        assert not (Path(workspace.app.root_path) / "templates" / filename).exists()


def test_authenticated_mutations_require_csrf_for_form_and_json(workspace, monkeypatch):
    import tasks
    monkeypatch.setattr(tasks, "dispatch_task", lambda *a, **k: None)
    workspace.app.config["WTF_CSRF_ENABLED"] = True
    page = workspace.client.get("/dashboard")
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)
    assert match
    for path in ("/sources", "/sources/1/backfills", "/backfills/1/cancel", "/items/1/retry", "/api/sources",
                 "/items/1/classification/overrides", "/api/items/1/classification/overrides",
                 "/items/1/classification/retry", "/api/items/1/classification/retry",
                 "/api/sources/1/backfills", "/api/backfills/1/cancel", "/api/items/1/retry", "/logout"):
        for kwargs in ({"data": {}}, {"json": {}}):
            response = workspace.client.post(path, **kwargs)
            assert response.status_code == 400
            assert "CSRF" not in response.text
    assert workspace.client.post("/api/items/1/retry", headers={"X-CSRFToken": match.group(1)}, json={}).status_code != 400


def test_unauthenticated_api_mutations_return_json_401_even_with_csrf_enabled(workspace):
    workspace.app.config["WTF_CSRF_ENABLED"] = True
    client = workspace.app.test_client()
    for method, path in (("get", "/api/items/1"), ("post", "/api/sources"), ("post", "/api/items/1/retry"),
                         ("post", "/api/items/1/classification/overrides"), ("post", "/api/items/1/classification/retry")):
        response = getattr(client, method)(path)
        assert response.status_code == 401 and response.json == {"error": "Authentication required"}
    for path in ("/", "/dashboard", "/sources/1", "/items/1/transcript"):
        assert client.get(path).status_code == 302


def test_transcript_is_bounded_private_s3_read_no_store_and_escaped(workspace, monkeypatch):
    from web import routes
    seen = []
    def download(key, *, max_bytes):
        seen.append((key, max_bytes))
        return "Requested transcript <script>alert('unsafe')</script>"
    monkeypatch.setattr(routes, "get_s3_manager", lambda *a: SimpleNamespace(download_text_content=download))
    response = workspace.client.get("/items/1/transcript")
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert "Requested transcript" in response.text and "&lt;script&gt;" in response.text
    assert "<script>alert" not in response.text and "private.txt" not in response.text
    assert seen[0][0].endswith("private.txt") and 0 < seen[0][1] <= 10 * 1024 * 1024
    assert "Requested transcript" not in workspace.client.get("/api/items/1").text


def test_item_json_contains_availability_and_safe_labels_only(workspace):
    response = workspace.client.get("/api/items/1")
    assert response.status_code == 200
    assert response.json["transcript_available"] is True and response.json["media_available"] is True
    assert response.json["transcript_origin"] == "Creator captions"
    for value in ("private.txt", "private.mp3", "secret", "/tmp/", "http", "_s3_key"):
        assert value not in response.text
    with workspace.app.app_context():
        item = db.session.get(ContentItem, 1)
        item.transcription_provider, item.language = "private secret", "key=secret"
        db.session.commit()
    assert "secret" not in workspace.client.get("/api/items/1").text


def test_provider_storage_and_unhandled_errors_are_generic(workspace, monkeypatch):
    from web import routes
    secret = "secret https://private.invalid?key=api /tmp/private command output"
    def failure(*args, **kwargs):
        raise RuntimeError(secret)
    monkeypatch.setattr(routes, "resolve_source", failure)
    response = workspace.client.post("/api/sources", json={"url": "https://example.com/feed"})
    assert response.status_code == 400 and secret not in response.text
    monkeypatch.setattr(routes, "get_s3_manager", failure)
    response = workspace.client.get("/items/1/transcript")
    assert response.status_code == 503 and response.headers["Cache-Control"] == "no-store"
    assert secret not in response.text


def test_no_get_mutation_and_shared_js_csrf(workspace):
    for path in ("/logout", "/api/items/1/retry", "/api/backfills/1/cancel", "/api/sources/1/backfills",
                 "/items/1/classification/overrides", "/api/items/1/classification/overrides",
                 "/items/1/classification/retry", "/api/items/1/classification/retry"):
        assert workspace.client.get(path).status_code == 405
    script = (Path(workspace.app.root_path) / "static/js/app.js").read_text()
    assert "apiRequest" in script and "csrf-token" in script and "X-CSRFToken" in script
    assert "console." not in script


def test_unrelated_item_json_does_not_project_free_form_title_or_raw_fields(workspace):
    with workspace.app.app_context():
        item = db.session.get(ContentItem, 1)
        item.title = "Private transcript words https://signed.invalid?token=secret"
        db.session.commit()
    response = workspace.client.get("/api/items/1")
    assert "Private transcript words" not in response.text and "token=secret" not in response.text


def test_unhandled_errors_do_not_serialize_or_log_private_diagnostics(workspace, caplog):
    secret = "private key=secret /tmp/private https://signed.invalid command output"
    @workspace.app.get("/api/test-error")
    def error():
        raise RuntimeError(secret)
    response = workspace.client.get("/api/test-error")
    assert response.status_code == 500 and response.json == {"error": "Request failed"}
    assert secret not in caplog.text and secret not in response.text
