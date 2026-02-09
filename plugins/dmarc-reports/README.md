# DMARC Aggregate Reports Plugin

Haraka plugin that receives, parses, and analyzes DMARC aggregate reports (RFC 7489) with Prometheus metrics export.

## Overview

DMARC (Domain-based Message Authentication, Reporting, and Conformance) aggregate reports are XML files that email providers send to report on email authentication results. This plugin:

1. Accepts mail to `dmarc@domain` addresses
2. Extracts XML from gzip/zip attachments
3. Parses DMARC aggregate report structure
4. Exports statistics to Prometheus for monitoring/alerting
5. Optionally stores reports and sends webhooks

## Configuration

### DMARC DNS Record

Set up your DMARC record to send reports to your mail relay:

```
_dmarc.example.com TXT "v=DMARC1; p=reject; rua=mailto:dmarc@example.com; ruf=mailto:dmarc@example.com"
```

### Helm Values

```yaml
inbound:
  enabled: true
  recipients:
    - dmarc  # Accept dmarc@domain

  dmarcReports:
    enabled: true
    metricsPort: 8094
    storeReports: false
    storePath: /data/dmarc-reports
    maxReportAge: 90
    webhook:
      enabled: false
      url: ""
```

### Haraka Configuration (dmarc_reports.ini)

```ini
[main]
enabled=true
metrics_port=8094
store_reports=false
store_path=/data/dmarc-reports
max_report_age=90

[webhook]
enabled=false
url=http://localhost:8888/dmarc-report
timeout=5000
```

## Prometheus Metrics

All metrics have prefix `haraka_dmarc_`:

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `reports_total` | Counter | `reporter_org`, `domain` | Total DMARC reports received |
| `messages_total` | Counter | `domain`, `disposition`, `dkim`, `spf` | Messages reported in aggregate reports |
| `source_messages_total` | Counter | `domain`, `source_ip`, `disposition` | Messages per source IP (top talkers) |
| `alignment_total` | Counter | `domain`, `dkim_aligned`, `spf_aligned` | DKIM/SPF alignment statistics |
| `policy_info` | Gauge | `domain`, `policy`, `subdomain_policy`, `pct` | Published DMARC policy |
| `last_report_age_seconds` | Gauge | `domain` | Age of last processed report |
| `parse_errors_total` | Counter | `error_type` | Parsing errors by type |

### Example Queries

```promql
# Reports received per day
sum(increase(haraka_dmarc_reports_total[24h])) by (domain)

# DMARC pass rate
sum(rate(haraka_dmarc_messages_total{dkim="pass"}[1h])) by (domain)
/ sum(rate(haraka_dmarc_messages_total[1h])) by (domain)

# Messages by disposition (none/quarantine/reject)
sum by (domain, disposition) (haraka_dmarc_messages_total)

# Top source IPs with failures
topk(10, sum by (source_ip) (haraka_dmarc_source_messages_total{disposition!="none"}))

# Domains with reject policy
haraka_dmarc_policy_info{policy="reject"}
```

## Grafana Dashboard

Example dashboard panels:

### DMARC Pass Rate by Domain
```promql
sum(rate(haraka_dmarc_messages_total{dkim="pass",spf="pass"}[24h])) by (domain)
/ sum(rate(haraka_dmarc_messages_total[24h])) by (domain) * 100
```

### Policy Enforcement Rate
```promql
sum by (domain, disposition) (increase(haraka_dmarc_messages_total[24h]))
```

### Report Sources
```promql
sum by (reporter_org) (increase(haraka_dmarc_reports_total[7d]))
```

## Webhook Payload

When webhook is enabled, each parsed report triggers:

```json
{
  "event": "dmarc_report",
  "timestamp": "2025-02-09T12:00:00.000Z",
  "report": {
    "reporter_org": "google.com",
    "reporter_email": "noreply-dmarc-support@google.com",
    "report_id": "12345678901234567890",
    "date_range": {
      "begin": 1736899200,
      "end": 1736985600
    },
    "policy_domain": "example.com",
    "policy": "reject",
    "subdomain_policy": "reject",
    "pct": 100,
    "adkim": "r",
    "aspf": "r",
    "records": [
      {
        "source_ip": "203.0.113.1",
        "count": 150,
        "disposition": "none",
        "dkim": "pass",
        "spf": "pass",
        "header_from": "example.com",
        "dkim_domain": "example.com",
        "dkim_result": "pass",
        "spf_domain": "example.com",
        "spf_result": "pass"
      }
    ]
  }
}
```

## Accepted Addresses

The plugin accepts mail to:
- `dmarc@domain`
- `dmarc-reports@domain`
- `_dmarc@domain`

## Report Formats

Supported DMARC report formats:
- Raw XML
- Gzip-compressed XML (.xml.gz)
- Zip-archived XML (.zip)
- Base64-encoded attachments

## Endpoints

The plugin exposes HTTP endpoints on the configured `metricsPort`:

| Endpoint | Description |
|----------|-------------|
| `/metrics` | Prometheus metrics |
| `/health` | Health check |
| `/stats` | JSON statistics |

## Alerting Examples

### Low DMARC Pass Rate
```yaml
- alert: DmarcPassRateLow
  expr: |
    sum(rate(haraka_dmarc_messages_total{dkim="pass",spf="pass"}[1h])) by (domain)
    / sum(rate(haraka_dmarc_messages_total[1h])) by (domain) < 0.9
  for: 1h
  labels:
    severity: warning
  annotations:
    summary: "DMARC pass rate below 90% for {{ $labels.domain }}"
```

### Unexpected Source IPs
```yaml
- alert: DmarcUnexpectedSource
  expr: |
    increase(haraka_dmarc_source_messages_total{disposition="reject"}[1h]) > 100
  for: 10m
  labels:
    severity: critical
  annotations:
    summary: "High volume of rejected mail from {{ $labels.source_ip }} for {{ $labels.domain }}"
```
