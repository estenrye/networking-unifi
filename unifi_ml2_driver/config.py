# Copyright 2016 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from oslo_config import cfg
from oslo_log import log as logging

CONF = cfg.CONF
LOG = logging.getLogger(__name__)

unifi_opts = [
    # Controller connection settings
    cfg.StrOpt('host',
                default='10.0.0.1',
                help='Host of the UniFi Network controller'),
    cfg.IntOpt('port',
                default=8443,
                help='Port of the UniFi Network controller'),
    cfg.StrOpt('apikey',
               secret=True,
               help='API key for UniFi controller authentication. '
                    'If set, username and password are ignored.'),
    cfg.StrOpt('username',
               help='Username for UniFi controller authentication'),
    cfg.StrOpt('password',
               secret=True,
               help='Password for UniFi controller authentication'),
    cfg.StrOpt('site',
               default='default',
               help='UniFi site name to manage'),
    cfg.StrOpt('default_firewall_zone',
               default=None,
               help='Name of the UniFi firewall zone that VLAN networks '
                    'created by this driver should be assigned to. If '
                    'unset, no firewall zone assignment is made.'),
    cfg.BoolOpt('verify_ssl',
                default=True,
                help='Verify SSL certificates for UniFi controller connection'),
    cfg.StrOpt('cafile',
                help='CA certificate file for SSL verification'),

    # Port configuration
    cfg.StrOpt('port_name_format',
               default='openstack-port-{port_id}',
               help='Format string for port names. Available variables: '
                    '{port_id}, {network_id}, {segmentation_id}'),
    cfg.StrOpt('port_description_format',
               default='OpenStack port {port_id}',
               help='Format string for port descriptions'),
    
    # Operation retry settings
    cfg.IntOpt('api_retry_count',
               default=3,
               help='Number of times to retry API calls'),
    cfg.IntOpt('port_setup_retry_count',
               default=3,
               help='Number of times to retry port setup operations'),
    cfg.IntOpt('port_setup_retry_interval',
               default=1,
               help='Interval between port setup retries in seconds'),

    # Feature flags
    cfg.BoolOpt('sync_startup',
                default=True,
                help='Sync networks and ports on startup, and periodically '
                     'thereafter (see sync_interval). Also the sole gate for '
                     'the periodic reconciliation loop -- running it '
                     'periodically without an initial pass at startup '
                     'doesn\'t make sense.'),
    cfg.IntOpt('sync_interval',
               default=300,
               help='Seconds between periodic reconciliation passes. Only '
                    'meaningful when sync_startup is true.'),
    cfg.StrOpt('nat66_tag',
               default='nat66=true',
               help='Tag that marks an IPv6 subnet for NAT66 masquerade. '
                    'A subnet carrying this exact tag gets a UniFi NAT '
                    'policy (Policy Engine -> Policy Table, type '
                    'MASQUERADE) masquerading its CIDR behind the '
                    'resolved egress interface (see '
                    'nat66_egress_tag_prefix/nat66_egress_interface); '
                    'removing the tag removes the policy.'),
    cfg.StrOpt('nat66_egress_tag_prefix',
               default='nat66-egress=',
               help='Tag prefix for a per-subnet NAT66 egress interface '
                    'override, e.g. a subnet tagged '
                    '"nat66-egress=Route64.org" masquerades through the '
                    'UniFi network or VPN tunnel named "Route64.org" '
                    '(confirmed live: VPN tunnels are networkconf '
                    'entries too, e.g. purpose=vpn-client) regardless of '
                    'nat66_egress_interface. Only consulted on subnets '
                    'already carrying nat66_tag.'),
    cfg.StrOpt('nat66_egress_interface',
               default=None,
               help='Exact name of the UniFi network or VPN tunnel '
                    '(a networkconf, matched by name regardless of '
                    'purpose -- WAN, VPN client/server, etc.) to use as '
                    'the default out_interface for NAT66 policies on '
                    'subnets with no per-subnet nat66_egress_tag_prefix '
                    'tag. If unset, the sole purpose=wan network is used '
                    'automatically when there is exactly one; with more '
                    'than one WAN and no per-subnet override, NAT66 sync '
                    'is skipped for that subnet (logged) rather than '
                    'guessing which uplink to masquerade behind.'),
    cfg.BoolOpt('use_all_networks_for_trunk',
                default=True,
                help='Use "All Networks" option for trunk ports'),
    cfg.BoolOpt('enable_port_security',
                default=True,
                help='Enable port security features like MAC address filtering'),
    cfg.BoolOpt('enable_qos',
                default=False,
                help='Enable QoS features on ports'),
    cfg.IntOpt('default_bandwidth_limit',
                default=0,
                help='Default bandwidth limit in Kbps (0 means unlimited)'),

    # Storm control settings
    cfg.BoolOpt('enable_storm_control',
                default=False,
                help='Enable storm control on ports'),
    cfg.IntOpt('storm_control_broadcasting',
                default=0,
                help='Storm control threshold for broadcast traffic (0-100%)'),
    cfg.IntOpt('storm_control_multicasting',
                default=0,
                help='Storm control threshold for multicast traffic (0-100%)'),
    cfg.IntOpt('storm_control_unknown_unicast',
                default=0,
                help='Storm control threshold for unknown unicast traffic (0-100%)'),

    # Monitoring settings
    cfg.BoolOpt('monitor_port_state',
                default=True,
                help='Monitor port state and update OpenStack port status'),
    cfg.IntOpt('monitor_interval',
                default=60,
                help='Interval in seconds to monitor port state'),

    # DNS integration
    cfg.BoolOpt('dns_integration_enabled',
                default=False,
                help='Enable DNS integration with UniFi controller'),
    cfg.StrOpt('dns_domain',
                help='Base DNS domain for port DNS entries'),
    cfg.StrOpt('dns_domain_format',
                default='{dns_name}.{dns_domain}',
                help='Format string for DNS domain names. Available variables: '
                     '{port_id}, {network_id}, {dns_name}, {dns_domain}')
]

coordination_opts = [
    cfg.StrOpt('backend_url',
               secret=True,
               help='The backend URL to use for distributed coordination.'),
    cfg.IntOpt('acquire_timeout',
               min=0,
               default=60,
               help='Timeout in seconds after which an attempt to grab a lock '
                    'is failed. Value of 0 is forever.'),
]

ngs_opts = [
    cfg.StrOpt('session_log_file',
               default=None,
               help='Netmiko session log file.')
]

CONF.register_opts(unifi_opts, group="unifi")
CONF.register_opts(coordination_opts, group='ngs_coordination')
CONF.register_opts(ngs_opts, group='ngs')
