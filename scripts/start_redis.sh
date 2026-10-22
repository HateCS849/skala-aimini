#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose -p burstguard-fixture -f examples/redis-compose.yaml up -d
