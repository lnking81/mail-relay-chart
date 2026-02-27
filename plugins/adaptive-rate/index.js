'use strict';

const fs = require('fs');
const path = require('path');

// Haraka hook return codes
// DELAY defers the message in-memory without disk rewrite:
//   send_email_respond calls next_cb() (frees the delivery slot),
//   then temp_fail_queue.add(filename, delay_seconds * 1000, cb)
//   which pushes hmail back to delivery_queue after the timer fires.
// Crucially, DELAY does NOT call temp_fail() and therefore does NOT
// trigger the 'deferred' hook — no self-feedback loop.
// Source: haraka-constants/index.js → exports.delay = 908
// Source: Haraka/outbound/hmail.js → send_email_respond()
let DELAY;
try {
    DELAY = require('haraka-constants').delay;
} catch (e) {
    DELAY = 908; // Known numeric value
}

/**
 * Haraka Adaptive Rate Limiting Plugin
 *
 * Automatically adjusts outbound delivery rate based on remote server responses.
 * When receiving 421 (rate limited) or 4xx errors, the plugin slows down.
 * When deliveries succeed, it gradually speeds up.
 *
 * Uses send_email hook with next(DELAY, seconds) to throttle delivery.
 * DELAY is a native Haraka constant (908) that defers the message in-memory
 * without triggering the deferred hook (no self-feedback loop).
 *
 * MX PROVIDER NORMALIZATION:
 * Rate limiting is done per MX provider, not per recipient domain.
 * This ensures proper throttling when multiple domains share the same MX:
 *   - strukov.net → smtp.google.com → google.com (rate limit key)
 *   - omagic.ai → smtp.google.com → google.com (same rate limit key)
 *
 * Known providers are normalized:
 *   - *.google.com, *.googlemail.com → google.com
 *   - *.outlook.com, *.hotmail.com, *.live.com → outlook.com
 *   - *.yahoo.com, *.yahoodns.net → yahoo.com
 *   - etc. (see normalizeMxProvider function)
 *
 * CONCURRENCY HANDLING:
 * - Tracks lastSendTime per MX provider to enforce minimum interval
 * - Multiple concurrent sends to same provider will respect the delay
 * - First send after a failure triggers the full delay
 * - Subsequent sends within delay window get remaining delay
 *
 * LIMITATIONS:
 * - In-memory state (lost on restart, not shared across instances)
 * - For multi-instance deployments, use Redis-backed rate limiting
 * - Delay is approximation due to async nature of Node.js
 *
 * PROMETHEUS METRICS (all per MX provider):
 * - haraka_adaptive_rate_delay_ms{domain} - current delay in milliseconds
 * - haraka_adaptive_rate_consecutive_failures{domain} - all deferred errors count
 * - haraka_adaptive_rate_consecutive_rate_limit_failures{domain} - rate limit errors only (controls delay)
 * - haraka_adaptive_rate_circuit_breaker_open{domain} - 1 if circuit is open, 0 otherwise
 * - haraka_adaptive_rate_circuit_breaker_open_until_timestamp_seconds{domain} - Unix timestamp when circuit closes (0 if closed)
 * - haraka_adaptive_rate_deliveries_total{domain} - total delivered messages
 * - haraka_adaptive_rate_deferrals_total{domain} - total deferred messages
 * - haraka_adaptive_rate_bounces_total{domain} - total bounced messages
 * - haraka_adaptive_rate_delays_applied_total{domain} - times delay was applied
 * - haraka_adaptive_rate_baseline_throttled_total{domain} - times baseline minDelay throttling was applied
 * - haraka_adaptive_rate_rate_limited_total{domain} - explicit rate limit responses (421)
 * - haraka_adaptive_rate_circuit_breaker_trips_total{domain} - times circuit breaker tripped
 *
 * Note: The label is called "domain" but actually contains MX provider name.
 *
 * Algorithm:
 * - On deferred with rate limit (421, 4.7.28, "rate limit", "too many", "throttl"):
 *   delay increases (exponential backoff)
 * - On deferred without rate limit (450, 451, 452, etc.): no delay increase
 *   (these are typically recipient-specific, not provider-wide)
 * - On delivered: delay decreases after threshold successes (recovery)
 * - On send_email: defer delivery using next(DELAY, seconds)
 *
 * THROTTLING MECHANISM:
 * All blocking uses DELAY (Haraka constant 908) in the send_email hook.
 * next(DELAY, N) calls next_cb() to free the delivery slot, then schedules
 * the message to be re-pushed to delivery_queue after N seconds.
 * On each re-delivery attempt, our send_email hook re-evaluates the state:
 * - If still within a pause window → DELAY again
 * - If pause expired → next() to proceed with delivery
 * DELAY does NOT trigger the 'deferred' hook (unlike DENYSOFT), so there is
 * no self-feedback loop. It also does NOT rewrite the queue file to disk.
 *
 * CIRCUIT BREAKER:
 * When consecutive rate-limit failures reach threshold, the "circuit opens":
 * - All sends to this provider are paused for circuitBreakerDuration
 * - No connection attempts are made during this period
 * - This gives the provider time to "cool down"
 * - After duration expires, circuit closes and normal operation resumes
 * - First successful delivery also closes the circuit immediately
 *
 * Configuration (adaptive-rate.ini):
 * [main]
 * enabled = true
 * min_delay = 1000          ; Minimum delay in ms (1 second)
 * max_delay = 300000        ; Maximum delay in ms (5 minutes)
 * initial_delay = 5000      ; Starting delay in ms (5 seconds)
 * backoff_multiplier = 1.5  ; Multiply delay on failure
 * recovery_rate = 0.9       ; Multiply delay on success (< 1 to speed up)
 * success_threshold = 5     ; Consecutive successes before reducing delay
 * circuit_breaker_threshold = 5   ; Rate-limit failures before circuit opens
 * circuit_breaker_duration = 600000 ; Circuit open duration in ms (10 minutes)
 *
 * [domains]
 * gmail.com = true          ; Enable adaptive rate for gmail.com
 * outlook.com = true
 *
 * [gmail.com]
 * min_delay = 15000         ; Override settings for Gmail
 */

// In-memory state for domain tracking
const domainState = new Map();

let config = {};
let plugin_instance = null;

// State persistence
let stateSaveInterval = null;
let lastStateSave = 0;

// Prometheus metrics (initialized lazily when prom-client is available)
let metricsInitialized = false;
let metricsServer = null;
let metricsServerStarted = false;  // Track if we've attempted to start
let metricsRegistry = null;
let metrics = {
    delayGauge: null,
    failuresGauge: null,
    rateLimitFailuresGauge: null,  // Rate limit specific failures
    circuitBreakerGauge: null,     // Circuit breaker state (0=closed, 1=open)
    circuitOpenUntilGauge: null,   // Circuit breaker close time (Unix timestamp, seconds)
    deliveriesCounter: null,
    deferralsCounter: null,
    bouncesCounter: null,
    delaysAppliedCounter: null,
    baselineThrottledCounter: null,
    rateLimitedCounter: null,
    circuitBreakerTripsCounter: null  // Circuit breaker trip count
};

