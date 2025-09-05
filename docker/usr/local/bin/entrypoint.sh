#!/bin/bash
set -e

echo "=== Mail Relay Container Starting ==="
echo "$(date): Container startup initiated"

# Function to setup Postfix directory structure and permissions from scratch
setup_postfix_directories() {
    local spool_dir="$1"
    echo "$(date): Setting up Postfix directory structure in: $spool_dir"

    # Ensure base directory exists
    mkdir -p "$spool_dir"

    # Set base directory ownership and permissions (without setgid)
    chown postfix:postfix "$spool_dir"
    chmod 755 "$spool_dir"
    # Explicitly remove any setuid/setgid bits from base directory
    chmod u-s,g-s "$spool_dir"

    # Create all required Postfix directories with proper ownership and permissions
    echo "$(date): Creating Postfix queue directories..."

    # Queue directories - owned by postfix:postfix with 700 permissions
    local queue_dirs="active bounce corrupt defer deferred flush hold incoming saved trace"
    for dir in $queue_dirs; do
        mkdir -p "$spool_dir/$dir"
        chown postfix:postfix "$spool_dir/$dir"
        chmod 700 "$spool_dir/$dir"
        # Explicitly remove any setgid bits that might have been inherited
        chmod -s "$spool_dir/$dir"
        echo "$(date): Created queue directory: $dir (postfix:postfix 700)"
    done

    # Private directory - owned by postfix:postfix with 700 permissions
    mkdir -p "$spool_dir/private"
    chown postfix:postfix "$spool_dir/private"
    chmod 700 "$spool_dir/private"
    # Explicitly remove any setgid bits that might have been inherited
    chmod -s "$spool_dir/private"
    echo "$(date): Created private directory (postfix:postfix 700)"

    # Public directory - owned by postfix:postdrop with 730 permissions
    mkdir -p "$spool_dir/public"
    chown postfix:postdrop "$spool_dir/public"
    chmod 730 "$spool_dir/public"
    # Explicitly remove any setuid/setgid bits that might have been inherited (keep just the standard permissions)
    chmod u-s,g-s "$spool_dir/public"
    chmod 730 "$spool_dir/public"
    echo "$(date): Created public directory (postfix:postdrop 730)"

    # Maildrop directory - owned by postfix:postdrop with 730 permissions
    mkdir -p "$spool_dir/maildrop"
    chown postfix:postdrop "$spool_dir/maildrop"
    chmod 730 "$spool_dir/maildrop"
    # Explicitly remove any setuid/setgid bits that might have been inherited (keep just the standard permissions)
    chmod u-s,g-s "$spool_dir/maildrop"
    chmod 730 "$spool_dir/maildrop"
    echo "$(date): Created maildrop directory (postfix:postdrop 730)"

    # PID directory - owned by root:root with 755 permissions
    mkdir -p "$spool_dir/pid"
    chown root:root "$spool_dir/pid"
    chmod 755 "$spool_dir/pid"
    echo "$(date): Created pid directory (root:root 755)"

    # System directories if they exist
    if [ -d "$spool_dir/etc" ]; then
        chown -R root:root "$spool_dir/etc"
        chmod -R 755 "$spool_dir/etc"
        echo "$(date): Fixed etc directory permissions (root:root 755)"
    fi

    if [ -d "$spool_dir/usr" ]; then
        chown -R root:root "$spool_dir/usr"
        chmod -R 755 "$spool_dir/usr"
        echo "$(date): Fixed usr directory permissions (root:root 755)"
    fi

    if [ -d "$spool_dir/lib" ]; then
        chown -R root:root "$spool_dir/lib"
        chmod -R 755 "$spool_dir/lib"
        echo "$(date): Fixed lib directory permissions (root:root 755)"
    fi

    # Set final base directory ownership and permissions
    chown postfix:postfix "$spool_dir"
    chmod 755 "$spool_dir"
    # Ensure no setuid/setgid bits are set on base directory
    chmod u-s,g-s "$spool_dir"
    echo "$(date): Set base spool directory permissions (postfix:postfix 755)"
}

