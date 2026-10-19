"""Worker application/engine ownership without external service connections."""

import os
from unittest.mock import Mock

from flask import Flask, current_app, has_app_context
import pytest

import app as application
from extensions import db
import tasks


@pytest.fixture
def worker_factory(monkeypatch):
    # Reset the process-local cache without constructing an application eagerly.
    monkeypatch.setattr(tasks, "_worker_application", None, raising=False)

    def create_app():
        app = Flask("task-lifecycle-test")
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        db.init_app(app)
        return app

    factory = Mock(side_effect=create_app)
    monkeypatch.setattr(application, "create_app", factory)
    return factory


def test_sequential_task_contexts_reuse_one_application_and_engine(worker_factory):
    apps, engines, sessions = [], [], []

    for _ in range(3):
        assert not has_app_context()
        with tasks.application_context():
            apps.append(current_app._get_current_object())
            engines.append(db.engine)
            sessions.append(db.session())
        assert not has_app_context()

    assert worker_factory.call_count == 1
    assert all(app is apps[0] for app in apps)
    assert all(engine is engines[0] for engine in engines)
    assert len({id(session) for session in sessions}) == 3


def test_pid_change_creates_a_child_application_then_reuses_it(worker_factory, monkeypatch):
    monkeypatch.setattr(os, "getpid", lambda: 1001)
    with tasks.application_context():
        parent_app = current_app._get_current_object()
        parent_engine = db.engine
    dispose = Mock(wraps=parent_engine.dispose)
    monkeypatch.setattr(parent_engine, "dispose", dispose)

    monkeypatch.setattr(os, "getpid", lambda: 1002)
    for _ in range(2):
        with tasks.application_context():
            assert current_app._get_current_object() is not parent_app
            assert db.engine is not parent_engine
            child_app = current_app._get_current_object()

    assert worker_factory.call_count == 2
    dispose.assert_called_once_with(close=False)
    with tasks.application_context():
        assert current_app._get_current_object() is child_app
    assert worker_factory.call_count == 2


@pytest.mark.parametrize("raises", [False, True], ids=["normal", "exception"])
def test_task_context_removes_scoped_session_on_exit(worker_factory, monkeypatch, raises):
    remove = Mock(wraps=db.session.remove)
    monkeypatch.setattr(db.session, "remove", remove)
    scope_keys = []

    def execute():
        with tasks.application_context():
            db.session()
            assert db.session.registry.has()
            scope_keys.append(db.session.registry.scopefunc())
            if raises:
                raise RuntimeError("task failed")

    if raises:
        with pytest.raises(RuntimeError, match="task failed"):
            execute()
    else:
        execute()

    assert not has_app_context()
    remove.assert_called_once_with()
    assert scope_keys[0] not in db.session.registry.registry


def test_existing_application_context_is_preserved(worker_factory):
    outer_app = Flask("existing-task-context")
    outer_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    db.init_app(outer_app)

    with outer_app.app_context():
        session = db.session()
        with tasks.application_context():
            assert current_app._get_current_object() is outer_app
            assert db.session() is session
        assert db.session() is session

    worker_factory.assert_not_called()