/**
 * Try to load prom-client from various locations
 * @returns {object|null} prom-client module or null if not found
 */
function loadPromClient(plugin) {
    // Method 1: Direct require (if installed globally or locally)
    try {
        const client = require('prom-client');
        plugin.logdebug('prom-client loaded via direct require');
        return client;
    } catch (e) {
        plugin.logdebug(`Direct require failed: ${e.message}`);
    }

    // Method 2: Use require.main.require (resolves from Haraka's main module)
    try {
        if (require.main && require.main.require) {
            const client = require.main.require('prom-client');
            plugin.logdebug('prom-client loaded via require.main.require');
            return client;
        }
    } catch (e) {
        plugin.logdebug(`require.main.require failed: ${e.message}`);
    }

    // Method 3: Try from haraka-plugin-prometheus's dependencies
    try {
        const client = require('@mailprotector/haraka-plugin-prometheus/node_modules/prom-client');
        plugin.logdebug('prom-client loaded from haraka-plugin-prometheus');
        return client;
    } catch (e) {
        plugin.logdebug(`haraka-plugin-prometheus path failed: ${e.message}`);
    }

    // Method 4: Search in process.mainModule (older Node.js)
    try {
        if (process.mainModule && process.mainModule.require) {
            const client = process.mainModule.require('prom-client');
            plugin.logdebug('prom-client loaded via process.mainModule.require');
            return client;
        }
    } catch (e) {
        plugin.logdebug(`process.mainModule.require failed: ${e.message}`);
    }

    // Method 5: Search in parent modules
    try {
        let parent = module.parent;
        while (parent) {
            try {
                const client = parent.require('prom-client');
                plugin.logdebug('prom-client loaded via parent module');
                return client;
            } catch (e) {
                // Try next parent
            }
            parent = parent.parent;
        }
    } catch (e) {
        plugin.logdebug(`Parent module search failed: ${e.message}`);
    }

    return null;
}

/**
 * Initialize Prometheus metrics with dedicated HTTP server
 */
function initMetrics(plugin) {
    if (metricsInitialized) return;

    try {
        // Try to load prom-client from various locations
        const client = loadPromClient(plugin);
        if (!client) {
            plugin.loginfo('Prometheus metrics disabled: prom-client not found in any location');
            return;
        }

        // Create dedicated registry for this plugin
        metricsRegistry = new client.Registry();
        metricsRegistry.setDefaultLabels({ plugin: 'adaptive_rate' });

        const prefix = 'haraka_adaptive_rate_';

        // Current delay per domain (Gauge)
        metrics.delayGauge = new client.Gauge({
            name: prefix + 'delay_ms',
            help: 'Current adaptive rate delay in milliseconds',
            labelNames: ['domain'],
            registers: [metricsRegistry]
        });

        // Consecutive failures per domain (Gauge) - all deferred errors
        metrics.failuresGauge = new client.Gauge({
            name: prefix + 'consecutive_failures',
            help: 'Current consecutive failure count (all deferred errors)',
            labelNames: ['domain'],
            registers: [metricsRegistry]
        });

        // Consecutive rate limit failures per domain (Gauge) - only rate limit errors
        metrics.rateLimitFailuresGauge = new client.Gauge({
            name: prefix + 'consecutive_rate_limit_failures',
            help: 'Current consecutive rate limit failure count (controls delay)',
            labelNames: ['domain'],
            registers: [metricsRegistry]
        });

        // Circuit breaker state (Gauge) - 1 if open, 0 if closed
        metrics.circuitBreakerGauge = new client.Gauge({
            name: prefix + 'circuit_breaker_open',
            help: 'Circuit breaker state: 1=open (paused), 0=closed (normal)',
            labelNames: ['domain'],
            registers: [metricsRegistry]
        });

        // Circuit breaker close time (Gauge) - Unix timestamp in seconds, 0 if closed
        metrics.circuitOpenUntilGauge = new client.Gauge({
            name: prefix + 'circuit_breaker_open_until_timestamp_seconds',
            help: 'Unix timestamp when circuit breaker closes, 0 if circuit is closed',
            labelNames: ['domain'],
            registers: [metricsRegistry]
        });

        // Deliveries counter (Counter)
        metrics.deliveriesCounter = new client.Counter({
            name: prefix + 'deliveries_total',
            help: 'Total delivered messages to rate-limited domains',
            labelNames: ['domain'],
            registers: [metricsRegistry]
        });

        // Deferrals counter (Counter)
        metrics.deferralsCounter = new client.Counter({
            name: prefix + 'deferrals_total',
            help: 'Total deferred messages to rate-limited domains',
            labelNames: ['domain'],
            registers: [metricsRegistry]
        });

        // Bounces counter (Counter)
        metrics.bouncesCounter = new client.Counter({
            name: prefix + 'bounces_total',
            help: 'Total bounced messages to rate-limited domains',
            labelNames: ['domain'],
            registers: [metricsRegistry]
        });

        // Delays applied counter (Counter)
        metrics.delaysAppliedCounter = new client.Counter({
            name: prefix + 'delays_applied_total',
            help: 'Total times delay was applied before sending',
            labelNames: ['domain'],
            registers: [metricsRegistry]
        });

        // Baseline minDelay throttling counter (Counter)
        metrics.baselineThrottledCounter = new client.Counter({
            name: prefix + 'baseline_throttled_total',
            help: 'Total times baseline minDelay throttling was applied before first rate-limit',
            labelNames: ['domain'],
            registers: [metricsRegistry]
        });

        // Rate limited responses counter (Counter)
        metrics.rateLimitedCounter = new client.Counter({
            name: prefix + 'rate_limited_total',
            help: 'Total explicit rate limit responses (421, 4.7.28)',
            labelNames: ['domain'],
            registers: [metricsRegistry]
        });

        // Circuit breaker trips counter (Counter)
        metrics.circuitBreakerTripsCounter = new client.Counter({
            name: prefix + 'circuit_breaker_trips_total',
            help: 'Total times circuit breaker was tripped',
            labelNames: ['domain'],
            registers: [metricsRegistry]
        });

        // Note: HTTP server is started lazily on first outbound hook call
        // This ensures it runs in the outbound process where metrics are collected

        metricsInitialized = true;
        plugin.loginfo('Prometheus metrics initialized (server will start on first outbound event)');
    } catch (err) {
        // Error during metric initialization
        plugin.logwarn(`Prometheus metrics initialization failed: ${err.message}`);
    }
}

/**
 * Ensure metrics server is running (called on first outbound hook)
 */
