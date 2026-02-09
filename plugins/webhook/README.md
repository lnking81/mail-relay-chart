# Haraka Webhook Plugin for Delivery Notifications

Custom plugin to send webhook notifications for email delivery events (delivered, bounced, deferred).

Supports **multiple webhook endpoints** with independent event filtering.

## Installation

This plugin is copied into the Haraka container via ConfigMap.

## Configuration

Create `config/webhook.ini`:

```ini
[main]
; Enable webhooks globally
enabled=true

; Global settings (applied to all endpoints)
timeout=5000
retry=true
max_retries=3

; First endpoint - main API
[endpoint.main-api]
url=https://api.example.com/webhooks/email
events=delivered,bounced
header_Authorization=Bearer your-secret-token
header_X-Source=mail-relay

; Second endpoint - analytics/logging
[endpoint.analytics]
url=http://localhost:8888/webhook
events=delivered,bounced,deferred

; Third endpoint - alerting (bounces only)
[endpoint.alerts]
url=https://alerts.example.com/email-bounces
events=bounced
header_X-Api-Key=alert-token-123
```

## Helm Values Example

```yaml
webhooks:
  enabled: true
  timeout: 5000
  retry: true
  maxRetries: 3
  endpoints:
    - name: main-api
      url: "https://api.example.com/webhooks/email"
      events:
        - delivered
        - bounced
      headers:
        Authorization: "Bearer token123"
    - name: debug-echo
      url: "http://localhost:8888/webhook"
      events:
        - delivered
        - bounced
        - deferred
```

## Webhook Payload

```json
{
  "event": "delivered|bounced|deferred",
  "timestamp": "2024-01-15T10:30:00.000Z",
  "message_id": "<unique-message-id@domain>",
  "from": "sender@domain.com",
  "to": ["recipient@example.com"],
  "subject": "Email subject",
  "host": "mail.example.com",
  "response": "250 2.0.0 OK",
  "delay": 1234,
  "metadata": {
    "queue_id": "abc123",
    "attempts": 1
  }
}
```

For bounces:

```json
{
  "event": "bounced",
  "bounce_type": "hard|soft",
  "bounce_code": "550",
  "bounce_message": "User unknown"
}
```
