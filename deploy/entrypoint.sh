#!/usr/bin/env bash

set -ex
COMMAND="${*:-start}"

NEUTRON_CONF="${NEUTRON_CONF:-/etc/neutron/neutron.conf}"

# Build MariaDB connection string if MariaDB is enabled
if [[ "${NEUTRON_USE_MARIADB}" == true ]]; then
    if [[ -z "${MARIADB_PASSWORD:-}" ]]; then
        echo "FATAL: NEUTRON_USE_MARIADB requires password"
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

if [ -n "${MARIADB_CONNECTION}" ]; then
  crudini --set "${NEUTRON_CONF}" database connection "${MARIADB_CONNECTION}"
fi

crudini --set "${NEUTRON_CONF}" DEFAULT auth_strategy noauth

tee /etc/sudoers <<EOF
Defaults	env_reset
Defaults	mail_badpass
Defaults	secure_path="/var/lib/openstack/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Defaults	use_pty
root	ALL=(ALL:ALL) ALL
%admin ALL=(ALL) ALL
%sudo	ALL=(ALL:ALL) ALL

Defaults:neutron secure_path = "/var/lib/openstack/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
neutron ALL = (root) NOPASSWD: /usr/bin/neutron-rootwrap /etc/neutron/rootwrap.conf *
@includedir /etc/sudoers.d
EOF

tee /var/lib/openstack/etc/neutron/rootwrap.conf <<EOF
[DEFAULT]
filters_path=/etc/neutron/rootwrap.d,/usr/share/neutron/rootwrap,/var/lib/openstack/etc/neutron/rootwrap.d
exec_dirs=/sbin,/usr/sbin,/bin,/usr/bin,/usr/local/bin,/usr/local/sbin,/etc/neutron/kill_scripts,/var/lib/openstack/bin
use_syslog=False
syslog_log_facility=syslog
syslog_log_level=ERROR
daemon_timeout=600
rlimit_nofile=1024
EOF

tee /etc/supervisord.conf <<EOF
[supervisord]
nodaemon=true
user=root
environment = PATH="/var/lib/openstack/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

[program:uwsgi_neutron-api]
stdout_logfile_maxbytes = 0
stdout_logfile = /dev/stdout
stderr_logfile_maxbytes = 0
stderr_logfile = /dev/stderr
command=/var/lib/openstack/bin/uwsgi
        --http-socket=":9696" --add-header="Connection: close"
        --buffer-size=65535 --die-on-term --enable-threads
        --hook-master-start="unix_signal:15 gracefully_kill_them_all"
        --lazy-apps --module wsgi:application
        --log-x-forwarded-for --master --procname-prefix-spaced="neutron-api:"
        --worker-reload-mercy=80 --thunder-lock
        --wsgi-file /var/lib/openstack/bin/neutron-api
        --uid neutron --gid neutron

[program:ironic-neutron-agent]
stdout_logfile_maxbytes = 0
stdout_logfile = /dev/stdout
stderr_logfile_maxbytes = 0
stderr_logfile = /dev/stderr
command=/var/lib/openstack/bin/ironic-neutron-agent --config-dir /etc/neutron --config-file /etc/neutron/plugins/ml2/ironic_neutron_agent.ini --log-file /dev/stdout

EOF

function start () {
  if [ -d "/var/lib/openstack/bin" ] && [ -f "/var/lib/openstack/bin/activate" ]; then
    SITE_PACKAGES_DIR="/var/lib/openstack/lib/python3.12/site-packages"
    if [ ! -d "${SITE_PACKAGES_DIR}/supervisor" ]; then
      echo "Installing additional Python packages..."
      # shellcheck source=/dev/null
      source "/var/lib/openstack/bin/activate"
      pip install supervisor
      deactivate
    fi
  fi

  if [ -z "${SKIP_NEUTRON_DB_SYNC}" ]; then
    echo "Syncing database schema..."
    neutron-db-manage --config-file "${NEUTRON_CONF}" upgrade heads || {
      echo "ERROR: Database sync failed"
    }
  else
    echo "Skipping database sync as SKIP_GLANCE_DB_SYNC is set"
  fi

  if dpkg -l | grep -q libpcre3-dev; then
    echo "libpcre3-dev is already installed."
  else
    echo "Installing libpcre3-dev..."
    apt update -qq && apt install -yq libpcre3-dev
  fi

  exec /var/lib/openstack/bin/supervisord --nodaemon -c /etc/supervisord.conf
}

function stop () {
  kill -TERM 1
}

$COMMAND
