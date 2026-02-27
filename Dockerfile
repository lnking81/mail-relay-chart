# Haraka Mail Relay Image
# Based on Node.js Alpine for minimal footprint
# Includes Python DNS manager for native Cloudflare support
FROM node:20-alpine

LABEL org.opencontainers.image.title="Haraka Mail Relay"
LABEL org.opencontainers.image.description="Haraka SMTP server for mail relay with DKIM signing and DNS management"
LABEL org.opencontainers.image.source="https://github.com/lnking81/mail-relay-chart"
LABEL org.opencontainers.image.version="3.0.0"

# Install system dependencies
RUN apk add --no-cache \
    # Required for DKIM key generation
    openssl \
    # DNS tools for debugging
    bind-tools \
    # Process management
    tini \
    # Debugging tools
    curl \
    bash \
    jq \
    # netcat for healthcheck
    netcat-openbsd \
    # Python for DNS manager
    python3 \
    py3-pip \
    # Kubernetes CLI for service discovery
    kubectl \
    && rm -rf /var/cache/apk/*

# Use existing node user (uid/gid 1000) - rename for clarity
# node:20-alpine already has user 'node' with uid/gid 1000
RUN deluser node 2>/dev/null || true \
    && delgroup node 2>/dev/null || true \
    && addgroup -g 1000 haraka \
    && adduser -D -u 1000 -G haraka -h /app -s /bin/sh haraka

# Create application directory structure
WORKDIR /app

# Install build dependencies, Haraka plugins, then cleanup
# Some plugins (syslog, geoip) require native compilation
RUN apk add --no-cache --virtual .build-deps \
    python3 \
    make \
    g++ \
    && npm install -g Haraka@3.1.3 \
    # === HTTP Server (required for Watch dashboard) ===
    express \
    ws \
    # === Core/Relay ===
    haraka-plugin-relay \
    haraka-plugin-bounce \
    haraka-plugin-limit \
    # === Authentication ===
    haraka-plugin-dkim \
    haraka-plugin-spf \
    # === Monitoring/Logging ===
    haraka-plugin-watch \
    haraka-plugin-syslog \
    haraka-plugin-elasticsearch \
    @mailprotector/haraka-plugin-prometheus \
    # Install prom-client globally for custom plugins (adaptive-rate)
    prom-client \
    # === Storage/Caching ===
    haraka-plugin-redis \
    # === Security/Anti-spam ===
    haraka-plugin-access \
    haraka-plugin-helo.checks \
    haraka-plugin-fcrdns \
    haraka-plugin-asn \
    haraka-plugin-geoip \
    haraka-plugin-karma \
    haraka-plugin-rspamd \
    haraka-plugin-spamassassin \
    haraka-plugin-clamd \
    haraka-plugin-avg \
    haraka-plugin-esets \
    haraka-plugin-messagesniffer \
    haraka-plugin-dcc \
    haraka-plugin-early_talker \
    haraka-plugin-greylist \
    haraka-plugin-dns-list \
    haraka-plugin-uribl \
    haraka-plugin-p0f \
    # === Headers/Processing ===
    haraka-plugin-headers \
    haraka-plugin-aliases \
    haraka-plugin-attachment \
    # === Recipient Validation ===
    haraka-plugin-qmail-deliverable \
    haraka-plugin-known-senders \
    && npm cache clean --force \
    && apk del .build-deps

# Initialize Haraka config directory
RUN haraka -i /app \
    && chown -R haraka:haraka /app

# Create symlink for scoped prometheus plugin (Haraka doesn't support scoped packages natively)
RUN ln -s /usr/local/lib/node_modules/@mailprotector/haraka-plugin-prometheus \
    /usr/local/lib/node_modules/haraka-plugin-prometheus

# Create required directories
RUN mkdir -p /app/config/dkim \
    /app/queue \
    /data \
    /tmp/haraka \
    && chown -R haraka:haraka /app /data /tmp/haraka

# Set proper permissions
RUN chmod 755 /app \
    && chmod 700 /app/config/dkim

# Copy custom plugins
COPY --chown=haraka:haraka plugins/ /app/plugins/

# Install Python DNS manager
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

# Copy DNS manager scripts
COPY --chown=haraka:haraka scripts/ /app/
RUN chmod +x /app/*.py

# Switch to non-root user
USER haraka

# Expose SMTP port
EXPOSE 25

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD echo "QUIT" | nc -w 5 localhost 25 | grep -q "220" || exit 1

# Use tini as init system
ENTRYPOINT ["/sbin/tini", "--"]

# Default command - start Haraka
CMD ["haraka", "-c", "/app"]
