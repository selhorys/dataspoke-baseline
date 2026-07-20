{{- define "event-consumer.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "event-consumer.fullname" -}}
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

{{- define "event-consumer.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "event-consumer.labels" -}}
helm.sh/chart: {{ include "event-consumer.chart" . }}
{{ include "event-consumer.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "event-consumer.selectorLabels" -}}
app.kubernetes.io/name: {{ include "event-consumer.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
  ServiceAccount the consumer pod runs as. With create=false the operator
  supplies the SA (and its RBAC); an empty name then falls back to the
  namespace default SA, which cannot read Secrets.
*/}}
{{- define "event-consumer.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "event-consumer.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
  K8s Secret holding the DataHub credentials. The name is a constant shared with
  the API's accessor (src/backend/admin/datahub_secret.py) — the consumer looks
  it up by that literal name, so exposing it as a value would let the chart
  claim a name the application never reads.
*/}}
{{- define "event-consumer.datahubSecretName" -}}
dataspoke-datahub-secret
{{- end }}
