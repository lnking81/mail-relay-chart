'use strict';

/**
 * Sender Validation Plugin for Haraka
 *
 * Validates MAIL FROM address for OUTBOUND relay only.
 * INBOUND mail (to our addresses like dmarc@, postmaster@, bounce+*) is NOT checked.
 *
 * The key insight: we can only determine if mail is inbound or outbound
 * at RCPT TO time, not at MAIL FROM time!
 *
 * Hook order:
 * 1. mail     - Just check blacklist (block known bad senders immediately)
 * 2. rcpt     - Check whitelist only if recipient is EXTERNAL (outbound relay)
 *               If recipient is our inbound address - anyone can send
 * 3. data_post - Check From header matches MAIL FROM (outbound only)
 *
 * Features:
 * - Whitelist: allowed domains/addresses for outbound sending
 * - Blacklist: forbidden addresses (always blocked, even for inbound)
 * - From header check: ensure From header matches MAIL FROM domain
 */

exports.register = function () {
    this.load_config();

    // Blacklist check on mail - block known bad senders immediately
    this.register_hook('mail', 'check_blacklist');

    // Whitelist check on rcpt - only for outbound (non-inbound recipients)
    this.register_hook('rcpt', 'check_whitelist_on_rcpt');

    // Check From header after DATA (outbound only)
    if (this.cfg.main.check_from_header) {
        this.register_hook('data_post', 'check_from_header');
    }
};

exports.load_config = function () {
    this.cfg = this.config.get('sender_validation.ini', {
        booleans: ['+main.enabled', '+main.check_from_header'],
    });

    // Defaults
    this.cfg.main = this.cfg.main || {};
    this.cfg.main.enabled = this.cfg.main.enabled !== false;
    this.cfg.main.check_from_header = this.cfg.main.check_from_header !== false;

    // Load whitelist (domains that can send outbound)
    this.cfg.whitelist = this.config.get('sender_validation.whitelist', 'list');
    this.cfg.whitelist_regex = this.config.get('sender_validation.whitelist_regex', 'list');

    // Load blacklist (forbidden senders - checked first)
    this.cfg.blacklist = this.config.get('sender_validation.blacklist', 'list');
    this.cfg.blacklist_regex = this.config.get('sender_validation.blacklist_regex', 'list');

    // Load inbound config to know our domains
    this.load_inbound_config();

    // Compile regex patterns
    this.whitelist_re = this.compile_regex(this.cfg.whitelist_regex);
    this.blacklist_re = this.compile_regex(this.cfg.blacklist_regex);
};

exports.load_inbound_config = function () {
    // Load inbound domains from rcpt_to.inbound.ini
    const inbound_cfg = this.config.get('rcpt_to.inbound.ini', {
        booleans: ['+main.enabled'],
    });

    this.inbound_enabled = inbound_cfg.main && inbound_cfg.main.enabled !== false;

    // Get bounce prefix from [main] section (same as rcpt-to-inbound plugin)
    this.bounce_prefix = (inbound_cfg.main && inbound_cfg.main.bounce_prefix) || 'bounce';

    // Our inbound domains
    this.inbound_domains = new Set();
    if (inbound_cfg.domains) {
        for (const [domain, enabled] of Object.entries(inbound_cfg.domains)) {
            if (enabled === 'true' || enabled === true) {
                this.inbound_domains.add(domain.toLowerCase());
            }
        }
    }

    // Inbound recipients (postmaster, dmarc, etc.) - exact matches only
    this.inbound_recipients = new Set();
    // Inbound prefixes - always include bounce prefix from [main]
    this.inbound_prefixes = [`${this.bounce_prefix}+`];

    if (inbound_cfg.recipients) {
        for (const [rcpt, enabled] of Object.entries(inbound_cfg.recipients)) {
            if (enabled === 'true' || enabled === true) {
                if (rcpt.endsWith('+')) {
                    // Prefix pattern like "bounce+"
                    this.inbound_prefixes.push(rcpt.toLowerCase());
                } else {
                    this.inbound_recipients.add(rcpt.toLowerCase());
                }
            }
        }
    }

    this.loginfo(`Inbound domains: ${[...this.inbound_domains].join(', ') || 'none'}`);
    this.loginfo(`Inbound recipients: ${[...this.inbound_recipients].join(', ') || 'none'}`);
    this.loginfo(`Inbound prefixes: ${this.inbound_prefixes.join(', ') || 'none'}`);
};

