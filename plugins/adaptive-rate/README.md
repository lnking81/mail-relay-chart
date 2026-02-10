# Haraka Adaptive Rate Limiting Plugin

Automatically adjusts outbound mail delivery rate based on remote server responses with intelligent circuit breaker protection.

## How It Works

**Problem**: Major email providers (Gmail, Yahoo, Microsoft) rate-limit senders who deliver too fast. Static rate limits are either too slow (waste time) or too fast (get blocked).

**Solution**: This plugin dynamically adjusts delivery delays based on actual server responses:

- **On rate limit errors (421, 4.7.28)**: Increases delay (exponential backoff)
- **On other 4xx errors**: Tracked but doesn't affect delay (recipient-specific issues)
- **On successful delivery**: Gradually decreases delay (recovery)
- **Per-MX-provider tracking**: Rate limiting is per MX provider, not recipient domain
- **Uses `send_email` hook with `DELAY`**: Actually throttles outbound delivery
- **Circuit Breaker**: After consecutive rate-limit failures, completely pause sends to let provider "cool down"

## Algorithm

```
Initial state: delay = initial_delay (e.g., 5 seconds)

On DEFERRED:
    If error is rate limit (421, 4.7.28, "rate limit", "too many", "throttl"):
        consecutiveRateLimitFailures++
        delay = min(delay × backoff_multiplier, max_delay)

        # Circuit Breaker
        If consecutiveRateLimitFailures >= circuit_breaker_threshold:
            circuitOpenUntil = now + circuit_breaker_duration
            # All sends paused until circuit closes
    Else (other 4xx errors like 450, 451, 452):
        # Track for monitoring, but don't increase delay
        # These are typically recipient-specific, not provider-wide

On DELIVERED:
    consecutiveRateLimitFailures = 0
    consecutiveSuccesses++

    # Close circuit on success (if it was open)
    If circuitOpenUntil > 0:
        circuitOpenUntil = 0
        delay = initial_delay  # Reset to fast start

    If consecutiveSuccesses >= success_threshold:
        delay = max(delay × recovery_rate, min_delay)
        consecutiveSuccesses = 0

On SEND_EMAIL (before delivery):
    # Check Circuit Breaker first
    If circuitOpenUntil > now:
        Apply DELAY until circuit closes (complete pause)
    Else If consecutiveRateLimitFailures > 0 AND delay > min_delay:
        Apply DELAY of current delay value
```

## Circuit Breaker

### Why It's Needed

When you reach `maxDelay` (e.g., 5 minutes) but the provider keeps rate-limiting, the standard exponential backoff keeps "knocking" every 5 minutes. Some providers interpret these periodic connection attempts as continued abuse and never lift the rate limit.

### How It Works

| Phase               | Condition                                        | Behavior                                       |
| ------------------- | ------------------------------------------------ | ---------------------------------------------- |
| **Closed** (normal) | `consecutiveRateLimitFailures < threshold`       | Normal exponential backoff                     |
| **Open** (tripped)  | `consecutiveRateLimitFailures >= threshold`      | ALL sends paused for `circuitBreakerDuration`  |
| **Recovery**        | After pause expires OR first successful delivery | Circuit closes, delay resets to `initialDelay` |

### Configuration

```ini
[main]
circuit_breaker_threshold = 5     ; Rate-limit failures before circuit opens
circuit_breaker_duration = 600000 ; 10 minutes complete pause
```

### Example Scenario

```
gmail.com: delay = 300000ms (maxDelay reached)
→ Message deferred: 421 Rate limited (failure #3)
→ Message deferred: 421 Rate limited (failure #4)
→ Message deferred: 421 Rate limited (failure #5 = threshold)
→ CIRCUIT BREAKER OPENS: No sends to gmail.com for 10 minutes
→ 10 minutes later: circuit closes, delay = 20000ms (initialDelay)
→ Try again: Success!
→ Continue with normal adaptive rate limiting
```

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

[domains]
; Enable adaptive rate for these domains
gmail.com = true
googlemail.com = true
outlook.com = true
hotmail.com = true
yahoo.com = true

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

  domains:
    gmail.com:
      enabled: true
      minDelay: 15000
      maxDelay: 300000
      initialDelay: 20000
      backoffMultiplier: 2.0
      successThreshold: 10
      circuitBreakerThreshold: 5
      circuitBreakerDuration: 900000 # 15 minutes for Gmail

    outlook.com:
      enabled: true
      minDelay: 10000
      circuitBreakerDuration: 900000 # 15 minutes for Microsoft
