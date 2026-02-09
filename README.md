# Mail Relay Helm Chart

Production-ready Helm chart for deploying a high-performance SMTP mail relay on Kubernetes with [Haraka](https://haraka.github.io/), DKIM signing, and automated DNS management.

## Features

| Category           | Features                                                                             |
| ------------------ | ------------------------------------------------------------------------------------ |
| **Mail Server**    | Haraka SMTP server with 50+ concurrent connections, HAProxy PROXY Protocol support   |
| **Security**       | DKIM signing (auto-generated keys), sender validation whitelist/blacklist, SMTP AUTH |
| **DNS**            | Automatic SPF/DKIM/DMARC/MX records via Cloudflare API or external-dns               |
| **Delivery**       | Static + adaptive rate limiting, per-domain throttling, IP warmup support            |
| **Inbound**        | VERP bounce processing, FBL (Feedback Loop) handling, HMAC-protected addresses       |
| **Webhooks**       | Delivery events (delivered, bounced, deferred, complaint) to multiple endpoints      |
| **Monitoring**     | Prometheus metrics, Grafana dashboards, real-time Watch dashboard                    |
| **Infrastructure** | Kubernetes-native, non-root container, Network Policies, PVC for queue               |

---

## Table of Contents

- [Quick Start](#quick-start)
- [Architecture](#architecture)
- [Configuration Reference](#configuration-reference)
  - [Basic Mail Settings](#basic-mail-settings)
  - [DKIM](#dkim)
  - [DNS Automation](#dns-automation)
  - [Services](#services)
  - [PROXY Protocol](#proxy-protocol)
  - [Sender Validation](#sender-validation)
  - [SMTP Authentication](#smtp-authentication)
  - [Rate Limiting](#rate-limiting)
  - [Adaptive Rate Limiting](#adaptive-rate-limiting)
  - [Inbound Mail (Bounces/FBL)](#inbound-mail-bouncesfbl)
  - [Webhooks](#webhooks)
  - [Monitoring](#monitoring)
- [Security](#security)
- [Operations](#operations)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

---

## Quick Start

### Prerequisites

- Kubernetes 1.20+
- Helm 3.2+
- (Optional) Cloudflare account for DNS automation

### Installation

```bash
# Add repo (if published to GitHub Pages)
helm repo add mail-relay https://lnking81.github.io/mail-relay-chart
helm repo update

# Or install from local clone
git clone https://github.com/lnking81/mail-relay-chart.git
cd mail-relay-chart

# Minimal installation
helm install mail-relay ./chart -n mail --create-namespace \
  --set mail.hostname=mail.example.com \
  --set mail.domains[0].name=example.com

# With custom values
helm install mail-relay ./chart -n mail --create-namespace -f my-values.yaml
```

### Minimal values.yaml

```yaml
mail:
  hostname: mail.example.com
  domains:
    - name: example.com
      dkimSelector: mail

services:
  - type: LoadBalancer
    port: 25
    targetPort: 25
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Kubernetes Cluster                        │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                    Mail Relay Pod                          │  │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐   │  │
│  │  │   Haraka    │  │ DNS Watcher │  │ Init Containers │   │  │
│  │  │  (SMTP)     │  │  (Sidecar)  │  │ - DKIM Init     │   │  │
│  │  │             │  │             │  │ - DNS Init      │   │  │
│  │  │  Plugins:   │  │ - Monitor   │  └─────────────────┘   │  │
│  │  │  - DKIM     │  │   IP changes│                        │  │
│  │  │  - Relay    │  │ - Update    │  ┌─────────────────┐   │  │
│  │  │  - Webhook  │  │   DNS       │  │   Volumes       │   │  │
│  │  │  - Limit    │  └─────────────┘  │ - Config        │   │  │
│  │  │  - ...      │                   │ - DKIM Keys     │   │  │
│  │  └──────┬──────┘                   │ - Queue (PVC)   │   │  │
│  │         │                          └─────────────────┘   │  │
│  └─────────┼────────────────────────────────────────────────┘  │
│            │                                                     │
│  ┌─────────▼─────────┐    ┌────────────────────┐               │
│  │  Service (LB/NP)  │    │  Secrets           │               │
│  │  Port 25          │    │  - DKIM private    │               │
│  │  PROXY Protocol   │    │  - Cloudflare API  │               │
│  └─────────┬─────────┘    └────────────────────┘               │
└────────────┼────────────────────────────────────────────────────┘
             │
             ▼
┌────────────────────────┐    ┌────────────────────┐
│  External Clients      │    │  DNS Provider      │
│  (Applications)        │    │  (Cloudflare/etc)  │
└────────────────────────┘    └────────────────────┘
```

### Components

| Component          | Description                                              |
| ------------------ | -------------------------------------------------------- |
| **Haraka**         | High-performance Node.js SMTP server                     |
| **DKIM Init**      | Generates RSA keys per domain, stores in K8s Secrets     |
| **DNS Init**       | Creates DNS records, waits for propagation               |
| **DNS Watcher**    | Monitors IP changes, updates DNS automatically           |
| **Custom Plugins** | Webhooks, adaptive rate, sender validation, VERP bounces |

---

## Configuration Reference

### Basic Mail Settings

```yaml
mail:
  # FQDN for SMTP banner and HELO
  hostname: mail.example.com

  # Domains to handle (each gets its own DKIM key)
  domains:
    - name: example.com
      dkimSelector: mail
    - name: another.com
      dkimSelector: mail

  # Networks allowed to relay (no auth required)
  trustedNetworks:
    - "10.0.0.0/8"
    - "172.16.0.0/12"
    - "192.168.0.0/16"

  # Upstream relay (optional - direct delivery if disabled)
  relay:
    enabled: false
    host: smtp.sendgrid.net
    port: 587
    tls: true
    auth:
      enabled: true
      username: apikey
      password: ""
      existingSecret: "" # Secret with keys: username, password
```

### Haraka Settings

```yaml
haraka:
  # Custom SMTP banner (hides version)
  smtpBanner: "ready"
  bannerUuidChars: 0 # Hide UUID in banner

  # Max message size (bytes), 0 = unlimited
  maxMessageSize: 26214400 # 25 MB

  # Outbound concurrency
  concurrency: 50

  # IP preference: "default", "v4", "v6"
  inetPrefer: "v4"

  # Queue directory
  queueDir: /data/queue
```

### DKIM

```yaml
dkim:
  # Enable DKIM signing
  enabled: true

  # RSA key size (2048 or 4096)
  keySize: 2048

  # Keys auto-generated and stored in K8s Secrets:
  # {release}-dkim-{domain-with-dashes}
```

DKIM keys are generated automatically on first install. To view:

```bash
kubectl get secret -n mail -l app.kubernetes.io/component=dkim
```

### DNS Automation

```yaml
dns:
  enabled: true

  # Provider: "cloudflare" or "external-dns"
  provider: cloudflare

  cloudflare:
    apiToken: "" # Use existingSecret in production
    existingSecret: "cloudflare-api-token"
    # zoneIds:    # Optional, auto-detected from domain
    #   example.com: "abc123..."

  # Records to create
  records:
    a: true # A record for mail hostname
    mx: true # MX record for domains
    spf: true # SPF TXT record
    dkim: true # DKIM TXT record
    dmarc: true # DMARC TXT record

  # Policies
  spfPolicy: "~all" # ~all (softfail), -all (hardfail)
  dmarcPolicy: none # none, quarantine, reject
  ttl: 300

  # IP detection
  ip:
    fromService: true # Detect from LoadBalancer
    detectOutbound: true # Detect outbound NAT IP
    static: [] # Or specify manually

  # IP change watcher sidecar
  watcher:
    enabled: true
    interval: 60

  # Wait for DNS before starting Haraka
  waitForRecords:
    enabled: true
    timeout: 600
```

### Services

Flexible multi-service configuration:

```yaml
services:
  # Main LoadBalancer with PROXY protocol
  - name: "" # Empty = release name
    type: LoadBalancer
    port: 25
    targetPort: 25
    proxyProtocol: true
    loadBalancerClass: "hcloud"
    externalTrafficPolicy: "Local"

  # Internal ClusterIP (no PROXY protocol)
  - name: internal
    type: ClusterIP
    port: 25
    targetPort: 2525
    proxyProtocol: false
```

Each unique `targetPort` creates a Haraka listener. Ports with `proxyProtocol: true` expect PROXY headers.

### PROXY Protocol

Preserve real client IP through load balancers:

```yaml
haraka:
  proxyProtocol:
    # Auto-detect trusted proxies from LoadBalancer status
    autoDetect: true

    # Or specify manually
    trustedProxies:
      - "10.0.0.0/8"
      - "192.168.1.100"

services:
  - type: LoadBalancer
    port: 25
    targetPort: 25
    proxyProtocol: true # This port expects PROXY headers
```

### Sender Validation

Restrict who can send through this relay:

```yaml
mail:
  senderValidation:
    enabled: true
    checkFromHeader: true # Verify From header matches MAIL FROM

    # Whitelist (if empty, all mail.domains allowed)
    allowedFrom:
      - "service@partner.com" # Specific address
      - "partner.com" # Entire domain
      - "/.*@notifications\\./" # Regex pattern

    # Blacklist (takes priority)
    forbiddenFrom:
      - "ceo@example.com"
      - "hr@example.com"
```

### SMTP Authentication

Allow external clients to authenticate:

```yaml
auth:
  enabled: true
  methods: "PLAIN,LOGIN"
  requireTls: true
  constrainSender: true # MAIL FROM must match auth user

  # Inline users (use existingSecret in production)
  users:
    sender@example.com: "password123"

  # Or use existing secret
  existingSecret: "smtp-auth-users"
```

### Rate Limiting

Static per-domain rate limits:

```yaml
rateLimit:
  enabled: true

  global:
    maxConnections: 100
    maxPerConnection: 50
    delayBetweenMessages: 0

  domains:
    gmail.com:
      enabled: true
      maxConnections: 1
      maxPerConnection: 3
      rate: "1/20s"
      delayMs: 15000

    outlook.com:
      enabled: true
      maxConnections: 2
      rate: "1/10s"

  customDomains:
    corporate.com:
      maxConnections: 5
      rate: "10/1m"
```

### Adaptive Rate Limiting

Auto-adjust delivery speed based on server responses:

```yaml
adaptiveRate:
  enabled: true

  defaults:
    minDelay: 1000 # Never faster than 1s
    maxDelay: 60000 # Never slower than 60s
    initialDelay: 5000 # Start at 5s
    backoffMultiplier: 1.5 # +50% on failure
    recoveryRate: 0.9 # -10% on success
    successThreshold: 5 # Successes before speedup

  domains:
    gmail.com:
      enabled: true
      minDelay: 15000
      maxDelay: 120000
      initialDelay: 20000
      backoffMultiplier: 2.0
      successThreshold: 10
```

**How it works:**

1. Start at `initialDelay`
2. On 421/rate-limit: `delay × backoffMultiplier`
3. On N consecutive successes: `delay × recoveryRate`
4. Bounded by `minDelay` / `maxDelay`

### Inbound Mail (Bounces/FBL)

Handle bounces and spam complaints with correlation to original message:

```yaml
inbound:
  enabled: true
  clientIdHeader: "X-Message-ID" # Your app provides this

  recipients:
    - postmaster
    - abuse
    - "bounce+" # VERP: bounce+{id}@domain
    - fbl

  bounce:
    enabled: true
    verpPrefix: "bounce+"
    useSenderDomain: true # bounce+id@sender-domain.com
    hmacSecret: "" # Auto-generated if empty
    requireHmac: true # Reject forged bounces
    maxAgeDays: 7

  security:
    spf:
      enabled: true
      rejectFail: true
    dkim:
      enabled: true
    dmarc:
      enabled: true
      rejectOnFail: true
```

**Flow:**

```
1. Client sends: X-Message-ID: order-123
2. Haraka sets: Return-Path: bounce+{ts}-{hmac}-order-123@domain
3. Bounce arrives: To: bounce+{ts}-{hmac}-order-123@domain
4. Webhook: { event: "bounce_received", message_id: "order-123" }
```

### Webhooks

Send delivery events to HTTP endpoints:

```yaml
webhooks:
  enabled: true
  timeout: 5000
  retry: true
  maxRetries: 3

  endpoints:
    - name: main
      url: "https://api.example.com/webhooks/email"
      events:
        - delivered
        - bounced
        - bounce_received
        - complaint
      headers:
        Authorization: "Bearer token123"

    - name: debug
      url: "http://localhost:8888/webhook"
      events:
        - delivered
        - bounced
        - deferred
```

**Webhook payload:**

```json
{
  "event": "delivered",
  "timestamp": "2024-01-15T10:30:00.000Z",
  "message_id": "<uuid@domain>",
  "from": "sender@domain.com",
  "to": ["recipient@example.com"],
  "subject": "Hello",
  "host": "mail.example.com",
  "response": "250 OK",
  "delay": 1234
}
```

### Monitoring

```yaml
metrics:
  enabled: true

  serviceMonitor:
    enabled: true
    interval: 30s
    labels:
      release: monitoring

dashboard:
  watch:
    enabled: true # Real-time SMTP traffic
    sampling: false

  logReader:
    enabled: false

  port: 8080

  grafana:
    enabled: true
    labels:
      grafana_dashboard: "1"
```

---

## Security

### Open Relay Prevention

> **CRITICAL**: Misconfigured mail relay = spam source = IP blacklisted

**Required steps:**

1. **Restrict trusted networks:**

```yaml
mail:
  trustedNetworks:
    - "10.42.0.0/16" # Your specific pod CIDR only
    # NOT 10.0.0.0/8!
```

2. **Preserve client IP:**

```yaml
services:
  - type: LoadBalancer
    externalTrafficPolicy: "Local" # Preserves real IP
    proxyProtocol: true # Or use PROXY protocol
```

3. **Enable network policy:**

```yaml
networkPolicy:
  enabled: true
  allowedCidrs:
    - "10.42.0.0/16"
```

**Test for open relay:**

```bash
# From EXTERNAL network
telnet <loadbalancer-ip> 25
EHLO test
MAIL FROM:<attacker@evil.com>
RCPT TO:<victim@gmail.com>

# Expected: 550 Relay access denied
# BAD: 250 OK (you're an open relay!)
```

### Container Security

- Runs as non-root (uid 1000)
- Read-only root filesystem
- No privileged mode
- Security context enforced

---

## Operations

### View DKIM Keys

```bash
# List DKIM secrets
kubectl get secrets -n mail -l app.kubernetes.io/component=dkim

# Get public key for DNS
kubectl get secret -n mail mail-relay-dkim-example-com \
  -o jsonpath='{.data.dns\.record}' | base64 -d
```

### Test SMTP

```bash
# Port forward
kubectl port-forward -n mail svc/mail-relay 2525:25

# Test with swaks
swaks --to test@gmail.com --from sender@example.com \
  --server localhost:2525 --port 2525
```

### View Queue

```bash
kubectl exec -n mail deployment/mail-relay -- \
  haraka -c /app -l queue
```

### Logs

```bash
# Main container
kubectl logs -n mail deployment/mail-relay -c haraka -f

# DNS watcher
kubectl logs -n mail deployment/mail-relay -c dns-watcher -f
```

### Backup Queue

```bash
POD=$(kubectl get pod -n mail -l app.kubernetes.io/name=mail-relay -o name | head -1)
kubectl exec -n mail $POD -- tar czf /tmp/queue.tar.gz /data/queue
kubectl cp mail/${POD#pod/}:/tmp/queue.tar.gz ./queue-backup.tar.gz
```

---

## Troubleshooting

### Pod Won't Start

```bash
# Check events
kubectl describe pod -n mail -l app.kubernetes.io/name=mail-relay

# Check init containers
kubectl logs -n mail -l app.kubernetes.io/name=mail-relay -c dkim-init
kubectl logs -n mail -l app.kubernetes.io/name=mail-relay -c dns-init
```

### DNS Not Creating

```bash
# Check DNS init logs
kubectl logs -n mail -l app.kubernetes.io/name=mail-relay -c dns-init

# Verify Cloudflare token
kubectl get secret -n mail cloudflare-api-token -o yaml
```

### Mail Not Delivering

```bash
# Check outbound logs
kubectl logs -n mail deployment/mail-relay -c haraka | grep -i "failed\|error\|bounce"

# Test DNS resolution
kubectl exec -n mail deployment/mail-relay -- dig +short MX gmail.com
```

### Rate Limited by Gmail

```bash
# Check adaptive rate metrics
kubectl exec -n mail deployment/mail-relay -- \
  curl -s localhost:8093/metrics | grep adaptive_rate
```

Enable more aggressive backoff:

```yaml
adaptiveRate:
  domains:
    gmail.com:
      backoffMultiplier: 2.5
      successThreshold: 15
```

---

## Development

### Project Structure

```
mail-relay-chart/
├── chart/                    # Helm chart
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
├── plugins/                  # Custom Haraka plugins
│   ├── adaptive-rate/        # Dynamic rate limiting
│   ├── webhook/              # Delivery webhooks
│   ├── sender-validation/    # From address whitelist/blacklist
│   ├── inbound-handler/      # Bounce/FBL processing
│   ├── outbound-headers/     # VERP Return-Path
│   ├── rcpt-to-inbound/      # Inbound routing
│   └── dmarc-verify/         # DMARC verification
├── scripts/                  # DNS management (Python)
│   ├── dns_manager.py
│   ├── dns_watcher.py
│   └── dns/
├── Dockerfile                # Node.js Alpine + Haraka
└── README.md
```

### Building Image

```bash
docker build -t mail-relay:dev .

# Multi-arch
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/lnking81/mail-relay-chart:dev --push .
```

### Local Testing

```bash
# Lint
helm lint ./chart

# Template
helm template test ./chart -f test-values.yaml

# Install
helm install test ./chart -n mail --create-namespace -f test-values.yaml
```

---

## License

MIT License - see [LICENSE](LICENSE)

## Links

- [GitHub Repository](https://github.com/lnking81/mail-relay-chart)
- [Haraka Documentation](https://haraka.github.io/)
- [Issues](https://github.com/lnking81/mail-relay-chart/issues)