exports.compile_regex = function (patterns) {
    if (!patterns || patterns.length === 0) return [];

    const compiled = [];
    for (const pattern of patterns) {
        try {
            compiled.push(new RegExp(pattern, 'i'));
        } catch (e) {
            this.logerror(`Invalid regex pattern: ${pattern}`);
        }
    }
    return compiled;
};

/**
 * Check blacklist on MAIL FROM - block known bad senders immediately
 */
exports.check_blacklist = function (next, connection, params) {
    if (!this.cfg.main.enabled) {
        return next();
    }

    const mail_from = params[0];
    if (!mail_from || !mail_from.address()) {
        // Null sender (bounces) - allow
        return next();
    }

    const address = mail_from.address().toLowerCase();
    const domain = mail_from.host ? mail_from.host.toLowerCase() : '';

    // Check blacklist - always block, even for inbound
    if (this.is_blacklisted(address, domain)) {
        connection.logwarn(this, `Sender blacklisted: ${address}`);
        connection.transaction.results.add(this, { fail: 'blacklisted', sender: address });
        return next(DENY, `Sender address ${address} is not allowed`);
    }

    // Store for later whitelist check
    connection.transaction.notes.sender_address = address;
    connection.transaction.notes.sender_domain = domain;

    return next();
};

/**
 * Check whitelist on RCPT TO - but only if recipient is EXTERNAL (outbound)
 * If recipient is our inbound address - anyone can send to us
 */
exports.check_whitelist_on_rcpt = function (next, connection, params) {
    if (!this.cfg.main.enabled) {
        return next();
    }

    // Only check for relaying connections (from trusted networks)
    if (!connection.relaying) {
        connection.logdebug(this, 'Not relaying, skipping whitelist check');
        return next();
    }

    const rcpt = params[0];
    if (!rcpt || !rcpt.address()) {
        return next();
    }

    const rcpt_address = rcpt.address().toLowerCase();
    const rcpt_domain = rcpt.host ? rcpt.host.toLowerCase() : '';
    const rcpt_user = rcpt.user ? rcpt.user.toLowerCase() : '';

    // Check if this is an INBOUND recipient (our address)
    if (this.is_inbound_recipient(rcpt_user, rcpt_domain)) {
        connection.loginfo(this, `Inbound recipient ${rcpt_address} - skipping sender whitelist`);
        connection.transaction.notes.is_inbound = true;
        return next();
    }

    // OUTBOUND: check sender whitelist
    const sender_address = connection.transaction.notes.sender_address;
    const sender_domain = connection.transaction.notes.sender_domain;

    if (!sender_address) {
        // Null sender - allow (bounces)
        return next();
    }

    // =========================================================================
    // LOOP DETECTION (FIRST!): Block VERP addresses from being relayed to EXTERNAL
    // If sender is bounce+...@domain and recipient is NOT inbound,
    // this is likely a mail loop or misconfiguration
    // This check MUST happen BEFORE whitelist check!
    // =========================================================================
    if (sender_address.match(/^bounce\+/i)) {
        connection.logwarn(this, `LOOP BLOCKED: Rejecting relay from VERP sender ${sender_address} to external ${rcpt_address}`);
        connection.transaction.results.add(this, {
            fail: 'loop_detected',
            sender: sender_address,
            recipient: rcpt_address,
        });
        return next(DENY, `Mail loop detected: bounce messages cannot be relayed to external addresses`);
    }

    // Check whitelist (only for non-VERP senders)
    if (this.is_whitelisted(sender_address, sender_domain)) {
        connection.loginfo(this, `Sender ${sender_address} whitelisted for outbound to ${rcpt_address}`);
        return next();
    }

    // Not in whitelist - deny outbound relay
    connection.logwarn(this, `Sender ${sender_address} not whitelisted for outbound relay`);
    connection.transaction.results.add(this, {
        fail: 'not_whitelisted',
        sender: sender_address,
        recipient: rcpt_address,
    });
    return next(DENY, `Sender address ${sender_address} is not allowed to relay`);
};

