"""Authentication uses the singleton administrator and safe browser sessions."""

import re
from urllib.parse import parse_qs, urlsplit

import pytest
from flask_login import current_user, login_required
from werkzeug.security import generate_password_hash

from extensions import db
from models import Admin


PASSWORD = "a-long-private-password"


@pytest.fixture
def auth_app(app_factory, request):
    app = app_factory(RATELIMIT_ENABLED=getattr(request, "param", False))

    @app.get("/private")
    @login_required
    def private():
        return {"email": current_user.email, "model": type(current_user._get_current_object()).__name__}

    @app.get("/api/private")
    @login_required
    def api_private():
        return {"ok": True}

    with app.app_context():
        db.create_all()
        db.session.add(Admin(email="owner@example.com", password_hash=generate_password_hash(PASSWORD)))
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


def login(client, **data):
    return client.post("/login", data={"email": "owner@example.com", "password": PASSWORD, **data})


def csrf_token(response):
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', response.text)
    assert match, response.text
    return match.group(1)


def test_login_form_is_available_without_registration(auth_app):
    response = auth_app.test_client().get("/login")
    assert response.status_code == 200
    assert 'type="password"' in response.text
    assert 'name="csrf_token"' in response.text
    assert "/register" not in response.text


def test_login_uses_admin_and_normalized_email(auth_app):
    client = auth_app.test_client()
    with client.session_transaction() as session:
        session["pre_auth_marker"] = "stale-session"
    assert login(client, email="  OWNER@Example.COM ").status_code == 302
    assert client.get("/private").get_json() == {"email": "owner@example.com", "model": "Admin"}
    with client.session_transaction() as session:
        assert "pre_auth_marker" not in session


def test_invalid_credentials_have_identical_generic_response(auth_app):
    client = auth_app.test_client()
    responses = [login(client, password="incorrect"), login(client, email="missing@example.com"),
                 login(client, email="invalid"), client.post("/login", data={})]
    assert {response.status_code for response in responses} == {401}
    assert len({response.text for response in responses}) == 1
    assert "Invalid email or password" in responses[0].text
    assert PASSWORD not in responses[0].text
    assert client.get("/private").status_code == 302


def test_no_administrator_has_same_generic_login_failure(auth_app):
    with auth_app.app_context():
        db.session.execute(db.delete(Admin))
        db.session.commit()
    response = login(auth_app.test_client())
    assert response.status_code == 401
    assert "Invalid email or password" in response.text


@pytest.mark.parametrize("auth_app", [True], indirect=True)
def test_login_has_five_attempts_per_minute_per_address(auth_app):
    client = auth_app.test_client()
    for _ in range(5):
        assert login(client, password="incorrect").status_code == 401
    assert login(client).status_code == 429
    assert client.post("/login", data={"email": "another@example.com", "password": "bad"}).status_code == 429
    assert client.post("/login", data={"email": "owner@example.com", "password": PASSWORD},
                       environ_overrides={"REMOTE_ADDR": "192.0.2.2"}).status_code == 302
    assert client.get("/login").status_code == 200


@pytest.mark.parametrize("target", ["https://evil.example/path", "//evil.example/path", "///evil.example",
                                  "/\\evil.example", "javascript:alert(1)", "relative/path",
                                  "/%2fevil.example", "/%5cevil.example", "/%0a/evil.example",
                                  "https://localhost/private", "/%252fevil.example"])
def test_login_rejects_unsafe_next(auth_app, target):
    response = login(auth_app.test_client(), next=target)
    assert response.status_code == 302
    assert response.headers["Location"] == "/"


@pytest.mark.parametrize("target", ["/private", "/private?tab=recent", "/private#details"])
def test_login_accepts_safe_relative_next(auth_app, target):
    response = login(auth_app.test_client(), next=target)
    assert response.headers["Location"] == target


def test_login_preserves_query_next(auth_app):
    response = auth_app.test_client().post("/login?next=/private", data={"email": "owner@example.com", "password": PASSWORD})
    assert response.headers["Location"] == "/private"


def test_logout_is_post_only_and_clears_authentication(auth_app):
    client = auth_app.test_client()
    assert login(client).status_code == 302
    assert client.get("/logout").status_code == 405
    assert client.get("/private").status_code == 200
    response = client.post("/logout")
    assert response.status_code == 302
    assert urlsplit(response.headers["Location"]).path == "/login"
    assert client.get("/private").status_code == 302


def test_login_and_logout_require_valid_csrf(auth_app):
    auth_app.config["WTF_CSRF_ENABLED"] = True
    client = auth_app.test_client()
    token = csrf_token(client.get("/login"))
    assert login(client).status_code == 400
    assert login(client, csrf_token=token).status_code == 302
    token = csrf_token(client.get("/login"))
    assert client.post("/logout").status_code == 400
    assert client.post("/logout", data={"csrf_token": "invalid"}).status_code == 400
    assert client.get("/private").status_code == 200
    assert client.post("/logout", data={"csrf_token": token}).status_code == 302
    assert client.get("/private").status_code == 302


@pytest.mark.parametrize("csrf_enabled", [False, True])
@pytest.mark.parametrize("method", ["get", "post"])
def test_registration_does_not_exist(auth_app, csrf_enabled, method):
    auth_app.config["WTF_CSRF_ENABLED"] = csrf_enabled
    assert getattr(auth_app.test_client(), method)("/register").status_code == 404


def test_browser_redirects_to_login_and_api_returns_json_401(auth_app):
    client = auth_app.test_client()
    response = client.get("/private?tab=recent")
    assert response.status_code == 302
    destination = urlsplit(response.headers["Location"])
    assert destination.path == "/login"
    assert parse_qs(destination.query)["next"] == ["/private?tab=recent"]
    for response in (client.get("/api/private"), client.get("/private", headers={"Accept": "application/json"})):
        assert response.status_code == 401
        assert response.is_json
        assert response.get_json() == {"error": "Authentication required"}
        assert "Location" not in response.headers


@pytest.mark.parametrize("user_id", ["garbage", "999999", "-1"])
def test_invalid_or_deleted_session_user_is_unauthenticated(auth_app, user_id):
    client = auth_app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = user_id
        session["_fresh"] = True
    assert client.get("/api/private").status_code == 401
