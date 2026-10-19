"""Import-safe worker policy. No client connections or dotenv side effects."""
import os
import ssl
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
task_queues = tuple(Queue(name) for name in ("default", "discovery", "downloads", "transcription"))
task_routes = {"tasks." + name: {"queue": "discovery"} for name in (
    "discover_source", "continue_discovery", "finalize_discovery", "run_backfill", "cancel_backfill", "check_all_sources")}
task_routes.update({"tasks.process_item": {"queue": "downloads"},
                    "tasks.transcribe_item": {"queue": "transcription"}})
beat_schedule = {"hourly-source-discovery": {"task": "tasks.check_all_sources", "schedule": 3600.0}}
