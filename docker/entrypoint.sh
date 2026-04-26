#!/bin/sh
set -eu

# Why this script exists:
# - Docker and Unraid users should not have to create directory trees manually.
# - The worker persists config, logs and pause/resume state below /data.

DATA_DIR="${PAPERLESS_KIPLUS_DATA_DIR:-/data}"
mkdir -p "$DATA_DIR/config" "$DATA_DIR/state" "$DATA_DIR/logs" "$DATA_DIR/exports"

exec "$@"
