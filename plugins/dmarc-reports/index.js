'use strict';

/**
 * Haraka DMARC Aggregate Reports Plugin
 *
 * Receives and parses DMARC aggregate reports (RFC 7489) and exports
 * statistics to Prometheus metrics.
 *
 * DMARC reports are XML files (usually gzip/zip compressed) sent to the
 * address specified in the domain's DMARC record rua= tag.
 * Example: v=DMARC1; p=reject; rua=mailto:dmarc@example.com
 *
 * The plugin:
 * 1. Accepts mail to dmarc@ addresses on configured domains
 * 2. Extracts XML attachment from MIME multipart message
 * 3. Decompresses if gzip/zip
 * 4. Parses XML and extracts statistics
 * 5. Exports Prometheus metrics for dashboards/alerting
 *
 * Prometheus Metrics:
 * - haraka_dmarc_reports_total{reporter_org, domain} - Reports received
 * - haraka_dmarc_messages_total{domain, disposition, dkim, spf} - Messages in reports
 * - haraka_dmarc_source_messages_total{domain, source_ip, disposition} - Per source IP
 * - haraka_dmarc_alignment_total{domain, dkim_aligned, spf_aligned} - Alignment stats
 * - haraka_dmarc_policy_published{domain, policy, subdomain_policy} - Domain policies
 *
 * Configuration (dmarc_reports.ini):
 * [main]
 * enabled=true
 * metrics_port=8094
 * ; Store parsed reports to disk (JSON format)
 * store_reports=false
 * store_path=/data/dmarc-reports
 * ; Maximum report age to process (days, 0=unlimited)
 * max_report_age=90
 *
 * [webhook]
 * ; Send webhook for each parsed report
 * enabled=false
 * url=http://localhost:8888/dmarc-report
 */

const zlib = require('zlib');
const { promisify } = require('util');
const gunzip = promisify(zlib.gunzip);
const http = require('http');
const https = require('https');
const url = require('url');
const path = require('path');
const fs = require('fs');

// In-memory statistics
const stats = {
    reports_total: new Map(),       // {reporter_org:domain} -> count
    messages_total: new Map(),      // {domain:disposition:dkim:spf} -> count
    source_messages: new Map(),     // {domain:source_ip:disposition} -> count
    alignment_total: new Map(),     // {domain:dkim_aligned:spf_aligned} -> count
    policy_published: new Map(),    // {domain} -> {policy, sp}
    last_report: null,
    errors_total: 0
};

let config = {
    enabled: true,
    metricsPort: 8094,
    storeReports: false,
    storePath: '/data/dmarc-reports',
    maxReportAge: 90,
    webhook: {
        enabled: false,
        url: '',
        timeout: 5000
    }
};

let metricsInitialized = false;
let metricsServer = null;
let metricsServerStarted = false;
let metricsRegistry = null;
let metrics = {};

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

    // Initialize Prometheus metrics
    initMetrics(plugin);

    // Start metrics HTTP server immediately
    ensureMetricsServer(plugin);

    // Register hooks
    plugin.register_hook('queue', 'process_dmarc_report');
    plugin.register_hook('queue_outbound', 'process_dmarc_report');

    logger('DMARC Reports plugin registered');
};

/**
 * Load configuration
 */
exports.load_config = function () {
    const plugin = this;

    const cfg = plugin.config.get('dmarc_reports.ini', {
        booleans: ['+main.enabled', '-main.store_reports', '-webhook.enabled']
    }, () => {
        plugin.load_config();
    });

    if (cfg.main) {
        config.enabled = cfg.main.enabled !== false;
        config.metricsPort = parseInt(cfg.main.metrics_port, 10) || 8094;
        config.storeReports = cfg.main.store_reports || false;
        config.storePath = cfg.main.store_path || '/data/dmarc-reports';
        config.maxReportAge = parseInt(cfg.main.max_report_age, 10) || 90;
    }

    if (cfg.webhook) {
        config.webhook.enabled = cfg.webhook.enabled || false;
        config.webhook.url = cfg.webhook.url || '';
        config.webhook.timeout = parseInt(cfg.webhook.timeout, 10) || 5000;
    }

    logger(`DMARC Reports config: enabled=${config.enabled}, metrics_port=${config.metricsPort}`);
};

/**
 * Try to load prom-client from various locations
 */
