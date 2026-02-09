# Outbound Headers Plugin

Adds VERP Return-Path and tracking headers for bounce/FBL correlation.

## Features

- Reads client-provided message ID from configurable header (default: `X-Message-ID`)
- Sets Return-Path to `bounce+{message_id}@{bounce_domain}` for VERP
- Adds `X-Haraka-MsgID` header for FBL report parsing
- Optionally adds `Feedback-ID` for Gmail FBL

## Configuration

Create `config/outbound_headers.ini`:

```ini
[main]
enabled=true
; Header to read client message ID from
client_id_header=X-Message-ID
; Domain for VERP bounce addresses
bounce_domain=mail.example.com
; Prefix for VERP addresses (bounce+{id}@domain)
bounce_prefix=bounce
; Add Feedback-ID header for Gmail FBL
add_feedback_id=true
; Tag for Feedback-ID
feedback_id_tag=mail-relay
; Use Haraka queue_id if client header not present
fallback_to_queue_id=true
```

## Usage

Client sends email with custom ID:

```
X-Message-ID: order-12345
From: shop@client.com
To: user@gmail.com
Subject: Your order
```

Plugin adds:

```
Return-Path: <bounce+order-12345@mail.example.com>
X-Haraka-MsgID: order-12345
Feedback-ID: order-12345:client.com:mail-relay:mail.example.com
```

When bounce/FBL is received, the `message_id` in webhook payload will be `order-12345`.

## Flow

```
Client → Haraka                          Remote MX
  │                                          │
  ├─ X-Message-ID: order-12345               │
  │                                          │
  ▼                                          │
[outbound-headers plugin]                    │
  │                                          │
  ├─ Return-Path: bounce+order-12345@...     │
  ├─ X-Haraka-MsgID: order-12345             │
  ├─ Feedback-ID: ...                        │
  │                                          ▼
  └─────────────────────────────────────► Delivered
                                             │
                                             ▼
                                         Bounce/FBL
                                             │
                                             ▼
[rcpt_to.inbound] ◄────────────────── To: bounce+order-12345@...
  │
  └─► message_id = "order-12345"
      │
      ▼
[inbound-handler]
  │
  └─► Webhook: { message_id: "order-12345", ... }
```
