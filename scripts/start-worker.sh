#!/bin/sh
set -eu

flask --app app:create_app system check --services database,redis,s3,transcribe,youtube,yt_dlp,deno
exec "$@"