function ensureMetricsServer(plugin) {
    if (metricsServerStarted || !metricsInitialized) return;
    metricsServerStarted = true;

    const metricsPort = config.metricsPort || 8081;
    startMetricsServer(plugin, metricsPort);
}

/**
 * Start HTTP server for metrics endpoint
 */
function startMetricsServer(plugin, port) {
    if (metricsServer) return;

    try {
        const http = require('http');

        metricsServer = http.createServer(async (req, res) => {
            if (req.url === '/metrics' && req.method === 'GET') {
                try {
                    const metricsOutput = await metricsRegistry.metrics();
                    res.setHeader('Content-Type', metricsRegistry.contentType);
                    res.end(metricsOutput);
                } catch (err) {
                    res.writeHead(500);
                    res.end(`Error: ${err.message}`);
                }
            } else if (req.url === '/health' && req.method === 'GET') {
                res.writeHead(200);
                res.end('OK');
            } else {
                res.writeHead(404);
                res.end('Not Found');
            }
        });

        metricsServer.listen(port, '0.0.0.0', () => {
            plugin.loginfo(`Adaptive rate metrics server listening on port ${port}`);
        });

        metricsServer.on('error', (err) => {
            if (err.code === 'EADDRINUSE') {
                // Another process already has the port - that's fine
                plugin.logdebug(`Metrics port ${port} already in use`);
                metricsServer = null;
            } else {
                plugin.logerror(`Metrics server error: ${err.message}`);
            }
        });
    } catch (err) {
        plugin.logwarn(`Failed to start metrics server: ${err.message}`);
    }
}

/**
 * Update Prometheus metrics for a domain
 */
function updateMetrics(domain, state) {
    if (!metricsInitialized) return;

    try {
        metrics.delayGauge.set({ domain }, state.delay);
        metrics.failuresGauge.set({ domain }, state.consecutiveFailures);
        metrics.rateLimitFailuresGauge.set({ domain }, state.consecutiveRateLimitFailures);
        // Circuit breaker: 1 if open (future timestamp), 0 if closed
        const isCircuitOpen = state.circuitOpenUntil > Date.now() ? 1 : 0;
        metrics.circuitBreakerGauge.set({ domain }, isCircuitOpen);
        const circuitOpenUntilTs = isCircuitOpen ? Math.floor(state.circuitOpenUntil / 1000) : 0;
        metrics.circuitOpenUntilGauge.set({ domain }, circuitOpenUntilTs);
    } catch (err) {
        // Ignore metric errors
    }
}

/**
 * Plugin registration
 */
exports.register = function () {
    plugin_instance = this;

    this.load_config();

    // Initialize Prometheus metrics (if prom-client available)
    initMetrics(this);

    // Register OUTBOUND hooks only
    // These hooks are specific to outbound delivery queue processing
    this.register_hook('send_email', 'on_send_email');   // Before sending from queue
    this.register_hook('delivered', 'on_delivered');     // After successful delivery
    this.register_hook('deferred', 'on_deferred');       // After temporary failure
    this.register_hook('bounce', 'on_bounce');           // After permanent failure

    this.loginfo('Adaptive rate limiting plugin registered (outbound only)');
};

/**
 * Save state to persistent storage
 * Called periodically and on significant events (circuit breaker trip)
 */
function saveState(plugin) {
    if (!config.stateFile) return;

    try {
        const stateData = {
            version: 1,
            savedAt: Date.now(),
            domains: {}
        };

        for (const [domain, state] of domainState.entries()) {
            // Only save domains with meaningful state
            if (state.delay > config.initialDelay ||
                state.consecutiveRateLimitFailures > 0 ||
                state.circuitOpenUntil > Date.now() ||
                state.noSendUntil > Date.now()) {
                stateData.domains[domain] = {
                    delay: state.delay,
                    consecutiveSuccesses: state.consecutiveSuccesses,
                    consecutiveFailures: state.consecutiveFailures,
                    consecutiveRateLimitFailures: state.consecutiveRateLimitFailures,
                    totalDelivered: state.totalDelivered,
                    totalDeferred: state.totalDeferred,
                    totalBounced: state.totalBounced,
                    totalRateLimited: state.totalRateLimited,
                    totalCircuitBreakerTrips: state.totalCircuitBreakerTrips,
                    circuitOpenUntil: state.circuitOpenUntil,
                    noSendUntil: state.noSendUntil,
                    lastUpdate: state.lastUpdate,
                    lastError: state.lastError
                };
            }
        }

        // Ensure directory exists
        const dir = path.dirname(config.stateFile);
        if (!fs.existsSync(dir)) {
            fs.mkdirSync(dir, { recursive: true });
        }

        // Write atomically (write to temp, then rename)
        const tempFile = config.stateFile + '.tmp';
        fs.writeFileSync(tempFile, JSON.stringify(stateData, null, 2));
        fs.renameSync(tempFile, config.stateFile);

        lastStateSave = Date.now();
        const domainCount = Object.keys(stateData.domains).length;
        if (domainCount > 0) {
            plugin.loginfo(`Adaptive rate state saved: ${domainCount} domains to ${config.stateFile}`);
        }
    } catch (err) {
        plugin.logerror(`Adaptive rate state save failed: ${err.message}`);
    }
}

/**
 * Load state from persistent storage
 * Only loads if state is not older than stateMaxAge
 */