/**
 * Check if recipient is one of our inbound addresses
 */
exports.is_inbound_recipient = function (user, domain) {
    // Check if inbound is enabled and domain is ours
    if (!this.inbound_enabled) {
        return false;
    }

    if (!this.inbound_domains.has(domain)) {
        return false;
    }

    // Check exact recipient match
    if (this.inbound_recipients.has(user)) {
        return true;
    }

    // Check prefix patterns (e.g., "bounce+" matches "bounce+abc")
    for (const prefix of this.inbound_prefixes) {
        if (user.startsWith(prefix)) {
            return true;
        }
    }

    return false;
};

exports.is_blacklisted = function (address, domain) {
    // Check exact address match
    if (this.cfg.blacklist.includes(address)) {
        return true;
    }

    // Check domain match
    if (domain && this.cfg.blacklist.includes(domain)) {
        return true;
    }

    // Check regex patterns
    for (const re of this.blacklist_re) {
        if (re.test(address)) {
            return true;
        }
    }

    return false;
};

exports.is_whitelisted = function (address, domain) {
    // Check exact address match
    if (this.cfg.whitelist.includes(address)) {
        return true;
    }

    // Check domain match
    if (domain && this.cfg.whitelist.includes(domain)) {
        return true;
    }

    // Check regex patterns
    for (const re of this.whitelist_re) {
        if (re.test(address)) {
            return true;
        }
    }

    return false;
};

exports.check_from_header = function (next, connection) {
    if (!this.cfg.main.enabled || !this.cfg.main.check_from_header) {
        return next();
    }

    // Skip for inbound mail
    if (connection.transaction.notes.is_inbound) {
        return next();
    }

    // Only check outbound (relaying)
    if (!connection.relaying) {
        return next();
    }

    const txn = connection.transaction;
    if (!txn) return next();

    const mail_from = txn.mail_from;
    if (!mail_from || !mail_from.host) {
        // Null sender
        return next();
    }

    const from_header = txn.header.get_decoded('From');
    if (!from_header) {
        return next();
    }

    // Extract domain from From header
    const from_domain = this.extract_domain(from_header);
    const mfrom_domain = mail_from.host.toLowerCase();

    if (!from_domain) {
        connection.logwarn(this, `Cannot extract domain from From header: ${from_header}`);
        return next();
    }

    // Check if From header domain matches MAIL FROM domain
    if (from_domain !== mfrom_domain) {
        connection.logwarn(
            this,
            `From header domain (${from_domain}) doesn't match MAIL FROM (${mfrom_domain})`
        );
        connection.transaction.results.add(this, {
            fail: 'from_mismatch',
            from_header: from_domain,
            mail_from: mfrom_domain,
        });
        return next(DENY, `From header domain must match sender domain`);
    }

    connection.logdebug(this, `From header domain matches MAIL FROM: ${from_domain}`);
    return next();
};

exports.extract_domain = function (from_header) {
    // Handle "Name <email@domain>" or just "email@domain"
    const match = from_header.match(/<([^>]+)>/) || from_header.match(/([^\s<>]+@[^\s<>]+)/);
    if (!match) return null;

    const email = match[1];
    const at_pos = email.lastIndexOf('@');
    if (at_pos === -1) return null;

    return email.substring(at_pos + 1).toLowerCase();
};
