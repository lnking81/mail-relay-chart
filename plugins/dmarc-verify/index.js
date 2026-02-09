'use strict';

/**
 * DMARC Verification Plugin for Haraka
 *
 * Verifies incoming mail against sender's DMARC policy by checking:
 * 1. SPF result + alignment (MAIL FROM domain matches From header domain)
 * 2. DKIM result + alignment (DKIM d= domain matches From header domain)
 *
 * If either SPF or DKIM passes with alignment, DMARC passes.
 * Otherwise, applies the sender's DMARC policy (none/quarantine/reject).
 */

const dns = require('dns').promises;

exports.register = function () {
  this.load_config();

  // Run after SPF and DKIM plugins have processed
  this.register_hook('data_post', 'check_dmarc');
};

exports.load_config = function () {
  this.cfg = this.config.get('dmarc_verify.ini', {
    booleans: [
      '+main.enabled',
      '-main.reject_on_fail',
      '-main.quarantine_on_fail',
    ],
  });

  // Defaults
  this.cfg.main = this.cfg.main || {};
  this.cfg.main.enabled = this.cfg.main.enabled !== false;
  this.cfg.main.reject_on_fail = this.cfg.main.reject_on_fail || false;
  this.cfg.main.quarantine_on_fail = this.cfg.main.quarantine_on_fail || false;
};

exports.check_dmarc = async function (next, connection) {
  if (!this.cfg.main.enabled) {
    return next();
  }

  const txn = connection.transaction;
  if (!txn) {
    return next();
  }

  // Skip for relaying (outbound) connections
  if (connection.relaying) {
    connection.logdebug(this, 'Skipping DMARC check for relaying connection');
    return next();
  }

  // Get From header
  const from_header = txn.header.get_decoded('From');
  if (!from_header) {
    connection.logwarn(this, 'No From header found');
    return next();
  }

  // Extract domain from From header
  const from_domain = this.extract_domain(from_header);
  if (!from_domain) {
    connection.logwarn(this, `Cannot extract domain from From: ${from_header}`);
    return next();
  }

  connection.logdebug(this, `From domain: ${from_domain}`);

  // Get DMARC policy from DNS
  const dmarc_record = await this.get_dmarc_record(from_domain, connection);
  if (!dmarc_record) {
    connection.loginfo(this, `No DMARC record for ${from_domain}`);
    txn.results.add(this, { msg: 'no_record', domain: from_domain });
    return next();
  }

  connection.logdebug(this, `DMARC record: ${dmarc_record}`);

  // Parse DMARC policy
  const policy = this.parse_dmarc(dmarc_record);
  connection.logdebug(this, `DMARC policy: ${JSON.stringify(policy)}`);

  // Check SPF alignment
  const spf_result = this.get_spf_result(connection);
  const spf_aligned = this.check_spf_alignment(connection, from_domain, policy);
  const spf_pass = spf_result === 'pass' && spf_aligned;

  // Check DKIM alignment
  const dkim_result = this.get_dkim_result(txn);
  const dkim_aligned = this.check_dkim_alignment(txn, from_domain, policy);
  const dkim_pass = dkim_result === 'pass' && dkim_aligned;

  connection.loginfo(
    this,
    `SPF: ${spf_result} (aligned: ${spf_aligned}), DKIM: ${dkim_result} (aligned: ${dkim_aligned})`
  );

  // DMARC passes if either SPF or DKIM passes with alignment
  const dmarc_pass = spf_pass || dkim_pass;

  // Record results
  txn.results.add(this, {
    pass: dmarc_pass ? 'pass' : 'fail',
    domain: from_domain,
    policy: policy.p,
    spf: spf_result,
    spf_aligned: spf_aligned,
    dkim: dkim_result,
    dkim_aligned: dkim_aligned,
  });

  // Add Authentication-Results header
  const auth_result = dmarc_pass ? 'pass' : 'fail';
  const ar_header =
    `dmarc=${auth_result} (p=${policy.p}) ` +
    `header.from=${from_domain}`;
  txn.add_header('Authentication-Results', `${connection.hello.host}; ${ar_header}`);

  if (dmarc_pass) {
    connection.loginfo(this, `DMARC pass for ${from_domain}`);
    return next();
  }

  // DMARC failed - apply policy
  connection.logwarn(this, `DMARC fail for ${from_domain}, policy: ${policy.p}`);

  // Check if we should enforce policy based on our config
  if (policy.p === 'reject' && this.cfg.main.reject_on_fail) {
    return next(DENY, `DMARC policy violation: mail from ${from_domain} rejected`);
  }

  if (policy.p === 'quarantine' && this.cfg.main.quarantine_on_fail) {
    // Add header for downstream filtering
    txn.add_header('X-DMARC-Status', 'quarantine');
    connection.loginfo(this, `DMARC quarantine for ${from_domain}`);
  }

  // policy=none or we're not enforcing - just log
  return next();
};

/**
 * Extract domain from email address in From header
 */
exports.extract_domain = function (from_header) {
  // Handle "Name <email@domain>" or just "email@domain"
  const match = from_header.match(/<([^>]+)>/) || from_header.match(/([^\s<>]+@[^\s<>]+)/);
  if (!match) return null;

  const email = match[1];
  const at_pos = email.lastIndexOf('@');
  if (at_pos === -1) return null;

  return email.substring(at_pos + 1).toLowerCase();
};

/**
 * Get DMARC record from DNS
 */
