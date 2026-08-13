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
| `climate-ref-frontend`   | [Climate-REF/ref-app](https://github.com/Climate-REF/ref-app)         | API and bundled Frontend                      |

Note: we may split the providers out into their own repositories in future.

## Versioning

This repository uses [Semantic Versioning](https://semver.org/).
Each release represents a tested, deployable combination of all components.

The `versions.toml` file pins the exact versions of each component in a given release.

## Quick Start

```bash
# Install the chart from the OCI registry
helm install ref oci://ghcr.io/climate-ref/charts/climate-ref-aft --version 0.5.4
```

## Development

[DEVELOPMENT.md](DEVELOPMENT.md) covers running the stack locally,
the integration tests, the CI workflows, and the release process.

## Related

- [Climate REF Documentation](https://climate-ref.readthedocs.io/)
- [Issue #513: Split diagnostic providers](https://github.com/Climate-REF/climate-ref/issues/513)
- [CMIP7 Assessment Fast Track](https://wcrp-cmip.org/cmip7-assessment-fast-track/)
