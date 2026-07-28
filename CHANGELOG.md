# Changelog

All notable changes to the Climate REF AFT deployment will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/).

<!-- towncrier release notes start -->

## climate-ref-aft 0.5.2 (2026-07-28)

### Features

- Adds an opt-in `livenessProbe` for the provider workers, configured via `defaults.livenessProbe`.
  The probe runs `celery inspect ping` against the pod's own worker node,
  so it catches a worker that has stopped consuming while its process stays up and the pod still reports Ready.
  It is off by default because a failing probe restarts the pod and destroys the execution it was running,
  so the timings have to suit how long a provider's diagnostics take. ([#30](https://github.com/Climate-REF/climate-ref-aft/pulls/30))

### Bug Fixes

- Sets `ESMVALTOOL_CONFIG_DIR` in the esmvaltool worker deployment whenever the chart mounts the managed config, instead of relying on a `values.yaml` env default that an override could drop.
  This guarantees esmvalcore reads the mounted memory caps, because it silently ignores a config directory it is not pointed at. ([#29](https://github.com/Climate-REF/climate-ref-aft/pulls/29))
- Removes the `CELERY_ACCEPT_CONTENT` default from `defaults.env`.

  The chart set it to the JSON array string `["json", "pickle"]`, which the worker now parses as a comma separated list.
  That yields the content type `["json"`, and the worker exits at startup with `SerializerNotInstalled`.
  The image owns the default now, which is `json,ref-json`.

  Registers a display-only `ref-json` codec in Flower's `celeryconfig.py`, so its result API can decode a task body. ([#32](https://github.com/Climate-REF/climate-ref-aft/pulls/32))


## climate-ref-aft 0.5.1 (2026-07-27)

### Improvements

- Adds a Helm render test suite so chart template regressions are caught in CI in seconds rather than by the minikube deployment job. ([#23](https://github.com/Climate-REF/climate-ref-aft/pulls/23))
- Add a Renovate config so a new `climate-ref` release opens a bump PR covering all six pinned locations. ([#25](https://github.com/Climate-REF/climate-ref-aft/pulls/25))

### Bug Fixes

- Various clean ups and fixes, including fixing of stale versions in the README.md,
   per-provider Helm values being silently ignored when the same key had a non-empty value under `defaults`,
  `dragonfly.enabled=false` aborting the Helm render. ([#23](https://github.com/Climate-REF/climate-ref-aft/pulls/23))

### Improved Documentation

- Split the development instructions out of the README into a new `DEVELOPMENT.md`. ([#24](https://github.com/Climate-REF/climate-ref-aft/pulls/24))

### Trivial/Internal Changes

- [#26](https://github.com/Climate-REF/climate-ref-aft/pulls/26)


## climate-ref-aft 0.5.0 (2026-07-27)

### Features

- Added `api.extraEnvFrom` and `defaults.extraEnvFrom`, so the api, the workers and the db-migrate hook can take environment from an existing Secret or ConfigMap instead of from values. ([#22](https://github.com/Climate-REF/climate-ref-aft/pulls/22))

### Bug Fixes

- Per-provider Helm values now override the `defaults` block, so `replicaCount` and other non-empty defaults can be set per provider. The db-migrate hook also honours `defaults.nodeSelector`, `defaults.affinity` and `defaults.tolerations`. ([#21](https://github.com/Climate-REF/climate-ref-aft/pulls/21))


## climate-ref-aft 0.4.0 (2026-07-17)

### Breaking Changes

- The minimum supported Python version is now 3.12; Python 3.11 is no longer supported.
  This follows climate-ref v0.15.0, which raised its own floor to 3.12.

  climate-ref v0.15.0 also made the diagnostic input-dataset hash deterministic across pandas versions and platforms.
  Because the underlying hash values change, existing databases will re-run each execution once on first use after upgrading. ([#17](https://github.com/Climate-REF/climate-ref-aft/pulls/17))

### Improvements

- Bump pinned climate-ref core, celery, esmvaltool, pmp, and ilamb components and the worker container image (helm + docker-compose) from ``v0.14.3`` to ``v0.16.2``. ([#17](https://github.com/Climate-REF/climate-ref-aft/pulls/17))
- Bump the API + frontend image (`climate-ref-frontend`) from ``v0.3.0`` to ``v0.4.0``.
  The v0.3.0 image bundled climate-ref 0.13.x, which could not read executions from a database written by the 0.16.x workers, so the API reported an empty executions list.
  v0.4.0 bundles climate-ref 0.16.2, matching the pinned components. ([#19](https://github.com/Climate-REF/climate-ref-aft/pulls/19))

### Trivial/Internal Changes

- [#18](https://github.com/Climate-REF/climate-ref-aft/pulls/18)


## climate-ref-aft 0.3.0 (2026-07-01)

### Features

- Add first-class ``concurrency`` and free-form ``extraArgs`` values to provider workers, rendered as Celery worker options after the ``--`` separator, and pin the esmvaltool worker to ``concurrency: 1`` to prevent node OOM kills when its Dask cluster fans out. ([#16](https://github.com/Climate-REF/climate-ref-aft/pulls/16))

### Improvements

- Add a CI-gated test that enforces consistent component version pins across ``pyproject.toml``, ``versions.toml``, the Helm chart, and ``docker-compose.yaml``, and track the frontend image version in ``versions.toml``. ([#15](https://github.com/Climate-REF/climate-ref-aft/pulls/15))

### Bug Fixes

- The Helm chart now refuses to render when ``api.env.ENVIRONMENT=production`` and ``api.env.SECRET_KEY`` is unset or left as the placeholder, preventing accidental production deploys with a known secret key. ([#15](https://github.com/Climate-REF/climate-ref-aft/pulls/15))

### Improved Documentation

- Correct the stale Helm install version in the README quick-start (``0.1.0`` to ``0.2.2``). ([#15](https://github.com/Climate-REF/climate-ref-aft/pulls/15))

### Trivial/Internal Changes

- [#15](https://github.com/Climate-REF/climate-ref-aft/pulls/15)


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
