'use strict';

/**
 * Haraka Strip Headers Plugin
 *
 * Removes internal/sensitive headers from outgoing emails to prevent
 * information disclosure about internal infrastructure:
 * - Received headers with internal IPs (RFC1918: 10.x, 172.16-31.x, 192.168.x)
 * - Received headers with internal/kubernetes hostnames
 * - Other configurable headers (X-Originating-IP, etc.)
 *
 * Configuration (strip_headers.ini):
 * [main]
 * enabled=true
 * ; Strip ALL Received headers (maximum privacy)
 * strip_all_received=false
 * ; Strip only Received headers with internal IPs/hostnames
 * strip_internal_received=true
 *
 * [headers]
 * ; Headers to always strip (one per line)
 * strip[]=X-Originating-IP
 * strip[]=X-Mailer
 *
 * [internal]
 * ; Regex patterns for internal hostnames (in Received headers)
 * hostname_patterns[]=-[a-z0-9]{5,10}-[a-z0-9]{4,5}
 * hostname_patterns[]=\.local$
 * hostname_patterns[]=\.internal$
 * hostname_patterns[]=\.cluster\.local$
 * hostname_patterns[]=\.svc$
 * hostname_patterns[]=\.pod$
 */

let config = {
    enabled: true,
    strip_all_received: false,
    strip_internal_received: true,
    headers_to_strip: [],
    hostname_patterns: []
};

// RFC1918 private IP ranges
const PRIVATE_IP_PATTERNS = [
    /\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b/,            // 10.0.0.0/8
    /\b172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b/, // 172.16.0.0/12
    /\b192\.168\.\d{1,3}\.\d{1,3}\b/,               // 192.168.0.0/16
    /\b127\.\d{1,3}\.\d{1,3}\.\d{1,3}\b/,           // 127.0.0.0/8 (loopback)
    /\b169\.254\.\d{1,3}\.\d{1,3}\b/,               // 169.254.0.0/16 (link-local)
    /\bfd[0-9a-f]{2}:/i,                            // IPv6 ULA (fd00::/8)
    /\bfe80:/i,                                      // IPv6 link-local
    /\b::1\b/                                        // IPv6 loopback
];

// Default internal hostname patterns (Kubernetes pods, etc.)
const DEFAULT_HOSTNAME_PATTERNS = [
    /-[a-z0-9]{5,10}-[a-z0-9]{4,5}/i,  // Kubernetes pod names: app-abc123-xy456
    /\.local$/i,                         // .local domains
    /\.internal$/i,                      // .internal domains
    /\.cluster\.local$/i,                // Kubernetes cluster domain
    /\.svc$/i,                           // Kubernetes service short names
    /\.pod$/i,                           // Kubernetes pod short names
    /\.default$/i,                       // Default namespace references
    /^localhost$/i                       // localhost
];

let hostname_regexes = [];
let logger;

/**
 * Plugin registration
 */
exports.register = function () {
    const plugin = this;
    logger = plugin.loginfo.bind(plugin);

    plugin.load_config();

    // Hook into data_post to modify headers before queuing
    plugin.register_hook('data_post', 'strip_headers');

    logger('Strip headers plugin registered');
};

/**
 * Load configuration
 */