exports.get_dmarc_record = async function (domain, connection) {
  try {
    // Try exact domain first
    let records = await this.dns_txt_lookup(`_dmarc.${domain}`);
    if (records && records.length > 0) {
      const dmarc = records.find((r) => r.startsWith('v=DMARC1'));
      if (dmarc) return dmarc;
    }

    // Try organizational domain (one level up)
    const parts = domain.split('.');
    if (parts.length > 2) {
      const org_domain = parts.slice(-2).join('.');
      records = await this.dns_txt_lookup(`_dmarc.${org_domain}`);
      if (records && records.length > 0) {
        const dmarc = records.find((r) => r.startsWith('v=DMARC1'));
        if (dmarc) return dmarc;
      }
    }

    return null;
  } catch (err) {
    connection.logdebug(this, `DMARC DNS lookup error: ${err.message}`);
    return null;
  }
};

/**
 * DNS TXT lookup helper
 */
exports.dns_txt_lookup = async function (name) {
  try {
    const records = await dns.resolveTxt(name);
    // TXT records come as arrays of strings, join them
    return records.map((r) => r.join(''));
  } catch (err) {
    if (err.code === 'ENODATA' || err.code === 'ENOTFOUND') {
      return null;
    }
    throw err;
  }
};

/**
 * Parse DMARC record into object
 */
exports.parse_dmarc = function (record) {
  const result = {
    v: 'DMARC1',
    p: 'none', // Policy: none, quarantine, reject
    sp: null, // Subdomain policy (inherits p if not set)
    adkim: 'r', // DKIM alignment: r=relaxed, s=strict
    aspf: 'r', // SPF alignment: r=relaxed, s=strict
    pct: 100, // Percentage to apply policy to
    rua: null, // Aggregate report URI
    ruf: null, // Failure report URI
  };

  const parts = record.split(';');
  for (const part of parts) {
    const [key, value] = part.trim().split('=').map((s) => s.trim());
    if (key && value) {
      switch (key.toLowerCase()) {
        case 'p':
          result.p = value.toLowerCase();
          break;
        case 'sp':
          result.sp = value.toLowerCase();
          break;
        case 'adkim':
          result.adkim = value.toLowerCase();
          break;
        case 'aspf':
          result.aspf = value.toLowerCase();
          break;
        case 'pct':
          result.pct = parseInt(value, 10) || 100;
          break;
        case 'rua':
          result.rua = value;
          break;
        case 'ruf':
          result.ruf = value;
          break;
      }
    }
  }

  // sp inherits from p if not set
  if (!result.sp) {
    result.sp = result.p;
  }

  return result;
};

/**
 * Get SPF result from previous plugin
 */
exports.get_spf_result = function (connection) {
  const spf_results = connection.results.get('spf');
  if (!spf_results) return 'none';

  // Check for pass in various result formats
  if (spf_results.pass && spf_results.pass.length > 0) {
    return 'pass';
  }

  if (spf_results.fail && spf_results.fail.length > 0) {
    return 'fail';
  }

  // Check scope results
  if (spf_results.scope) {
    if (spf_results.scope.mfrom === 'pass' || spf_results.scope.helo === 'pass') {
      return 'pass';
    }
  }

  return 'none';
};

/**
 * Check SPF alignment with From domain
 */
exports.check_spf_alignment = function (connection, from_domain, policy) {
  // Get MAIL FROM domain
  const mail_from = connection.transaction.mail_from;
  if (!mail_from || !mail_from.host) {
    return false;
  }

  const mfrom_domain = mail_from.host.toLowerCase();

  if (policy.aspf === 's') {
    // Strict: exact match
    return mfrom_domain === from_domain;
  } else {
    // Relaxed: organizational domain match
    return this.org_domain_match(mfrom_domain, from_domain);
  }
};

/**
 * Get DKIM result from previous plugin
 */
exports.get_dkim_result = function (txn) {
  const dkim_results = txn.results.get('dkim');
  if (!dkim_results) return 'none';

  // Check verify results
  if (dkim_results.pass && dkim_results.pass.length > 0) {
    return 'pass';
  }

  // Check for verification result
  if (dkim_results.result === 'pass') {
    return 'pass';
  }

  return 'none';
};

/**
 * Check DKIM alignment with From domain
 */
exports.check_dkim_alignment = function (txn, from_domain, policy) {
  const dkim_results = txn.results.get('dkim');
  if (!dkim_results) return false;

  // Get DKIM signing domain(s) from results
  let dkim_domains = [];

  // Various ways DKIM results store domain
  if (dkim_results.domain) {
    dkim_domains.push(dkim_results.domain);
  }
  if (dkim_results.pass) {
    // pass array may contain domain info
    for (const p of dkim_results.pass) {
      if (typeof p === 'string' && p.includes('@')) {
        const d = p.split('@')[1];
        if (d) dkim_domains.push(d);
      } else if (typeof p === 'object' && p.domain) {
        dkim_domains.push(p.domain);
      }
    }
  }

  // Check alignment for each signing domain
  for (const dkim_domain of dkim_domains) {
    const d = dkim_domain.toLowerCase();

    if (policy.adkim === 's') {
      // Strict: exact match
      if (d === from_domain) return true;
    } else {
      // Relaxed: organizational domain match
      if (this.org_domain_match(d, from_domain)) return true;
    }
  }

  return false;
};

/**
 * Check if two domains share the same organizational domain
 */
exports.org_domain_match = function (domain1, domain2) {
  const d1 = domain1.toLowerCase();
  const d2 = domain2.toLowerCase();

  // Exact match
  if (d1 === d2) return true;

  // Get organizational domains (simplified: last 2 parts)
  const org1 = this.get_org_domain(d1);
  const org2 = this.get_org_domain(d2);

  return org1 === org2;
};

/**
 * Get organizational domain (simplified implementation)
 */
exports.get_org_domain = function (domain) {
  const parts = domain.split('.');
  if (parts.length <= 2) return domain;

  // Simple heuristic: last 2 parts
  // Note: In production, use Public Suffix List for accuracy
  return parts.slice(-2).join('.');
};
