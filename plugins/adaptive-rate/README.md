# Haraka Adaptive Rate Limiting Plugin

Automatically adjusts outbound mail delivery rate based on remote server responses with intelligent circuit breaker protection.

## How It Works

**Problem**: Major email providers (Gmail, Yahoo, Microsoft) rate-limit senders who deliver too fast. Static rate limits are either too slow (waste time) or too fast (get blocked).

**Solution**: This plugin dynamically adjusts delivery delays based on actual server responses:

- **On rate limit errors (421, 4.7.28)**: Increases delay (exponential backoff) + sets immediate pause
- **On other 4xx errors (450, 452, etc.)**: Tracked for monitoring, but does **not** affect delay or block recovery
- **On successful delivery**: Gradually decreases delay after threshold consecutive successes (gradual recovery)
- **Per-MX-provider tracking**: Rate limiting is per MX provider, not recipient domain
- **Throttling via DELAY**: Messages are deferred to Haraka's `temp_fail_queue` with precise delay (no slot hogging)
- **Circuit Breaker**: After consecutive rate-limit failures, completely pause sends; extends on continued failures

## Throttling Mechanism

All rate-limit enforcement uses **DELAY** (Haraka constant 908) via `next(DELAY, delay_seconds)` in the `send_email` hook. Haraka routes DELAY responses to an in-memory `temp_fail_queue`, calls `next_cb()` to free the delivery slot, and re-enqueues the message after the specified delay. Critically, DELAY does **not** call `temp_fail()` and does **not** trigger the `deferred` hook — eliminating self-feedback loops.

On each send attempt, the `send_email` hook re-evaluates the current state:

- If circuit breaker is open → `next(DELAY, seconds)` (defer)
- If `noSendUntil` pause is active → `next(DELAY, seconds)` (defer)
- Slot-based pacing: each message claims a slot in `nextSendTime` queue → serialized delivery
- Otherwise → allow delivery (`next()`)

This approach avoids the **thundering herd problem**: concurrent connections all compute their slot from the same `nextSendTime`, advancing it atomically. N connections = 1 message per delay interval, regardless of concurrency. Messages beyond `CLAIM_HORIZON` (min(delay×10, 5s)) are DELAYed without claiming, then re-evaluate — enabling instant recovery when delay decreases.

## Algorithm

```
Initial state: delay = initial_delay (e.g., 5 seconds)

On DEFERRED:
  If rate-limit error (421, 4.7.28, "rate limit", "too many", "throttl"):
    consecutiveSuccesses = 0          # Reset recovery progress
    consecutiveRateLimitFailures++
    delay = min(delay × backoff_multiplier, max_delay)
    noSendUntil = now + delay         # Immediate hard pause

    If consecutiveRateLimitFailures >= circuit_breaker_threshold:
      If circuit already open:
        circuitOpenUntil += circuit_breaker_duration  # EXTEND
      Else:
        circuitOpenUntil = now + circuit_breaker_duration  # OPEN

  Else (450 mailbox unavailable, 452 mailbox full, 451 local error, etc.):
    consecutiveFailures++             # For monitoring only
    # Does NOT reset consecutiveSuccesses
    # Does NOT increase delay
    # Does NOT affect rate-limit state

On DELIVERED:
  consecutiveSuccesses++
  consecutiveFailures = 0

  If circuit breaker is OPEN:
    # Do NOT close early — prevents race condition with in-flight messages
    # Count success, but circuit expires naturally

  If consecutiveSuccesses >= success_threshold AND circuit NOT open:
    delay = max(delay × recovery_rate, min_delay)
    consecutiveRateLimitFailures -= success_threshold  # Gradual decrease
    consecutiveSuccesses = 0
    Clear noSendUntil pause

On SEND_EMAIL (before delivery attempt):
  1. Circuit breaker check:
     If circuitOpenUntil > now → DELAY N seconds (defer)

  2. Circuit expired cleanup:
     If circuitOpenUntil expired → clear flags, keep delay (gradual recovery)

  3. Immediate pause check:
     If noSendUntil > now → DELAY N seconds (defer)

  4. Slot-based pacing (atomic slot claim):
     delay = rateLimitStreak > 0 ? state.delay : minDelay
     Collapse stale slots: if nextSendTime < now → reset to now
     Recovery collapse: if delay < paceDelay → nextSendTime = now + delay
     mySlot = nextSendTime; waitMs = mySlot - now
     claimHorizon = min(delay × 10, 5000ms)
     If waitMs ≤ 0 → claim slot (nextSendTime = now + delay), send
     If waitMs ≤ claimHorizon → claim slot (nextSendTime = mySlot + delay), DELAY waitMs
     If waitMs > claimHorizon → do NOT claim, DELAY min(waitMs, delay, 5s) → re-enter

  5. Allow delivery → next()
```