```

## Behavior Examples

### New IP Warming

```
Start: delay=20s for gmail.com
→ Deliver message 1: Success
→ Deliver message 2: Success
→ Deliver message 3: Success
→ Deliver message 4: Success
→ Deliver message 5: Success (threshold reached)
→ delay reduced to 18s
→ Continue delivering...
→ After many successes: delay=15s (minimum)

Gmail is happy, you've warmed up!
```

### Rate Limit Response

```
Current: delay=15s for gmail.com
→ Deliver message: 421 4.7.28 Rate limited
→ delay increased to 22.5s (×1.5)
→ consecutiveRateLimitFailures = 1
→ Next attempt succeeds
→ consecutiveRateLimitFailures = 0
→ After 10 successes: delay starts recovering
```

### Circuit Breaker Activation

```
gmail.com: delay = 300000ms (max reached), failures = 4
→ Deliver message: 421 4.7.28 Rate limited
→ consecutiveRateLimitFailures = 5 (threshold reached!)
→ CIRCUIT BREAKER OPENS
→ All sends to gmail.com paused for 10 minutes
→ No connection attempts during this time
→ After 10 minutes: circuit closes
→ delay reset to 20000ms (initialDelay)
→ Resume normal delivery
```

### Non-Rate-Limit 4xx Error

```
Current: delay=15s for some-domain.com
→ Deliver message: 450 Mailbox temporarily unavailable
→ delay unchanged (still 15s)
→ consecutiveRateLimitFailures = 0 (no rate limiting)
→ Only tracked for monitoring, no slowdown
→ Problem is with recipient, not provider-wide
```

## Monitoring

The plugin tracks per-domain statistics:

- Current delay (ms)
- Consecutive successes
- Consecutive failures (all deferred errors)
- Consecutive rate limit failures (only rate limit errors - controls delay)
- Last update timestamp

### Prometheus Metrics

When `prom-client` is available (installed by `haraka-plugin-prometheus`), the plugin exports the following metrics:

| Metric                                                         | Type    | Description                                 |
| -------------------------------------------------------------- | ------- | ------------------------------------------- |
| `haraka_adaptive_rate_delay_ms{domain}`                        | Gauge   | Current delay in milliseconds               |
| `haraka_adaptive_rate_consecutive_failures{domain}`            | Gauge   | All deferred errors (for monitoring)        |
| `haraka_adaptive_rate_consecutive_rate_limit_failures{domain}` | Gauge   | Rate limit errors only (controls delay)     |
| `haraka_adaptive_rate_circuit_breaker_open{domain}`            | Gauge   | Circuit breaker state (1=open, 0=closed)    |
| `haraka_adaptive_rate_deliveries_total{domain}`                | Counter | Total delivered messages                    |
| `haraka_adaptive_rate_deferrals_total{domain}`                 | Counter | Total deferred messages                     |
| `haraka_adaptive_rate_bounces_total{domain}`                   | Counter | Total bounced messages                      |
| `haraka_adaptive_rate_delays_applied_total{domain}`            | Counter | Times delivery was delayed                  |
| `haraka_adaptive_rate_rate_limited_total{domain}`              | Counter | Explicit rate limit responses (421, 4.7.28) |
| `haraka_adaptive_rate_circuit_breaker_trips_total{domain}`     | Counter | Total circuit breaker activations           |

Metrics are automatically enabled when both `metrics.enabled` and `adaptiveRate.enabled` are true in the Helm chart.

### Grafana Dashboard

Example queries:

```promql
# Current delay by domain
haraka_adaptive_rate_delay_ms{namespace=~"$namespace", release=~"$release"}

# Rate limited responses per hour
sum(increase(haraka_adaptive_rate_rate_limited_total[1h])) by (domain)

# Domains currently being rate limited (delay will be applied)
haraka_adaptive_rate_consecutive_rate_limit_failures > 0

# All domains with delivery issues (monitoring)
haraka_adaptive_rate_consecutive_failures > 0

# Domains with open circuit breaker (all sends paused)
haraka_adaptive_rate_circuit_breaker_open == 1

# Circuit breaker trips in the last hour
sum(increase(haraka_adaptive_rate_circuit_breaker_trips_total[1h])) by (domain)
```

## Comparison with Static Rate Limiting

| Aspect              | Static (`limit` plugin) | Adaptive (this plugin) |
| ------------------- | ----------------------- | ---------------------- |
| Configuration       | Fixed rate per domain   | Auto-adjusts           |
| New IP warmup       | Manual schedule needed  | Automatic              |
| Rate limit response | Ignores feedback        | Slows down             |
| Recovery            | Manual adjustment       | Automatic speedup      |
| Efficiency          | Can be too slow         | Optimizes over time    |

**Recommendation**: Use both together:

- Static `limit` plugin for hard caps (safety net)
- Adaptive plugin for dynamic optimization within those caps