exports.load_config = function () {
    const plugin = this;

    const cfg = plugin.config.get('strip_headers.ini', {
        booleans: [
            '+main.enabled',
            '-main.strip_all_received',
            '+main.strip_internal_received'
        ]
    }, () => {
        plugin.load_config();
    });

    if (cfg.main) {
        config.enabled = cfg.main.enabled !== false;
        config.strip_all_received = cfg.main.strip_all_received === true;
        config.strip_internal_received = cfg.main.strip_internal_received !== false;
    }

    // Load headers to strip
    config.headers_to_strip = [];
    if (cfg.headers && cfg.headers.strip) {
        const strips = Array.isArray(cfg.headers.strip) ? cfg.headers.strip : [cfg.headers.strip];
        config.headers_to_strip = strips.filter(h => h && h.trim());
    }

    // Load hostname patterns
    hostname_regexes = [...DEFAULT_HOSTNAME_PATTERNS];
    if (cfg.internal && cfg.internal.hostname_patterns) {
        const patterns = Array.isArray(cfg.internal.hostname_patterns)
            ? cfg.internal.hostname_patterns
            : [cfg.internal.hostname_patterns];

        for (const pattern of patterns) {
            if (pattern && pattern.trim()) {
                try {
                    hostname_regexes.push(new RegExp(pattern, 'i'));
                } catch (err) {
                    plugin.logerror(`Invalid hostname pattern: ${pattern} - ${err.message}`);
                }
            }
        }
    }

    logger(`Config loaded: enabled=${config.enabled}, strip_all_received=${config.strip_all_received}, ` +
        `strip_internal_received=${config.strip_internal_received}, ` +
        `headers_to_strip=[${config.headers_to_strip.join(',')}], ` +
        `hostname_patterns=${hostname_regexes.length}`);
};

/**
 * Check if a Received header contains internal information
 * @param {string} value - Received header value
 * @returns {boolean} true if header contains internal info
 */
function is_internal_received(value) {
    if (!value) return false;

    // Check for private IPs
    for (const pattern of PRIVATE_IP_PATTERNS) {
        if (pattern.test(value)) {
            return true;
        }
    }

    // Check for internal hostnames
    for (const pattern of hostname_regexes) {
        if (pattern.test(value)) {
            return true;
        }
    }

    return false;
}

/**
 * Strip headers from the message
 * Only for OUTBOUND (relay) connections, not inbound mail
 */
exports.strip_headers = function (next, connection) {
    const plugin = this;
    const transaction = connection?.transaction;

    if (!config.enabled || !transaction) {
        return next();
    }

    // Only strip headers for RELAY connections (outbound mail)
    // Don't strip headers from inbound mail (bounces, external senders)
    // connection.relaying is set by relay plugin when connection is authorized to relay
    if (!connection.relaying) {
        plugin.logdebug('Skipping header strip for non-relay connection');
        return next();
    }

    try {
        let stripped_count = 0;

        // Handle Received headers
        if (config.strip_all_received) {
            // Remove ALL Received headers
            const received = transaction.header.get_all('Received');
            if (received && received.length > 0) {
                transaction.remove_header('Received');
                stripped_count += received.length;
                logger(`Stripped ALL ${received.length} Received headers`);
            }
        } else if (config.strip_internal_received) {
            // Remove only Received headers with internal info
            const received = transaction.header.get_all('Received');
            if (received && received.length > 0) {
                // We need to remove selectively, but Haraka doesn't support
                // removing specific instances. So we remove all and re-add non-internal ones.
                const keep = [];
                const remove_indices = [];

                for (let i = 0; i < received.length; i++) {
                    if (is_internal_received(received[i])) {
                        remove_indices.push(i);
                        stripped_count++;
                        plugin.logdebug(`Stripping internal Received[${i}]: ${received[i].substring(0, 80)}...`);
                    } else {
                        keep.push(received[i]);
                    }
                }

                if (remove_indices.length > 0) {
                    // Remove all Received headers
                    transaction.remove_header('Received');

                    // Re-add the non-internal ones (in reverse order to maintain order)
                    for (let i = keep.length - 1; i >= 0; i--) {
                        transaction.add_header('Received', keep[i].trim());
                    }

                    logger(`Stripped ${remove_indices.length} internal Received headers, kept ${keep.length}`);
                }
            }
        }

        // Strip other configured headers
        for (const header of config.headers_to_strip) {
            const values = transaction.header.get_all(header);
            if (values && values.length > 0) {
                transaction.remove_header(header);
                stripped_count += values.length;
                logger(`Stripped ${values.length} ${header} header(s)`);
            }
        }

        if (stripped_count > 0) {
            plugin.loginfo(`Stripped ${stripped_count} header(s) total`);
        }

    } catch (err) {
        plugin.logerror(`Error stripping headers: ${err.message}`);
    }

    next();
};

// Export for testing
exports.is_internal_received = is_internal_received;
exports.get_config = () => config;
