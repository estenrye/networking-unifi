#!/usr/bin/bash

set -euxo pipefail

NEUTRON_EXTERNAL_IP="${NEUTRON_EXTERNAL_IP:-}"

# Define the VLAN interfaces to be included in introspection report, e.g.
#   all - all VLANs on all interfaces using LLDP information
#   <interface> - all VLANs on a particular interface using LLDP information
#   <interface.vlan> - a particular VLAN on an interface, not relying on LLDP
export NEUTRON_ENABLE_VLAN_INTERFACES=${NEUTRON_ENABLE_VLAN_INTERFACES:-${NEUTRON_INSPECTOR_VLAN_INTERFACES:-all}}

# shellcheck disable=SC1091
. /bin/tls-common.sh
# shellcheck disable=SC1091
. /bin/neutron-common.sh
# shellcheck disable=SC1091
. /bin/auth-common.sh

export HTTP_PORT=${HTTP_PORT:-80}

if [[ "${NEUTRON_USE_MARIADB}" == true ]]; then
    if [[ -z "${MARIADB_PASSWORD:-}" ]]; then
        echo "FATAL: NEUTRON_USE_MARIADB requires password, mount a secret under /auth/mariadb"
        exit 1
    fi
    MARIADB_DATABASE=${MARIADB_DATABASE:-neutron}
    MARIADB_USER=${MARIADB_USER:-neutron}
    MARIADB_HOST=${MARIADB_HOST:-127.0.0.1}
    export MARIADB_CONNECTION="mysql+pymysql://${MARIADB_USER}:${MARIADB_PASSWORD}@${MARIADB_HOST}/${MARIADB_DATABASE}?charset=utf8"
    if [[ "$MARIADB_TLS_ENABLED" == "true" ]]; then
        export MARIADB_CONNECTION="${MARIADB_CONNECTION}&ssl=on&ssl_ca=${MARIADB_CACERT_FILE}"
    fi
fi

# zero makes it do cpu number detection on Ironic side
export NUMWORKERS=${NUMWORKERS:-0}

wait_for_interface_or_ip

if [[ -n "$NEUTRON_EXTERNAL_IP" ]]; then
    export NEUTRON_EXTERNAL_CALLBACK_URL=${NEUTRON_EXTERNAL_CALLBACK_URL:-"${NEUTRON_SCHEME}://${NEUTRON_EXTERNAL_IP}:${NEUTRON_ACCESS_PORT}"}
    if [[ "$NEUTRON_VMEDIA_TLS_SETUP" == "true" ]]; then
        export NEUTRON_EXTERNAL_HTTP_URL=${NEUTRON_EXTERNAL_HTTP_URL:-"https://${NEUTRON_EXTERNAL_IP}:${VMEDIA_TLS_PORT}"}
    else
        export NEUTRON_EXTERNAL_HTTP_URL=${NEUTRON_EXTERNAL_HTTP_URL:-"http://${NEUTRON_EXTERNAL_IP}:${HTTP_PORT}"}
    fi
fi

if [[ -f "${NEUTRON_CONF_DIR}/neutron.conf" ]]; then
    # Make a copy of the original supposed empty configuration file
    cp "${NEUTRON_CONF_DIR}/neutron.conf" "${NEUTRON_CONF_DIR}/neutron.conf.orig"
fi

# oslo.config also supports Config Opts From Environment, log them to stdout
echo 'Options set from Environment variables'
env | grep "^OS_" || true

# The original neutron.conf is empty, and can be found in neutron.conf_orig
render_j2_config "/etc/neutron/neutron.conf.j2" \
    "${NEUTRON_CONF_DIR}/neutron.conf"

configure_json_rpc_auth

# Make sure neutron traffic bypasses any proxies
export NO_PROXY="${NO_PROXY:-},$NEUTRON_IP"
