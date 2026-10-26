"""Import-safe worker policy. No client connections or dotenv side effects."""
import os
import ssl
from celery.schedules import crontab
from kombu import Queue

broker_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
result_backend = broker_url
if broker_url.startswith("rediss://"):
    broker_use_ssl = {"ssl_cert_reqs": ssl.CERT_REQUIRED}
    redis_backend_use_ssl = {"ssl_cert_reqs": ssl.CERT_REQUIRED}

task_serializer = result_serializer = "json"
accept_content = result_accept_content = ["json"]
timezone = "UTC"
enable_utc = True
task_acks_late = True
task_reject_on_worker_lost = True
worker_prefetch_multiplier = 1
broker_connection_retry_on_startup = True
broker_connection_retry = True
task_soft_time_limit = 1800
task_time_limit = 1860
result_expires = 3600
task_default_queue = "default"
task_create_missing_queues = False
task_queues = tuple(Queue(name) for name in (
    "default", "discovery", "downloads", "transcription", "classification", "knowledge"))
task_routes = {"tasks." + name: {"queue": "discovery"} for name in (
    "discover_source", "continue_discovery", "finalize_discovery", "run_backfill", "cancel_backfill", "check_all_sources")}
task_routes.update({"tasks.process_item": {"queue": "downloads"},
                    "tasks.transcribe_item": {"queue": "transcription"}})
beat_schedule = {"hourly-source-discovery": {"task": "tasks.check_all_sources", "schedule": 3600.0}}
task_routes.update({"tasks." + name: {"queue": "classification"}
    for name in ("classify_item", "reconcile_classifications", "scan_source_rule")})
beat_schedule["classification-reconciliation"] = {"task": "tasks.reconcile_classifications",
    "schedule": float(os.environ.get("CLASSIFICATION_RECONCILE_INTERVAL_SECONDS", "300"))}
# Guest extraction calls the model router, so it runs only on the classifier's queue.
task_routes["tasks.extract_guests"] = {"queue": "classification"}
if os.environ.get("GUEST_EXTRACTION_ENABLED", "false").strip().lower() == "true":
    # Validated as config.load_settings does: beat must never schedule paid calls more often than hourly.
    _guest_interval = os.environ.get("GUEST_EXTRACTION_INTERVAL_SECONDS", "3600").strip()
    if not _guest_interval.isascii() or not _guest_interval.isdigit() or int(_guest_interval) < 3600:
        raise ValueError("GUEST_EXTRACTION_INTERVAL_SECONDS must be an integer of at least 3600")
    beat_schedule["guest-extraction"] = {"task": "tasks.extract_guests", "schedule": float(int(_guest_interval))}
# Only the optional exporter service consumes "knowledge"; a stalled export
# never delays media or classification work, and a missed night expires.
task_routes["tasks.export_knowledge"] = {"queue": "knowledge"}
if os.environ.get("KNOWLEDGE_EXPORT_ENABLED", "false").strip().lower() == "true":
    beat_schedule["knowledge-export-nightly"] = {"task": "tasks.export_knowledge",
        "schedule": crontab(minute=0, hour=4), "options": {"expires": 6 * 3600}}
