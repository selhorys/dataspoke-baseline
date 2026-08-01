{{- define "frontend.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "frontend.fullname" -}}
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

{{- define "frontend.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "frontend.labels" -}}
helm.sh/chart: {{ include "frontend.chart" . }}
{{ include "frontend.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "frontend.selectorLabels" -}}
app.kubernetes.io/name: {{ include "frontend.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Render an image reference for this workload (context: `.Values.image`, keys:
repository, tag, digest — pullPolicy is ignored here). Mirrors the umbrella
chart's `dataspoke.imageRef` (dataspoke/templates/_helpers.tpl) byte-for-byte,
but defined chart-scoped so this subchart lints and renders standalone
(`helm lint helm-charts/dataspoke/subcharts/frontend`) without depending on a
named template defined only in the parent chart. Helm's named-template
namespace is global within one render, so the umbrella install is unaffected
either way — this subchart's own deployment.yaml calls this name instead of
`dataspoke.imageRef` to keep the standalone render working; do not also
define `dataspoke.imageRef` here, which would be last-one-wins across the
whole umbrella render.
*/}}
{{- define "frontend.imageRef" -}}
{{- if .digest -}}
{{- printf "%s@%s" .repository .digest -}}
{{- else -}}
{{- printf "%s:%s" .repository .tag -}}
{{- end -}}
{{- end }}