function loadPromClient(plugin) {
    const locations = [
        () => require('prom-client'),
        () => require.main?.require?.('prom-client'),
        () => require('@mailprotector/haraka-plugin-prometheus/node_modules/prom-client'),
        () => process.mainModule?.require?.('prom-client')
    ];

    for (const tryLoad of locations) {
        try {
            const client = tryLoad();
            if (client) return client;
        } catch (e) {
            // Try next
        }
    }

    // Search parent modules
    let parent = module.parent;
    while (parent) {
        try {
            const client = parent.require('prom-client');
            if (client) return client;
        } catch (e) {
            // Try next
        }
        parent = parent.parent;
    }

    return null;
}

/**
 * Initialize Prometheus metrics
 */
function initMetrics(plugin) {
    if (metricsInitialized) return;

    try {
        const client = loadPromClient(plugin);
        if (!client) {
            plugin.loginfo('DMARC Reports: prom-client not found, metrics disabled');
            return;
        }

        metricsRegistry = new client.Registry();
        metricsRegistry.setDefaultLabels({ plugin: 'dmarc_reports' });

        const prefix = 'haraka_dmarc_';

        // Reports received counter
        metrics.reportsCounter = new client.Counter({
            name: prefix + 'reports_total',
            help: 'Total DMARC aggregate reports received',
            labelNames: ['reporter_org', 'domain'],
            registers: [metricsRegistry]
        });

        // Messages in reports counter
        metrics.messagesCounter = new client.Counter({
            name: prefix + 'messages_total',
            help: 'Total messages reported in DMARC aggregate reports',
            labelNames: ['domain', 'disposition', 'dkim', 'spf'],
            registers: [metricsRegistry]
        });

        // Messages per source IP (top talkers)
        metrics.sourceMessagesCounter = new client.Counter({
            name: prefix + 'source_messages_total',
            help: 'Messages per source IP in DMARC reports',
            labelNames: ['domain', 'source_ip', 'disposition'],
            registers: [metricsRegistry]
        });

        // Alignment statistics
        metrics.alignmentCounter = new client.Counter({
            name: prefix + 'alignment_total',
            help: 'DMARC alignment results',
            labelNames: ['domain', 'dkim_aligned', 'spf_aligned'],
            registers: [metricsRegistry]
        });

        // Policy published (gauge - latest seen)
        metrics.policyGauge = new client.Gauge({
            name: prefix + 'policy_info',
            help: 'DMARC policy published by domain (1=exists)',
            labelNames: ['domain', 'policy', 'subdomain_policy', 'pct'],
            registers: [metricsRegistry]
        });

        // Parsing errors
        metrics.errorsCounter = new client.Counter({
            name: prefix + 'parse_errors_total',
            help: 'DMARC report parsing errors',
            labelNames: ['error_type'],
            registers: [metricsRegistry]
        });

        // Report date range (for detecting old reports)
        metrics.reportAgeGauge = new client.Gauge({
            name: prefix + 'last_report_age_seconds',
            help: 'Age of the last processed DMARC report',
            labelNames: ['domain'],
            registers: [metricsRegistry]
        });

        metricsInitialized = true;
        plugin.loginfo('DMARC Reports: Prometheus metrics initialized');

    } catch (err) {
        plugin.logwarn(`DMARC Reports: Metrics init failed: ${err.message}`);
    }
}

/**
 * Start metrics HTTP server
 */
function ensureMetricsServer(plugin) {
    if (metricsServerStarted || !metricsInitialized) return;
    metricsServerStarted = true;

    try {
        metricsServer = http.createServer(async (req, res) => {
            if (req.url === '/metrics' && req.method === 'GET') {
                try {
                    const output = await metricsRegistry.metrics();
                    res.setHeader('Content-Type', metricsRegistry.contentType);
                    res.end(output);
                } catch (err) {
                    res.writeHead(500);
                    res.end(`Error: ${err.message}`);
                }
            } else if (req.url === '/health') {
                res.writeHead(200);
                res.end('OK');
            } else if (req.url === '/stats') {
                // Return raw stats as JSON
                res.setHeader('Content-Type', 'application/json');
                res.end(JSON.stringify(getStats(), null, 2));
            } else {
                res.writeHead(404);
                res.end('Not Found');
            }
        });

        metricsServer.listen(config.metricsPort, '0.0.0.0', () => {
            plugin.loginfo(`DMARC Reports metrics server on port ${config.metricsPort}`);
        });

        metricsServer.on('error', (err) => {
            if (err.code === 'EADDRINUSE') {
                plugin.logdebug(`DMARC Reports: Port ${config.metricsPort} in use`);
            } else {
                plugin.logerror(`DMARC Reports: Server error: ${err.message}`);
            }
        });

    } catch (err) {
        plugin.logwarn(`Failed to start DMARC metrics server: ${err.message}`);
    }
}

