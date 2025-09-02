# Custom mail relay image with pre-installed packages
FROM debian:12-slim

# Install all required packages in one layer
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
    postfix \
    opendkim \
    opendkim-tools \
    rsyslog \
    ca-certificates \
    curl \
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
    && mkdir -p /data/dkim-keys

# Create opendkim user
RUN useradd -r -d /var/lib/opendkim -s /bin/false opendkim

# Set up permissions
RUN chown -R opendkim:opendkim /etc/opendkim/keys/ \
    && chmod -R 700 /etc/opendkim/keys/ \
    && chown -R opendkim:opendkim /var/run/opendkim \
    && chown -R postfix:postfix /var/spool/postfix

EXPOSE 25

# Default command
CMD ["/bin/bash"]
