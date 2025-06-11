#!/usr/bin/env bash

set -ex
COMMAND="${*:-start}"

NEUTRON_CONF="${NEUTRON_CONF:-/etc/neutron/neutron.conf}"

printf '127.0.0.1\t%s\n127.0.0.1\t%s\n' "$(hostname)" "rabbitmq" >> /etc/hosts

# tee > /etc/neutron/dhcp_agent.ini << EOF
# [default]
# dnsmasq_config_file = /etc/neutron/dnsmasq.conf
# force_metadata = True
# interface_driver = openvswitch

# [ovs]
# ovsdb_connection = unix:/var/run/openvswitch/db.sock
# EOF

tee > /etc/neutron/neutron-agent.conf << EOF
[DEFAULT]
host = $(hostname --fqdn)
EOF

tee /etc/sudoers <<EOF
Defaults	env_reset
Defaults	mail_badpass
Defaults	secure_path="/var/lib/openstack/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Defaults	use_pty
root	ALL=(ALL:ALL) NOPASSWD:ALL
%admin ALL=(ALL) NOPASSWD:ALL
%sudo	ALL=(ALL:ALL) NOPASSWD:ALL
@includedir /etc/sudoers.d
EOF

tee /etc/sudoers.d/neutron <<EOF
Defaults !requiretty
Defaults secure_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin:/var/lib/openstack/bin:/var/lib/kolla/venv/bin"
neutron ALL = (root) NOPASSWD: /var/lib/kolla/venv/bin/neutron-rootwrap /etc/neutron/rootwrap.conf *, /var/lib/openstack/bin/neutron-rootwrap /etc/neutron/rootwrap.conf *
neutron ALL = (root) NOPASSWD: /var/lib/kolla/venv/bin/neutron-rootwrap-daemon /etc/neutron/rootwrap.conf, /var/lib/openstack/bin/neutron-rootwrap-daemon /etc/neutron/rootwrap.conf
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
logfile=/tmp/supervisord.log
logfile_maxbytes=50MB
logfile_backups=3
loglevel=info
pidfile=/tmp/supervisord.pid
minfds=1024
minprocs=200
nodaemon=true
user=root

[supervisorctl]
serverurl = unix:///tmp/supervisor.sock

[program:neutron-metadata-agent]
command=/var/lib/openstack/bin/neutron-metadata-agent --config-file /etc/neutron/neutron.conf --config-file /etc/neutron/metadata_agent.ini --log-file /dev/stdout
stdout_logfile_maxbytes = 0
stdout_logfile = /dev/stdout
stderr_logfile_maxbytes = 0
stderr_logfile = /dev/stderr
redirect_stderr=true

[program:neutron-l3-agent]
command=/var/lib/openstack/bin/neutron-l3-agent --config-file /etc/neutron/neutron.conf --config-file /etc/neutron/l3_agent.ini --log-file /dev/stdout
stdout_logfile_maxbytes = 0
stdout_logfile = /dev/stdout
stderr_logfile_maxbytes = 0
stderr_logfile = /dev/stderr
redirect_stderr=true

[program:neutron-openvswitch-agent]
command=/var/lib/openstack/bin/neutron-openvswitch-agent --config-file /etc/neutron/neutron.conf --config-file /etc/neutron/plugins/ml2/openvswitch_agent.ini --log-file /dev/stdout
stdout_logfile_maxbytes = 0
stdout_logfile = /dev/stdout
stderr_logfile_maxbytes = 0
stderr_logfile = /dev/stderr
redirect_stderr=true

[program:neutron-dhcp-agent]
command=/var/lib/openstack/bin/neutron-dhcp-agent --config-file /etc/neutron/neutron.conf --config-file /etc/neutron/dhcp_agent.ini --log-file /dev/stdout
stdout_logfile_maxbytes = 0
stdout_logfile = /dev/stdout
stderr_logfile_maxbytes = 0
stderr_logfile = /dev/stderr
redirect_stderr=true
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

  exec /var/lib/openstack/bin/supervisord --nodaemon -c /etc/supervisord.conf
}

function stop () {
  kill -TERM 1
}

$COMMAND
