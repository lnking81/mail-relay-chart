'use strict';

/**
 * Haraka Outbound Headers Plugin
 *
 * Adds tracking headers and VERP Return-Path for bounce/FBL correlation:
 * - Reads client-provided message ID from configurable header (default: X-Message-ID)
 * - Sets Return-Path to bounce+{ts}-{hmac}-{message_id}@{bounce_domain} for VERP
 * - Adds X-Haraka-MsgID header for FBL report parsing
 * - Optionally adds Feedback-ID for Gmail FBL
 * - HMAC validation prevents spam bounce attacks
 *
 * Configuration (outbound_headers.ini):
 * [main]
 * enabled=true
 * client_id_header=X-Message-ID
 * bounce_domain=mail.example.com
 * bounce_prefix=bounce
 * add_feedback_id=true
 * feedback_id_tag=mail-relay
 * hmac_secret=your-secret-key  ; Required for bounce validation
 * bounce_max_age_days=7        ; Reject bounces older than this
 */

const crypto = require('crypto');

let config = {
    enabled: true,
    client_id_header: 'X-Message-ID',
    bounce_domain: '',           // Fallback domain
    bounce_domains: new Set(),   // All valid bounce domains (from mail.domains)
    bounce_prefix: 'bounce',
    add_feedback_id: true,
    feedback_id_tag: 'mail-relay',
    fallback_to_queue_id: true,
    hmac_secret: '',
    bounce_max_age_days: 7,
    use_sender_domain: true      // Use sender's domain for bounce if in allowed list
};

// Auto-generated secret if not configured (persists for process lifetime)
let auto_secret = null;

let logger;

/**
 * Plugin registration
 */
exports.register = function () {
    const plugin = this;
    logger = plugin.loginfo.bind(plugin);

    plugin.load_config();

    // Hook into data phase to modify headers before queuing
    plugin.register_hook('data_post', 'add_tracking_headers');

    logger('Outbound headers plugin registered');
};

/**
 * Load configuration
 */
exports.load_config = function () {
    const plugin = this;

    const cfg = plugin.config.get('outbound_headers.ini', {
        booleans: [
            '+main.enabled',
            '+main.add_feedback_id',
            '+main.fallback_to_queue_id',
            '+main.use_sender_domain'
        ]
    }, () => {
        plugin.load_config();
    });

    if (cfg.main) {
        config.enabled = cfg.main.enabled !== false;
        config.client_id_header = cfg.main.client_id_header || 'X-Message-ID';
        config.bounce_domain = cfg.main.bounce_domain || '';
        config.bounce_prefix = cfg.main.bounce_prefix || 'bounce';
        config.add_feedback_id = cfg.main.add_feedback_id !== false;
        config.feedback_id_tag = cfg.main.feedback_id_tag || 'mail-relay';
        config.fallback_to_queue_id = cfg.main.fallback_to_queue_id !== false;
        config.hmac_secret = cfg.main.hmac_secret || '';
        config.bounce_max_age_days = parseInt(cfg.main.bounce_max_age_days, 10) || 7;
        config.use_sender_domain = cfg.main.use_sender_domain !== false;
    }

    // Load allowed bounce domains
    config.bounce_domains = new Set();
    if (cfg.domains) {
        for (const domain of Object.keys(cfg.domains)) {
            if (cfg.domains[domain]) {
                config.bounce_domains.add(domain.toLowerCase());
            }
        }
    }
    // Always add the main bounce_domain
    if (config.bounce_domain) {
        config.bounce_domains.add(config.bounce_domain.toLowerCase());
    }

    // Generate auto secret if not configured
    if (!config.hmac_secret && !auto_secret) {
        auto_secret = crypto.randomBytes(32).toString('hex');
        logger('Generated auto HMAC secret (will change on restart)');
    }

    logger(`Config loaded: client_id_header=${config.client_id_header}, bounce_domain=${config.bounce_domain}, domains=[${[...config.bounce_domains].join(',')}], hmac=${config.hmac_secret ? 'configured' : 'auto'}`);
};

