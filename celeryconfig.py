import os
from dotenv import load_dotenv

load_dotenv()

# Broker settings
broker_url = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
result_backend = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')

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