'use strict';

/**
 * Haraka rcpt_to.inbound Plugin
 *
 * Controls inbound mail acceptance for our domains:
 *
 * When enabled=true (inbound mode):
 * - Accepts bounce+{timestamp}-{hmac}-{message_id}@domain - VERP bounce addresses
 * - Accepts fbl@domain - Feedback loop reports
 * - Accepts postmaster@domain, abuse@domain - RFC-required addresses
 * - Rejects other addresses on our domains
 *
 * When enabled=false (relay-only mode):
 * - REJECTS ALL mail to our domains (we only relay outbound)
 * - Ensures trusted networks can't send to our domains
 *
 * Note: OK, DENY, and CONT are injected as globals by Haraka's plugin loader
 *
 * Configuration (rcpt_to.inbound.ini):
 * [main]
 * enabled=true          ; false = reject all mail to our domains (relay-only)
 * bounce_prefix=bounce
 *
 * [domains]
 * ; List of domains we control (reject or accept inbound for)
 * example.com=true
 * mail.example.com=true
 *
 * [recipients]
 * ; Additional static recipients to accept (only when enabled=true)
 * postmaster=true
 * abuse=true
 * fbl=true
 */

let config = {
    enabled: true,
    bounce_prefix: 'bounce',
    domains: new Set(),
    recipients: new Set(['postmaster', 'abuse', 'fbl'])
};

let logger;

/**
 * Plugin registration
 */
exports.register = function () {
    const plugin = this;
    logger = plugin.loginfo.bind(plugin);

    plugin.load_config();

    // Register rcpt hook to accept/reject recipients
    plugin.register_hook('rcpt', 'check_recipient');

    logger('rcpt_to.inbound plugin registered');
};

/**
 * Load configuration
 */
exports.load_config = function () {
    const plugin = this;

    const cfg = plugin.config.get('rcpt_to.inbound.ini', {
        booleans: ['+main.enabled']
    }, () => {
        plugin.load_config();
    });

    if (cfg.main) {
        config.enabled = cfg.main.enabled !== false;
        config.bounce_prefix = cfg.main.bounce_prefix || 'bounce';
    }

    // Load domains
    config.domains = new Set();
    if (cfg.domains) {
        for (const domain of Object.keys(cfg.domains)) {
            if (cfg.domains[domain]) {
                config.domains.add(domain.toLowerCase());
            }
        }
    }

    // Load additional recipients
    config.recipients = new Set(['postmaster', 'abuse', 'fbl', 'dmarc']); // Always include RFC-required + dmarc
    if (cfg.recipients) {
        for (const rcpt of Object.keys(cfg.recipients)) {
            if (cfg.recipients[rcpt]) {
                config.recipients.add(rcpt.toLowerCase());
            }
        }
    }

    logger(`Config loaded: enabled=${config.enabled}, domains=${[...config.domains].join(',') || '(from haraka)'}, bounce_prefix=${config.bounce_prefix}`);
};

/**
 * Check if recipient should be accepted for inbound processing
 *
 * When enabled=true (inbound mode):
 *   - Accept recognized addresses (bounce+, postmaster, abuse, fbl)
 *   - REJECT unknown addresses on our domains
 *
 * When enabled=false (relay-only mode):
 *   - REJECT ALL mail to our domains
 */
