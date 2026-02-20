# Changelog

All notable changes to the Climate REF AFT deployment will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Calendar Versioning](https://calver.org/) with the format `YYYY.MM`.

<!-- towncrier release notes start -->

## [2026.02] - Unreleased

### Fixed

- Add package stub so hatchling can build the project wheel
- Add `helm dependency build` step in CI before linting
- Fix trailing YAML document separators in Helm provider templates
- Fix import sorting in test files

### Added

- Initial AFT repository scaffolding
- Helm chart for Kubernetes deployment (moved from climate-ref monorepo)
- Docker Compose configuration for local development
- Cross-provider integration test suite
- Version manifest (`versions.toml`) pinning all component versions
- CI workflows: PR checks, nightly compatibility, release automation
- Smoke test script for Docker-based deployments

### Component Versions

| Component | Version |
|-----------|---------|
| climate-ref-core | >=0.9.0 |
| climate-ref | >=0.9.0 |
| climate-ref-celery | >=0.9.0 |
| climate-ref-esmvaltool | >=0.9.0 |
| climate-ref-pmp | >=0.9.0 |
| climate-ref-ilamb | >=0.9.0 |
