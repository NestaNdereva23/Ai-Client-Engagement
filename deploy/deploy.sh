#!/usr/bin/env bash
set -euo pipefail

ACE_DIR="${ACE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ACE_BRANCH="${ACE_BRANCH:-main}"
ACE_COMPOSE_FILE="${ACE_COMPOSE_FILE:-docker-compose.prod.yml}"
ACE_ENV_FILE="${ACE_ENV_FILE:-.env.prod}"
ACE_SERVICE="${ACE_SERVICE:-ace}"
ACE_IMAGE="${ACE_IMAGE:-ace-app}"
ACE_LOCK_FILE="${ACE_LOCK_FILE:-/var/lock/ace-deploy.lock}"
ACE_HEALTH_WAIT_SECONDS="${ACE_HEALTH_WAIT_SECONDS:-120}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*"
}

fail() {
    log "$*" >&2
    exit 1
}

compose() {
    docker compose -f "$ACE_COMPOSE_FILE" --env-file "$ACE_ENV_FILE" "$@"
}

take_lock() {
    exec 9>"$ACE_LOCK_FILE"
    flock --nonblock 9 || fail "another deploy is still running"
}

check_ready_to_deploy() {
    [ -f "$ACE_ENV_FILE" ] || fail "$ACE_ENV_FILE is missing"
    [ "$(git rev-parse --abbrev-ref HEAD)" = "$ACE_BRANCH" ] ||
        fail "this checkout is not on $ACE_BRANCH"
    [ -z "$(git status --porcelain --untracked-files=no)" ] ||
        fail "there are uncommitted changes here, commit or discard them first"
}

pull_latest() {
    git fetch --quiet origin "$ACE_BRANCH"
    git merge --ff-only --quiet FETCH_HEAD
}

keep_previous_image() {
    if docker image inspect "$ACE_IMAGE:latest" >/dev/null 2>&1; then
        docker tag "$ACE_IMAGE:latest" "$ACE_IMAGE:previous"
    fi
}

wait_until_healthy() {
    local container status="unknown" waited=0
    container="$(compose ps --quiet "$ACE_SERVICE")"
    while [ "$waited" -lt "$ACE_HEALTH_WAIT_SECONDS" ]; do
        status="$(docker inspect --format '{{.State.Health.Status}}' "$container")"
        if [ "$status" = "healthy" ]; then
            return 0
        fi
        sleep 5
        waited=$((waited + 5))
    done
    fail "$ACE_SERVICE was not healthy after ${ACE_HEALTH_WAIT_SECONDS}s, last status: $status"
}

report_failure() {
    log "deploy failed. The old image is kept as $ACE_IMAGE:previous."
    log "Migrations that already ran are not undone by going back to it."
}

cd "$ACE_DIR"
take_lock
check_ready_to_deploy

trap report_failure EXIT

before="$(git rev-parse HEAD)"
pull_latest
after="$(git rev-parse HEAD)"
log "deploying ${before:0:7} to ${after:0:7}"

compose config --quiet
if [ "$before" != "$after" ]; then
    keep_previous_image
fi
compose build "$ACE_SERVICE"
compose up -d
wait_until_healthy
docker image prune --force >/dev/null

trap - EXIT
log "deploy finished, now running ${after:0:7}"
