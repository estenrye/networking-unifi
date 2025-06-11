# This dockerfile was licensed under Apache 2.0 License (see LICENSE-haizaar)
# Source: https://github.com/haizaar/docker-python-minimal

FROM python:3.12-slim-bookworm as builder

RUN apt update -qqqq && \
    apt install -yq --no-install-recommends \
        wget \
        sudo \
        ca-certificates && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir \
        unifi-ml2-driver \
        crudini \
        --root-user-action ignore && \
    echo "root ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

ARG TARGETARCH
ENV TARGETARCH=${TARGETARCH}

RUN wget "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz" -O "/tmp/s6-overlay-noarch.tar.xz" && \
    tar -C / -Jxpf "/tmp/s6-overlay-noarch.tar.xz" && \
    rm -f "/tmp/s6-overlay-noarch.tar.xz"
RUN if [[ "${TARGETARCH}" == "arm64" ]]; then S6_ARCHIVE="s6-overlay-aarch64.tar.xz"; else S6_ARCHIVE="s6-overlay-x86_64.tar.xz"; fi && \
    wget "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/${S6_ARCHIVE}" -O "/tmp/${S6_ARCHIVE}" && \
    tar -C / -Jxpf "/tmp/${S6_ARCHIVE}" && \
    rm -f "/tmp/${S6_ARCHIVE}"

COPY deploy/root/ /

RUN pip install --no-cache-dir neutron==26.0.0 \
        networking-baremetal==6.5.0 \
        crudini \
        --root-user-action ignore && \
    cp -r /usr/local/etc/neutron /etc/neutron && \
    mkdir -p /etc/neutron/plugins/ml2

COPY deploy/neutron.conf /etc/neutron/neutron.conf
COPY deploy/ml2_conf.ini /etc/neutron/plugins/ml2/ml2_conf.ini

ADD <<-EOF /etc/neutron/plugins/ml2/ml2_conf.ini
[ml2]
mechanism_drivers = unifi,baremetal
EOF

COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && \
    ln -s /entrypoint.sh /usr/local/bin/entrypoint

ENTRYPOINT ["/init"]