/**
 * Get HMAC secret (configured or auto-generated)
 */
function get_hmac_secret() {
    return config.hmac_secret || auto_secret;
}

/**
 * Generate HMAC for bounce validation
 * @param {number} timestamp - Unix timestamp in seconds
 * @param {string} message_id - Message ID
 * @returns {string} First 8 chars of HMAC
 */
function generate_bounce_hmac(timestamp, message_id) {
    const secret = get_hmac_secret();
    const data = `${timestamp}:${message_id}`;
    const hmac = crypto.createHmac('sha256', secret).update(data).digest('hex');
    return hmac.substring(0, 8); // 8 chars = 32 bits of entropy
}

/**
 * Verify HMAC for incoming bounce
 * @param {number} timestamp - Timestamp from VERP address
 * @param {string} hmac - HMAC from VERP address
 * @param {string} message_id - Message ID from VERP address
 * @returns {{valid: boolean, reason: string}}
 */
function verify_bounce_hmac(timestamp, hmac, message_id) {
    const secret = get_hmac_secret();

    // Check timestamp age
    const now = Math.floor(Date.now() / 1000);
    const max_age = config.bounce_max_age_days * 24 * 60 * 60;

    if (timestamp < now - max_age) {
        return { valid: false, reason: 'bounce too old' };
    }

    if (timestamp > now + 3600) { // 1 hour future tolerance
        return { valid: false, reason: 'timestamp in future' };
    }

    // Verify HMAC
    const expected_hmac = generate_bounce_hmac(timestamp, message_id);
    if (hmac !== expected_hmac) {
        return { valid: false, reason: 'invalid hmac' };
    }

    return { valid: true, reason: '' };
}

// Export for use by other plugins
exports.verify_bounce_hmac = verify_bounce_hmac;
exports.get_hmac_secret = get_hmac_secret;

/**
 * Add tracking headers and VERP Return-Path
 */
