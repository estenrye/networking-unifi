# UniFi ML2 Driver Dockerfile
FROM python:3.12-slim-bookworm

# Set s6-overlay version
ENV S6_OVERLAY_VERSION=3.1.6.2
ENV PYTHONUNBUFFERED=1

ARG TARGETARCH
ENV TARGETARCH=${TARGETARCH}

# Install system dependencies, build tools, packages, and clean up in single layer
RUN apt update && \
    apt install -y --no-install-recommends \
        # Runtime dependencies (kept)
        sudo \
        ca-certificates \
        libmariadb3 \
        # Build dependencies (will be removed)
        build-essential \
        gcc \
        wget \
        libffi-dev \
        libssl-dev \
        libxml2-dev \
        libxslt1-dev \
        zlib1g-dev \
        libmariadb-dev \
        pkg-config && \
    # Install s6-overlay
    wget "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz" -O "/tmp/s6-overlay-noarch.tar.xz" && \
    tar -C / -Jxpf "/tmp/s6-overlay-noarch.tar.xz" && \
    rm -f "/tmp/s6-overlay-noarch.tar.xz" && \
    # Install architecture-specific s6-overlay
    if [ "${TARGETARCH}" = "arm64" ]; then S6_ARCHIVE="s6-overlay-aarch64.tar.xz"; else S6_ARCHIVE="s6-overlay-x86_64.tar.xz"; fi && \
    wget "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/${S6_ARCHIVE}" -O "/tmp/${S6_ARCHIVE}" && \
    tar -C / -Jxpf "/tmp/${S6_ARCHIVE}" && \
    rm -f "/tmp/${S6_ARCHIVE}" && \
    # Install Python packages
    pip install --no-cache-dir \
        neutron==26.0.0 \
        networking-baremetal==6.5.0 \
        crudini \
        pymysql \
        --root-user-action ignore && \
    # Clean up build dependencies and cache
    apt remove -y \
        build-essential \
        gcc \
        wget \
        libffi-dev \
        libssl-dev \
        libxml2-dev \
        libxslt1-dev \
        zlib1g-dev \
        libmariadb-dev \
        pkg-config && \
    apt autoremove -y && \
    apt autoclean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* /root/.cache

# Create neutron user and directories
RUN useradd -r -d /var/lib/neutron -s /bin/false neutron && \
    mkdir -p /var/lib/neutron /var/log/neutron /etc/neutron/plugins/ml2 && \
    chown -R neutron:neutron /var/lib/neutron /var/log/neutron

# Copy and install the local networking-unifi package, then clean up
COPY . /tmp/networking-unifi
WORKDIR /tmp/networking-unifi
RUN pip install --no-cache-dir . --root-user-action ignore && \
    # Copy neutron configuration from installed package if available
    if [ -d /usr/local/etc/neutron ]; then cp -r /usr/local/etc/neutron/* /etc/neutron/ 2>/dev/null || true; fi && \
    # Clean up source code after installation
    cd / && rm -rf /tmp/networking-unifi /root/.cache

# Copy s6 service definitions and configuration files
COPY deploy/root/ /

# Final configuration and setup
RUN crudini --set /etc/neutron/plugins/ml2/ml2_conf.ini ml2 mechanism_drivers "unifi,baremetal" && \
    echo "root ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers && \
    echo "neutron ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers && \
    chown -R neutron:neutron /var/lib/neutron /var/log/neutron /etc/neutron

WORKDIR /

# Expose default neutron port
EXPOSE 9696

ENTRYPOINT ["/init"]
CMD ["svc-bundle"]
