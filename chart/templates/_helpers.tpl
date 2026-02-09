{{/*
Expand the name of the chart.
*/}}
{{- define "mail-relay.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
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
DKIM selector for a domain - single source of truth
Usage: {{ include "mail-relay.dkimSelector" $domain }}
*/}}
{{- define "mail-relay.dkimSelector" -}}
{{- .dkimSelector | default "mail" -}}
{{- end }}

{{/*
DKIM secret name for a domain
Usage: {{ include "mail-relay.dkimSecretName" (list $ $domain) }}
*/}}
{{- define "mail-relay.dkimSecretName" -}}
{{- $ctx := index . 0 -}}
{{- $domain := index . 1 -}}
{{- printf "%s-dkim-%s" (include "mail-relay.fullname" $ctx) ($domain.name | replace "." "-") -}}
{{- end }}

{{/*
Trusted networks list for Haraka relay plugin
*/}}
{{- define "mail-relay.trustedNetworks" -}}
{{- range .Values.mail.trustedNetworks }}
{{ . }}
{{- end }}
{{- end }}

{{/*
DMARC record value
Usage: {{ include "mail-relay.dmarcRecord" (list $domain $.Values) }}
*/}}
{{- define "mail-relay.dmarcRecord" -}}
{{- $domain := index . 0 -}}
{{- $values := index . 1 -}}
{{/*
  RUA priority:
  1. Explicit dns.dmarcRua setting
  2. dmarc@domain if inbound.dmarcReports.enabled (to receive aggregate reports)
  3. postmaster@domain as default
*/}}
{{- $rua := "" -}}
{{- if $values.dns.dmarcRua -}}
  {{- $rua = $values.dns.dmarcRua -}}
{{- else if $values.inbound.dmarcReports.enabled -}}
  {{- $rua = printf "dmarc@%s" $domain.name -}}
{{- else -}}
  {{- $rua = printf "postmaster@%s" $domain.name -}}
{{- end -}}
{{/*
  PCT handling:
  - If dmarcPct is explicitly set, use it
  - If dmarcReports.enabled and dmarcPct is empty, use 100 for full visibility
  - Otherwise, omit pct (RFC default is 100)
*/}}
{{- $pct := "" -}}
{{- if $values.dns.dmarcPct -}}
  {{- $pct = printf "; pct=%s" (toString $values.dns.dmarcPct) -}}
{{- else if $values.inbound.dmarcReports.enabled -}}
  {{- $pct = "; pct=100" -}}
{{- end -}}
v=DMARC1; p={{ $values.dns.dmarcPolicy }}{{ $pct }}; rua=mailto:{{ $rua }}
{{- end }}

{{/*
SPF record value
Usage: {{ include "mail-relay.spfRecord" (list "${IP}" $.Values) }}
*/}}
{{- define "mail-relay.spfRecord" -}}
{{- $ip := index . 0 -}}
{{- $values := index . 1 -}}
v=spf1 ip4:{{ $ip }} {{ $values.dns.spfPolicy }}
{{- end }}

{{/*
Check if IP detection is needed (not using static IPs)
*/}}
{{- define "mail-relay.needsIpDetection" -}}
{{- if and .Values.dns.enabled (not .Values.dns.ip.static) -}}
true
{{- end -}}
{{- end }}

{{/*
Image name with tag
*/}}
{{- define "mail-relay.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end }}

{{/*
DNS Manager owner ID
*/}}
{{- define "mail-relay.dnsOwnerId" -}}
{{- .Values.dns.ownership.ownerId | default (printf "%s/%s" .Release.Namespace .Release.Name) -}}
{{- end }}

{{/*
DNS Manager environment variables
Used by dns-init and dns-watcher containers
*/}}
{{- define "mail-relay.dnsEnvVars" -}}
- name: DNS_PROVIDER
  value: {{ .Values.dns.provider | quote }}
- name: DNS_OWNER_ID
  value: {{ include "mail-relay.dnsOwnerId" . | quote }}
- name: NAMESPACE
  valueFrom:
    fieldRef:
      fieldPath: metadata.namespace
- name: RELEASE_NAME
  value: {{ .Release.Name | quote }}
- name: SERVICE_NAME
  value: {{ include "mail-relay.fullname" . | quote }}
- name: MAIL_HOSTNAME
  value: {{ .Values.mail.hostname | quote }}
- name: MAIL_DOMAINS
  value: {{ .Values.mail.domains | toJson | quote }}
- name: DNS_TTL
  value: {{ .Values.dns.ttl | quote }}
- name: DNS_CREATE_A
  value: {{ .Values.dns.records.a | quote }}
- name: DNS_CREATE_MX
  value: {{ .Values.dns.records.mx | quote }}
- name: DNS_CREATE_SPF
  value: {{ .Values.dns.records.spf | quote }}
- name: DNS_CREATE_DKIM
  value: {{ .Values.dns.records.dkim | quote }}
- name: DNS_CREATE_DMARC
  value: {{ .Values.dns.records.dmarc | quote }}
- name: DNS_SPF_POLICY
  value: {{ .Values.dns.spfPolicy | quote }}
- name: DNS_DMARC_POLICY
  value: {{ .Values.dns.dmarcPolicy | quote }}
{{/*
  Pass DMARC PCT to DNS manager:
  - Explicit value if set
  - 100 if dmarcReports enabled (full visibility)
  - Empty = omit from record (RFC default is 100)
*/}}
{{- if .Values.dns.dmarcPct }}
- name: DNS_DMARC_PCT
  value: {{ .Values.dns.dmarcPct | quote }}
{{- else if .Values.inbound.dmarcReports.enabled }}
- name: DNS_DMARC_PCT
  value: "100"
{{- end }}
{{/*
  Pass DMARC RUA to DNS manager:
  - Explicit value if set
  - dmarc@<first-domain> if dmarcReports enabled
  - Empty = use default postmaster@domain
*/}}
{{- if .Values.dns.dmarcRua }}
- name: DNS_DMARC_RUA
  value: {{ .Values.dns.dmarcRua | quote }}
{{- else if .Values.inbound.dmarcReports.enabled }}
- name: DNS_DMARC_RUA
  value: dmarc@{{ (index .Values.mail.domains 0).name }}
{{- end }}
- name: DETECT_OUTBOUND_IP
  value: {{ .Values.dns.ip.detectOutbound | quote }}
{{- if .Values.dns.ip.static }}
- name: STATIC_IPS
  value: {{ .Values.dns.ip.static | join "," | quote }}
{{- end }}
{{- if eq .Values.dns.provider "cloudflare" }}
- name: CF_API_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ .Values.dns.cloudflare.existingSecret | default (printf "%s-cloudflare" (include "mail-relay.fullname" .)) }}
      key: api-token
- name: CLOUDFLARE_PROXIED
  value: {{ .Values.dns.cloudflare.proxied | quote }}
{{- if .Values.dns.cloudflare.zoneIds }}
- name: CLOUDFLARE_ZONE_IDS
  value: {{ range $domain, $zoneId := .Values.dns.cloudflare.zoneIds }}{{ $domain }}:{{ $zoneId }},{{ end }}
{{- end }}
{{- end }}
{{- end }}
