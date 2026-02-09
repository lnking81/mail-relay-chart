'use strict';

/**
 * Sender Validation Plugin for Haraka
 *
 * Validates MAIL FROM address for OUTBOUND (relaying) connections only.
 * Inbound mail (bounces, FBL) is NOT checked - anyone can send to us.
 *
 * Features:
 * - Whitelist: allowed domains/addresses for sending
 * - Blacklist: forbidden addresses (higher priority than whitelist)
 * - From header check: ensure From header matches MAIL FROM domain
 */

exports.register = function () {
    this.load_config();

    this.register_hook('mail', 'check_mail_from');

    // Check From header after DATA
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

    // Load whitelist (domains that can send)
    this.cfg.whitelist = this.config.get('sender_validation.whitelist', 'list');
    this.cfg.whitelist_regex = this.config.get('sender_validation.whitelist_regex', 'list');

    // Load blacklist (forbidden senders - checked first)
    this.cfg.blacklist = this.config.get('sender_validation.blacklist', 'list');
    this.cfg.blacklist_regex = this.config.get('sender_validation.blacklist_regex', 'list');

    // Compile regex patterns
    this.whitelist_re = this.compile_regex(this.cfg.whitelist_regex);
    this.blacklist_re = this.compile_regex(this.cfg.blacklist_regex);
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

exports.check_mail_from = function (next, connection, params) {
    if (!this.cfg.main.enabled) {
        return next();
    }

    // IMPORTANT: Only check OUTBOUND (relaying) connections!
    // Inbound mail (bounces, external senders) should NOT be filtered by sender
    if (!connection.relaying) {
        connection.logdebug(this, 'Skipping sender validation for inbound connection');
        return next();
    }

    const mail_from = params[0];
    if (!mail_from || !mail_from.address()) {
        // Null sender (bounces) - allow
        connection.logdebug(this, 'Allowing null sender');
        return next();
    }

    const address = mail_from.address().toLowerCase();
    const domain = mail_from.host ? mail_from.host.toLowerCase() : '';

    connection.logdebug(this, `Checking sender: ${address} (domain: ${domain})`);

    // Check blacklist first (higher priority)
    if (this.is_blacklisted(address, domain)) {
        connection.logwarn(this, `Sender blacklisted: ${address}`);
        connection.transaction.results.add(this, { fail: 'blacklisted', sender: address });
        return next(DENY, `Sender address ${address} is not allowed`);
    }

    // Check whitelist
    if (this.is_whitelisted(address, domain)) {
        connection.loginfo(this, `Sender whitelisted: ${address}`);
        connection.transaction.results.add(this, { pass: 'whitelisted', sender: address });
        return next();
    }

    // Not in whitelist - deny
    connection.logwarn(this, `Sender not in whitelist: ${address}`);
    connection.transaction.results.add(this, { fail: 'not_whitelisted', sender: address });
    return next(DENY, `Sender address ${address} is not allowed to relay`);
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

    // Only check outbound
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
