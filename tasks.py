"""ID-only task boundary; application/provider clients are created lazily."""
from contextlib import contextmanager
from types import SimpleNamespace

from celery import Celery
from flask import current_app, has_app_context

celery_app = Celery("podcast_transcription")
celery_app.config_from_object("celeryconfig")


@contextmanager
def application_context():
    if has_app_context():
        yield
    else:
        from app import create_app
        app = create_app()
        with app.app_context():
            yield


def dispatch_task(name, identity, **options):
    from discovery_service import positive_id
    positive_id(identity)
    return celery_app.send_task("tasks." + name, args=[identity], **options)


def get_discovery_service():
    from discovery_service import DiscoveryService, adapter_for_source
    from extensions import db
    settings = SimpleNamespace(**dict(current_app.config))
    return DiscoveryService(db.session, settings, lambda source: adapter_for_source(source, settings), dispatch_task)


def _execute(method, identity=None):
    if method != "check_all_sources":
        from discovery_service import positive_id
        positive_id(identity)
    with application_context():
        function = getattr(get_discovery_service(), method)
        return function() if identity is None else function(identity)


@celery_app.task(name="tasks.discover_source")
def discover_source(source_id):
    return _execute("discover_source", source_id)


@celery_app.task(name="tasks.continue_discovery")
def continue_discovery(run_id):
    return _execute("continue_discovery", run_id)


@celery_app.task(name="tasks.finalize_discovery")
def finalize_discovery(run_id):
    return _execute("finalize_discovery", run_id)


@celery_app.task(name="tasks.run_backfill")
def run_backfill(request_id):
    return _execute("run_backfill", request_id)


@celery_app.task(name="tasks.cancel_backfill")
def cancel_backfill(request_id):
    return _execute("cancel_backfill", request_id)


@celery_app.task(name="tasks.check_all_sources")
def check_all_sources():
    return _execute("check_all_sources")