/**
 * Process DMARC report from inbound email
 */
exports.process_dmarc_report = async function (next, connection) {
    const plugin = this;
    const transaction = connection?.transaction;

    if (!config.enabled || !transaction) {
        return next();
    }

    // Check if this is a DMARC report (marked by rcpt_to.inbound plugin)
    if (transaction.notes.inbound_type !== 'dmarc') {
        return next();
    }

    // Start metrics server on first call
    ensureMetricsServer(plugin);

    logger(`Processing DMARC report to ${transaction.notes.inbound_recipient}`);

    try {
        // Get message body
        const body = await getMessageBody(transaction);
        if (!body || body.length < 100) {
            logwarn('DMARC report: No body or too short');
            recordError('no_body');
            return next(OK, 'Report received (no content)');
        }

        // Extract XML from MIME message
        const xmlContent = await extractXmlFromMime(body, plugin);
        if (!xmlContent) {
            logwarn('DMARC report: Could not extract XML');
            recordError('no_xml');
            return next(OK, 'Report received (no XML found)');
        }

        // Parse XML report
        const report = parseXmlReport(xmlContent);
        if (!report) {
            logwarn('DMARC report: Failed to parse XML');
            recordError('parse_failed');
            return next(OK, 'Report received (parse failed)');
        }

        // Check report age
        if (config.maxReportAge > 0 && report.date_range) {
            const reportEnd = report.date_range.end * 1000;
            const ageMs = Date.now() - reportEnd;
            const ageDays = ageMs / (24 * 60 * 60 * 1000);

            if (ageDays > config.maxReportAge) {
                logger(`Skipping old DMARC report: ${Math.floor(ageDays)} days old`);
                recordError('too_old');
                return next(OK, 'Report received (too old)');
            }
        }

        // Record metrics
        recordReportMetrics(report, plugin);

        // Store report if enabled
        if (config.storeReports) {
            await storeReport(report, plugin);
        }

        // Send webhook if enabled
        if (config.webhook.enabled && config.webhook.url) {
            sendWebhook(report);
        }

        logger(`DMARC report processed: org=${report.reporter_org}, domain=${report.policy_domain}, records=${report.records?.length || 0}`);

        return next(OK, 'DMARC report processed');

    } catch (err) {
        logerror(`DMARC report processing error: ${err.message}`);
        recordError('exception');
        return next(OK, 'Report received with errors');
    }
};

/**
 * Get message body from transaction
 */
function getMessageBody(transaction) {
    return new Promise((resolve) => {
        const timeout = setTimeout(() => resolve(''), 10000);

        try {
            // Try transaction.body.bodytext first
            if (transaction.body?.bodytext) {
                clearTimeout(timeout);
                return resolve(transaction.body.bodytext);
            }

            // Try data_lines
            if (transaction.data_lines?.length > 0) {
                clearTimeout(timeout);
                return resolve(transaction.data_lines.join('\n'));
            }

            // Try message_stream
            if (transaction.message_stream?.readable) {
                let body = '';
                transaction.message_stream.on('data', chunk => body += chunk.toString());
                transaction.message_stream.on('end', () => {
                    clearTimeout(timeout);
                    resolve(body);
                });
                transaction.message_stream.on('error', () => {
                    clearTimeout(timeout);
                    resolve('');
                });
                if (transaction.message_stream.resume) {
                    transaction.message_stream.resume();
                }
            } else {
                clearTimeout(timeout);
                resolve('');
            }
        } catch (err) {
            clearTimeout(timeout);
            resolve('');
        }
    });
}

/**
 * Extract XML content from MIME message
 * Handles: raw XML, gzip, zip attachments
 */
