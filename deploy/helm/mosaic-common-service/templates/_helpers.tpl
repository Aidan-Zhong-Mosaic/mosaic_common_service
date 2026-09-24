{{/*
Base name for resources, honoring a nameOverride if ever added.
*/}}
{{- define "mosaic-common-service.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified app name, prefixed with the release name unless it's already
part of it (mirrors the standard helm create scaffold).
*/}}
{{- define "mosaic-common-service.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "mosaic-common-service.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "mosaic-common-service.labels" -}}
helm.sh/chart: {{ include "mosaic-common-service.chart" . }}
{{ include "mosaic-common-service.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "mosaic-common-service.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mosaic-common-service.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "mosaic-common-service.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "mosaic-common-service.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Name of the Secret holding the Redshift credentials JSON - either a pre-created
one the caller points at, or the one this chart creates from values.
*/}}
{{- define "mosaic-common-service.credentialsSecretName" -}}
{{- if .Values.redshiftCredentials.existingSecret -}}
{{- .Values.redshiftCredentials.existingSecret -}}
{{- else -}}
{{- printf "%s-redshift-credentials" (include "mosaic-common-service.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "mosaic-common-service.credentialsSecretKey" -}}
{{- if .Values.redshiftCredentials.existingSecret -}}
{{- .Values.redshiftCredentials.existingSecretKey -}}
{{- else -}}
credentials.json
{{- end -}}
{{- end -}}
