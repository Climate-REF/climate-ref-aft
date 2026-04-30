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
kubectl wait --for=condition=Ready pod \
    -l app.kubernetes.io/component=api --timeout=60s

# Drive requests through the orchestrator pod so kubectl exec propagates
# the python exit code reliably and we don't need a separate curl image.
API_BASE="http://${RELEASE}-climate-ref-aft-api/api/v1"
api_check() {
    local path=$1 expect_nonempty=${2:-0}
    kubectl exec deployment/${RELEASE}-climate-ref-aft-orchestrator -- \
        python3 -c "
import json, sys, urllib.request
url = '${API_BASE}${path}'
with urllib.request.urlopen(url, timeout=30) as r:
    body = r.read().decode()
    assert r.status == 200, f'{url} -> {r.status}'
try:
    d = json.loads(body)
except json.JSONDecodeError:
    print('non-json body:', body[:200]); sys.exit(0)
if isinstance(d, list):
    n = len(d)
else:
    n = d.get('count', len(d.get('results') or d.get('data') or []))
print(f'{url} -> {n} item(s)')
if int('${expect_nonempty}'):
    assert n > 0, f'expected non-empty result from {url}'
"
}

api_check /utils/health-check/        || fail "health-check failed"
api_check /cmip7-aft-diagnostics/ 1   || fail "diagnostics fetch failed"
api_check /executions/ 1              || fail "executions fetch failed"

log "End-to-end test passed."
