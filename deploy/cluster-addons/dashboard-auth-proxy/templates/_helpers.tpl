{{- define "dashboard-auth-proxy.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "dashboard-auth-proxy.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "dashboard-auth-proxy.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "dashboard-auth-proxy.labels" -}}
helm.sh/chart: {{ include "dashboard-auth-proxy.chart" . }}
{{ include "dashboard-auth-proxy.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "dashboard-auth-proxy.selectorLabels" -}}
app.kubernetes.io/name: {{ include "dashboard-auth-proxy.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
