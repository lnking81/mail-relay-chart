# Custom mail relay image with pre-installed packages
FROM debian:13-slim

# Install all required packages in one layer
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
    postfix \
    opendkim \
    opendkim-tools \
    supervisor \
    ca-certificates \
    curl \
    # DNS and networking tools for troubleshooting
    dnsutils \
    iputils-ping \
    telnet \
    netcat-openbsd \
    nmap \
    # Add kubectl for DNS management
    && curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/ \
    # Clean up to reduce image size
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /tmp/* \
    && rm -rf /var/tmp/*

# Create necessary directories
RUN mkdir -p /var/spool/postfix \
    && mkdir -p /var/log \
    && mkdir -p /etc/opendkim/keys \
    && mkdir -p /var/run/opendkim \
    && mkdir -p /data/dkim-keys \
    && mkdir -p /var/spool/rsyslog

# Create opendkim user if it doesn't exist (it's usually created by the opendkim package)
RUN id -u opendkim >/dev/null 2>&1 || useradd -r -d /var/lib/opendkim -s /bin/false opendkim

# Create syslog user and group if they don't exist (for rsyslog)
RUN id -u syslog >/dev/null 2>&1 || useradd -r -d /var/log -s /bin/false syslog \
    && getent group adm >/dev/null 2>&1 || groupadd adm \
    && usermod -a -G adm syslog

# Set up permissions
RUN chown -R opendkim:opendkim /etc/opendkim/keys/ \
    && chmod -R 700 /etc/opendkim/keys/ \
    && chown -R opendkim:opendkim /var/run/opendkim \
    && chown -R postfix:postfix /var/spool/postfix \
    && mkdir -p /var/log && chmod 755 /var/log

# Copy supervisord configuration
COPY supervisord.conf /etc/supervisor/supervisord.conf

# Copy entrypoint script
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 25

# Use the entrypoint script
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
