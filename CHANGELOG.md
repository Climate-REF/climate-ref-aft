# Changelog

All notable changes to the Climate REF AFT deployment will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/).

<!-- towncrier release notes start -->

## climate-ref-aft 0.2.2 (2026-05-19)

### Features

- Ship memory-control defaults for diagnostic providers.
  ``providers.esmvaltool.config`` now renders a chart-managed ConfigMap mounted at ``/etc/esmvaltool`` with ``ESMVALTOOL_CONFIG_DIR`` wired up, capping esmvalcore dask workers per the upstream `memory use guide <https://climate-ref.readthedocs.io/en/latest/how-to-guides/control-memory-use/>`_.
  ``providers.pmp`` and ``providers.ilamb`` default ``DASK_SCHEDULER=synchronous`` to avoid the crashes tracked in Climate-REF/climate-ref#437.
  Set ``providers.esmvaltool.config: null`` to opt out and supply your own configuration. ([#13](https://github.com/Climate-REF/climate-ref-aft/pulls/13))

### Improvements

- Bump pinned climate-ref core, celery, esmvaltool, pmp, and ilamb components and the worker container image (helm + docker-compose) from ``v0.14.0`` to ``v0.14.3``. ([#14](https://github.com/Climate-REF/climate-ref-aft/pulls/14))


## climate-ref-aft 0.2.1 (2026-05-12)

### Bug Fixes

- Run ``helm dependency build`` before ``helm package`` in the release workflow so chart dependencies (e.g. ``dragonfly``) are vendored at publish time, restoring the tag-triggered Helm release job. ([#12](https://github.com/Climate-REF/climate-ref-aft/pulls/12))


## climate-ref-aft 0.2.0 (2026-05-12)

### Features

- Add ref-app API component and Gateway API HTTPRoute support to the Helm chart. ([#3](https://github.com/Climate-REF/climate-ref-aft/pulls/3))
- Validate the API against the compute engine end-to-end. The docker-compose
  smoke test and the Helm CI deployment now hit ``ref-app`` after a solve to
  confirm executions written by Celery workers are visible through the API.
  A new ``scripts/e2e-minikube.sh`` runs the same flow locally on minikube. ([#7](https://github.com/Climate-REF/climate-ref-aft/pulls/7))

### Improvements

- Migrate versioning from CalVer to SemVer and unify repo and Helm chart version under a single scheme. ([#7](https://github.com/Climate-REF/climate-ref-aft/pulls/7))
- Bump pinned climate-ref components and worker image to v0.14.0. ([#11](https://github.com/Climate-REF/climate-ref-aft/pulls/11))

### Bug Fixes

- Align ``REF_CONFIGURATION`` between API and workers to use ``/ref``, and re-enable API in minimal CI deployment test. ([#7](https://github.com/Climate-REF/climate-ref-aft/pulls/7))
- Fix ``bump-my-version`` configuration so the ``[aft]`` ``version`` pattern no longer matches ``chart-version`` as a substring,
  restoring the ``Bump version`` release workflow.

  Pin project interpreter to Python 3.13 via ``.python-version`` so ``uv sync`` resolves prebuilt wheels for scipy/numpy on runners that ship CPython 3.14. ([#10](https://github.com/Climate-REF/climate-ref-aft/pulls/10))
