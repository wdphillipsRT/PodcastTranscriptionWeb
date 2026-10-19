"""Factory construction and imports must never contact external services."""

import importlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import textwrap

import pytest
from flask import Flask, request, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.engine import Engine


@pytest.fixture(autouse=True)
def forbid_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Factory tests must not connect to networks or databases")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(Engine, "connect", forbidden)


def test_import_app_does_not_construct_app_or_load_settings_or_services():
    script = textwrap.dedent("""\
        import importlib
        import socket
        from unittest.mock import patch
        from flask import Flask
        from flask_sqlalchemy import SQLAlchemy
        from sqlalchemy.engine import Engine
        import config
        import dotenv

        def forbidden(*args, **kwargs):
            raise AssertionError('Import must not construct apps, load settings, or use services')

        with patch.object(Flask, '__init__', forbidden), \\
             patch.object(config, 'load_settings', forbidden), \\
             patch.object(dotenv, 'load_dotenv', forbidden), \\
             patch.object(socket.socket, 'connect', forbidden), \\
             patch.object(socket, 'create_connection', forbidden), \\
             patch.object(Engine, 'connect', forbidden), \\
             patch.object(SQLAlchemy, 'create_all', forbidden):
            module = importlib.import_module('app')
            assert callable(getattr(module, 'create_app', None))
            assert not isinstance(getattr(module, 'app', None), Flask)
    """)
    environment = dict(os.environ)
    for name in ("SECRET_KEY", "DATABASE_URL", "REDIS_URL", "AWS_REGION",
                 "AWS_S3_BUCKET_NAME", "YOUTUBE_API_KEY"):
        environment.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_healthz_returns_exact_response_without_services(app_factory):
    app = app_factory()
    response = app.test_client().get("/healthz")
    assert response.status_code == 200
    assert response.content_type == "application/json"
    assert response.get_json() == {"status": "ok"}


def test_factory_never_creates_tables(app_factory, monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("Schema creation must be an explicit administrative action")

    monkeypatch.setattr(SQLAlchemy, "create_all", forbidden)
    database = tmp_path / "must-not-be-created.sqlite"
    app = app_factory(SQLALCHEMY_DATABASE_URI=f"sqlite:///{database}")
    assert not database.exists()
    assert app.test_client().get("/healthz").status_code == 200
    assert not database.exists()


def test_extensions_are_initialized_on_each_independent_app(app_factory):
    from extensions import csrf, db, limiter, login_manager, migrate

    first = app_factory(RATELIMIT_ENABLED=True)
    second = app_factory(RATELIMIT_ENABLED=True)
    assert isinstance(first, Flask)
    assert first is not second
    for app in (first, second):
        assert app.extensions["sqlalchemy"] is db
        assert app.extensions["migrate"].migrate is migrate
        assert app.extensions["csrf"] is csrf
        assert limiter in app.extensions["limiter"]
        assert app.login_manager is login_manager


def test_factory_loads_validated_settings_before_overrides(app_factory, app_environment):
    app = app_factory(SECRET_KEY="explicit-override", MAX_CONTENT_LENGTH=1234)
    assert app.secret_key == "explicit-override"
    assert app.config["MAX_CONTENT_LENGTH"] == 1234
    assert app.config["AWS_S3_BUCKET_NAME"] == app_environment["AWS_S3_BUCKET_NAME"]
    assert app.config["YOUTUBE_API_KEY"] == app_environment["YOUTUBE_API_KEY"]
    assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:"


def test_secure_cookie_and_csrf_defaults_are_enabled(app_environment):
    from app import create_app

    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "RATELIMIT_STORAGE_URI": "memory://",
    })

    @app.post("/test-csrf")
    def protected_post():
        return {"accepted": True}

    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["REMEMBER_COOKIE_SECURE"] is True
    assert app.config["WTF_CSRF_ENABLED"] is True
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config["REMEMBER_COOKIE_HTTPONLY"] is True
    assert app.config["REMEMBER_COOKIE_SAMESITE"] == "Lax"
    assert app.test_client().post("/test-csrf").status_code == 400


def test_test_overrides_disable_cookie_security_csrf_and_rate_limits(app_factory):
    from extensions import limiter

    app = app_factory()

    @app.post("/test-overrides")
    @limiter.limit("1 per minute")
    def overridden_post():
        session["checked"] = True
        return {"accepted": True}

    client = app.test_client()
    for _ in range(2):
        response = client.post("/test-overrides")
        assert response.status_code == 200
    assert app.config["SESSION_COOKIE_SECURE"] is False
    assert app.config["REMEMBER_COOKIE_SECURE"] is False
    assert app.config["WTF_CSRF_ENABLED"] is False
    assert app.config["RATELIMIT_ENABLED"] is False
    assert "Secure" not in response.headers["Set-Cookie"]
    assert "HttpOnly" in response.headers["Set-Cookie"]


def test_rate_limiter_can_be_enabled_without_external_services(app_factory):
    from extensions import limiter

    app = app_factory(RATELIMIT_ENABLED=True)

    @app.get("/test-limit")
    @limiter.limit("1 per minute")
    def limited_get():
        return {"accepted": True}

    client = app.test_client()
    assert client.get("/test-limit").status_code == 200
    assert client.get("/test-limit").status_code == 429


def test_proxyfix_trusts_exactly_one_hop(app_factory):
    app = app_factory()

    @app.get("/test-proxy")
    def proxy_values():
        return {
            "remote_addr": request.remote_addr,
            "scheme": request.scheme,
            "host": request.host,
            "script_root": request.script_root,
        }

    response = app.test_client().get("/test-proxy", headers={
        "X-Forwarded-For": "198.51.100.1, 203.0.113.2",
        "X-Forwarded-Proto": "http, https",
        "X-Forwarded-Host": "spoofed.invalid, podcasts.example",
        "X-Forwarded-Port": "81, 8443",
        "X-Forwarded-Prefix": "/spoofed, /podcasts",
    })
    assert response.get_json() == {
        "remote_addr": "203.0.113.2",
        "scheme": "https",
        "host": "podcasts.example:8443",
        "script_root": "/podcasts",
    }


def test_wsgi_constructs_the_app_at_the_production_boundary(app_environment, monkeypatch):
    module = importlib.import_module("app")
    factory = getattr(module, "create_app", None)
    assert callable(factory), "app must export create_app()"
    constructed = Flask("test-production-boundary")
    calls = []

    def construct():
        calls.append(True)
        return constructed

    monkeypatch.setattr(module, "create_app", construct)
    monkeypatch.delitem(sys.modules, "wsgi", raising=False)
    try:
        assert importlib.import_module("wsgi").app is constructed
        assert calls == [True]
    finally:
        sys.modules.pop("wsgi", None)
