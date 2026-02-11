'use strict';

/**
 * Haraka Inbound Handler Plugin
 *
 * Processes inbound bounce/FBL messages and sends webhooks:
 * - Validates HMAC signature on VERP addresses (protection from spam bounces)
 * - Parses DSN (RFC 3464) bounce messages
 * - Parses ARF (RFC 5965) feedback loop messages
 * - Extracts original message ID from VERP or headers
 * - Sends webhook notifications
 * - Discards messages after processing (doesn't deliver)
 *
 * Note: OK and DENY are injected as globals by Haraka's plugin loader
 *
 * Configuration (inbound_handler.ini):
 * [main]
 * enabled=true
 * ; Require HMAC validation for bounces (rejects legacy/forged bounces)
 * require_hmac=true
 * ; HMAC secret (reads from outbound_headers.ini if not set)
 * hmac_secret=
 * ; Maximum bounce age in days (reads from outbound_headers.ini if not set)
 * bounce_max_age_days=7
 *
 * [webhook]
 * ; Webhook URL for inbound events (uses webhook.ini if not set)
 * url=
 * timeout=5000
 *
 * Events sent:
 * - bounce_received: DSN bounce notification
 * - complaint: FBL spam complaint
 */

const http = require('http');
const https = require('https');
const url = require('url');
const crypto = require('crypto');

let config = {
    enabled: true,
    require_hmac: true,      // Require HMAC validation for bounces
    hmac_secret: '',         // HMAC secret for validation
    bounce_max_age_days: 7,  // Max age of valid bounces in days
    webhook: {
        url: '',
        timeout: 5000,
        headers: {}
    }
};

let logger;
let logwarn;
let logerror;

/**
 * Plugin registration
 */
exports.register = function () {
    const plugin = this;
    logger = plugin.loginfo.bind(plugin);
    logwarn = plugin.logwarn.bind(plugin);
    logerror = plugin.logerror.bind(plugin);

    plugin.load_config();

    // Register BOTH hooks to handle inbound messages from any source:
    // - queue: for non-relay connections (external senders, relay=N)
    // - queue_outbound: for relay connections (internal/trusted networks, relay=Y)
    plugin.register_hook('queue', 'process_inbound');
    plugin.register_hook('queue_outbound', 'process_inbound');

    logger('Inbound handler plugin registered (queue + queue_outbound hooks)');
};

/**
 * Load configuration
 */
exports.load_config = function () {
    const plugin = this;

    const cfg = plugin.config.get('inbound_handler.ini', {
        booleans: ['+main.enabled', '+main.require_hmac']
    }, () => {
        plugin.load_config();
    });

    if (cfg.main) {
        config.enabled = cfg.main.enabled !== false;
        config.require_hmac = cfg.main.require_hmac !== false;
        config.hmac_secret = cfg.main.hmac_secret || '';
        config.bounce_max_age_days = parseInt(cfg.main.bounce_max_age_days, 10) || 7;
    }

    // If HMAC secret not configured, try to read from outbound_headers.ini (shared secret)
    if (!config.hmac_secret) {
        const outboundCfg = plugin.config.get('outbound_headers.ini');
        if (outboundCfg?.main?.hmac_secret) {
            config.hmac_secret = outboundCfg.main.hmac_secret;
            logger('Using HMAC secret from outbound_headers.ini');
        }
        if (outboundCfg?.main?.bounce_max_age_days) {
            config.bounce_max_age_days = parseInt(outboundCfg.main.bounce_max_age_days, 10) || 7;
        }
    }

    // Webhook settings
    if (cfg.webhook) {
        config.webhook.url = cfg.webhook.url || '';
        config.webhook.timeout = parseInt(cfg.webhook.timeout, 10) || 5000;

        // Parse headers
        config.webhook.headers = {};
        for (const key of Object.keys(cfg.webhook)) {
            if (key.startsWith('header_')) {
                const headerName = key.replace('header_', '').replace(/_/g, '-');
                config.webhook.headers[headerName] = cfg.webhook[key];
            }
        }
    }

    // If no webhook URL configured, try to use webhook.ini endpoints
    if (!config.webhook.url) {
        const webhookCfg = plugin.config.get('webhook.ini');
        if (webhookCfg?.main?.url) {
            config.webhook.url = webhookCfg.main.url;
            logger(`Using webhook URL from webhook.ini: ${config.webhook.url}`);
        }
    }

    logger(`Config loaded: enabled=${config.enabled}, require_hmac=${config.require_hmac}, webhook=${config.webhook.url || '(using webhook.ini)'}`);
};

