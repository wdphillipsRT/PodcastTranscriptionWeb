import os
import ssl
from dotenv import load_dotenv

load_dotenv()

# Broker settings with SSL configuration for Heroku Redis
redis_url = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
broker_url = redis_url
result_backend = redis_url

# SSL configuration for Heroku Redis (rediss://)
if redis_url.startswith('rediss://'):
    broker_use_ssl = {
        'ssl_cert_reqs': ssl.CERT_NONE
    }
    redis_backend_use_ssl = {
        'ssl_cert_reqs': ssl.CERT_NONE
    }

# Task settings
task_serializer = 'json'
accept_content = ['json']
result_serializer = 'json'
timezone = 'UTC'
enable_utc = True

# Worker settings
worker_prefetch_multiplier = 1
task_acks_late = True
worker_max_tasks_per_child = 1000

# Task routing
task_routes = {
    'tasks.download_episode': {'queue': 'downloads'},
    'tasks.transcribe_episode': {'queue': 'transcription'},
    'tasks.summarize_episode': {'queue': 'summarization'},
    'tasks.send_transcript_email': {'queue': 'emails'},
    'tasks.send_summary_email': {'queue': 'emails'},
    'tasks.send_welcome_email': {'queue': 'emails'},
}

# Beat schedule for periodic tasks
beat_schedule = {
    'check-for-new-episodes': {
        'task': 'tasks.check_all_podcasts_for_new_episodes',
        'schedule': 3600.0,  # Every hour
    },
}