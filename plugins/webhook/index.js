'use strict';

/**
 * Haraka Webhook Plugin
 *
 * Sends webhook notifications for email delivery events:
 * - delivered: Email successfully delivered
 * - bounced: Email bounced (hard or soft)
 * - deferred: Email temporarily deferred
 *
 * Supports multiple webhook endpoints with independent event filtering.
 */

const http = require('http');
const https = require('https');
const url = require('url');

let config = {
    enabled: false,
    endpoints: [],
    timeout: 5000,
    retry: true,
    max_retries: 3
};
let logger;

/**
 * Plugin registration
 */
exports.register = function () {
    const plugin = this;
    logger = plugin.loginfo.bind(plugin);

    plugin.load_config();

    // Register hooks
    plugin.register_hook('delivered', 'on_delivered');
    plugin.register_hook('bounce', 'on_bounce');
    plugin.register_hook('deferred', 'on_deferred');

    logger('Webhook plugin registered');
};

/**
 * Load configuration from webhook.ini
 *
 * Supports two formats:
 *
 * 1. Legacy format (single endpoint in [main]):
 * [main]
 * enabled=true
 * url=https://api.example.com/webhooks/email
 * events=delivered,bounced
 * header_Authorization=Bearer token123
 *
 * 2. Multi-endpoint format (recommended):
 * [main]
 * enabled=true
 * timeout=5000
 * retry=true
 * max_retries=3
 *
 * [endpoint.main-webhook]
 * url=https://api.example.com/webhooks/email
 * events=delivered,bounced
 * header_Authorization=Bearer token123
 *
 * [endpoint.analytics]
 * url=http://localhost:8888/webhook
 * events=delivered,bounced,deferred
 */
exports.load_config = function () {
    const plugin = this;

    const cfg = plugin.config.get('webhook.ini', {
        booleans: [
            '+main.enabled',
            '+main.retry'
        ]
    }, () => {
        plugin.load_config();
    });

    // Global settings
    config.enabled = cfg.main?.enabled !== false;
    config.timeout = parseInt(cfg.main?.timeout, 10) || 5000;
    config.retry = cfg.main?.retry !== false;
    config.max_retries = parseInt(cfg.main?.max_retries, 10) || 3;

    // Parse endpoints from [endpoint.NAME] sections
    config.endpoints = [];

    for (const section of Object.keys(cfg)) {
        if (!section.startsWith('endpoint.')) continue;

        const name = section.replace('endpoint.', '');
        const endpointCfg = cfg[section];

        if (!endpointCfg.url) {
            plugin.logwarn(`Webhook endpoint '${name}' has no URL, skipping`);
            continue;
        }

        const endpoint = {
            name: name,
            url: endpointCfg.url,
            events: (endpointCfg.events || 'delivered,bounced,deferred').split(',').map(e => e.trim()),
            headers: {}
        };

        // Parse headers (header_NAME=value)
        for (const key of Object.keys(endpointCfg)) {
            if (key.startsWith('header_')) {
                const headerName = key.replace('header_', '').replace(/_/g, '-');
                endpoint.headers[headerName] = endpointCfg[key];
            }
        }

        config.endpoints.push(endpoint);
        logger(`Loaded webhook endpoint '${name}': ${endpoint.url} (events: ${endpoint.events.join(',')})`);
    }

    // Legacy format support: url in [main] section (backward compatibility)
    if (config.endpoints.length === 0 && cfg.main?.url) {
        const endpoint = {
            name: 'default',
            url: cfg.main.url,
            events: (cfg.main.events || 'delivered,bounced,deferred').split(',').map(e => e.trim()),
            headers: {}
        };

        // Parse headers from main section (header_NAME=value or auth_header/auth_token)
        for (const key of Object.keys(cfg.main)) {
            if (key.startsWith('header_')) {
                const headerName = key.replace('header_', '').replace(/_/g, '-');
                endpoint.headers[headerName] = cfg.main[key];
            }
        }

        // Legacy auth_header/auth_token support
        if (cfg.main.auth_header && cfg.main.auth_token) {
            endpoint.headers[cfg.main.auth_header] = cfg.main.auth_token;
        }

        config.endpoints.push(endpoint);
        logger(`Loaded legacy webhook endpoint: ${endpoint.url} (events: ${endpoint.events.join(',')})`);
    }

    if (config.enabled && config.endpoints.length === 0) {
        plugin.logerror('Webhook URL not configured');
        config.enabled = false;
    }

    logger(`Webhook config loaded: enabled=${config.enabled}, endpoints=${config.endpoints.length}`);
};