/**
 * Process inbound message (bounce/FBL)
 */
exports.process_inbound = function (next, connection) {
    const plugin = this;
    const transaction = connection?.transaction;

    if (!config.enabled || !transaction) {
        return next();
    }

    // Check if this is an inbound message (marked by rcpt_to.inbound plugin)
    const inbound_type = transaction.notes.inbound_type;
    if (!inbound_type) {
        // Not an inbound message, let other queue plugins handle
        return next();
    }

    logger(`Processing inbound ${inbound_type} message`);

    // Process based on type
    switch (inbound_type) {
        case 'bounce':
            process_bounce(plugin, transaction, connection, next);
            break;
        case 'fbl':
            process_fbl(plugin, transaction, next);
            break;
        case 'admin':
            // Admin messages (postmaster, abuse) - just discard for now
            // Could be forwarded or processed differently
            logger(`Discarding admin message to ${transaction.notes.inbound_recipient}`);
            return next(OK, 'Message accepted for processing');
        default:
            logger(`Unknown inbound type: ${inbound_type}`);
            return next();
    }
};

/**
 * Verify HMAC signature for bounce address
 * Returns { valid: boolean, reason: string }
 */
function verify_bounce_hmac(timestamp, hmac, message_id) {
    // Check if HMAC secret is configured
    if (!config.hmac_secret) {
        return { valid: false, reason: 'HMAC secret not configured' };
    }

    // Check timestamp age (timestamp is in seconds, convert to ms)
    const ts = parseInt(timestamp, 10);
    if (isNaN(ts)) {
        return { valid: false, reason: 'Invalid timestamp format' };
    }

    const ts_ms = ts * 1000;  // Convert seconds to milliseconds
    const now = Date.now();
    const max_age_ms = config.bounce_max_age_days * 24 * 60 * 60 * 1000;
    const age = now - ts_ms;

    if (age > max_age_ms) {
        return { valid: false, reason: `Bounce too old: ${Math.floor(age / (24 * 60 * 60 * 1000))} days` };
    }

    if (age < -60000) { // Allow 1 minute clock skew
        return { valid: false, reason: 'Timestamp in future' };
    }

    // Verify HMAC
    const expected_hmac = crypto
        .createHmac('sha256', config.hmac_secret)
        .update(`${timestamp}:${message_id}`)
        .digest('hex')
        .substring(0, 8);

    if (hmac.toLowerCase() !== expected_hmac.toLowerCase()) {
        return { valid: false, reason: 'HMAC mismatch' };
    }

    return { valid: true, reason: 'OK' };
}

/**
 * Process bounce (DSN) message
 */
async function process_bounce(plugin, transaction, connection, next) {
    try {
        const message_id = transaction.notes.inbound_message_id;
        const recipient = transaction.notes.inbound_recipient;
        const hmac_protected = transaction.notes.inbound_hmac_protected;

        // HMAC validation
        if (config.require_hmac) {
            if (!hmac_protected) {
                // Legacy format without HMAC - reject
                logwarn(`Rejecting bounce without HMAC: ${recipient} (legacy format)`);
                return next(DENY, 'Invalid bounce address format');
            }

            const timestamp = transaction.notes.inbound_timestamp;
            const hmac = transaction.notes.inbound_hmac;

            const validation = verify_bounce_hmac(timestamp, hmac, message_id);
            if (!validation.valid) {
                logwarn(`Rejecting invalid bounce: ${recipient} - ${validation.reason}`);
                return next(DENY, `Invalid bounce: ${validation.reason}`);
            }

            logger(`HMAC validation passed for bounce: ${recipient}`);
        } else if (hmac_protected) {
            // HMAC not required but present - still validate it
            const timestamp = transaction.notes.inbound_timestamp;
            const hmac = transaction.notes.inbound_hmac;

            const validation = verify_bounce_hmac(timestamp, hmac, message_id);
            if (!validation.valid) {
                logwarn(`HMAC validation failed (not rejecting): ${recipient} - ${validation.reason}`);
            } else {
                logger(`HMAC validation passed (optional): ${recipient}`);
            }
        }

        // Get message body for DSN parsing
        const body = await get_message_body(transaction);
        const dsn_info = parse_dsn(body, transaction);

        const payload = {
            event: 'bounce_received',
            timestamp: new Date().toISOString(),
            message_id: message_id || dsn_info.original_message_id || '',
            verp_recipient: recipient,
            bounce_type: dsn_info.bounce_type || 'unknown',
            diagnostic_code: dsn_info.diagnostic_code || '',
            status: dsn_info.status || '',
            remote_mta: dsn_info.remote_mta || '',
            original_recipient: dsn_info.original_recipient || '',
            reporting_mta: dsn_info.reporting_mta || '',
            hmac_validated: hmac_protected && config.require_hmac,
            raw_dsn: dsn_info.raw || ''
        };

        logger(`Bounce received: message_id=${payload.message_id}, type=${payload.bounce_type}, status=${payload.status}`);

        // Send webhook
        await send_webhook(plugin, payload);

        // Discard message - we've processed it
        return next(OK, 'Bounce processed');

    } catch (err) {
        logerror(`Error processing bounce: ${err.message}`);
        // Still accept the message to avoid loops
        return next(OK, 'Bounce accepted with errors');
    }
}