exports.add_tracking_headers = function (next, connection) {
    const plugin = this;
    const transaction = connection?.transaction;

    if (!config.enabled || !transaction) {
        return next();
    }

    try {
        // Get message ID from client header or fall back to queue_id
        let message_id = transaction.header.get(config.client_id_header);

        if (message_id) {
            // Clean up header value (remove newlines, trim)
            message_id = message_id.replace(/[\r\n]/g, '').trim();
        }

        if (!message_id && config.fallback_to_queue_id) {
            message_id = transaction.uuid;
        }

        if (!message_id) {
            plugin.logwarn(`No ${config.client_id_header} header and no queue_id fallback`);
            return next();
        }

        // Sanitize message_id for use in email address (remove special chars)
        const safe_message_id = sanitize_for_email(message_id);

        // Store original and safe ID in transaction notes for other plugins
        transaction.notes.client_message_id = message_id;
        transaction.notes.safe_message_id = safe_message_id;

        // Add X-Haraka-MsgID header for FBL report parsing
        // Remove existing header first to avoid duplicates
        transaction.remove_header('X-Haraka-MsgID');
        transaction.add_header('X-Haraka-MsgID', message_id);

        // Set VERP Return-Path if bounce domain is available
        // Format: bounce+{timestamp}-{hmac}-{message_id}@domain
        // Use sender's domain if in allowed list, otherwise fallback to bounce_domain
        const sender_domain = get_from_domain(transaction);
        let bounce_domain = config.bounce_domain;

        if (config.use_sender_domain && sender_domain) {
            const sender_domain_lower = sender_domain.toLowerCase();
            if (config.bounce_domains.has(sender_domain_lower)) {
                bounce_domain = sender_domain_lower;
                logger(`Using sender domain for bounce: ${bounce_domain}`);
            }
        }

        if (bounce_domain) {
            const timestamp = Math.floor(Date.now() / 1000);
            const hmac = generate_bounce_hmac(timestamp, safe_message_id);
            const verp_local = `${config.bounce_prefix}+${timestamp}-${hmac}-${safe_message_id}`;
            const verp_address = `${verp_local}@${bounce_domain}`;

            // Store VERP address and components in notes
            transaction.notes.verp_return_path = verp_address;
            transaction.notes.verp_timestamp = timestamp;
            transaction.notes.verp_hmac = hmac;
            transaction.notes.verp_domain = bounce_domain;

            // Store original mail_from for reference
            const original_mail_from = transaction.mail_from ? transaction.mail_from.original : '';
            transaction.notes.original_mail_from = original_mail_from;

            // CRITICAL: Actually change the envelope MAIL FROM to VERP address
            // This ensures bounces come back to bounce+...@domain instead of original sender
            // Haraka's Address class parses the address into user/host components
            try {
                const Address = require('address-rfc2821').Address;
                transaction.mail_from = new Address(`<${verp_address}>`);
                logger(`VERP envelope MAIL FROM changed: ${original_mail_from} → ${verp_address}`);
            } catch (err) {
                // Fallback: modify existing mail_from object directly
                if (transaction.mail_from) {
                    transaction.mail_from.original = verp_address;
                    transaction.mail_from.user = verp_local;
                    transaction.mail_from.host = bounce_domain;
                    logger(`VERP envelope MAIL FROM modified: ${original_mail_from} → ${verp_address}`);
                } else {
                    plugin.logwarn(`Cannot set VERP: no mail_from object`);
                }
            }

            // Also add header for debugging/tracking
            transaction.remove_header('X-Haraka-Return-Path');
            transaction.add_header('X-Haraka-Return-Path', verp_address);

            logger(`VERP Return-Path: ${verp_address} (msg_id: ${message_id}, sender: ${sender_domain || 'n/a'})`);
        }

        // Add Feedback-ID for Gmail FBL (format: a]:[b]:[c]:[d])
        // Gmail FBL requires this header to identify senders
        // Format: CampaignID:CustomerID:MailType:SenderID
        if (config.add_feedback_id) {
            const from_domain = get_from_domain(transaction);
            const feedback_id = [
                safe_message_id.substring(0, 50),  // Campaign/Message ID (truncated)
                from_domain || 'unknown',           // Customer/Domain
                config.feedback_id_tag,             // Mail type tag
                config.bounce_domain || from_domain || 'haraka'  // Sender ID
            ].join(':');

            transaction.remove_header('Feedback-ID');
            transaction.add_header('Feedback-ID', feedback_id);

            logger(`Feedback-ID: ${feedback_id}`);
        }

        // Log for debugging
        plugin.logdebug(`Added tracking headers: msg_id=${message_id}, verp=${transaction.notes.verp_return_path || 'none'}`);

    } catch (err) {
        plugin.logerror(`Error adding tracking headers: ${err.message}`);
    }

    next();
};

/**
 * Sanitize string for use in email local part
 * Keeps: alphanumeric, dash, underscore, dot
 */
function sanitize_for_email(str) {
    if (!str) return '';

    return str
        .replace(/[^a-zA-Z0-9\-_.]/g, '_')  // Replace special chars with underscore
        .replace(/_+/g, '_')                 // Collapse multiple underscores
        .replace(/^_|_$/g, '')               // Remove leading/trailing underscores
        .substring(0, 64);                   // Limit length
}

/**
 * Extract domain from From header
 */
function get_from_domain(transaction) {
    try {
        const from = transaction.mail_from;
        if (from && from.host) {
            return from.host;
        }

        // Fallback to parsing From header
        const from_header = transaction.header.get('From');
        if (from_header) {
            const match = from_header.match(/@([^>\s]+)/);
            if (match) return match[1];
        }
    } catch (err) {
        // Ignore errors
    }
    return null;
}

/**
 * Hook for outbound.send_email - modify Return-Path
 * This hook is called when the message is being sent out
 */
exports.hook_get_mx = function (next, hmail, domain) {
    // This hook allows us to see the hmail object
    // VERP is set via transaction notes, outbound should honor it
    next();
};
