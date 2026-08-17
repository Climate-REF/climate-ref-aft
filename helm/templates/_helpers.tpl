{{/*
Expand the name of the chart.
*/}}
{{- define "ref.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "ref.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "ref.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "ref.labels" -}}
helm.sh/chart: {{ include "ref.chart" . }}
{{ include "ref.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "ref.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ref.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Fail the render if the API runs in production with an unset or placeholder
SECRET_KEY. Operators must supply a strong, unique api.env.SECRET_KEY.
*/}}
{{- define "ref.validateApiSecret" -}}
{{- if .Values.api.enabled -}}
{{- $env := .Values.api.env -}}
{{- $secret := toString (default "" $env.SECRET_KEY) -}}
{{- /* api.extraEnvFrom can carry SECRET_KEY, and this template cannot see inside it. */ -}}
{{- if and (not .Values.api.extraEnvFrom) (eq (toString (default "" $env.ENVIRONMENT)) "production") (or (eq $secret "") (eq $secret "changethis")) -}}
{{- fail "api.env.SECRET_KEY is unset or the 'changethis' placeholder while api.env.ENVIRONMENT=production. Set it, or supply it through api.extraEnvFrom, before deploying." -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Resolve the ServiceAccount name for a component.
Takes a dict of `root`, `component` (the name suffix) and `serviceAccount` (the component's block).
An explicit name wins, otherwise the chart names the account after the component.
Returns an empty string when the component neither names nor creates one,
so a Deployment can omit the field via `with` and fall back to the namespace default.
Both the Deployment that mounts the account and the template that creates it resolve it here,
because a pod naming an account the chart does not create is not admitted.
*/}}
{{- define "ref.serviceAccountName" -}}
{{- $sa := .serviceAccount | default dict -}}
{{- if $sa.name -}}
{{ $sa.name }}
{{- else if $sa.create -}}
{{ include "ref.fullname" .root }}-{{ .component }}
{{- end -}}
{{- end -}}

{{/*
The set of Celery worker instances the chart renders: the providers plus the orchestrator.
*/}}
{{- define "ref.workerInstances" -}}
{{- $instances := omit .Values.providers "defaults" -}}
{{- $orchestrator := .Values.orchestrator | default dict -}}
{{- if ne (toString $orchestrator.enabled) "false" -}}
{{- $instances = merge (dict "orchestrator" (omit $orchestrator "enabled")) $instances -}}
{{- end -}}
{{- toYaml $instances -}}
{{- end -}}

{{/*
Fail the render when the orchestrator is still configured under `providers`.
It moved to a top-level block in chart 0.6.0, and a stale entry would otherwise
render a second orchestrator Deployment consuming the same `celery` queue.
*/}}
{{- define "ref.validateOrchestrator" -}}
{{- if hasKey (.Values.providers | default dict) "orchestrator" -}}
{{- fail "providers.orchestrator moved to the top-level `orchestrator` block in chart 0.6.0. Move its values there and remove the providers.orchestrator key." -}}
{{- end -}}
{{- end -}}

{{/*
Resolve a provider's effective spec: the shared defaults with the provider's own values on top.
Takes a dict of `root` (the top level context) and `spec` (the provider's own values).
Every provider template must resolve its spec through here,
so that override precedence is defined in one place rather than per object.
*/}}
{{- define "ref.providerSpec" -}}
{{- toYaml (mergeOverwrite (deepCopy .root.Values.defaults) (.spec | default dict)) -}}
{{- end -}}

{{/*
The Celery queues an instance consumes.
Takes a dict of `instance` (the deployment identity) and `spec` (already resolved through ref.providerSpec).
An explicit `queues` wins, matching the `--queues` the Deployment passes to the worker.
Otherwise the worker consumes the single queue `start-worker` derives from its provider,
which is the provider name, or `celery` for the orchestrator.
Returns a YAML list, so callers must pipe it through `fromYamlArray`.
*/}}
{{- define "ref.instanceQueues" -}}
{{- $provider := .spec.provider | default .instance -}}
{{- if .spec.queues -}}
{{- toYaml .spec.queues -}}
{{- else if eq $provider "orchestrator" -}}
{{- toYaml (list "celery") -}}
{{- else -}}
{{- toYaml (list $provider) -}}
{{- end -}}
{{- end -}}

{{/*
Report whether an instance has any autoscaler attached, HPA or KEDA.
Takes the resolved spec. Returns a non-empty string when one is, so callers must use `include`.
The Deployment omits `replicas` in that case, because an autoscaler owns the field
and a chart-set value fights it back to the static count on every upgrade.
*/}}
{{- define "ref.autoscalerEnabled" -}}
{{- $autoscaling := .autoscaling | default dict -}}
{{- $keda := .keda | default dict -}}
{{- if or $autoscaling.enabled $keda.enabled -}}true{{- end -}}
{{- end -}}

{{/*
Broker address for a KEDA redis trigger, as `host:port` without a scheme.
Takes a dict of `root` and `keda` (the instance's resolved keda block).
Defaults to the bundled Dragonfly, because that is the broker the workers themselves use.
An external broker cannot be derived from externalBroker.url,
because KEDA's scaler wants the host and port alone and takes credentials through its own metadata.
*/}}
{{- define "ref.kedaRedisAddress" -}}
{{- $keda := .keda | default dict -}}
{{- if $keda.redisAddress -}}
{{ $keda.redisAddress }}
{{- else if include "ref.dragonflyEnabled" .root -}}
{{- $dragonfly := .root.Values.dragonfly | default dict -}}
{{ include "dragonfly.fullname" .root.Subcharts.dragonfly }}:{{ $dragonfly.service.port }}
{{- else -}}
{{- fail "keda.enabled is set while dragonfly.enabled is false, so keda.redisAddress must give the broker as host:port" -}}
{{- end -}}
{{- end -}}

{{/*
Render one provider's Secret.
Takes a dict of `root`, `provider` and `spec` (already resolved through ref.providerSpec).
The Deployment hashes this to key its pods to their own environment,
so a change to one provider does not restart the others.
*/}}
{{- define "ref.providerSecret" -}}
apiVersion: v1
kind: Secret
metadata:
  name: {{ include "ref.fullname" .root }}-{{ .provider }}
  labels:
    app.kubernetes.io/component: {{ .provider }}
    {{- include "ref.labels" .root | nindent 4 }}
stringData:
  {{- tpl (toYaml .spec.env) .root | nindent 2 }}
{{- end -}}

{{/*
Report whether the bundled Dragonfly subchart is deployed.
An absent dragonfly.enabled means enabled, which is how Helm reads the subchart condition,
so that `helm upgrade --reuse-values` from a release predating the key behaves unchanged.
Returns a non-empty string when enabled and an empty string when not,
so callers must use it via `include`, not by comparing to a boolean.
*/}}
{{- define "ref.dragonflyEnabled" -}}
{{- $dragonfly := .Values.dragonfly | default dict -}}
{{- if hasKey $dragonfly "enabled" -}}
{{- if $dragonfly.enabled -}}true{{- end -}}
{{- else -}}
true
{{- end -}}
{{- end -}}

{{/*
Init container that holds a pod back until the bundled Dragonfly answers on its port,
because a Celery client that connects while the broker is still starting can wedge silently.
Takes the root context.
*/}}
{{- define "ref.waitForDragonfly" -}}
{{- if include "ref.dragonflyEnabled" . -}}
{{- $dragonfly := .Values.dragonfly | default dict -}}
initContainers:
- name: wait-for-dragonfly
  image: busybox:1.37
  command:
  - sh
  - -c
  - |
    echo "Waiting for Dragonfly at {{ include "dragonfly.fullname" .Subcharts.dragonfly }}:{{ $dragonfly.service.port }}..."
    until nc -z {{ include "dragonfly.fullname" .Subcharts.dragonfly }} {{ $dragonfly.service.port }}; do
      echo "Dragonfly not ready, retrying in 2s..."
      sleep 2
    done
    echo "Dragonfly is ready"
{{- end -}}
{{- end -}}

{{/*
Resolve the Celery broker URL.
Uses the bundled Dragonfly subchart when it is deployed,
otherwise the operator must point the chart at their own broker via externalBroker.url.
The URL is escaped for the single-quoted YAML scalar that toYaml emits,
because tpl injects it after that quoting has already happened.
*/}}
{{- define "ref.brokerUrl" -}}
{{- $dragonfly := .Values.dragonfly | default dict -}}
{{- if include "ref.dragonflyEnabled" . -}}
redis://{{ include "dragonfly.fullname" .Subcharts.dragonfly }}:{{ $dragonfly.service.port }}
{{- else -}}
{{- $url := required "dragonfly.enabled is false, so externalBroker.url must be set to your own Celery broker" (.Values.externalBroker | default dict).url -}}
{{- $url | replace "'" "''" -}}
{{- end -}}
{{- end -}}

{{/*
Celery routing table wiring, shared by the API and worker deployments.
The mount and the REF_CELERY_ROUTES value both build on ref.celeryRoutesDir,
so the env var and the mounted file cannot drift apart.
*/}}
{{- define "ref.celeryRoutesDir" -}}/etc/climate-ref/routes{{- end }}

{{- define "ref.celeryRoutesVolumeMount" -}}
name: celery-routes
mountPath: {{ include "ref.celeryRoutesDir" . }}
readOnly: true
{{- end }}

{{- define "ref.celeryRoutesVolume" -}}
name: celery-routes
configMap:
  name: {{ include "ref.fullname" . }}-celery-routes
{{- end }}

{{- define "ref.celeryRoutesEnv" -}}
- name: REF_CELERY_ROUTES
  value: {{ include "ref.celeryRoutesDir" . }}/routes.toml
{{- end }}
