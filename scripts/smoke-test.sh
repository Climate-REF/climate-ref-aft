#!/usr/bin/env bash
set -euo pipefail

# Smoke test for the Climate REF AFT docker stack.
#
# Verifies that all services start, data can be ingested,
# and the solver can execute diagnostics across each of the Fast Track providers.
#
# Usage:
#   bash scripts/smoke-test.sh

COMPOSE_FILE="docker/docker-compose.yaml"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

ok()   { echo -e "${GREEN}  $*${NC}"; }
fail() { echo -e "${RED}  $*${NC}"; exit 1; }

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

# Run a `climate-ref` CLI command in a throwaway container, reporting either way.
step() {
    local description=$1
    shift
    if compose run --rm climate-ref "$@"; then
        ok "$description successful"
    else
        fail "$description failed"
    fi
}

check_service() {
    local service=$1
    local max_attempts=30
    local attempt=1

    echo "Checking service: $service"
    while [ $attempt -le $max_attempts ]; do
        if compose ps "$service" | grep -q "Up"; then
            ok "$service is up"
            return 0
        fi
        echo "Waiting for $service to be ready... (attempt $attempt/$max_attempts)"
        sleep 2
        attempt=$((attempt + 1))
    done

    fail "$service failed to start"
}

echo "Starting docker stack..."
compose up -d

echo "Checking service health..."
services=("redis" "postgres" "ref-app" "flower" "climate-ref" "climate-ref-esmvaltool" "climate-ref-pmp" "climate-ref-ilamb")
for service in "${services[@]}"; do
    check_service "$service"
done

echo "Sleeping to wait for services to stabilize..."
sleep 5

compose ps

echo "Fetching sample data..."
compose run --rm climate-ref datasets fetch-data --registry sample-data --output-directory /ref/sample-data

step "CMIP6 data ingestion" -v datasets ingest --source-type cmip6 /ref/sample-data/CMIP6
step "Obs4MIPs data ingestion" datasets ingest --source-type obs4mips /ref/sample-data/obs4REF

# A fixed set of fast diagnostics keeps run times predictable
step "Solving" -v solve --timeout 180 --one-per-provider \
    --diagnostic global-mean-timeseries \
    --diagnostic annual-cycle \
    --diagnostic gpp-wecann

# Validate the API can read the results produced by the compute engine.
# ref-app exposes read-only endpoints, so this checks it can talk to the same
# database that the workers wrote results into.
API_URL="${REF_API_URL:-http://localhost:8000}"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
echo "Validating API at $API_URL..."

python3 "$SCRIPT_DIR/lib/api_check.py" "$API_URL/api/v1/utils/health-check/"
python3 "$SCRIPT_DIR/lib/api_check.py" "$API_URL/api/v1/cmip7-aft-diagnostics/" 1
python3 "$SCRIPT_DIR/lib/api_check.py" "$API_URL/api/v1/executions/" 1

ok "All smoke tests passed!"
echo "The docker stack is healthy and ready for use."
