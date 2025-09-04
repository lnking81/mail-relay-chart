{{/*
Expand the name of the chart.
*/}}
{{- define "mail-relay.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "mail-relay.fullname" -}}
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
{{- define "mail-relay.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "mail-relay.labels" -}}
helm.sh/chart: {{ include "mail-relay.chart" . }}
{{ include "mail-relay.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "mail-relay.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mail-relay.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "mail-relay.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "mail-relay.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Get the external DNS hostname
*/}}
{{- define "mail-relay.externalDnsHostname" -}}
{{- if .Values.externalDns.hostname }}
{{- .Values.externalDns.hostname }}
{{- else }}
{{- .Values.mail.hostname }}
{{- end }}
{{- end }}

{{/*
Generate trusted hosts list for OpenDKIM
*/}}
{{- define "mail-relay.trustedHosts" -}}
{{- range .Values.mail.trustedNetworks }}
{{ . }}
{{- end }}
{{- range .Values.mail.trustedIPs }}
{{ . }}
{{- end }}
localhost
127.0.0.1
{{ .Values.mail.hostname }}
{{- range .Values.mail.domains }}
{{ .name }}
{{- end }}
{{- end }}

{{/*
Generate DKIM key table entries
*/}}
{{- define "mail-relay.dkimKeyTable" -}}
{{- range .Values.mail.domains }}
{{ .dkimSelector }}._domainkey.{{ .name }} {{ .name }}:{{ .dkimSelector }}:/etc/opendkim/keys/{{ .name }}.private
{{- end }}
{{- end }}

{{/*
Generate DKIM signing table entries
*/}}
{{- define "mail-relay.dkimSigningTable" -}}
{{- range .Values.mail.domains }}
*@{{ .name }} {{ .dkimSelector }}._domainkey.{{ .name }}
{{- end }}
{{- end }}
