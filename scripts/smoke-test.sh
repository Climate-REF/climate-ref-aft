#!/usr/bin/env bash
set -euo pipefail

# Smoke test for the Climate REF AFT docker stack.
#
# This script verifies that all services start correctly, data can be ingested,
# and the solver can execute diagnostics across all providers.
#
# Usage:
#   bash scripts/smoke-test.sh

COMPOSE_FILE="docker/docker-compose.yaml"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

echo "Starting smoke test for Climate REF AFT docker stack..."

check_service() {
    local service=$1
    local max_attempts=30
    local attempt=1

    echo "Checking service: $service"
    while [ $attempt -le $max_attempts ]; do
        if docker compose -f "$COMPOSE_FILE" ps "$service" | grep -q "Up"; then
            echo -e "${GREEN}  $service is up${NC}"
            return 0
        fi
        echo "Waiting for $service to be ready... (attempt $attempt/$max_attempts)"
        sleep 2
        attempt=$((attempt + 1))
    done

    echo -e "${RED}  $service failed to start${NC}"
    return 1
}

# Start the stack
echo "Starting docker stack..."
docker compose -f "$COMPOSE_FILE" up -d

# Check if all services are running
echo "Checking service health..."
services=("redis" "postgres" "ref-app" "flower" "climate-ref" "climate-ref-esmvaltool" "climate-ref-pmp" "climate-ref-ilamb")
for service in "${services[@]}"; do
    check_service "$service" || exit 1
done

# Sleep to allow services to stabilize
echo "Sleeping to wait for services to stabilize..."
sleep 5

docker compose -f "$COMPOSE_FILE" ps

# Fetch sample data
echo "Fetching sample data..."
docker compose -f "$COMPOSE_FILE" run --rm climate-ref datasets fetch-data --registry sample-data --output-directory /ref/sample-data

# Ingest sample data
echo "Ingesting sample data..."
if docker compose -f "$COMPOSE_FILE" run --rm climate-ref -v datasets ingest --source-type cmip6 /ref/sample-data/CMIP6; then
    echo -e "${GREEN}  CMIP6 data ingestion successful${NC}"
else
    echo -e "${RED}  CMIP6 data ingestion failed${NC}"
    exit 1
fi

if docker compose -f "$COMPOSE_FILE" run --rm climate-ref datasets ingest --source-type obs4mips /ref/sample-data/obs4REF; then
    echo -e "${GREEN}  Obs4MIPs data ingestion successful${NC}"
else
    echo -e "${RED}  Obs4MIPs data ingestion failed${NC}"
    exit 1
fi

# Run a simple solve
if docker compose -f "$COMPOSE_FILE" run --rm climate-ref -v solve --timeout 180 --one-per-provider; then
    echo -e "${GREEN}  Solving completed before timeout${NC}"
else
    echo -e "${RED}  Solving failed${NC}"
    exit 1
fi

echo -e "${GREEN}  All smoke tests passed!${NC}"
echo "The docker stack is healthy and ready for use."