async function extractXmlFromMime(body, plugin) {
    // Check if body is already XML
    if (body.includes('<?xml') || body.includes('<feedback')) {
        const xmlStart = body.indexOf('<?xml');
        const feedbackStart = body.indexOf('<feedback');
        const start = xmlStart >= 0 ? xmlStart : feedbackStart;
        if (start >= 0) {
            const end = body.indexOf('</feedback>');
            if (end > start) {
                return body.substring(start, end + '</feedback>'.length);
            }
        }
    }

    // Look for base64 encoded attachment
    const base64Patterns = [
        // Content-Type: application/gzip or application/zip
        /Content-Type:\s*application\/(gzip|x-gzip|zip|x-zip)[^]*?Content-Transfer-Encoding:\s*base64[^]*?\r?\n\r?\n([A-Za-z0-9+/=\s]+)/i,
        // Content-Type: text/xml (base64)
        /Content-Type:\s*text\/xml[^]*?Content-Transfer-Encoding:\s*base64[^]*?\r?\n\r?\n([A-Za-z0-9+/=\s]+)/i,
        // Generic base64 block after boundary
        /--[^\r\n]+\r?\n[^]*?Content-Transfer-Encoding:\s*base64[^]*?\r?\n\r?\n([A-Za-z0-9+/=\s]+)/i
    ];

    for (const pattern of base64Patterns) {
        const match = body.match(pattern);
        if (match) {
            try {
                const base64Data = (match[2] || match[1]).replace(/\s/g, '');
                const buffer = Buffer.from(base64Data, 'base64');

                // Check if it's gzip compressed (magic bytes 1f 8b)
                if (buffer[0] === 0x1f && buffer[1] === 0x8b) {
                    const decompressed = await gunzip(buffer);
                    const xml = decompressed.toString('utf8');
                    if (xml.includes('<feedback')) {
                        return xml;
                    }
                }

                // Check if it's a ZIP file (PK magic bytes)
                if (buffer[0] === 0x50 && buffer[1] === 0x4b) {
                    const xml = await extractFromZip(buffer);
                    if (xml) return xml;
                }

                // Try as plain XML
                const text = buffer.toString('utf8');
                if (text.includes('<feedback')) {
                    return text;
                }

            } catch (err) {
                plugin.logdebug(`Failed to decode base64 attachment: ${err.message}`);
            }
        }
    }

    // Try to find raw gzip in body (non-base64)
    const gzipIndex = body.indexOf('\x1f\x8b');
    if (gzipIndex >= 0) {
        try {
            // Find the attachment boundary end
            const attachmentData = body.substring(gzipIndex);
            const boundaryIndex = attachmentData.indexOf('\r\n--');
            const gzipData = boundaryIndex > 0
                ? attachmentData.substring(0, boundaryIndex)
                : attachmentData;

            const decompressed = await gunzip(Buffer.from(gzipData, 'binary'));
            const xml = decompressed.toString('utf8');
            if (xml.includes('<feedback')) {
                return xml;
            }
        } catch (err) {
            plugin.logdebug(`Failed to decompress inline gzip: ${err.message}`);
        }
    }

    return null;
}

/**
 * Extract XML from ZIP archive (simple implementation)
 * Note: For full ZIP support, consider using 'adm-zip' or 'unzipper' package
 */
async function extractFromZip(buffer) {
    // Simple ZIP extraction - find XML in uncompressed entries
    // ZIP local file header signature: 50 4b 03 04
    try {
        const zip = buffer.toString('binary');
        let pos = 0;

        while (pos < zip.length - 30) {
            // Look for local file header
            if (zip.charCodeAt(pos) === 0x50 &&
                zip.charCodeAt(pos + 1) === 0x4b &&
                zip.charCodeAt(pos + 2) === 0x03 &&
                zip.charCodeAt(pos + 3) === 0x04) {

                const compressionMethod = zip.charCodeAt(pos + 8) | (zip.charCodeAt(pos + 9) << 8);
                const compressedSize = zip.charCodeAt(pos + 18) |
                    (zip.charCodeAt(pos + 19) << 8) |
                    (zip.charCodeAt(pos + 20) << 16) |
                    (zip.charCodeAt(pos + 21) << 24);
                const filenameLen = zip.charCodeAt(pos + 26) | (zip.charCodeAt(pos + 27) << 8);
                const extraLen = zip.charCodeAt(pos + 28) | (zip.charCodeAt(pos + 29) << 8);

                const dataStart = pos + 30 + filenameLen + extraLen;
                const dataEnd = dataStart + compressedSize;

                if (dataEnd <= zip.length) {
                    let content;

                    if (compressionMethod === 0) {
                        // Stored (uncompressed)
                        content = zip.substring(dataStart, dataEnd);
                    } else if (compressionMethod === 8) {
                        // Deflate
                        try {
                            const inflated = zlib.inflateRawSync(
                                Buffer.from(zip.substring(dataStart, dataEnd), 'binary')
                            );
                            content = inflated.toString('utf8');
                        } catch (e) {
                            // Try next entry
                        }
                    }

                    if (content && content.includes('<feedback')) {
                        return content;
                    }
                }

                pos = dataEnd;
            } else {
                pos++;
            }
        }
    } catch (err) {
        // ZIP parsing failed
    }

    return null;
}

