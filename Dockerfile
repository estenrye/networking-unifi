# UniFi ML2 Driver Dockerfile
FROM python:3.12-slim-bookworm

# Set s6-overlay version
ENV S6_OVERLAY_VERSION=3.1.6.2
ENV PYTHONUNBUFFERED=1

ARG TARGETARCH
ENV TARGETARCH=${TARGETARCH}

# Install system dependencies and build tools
RUN apt update && \
    apt install -y --no-install-recommends \
        build-essential \
        gcc \
        wget \
        sudo \
        ca-certificates \
        libffi-dev \
        libssl-dev \
        libxml2-dev \
        libxslt1-dev \
        zlib1g-dev \
        libmariadb-dev \
        pkg-config && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install s6-overlay
RUN wget "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz" -O "/tmp/s6-overlay-noarch.tar.xz" && \
    tar -C / -Jxpf "/tmp/s6-overlay-noarch.tar.xz" && \
    rm -f "/tmp/s6-overlay-noarch.tar.xz"

RUN if [ "${TARGETARCH}" = "arm64" ]; then S6_ARCHIVE="s6-overlay-aarch64.tar.xz"; else S6_ARCHIVE="s6-overlay-x86_64.tar.xz"; fi && \
    wget "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/${S6_ARCHIVE}" -O "/tmp/${S6_ARCHIVE}" && \
    tar -C / -Jxpf "/tmp/${S6_ARCHIVE}" && \
    rm -f "/tmp/${S6_ARCHIVE}"

# Create neutron user and directories
RUN useradd -r -d /var/lib/neutron -s /bin/false neutron && \
    mkdir -p /var/lib/neutron /var/log/neutron /etc/neutron/plugins/ml2 && \
    chown -R neutron:neutron /var/lib/neutron /var/log/neutron

# Install neutron and dependencies
RUN pip install --no-cache-dir \
        neutron==26.0.0 \
        networking-baremetal==6.5.0 \
        crudini \
        pymysql \
        --root-user-action ignore

# Copy and install the local unifi-ml2-driver package
COPY . /tmp/unifi-ml2-driver
WORKDIR /tmp/unifi-ml2-driver
RUN pip install --no-cache-dir . --root-user-action ignore

# Copy neutron configuration from installed package if available
RUN if [ -d /usr/local/etc/neutron ]; then cp -r /usr/local/etc/neutron/* /etc/neutron/ 2>/dev/null || true; fi

# Copy s6 service definitions and configuration files
COPY deploy/root/ /
COPY deploy/api-paste.ini /etc/neutron/api-paste.ini

# Configure ML2 to use unifi mechanism driver
RUN crudini --set /etc/neutron/plugins/ml2/ml2_conf.ini ml2 mechanism_drivers "unifi,baremetal"

# Setup sudo permissions
RUN echo "root ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers && \
    echo "neutron ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

# Copy and setup entrypoint
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && \
    ln -s /entrypoint.sh /usr/local/bin/entrypoint

# Set ownership and permissions
RUN chown -R neutron:neutron /var/lib/neutron /var/log/neutron /etc/neutron

WORKDIR /

# Expose default neutron port
EXPOSE 9696

ENTRYPOINT ["/init"]
CMD ["svc-bundle"]