function loadState(plugin) {
    if (!config.stateFile) return;

    try {
        if (!fs.existsSync(config.stateFile)) {
            plugin.loginfo(`Adaptive rate state file not found: ${config.stateFile}`);
            return;
        }

        const content = fs.readFileSync(config.stateFile, 'utf8');
        const stateData = JSON.parse(content);

        // Check version
        if (stateData.version !== 1) {
            plugin.logwarn(`Adaptive rate state file version mismatch: expected 1, got ${stateData.version}`);
            return;
        }

        // Check age
        const age = Date.now() - stateData.savedAt;
        if (age > config.stateMaxAge) {
            plugin.loginfo(`Adaptive rate state too old: ${Math.round(age / 1000)}s > ${Math.round(config.stateMaxAge / 1000)}s max, ignoring`);
            return;
        }

        // Restore state
        let restored = 0;
        const now = Date.now();

        for (const [domain, saved] of Object.entries(stateData.domains)) {
            // Skip if circuit/pause already expired
            const circuitExpired = saved.circuitOpenUntil > 0 && saved.circuitOpenUntil <= now;
            const pauseExpired = saved.noSendUntil > 0 && saved.noSendUntil <= now;

            // Only restore if still meaningful
            if (saved.delay > config.initialDelay ||
                saved.consecutiveRateLimitFailures > 0 ||
                (saved.circuitOpenUntil > now) ||
                (saved.noSendUntil > now)) {

                const cfg = getDomainConfig(domain);
                domainState.set(domain, {
                    delay: saved.delay,
                    consecutiveSuccesses: saved.consecutiveSuccesses || 0,
                    consecutiveFailures: saved.consecutiveFailures || 0,
                    consecutiveRateLimitFailures: saved.consecutiveRateLimitFailures || 0,
                    totalDelivered: saved.totalDelivered || 0,
                    totalDeferred: saved.totalDeferred || 0,
                    totalBounced: saved.totalBounced || 0,
                    totalRateLimited: saved.totalRateLimited || 0,
                    totalCircuitBreakerTrips: saved.totalCircuitBreakerTrips || 0,
                    lastUpdate: saved.lastUpdate || now,
                    lastSendTime: 0,  // Reset - don't inherit send timing
                    circuitOpenUntil: circuitExpired ? 0 : (saved.circuitOpenUntil || 0),
                    noSendUntil: pauseExpired ? 0 : (saved.noSendUntil || 0),
                    lastError: saved.lastError || null
                });

                // Update metrics for restored state
                updateMetrics(domain, domainState.get(domain));
                restored++;

                // Log important restored states
                const state = domainState.get(domain);
                if (state.circuitOpenUntil > now) {
                    const remaining = Math.round((state.circuitOpenUntil - now) / 1000);
                    plugin.logwarn(`Adaptive rate: restored OPEN circuit for ${domain}, ${remaining}s remaining`);
                } else if (state.consecutiveRateLimitFailures > 0) {
                    plugin.loginfo(`Adaptive rate: restored ${domain} - delay=${state.delay}ms, streak=${state.consecutiveRateLimitFailures}`);
                }
            }
        }

        plugin.loginfo(`Adaptive rate state restored: ${restored} domains from ${config.stateFile} (age: ${Math.round(age / 1000)}s)`);

    } catch (err) {
        plugin.logerror(`Adaptive rate state load failed: ${err.message}`);
    }
}

/**
 * Export saveState for manual trigger or circuit breaker events
 */
exports.save_state = function () {
    if (plugin_instance) {
        saveState(plugin_instance);
    }
};

/**
 * Load configuration
 */
exports.load_config = function () {
    const plugin = this;

    const cfg = plugin.config.get('adaptive-rate.ini', {
        booleans: ['+enabled']
    }, () => {
        plugin.load_config();
    });

    // Main configuration
    config = {
        enabled: cfg.main?.enabled !== false,
        metricsPort: parseInt(cfg.main?.metrics_port, 10) || 8081,
        minDelay: parseInt(cfg.main?.min_delay, 10) || 1000,
        maxDelay: parseInt(cfg.main?.max_delay, 10) || 300000,  // 5 minutes default
        initialDelay: parseInt(cfg.main?.initial_delay, 10) || 5000,
        backoffMultiplier: parseFloat(cfg.main?.backoff_multiplier) || 1.5,
        recoveryRate: parseFloat(cfg.main?.recovery_rate) || 0.9,
        successThreshold: parseInt(cfg.main?.success_threshold, 10) || 5,
        circuitBreakerThreshold: parseInt(cfg.main?.circuit_breaker_threshold, 10) || 5,
        circuitBreakerDuration: parseInt(cfg.main?.circuit_breaker_duration, 10) || 600000,  // 10 minutes
        domains: {},
        domainOverrides: {}
    };

    // Parse enabled domains
    if (cfg.domains) {
        for (const [domain, enabled] of Object.entries(cfg.domains)) {
            if (enabled === true || enabled === 'true' || enabled === '1') {
                config.domains[domain.toLowerCase()] = true;
            }
        }
    }

    // Parse domain-specific overrides
    for (const section of Object.keys(cfg)) {
        if (section !== 'main' && section !== 'domains' && cfg[section]) {
            const domainCfg = cfg[section];
            config.domainOverrides[section.toLowerCase()] = {
                minDelay: parseInt(domainCfg.min_delay, 10) || config.minDelay,
                maxDelay: parseInt(domainCfg.max_delay, 10) || config.maxDelay,
                initialDelay: parseInt(domainCfg.initial_delay, 10) || config.initialDelay,
                backoffMultiplier: parseFloat(domainCfg.backoff_multiplier) || config.backoffMultiplier,
                recoveryRate: parseFloat(domainCfg.recovery_rate) || config.recoveryRate,
                successThreshold: parseInt(domainCfg.success_threshold, 10) || config.successThreshold,
                circuitBreakerThreshold: parseInt(domainCfg.circuit_breaker_threshold, 10) || config.circuitBreakerThreshold,
                circuitBreakerDuration: parseInt(domainCfg.circuit_breaker_duration, 10) || config.circuitBreakerDuration
            };
        }
    }

    // State persistence configuration
    config.stateFile = cfg.main?.state_file || '';  // Empty = disabled
    config.stateSaveInterval = parseInt(cfg.main?.state_save_interval, 10) || 300000;  // 5 minutes
    config.stateMaxAge = parseInt(cfg.main?.state_max_age, 10) || 3600000;  // 1 hour

    // Load persisted state if configured
    if (config.stateFile) {
        loadState(plugin);

        // Set up periodic save
        if (stateSaveInterval) {
            clearInterval(stateSaveInterval);
        }
        stateSaveInterval = setInterval(() => {
            saveState(plugin);
        }, config.stateSaveInterval);

        plugin.loginfo(`Adaptive rate state persistence enabled: file=${config.stateFile}, interval=${config.stateSaveInterval}ms, maxAge=${config.stateMaxAge}ms`);
    }

    plugin.loginfo(`Adaptive rate config loaded: enabled=${config.enabled}, domains=${Object.keys(config.domains).join(',')}`);
};

/**
 * Check if domain should have adaptive rate limiting
 * Also checks mapped provider (e.g., gmail.com -> google.com)
 */
function isDomainEnabled(domain) {
    if (!domain) return false;

    // Check wildcard (__all__ in config, or * in code)
    if (config.domains['__all__'] || config.domains['*']) return true;

    // Check exact match
    if (config.domains[domain]) return true;

    // Check mapped provider (e.g., gmail.com -> google.com)
    const mappedProvider = KNOWN_PROVIDER_MAPPINGS[domain];
    if (mappedProvider && config.domains[mappedProvider]) return true;

    // Check parent domains (e.g., mail.google.com -> google.com)
    const parts = domain.split('.');
    for (let i = 1; i < parts.length; i++) {
        const parent = parts.slice(i).join('.');
        if (config.domains[parent]) return true;
    }

    return false;
}

/**
 * Extract base domain from MX hostname (last 2 parts)
 * Examples:
 *   smtp.google.com -> google.com
 *   aspmx.l.google.com -> google.com
 *   mx1.mail.yahoo.com -> yahoo.com
 *   mx.yandex.ru -> yandex.ru
 */