/**
 * Process FBL (ARF) message
 */
async function process_fbl(plugin, transaction, next) {
    try {
        const recipient = transaction.notes.inbound_recipient;

        // Get message body for ARF parsing
        const body = await get_message_body(transaction);
        const arf_info = parse_arf(body, transaction);

        const payload = {
            event: 'complaint',
            timestamp: new Date().toISOString(),
            message_id: arf_info.original_message_id || '',
            fbl_recipient: recipient,
            feedback_type: arf_info.feedback_type || 'abuse',
            user_agent: arf_info.user_agent || '',
            source_ip: arf_info.source_ip || '',
            original_from: arf_info.original_from || '',
            original_to: arf_info.original_to || '',
            original_subject: arf_info.original_subject || '',
            arrival_date: arf_info.arrival_date || ''
        };

        logger(`FBL complaint received: message_id=${payload.message_id}, type=${payload.feedback_type}`);

        // Send webhook
        await send_webhook(plugin, payload);

        // Discard message - we've processed it
        return next(OK, 'Complaint processed');

    } catch (err) {
        logerror(`Error processing FBL: ${err.message}`);
        // Still accept the message to avoid loops
        return next(OK, 'FBL accepted with errors');
    }
}

/**
 * Get message body from transaction
 * Note: In queue hook, message_stream may already be consumed.
 * We try multiple approaches to get the body.
 */
function get_message_body(transaction) {
    return new Promise((resolve) => {
        // Timeout to prevent hanging
        const timeout = setTimeout(() => {
            if (logwarn) logwarn('get_message_body timeout, returning empty');
            resolve('');
        }, 5000);

        try {
            // Method 1: Try transaction.body (MessageBody object)
            if (transaction.body) {
                clearTimeout(timeout);
                // body.bodytext contains the raw message
                if (transaction.body.bodytext) {
                    return resolve(transaction.body.bodytext);
                }
                // Try toString
                const bodyStr = transaction.body.toString();
                if (bodyStr && bodyStr !== '[object Object]') {
                    return resolve(bodyStr);
                }
            }

            // Method 2: Try to read from data_lines (raw lines)
            if (transaction.data_lines && transaction.data_lines.length > 0) {
                clearTimeout(timeout);
                return resolve(transaction.data_lines.join('\n'));
            }

            // Method 3: Try message_stream with pipe (may work in some cases)
            if (transaction.message_stream && transaction.message_stream.readable) {
                let body = '';
                transaction.message_stream.on('data', (chunk) => {
                    body += chunk.toString();
                });
                transaction.message_stream.on('end', () => {
                    clearTimeout(timeout);
                    resolve(body);
                });
                transaction.message_stream.on('error', () => {
                    clearTimeout(timeout);
                    resolve('');
                });
                // Resume stream if paused
                if (transaction.message_stream.resume) {
                    transaction.message_stream.resume();
                }
            } else {
                // No body available
                clearTimeout(timeout);
                resolve('');
            }
        } catch (err) {
            clearTimeout(timeout);
            if (logwarn) logwarn(`get_message_body error: ${err.message}`);
            resolve('');
        }
    });
}

/**
 * Parse DSN (RFC 3464) bounce message
 */
