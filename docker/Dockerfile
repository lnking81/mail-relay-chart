# Custom mail relay image with pre-installed packages
FROM debian:13-slim

# Install all required packages in one layer (this layer will be cached)
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
    postfix \
    opendkim \
    opendkim-tools \
    supervisor \
    ca-certificates \
    curl \
    wget \
    # DNS and networking tools for troubleshooting
    dnsutils \
    iputils-ping \
    telnet \
    netcat-openbsd \
    nmap \
    tcpdump \
    # System debugging tools
    strace \
    ltrace \
    gdb \
    # Process and system monitoring
    htop \
    psmisc \
    procps \
    lsof \
    # Network debugging
    net-tools \
    iproute2 \
    # Text processing and utilities
    vim \
    nano \
    less \
    tree \
    jq \
    # File and permission debugging
    acl \
    attr \
    # Mail testing tools
    swaks \
    mailutils \
    # Clean up to reduce image size
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /tmp/* \
    && rm -rf /var/tmp/*

# Add kubectl for DNS management (multi-arch compatible) - separate layer
RUN ARCH=$(dpkg --print-architecture) && \
    case "$ARCH" in \
    amd64) KUBECTL_ARCH=amd64 ;; \
    arm64) KUBECTL_ARCH=arm64 ;; \
    armhf) KUBECTL_ARCH=arm ;; \
    *) echo "Unsupported architecture: $ARCH" && exit 1 ;; \
    esac && \
    KUBECTL_VERSION=$(curl -L -s https://dl.k8s.io/release/stable.txt) && \
    curl -LO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${KUBECTL_ARCH}/kubectl" && \
    chmod +x kubectl && \
    mv kubectl /usr/local/bin/

# Create necessary directories
RUN mkdir -p /var/spool/postfix \
    && mkdir -p /var/log \
    && mkdir -p /etc/opendkim/keys \
    && mkdir -p /var/run/opendkim \
    && mkdir -p /data/dkim-keys

# Ensure required groups exist
RUN getent group postfix >/dev/null 2>&1 || groupadd -r postfix \
    && getent group postdrop >/dev/null 2>&1 || groupadd -r postdrop

# Create opendkim user if it doesn't exist (it's usually created by the opendkim package)
RUN id -u opendkim >/dev/null 2>&1 || useradd -r -d /var/lib/opendkim -s /bin/false opendkim

# Set up initial permissions (will be properly configured by entrypoint)
RUN chown -R opendkim:opendkim /etc/opendkim/keys/ \
    && chmod -R 700 /etc/opendkim/keys/ \
    && chown -R opendkim:opendkim /var/run/opendkim \
    && mkdir -p /var/log && chmod 755 /var/log \
    && echo "Initial permissions set - entrypoint will configure Postfix directories properly"

# Copy all files
COPY . /

EXPOSE 25

# Use the entrypoint script
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