/**
 * Send webhook notification to a single endpoint
 */
function send_to_endpoint(plugin, endpoint, payload, attempt = 1) {
    return new Promise((resolve, reject) => {
        const parsed = url.parse(endpoint.url);
        const data = JSON.stringify(payload);

        const options = {
            hostname: parsed.hostname,
            port: parsed.port || (parsed.protocol === 'https:' ? 443 : 80),
            path: parsed.path,
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(data),
                'User-Agent': 'Haraka-Webhook/1.0',
                ...endpoint.headers
            },
            timeout: config.timeout
        };

        const transport = parsed.protocol === 'https:' ? https : http;

        const req = transport.request(options, (res) => {
            let body = '';
            res.on('data', chunk => body += chunk);
            res.on('end', () => {
                if (res.statusCode >= 200 && res.statusCode < 300) {
                    logger(`Webhook '${endpoint.name}' sent: ${payload.event} (${res.statusCode})`);
                    resolve();
                } else {
                    const err = new Error(`Webhook '${endpoint.name}' failed: ${res.statusCode} - ${body}`);
                    plugin.logerror(err.message);

                    if (config.retry && attempt < config.max_retries) {
                        const delay = Math.pow(2, attempt) * 1000;
                        plugin.logwarn(`Retrying '${endpoint.name}' in ${delay}ms (attempt ${attempt + 1}/${config.max_retries})`);
                        setTimeout(() => {
                            send_to_endpoint(plugin, endpoint, payload, attempt + 1)
                                .then(resolve)
                                .catch(reject);
                        }, delay);
                    } else {
                        reject(err);
                    }
                }
            });
        });

        req.on('error', (err) => {
            plugin.logerror(`Webhook '${endpoint.name}' error: ${err.message}`);

            if (config.retry && attempt < config.max_retries) {
                const delay = Math.pow(2, attempt) * 1000;
                plugin.logwarn(`Retrying '${endpoint.name}' in ${delay}ms (attempt ${attempt + 1}/${config.max_retries})`);
                setTimeout(() => {
                    send_to_endpoint(plugin, endpoint, payload, attempt + 1)
                        .then(resolve)
                        .catch(reject);
                }, delay);
            } else {
                reject(err);
            }
        });

        req.on('timeout', () => {
            req.destroy();
            const err = new Error(`Webhook '${endpoint.name}' timeout`);
            plugin.logerror(err.message);
            reject(err);
        });

        req.write(data);
        req.end();
    });
}

/**
 * Send webhook to all endpoints matching the event
 */
function send_webhook(plugin, event, payload) {
    if (!config.enabled || config.endpoints.length === 0) {
        return Promise.resolve();
    }

    // Find endpoints that want this event
    const matchingEndpoints = config.endpoints.filter(ep => ep.events.includes(event));

    if (matchingEndpoints.length === 0) {
        return Promise.resolve();
    }

    // Send to all matching endpoints in parallel
    const promises = matchingEndpoints.map(endpoint =>
        send_to_endpoint(plugin, endpoint, payload)
            .catch(err => plugin.logerror(`Failed to send to '${endpoint.name}': ${err.message}`))
    );

    return Promise.all(promises);
}

/**
 * Extract message_id from hmail notes
 * Stored by outbound-headers plugin as client_message_id
 */
function get_message_id(hmail) {
    // Primary: from transaction notes (set by outbound-headers plugin)
    if (hmail?.todo?.notes?.client_message_id) {
        return hmail.todo.notes.client_message_id;
    }
    // Fallback: safe_message_id (sanitized version)
    if (hmail?.todo?.notes?.safe_message_id) {
        return hmail.todo.notes.safe_message_id;
    }
    // Last resort: queue_id
    return hmail?.uuid || '';
}

/**
 * Hook: Email delivered successfully
 */
exports.on_delivered = function (next, hmail, params) {
    const plugin = this;

    const payload = {
        event: 'delivered',
        timestamp: new Date().toISOString(),
        message_id: get_message_id(hmail),
        queue_id: hmail?.uuid || '',
        from: hmail?.todo?.mail_from?.address() || '',
        to: (hmail?.todo?.rcpt_to || []).map(r => r.address()),
        host: params?.host || '',
        response: params?.response || '',
        delay: params?.delay || 0,
        metadata: {
            attempts: hmail?.num_failures || 0,
            mx_host: params?.mx?.exchange || ''
        }
    };

    send_webhook(plugin, 'delivered', payload)
        .catch(err => plugin.logerror(`Failed to send delivered webhooks: ${err.message}`));

    next();
};

