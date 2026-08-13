Bump pinned climate-ref core, celery, esmvaltool, pmp, and ilamb components
and the worker container image (helm + docker-compose) from ``v0.16.2`` to ``v0.17.0``.
This release carries the Celery queue routing table and the per-execution resource capture
(`ref executions resources`) that informs the slug-to-size routing rules.
The pinned `climate-ref-frontend` v0.4.0 image still bundles climate-ref 0.16.2,
so solves triggered through the API ignore the routing table until a new frontend release lands.