function normalizeMxProvider(mxHost) {
    if (!mxHost || typeof mxHost !== 'string') return null;

    const parts = mxHost.toLowerCase().trim().split('.');
    if (parts.length >= 2) {
        return parts.slice(-2).join('.');
    }
    return mxHost.toLowerCase();
}

// Hardcoded mapping from common recipient domains to their MX providers
// This ensures consistent rate limiting even before MX is resolved
const KNOWN_PROVIDER_MAPPINGS = {
    // Google
    'gmail.com': 'google.com',
    'googlemail.com': 'google.com',
    // Microsoft
    'hotmail.com': 'outlook.com',
    'live.com': 'outlook.com',
    'msn.com': 'outlook.com',
    'outlook.com': 'outlook.com',
    // Yahoo
    'yahoo.com': 'yahoo.com',
    'ymail.com': 'yahoo.com',
    'rocketmail.com': 'yahoo.com',
    // iCloud
    'icloud.com': 'icloud.com',
    'me.com': 'icloud.com',
    'mac.com': 'icloud.com',
    // AOL (owned by Yahoo)
    'aol.com': 'yahoo.com',
    // Mail.ru
    'mail.ru': 'mail.ru',
    'inbox.ru': 'mail.ru',
    'list.ru': 'mail.ru',
    'bk.ru': 'mail.ru',
    // Yandex
    'yandex.ru': 'yandex.ru',
    'yandex.com': 'yandex.ru',
    'ya.ru': 'yandex.ru',
};

// Cache mapping from recipient domain to MX provider
// Used in send_email hook where MX isn't known yet
const domainToMxCache = new Map();

/**
 * Get MX provider for rate limiting, with caching
 * Uses hardcoded mappings for known providers, then cache, then MX lookup result
 */
function getMxProvider(recipientDomain, mxHost) {
    // If MX host is provided, normalize it and cache the mapping
    if (mxHost) {
        const provider = normalizeMxProvider(mxHost);
        if (provider) {
            // Cache for future send_email lookups
            domainToMxCache.set(recipientDomain, provider);
            return provider;
        }
    }

    // Check hardcoded mappings first (most reliable)
    if (KNOWN_PROVIDER_MAPPINGS[recipientDomain]) {
        return KNOWN_PROVIDER_MAPPINGS[recipientDomain];
    }

    // Try cache (populated from previous MX lookups)
    if (domainToMxCache.has(recipientDomain)) {
        return domainToMxCache.get(recipientDomain);
    }

    // Fallback to recipient domain (will be corrected on first delivery/deferral)
    return recipientDomain;
}

/**
 * Get configuration for specific domain
 */
/**
 * Get configuration for specific domain
 * Also checks mapped provider for overrides
 */
function getDomainConfig(domain) {
    // Check for domain-specific override first
    if (config.domainOverrides[domain]) {
        return config.domainOverrides[domain];
    }

    // Check mapped provider override (e.g., state is 'google.com', check 'gmail.com' config)
    // This supports users who configured 'gmail.com' in values.yaml
    for (const [recipientDomain, provider] of Object.entries(KNOWN_PROVIDER_MAPPINGS)) {
        if (provider === domain && config.domainOverrides[recipientDomain]) {
            return config.domainOverrides[recipientDomain];
        }
    }

    // Check for wildcard override (__all__)
    if (config.domainOverrides['__all__']) {
        return config.domainOverrides['__all__'];
    }
    // Fall back to global config
    return config;
}

/**
 * Check if this is outbound context (has todo with domain)
 */
function isOutbound(hmail) {
    // Outbound HMailItem has todo object with domain
    return hmail && hmail.todo && typeof hmail.todo.domain === 'string';
}

/**
 * Get or create domain state
 */
function getState(domain) {
    if (!domainState.has(domain)) {
        const cfg = getDomainConfig(domain);
        domainState.set(domain, {
            delay: cfg.initialDelay,
            consecutiveSuccesses: 0,
            consecutiveFailures: 0,           // All deferred errors (for monitoring)
            consecutiveRateLimitFailures: 0,  // Only rate limit errors (controls delay)
            totalDelivered: 0,
            totalDeferred: 0,
            totalBounced: 0,
            totalRateLimited: 0,              // Total rate limit responses
            totalCircuitBreakerTrips: 0,      // Total circuit breaker activations
            lastUpdate: Date.now(),
            lastSendTime: 0,           // Track last send time for rate limiting
            circuitOpenUntil: 0,       // Timestamp when circuit breaker closes (0 = closed)
            noSendUntil: 0,            // Immediate pause until timestamp (set on rate limit)
            lastError: null
        });
    }
    return domainState.get(domain);
}

/**
 * Hook: Before sending email - apply delay if needed (OUTBOUND ONLY)
 *
 * Rate limiting strategy:
 * - Track last send time per MX provider (not recipient domain)
 * - If time since last send < current delay, apply remaining delay
 * - This ensures minimum interval between sends to same MX provider
 *
 * Note: MX is not yet known at send_email time, so we use cached mapping
 * from previous deliveries. New domains will use recipient domain as fallback.
 */