function parse_dsn(body, transaction) {
    const info = {
        bounce_type: 'unknown',
        diagnostic_code: '',
        status: '',
        remote_mta: '',
        original_recipient: '',
        reporting_mta: '',
        original_message_id: '',
        raw: ''
    };

    try {
        // Try to extract from headers first
        const x_haraka_msgid = transaction.header?.get('X-Failed-Recipients') ||
            transaction.header?.get('X-Haraka-MsgID');
        if (x_haraka_msgid) {
            info.original_message_id = x_haraka_msgid.trim();
        }

        // Parse DSN structure (multipart/report)
        // Look for delivery-status part
        const status_match = body.match(/Status:\s*(\d\.\d\.\d)/i);
        if (status_match) {
            info.status = status_match[1];
            // Determine bounce type from status code
            if (info.status.startsWith('5.')) {
                info.bounce_type = 'hard';
            } else if (info.status.startsWith('4.')) {
                info.bounce_type = 'soft';
            }
        }

        // Extract diagnostic code
        const diag_match = body.match(/Diagnostic-Code:\s*(.+?)(?:\r?\n(?!\s)|\r?\n\r?\n)/is);
        if (diag_match) {
            info.diagnostic_code = diag_match[1].replace(/\s+/g, ' ').trim().substring(0, 500);
        }

        // Extract remote MTA
        const remote_mta_match = body.match(/Remote-MTA:\s*dns;\s*(.+)/i);
        if (remote_mta_match) {
            info.remote_mta = remote_mta_match[1].trim();
        }

        // Extract reporting MTA
        const reporting_mta_match = body.match(/Reporting-MTA:\s*dns;\s*(.+)/i);
        if (reporting_mta_match) {
            info.reporting_mta = reporting_mta_match[1].trim();
        }

        // Extract original recipient
        const orig_rcpt_match = body.match(/Original-Recipient:\s*rfc822;\s*(.+)/i) ||
            body.match(/Final-Recipient:\s*rfc822;\s*(.+)/i);
        if (orig_rcpt_match) {
            info.original_recipient = orig_rcpt_match[1].trim();
        }

        // Try to find original Message-ID in the attached original message
        const msgid_match = body.match(/Message-ID:\s*<([^>]+)>/i);
        if (msgid_match && !info.original_message_id) {
            info.original_message_id = msgid_match[1];
        }

        // Look for X-Haraka-MsgID in attached message
        const haraka_msgid_match = body.match(/X-Haraka-MsgID:\s*(.+)/i);
        if (haraka_msgid_match) {
            info.original_message_id = haraka_msgid_match[1].trim();
        }

        // Store truncated raw DSN for debugging
        info.raw = body.substring(0, 2000);

    } catch (err) {
        // Parsing errors are non-fatal
        if (logwarn) logwarn(`DSN parsing error: ${err.message}`);
    }

    return info;
}

/**
 * Parse ARF (RFC 5965) feedback report
 */
function parse_arf(body, transaction) {
    const info = {
        feedback_type: 'abuse',
        user_agent: '',
        source_ip: '',
        original_from: '',
        original_to: '',
        original_subject: '',
        original_message_id: '',
        arrival_date: ''
    };

    try {
        // ARF structure:
        // Part 1: Human-readable
        // Part 2: machine-readable (message/feedback-report)
        // Part 3: Original message (message/rfc822)

        // Extract feedback type
        const feedback_type_match = body.match(/Feedback-Type:\s*(.+)/i);
        if (feedback_type_match) {
            info.feedback_type = feedback_type_match[1].trim().toLowerCase();
        }

        // Extract user agent
        const ua_match = body.match(/User-Agent:\s*(.+)/i);
        if (ua_match) {
            info.user_agent = ua_match[1].trim();
        }

        // Extract source IP
        const source_ip_match = body.match(/Source-IP:\s*(.+)/i);
        if (source_ip_match) {
            info.source_ip = source_ip_match[1].trim();
        }

        // Extract arrival date
        const arrival_match = body.match(/Arrival-Date:\s*(.+)/i);
        if (arrival_match) {
            info.arrival_date = arrival_match[1].trim();
        }

        // Find original message section and extract headers
        // Look for the message/rfc822 boundary
        const rfc822_index = body.indexOf('Content-Type: message/rfc822');
        if (rfc822_index !== -1) {
            const original_section = body.substring(rfc822_index);

            // Extract From
            const from_match = original_section.match(/^From:\s*(.+)/mi);
            if (from_match) {
                info.original_from = from_match[1].trim();
            }

            // Extract To
            const to_match = original_section.match(/^To:\s*(.+)/mi);
            if (to_match) {
                info.original_to = to_match[1].trim();
            }

            // Extract Subject
            const subject_match = original_section.match(/^Subject:\s*(.+)/mi);
            if (subject_match) {
                info.original_subject = subject_match[1].trim();
            }

            // Extract Message-ID
            const msgid_match = original_section.match(/^Message-ID:\s*<([^>]+)>/mi);
            if (msgid_match) {
                info.original_message_id = msgid_match[1];
            }

            // Look for X-Haraka-MsgID
            const haraka_msgid_match = original_section.match(/^X-Haraka-MsgID:\s*(.+)/mi);
            if (haraka_msgid_match) {
                info.original_message_id = haraka_msgid_match[1].trim();
            }
        }

    } catch (err) {
        if (logwarn) logwarn(`ARF parsing error: ${err.message}`);
    }

    return info;
}

