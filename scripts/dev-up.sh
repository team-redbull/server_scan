#!/usr/bin/env bash
# Bring up MongoDB + Redis for local development with nothing beyond a
# working `podman` (or `docker`) binary — no compose provider required.
# This machine has rootless podman 4.9 but no `podman-compose` and no
# `docker-compose` plugin installed, so `compose.yaml` (the documented,
# spec-standard path) isn't runnable out of the box here; this script is
# the zero-extra-tooling fallback for exactly that situation, per spec
# section 52 ("`docker compose up` must be sufficient for development").
#
# Usage:
#   scripts/dev-up.sh          # start mongo + redis
#   scripts/dev-up.sh down     # stop and remove them
#   scripts/dev-up.sh status   # show container state

set -euo pipefail

RUNTIME="${CONTAINER_RUNTIME:-podman}"
POD_NAME="server-inventory-dev"
MONGO_IMAGE="docker.io/library/mongo:8"
REDIS_IMAGE="docker.io/library/redis:8-alpine"

if ! command -v "$RUNTIME" >/dev/null 2>&1; then
  echo "error: '$RUNTIME' not found. Install podman or docker, or set CONTAINER_RUNTIME." >&2
  exit 1
fi

pod_exists() { "$RUNTIME" pod exists "$POD_NAME" 2>/dev/null; }

up() {
  if pod_exists; then
    echo "Pod '$POD_NAME' already exists. Use 'scripts/dev-up.sh down' first to recreate it."
  else
    echo "Creating pod '$POD_NAME' (mongo:27017, redis:6379)..."
    "$RUNTIME" pod create --name "$POD_NAME" -p 27017:27017 -p 6379:6379
    "$RUNTIME" run -d --pod "$POD_NAME" --name "${POD_NAME}-mongo" \
      -v "${POD_NAME}-mongo-data:/data/db" "$MONGO_IMAGE"
    "$RUNTIME" run -d --pod "$POD_NAME" --name "${POD_NAME}-redis" "$REDIS_IMAGE"
  fi

  echo -n "Waiting for MongoDB to accept connections"
  for _ in $(seq 1 30); do
    if "$RUNTIME" exec "${POD_NAME}-mongo" mongosh --quiet --eval 'db.adminCommand("ping")' >/dev/null 2>&1; then
      echo " - ready."
      break
    fi
    echo -n "."
    sleep 1
  done

  echo -n "Waiting for Redis to accept connections"
  for _ in $(seq 1 30); do
    if "$RUNTIME" exec "${POD_NAME}-redis" redis-cli ping >/dev/null 2>&1; then
      echo " - ready."
      break
    fi
    echo -n "."
    sleep 1
  done

  cat <<EOF

Dev stack is up:
  mongodb://localhost:27017
  redis://localhost:6379/0

Run the API against it with:
  cd backend && uv run uvicorn app.main:app --reload --port 8080
EOF
}

down() {
  if pod_exists; then
    echo "Stopping and removing pod '$POD_NAME'..."
    "$RUNTIME" pod rm -f "$POD_NAME"
  else
    echo "Pod '$POD_NAME' does not exist; nothing to do."
  fi
}

status() {
  if pod_exists; then
    "$RUNTIME" pod ps --filter "name=$POD_NAME"
    "$RUNTIME" ps --pod --filter "pod=$POD_NAME"
  else
    echo "Pod '$POD_NAME' is not running."
  fi
}

case "${1:-up}" in
  up) up ;;
  down) down ;;
  status) status ;;
  *)
    echo "Usage: $0 [up|down|status]" >&2
    exit 1
    ;;
esac