/**
 * Classify bounce type based on error code and message
 * @param {Object} error - Bounce error object
 * @returns {{type: string, code: string, message: string}}
 */
function classify_bounce(error) {
    // Extract code from various error formats
    let code = '';
    let message = error?.message || error?.msg || 'Unknown error';

    // Try to extract SMTP code from error object
    if (error?.code) {
        code = String(error.code);
    }

    // Try to extract enhanced status code (e.g., 550 5.1.1)
    if (!code || code === 'undefined') {
        const codeMatch = message.match(/(\d{3})(?:\s+\d\.\d\.\d)?/);
        if (codeMatch) {
            code = codeMatch[1];
        }
    }

    // Determine bounce type
    // Hard bounce: 5xx codes, permanent failures
    // Soft bounce: 4xx codes, temporary failures
    const firstDigit = code.charAt(0);
    const hardBouncePatterns = /permanent|invalid|does\s*not\s*exist|no\s*such\s*user|user\s*unknown|mailbox\s*not\s*found|rejected|refused|blocked|blacklist/i;
    const softBouncePatterns = /temporary|try\s*again|later|defer|quota|full|busy|unavailable/i;

    let type = 'unknown';
    if (firstDigit === '5' || hardBouncePatterns.test(message)) {
        type = 'hard';
    } else if (firstDigit === '4' || softBouncePatterns.test(message)) {
        type = 'soft';
    } else if (message.toLowerCase().includes('some recipients failed')) {
        // Partial delivery - check if we have more details
        type = 'partial';
    }

    return { type, code, message };
}

/**
 * Hook: Email bounced
 */
exports.on_bounce = function (next, hmail, error) {
    const plugin = this;

    // Log error structure for debugging
    plugin.logdebug(`Bounce error object: ${JSON.stringify(error)}`);

    const bounceInfo = classify_bounce(error);

    // Get failed recipients if available
    let failed_recipients = [];
    if (error?.rcpt) {
        // Single recipient
        failed_recipients = [error.rcpt.address ? error.rcpt.address() : String(error.rcpt)];
    } else if (error?.rcpts) {
        // Multiple recipients
        failed_recipients = error.rcpts.map(r => r.address ? r.address() : String(r));
    } else {
        // Fall back to all recipients
        failed_recipients = (hmail?.todo?.rcpt_to || []).map(r => r.address());
    }

    const payload = {
        event: 'bounced',
        timestamp: new Date().toISOString(),
        message_id: get_message_id(hmail),
        queue_id: hmail?.uuid || '',
        from: hmail?.todo?.mail_from?.address() || '',
        to: failed_recipients,
        bounce_type: bounceInfo.type,
        bounce_code: bounceInfo.code,
        bounce_message: bounceInfo.message,
        metadata: {
            attempts: hmail?.num_failures || 0,
            reason: error?.reason || '',
            error_details: {
                code: error?.code,
                msg: error?.msg,
                component: error?.component
            }
        }
    };

    send_webhook(plugin, 'bounced', payload)
        .catch(err => plugin.logerror(`Failed to send bounce webhooks: ${err.message}`));

    next();
};

/**
 * Hook: Email deferred
 */
exports.on_deferred = function (next, hmail, params) {
    const plugin = this;

    const payload = {
        event: 'deferred',
        timestamp: new Date().toISOString(),
        message_id: get_message_id(hmail),
        queue_id: hmail?.uuid || '',
        from: hmail?.todo?.mail_from?.address() || '',
        to: (hmail?.todo?.rcpt_to || []).map(r => r.address()),
        host: params?.host || '',
        response: params?.response || '',
        delay: params?.delay || 0,
        next_attempt: params?.next_attempt || '',
        metadata: {
            attempts: hmail?.num_failures || 0,
            reason: params?.reason || ''
        }
    };

    send_webhook(plugin, 'deferred', payload)
        .catch(err => plugin.logerror(`Failed to send deferred webhooks: ${err.message}`));

    next();
};

/**
 * Test webhook connectivity (sends to all endpoints)
 */
exports.test_webhook = function (callback) {
    const plugin = this;

    const payload = {
        event: 'test',
        timestamp: new Date().toISOString(),
        message: 'Webhook connectivity test'
    };

    const promises = config.endpoints.map(endpoint =>
        send_to_endpoint(plugin, endpoint, payload)
    );

    Promise.all(promises)
        .then(() => callback(null, `Webhook test successful (${config.endpoints.length} endpoints)`))
        .catch(err => callback(err));
};