/**
 * Send webhook notification
 */
function send_webhook(plugin, payload) {
    return new Promise((resolve, reject) => {
        // First try dedicated inbound_handler URL
        let webhook_url = config.webhook.url;
        let webhook_headers = config.webhook.headers;

        // If no dedicated URL, try to send via existing webhook endpoints
        if (!webhook_url) {
            // Load webhook.ini to find endpoints
            const webhookCfg = plugin.config.get('webhook.ini');

            // Check for endpoint sections
            for (const section of Object.keys(webhookCfg || {})) {
                if (section.startsWith('endpoint.')) {
                    const endpoint = webhookCfg[section];
                    // Check if endpoint wants this event type
                    const events = (endpoint.events || '').split(',').map(e => e.trim());
                    if (events.includes(payload.event) || events.includes('*')) {
                        webhook_url = endpoint.url;
                        // Also read headers from this endpoint
                        webhook_headers = { ...webhook_headers };
                        for (const key of Object.keys(endpoint)) {
                            if (key.startsWith('header_')) {
                                const headerName = key.replace('header_', '').replace(/_/g, '-');
                                webhook_headers[headerName] = endpoint[key];
                            }
                        }
                        break;
                    }
                }
            }

            // Fallback to main.url
            if (!webhook_url && webhookCfg?.main?.url) {
                webhook_url = webhookCfg.main.url;
                // Also read headers from main section
                webhook_headers = { ...webhook_headers };
                for (const key of Object.keys(webhookCfg.main)) {
                    if (key.startsWith('header_')) {
                        const headerName = key.replace('header_', '').replace(/_/g, '-');
                        webhook_headers[headerName] = webhookCfg.main[key];
                    }
                }
                // Legacy auth_header/auth_token support
                if (webhookCfg.main.auth_header && webhookCfg.main.auth_token) {
                    webhook_headers[webhookCfg.main.auth_header] = webhookCfg.main.auth_token;
                }
            }
        }

        if (!webhook_url) {
            logger('No webhook URL configured, skipping notification');
            return resolve();
        }

        const parsed = url.parse(webhook_url);
        const data = JSON.stringify(payload);

        const options = {
            hostname: parsed.hostname,
            port: parsed.port || (parsed.protocol === 'https:' ? 443 : 80),
            path: parsed.path,
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(data),
                'User-Agent': 'Haraka-Inbound-Handler/1.0',
                ...webhook_headers
            },
            timeout: config.webhook.timeout
        };

        const transport = parsed.protocol === 'https:' ? https : http;

        const req = transport.request(options, (res) => {
            let body = '';
            res.on('data', chunk => body += chunk);
            res.on('end', () => {
                if (res.statusCode >= 200 && res.statusCode < 300) {
                    logger(`Webhook sent: ${payload.event} (${res.statusCode})`);
                    resolve();
                } else {
                    logerror(`Webhook failed: ${res.statusCode} - ${body}`);
                    // Don't reject - we still want to accept the message
                    resolve();
                }
            });
        });

        req.on('error', (err) => {
            logerror(`Webhook error: ${err.message}`);
            // Don't reject - we still want to accept the message
            resolve();
        });

        req.on('timeout', () => {
            req.destroy();
            logerror('Webhook timeout');
            resolve();
        });

        req.write(data);
        req.end();
    });
}