exports.check_recipient = function (next, connection, params) {
    const plugin = this;
    const transaction = connection?.transaction;
    const rcpt = params[0];

    if (!rcpt) {
        return next();
    }

    const rcpt_address = rcpt.address().toLowerCase();
    const rcpt_user = rcpt.user.toLowerCase();
    const rcpt_host = rcpt.host.toLowerCase();

    // Check if domain is one of ours
    if (!is_our_domain(rcpt_host, plugin)) {
        // Not our domain, let relay plugin handle it
        return next();
    }

    // =========================================================================
    // RELAY-ONLY MODE (inbound disabled)
    // Reject ALL mail to our domains - we only relay outbound
    // =========================================================================
    if (!config.enabled) {
        logger(`Rejecting mail to our domain (relay-only mode): ${rcpt_address}`);
        return next(DENY, `We do not accept mail for <${rcpt_address}>`);
    }

    // =========================================================================
    // INBOUND MODE (enabled)
    // Accept recognized addresses, reject everything else on our domains
    // =========================================================================

    // Check for VERP bounce address
    // Format: bounce+{timestamp}-{hmac}-{message_id}@domain (HMAC-protected)
    // Legacy: bounce+{message_id}@domain (no HMAC)
    const verp_regex = new RegExp(`^${config.bounce_prefix}\\+(.+)$`, 'i');
    const verp_match = rcpt_user.match(verp_regex);

    if (verp_match) {
        const verp_data = verp_match[1];

        // Store in transaction notes for inbound-handler
        if (transaction) {
            transaction.notes.inbound_type = 'bounce';
            transaction.notes.inbound_recipient = rcpt_address;

            // Try to parse new format: {timestamp}-{hmac}-{message_id}
            // HMAC is 8 hex chars, timestamp is numeric
            const hmac_match = verp_data.match(/^(\d+)-([a-f0-9]{8})-(.+)$/i);

            if (hmac_match) {
                // New HMAC-protected format
                transaction.notes.inbound_timestamp = hmac_match[1];
                transaction.notes.inbound_hmac = hmac_match[2].toLowerCase();
                transaction.notes.inbound_message_id = hmac_match[3];
                transaction.notes.inbound_hmac_protected = true;

                logger(`Accepting VERP bounce (HMAC): ${rcpt_address} (ts: ${hmac_match[1]}, message_id: ${hmac_match[3]})`);
            } else {
                // Legacy format without HMAC
                transaction.notes.inbound_message_id = verp_data;
                transaction.notes.inbound_hmac_protected = false;

                logger(`Accepting VERP bounce (legacy): ${rcpt_address} (message_id: ${verp_data})`);
            }
        }

        return next(OK);
    }

    // Check for FBL address
    if (rcpt_user === 'fbl') {
        if (transaction) {
            transaction.notes.inbound_type = 'fbl';
            transaction.notes.inbound_recipient = rcpt_address;
        }

        logger(`Accepting FBL: ${rcpt_address}`);
        return next(OK);
    }

    // Check for DMARC aggregate reports address
    if (rcpt_user === 'dmarc' || rcpt_user === 'dmarc-reports' || rcpt_user === '_dmarc') {
        if (transaction) {
            transaction.notes.inbound_type = 'dmarc';
            transaction.notes.inbound_recipient = rcpt_address;
        }

        logger(`Accepting DMARC report: ${rcpt_address}`);
        return next(OK);
    }

    // Check for static recipients (postmaster, abuse, etc.)
    if (config.recipients.has(rcpt_user)) {
        if (transaction) {
            transaction.notes.inbound_type = 'admin';
            transaction.notes.inbound_recipient = rcpt_address;
        }

        logger(`Accepting admin address: ${rcpt_address}`);
        return next(OK);
    }

    // Unknown address on our domain - reject it
    logger(`Rejecting unknown address on our domain: ${rcpt_address}`);
    return next(DENY, `I cannot deliver mail for <${rcpt_address}>`);
};

/**
 * Check if domain is configured for inbound processing
 */
function is_our_domain(domain, plugin) {
    // If specific domains are configured, check against them
    if (config.domains.size > 0) {
        return config.domains.has(domain);
    }

    // Otherwise, try to use Haraka's hostname
    try {
        const me = plugin.config.get('me');
        if (me && me.toLowerCase() === domain) {
            return true;
        }

        // Also check host_list if available
        const host_list = plugin.config.get('host_list', 'list');
        if (host_list && host_list.includes(domain)) {
            return true;
        }
    } catch (err) {
        // Ignore config errors
    }

    return false;
}
