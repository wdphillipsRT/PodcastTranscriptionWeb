#!/bin/sh
set -eu

export PYTHONPATH=/app

if ! version="$(claude --version 2>/dev/null)" || [ "$version" != "${CLAUDE_EXPECTED_VERSION:-2.1.303} (Claude Code)" ]; then
    printf '%s\n' 'Classifier Claude version check failed.' >&2
    exit 1
fi

if ! claude auth status >/dev/null 2>&1; then
    printf '%s\n' 'Classifier Claude authentication check failed.' >&2
    exit 1
fi

flask --app app:create_app system check --services database,redis,s3
exec "$@"
