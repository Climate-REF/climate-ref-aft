#!/usr/bin/env bash
set -euo pipefail

# End-to-end test for the Climate REF AFT helm chart on a local minikube.
#
# Mirrors the `Test Helm Deployment` job in .github/workflows/packaging.yaml:
#   1. Start minikube with a host mount for /cache/ref-config.
#   2. Install the chart with helm/ci/gh-actions-values.yaml.
#   3. Initialise providers and ingest sample data via the orchestrator.
#   4. Run a small solve across all three providers.
#   5. Hit the api Service from inside the cluster and verify it can see
#      the executions written by the workers.
#
# Usage:
#   bash scripts/e2e-minikube.sh
#
# Environment overrides:
#   RELEASE          helm release name (default: test)
#   CHART_PATH       chart directory or OCI ref (default: ./helm)
#   VALUES_FILE      values file (default: helm/ci/gh-actions-values.yaml)
#   CACHE_DIR        host directory mounted into minikube (default: ./.cache/ref-config)
#   KEEP_RUNNING     "1" to leave the release installed after a successful run
#
# Requirements:
#   minikube, helm, kubectl on PATH.

RELEASE=${RELEASE:-test}
CHART_PATH=${CHART_PATH:-./helm}
VALUES_FILE=${VALUES_FILE:-helm/ci/gh-actions-values.yaml}
CACHE_DIR=${CACHE_DIR:-$(pwd)/.cache/ref-config}
KEEP_RUNNING=${KEEP_RUNNING:-0}

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}==>${NC} $*"; }
fail() { echo -e "${RED}!! $*${NC}"; exit 1; }

cleanup() {
    if [ "$KEEP_RUNNING" = "1" ]; then
        log "KEEP_RUNNING=1 — leaving release '$RELEASE' installed."
        return
    fi
    log "Uninstalling release '$RELEASE'..."
    helm uninstall "$RELEASE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

mkdir -p "$CACHE_DIR"
# The chart's worker pods run as uid 1000; make the host mount writable.
chmod 0777 "$CACHE_DIR"

if ! minikube status >/dev/null 2>&1; then
    log "Starting minikube with host mount $CACHE_DIR -> /cache/ref-config..."
    minikube start --mount --mount-string="$CACHE_DIR:/cache/ref-config"
else
    log "minikube already running. Make sure /cache/ref-config is mounted from $CACHE_DIR."
fi

log "Installing chart '$RELEASE' from $CHART_PATH with $VALUES_FILE..."
helm install "$RELEASE" "$CHART_PATH" -f "$VALUES_FILE"

log "Waiting for orchestrator to come up..."
kubectl wait deployment/${RELEASE}-climate-ref-aft-orchestrator \
    --for=condition=available --timeout=300s

log "Initialising providers (no data download for esmvaltool)..."
kubectl exec deployment/${RELEASE}-climate-ref-aft-orchestrator -- \
    ref providers setup --skip-data --skip-validate
kubectl exec deployment/${RELEASE}-climate-ref-aft-orchestrator -- \
    ref providers setup --provider pmp
kubectl exec deployment/${RELEASE}-climate-ref-aft-orchestrator -- \
    ref providers setup --provider ilamb

log "Fetching sample data..."
kubectl exec deployment/${RELEASE}-climate-ref-aft-orchestrator -- \
    ref datasets fetch-data --registry sample-data --output-directory /ref/sample-data

log "Ingesting sample CMIP6 + obs4mips..."
kubectl exec deployment/${RELEASE}-climate-ref-aft-orchestrator -- \
    ref -v datasets ingest --source-type cmip6 /ref/sample-data/CMIP6
kubectl exec deployment/${RELEASE}-climate-ref-aft-orchestrator -- \
    ref -v datasets ingest --source-type obs4mips /ref/sample-data/obs4REF

log "Running a small solve across all three providers..."
kubectl exec deployment/${RELEASE}-climate-ref-aft-orchestrator -- \
    ref -v solve --timeout 720 --one-per-provider \
    --diagnostic global-mean-timeseries \
    --diagnostic annual-cycle \
    --diagnostic gpp-wecann \
    --provider esmvaltool \
    --provider pmp \
    --provider ilamb

log "Validating API endpoints..."
# ref-app eagerly imports providers from /ref/software at startup. Provider
# setup happens after the api Deployment is created, so the initial api pod
# may have crashed before /ref/software was populated. Force a rollout so a
# fresh pod starts against the now-populated /ref.
kubectl rollout restart deployment/${RELEASE}-climate-ref-aft-api
kubectl rollout status  deployment/${RELEASE}-climate-ref-aft-api --timeout=300s

API_BASE="http://${RELEASE}-climate-ref-aft-api/api/v1"
run_curl() {
    local path=$1
    kubectl run curl-$RANDOM --rm -i --restart=Never \
        --image=curlimages/curl:8.10.1 -- \
        curl -fsS --max-time 30 "$API_BASE$path"
}

run_curl /utils/health-check/                                              || fail "health-check failed"
DIAG_JSON=$(run_curl /cmip7-aft-diagnostics/)                              || fail "diagnostics fetch failed"
EXEC_JSON=$(run_curl /executions/)                                         || fail "executions fetch failed"

DIAG_COUNT=$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print(len(d if isinstance(d,list) else d.get("data",[])))' "$DIAG_JSON")
EXEC_COUNT=$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print(len(d if isinstance(d,list) else d.get("data",[])))' "$EXEC_JSON")

[ "$DIAG_COUNT" -gt 0 ] || fail "expected non-empty diagnostics list, got $DIAG_COUNT"
[ "$EXEC_COUNT" -gt 0 ] || fail "expected at least one execution from the solve step, got $EXEC_COUNT"

log "API saw $DIAG_COUNT diagnostics and $EXEC_COUNT executions."
log "End-to-end test passed."
