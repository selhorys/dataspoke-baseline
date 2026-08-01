{{/*
Expand the name of the chart.
*/}}
{{- define "dataspoke.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "dataspoke.fullname" -}}
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
{{- define "dataspoke.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "dataspoke.labels" -}}
helm.sh/chart: {{ include "dataspoke.chart" . }}
{{ include "dataspoke.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "dataspoke.selectorLabels" -}}
app.kubernetes.io/name: {{ include "dataspoke.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Render an image reference for one of the DataSpoke-owned workloads (api,
frontend, event-consumer). Takes the workload's `image:` map as context
(keys: repository, tag, digest — pullPolicy is ignored here).

Pins to "<repository>@<digest>" when install.sh resolved a content digest for
this run (see resolve_image_digest in helm-charts/bin/lib/helpers.sh and
_resolve_digest_or_abort in install.sh) — an immutable reference makes
imagePullPolicy: IfNotPresent safe even under a mutable tag, because a cached
"repo@sha256:X" can only ever be content X, and it makes the pod-template hash
change exactly when image content changes so Helm rolls the Deployment by
construction. Renders the mutable "<repository>:<tag>" ONLY when the operator
passed --no-digest-pin, the explicit escape hatch that skips digest
resolution entirely and substitutes an explicit `kubectl rollout restart`
(plus imagePullPolicy: Always, so that restart actually re-pulls) after the
upgrade instead. A registry lookup failure, a missing CLI, or any other
resolution error does NOT fall through to this "<repository>:<tag>" branch —
install.sh aborts the whole install before the umbrella `helm upgrade` ever
renders this template. See spec/feature/HELM_CHART.md §Digest stamping.

Used by this chart's own api-deployment.yaml (context: `.Values.api.image`).
The frontend and event-consumer subcharts define their own chart-scoped
`frontend.imageRef` / `event-consumer.imageRef` (identical body) instead of
calling this one, so each subchart lints and renders standalone
(`helm lint helm-charts/dataspoke/subcharts/frontend`) without depending on a
named template defined only in this parent chart — Helm's named-template
namespace is global within one render, so the umbrella install works either
way, but a subchart rendered alone would fail to resolve a template it does
not itself define.
*/}}
{{- define "dataspoke.imageRef" -}}
{{- if .digest -}}
{{- printf "%s@%s" .repository .digest -}}
{{- else -}}
{{- printf "%s:%s" .repository .tag -}}
{{- end -}}
{{- end }}