/**
 * Parse DMARC XML report
 * Returns structured report object
 */
function parseXmlReport(xml) {
    const report = {
        reporter_org: '',
        reporter_email: '',
        report_id: '',
        date_range: { begin: 0, end: 0 },
        policy_domain: '',
        policy: 'none',
        subdomain_policy: 'none',
        pct: 100,
        adkim: 'r',
        aspf: 'r',
        records: []
    };

    try {
        // Extract report metadata
        report.reporter_org = extractTag(xml, 'org_name') || 'unknown';
        report.reporter_email = extractTag(xml, 'email') || '';
        report.report_id = extractTag(xml, 'report_id') || '';

        // Date range
        const dateRange = extractSection(xml, 'date_range');
        if (dateRange) {
            report.date_range.begin = parseInt(extractTag(dateRange, 'begin'), 10) || 0;
            report.date_range.end = parseInt(extractTag(dateRange, 'end'), 10) || 0;
        }

        // Policy published
        const policy = extractSection(xml, 'policy_published');
        if (policy) {
            report.policy_domain = extractTag(policy, 'domain') || '';
            report.policy = extractTag(policy, 'p') || 'none';
            report.subdomain_policy = extractTag(policy, 'sp') || report.policy;
            report.pct = parseInt(extractTag(policy, 'pct'), 10) || 100;
            report.adkim = extractTag(policy, 'adkim') || 'r';
            report.aspf = extractTag(policy, 'aspf') || 'r';
        }

        // Parse records
        const recordMatches = xml.match(/<record>[\s\S]*?<\/record>/gi) || [];
        for (const recordXml of recordMatches) {
            const record = parseRecord(recordXml);
            if (record) {
                report.records.push(record);
            }
        }

        return report;

    } catch (err) {
        return null;
    }
}

/**
 * Parse single record from DMARC report
 */
function parseRecord(xml) {
    try {
        const record = {
            source_ip: '',
            count: 0,
            disposition: 'none',
            dkim: 'none',
            spf: 'none',
            header_from: '',
            dkim_domain: '',
            dkim_result: 'none',
            spf_domain: '',
            spf_result: 'none'
        };

        // Row data
        const row = extractSection(xml, 'row');
        if (row) {
            record.source_ip = extractTag(row, 'source_ip') || '';
            record.count = parseInt(extractTag(row, 'count'), 10) || 1;

            const policyEval = extractSection(row, 'policy_evaluated');
            if (policyEval) {
                record.disposition = extractTag(policyEval, 'disposition') || 'none';
                record.dkim = extractTag(policyEval, 'dkim') || 'none';
                record.spf = extractTag(policyEval, 'spf') || 'none';
            }
        }

        // Identifiers
        const identifiers = extractSection(xml, 'identifiers');
        if (identifiers) {
            record.header_from = extractTag(identifiers, 'header_from') || '';
        }

        // Auth results
        const authResults = extractSection(xml, 'auth_results');
        if (authResults) {
            // DKIM result
            const dkimSection = extractSection(authResults, 'dkim');
            if (dkimSection) {
                record.dkim_domain = extractTag(dkimSection, 'domain') || '';
                record.dkim_result = extractTag(dkimSection, 'result') || 'none';
            }

            // SPF result
            const spfSection = extractSection(authResults, 'spf');
            if (spfSection) {
                record.spf_domain = extractTag(spfSection, 'domain') || '';
                record.spf_result = extractTag(spfSection, 'result') || 'none';
            }
        }

        return record;

    } catch (err) {
        return null;
    }
}

/**
 * Extract tag value from XML
 */
function extractTag(xml, tag) {
    const regex = new RegExp(`<${tag}>([^<]*)</${tag}>`, 'i');
    const match = xml.match(regex);
    return match ? match[1].trim() : null;
}

/**
 * Extract section from XML
 */
function extractSection(xml, tag) {
    const regex = new RegExp(`<${tag}>[\\s\\S]*?</${tag}>`, 'i');
    const match = xml.match(regex);
    return match ? match[0] : null;
}

/**
 * Record report metrics in Prometheus
 */
