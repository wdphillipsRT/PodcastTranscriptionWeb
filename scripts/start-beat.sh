#!/bin/sh
set -eu

flask --app app:create_app system check --services database,redis,beat
exec "$@"