# Function to verify Postfix permissions
verify_postfix_permissions() {
    local spool_dir="$1"
    echo "$(date): Verifying Postfix permissions in: $spool_dir"

    # Check critical directories
    local errors=0

    # Check queue directories
    local queue_dirs="active bounce corrupt defer deferred flush hold incoming private saved trace"
    for dir in $queue_dirs; do
        if [ -d "$spool_dir/$dir" ]; then
            local owner=$(stat -c '%U:%G' "$spool_dir/$dir" 2>/dev/null || echo "unknown")
            local perms=$(stat -c '%a' "$spool_dir/$dir" 2>/dev/null || echo "000")
            if [ "$owner" != "postfix:postfix" ] || [ "$perms" != "700" ]; then
                echo "$(date): WARNING: Incorrect permissions on $dir - Owner: $owner, Perms: $perms"
                errors=$((errors + 1))
            fi
        fi
    done

    # Check special directories
    if [ -d "$spool_dir/public" ]; then
        local owner=$(stat -c '%U:%G' "$spool_dir/public" 2>/dev/null || echo "unknown")
        local perms=$(stat -c '%a' "$spool_dir/public" 2>/dev/null || echo "000")
        if [ "$owner" != "postfix:postdrop" ] || [ "$perms" != "730" ]; then
            echo "$(date): WARNING: Incorrect permissions on public - Owner: $owner, Perms: $perms"
            errors=$((errors + 1))
        fi
    fi

    if [ -d "$spool_dir/maildrop" ]; then
        local owner=$(stat -c '%U:%G' "$spool_dir/maildrop" 2>/dev/null || echo "unknown")
        local perms=$(stat -c '%a' "$spool_dir/maildrop" 2>/dev/null || echo "000")
        if [ "$owner" != "postfix:postdrop" ] || [ "$perms" != "730" ]; then
            echo "$(date): WARNING: Incorrect permissions on maildrop - Owner: $owner, Perms: $perms"
            errors=$((errors + 1))
        fi
    fi

    if [ "$errors" -eq 0 ]; then
        echo "$(date): Postfix permissions verification passed"
    else
        echo "$(date): Postfix permissions verification found $errors issues"
    fi

    return $errors
}

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

    # Set permissions for OpenDKIM with strict security
    echo "$(date): Setting strict OpenDKIM permissions..."
    chown -R opendkim:opendkim /data/dkim-keys/ 2>/dev/null || true
    chmod 700 /data/dkim-keys/ 2>/dev/null || true
    chmod 600 /data/dkim-keys/*.private 2>/dev/null || true
    chmod 644 /data/dkim-keys/*.txt 2>/dev/null || true
    chown -R opendkim:opendkim /etc/opendkim/keys/ 2>/dev/null || true
    chmod 700 /etc/opendkim/keys/ 2>/dev/null || true
    mkdir -p /var/run/opendkim
    chown -R opendkim:opendkim /var/run/opendkim 2>/dev/null || true
    chmod 755 /var/run/opendkim 2>/dev/null || true

    # Verify OpenDKIM key permissions
    echo "$(date): Verifying OpenDKIM key permissions..."
    for domain in $DKIM_DOMAINS; do
        private_key="/data/dkim-keys/${domain}.private"
        if [ -f "$private_key" ]; then
            key_perms=$(stat -c '%a' "$private_key" 2>/dev/null || echo "000")
            key_owner=$(stat -c '%U:%G' "$private_key" 2>/dev/null || echo "unknown")
            echo "$(date): Key $private_key - Owner: $key_owner, Permissions: $key_perms"
            if [ "$key_perms" != "600" ] || [ "$key_owner" != "opendkim:opendkim" ]; then
                echo "$(date): WARNING: Fixing key permissions for $private_key"
                chown opendkim:opendkim "$private_key"
                chmod 600 "$private_key"
            fi
        fi
    done

    echo "$(date): OpenDKIM configuration completed"
fi

# Set up Postfix directories and permissions from scratch
echo "$(date): Setting up Postfix directories and permissions..."

# Set up persistent Postfix queue if enabled
if [ "${PERSISTENCE_ENABLED:-false}" = "true" ]; then
    echo "$(date): Setting up persistent Postfix queue..."

    # Create persistent storage directory
    mkdir -p /data/postfix-spool

    # If this is the first run, initialize the Postfix spool structure
    if [ ! -f "/data/postfix-spool/.initialized" ]; then
        echo "$(date): First run - initializing persistent Postfix queue structure..."

        # Copy initial structure if the default spool exists and has content
        if [ -d "/var/spool/postfix" ] && [ "$(ls -A /var/spool/postfix 2>/dev/null | wc -l)" -gt 0 ]; then
            echo "$(date): Copying initial Postfix spool structure..."
            cp -r /var/spool/postfix/* /data/postfix-spool/ 2>/dev/null || true
        fi

        # Set up directory structure from scratch
        setup_postfix_directories "/data/postfix-spool"

        # Mark as initialized
        touch /data/postfix-spool/.initialized
        echo "$(date): Persistent Postfix queue structure initialized"
    else
        echo "$(date): Using existing persistent queue structure - fixing permissions..."
        # Fix permissions on existing structure
        setup_postfix_directories "/data/postfix-spool"
    fi

    # Remove old spool directory and create symlink
    echo "$(date): Linking persistent queue directory..."
    if [ -L "/var/spool/postfix" ]; then
        rm -f /var/spool/postfix
    elif [ -d "/var/spool/postfix" ]; then
        rm -rf /var/spool/postfix
    fi
    ln -sf /data/postfix-spool /var/spool/postfix

    # Verify the symlink has correct ownership
    chown -h postfix:postfix /var/spool/postfix

    # Final verification
    verify_postfix_permissions "/data/postfix-spool"
    echo "$(date): Persistent queue setup completed successfully"
else
    echo "$(date): Setting up non-persistent Postfix queue..."
    # For non-persistent setup, ensure the default directory has proper permissions
    setup_postfix_directories "/var/spool/postfix"
    verify_postfix_permissions "/var/spool/postfix"
    echo "$(date): Non-persistent queue setup completed successfully"
fi

# Ensure Postfix configuration directory permissions
echo "$(date): Setting Postfix configuration permissions..."
chown -R root:root /etc/postfix
find /etc/postfix -type d -exec chmod 755 {} \;
find /etc/postfix -type f -exec chmod 644 {} \;

# Secure sensitive Postfix files
if [ -f /etc/postfix/sasl_passwd ]; then
    chmod 600 /etc/postfix/sasl_passwd*
    chown root:root /etc/postfix/sasl_passwd*
fi

# Create supervisor directories
mkdir -p /var/log/supervisor

# Copy supervisor configuration from configmap
echo "$(date): Copying supervisor configuration from configmap..."
cp /tmp/supervisor-config/supervisord.conf /etc/supervisor/supervisord.conf

echo "$(date): Starting supervisord to manage mail relay services..."
echo "=== Services will be managed by supervisord ==="
echo "=== Postfix will log directly to stdout ==="

# Final sanity check - ensure critical directories exist and have correct permissions
echo "$(date): Performing final sanity checks..."

# Verify postfix user exists
if ! id postfix >/dev/null 2>&1; then
    echo "$(date): ERROR: postfix user does not exist!"
    exit 1
fi

# Verify postdrop group exists
if ! getent group postdrop >/dev/null 2>&1; then
    echo "$(date): ERROR: postdrop group does not exist!"
    exit 1
fi

# Check if we can access the spool directory
if [ ! -d "/var/spool/postfix" ]; then
    echo "$(date): ERROR: /var/spool/postfix does not exist!"
    exit 1
fi

# Quick permission test
SPOOL_DIR="/var/spool/postfix"
if [ -L "$SPOOL_DIR" ]; then
    REAL_SPOOL=$(readlink -f "$SPOOL_DIR")
    echo "$(date): Using persistent spool directory: $REAL_SPOOL"
else
    echo "$(date): Using direct spool directory: $SPOOL_DIR"
fi

echo "$(date): Sanity checks completed"

# Start supervisord which will manage all services
exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
