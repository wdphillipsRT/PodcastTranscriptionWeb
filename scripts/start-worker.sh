#!/bin/sh
set -eu

flask --app app:create_app system check --services "${SYSTEM_CHECK_SERVICES:-database,redis,s3,transcribe,youtube,yt_dlp,deno}"
exec "$@"