exports.on_send_email = function (next, hmail) {
    const plugin = this;

    // Start metrics server on first outbound call (ensures it runs in outbound process)
    ensureMetricsServer(plugin);

    if (!config.enabled) return next();

    // Verify this is outbound context
    if (!isOutbound(hmail)) {
        plugin.logdebug(`on_send_email: skipping - not outbound`);
        return next();
    }

    const recipientDomain = hmail.todo.domain;

    // Get MX provider FIRST (from cache or fallback to recipient domain)
    // This is critical: config may have 'google.com' but recipient is 'gmail.com'
    const mxProvider = getMxProvider(recipientDomain, null);

    // Check if either MX provider OR recipient domain is enabled
    // This ensures gmail.com -> google.com mapping is considered
    if (!isDomainEnabled(mxProvider) && !isDomainEnabled(recipientDomain)) {
        return next();
    }

    // Debug only - too noisy for info

    const state = getState(mxProvider);
    const cfg = getDomainConfig(mxProvider);
    const now = Date.now();

    // CIRCUIT BREAKER CHECK
    // If circuit is open, DELAY for remaining time
    if (state.circuitOpenUntil > now) {
        const remainingMs = state.circuitOpenUntil - now;
        const delaySec = Math.ceil(remainingMs / 1000);
        plugin.logwarn(`Adaptive rate: CIRCUIT OPEN for ${mxProvider} - DELAY ${delaySec}s (closes at ${new Date(state.circuitOpenUntil).toISOString()})`);

        // Record metric: delay applied (due to circuit breaker)
        if (metricsInitialized && metrics.delaysAppliedCounter) {
            try { metrics.delaysAppliedCounter.inc({ domain: mxProvider }); } catch (e) { /* ignore */ }
        }

        return next(DELAY, String(delaySec));
    }

    // Circuit was open but now closed - gradual recovery, NOT full reset
    if (state.circuitOpenUntil > 0 && state.circuitOpenUntil <= now) {
        plugin.loginfo(`Adaptive rate: Circuit CLOSED for ${mxProvider} - starting gradual recovery (delay: ${state.delay}ms, will decrease on successes)`);
        state.circuitOpenUntil = 0;
        // DO NOT reset consecutiveRateLimitFailures - require successes to clear it
        // DO NOT reset delay - keep high delay, let successful deliveries reduce it gradually
        state.noSendUntil = 0;  // Clear immediate pause to allow sending
        updateMetrics(mxProvider, state);
    }

    // IMMEDIATE PAUSE CHECK (softer than circuit breaker, set on each rate limit)
    // This ensures immediate throttling even before circuit breaker threshold is reached
    if (state.noSendUntil > now) {
        const remainingMs = state.noSendUntil - now;
        const delaySec = Math.ceil(remainingMs / 1000);

        // Record metric: delay applied
        if (metricsInitialized && metrics.delaysAppliedCounter) {
            try { metrics.delaysAppliedCounter.inc({ domain: mxProvider }); } catch (e) { /* ignore */ }
        }

        plugin.loginfo(`Adaptive rate: PAUSED for ${mxProvider} - DELAY ${delaySec}s (streak: ${state.consecutiveRateLimitFailures})`);
        return next(DELAY, String(delaySec));
    }

    // Calculate time since last send
    const timeSinceLastSend = now - state.lastSendTime;

    // Apply provider pacing between sends:
    // - Before first rate-limit, enforce baseline minDelay (preemptive pacing)
    // - After rate-limit failures, enforce adaptive delay (reactive backoff)
    const targetInterval = state.consecutiveRateLimitFailures > 0
        ? state.delay
        : cfg.minDelay;

    if (targetInterval > 0) {
        const remainingDelay = targetInterval - timeSinceLastSend;

        if (remainingDelay > 0) {
            const delaySec = Math.ceil(remainingDelay / 1000);

            // Record metric: delay applied
            if (metricsInitialized && metrics.delaysAppliedCounter) {
                try { metrics.delaysAppliedCounter.inc({ domain: mxProvider }); } catch (e) { /* ignore */ }
            }

            const mode = state.consecutiveRateLimitFailures > 0 ? 'rate-limit throttle' : 'baseline throttle';

            if (mode === 'baseline throttle' && metricsInitialized && metrics.baselineThrottledCounter) {
                try { metrics.baselineThrottledCounter.inc({ domain: mxProvider }); } catch (e) { /* ignore */ }
            }

            plugin.loginfo(`Adaptive rate: ${mode} ${mxProvider} - DELAY ${delaySec}s (streak: ${state.consecutiveRateLimitFailures})`);
            return next(DELAY, String(delaySec));
        }
    }

    // Update last send time
    state.lastSendTime = now;

    next();
};

/**
 * Hook: Email delivered successfully (OUTBOUND ONLY)
 */
exports.on_delivered = function (next, hmail, params) {
    const plugin = this;

    // Start metrics server on first outbound call
    ensureMetricsServer(plugin);

    if (!config.enabled) return next();

    // Verify this is outbound context
    if (!isOutbound(hmail)) {
        plugin.logdebug(`on_delivered: skipping - not outbound`);
        return next();
    }

    const recipientDomain = hmail.todo.domain;

    // Get MX host from params (first element is MX hostname)
    // Resolve mxProvider BEFORE isDomainEnabled check
    const mxHost = params?.[0] || null;
    const mxProvider = getMxProvider(recipientDomain, mxHost);

    // Check if either MX provider OR recipient domain is enabled
    if (!isDomainEnabled(mxProvider) && !isDomainEnabled(recipientDomain)) {
        return next();
    }

    plugin.logdebug(`on_delivered: recipient=${recipientDomain}, mx_host=${mxHost}, mxProvider=${mxProvider}`);

    const state = getState(mxProvider);
    const cfg = getDomainConfig(mxProvider);
    const now = Date.now();

    state.totalDelivered++;
    state.consecutiveSuccesses++;
    state.consecutiveFailures = 0;
    state.lastUpdate = now;

    // CRITICAL: Do NOT reset rate limit state if circuit breaker is active
    // This prevents race condition where parallel in-flight deliveries
    // (sent BEFORE rate-limit was detected) reset the protection
    const isCircuitActive = state.circuitOpenUntil > now;
    const isPauseActive = state.noSendUntil > now;

    if (isCircuitActive) {
        // Circuit is open - do NOT close it early, let it expire naturally
        // This prevents race condition with parallel deliveries
        plugin.loginfo(`Adaptive rate: ${mxProvider} delivered while circuit OPEN - ignoring (circuit closes at ${new Date(state.circuitOpenUntil).toISOString()})`);
        // Still count successes, will be used when circuit naturally closes
    }
    // NOTE: Pause (noSendUntil) does NOT block recovery accounting
    // Pause only throttles outbound send_email, not success counting
    // Successful deliveries prove Google is accepting, so count them for recovery

    // Record metrics
    if (metricsInitialized && metrics.deliveriesCounter) {
        try { metrics.deliveriesCounter.inc({ domain: mxProvider }); } catch (e) { /* ignore */ }
    }
    updateMetrics(mxProvider, state);

    // Reduce delay AND reduce streak after threshold consecutive successes
    // Only block recovery if circuit is OPEN (hard stop)
    // Pause does NOT block - it only throttles send_email
    if (!isCircuitActive && state.consecutiveSuccesses >= cfg.successThreshold) {
        const oldDelay = state.delay;
        const oldStreak = state.consecutiveRateLimitFailures;

        state.delay = Math.max(
            Math.floor(state.delay * cfg.recoveryRate),
            cfg.minDelay
        );
        // Reduce streak gradually, not instant reset
        state.consecutiveRateLimitFailures = Math.max(0, state.consecutiveRateLimitFailures - cfg.successThreshold);
        state.consecutiveSuccesses = 0;

        // Clear pause if we're recovering successfully
        if (state.noSendUntil > 0) {
            state.noSendUntil = 0;
            plugin.loginfo(`Adaptive rate: ${mxProvider} pause cleared after ${cfg.successThreshold} consecutive successes`);
        }

        if (oldDelay !== state.delay || oldStreak !== state.consecutiveRateLimitFailures) {
            plugin.loginfo(`Adaptive rate: ${mxProvider} recovery - delay ${oldDelay}ms -> ${state.delay}ms, streak ${oldStreak} -> ${state.consecutiveRateLimitFailures} (${cfg.successThreshold} consecutive successes)`);
            updateMetrics(mxProvider, state);
        }
    }

    // Only log if there's active protection or meaningful streak
    if (isCircuitActive || isPauseActive || state.consecutiveSuccesses > 0) {
        plugin.loginfo(`Adaptive rate: ${mxProvider} delivered (total: ${state.totalDelivered}, successStreak: ${state.consecutiveSuccesses}/${cfg.successThreshold})`);
    }
    next();
};