## Key Design Decisions

### Non-rate-limit errors don't block recovery

A "452 Mailbox full" is a **recipient-specific** problem (one user's mailbox), not a provider-wide rate limit. If Google is rate-limiting you but also delivering to other recipients, those successful deliveries should count toward recovery. Non-rate-limit deferrals:

- Do **not** reset `consecutiveSuccesses`
- Do **not** increase delay
- Do **not** affect `consecutiveRateLimitFailures`
- Are counted in `consecutiveFailures` and `totalDeferred` for monitoring

### Circuit breaker doesn't close on successful delivery

When a circuit opens, there may be messages already in-flight (sent before the rate limit was detected). These messages can succeed, but that doesn't mean the rate limit is lifted. To prevent a race condition:

- Successes during open circuit are **counted** but don't close the circuit
- Circuit expires naturally after `circuitBreakerDuration`
- Continued rate-limit errors **extend** the circuit duration

### Gradual recovery (no instant reset)

After circuit expires or rate-limit streak is active, recovery is gradual:

- Delay is preserved (not reset to `initialDelay`)
- `consecutiveRateLimitFailures` decreases by `successThreshold` per recovery cycle (not reset to 0)
- This prevents a sudden burst of fast deliveries that could trigger rate limiting again

## Circuit Breaker

### Why It's Needed

When you reach `maxDelay` (e.g., 5 minutes) but the provider keeps rate-limiting, the standard exponential backoff keeps "knocking" every 5 minutes. Some providers interpret these periodic connection attempts as continued abuse and never lift the rate limit.

### How It Works

| Phase               | Condition                                   | Behavior                                             |
| ------------------- | ------------------------------------------- | ---------------------------------------------------- |
| **Closed** (normal) | `consecutiveRateLimitFailures < threshold`  | Normal exponential backoff + immediate pause         |
| **Open** (tripped)  | `consecutiveRateLimitFailures >= threshold` | ALL sends DELAY for `circuitBreakerDuration`         |
| **Extended**        | New rate-limits while circuit is open       | Duration extended (in-flight messages still failing) |
| **Recovery**        | Circuit duration expires                    | Circuit closes, delay preserved, gradual recovery    |

### Configuration

```ini
[main]
circuit_breaker_threshold = 5     ; Rate-limit failures before circuit opens
circuit_breaker_duration = 600000 ; 10 minutes complete pause
```

### Example Scenario

```
google.com: delay = 300000ms (maxDelay reached)
→ Message deferred: 421 Rate limited (streak #3)
→ Message deferred: 421 Rate limited (streak #4)
→ Message deferred: 421 Rate limited (streak #5 = threshold)
→ CIRCUIT BREAKER OPENS: All sends to google.com → DELAY for 2 min
→ In-flight message fails: 421 Rate limited (streak #6)
→ Circuit EXTENDED by another 2 min
→ Eventually: circuit expires, delay still 300000ms
→ Gradual recovery: after 5 consecutive successes, delay decreases
→ Continue until delay reaches minDelay
```

## MX Provider Normalization

Rate limiting is done per **MX provider**, not per recipient domain. Multiple domains sharing the same MX get a single rate-limit bucket:

```
gmail.com      → MX: smtp.google.com → rate limit key: google.com
strukov.net    → MX: smtp.google.com → rate limit key: google.com  (same!)
hotmail.com    → MX: smtp.outlook.com → rate limit key: outlook.com
live.com       → MX: smtp.outlook.com → rate limit key: outlook.com (same!)
```

Known providers are pre-mapped (`gmail.com` → `google.com`, etc.) for instant lookup before MX resolution. Unknown domains use cached MX results from previous deliveries.

## Configuration

Create `config/adaptive-rate.ini`:

```ini
[main]
enabled = true

; Delay bounds in milliseconds
min_delay = 1000          ; 1 second minimum
max_delay = 300000        ; 5 minutes maximum
initial_delay = 5000      ; Start with 5 seconds

; Backoff settings
backoff_multiplier = 1.5  ; Increase delay by 50% on failure
recovery_rate = 0.9       ; Decrease delay by 10% on success
success_threshold = 5     ; Need 5 consecutive successes to speed up

; Circuit breaker settings
circuit_breaker_threshold = 5     ; Rate-limit failures before circuit opens
circuit_breaker_duration = 600000 ; 10 minutes complete pause

; State persistence (optional)
state_file = /data/adaptive-rate-state.json
state_save_interval = 300000      ; Save every 5 minutes
state_max_age = 3600000           ; Ignore state older than 1 hour

; Metrics HTTP server port
metrics_port = 8081

[domains]
; Enable for all domains
* = true
; Or list specific domains:
; gmail.com = true
; outlook.com = true

; Per-domain overrides (optional)
[gmail.com]
min_delay = 15000         ; Gmail needs slower minimum (15 sec)
max_delay = 300000        ; Up to 5 minutes during heavy limiting
initial_delay = 20000     ; Start slow with Gmail
backoff_multiplier = 2.0  ; More aggressive backoff
success_threshold = 10    ; Need more successes before speeding up
circuit_breaker_threshold = 5
circuit_breaker_duration = 900000  ; 15 minutes for strict providers
```

## Usage with Helm Chart

```yaml
adaptiveRate:
  enabled: true

  defaults:
    minDelay: 1000
    maxDelay: 300000
    initialDelay: 5000
    backoffMultiplier: 1.5
    recoveryRate: 0.9
    successThreshold: 5
    circuitBreakerThreshold: 5
    circuitBreakerDuration: 600000 # 10 minutes

  customDomains:
    "*":
      enabled: true

    gmail.com:
      enabled: true
      minDelay: 15000
      initialDelay: 20000
      backoffMultiplier: 2.0
      successThreshold: 10
      circuitBreakerDuration: 900000 # 15 minutes
```

## Behavior Examples

### Rate Limit → Backoff → Recovery

```
google.com: delay = 20000ms (initialDelay for gmail config)

→ Deliver message: 421 4.7.28 Rate limited
  delay: 20000 → 40000ms (×2.0 backoff)
  noSendUntil: now + 40000ms
  streak: 1

→ Next 40 seconds: all sends to google.com → DELAY (deferred)

→ After pause expires, deliver message: Success!
  consecutiveSuccesses: 1/10

→ 452 Mailbox full for user@gmail.com
  consecutiveSuccesses: still 1 (NOT reset!)
  delay: still 40000ms (NOT increased!)

→ 9 more successful deliveries...
  consecutiveSuccesses: 10 (threshold!)
  delay: 40000 → 36000ms (×0.9 recovery)
  streak: 1 → 0 (decreased by threshold)

→ Continue delivering at recovered rate
```

### Circuit Breaker Trip and Recovery

```
google.com: delay = 300000ms (maxDelay), streak = 4
→ Message deferred: 421 Rate limited (streak #5 = threshold!)
→ CIRCUIT BREAKER OPENS (all sends → DELAY)
→ In-flight message succeeds → counted but circuit NOT closed
→ In-flight message fails: 421 → circuit EXTENDED
→ Circuit eventually expires
→ delay still 300000ms (NOT reset to initialDelay!)
→ Gradual recovery through successful deliveries
```

### Non-Rate-Limit Error (no impact)

```
google.com: delay = 15000ms, successStreak = 3/5

→ 452 4.2.2 Mailbox full (recipient-specific)
  delay: 15000ms (unchanged)
  successStreak: 3 (NOT reset!)
  rateLimitStreak: 0 (unchanged)

→ Next delivery: Success!
  successStreak: 4/5

→ Next delivery: Success!
  successStreak: 5 → threshold! Recovery triggered
```

## Monitoring

### Prometheus Metrics

When `prom-client` is available, the plugin exports metrics on a dedicated HTTP server (default port 8081):

| Metric                                                                      | Type    | Description                                      |
| --------------------------------------------------------------------------- | ------- | ------------------------------------------------ |
| `haraka_adaptive_rate_delay_ms{domain}`                                     | Gauge   | Current delay in milliseconds                    |
| `haraka_adaptive_rate_consecutive_failures{domain}`                         | Gauge   | All deferred errors (monitoring only)            |
| `haraka_adaptive_rate_consecutive_rate_limit_failures{domain}`              | Gauge   | Rate limit errors only (controls delay)          |
| `haraka_adaptive_rate_circuit_breaker_open{domain}`                         | Gauge   | Circuit breaker state (1=open, 0=closed)         |
| `haraka_adaptive_rate_circuit_breaker_open_until_timestamp_seconds{domain}` | Gauge   | Unix timestamp when circuit closes (0 if closed) |
| `haraka_adaptive_rate_deliveries_total{domain}`                             | Counter | Total delivered messages                         |
| `haraka_adaptive_rate_deferrals_total{domain}`                              | Counter | Total deferred messages (all types)              |
| `haraka_adaptive_rate_bounces_total{domain}`                                | Counter | Total bounced messages (permanent failures)      |
| `haraka_adaptive_rate_delays_applied_total{domain}`                         | Counter | Times DELAY was returned (throttle applied)      |
| `haraka_adaptive_rate_baseline_throttled_total{domain}`                     | Counter | Preemptive minDelay throttles before rate limits |
| `haraka_adaptive_rate_rate_limited_total{domain}`                           | Counter | Explicit rate limit responses (421, 4.7.28)      |
| `haraka_adaptive_rate_circuit_breaker_trips_total{domain}`                  | Counter | Total circuit breaker activations                |

> Note: The `domain` label contains the MX provider name (e.g., `google.com`), not the recipient domain.

### Grafana Queries

```promql
# Current delay by provider
haraka_adaptive_rate_delay_ms

# Providers with active rate limiting
haraka_adaptive_rate_consecutive_rate_limit_failures > 0

# Providers with open circuit breaker
haraka_adaptive_rate_circuit_breaker_open == 1

# Rate limit responses per hour
sum(increase(haraka_adaptive_rate_rate_limited_total[1h])) by (domain)

# Baseline throttles per hour (preemptive pacing)
sum(increase(haraka_adaptive_rate_baseline_throttled_total[1h])) by (domain)

# Circuit breaker trips in the last hour
sum(increase(haraka_adaptive_rate_circuit_breaker_trips_total[1h])) by (domain)

# Delivery success rate by provider
sum(rate(haraka_adaptive_rate_deliveries_total[5m])) by (domain)
/ (sum(rate(haraka_adaptive_rate_deliveries_total[5m])) by (domain)
 + sum(rate(haraka_adaptive_rate_deferrals_total[5m])) by (domain))
```

### Admin API

The plugin exports functions for monitoring and manual intervention:

| Function                        | Description                                      |
| ------------------------------- | ------------------------------------------------ |
| `get_stats()`                   | All domain statistics                            |
| `get_domain_stats(domain)`      | Stats for specific MX provider                   |
| `get_problem_domains(minFails)` | Domains with high failure rates                  |
| `get_open_circuits()`           | Currently open circuit breakers                  |
| `close_circuit(domain)`         | Force close circuit + reset delay (admin rescue) |
| `reset_domain(domain)`          | Clear all state for a domain                     |
| `reset_all()`                   | Clear all state                                  |

## State Persistence

The plugin can persist rate-limit state to disk, surviving pod restarts:

```ini
[main]
state_file = /data/adaptive-rate-state.json
state_save_interval = 300000   ; Save every 5 minutes
state_max_age = 3600000        ; Ignore saved state older than 1 hour
```

State is saved immediately on circuit breaker trips and periodically via timer. On startup, saved state is restored if not older than `state_max_age`.

## Comparison with Static Rate Limiting

| Aspect              | Static (`limit` plugin) | Adaptive (this plugin)           |
| ------------------- | ----------------------- | -------------------------------- |
| Configuration       | Fixed rate per domain   | Auto-adjusts based on responses  |
| New IP warmup       | Manual schedule needed  | Automatic                        |
| Rate limit response | Ignores feedback        | Slows down (exponential backoff) |
| Recovery            | Manual adjustment       | Automatic gradual speedup        |
| Circuit breaker     | None                    | Automatic pause + extend         |
| Non-rate-limit 4xx  | Counts as failure       | Ignored for rate adjustments     |

**Recommendation**: Use both together — static `limit` as a safety cap, adaptive plugin for dynamic optimization.
