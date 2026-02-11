# Webhook Events JSON Examples

This document contains JSON examples for all webhook events sent by the mail relay.

## Table of Contents

- [Delivered Event](#delivered-event)
- [Bounced Event](#bounced-event)
  - [Hard Bounce](#hard-bounce)
  - [Soft Bounce](#soft-bounce)
  - [Partial Delivery](#partial-delivery)
- [Deferred Event](#deferred-event)
- [Bounce Received Event](#bounce-received-event) (inbound DSN)
- [Complaint Event](#complaint-event) (FBL/spam report)
- [Test Event](#test-event)

---

## Delivered Event

Sent when an email is successfully delivered to the recipient's mail server.

```json
{
  "event": "delivered",
  "timestamp": "2026-02-11T14:30:00.000Z",
  "message_id": "<abc123-456def@example.com>",
  "queue_id": "1A2B3C4D-EFGH-5678-IJKL-9MNOPQRSTUV",
  "from": "sender@yourdomain.com",
  "to": ["recipient@example.com"],
  "host": "mail.example.com",
  "response": "250 2.0.0 OK: Message queued as 1234567890",
  "delay": 1523,
  "metadata": {
    "attempts": 1,
    "mx_host": "mail.example.com"
  }
}
```

### Multiple Recipients

```json
{
  "event": "delivered",
  "timestamp": "2026-02-11T14:30:00.000Z",
  "message_id": "<abc123-456def@example.com>",
  "queue_id": "1A2B3C4D-EFGH-5678-IJKL-9MNOPQRSTUV",
  "from": "newsletter@yourdomain.com",
  "to": ["user1@example.com", "user2@example.com", "user3@example.com"],
  "host": "mx.example.com",
  "response": "250 2.0.0 OK",
  "delay": 2150,
  "metadata": {
    "attempts": 1,
    "mx_host": "mx.example.com"
  }
}
```

---

## Bounced Event

Sent when an email cannot be delivered.

### Hard Bounce

Permanent delivery failure (5xx SMTP codes). Usually indicates invalid address.

```json
{
  "event": "bounced",
  "timestamp": "2026-02-11T14:35:00.000Z",
  "message_id": "<abc123-456def@example.com>",
  "queue_id": "1A2B3C4D-EFGH-5678-IJKL-9MNOPQRSTUV",
  "from": "sender@yourdomain.com",
  "to": ["nonexistent@example.com"],
  "bounce_type": "hard",
  "bounce_code": "550",
  "bounce_message": "550 5.1.1 The email account that you tried to reach does not exist",
  "metadata": {
    "attempts": 1,
    "reason": "",
    "error_details": {
      "code": "550",
      "msg": "5.1.1 The email account that you tried to reach does not exist",
      "component": "remote"
    }
  }
}
```

#### User Unknown / Mailbox Not Found

```json
{
  "event": "bounced",
  "timestamp": "2026-02-11T14:35:00.000Z",
  "message_id": "<abc123-456def@example.com>",
  "queue_id": "1A2B3C4D-EFGH-5678-IJKL-9MNOPQRSTUV",
  "from": "sender@yourdomain.com",
  "to": ["unknown@example.com"],
  "bounce_type": "hard",
  "bounce_code": "550",
  "bounce_message": "550 5.1.1 User unknown",
  "metadata": {
    "attempts": 1,
    "reason": "",
    "error_details": {
      "code": "550",
      "msg": "User unknown",
      "component": "remote"
    }
  }
}
```

#### Rejected by Policy / Blacklisted

```json
{
  "event": "bounced",
  "timestamp": "2026-02-11T14:35:00.000Z",
  "message_id": "<abc123-456def@example.com>",
  "queue_id": "1A2B3C4D-EFGH-5678-IJKL-9MNOPQRSTUV",
  "from": "sender@yourdomain.com",
  "to": ["user@strict-policy.com"],
  "bounce_type": "hard",
  "bounce_code": "550",
  "bounce_message": "550 5.7.1 Message rejected due to content policy",
  "metadata": {
    "attempts": 1,
    "reason": "",
    "error_details": {
      "code": "550",
      "msg": "Message rejected due to content policy",
      "component": "remote"
    }
  }
}
```

#### Domain Does Not Exist

```json
{
  "event": "bounced",
  "timestamp": "2026-02-11T14:35:00.000Z",
  "message_id": "<abc123-456def@example.com>",
  "queue_id": "1A2B3C4D-EFGH-5678-IJKL-9MNOPQRSTUV",
  "from": "sender@yourdomain.com",
  "to": ["user@nonexistent-domain.invalid"],
  "bounce_type": "hard",
  "bounce_code": "550",
  "bounce_message": "550 Domain not found",
  "metadata": {
    "attempts": 3,
    "reason": "No MX records found",
    "error_details": {
      "code": "550",
      "msg": "Domain not found",
      "component": "dns"
    }
  }
}
```

### Soft Bounce

Temporary delivery failure (4xx SMTP codes). Usually retried automatically.

```json
{
  "event": "bounced",
  "timestamp": "2026-02-11T14:40:00.000Z",
  "message_id": "<abc123-456def@example.com>",
  "queue_id": "1A2B3C4D-EFGH-5678-IJKL-9MNOPQRSTUV",
  "from": "sender@yourdomain.com",
  "to": ["user@busy-server.com"],
  "bounce_type": "soft",
  "bounce_code": "450",
  "bounce_message": "450 4.7.1 Please try again later",
  "metadata": {
    "attempts": 3,
    "reason": "temporary",
    "error_details": {
      "code": "450",
      "msg": "Please try again later",
      "component": "remote"
    }
  }
}
```

#### Mailbox Full / Over Quota

```json
{
  "event": "bounced",
  "timestamp": "2026-02-11T14:40:00.000Z",
  "message_id": "<abc123-456def@example.com>",
  "queue_id": "1A2B3C4D-EFGH-5678-IJKL-9MNOPQRSTUV",
  "from": "sender@yourdomain.com",
  "to": ["user@example.com"],
  "bounce_type": "soft",
  "bounce_code": "452",
  "bounce_message": "452 4.2.2 Mailbox full - user over quota",
  "metadata": {
    "attempts": 2,
    "reason": "",
    "error_details": {
      "code": "452",
      "msg": "Mailbox full - user over quota",
      "component": "remote"
    }
  }
}
```

#### Server Temporarily Unavailable

```json
{
  "event": "bounced",
  "timestamp": "2026-02-11T14:40:00.000Z",
  "message_id": "<abc123-456def@example.com>",
  "queue_id": "1A2B3C4D-EFGH-5678-IJKL-9MNOPQRSTUV",
  "from": "sender@yourdomain.com",
  "to": ["user@example.com"],
  "bounce_type": "soft",
  "bounce_code": "421",
  "bounce_message": "421 4.7.0 Service temporarily unavailable, please try again later",
  "metadata": {
    "attempts": 2,
    "reason": "",
    "error_details": {
      "code": "421",
      "msg": "Service temporarily unavailable",
      "component": "remote"
    }
  }
}
```

#### Rate Limited / Greylisting

```json
{
  "event": "bounced",
  "timestamp": "2026-02-11T14:40:00.000Z",
  "message_id": "<abc123-456def@example.com>",
  "queue_id": "1A2B3C4D-EFGH-5678-IJKL-9MNOPQRSTUV",
  "from": "sender@yourdomain.com",
  "to": ["user@greylist-server.com"],
  "bounce_type": "soft",
  "bounce_code": "451",
  "bounce_message": "451 4.7.1 Greylisting in effect, please retry in 300 seconds",
  "metadata": {
    "attempts": 1,
    "reason": "",
    "error_details": {
      "code": "451",
      "msg": "Greylisting in effect, please retry in 300 seconds",
      "component": "remote"
    }
  }
}
```

### Partial Delivery

Some recipients succeeded, some failed.

```json
{
  "event": "bounced",
  "timestamp": "2026-02-11T14:45:00.000Z",
  "message_id": "<abc123-456def@example.com>",
  "queue_id": "1A2B3C4D-EFGH-5678-IJKL-9MNOPQRSTUV",
  "from": "sender@yourdomain.com",
  "to": ["invalid1@example.com", "invalid2@example.com"],
  "bounce_type": "partial",
  "bounce_code": "250",
  "bounce_message": "Some recipients failed",
  "metadata": {
    "attempts": 1,
    "reason": "",
    "error_details": {
      "code": "250",
      "msg": "Some recipients failed",
      "component": "remote"
    }
  }
}
```

---

## Deferred Event

Sent when an email delivery is temporarily delayed and will be retried.

```json
{
  "event": "deferred",
  "timestamp": "2026-02-11T14:50:00.000Z",
  "message_id": "<abc123-456def@example.com>",
  "queue_id": "1A2B3C4D-EFGH-5678-IJKL-9MNOPQRSTUV",
  "from": "sender@yourdomain.com",
  "to": ["user@slow-server.com"],
  "host": "mx.slow-server.com",
  "response": "451 4.7.1 Please try again later",
  "delay": 5000,
  "next_attempt": "2026-02-11T15:05:00.000Z",
  "metadata": {
    "attempts": 1,
    "reason": "temporary failure"
  }
}
```

### Connection Timeout

```json
{
  "event": "deferred",
  "timestamp": "2026-02-11T14:50:00.000Z",
  "message_id": "<abc123-456def@example.com>",
  "queue_id": "1A2B3C4D-EFGH-5678-IJKL-9MNOPQRSTUV",
  "from": "sender@yourdomain.com",
  "to": ["user@unreachable.com"],
  "host": "",
  "response": "Connection timed out",
  "delay": 30000,
  "next_attempt": "2026-02-11T15:20:00.000Z",
  "metadata": {
    "attempts": 2,
    "reason": "connection timeout"
  }
}
```

### No MX Response

```json
{
  "event": "deferred",
  "timestamp": "2026-02-11T14:50:00.000Z",
  "message_id": "<abc123-456def@example.com>",
  "queue_id": "1A2B3C4D-EFGH-5678-IJKL-9MNOPQRSTUV",
  "from": "sender@yourdomain.com",
  "to": ["user@dns-issues.com"],
  "host": "",
  "response": "DNS lookup failed",
  "delay": 0,
  "next_attempt": "2026-02-11T15:10:00.000Z",
  "metadata": {
    "attempts": 1,
    "reason": "DNS temporarily unavailable"
  }
}
```

### Server Busy

```json
{
  "event": "deferred",
  "timestamp": "2026-02-11T14:50:00.000Z",
  "message_id": "<abc123-456def@example.com>",
  "queue_id": "1A2B3C4D-EFGH-5678-IJKL-9MNOPQRSTUV",
  "from": "sender@yourdomain.com",
  "to": ["user@busy-mx.com"],
  "host": "mx.busy-mx.com",
  "response": "421 4.7.0 Too many connections, try again later",
  "delay": 2500,
  "next_attempt": "2026-02-11T15:00:00.000Z",
  "metadata": {
    "attempts": 1,
    "reason": "too many connections"
  }
}
```

---

## Bounce Received Event

Sent when an inbound DSN (Delivery Status Notification) bounce is received. This happens when a remote server sends back a bounce notification asynchronously (after initial acceptance).

Requires `inbound.enabled: true` in Helm values.

### Standard DSN Bounce

```json
{
  "event": "bounce_received",
  "timestamp": "2026-02-11T15:00:00.000Z",
  "message_id": "order-123-abc456",
  "verp_recipient": "bounce+1707661200.a1b2c3d4.order-123-abc456@yourdomain.com",
  "bounce_type": "hard",
  "diagnostic_code": "smtp; 550 5.1.1 The email account that you tried to reach does not exist",
  "status": "5.1.1",
  "remote_mta": "mail.example.com",
  "original_recipient": "nonexistent@example.com",
  "reporting_mta": "mx.example.com",
  "hmac_validated": true,
  "raw_dsn": "Reporting-MTA: dns; mx.example.com\nFinal-Recipient: rfc822; nonexistent@example.com\nAction: failed\nStatus: 5.1.1\n..."
}
```

### Soft Bounce (Temporary Failure)

```json
{
  "event": "bounce_received",
  "timestamp": "2026-02-11T15:00:00.000Z",
  "message_id": "newsletter-789",
  "verp_recipient": "bounce+1707661200.f5e6d7c8.newsletter-789@yourdomain.com",
  "bounce_type": "soft",
  "diagnostic_code": "smtp; 452 4.2.2 Mailbox full",
  "status": "4.2.2",
  "remote_mta": "mail.recipient.com",
  "original_recipient": "user@recipient.com",
  "reporting_mta": "mx.recipient.com",
  "hmac_validated": true,
  "raw_dsn": "..."
}
```

### Without HMAC (Legacy Address)

When `require_hmac: false` in config, legacy bounce addresses are accepted:

```json
{
  "event": "bounce_received",
  "timestamp": "2026-02-11T15:00:00.000Z",
  "message_id": "",
  "verp_recipient": "bounce+user=example.com@yourdomain.com",
  "bounce_type": "unknown",
  "diagnostic_code": "smtp; 550 User not found",
  "status": "5.0.0",
  "remote_mta": "mail.example.com",
  "original_recipient": "user@example.com",
  "reporting_mta": "bounce.example.com",
  "hmac_validated": false,
  "raw_dsn": "..."
}
```

---

## Complaint Event

Sent when an FBL (Feedback Loop) spam complaint is received. Major email providers (Microsoft, Yahoo, etc.) send these when users mark emails as spam.

Requires `inbound.enabled: true` and FBL address registered with email providers.

### Standard Spam Complaint

```json
{
  "event": "complaint",
  "timestamp": "2026-02-11T15:05:00.000Z",
  "message_id": "promo-campaign-456",
  "fbl_recipient": "fbl@yourdomain.com",
  "feedback_type": "abuse",
  "user_agent": "Feedback-Loop/1.0 (Microsoft JMRP)",
  "source_ip": "40.92.10.25",
  "original_from": "marketing@yourdomain.com",
  "original_to": "subscriber@outlook.com",
  "original_subject": "Special Offer Just For You!",
  "arrival_date": "Mon, 10 Feb 2026 09:30:00 -0000"
}
```

### Different Feedback Types

#### Abuse (Spam)

```json
{
  "event": "complaint",
  "timestamp": "2026-02-11T15:05:00.000Z",
  "message_id": "email-abc123",
  "fbl_recipient": "fbl@yourdomain.com",
  "feedback_type": "abuse",
  "user_agent": "Yahoo!-Mail-Feedback/2.0",
  "source_ip": "",
  "original_from": "newsletter@yourdomain.com",
  "original_to": "user@yahoo.com",
  "original_subject": "Weekly Newsletter",
  "arrival_date": "Sun, 09 Feb 2026 14:00:00 +0000"
}
```

#### Fraud

```json
{
  "event": "complaint",
  "timestamp": "2026-02-11T15:05:00.000Z",
  "message_id": "suspicious-email-789",
  "fbl_recipient": "fbl@yourdomain.com",
  "feedback_type": "fraud",
  "user_agent": "AOL-Feedback-Reporter/1.0",
  "source_ip": "64.12.88.100",
  "original_from": "service@yourdomain.com",
  "original_to": "user@aol.com",
  "original_subject": "Account Verification Required",
  "arrival_date": "Sat, 08 Feb 2026 11:15:00 -0500"
}
```

#### Not Spam (False Positive Report)

```json
{
  "event": "complaint",
  "timestamp": "2026-02-11T15:05:00.000Z",
  "message_id": "legit-email-999",
  "fbl_recipient": "fbl@yourdomain.com",
  "feedback_type": "not-spam",
  "user_agent": "Feedback-Loop/1.0 (Microsoft SNDS)",
  "source_ip": "",
  "original_from": "support@yourdomain.com",
  "original_to": "customer@hotmail.com",
  "original_subject": "Your Support Ticket #12345",
  "arrival_date": "Fri, 07 Feb 2026 16:45:00 +0000"
}
```

---

## Test Event

Sent when testing webhook connectivity.

```json
{
  "event": "test",
  "timestamp": "2026-02-11T14:55:00.000Z",
  "message": "Webhook connectivity test"
}
```

---

## HTTP Request Details

All webhooks are sent as HTTP POST requests with:

| Header         | Value                      |
| -------------- | -------------------------- |
| Content-Type   | `application/json`         |
| User-Agent     | `Haraka-Webhook/1.0`       |
| Custom Headers | As configured per endpoint |

### Retry Behavior

- **Default retries**: 3 attempts
- **Backoff strategy**: Exponential (2^attempt × 1000ms)
- **Retry delays**: 2s, 4s, 8s
- **Success codes**: `2xx` HTTP status codes

### Configuration Example

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
        - bounce_received # Inbound DSN bounces (requires inbound.enabled)
        - complaint # FBL spam complaints (requires inbound.enabled)
      headers:
        Authorization: "Bearer your-secret-token"
        X-Source: "mail-relay"
    - name: analytics
      url: "http://localhost:8888/webhook"
      events:
        - delivered
        - bounced
        - deferred
        - bounce_received
        - complaint
```

## Event Summary

| Event             | Source          | Description                                            |
| ----------------- | --------------- | ------------------------------------------------------ |
| `delivered`       | webhook plugin  | Email successfully delivered to remote MTA             |
| `bounced`         | webhook plugin  | Immediate delivery failure (sync bounce)               |
| `deferred`        | webhook plugin  | Temporary failure, will retry                          |
| `bounce_received` | inbound-handler | Async DSN bounce received (requires `inbound.enabled`) |
| `complaint`       | inbound-handler | FBL spam complaint (requires `inbound.enabled`)        |
| `test`            | webhook plugin  | Connectivity test event                                |
