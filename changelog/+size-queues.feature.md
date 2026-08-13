Adds size-based Celery queue routing to the chart.
A new `celeryRoutes` value writes a TOML routing table to a ConfigMap,
exposed to the API and every worker via `REF_CELERY_ROUTES`.
Worker instances under `providers.*` gain `provider` and `queues` fields,
so differently sized pools of one provider can consume size-specific queues
such as `esmvaltool-large`.
Requires climate-ref v0.17.0 or newer. Without `celeryRoutes` set,
behaviour is unchanged.
