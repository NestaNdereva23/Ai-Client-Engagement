#!/usr/bin/env bash
set -euo pipefail

ACE_DIR="${ACE_DIR:-/opt/ace-deploy/Ai-Client-Engagement}"
ACE_COMPOSE_FILE="${ACE_COMPOSE_FILE:-docker-compose.prod.yml}"
ACE_ENV_FILE="${ACE_ENV_FILE:-.env.prod}"
ACE_SERVICE="${ACE_SERVICE:-ace}"
ACE_LOCK_FILE="${ACE_LOCK_FILE:-/var/lock/ace-risk-detect.lock}"

if [ "${ACE_RISK_LOCKED:-0}" != "1" ]; then
    export ACE_RISK_LOCKED=1
    status=0
    flock --nonblock --conflict-exit-code 75 "$ACE_LOCK_FILE" "$0" "$@" || status=$?
    if [ "$status" = "75" ]; then
        echo "another risk detection run is still going, skipping this one" >&2
    fi
    exit "$status"
fi

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*"
}

cd "$ACE_DIR"

log "risk detection starting"

status=0
docker compose -f "$ACE_COMPOSE_FILE" --env-file "$ACE_ENV_FILE" \
    exec -T "$ACE_SERVICE" python scripts/risk/detect.py "$@" || status=$?

if [ "$status" != "0" ]; then
    log "risk detection failed with exit code $status"
    exit "$status"
fi

log "risk detection finished"
