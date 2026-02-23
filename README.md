# Climate REF AFT (Assessment Fast Track)

Deployment configuration, integration tests, and version manifest for the
[CMIP7 Assessment Fast Track](https://wcrp-cmip.org/cmip7-assessment-fast-track/)
evaluation pipeline built on [Climate REF](https://github.com/Climate-REF/climate-ref).

## What This Repository Contains

| Directory | Purpose |
|-----------|---------|
| `helm/` | Helm chart for Kubernetes deployment |
| `docker/` | Docker Compose for local development and testing |
| `tests/` | Cross-provider integration tests |
| `scripts/` | Smoke tests and deployment helpers |
| `versions.toml` | Version manifest pinning all component versions |

## Components

The AFT deployment brings together independently versioned packages:

| Package | Repository | Description |
|---------|------------|-------------|
| `climate-ref-core` | [Climate-REF/climate-ref](https://github.com/Climate-REF/climate-ref) | Core library with base classes and interfaces |
| `climate-ref` | [Climate-REF/climate-ref](https://github.com/Climate-REF/climate-ref) | Main application, CLI, database, solver |
| `climate-ref-celery` | [Climate-REF/climate-ref](https://github.com/Climate-REF/climate-ref) | Celery executor for distributed execution |
| `climate-ref-esmvaltool` | [Climate-REF/climate-ref](https://github.com/Climate-REF/climate-ref) | ESMValTool diagnostic provider |
| `climate-ref-pmp` | [Climate-REF/climate-ref](https://github.com/Climate-REF/climate-ref) | PCMDI Metrics Package diagnostic provider |
| `climate-ref-ilamb` | [Climate-REF/climate-ref](https://github.com/Climate-REF/climate-ref) | ILAMB diagnostic provider |

Note: we intend to split the providers out into their own repositories in the coming weeks.

## Versioning

This repository uses [Calendar Versioning](https://calver.org/) with the format `YYYY.MM`
(e.g., `2026.02`). Each release represents a tested, deployable combination of all components.

The `versions.toml` file pins the exact version ranges for each component in a given release.

## Quick Start

### Local Development (Docker Compose)

```bash
# Start the full stack
docker compose -f docker/docker-compose.yaml up -d

# Run smoke tests
bash scripts/smoke-test.sh
```

### Kubernetes (Helm)

```bash
# Install the chart
helm install ref ./helm -f helm/local-test-values.yaml

# Or from the OCI registry
helm install ref oci://ghcr.io/climate-ref/charts/climate-ref-aft --version 0.9.1
```

### Integration Tests

```bash
# Install test dependencies
uv sync --all-extras

# Run integration tests (requires providers to be set up)
uv run pytest tests/ -v

# Run slow integration tests (full end-to-end)
uv run pytest tests/ -v --slow
```

## CI Workflows

| Workflow | Trigger | What It Does |
|----------|---------|--------------|
| `ci.yml` | Push, PR | Lint, install pinned versions, run integration tests |
| `packaging.yaml` | Push, PR | Helm chart OCI publish and minikube deployment test |
| `nightly.yml` | Scheduled (daily) | Test against latest versions of all components |
| `release.yml` | Tag push | Publish Helm chart, create GitHub release |

## Related

- [Climate REF Documentation](https://climate-ref.readthedocs.io/)
- [Issue #513: Split diagnostic providers](https://github.com/Climate-REF/climate-ref/issues/513)
- [CMIP7 Assessment Fast Track](https://wcrp-cmip.org/cmip7-assessment-fast-track/)