function recordReportMetrics(report, plugin) {
    if (!metricsInitialized) return;

    const domain = report.policy_domain || 'unknown';
    const reporter = report.reporter_org || 'unknown';

    try {
        // Report counter
        metrics.reportsCounter.inc({ reporter_org: reporter, domain });

        // Policy gauge
        metrics.policyGauge.set(
            {
                domain,
                policy: report.policy,
                subdomain_policy: report.subdomain_policy,
                pct: String(report.pct)
            },
            1
        );

        // Report age
        if (report.date_range.end) {
            const ageSeconds = Math.floor((Date.now() - report.date_range.end * 1000) / 1000);
            metrics.reportAgeGauge.set({ domain }, Math.max(0, ageSeconds));
        }

        // Process records
        for (const record of report.records || []) {
            const count = record.count || 1;

            // Messages by disposition/dkim/spf
            metrics.messagesCounter.inc(
                {
                    domain,
                    disposition: record.disposition,
                    dkim: record.dkim,
                    spf: record.spf
                },
                count
            );

            // Source IP tracking (limit cardinality)
            if (record.source_ip && count >= 10) {
                metrics.sourceMessagesCounter.inc(
                    {
                        domain,
                        source_ip: record.source_ip,
                        disposition: record.disposition
                    },
                    count
                );
            }

            // Alignment tracking
            const dkimAligned = record.dkim === 'pass' ? 'yes' : 'no';
            const spfAligned = record.spf === 'pass' ? 'yes' : 'no';
            metrics.alignmentCounter.inc(
                {
                    domain,
                    dkim_aligned: dkimAligned,
                    spf_aligned: spfAligned
                },
                count
            );
        }

        // Update in-memory stats
        stats.last_report = {
            timestamp: new Date().toISOString(),
            reporter_org: reporter,
            domain,
            records_count: report.records?.length || 0
        };

    } catch (err) {
        plugin.logwarn(`Failed to record metrics: ${err.message}`);
    }
}

/**
 * Record parsing error
 */
function recordError(errorType) {
    stats.errors_total++;
    if (metricsInitialized && metrics.errorsCounter) {
        try {
            metrics.errorsCounter.inc({ error_type: errorType });
        } catch (e) {
            // Ignore
        }
    }
}

/**
 * Store report to disk
 */
async function storeReport(report, plugin) {
    try {
        const dir = config.storePath;
        if (!fs.existsSync(dir)) {
            fs.mkdirSync(dir, { recursive: true });
        }

        const filename = `${report.policy_domain}_${report.reporter_org}_${report.report_id || Date.now()}.json`
            .replace(/[^a-zA-Z0-9._-]/g, '_');
        const filepath = path.join(dir, filename);

        fs.writeFileSync(filepath, JSON.stringify(report, null, 2));
        logger(`Stored DMARC report: ${filepath}`);

    } catch (err) {
        plugin.logwarn(`Failed to store report: ${err.message}`);
    }
}

/**
 * Send webhook notification
 */
function sendWebhook(report) {
    try {
        const parsed = url.parse(config.webhook.url);
        const data = JSON.stringify({
            event: 'dmarc_report',
            timestamp: new Date().toISOString(),
            report
        });

        const options = {
            hostname: parsed.hostname,
            port: parsed.port || (parsed.protocol === 'https:' ? 443 : 80),
            path: parsed.path,
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(data),
                'User-Agent': 'Haraka-DMARC-Reports/1.0'
            },
            timeout: config.webhook.timeout
        };

        const transport = parsed.protocol === 'https:' ? https : http;
        const req = transport.request(options, (res) => {
            if (res.statusCode >= 200 && res.statusCode < 300) {
                logger(`DMARC webhook sent: ${res.statusCode}`);
            } else {
                logwarn(`DMARC webhook failed: ${res.statusCode}`);
            }
        });

        req.on('error', (err) => {
            logwarn(`DMARC webhook error: ${err.message}`);
        });

        req.on('timeout', () => {
            req.destroy();
            logwarn('DMARC webhook timeout');
        });

        req.write(data);
        req.end();

    } catch (err) {
        logwarn(`DMARC webhook error: ${err.message}`);
    }
}

/**
 * Get current statistics
 */
function getStats() {
    return {
        last_report: stats.last_report,
        errors_total: stats.errors_total,
        metrics_initialized: metricsInitialized
    };
}

/**
 * Export stats for external access
 */
exports.get_stats = getStats;
