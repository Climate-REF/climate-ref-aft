# Changelog

All notable changes to the Climate REF AFT deployment will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/).

<!-- towncrier release notes start -->

## climate-ref-aft 0.6.1 (2026-08-17)

### Features

- Adds a `keda` block to the worker `defaults`,
  rendering a KEDA ScaledObject that scales a worker on the depth of the queues it consumes,
  down to zero when they are empty.

  - The chart derives one redis trigger per queue the instance consumes, pointed at the bundled Dragonfly.
  - An optional Prometheus trigger holds the pods up while a diagnostic is still executing.

  This replaces `autoscaling` rather than layering on it,
  because two autoscalers on one Deployment fight over its replica count.

  ([#48](https://github.com/Climate-REF/climate-ref-aft/pulls/48))
- Adds `api.priorityClassName` and `defaults.priorityClassName`,
  so the API and the workers can be given separate scheduling priorities.
  The worker value is overridable per instance, letting one provider sit below the rest. ([#49](https://github.com/Climate-REF/climate-ref-aft/pulls/49))


## climate-ref-aft 0.6.0 (2026-08-17)

No significant changes.


## climate-ref-aft 0.5.5 (2026-08-16)

### Breaking Changes

- Move the orchestrator from `providers.orchestrator` to a top-level `orchestrator` block.
  Provider workers now default to a read-only `/ref` with a shared read-write `/ref/scratch`,
  while the orchestrator and the migrate Job keep write access to the whole tree. ([#39](https://github.com/Climate-REF/climate-ref-aft/pulls/39))

### Bug Fixes

- Render `api.httpRoute.filters` and `flower.httpRoute.filters` on the HTTPRoute rule.
  Filters set in values were silently dropped, so a route intended to sit behind a forward-auth Middleware was reachable without it. ([#39](https://github.com/Climate-REF/climate-ref-aft/pulls/39))

### Trivial/Internal Changes

- [#42](https://github.com/Climate-REF/climate-ref-aft/pulls/42)


## climate-ref-aft 0.5.4 (2026-08-13)

### Improvements

- Bumps the pinned climate-ref components from 0.17.0 to 0.17.1. ([#38](https://github.com/Climate-REF/climate-ref-aft/pulls/38))


## climate-ref-aft 0.5.3 (2026-08-13)

### Features

- Adds size-based Celery queue routing to the chart.
  A new `celeryRoutes` value writes a TOML routing table to a ConfigMap,
  exposed to the API and every worker via `REF_CELERY_ROUTES`.
  Worker instances under `providers.*` gain `provider` and `queues` fields,
  so differently sized pools of one provider can consume size-specific queues
  such as `esmvaltool-large`.
  Requires climate-ref v0.17.0 or newer. Without `celeryRoutes` set,
  behaviour is unchanged. ([#35](https://github.com/Climate-REF/climate-ref-aft/pulls/35))

### Improvements

- Makes the chart-managed provider config generic, so any provider can carry one rather than esmvaltool alone.
  `config` is the document, `configMountPath` is the directory it mounts into, and `configEnvVar` optionally points an environment variable at it.
  Provider templates now resolve their values through a single `ref.providerSpec` helper, so override precedence is defined in one place.
  The ServiceAccount a component mounts and the one the chart creates for it are both resolved by a single `ref.serviceAccountName` helper, so the two cannot disagree. ([#33](https://github.com/Climate-REF/climate-ref-aft/pulls/33))
- Ships resource requests, limits and task time limits for every pod rather than leaving them unset. ([#34](https://github.com/Climate-REF/climate-ref-aft/pulls/34))
- Bumps the API + frontend image (`climate-ref-frontend`) from ``v0.4.0`` to ``v0.4.1``.
  v0.4.1 bundles climate-ref 0.17.0, matching the pinned components,
  so the API reads a database that carries the 0.17.0 migrations,
  including the new per-execution resource columns. ([#35](https://github.com/Climate-REF/climate-ref-aft/pulls/35))
- Bumps the pinned climate-ref core, celery, esmvaltool, pmp, and ilamb components
  and the worker container image (helm + docker-compose) from ``v0.16.2`` to ``v0.17.0``.
  This release carries the Celery queue routing table and the per-execution resource capture
  (`ref executions resources`) that informs the slug-to-size routing rules. ([#35](https://github.com/Climate-REF/climate-ref-aft/pulls/35))
- Moves the integration job onto the arc-climate-ref runner scale set.
  Each run now starts from a fresh database on ephemeral storage,
  so one branch's schema migrations can no longer poison another branch's runs. ([#37](https://github.com/Climate-REF/climate-ref-aft/pulls/37))

### Bug Fixes

- Keys each provider worker's `checksum/config` annotation to its own Secret rather than to every provider's.
  A change to one provider's `env` no longer restarts the other workers and re-runs their in-flight executions.
  The annotations also cover a provider's chart-managed `config`, so a config change now restarts the worker that mounts it. ([#33](https://github.com/Climate-REF/climate-ref-aft/pulls/33))
- Wait for Dragonfly before starting the Celery workers.
  A worker that connected while the broker was still starting could wedge silently,
  leaving its queue without a consumer and hanging the CI Simple Solve step. ([#37](https://github.com/Climate-REF/climate-ref-aft/pulls/37))


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
