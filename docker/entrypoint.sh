#!/bin/bash
set -e

echo "=== Mail Relay Container Starting ==="
echo "$(date): Container startup initiated"

# Copy Postfix configuration from configmap
echo "$(date): Copying Postfix configuration from configmap..."
cp /tmp/postfix-config/main.cf /etc/postfix/main.cf
cp /tmp/postfix-config/master.cf /etc/postfix/master.cf

# Copy header checks if enabled
if [ "${HEADER_CHECKS_ENABLED:-false}" = "true" ] && [ -f /tmp/postfix-config/header_checks ]; then
    echo "$(date): Setting up header checks..."
    cp /tmp/postfix-config/header_checks /etc/postfix/header_checks
    postmap /etc/postfix/header_checks
fi

# Copy SASL password file if relay credentials are enabled
if [ "${RELAY_CREDENTIALS_ENABLED:-false}" = "true" ] && [ -f /tmp/postfix-config/sasl_passwd ]; then
    echo "$(date): Setting up SASL authentication..."
    cp /tmp/postfix-config/sasl_passwd /etc/postfix/sasl_passwd
    postmap /etc/postfix/sasl_passwd
    chmod 600 /etc/postfix/sasl_passwd*
fi

# Copy sender access file if enabled
if [ "${SENDER_ACCESS_ENABLED:-false}" = "true" ] && [ -f /tmp/postfix-config/sender_access ]; then
    echo "$(date): Setting up sender access control..."
    cp /tmp/postfix-config/sender_access /etc/postfix/sender_access
    postmap /etc/postfix/sender_access
fi

echo "$(date): Postfix configuration copied successfully"

# Set up OpenDKIM if enabled
if [ "${DKIM_ENABLED:-false}" = "true" ]; then
    echo "$(date): Setting up OpenDKIM configuration..."
    cp /tmp/opendkim-config/opendkim.conf /etc/opendkim.conf
    cp /tmp/opendkim-config/TrustedHosts /etc/opendkim/TrustedHosts
    cp /tmp/opendkim-config/KeyTable /etc/opendkim/KeyTable
    cp /tmp/opendkim-config/SigningTable /etc/opendkim/SigningTable

    # Generate DKIM keys if auto-generation is enabled
    if [ "${DKIM_AUTO_GENERATE:-false}" = "true" ]; then
        echo "$(date): Auto-generating DKIM keys..."
        mkdir -p /data/dkim-keys

        # Parse domains from environment variable (comma-separated)
        IFS=',' read -ra DOMAINS <<< "${DKIM_DOMAINS:-}"
        for domain in "${DOMAINS[@]}"; do
            domain=$(echo "$domain" | xargs) # trim whitespace
            if [ -n "$domain" ] && [ ! -f "/data/dkim-keys/${domain}.private" ]; then
                echo "$(date): Generating DKIM key for $domain"
                opendkim-genkey -b "${DKIM_KEY_SIZE:-2048}" -s "${DKIM_SELECTOR:-mail}" -d "$domain" -D /tmp
                mv "/tmp/${DKIM_SELECTOR:-mail}.private" "/data/dkim-keys/${domain}.private"
                mv "/tmp/${DKIM_SELECTOR:-mail}.txt" "/data/dkim-keys/${domain}.txt"
                echo "DKIM DNS record for $domain:"
                cat "/data/dkim-keys/${domain}.txt"
            fi
            # Link the key to the expected location
            mkdir -p /etc/opendkim/keys
            ln -sf "/data/dkim-keys/${domain}.private" "/etc/opendkim/keys/${domain}.private"
        done
    fi

    # Set permissions for OpenDKIM
    chown -R opendkim:opendkim /etc/opendkim/keys/ 2>/dev/null || true
    chmod -R 700 /etc/opendkim/keys/ 2>/dev/null || true
    mkdir -p /var/run/opendkim
    chown -R opendkim:opendkim /var/run/opendkim 2>/dev/null || true
    echo "$(date): OpenDKIM configuration completed"
fi

# Set up postfix permissions
chown -R postfix:postfix /var/spool/postfix

# Set up persistent Postfix queue if enabled
if [ "${PERSISTENCE_ENABLED:-false}" = "true" ]; then
    echo "$(date): Setting up persistent Postfix queue..."
    mkdir -p /data/postfix-spool

    # If this is the first run, copy the default Postfix spool structure
    if [ ! -f "/data/postfix-spool/.initialized" ]; then
        echo "$(date): Initializing Postfix queue structure..."
        cp -r /var/spool/postfix/* /data/postfix-spool/
        touch /data/postfix-spool/.initialized
        echo "$(date): Postfix queue structure initialized"
    else
        echo "$(date): Using existing persistent queue structure"
    fi

    # Mount the persistent queue directory
    echo "$(date): Linking persistent queue directory..."
    rm -rf /var/spool/postfix
    ln -sf /data/postfix-spool /var/spool/postfix

    # Set proper permissions
    chown -R postfix:postfix /data/postfix-spool
    echo "$(date): Persistent queue setup completed"
fi

# Start rsyslog with configuration to forward to stdout
echo "$(date): Starting rsyslog with stdout forwarding..."
cat > /etc/rsyslog.d/49-stdout.conf << 'EOF'
# Forward mail logs to stdout
mail.*                          /proc/1/fd/1
# Also log to files for compatibility
mail.*                          -/var/log/mail.log
EOF

rsyslogd -n &
RSYSLOG_PID=$!

# Give rsyslog a moment to start
sleep 2
echo "$(date): Rsyslog started with PID $RSYSLOG_PID"

# Start OpenDKIM if enabled
if [ "${DKIM_ENABLED:-false}" = "true" ]; then
    echo "$(date): Starting OpenDKIM with stdout logging..."
    opendkim -f -v &
    OPENDKIM_PID=$!

    # Wait for OpenDKIM to start
    sleep 5
    echo "$(date): OpenDKIM started with PID $OPENDKIM_PID"
fi

# Start Postfix with foreground logging
echo "$(date): Starting Postfix with stdout logging..."
/usr/sbin/postfix start-fg &
POSTFIX_PID=$!
echo "$(date): Postfix started with PID $POSTFIX_PID"

# Start a log monitoring process to ensure all logs go to stdout
echo "$(date): Starting log monitoring..."
(
    # Monitor mail logs and forward to stdout
    tail -F /var/log/mail.log /var/log/maillog /var/log/messages 2>/dev/null | while read line; do
        echo "[MAIL-LOG] $line"
    done
) &
LOG_MONITOR_PID=$!

# Function to handle shutdown
shutdown() {
    echo "$(date): Shutting down mail relay services..."
    if [ "${DKIM_ENABLED:-false}" = "true" ] && [ -n "${OPENDKIM_PID:-}" ]; then
        echo "$(date): Stopping OpenDKIM..."
        kill $OPENDKIM_PID 2>/dev/null || true
    fi
    echo "$(date): Stopping Postfix..."
    /usr/sbin/postfix stop
    kill $POSTFIX_PID 2>/dev/null || true
    echo "$(date): Stopping log monitor..."
    kill $LOG_MONITOR_PID 2>/dev/null || true
    echo "$(date): Stopping rsyslog..."
    kill $RSYSLOG_PID 2>/dev/null || true
    echo "$(date): Mail relay shutdown complete"
    exit 0
}

# Set up signal handlers
trap 'shutdown' SIGTERM SIGINT

echo "$(date): Mail relay services started successfully"
echo "=== Monitoring mail relay processes ==="

# Wait for processes
wait $POSTFIX_PID
