# Haraka Strip Headers Plugin

Removes internal/sensitive headers from outgoing emails to prevent information disclosure about your infrastructure.

## Features

- **Strip Internal Received Headers**: Removes `Received` headers containing:
  - RFC1918 private IPs (10.x.x.x, 172.16-31.x.x, 192.168.x.x)
  - Loopback addresses (127.x.x.x, ::1)
  - Link-local addresses (169.254.x.x, fe80::)
  - IPv6 ULA (fd00::/8)
  - Kubernetes pod names (patterns like `app-abc123-xy456`)
  - Internal domains (.local, .internal, .cluster.local, .svc, .pod)

- **Strip All Received Headers**: Option to remove ALL Received headers for maximum privacy

- **Strip Custom Headers**: Configure additional headers to remove (X-Originating-IP, X-Mailer, etc.)

## Why?

When clients send email to your relay, they add `Received` headers containing internal details:

```
Received: from openmagic-api-worker-notifications-5b8d8c7878-529xw ([10.42.3.77])
          by mail.alerts.omagic.ai (Haraka) with ESMTP id DFCF7BEA-2F88-4D9B-A9BB-320EB10BDCE0.1
          envelope-from <noreply@alerts.omagic.ai>; Tue, 10 Feb 2026 13:29:01 +0000
```

This exposes:

- Internal pod/service names (Kubernetes topology)
- Internal IP addresses (network structure)
- Software versions and infrastructure details

This plugin strips such headers before outbound delivery.

## Configuration

### Helm Values

```yaml
haraka:
  stripHeaders:
    enabled: true
    # Remove ALL Received headers (maximum privacy)
    stripAllReceived: false
    # Remove only Received headers with internal IPs/hostnames
    stripInternalReceived: true
    # Additional headers to always strip
    headers:
      - X-Originating-IP
      - X-Mailer
    # Additional hostname patterns to detect as internal (regex)
    internalHostnamePatterns:
      - "my-internal-pattern"
```

### Manual Configuration (strip_headers.ini)

```ini
[main]
enabled=true
; Strip ALL Received headers (maximum privacy)
strip_all_received=false
; Strip only Received headers with internal IPs/hostnames
strip_internal_received=true

[headers]
; Headers to always strip
strip[]=X-Originating-IP
strip[]=X-Mailer

[internal]
; Additional regex patterns for internal hostnames
hostname_patterns[]=\.my-internal\.domain$
```

## Default Internal Detection Patterns

### IP Ranges (RFC1918 + Special)

- `10.0.0.0/8` - Class A private
- `172.16.0.0/12` - Class B private
- `192.168.0.0/16` - Class C private
- `127.0.0.0/8` - Loopback
- `169.254.0.0/16` - Link-local
- `fd00::/8` - IPv6 ULA
- `fe80::/10` - IPv6 link-local

### Hostname Patterns

- Kubernetes pod names: `-[a-z0-9]{5,10}-[a-z0-9]{4,5}` (e.g., `app-abc123-xy456`)
- `.local` domains
- `.internal` domains
- `.cluster.local` (Kubernetes)
- `.svc` and `.pod` (Kubernetes short names)
- `localhost`

## Hooks

- `data_post` - Strips headers after message data is received, before queuing

**Important:** Only processes **outbound/relay** connections (where `connection.relaying=true`).
Inbound mail (bounces, FBL, external senders) is NOT modified — their routing headers are preserved for debugging.

**DKIM:** Headers are stripped BEFORE DKIM signing (DKIM signs on `queue_outbound`), so the signature remains valid.

## Testing

After enabling, send a test email and check the outbound message headers:

```bash
# Check raw email headers
kubectl exec -n mail deployment/mail-relay -- \
  haraka -c /app/config --dump-headers

# Or send test and check received email
swaks --to recipient@external.com --from sender@yourdomain.com \
  --server your-relay:25
```

The internal Received headers should be gone from the delivered message.