/**
 * Hook: Email deferred (temporary failure) (OUTBOUND ONLY)
 */
exports.on_deferred = function (next, hmail, params) {
    const plugin = this;

    // Start metrics server on first outbound call
    ensureMetricsServer(plugin);

    if (!config.enabled) return next();

    // Verify this is outbound context
    if (!isOutbound(hmail)) {
        plugin.logdebug(`on_deferred: skipping - not outbound`);
        return next();
    }

    const recipientDomain = hmail.todo.domain;
    const errMsg = params?.err?.message || params?.err || String(params?.err || '');

    // Get MX host from params or hmail
    // In deferred hook, MX might be in params.host or hmail.todo.mxlist
    // Resolve mxProvider BEFORE isDomainEnabled check
    const mxHost = params?.host || hmail?.todo?.mxlist?.[0]?.exchange || null;
    const mxProvider = getMxProvider(recipientDomain, mxHost);

    // Check if either MX provider OR recipient domain is enabled
    if (!isDomainEnabled(mxProvider) && !isDomainEnabled(recipientDomain)) {
        return next();
    }

    // Log all deferred events at info level - they're important for troubleshooting
    plugin.loginfo(`Adaptive rate: ${mxProvider} deferred - ${errMsg.substring(0, 100)}`);

    // Note: DELAY responses from send_email do NOT trigger the deferred hook
    // (Haraka routes them to temp_fail_queue internally), so all events here
    // are genuine remote MX deferrals.

    const state = getState(mxProvider);
    const cfg = getDomainConfig(mxProvider);

    state.totalDeferred++;
    state.consecutiveFailures++;      // Track all failures for monitoring
    state.lastUpdate = Date.now();
    state.lastError = errMsg.substring(0, 200);

    // Record metric: deferral
    if (metricsInitialized && metrics.deferralsCounter) {
        try { metrics.deferralsCounter.inc({ domain: mxProvider }); } catch (e) { /* ignore */ }
    }

    // Check for explicit rate limiting (421, 4.7.28, etc.)
    // Only increase delay for rate limit errors, not all 4xx errors
    // Other 4xx (450 mailbox unavailable, 451 local error, 452 storage, 454 TLS)
    // are typically recipient-specific and shouldn't slow down the whole provider
    const isRateLimited = /421|4\.7\.28|rate.?limit|too.?many|try.?again.?later|throttl/i.test(errMsg);

    if (isRateLimited) {
        // Only reset success streak for rate-limit errors
        // Non-rate-limit deferrals (mailbox full, etc.) are recipient-specific
        // and should NOT block recovery of provider-wide rate limiting
        state.consecutiveSuccesses = 0;
        state.consecutiveRateLimitFailures++;  // Track rate limit streak separately
        state.totalRateLimited++;

        const oldDelay = state.delay;
        state.delay = Math.min(
            Math.floor(state.delay * cfg.backoffMultiplier),
            cfg.maxDelay
        );

        // IMMEDIATE PAUSE: Set hard pause timestamp based on current delay
        // This blocks ALL sends to this provider until pause expires
        state.noSendUntil = Date.now() + state.delay;
        plugin.logwarn(`Adaptive rate: ${mxProvider} RATE LIMITED - delay ${oldDelay}ms -> ${state.delay}ms, PAUSED until ${new Date(state.noSendUntil).toISOString()} (rate limit streak: ${state.consecutiveRateLimitFailures})`);

        // CIRCUIT BREAKER: Open or EXTEND circuit on continued rate-limits
        if (state.consecutiveRateLimitFailures >= cfg.circuitBreakerThreshold) {
            const now = Date.now();
            const wasOpen = state.circuitOpenUntil > now;
            // For extend: use MAX of current circuit end and now + duration
            // This ensures circuit ALWAYS extends forward, even if Date.now() returns same millisecond
            const newCircuitEnd = wasOpen
                ? Math.max(state.circuitOpenUntil, now) + cfg.circuitBreakerDuration
                : now + cfg.circuitBreakerDuration;

            if (!wasOpen) {
                // First trip - open circuit
                state.circuitOpenUntil = newCircuitEnd;
                state.totalCircuitBreakerTrips++;
                plugin.logerror(`Adaptive rate: CIRCUIT BREAKER TRIPPED for ${mxProvider} - pausing ALL sends for ${cfg.circuitBreakerDuration / 1000}s (${state.consecutiveRateLimitFailures} consecutive rate limits)`);

                // Record metric: circuit breaker trip
                if (metricsInitialized && metrics.circuitBreakerTripsCounter) {
                    try { metrics.circuitBreakerTripsCounter.inc({ domain: mxProvider }); } catch (e) { /* ignore */ }
                }

                // Save state immediately on circuit breaker trip
                saveState(plugin);
            } else {
                // Circuit already open - EXTEND it (rate-limits still coming from in-flight messages)
                state.circuitOpenUntil = newCircuitEnd;
                plugin.logwarn(`Adaptive rate: Circuit EXTENDED for ${mxProvider} - still receiving rate-limits (streak: ${state.consecutiveRateLimitFailures}), new close time: ${new Date(newCircuitEnd).toISOString()}`);
            }
        }

        // Record metric: explicit rate limit
        if (metricsInitialized && metrics.rateLimitedCounter) {
            try { metrics.rateLimitedCounter.inc({ domain: mxProvider }); } catch (e) { /* ignore */ }
        }
    } else {
        plugin.loginfo(`Adaptive rate: ${mxProvider} deferred (non-rate-limit) - no delay increase (all failures: ${state.consecutiveFailures})`);
    }

    updateMetrics(mxProvider, state);
    next();
};

/**
 * Hook: Email bounced (permanent failure) (OUTBOUND ONLY)
 */
exports.on_bounce = function (next, hmail, err) {
    const plugin = this;

    // Start metrics server on first outbound call
    ensureMetricsServer(plugin);

    if (!config.enabled) return next();

    // Verify this is outbound context
    if (!isOutbound(hmail)) {
        plugin.logdebug(`on_bounce: skipping - not outbound`);
        return next();
    }

    const recipientDomain = hmail.todo.domain;

    // Get MX host from hmail (bounce may not have it readily available)
    // Resolve mxProvider BEFORE isDomainEnabled check
    const mxHost = hmail?.todo?.mxlist?.[0]?.exchange || null;
    const mxProvider = getMxProvider(recipientDomain, mxHost);

    // Check if either MX provider OR recipient domain is enabled
    if (!isDomainEnabled(mxProvider) && !isDomainEnabled(recipientDomain)) {
        return next();
    }

    plugin.logdebug(`on_bounce: recipient=${recipientDomain}, mxProvider=${mxProvider}`);

    const state = getState(mxProvider);
    state.totalBounced++;
    state.lastUpdate = Date.now();

    // Record metric: bounce
    if (metricsInitialized && metrics.bouncesCounter) {
        try { metrics.bouncesCounter.inc({ domain: mxProvider }); } catch (e) { /* ignore */ }
    }

    plugin.logwarn(`Adaptive rate: ${mxProvider} bounced (total bounces: ${state.totalBounced})`);
    next();
};

