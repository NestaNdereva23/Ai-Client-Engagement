#!/usr/bin/env bash
set -euo pipefail

ACE_DIR="${ACE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
ACE_COMPOSE_FILE="${ACE_COMPOSE_FILE:-docker-compose.prod.yml}"
ACE_ENV_FILE="${ACE_ENV_FILE:-.env.prod}"
ACE_SERVICE="${ACE_SERVICE:-ace}"
ACE_LOCK_FILE="${ACE_LOCK_FILE:-/var/lock/ace-batch-ingest.lock}"

if [ "${ACE_BATCH_INGEST_LOCKED:-0}" != "1" ]; then
    export ACE_BATCH_INGEST_LOCKED=1
    status=0
    flock --nonblock --conflict-exit-code 75 "$ACE_LOCK_FILE" "$0" "$@" || status=$?
    if [ "$status" = "75" ]; then
        echo "another batch ingest tick is still going, skipping this one" >&2
    fi
    exit "$status"
fi

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*"
}

cd "$ACE_DIR"

log "batch ingest tick starting"

status=0
docker compose -f "$ACE_COMPOSE_FILE" --env-file "$ACE_ENV_FILE" \
    exec -T "$ACE_SERVICE" python scripts/inactive/batch_ingest_tick.py "$@" || status=$?

if [ "$status" != "0" ]; then
    log "batch ingest tick failed with exit code $status"
    exit "$status"
fi

log "batch ingest tick finished"
