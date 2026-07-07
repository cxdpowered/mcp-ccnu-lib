#!/bin/sh
set -eu

DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR"

# Bind-mounted data can keep root ownership from older container runs. The app
# process runs as uid 1000, so repair ownership before starting the server.
chown -R app:app "$DATA_DIR" 2>/dev/null || true

if [ "$(id -u)" = "0" ]; then
    exec runuser -u app -- "$@"
fi

exec "$@"
