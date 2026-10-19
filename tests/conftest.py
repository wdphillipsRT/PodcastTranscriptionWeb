"""Synthetic application settings shared by isolated web tests."""

import importlib

import pytest


@pytest.fixture
def app_environment(monkeypatch):
    environment = {
        "SECRET_KEY": "test-only-factory-secret-" + "s" * 32,
        "DATABASE_URL": "postgresql+psycopg2://test:test@postgres.invalid/podcasts",
        "REDIS_URL": "redis://redis.invalid:6379/0",
        "AWS_REGION": "us-east-1",
        "AWS_S3_BUCKET_NAME": "podcast-transcriptions-web-2025",
        "YOUTUBE_API_KEY": "test-only-youtube-key",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    return environment


@pytest.fixture
def app_factory(app_environment):
    def create_test_app(**overrides):
        module = importlib.import_module("app")
        factory = getattr(module, "create_app", None)
        assert callable(factory), "app must export create_app()"
        config = {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "SESSION_COOKIE_SECURE": False,
            "REMEMBER_COOKIE_SECURE": False,
            "WTF_CSRF_ENABLED": False,
            "RATELIMIT_ENABLED": False,
            "RATELIMIT_STORAGE_URI": "memory://",
        }
        config.update(overrides)
        return factory(config)

    return create_test_app


@pytest.fixture
def app(app_factory):
    return app_factory()


@pytest.fixture
def client(app):
    return app.test_client()