/**
 * Get current statistics for all domains (for monitoring/Prometheus)
 */
exports.get_stats = function () {
    const stats = {};
    const now = Date.now();
    for (const [domain, state] of domainState.entries()) {
        const isCircuitOpen = state.circuitOpenUntil > now;
        const isPaused = state.noSendUntil > now;
        stats[domain] = {
            delay_ms: state.delay,
            consecutive_successes: state.consecutiveSuccesses,
            consecutive_failures: state.consecutiveFailures,
            consecutive_rate_limit_failures: state.consecutiveRateLimitFailures,
            total_delivered: state.totalDelivered,
            total_deferred: state.totalDeferred,
            total_bounced: state.totalBounced,
            total_rate_limited: state.totalRateLimited,
            paused: isPaused,
            paused_until: isPaused ? new Date(state.noSendUntil).toISOString() : null,
            paused_remaining_ms: isPaused ? state.noSendUntil - now : 0,
            circuit_breaker_open: isCircuitOpen,
            circuit_breaker_closes_at: isCircuitOpen ? new Date(state.circuitOpenUntil).toISOString() : null,
            circuit_breaker_remaining_ms: isCircuitOpen ? state.circuitOpenUntil - now : 0,
            total_circuit_breaker_trips: state.totalCircuitBreakerTrips,
            last_send_time: state.lastSendTime ? new Date(state.lastSendTime).toISOString() : null,
            last_error: state.lastError,
            last_update: new Date(state.lastUpdate).toISOString()
        };
    }
    return stats;
};

/**
 * Get stats for specific domain
 */
exports.get_domain_stats = function (domain) {
    if (domainState.has(domain)) {
        const state = domainState.get(domain);
        const now = Date.now();
        const isCircuitOpen = state.circuitOpenUntil > now;
        const isPaused = state.noSendUntil > now;
        return {
            delay_ms: state.delay,
            consecutive_successes: state.consecutiveSuccesses,
            consecutive_failures: state.consecutiveFailures,
            consecutive_rate_limit_failures: state.consecutiveRateLimitFailures,
            total_delivered: state.totalDelivered,
            total_deferred: state.totalDeferred,
            total_bounced: state.totalBounced,
            total_rate_limited: state.totalRateLimited,
            paused: isPaused,
            paused_until: isPaused ? new Date(state.noSendUntil).toISOString() : null,
            paused_remaining_ms: isPaused ? state.noSendUntil - now : 0,
            circuit_breaker_open: isCircuitOpen,
            circuit_breaker_closes_at: isCircuitOpen ? new Date(state.circuitOpenUntil).toISOString() : null,
            circuit_breaker_remaining_ms: isCircuitOpen ? state.circuitOpenUntil - now : 0,
            total_circuit_breaker_trips: state.totalCircuitBreakerTrips,
            last_send_time: state.lastSendTime ? new Date(state.lastSendTime).toISOString() : null,
            last_error: state.lastError,
            last_update: new Date(state.lastUpdate).toISOString()
        };
    }
    return null;
};

/**
 * Reset state for a domain (for testing/admin)
 */
exports.reset_domain = function (domain) {
    if (domainState.has(domain)) {
        domainState.delete(domain);
        return true;
    }
    return false;
};

/**
 * Close circuit breaker for a domain (for admin recovery)
 */
exports.close_circuit = function (domain) {
    if (domainState.has(domain)) {
        const state = domainState.get(domain);
        const cfg = getDomainConfig(domain);
        let changed = false;
        if (state.circuitOpenUntil > 0) {
            state.circuitOpenUntil = 0;
            changed = true;
        }
        if (state.noSendUntil > 0) {
            state.noSendUntil = 0;
            changed = true;
        }
        if (changed) {
            state.consecutiveRateLimitFailures = 0;
            state.delay = cfg.initialDelay;
            updateMetrics(domain, state);
            return true;
        }
    }
    return false;
};

/**
 * Reset all domain states
 */
exports.reset_all = function () {
    const count = domainState.size;
    domainState.clear();
    return count;
};

/**
 * Periodic cleanup of stale entries (call from cron/timer if needed)
 */
exports.cleanup_stale = function (maxAgeMs = 3600000) { // 1 hour default
    const now = Date.now();
    let cleaned = 0;

    for (const [domain, state] of domainState.entries()) {
        if (now - state.lastUpdate > maxAgeMs) {
            domainState.delete(domain);
            cleaned++;
        }
    }

    return cleaned;
};

/**
 * Get list of domains with high failure rates (for alerting)
 */
exports.get_problem_domains = function (minFailures = 3) {
    const problems = [];
    const now = Date.now();
    for (const [domain, state] of domainState.entries()) {
        const isCircuitOpen = state.circuitOpenUntil > now;
        const isPaused = state.noSendUntil > now;
        if (state.consecutiveFailures >= minFailures || isCircuitOpen || isPaused) {
            problems.push({
                domain,
                consecutive_failures: state.consecutiveFailures,
                consecutive_rate_limit_failures: state.consecutiveRateLimitFailures,
                delay_ms: state.delay,
                paused: isPaused,
                paused_remaining_ms: isPaused ? state.noSendUntil - now : 0,
                circuit_breaker_open: isCircuitOpen,
                circuit_breaker_remaining_ms: isCircuitOpen ? state.circuitOpenUntil - now : 0,
                last_error: state.lastError
            });
        }
    }
    return problems.sort((a, b) => {
        // Sort by circuit breaker status first, then pause, then by failures
        if (a.circuit_breaker_open !== b.circuit_breaker_open) {
            return a.circuit_breaker_open ? -1 : 1;
        }
        if (a.paused !== b.paused) {
            return a.paused ? -1 : 1;
        }
        return b.consecutive_failures - a.consecutive_failures;
    });
};

/**
 * Get list of domains with open circuit breakers (for alerting)
 */
exports.get_open_circuits = function () {
    const circuits = [];
    const now = Date.now();
    for (const [domain, state] of domainState.entries()) {
        if (state.circuitOpenUntil > now) {
            circuits.push({
                domain,
                closes_at: new Date(state.circuitOpenUntil).toISOString(),
                remaining_ms: state.circuitOpenUntil - now,
                remaining_seconds: Math.ceil((state.circuitOpenUntil - now) / 1000),
                total_trips: state.totalCircuitBreakerTrips,
                last_error: state.lastError
            });
        }
    }
    return circuits.sort((a, b) => a.remaining_ms - b.remaining_ms);
};
