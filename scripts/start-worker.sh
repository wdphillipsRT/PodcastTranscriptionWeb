#!/bin/sh
set -eu

# Celery's console loader can import tasks from the working directory without
# retaining that directory on sys.path in prefork children. Pin the application
# root so lazy task imports cannot depend on process working-directory behavior.
export PYTHONPATH=/app

flask --app app:create_app system check --services database,redis,s3,transcribe,youtube,yt_dlp,deno
exec "$@"
