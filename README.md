# Climate REF AFT (Assessment Fast Track)

Deployment configuration, integration tests, and version manifest for the
[CMIP7 Assessment Fast Track](https://wcrp-cmip.org/cmip7-assessment-fast-track/)
evaluation pipeline built on [Climate REF](https://github.com/Climate-REF/climate-ref).

## What This Repository Contains

| Directory       | Purpose                                          |
| --------------- | ------------------------------------------------ |
| `helm/`         | Helm chart for Kubernetes deployment             |
| `docker/`       | Docker Compose for local development and testing |
| `tests/`        | Cross-provider integration tests                 |
| `scripts/`      | Smoke tests and deployment helpers               |
| `versions.toml` | Version manifest pinning all component versions  |

## Components

The AFT deployment brings together independently versioned packages:

| Package                  | Repository                                                            | Description                                   |
| ------------------------ | --------------------------------------------------------------------- | --------------------------------------------- |
| `climate-ref-core`       | [Climate-REF/climate-ref](https://github.com/Climate-REF/climate-ref) | Core library with base classes and interfaces |
| `climate-ref`            | [Climate-REF/climate-ref](https://github.com/Climate-REF/climate-ref) | Main application, CLI, database, solver       |
| `climate-ref-celery`     | [Climate-REF/climate-ref](https://github.com/Climate-REF/climate-ref) | Celery executor for distributed execution     |
| `climate-ref-esmvaltool` | [Climate-REF/climate-ref](https://github.com/Climate-REF/climate-ref) | ESMValTool diagnostic provider                |
| `climate-ref-pmp`        | [Climate-REF/climate-ref](https://github.com/Climate-REF/climate-ref) | PCMDI Metrics Package diagnostic provider     |
| `climate-ref-ilamb`      | [Climate-REF/climate-ref](https://github.com/Climate-REF/climate-ref) | ILAMB diagnostic provider                     |
| `climate-ref-frontend`   | [Climate-REF/ref-ap](https://github.com/Climate-REF/ref-app)          | API + Frontend                                |

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
helm install ref oci://ghcr.io/climate-ref/charts/climate-ref-aft --version 0.11.1
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

## Release Process

This project uses [Calendar Versioning](https://calver.org/) (`YYYY.MM.P`) with
[bump-my-version](https://github.com/callowayproject/bump-my-version) and
[towncrier](https://towncrier.readthedocs.io/) for changelog generation.

### Adding changelog fragments

Every user-facing change should include a changelog fragment in `changelog/`:

```bash
# Create a fragment linked to a PR number
echo "Description of the change." > changelog/<PR_NUMBER>.<type>.md
```

Where `<type>` is one of: `breaking`, `deprecation`, `feature`, `improvement`, `fix`, `docs`, `trivial`.

### Triggering a release

Releases are created via the **Bump version** workflow in GitHub Actions:

1. Go to **Actions** > **Bump version** > **Run workflow**
2. Choose the bump rule:
   - `patch` -- increment the patch number (e.g. `2026.02.0` -> `2026.02.1`). Also auto-updates the CalVer portion if the current month has changed.
   - `release` -- update to the current year/month and reset patch to 0 (e.g. `2026.02.1` -> `2026.03.0`)

The workflow will:

1. Compile changelog fragments via towncrier
2. Bump the version in `pyproject.toml` and `versions.toml`
3. Create a version commit and tag (e.g. `v2026.03.0`)
4. Push the commit and tag, which triggers `release.yml`

The `release.yml` workflow then:

- Publishes the Helm chart to the GHCR OCI registry
- Creates a GitHub Release with `versions.toml` attached

### Helm chart versioning

The Helm chart has its own version managed separately via `.bumpversion-helm.toml`.
Bump it when the chart templates change independently of the AFT release:

```bash
uv run bump-my-version --config-file .bumpversion-helm.toml bump patch
```

## Related

- [Climate REF Documentation](https://climate-ref.readthedocs.io/)
- [Issue #513: Split diagnostic providers](https://github.com/Climate-REF/climate-ref/issues/513)
- [CMIP7 Assessment Fast Track](https://wcrp-cmip.org/cmip7-assessment-fast-track/)
