#!/usr/bin/env bash
# Deploy the API on the VPS: pull, fix data ownership, rebuild, health check.
# Run from the repo root on the server: /srv/geoapi
set -euo pipefail

cd "$(dirname "$0")"

echo "==> pulling"
git pull --ff-only

# The container runs as uid 10001 and SQLite has to write into the bind mount.
echo "==> preparing data directory"
mkdir -p data
chown -R 10001:10001 data

echo "==> building and starting"
docker compose up -d --build

echo "==> waiting for health"
for _ in $(seq 1 30); do
  status=$(docker inspect geoapi --format '{{.State.Health.Status}}' 2>/dev/null || echo unknown)
  if [ "$status" = "healthy" ]; then
    break
  fi
  sleep 2
done
echo "container: $(docker inspect geoapi --format '{{.State.Health.Status}}')"

echo "==> local health check through caddy"
curl -s -H "Host: geo-api.lverma.com" http://127.0.0.1:80/api/health/
echo
