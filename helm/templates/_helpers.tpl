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
Resolve the Celery broker URL.
Uses the bundled Dragonfly subchart when it is enabled,
otherwise the operator must point the chart at their own broker.
An absent dragonfly.enabled means enabled, which is how Helm reads the subchart condition,
so that `helm upgrade --reuse-values` from a release predating externalBroker still renders.
*/}}
{{- define "ref.brokerUrl" -}}
{{- $dragonfly := .Values.dragonfly | default dict -}}
{{- $enabled := true -}}
{{- if hasKey $dragonfly "enabled" -}}
{{- $enabled = $dragonfly.enabled -}}
{{- end -}}
{{- if $enabled -}}
redis://{{ include "dragonfly.fullname" .Subcharts.dragonfly }}:{{ $dragonfly.service.port }}
{{- else -}}
{{- $url := required "dragonfly.enabled is false, so externalBroker.url must be set to your own Celery broker" (.Values.externalBroker | default dict).url -}}
{{- $url | replace "'" "''" -}}
{{- end -}}
{{- end -}}
