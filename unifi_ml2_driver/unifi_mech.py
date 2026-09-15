# Copyright 2015 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
UniFi ML2 Driver for OpenStack Neutron

This driver implements network operations for UniFi switches
managed through a UniFi Network controller.
"""

import asyncio
from contextlib import contextmanager
import ipaddress
import re
import threading
import time

from neutron.db import provisioning_blocks
from neutron_lib.api.definitions import dns as dns_apidef
from neutron_lib.api.definitions import portbindings
from neutron_lib import constants as n_const
from neutron_lib import context as n_context
from neutron_lib.callbacks import resources, events
from neutron_lib.plugins.ml2 import api
from neutron_lib.plugins import directory
from neutron_lib.plugins import constants as plugin_constants
from oslo_config import cfg
from oslo_log import log as logging
from oslo_service import loopingcall

from unifi_ml2_driver import exceptions
from unifi_ml2_driver.dns_handler import UnifiDnsHandler
from unifi_ml2_driver.unifi_api import get_unifi_api
from aiounifi.errors import AiounifiException
from aiounifi.models.network import NetworkCreateRequest, NetworkDeleteRequest, NetworkUpdateRequest, Network, TypedNetwork
from aiounifi.models.firewall_zone import FirewallZoneUpdateRequest, TypedFirewallZone
from aiounifi.models.device import Device, DeviceListRequest, TypedDevicePortOverrides, DeviceSetPortProfileRequest
from unifi_ml2_driver import trunk_driver
from unifi_ml2_driver.nat_policy import (
    NatPolicyCreateRequest, NatPolicyDeleteRequest, NatPolicyListRequest,
    NatPolicyUpdateRequest, TypedNatPolicy,
)

LOG = logging.getLogger(__name__)

from unifi_ml2_driver.config import CONF

class UnifiMechDriver(api.MechanismDriver):
    """UniFi Mechanism Driver for ML2 plugin.

    This driver manages VLANs and port configurations on UniFi switches
    through a UniFi Network controller.
    """

    _ORPHAN_NETWORK_NAME_RE = re.compile(
        r'^OpenStack-(?P<netid>[0-9a-f-]{36})-VLAN(?P<vlan>\d+)$')
    _NAT66_POLICY_DESCRIPTION_RE = re.compile(
        r'^OpenStack-NAT66-(?P<subnetid>[0-9a-f-]{36})$')

    def __init__(self):
        self.controller = None
        self.switches = {}
        self.port_mappings = {}
        self.vif_details = {portbindings.VIF_DETAILS_CONNECTIVITY:
                            portbindings.CONNECTIVITY_L2}
        self.trunk_driver = None
        self._controllers = {}
        self._sync_loop = None
        self.dns_handler = UnifiDnsHandler(self)

    @property
    def connectivity(self): # type: ignore
        return portbindings.CONNECTIVITY_L2

    def initialize(self):
        """Perform driver initialization.

        Called after all drivers have been loaded and the database has
        been initialized. No abstract methods defined below will be
        called prior to this method being called.
        """
        LOG.info("Initializing UniFi ML2 driver")

        # Initialize trunk driver if available
        self.trunk_driver = trunk_driver.UnifiTrunkDriver.create(self)

        # Verify we have required configuration
        if not CONF.unifi.host:
            LOG.warning("UniFi controller URL not configured. Driver disabled.")
            return

        # apikey and username/password are alternative auth methods (see
        # CONF.unifi.apikey's help text: "If set, username and password
        # are ignored") -- this previously only ever checked for
        # username/password, so it always warned "disabled" and returned
        # early on an apikey-only config (this exact cluster's config,
        # confirmed live) without actually disabling anything: every
        # postcommit hook calls _get_controller() independently of
        # initialize() and worked fine regardless. The one real
        # consequence was that the sync_startup reconciliation loop below
        # could never start, since this return happens before reaching it.
        if not CONF.unifi.apikey and not (CONF.unifi.username and CONF.unifi.password):
            LOG.warning("UniFi credentials not configured (no apikey or "
                       "username/password). Driver disabled.")
            return

        # Test connection to controller
        try:
            with self._get_controller() as controller:
                LOG.info("Successfully connected to UniFi controller at %s",
                         CONF.unifi.host)
        except Exception as e:
            LOG.error("Failed to connect to UniFi controller: %s", e)
            # Don't raise - let the driver remain initialized but inactive
            return

        # Reconcile Neutron's VLAN networks/subnets with UniFi on startup,
        # then periodically thereafter. This is the only way the driver
        # ever observes some kinds of drift -- notably, Neutron's tag API
        # never invokes any ML2 mechanism-driver hook at all, so a tag
        # change (e.g. marking a subnet for the not-yet-implemented NAT66
        # masquerade feature) is silent from this driver's perspective
        # except here. sync_startup gates the whole reconciliation system.
        #
        # Deliberately NOT calling self._sync_networks() synchronously
        # here first: this whole method runs from inside
        # Ml2Plugin.__init__ (mechanism_manager.initialize() is called
        # partway through it), and neutron/manager.py registers the core
        # plugin in the directory (directory.add_plugin(CORE, plugin))
        # only *after* that __init__ call returns -- confirmed by reading
        # both files directly. directory.get_plugin(CORE) is therefore
        # guaranteed to return None if called synchronously from here, so
        # a startup-time call to _sync_networks() would always silently
        # no-op. Instead, the loop's own first tick (short initial_delay)
        # serves as "the startup sync" -- by the time it actually fires,
        # __init__ has long since returned and the plugin is registered.
        if CONF.unifi.sync_startup:
            self._sync_loop = loopingcall.FixedIntervalLoopingCall(
                self._sync_networks)
            self._sync_loop.start(interval=CONF.unifi.sync_interval,
                                  initial_delay=10)

    def _get_controller(self):
        """Get or create a UniFi controller connection.
        
        Returns:
            A context manager that yields a controller client
        """
        if CONF.unifi.host not in self._controllers:
            # Empty dict for config since we're using CONF directly in get_unifi_api
            self._controllers[CONF.unifi.host] = {}

        return self._get_api(CONF.unifi.host)

    @contextmanager
    def _get_api(self, controller_id):
        """Get a UniFi API client using the async helper.
        
        Args:
            controller_id: Controller identifier (usually URL)
            
        Returns:
            A UniFi controller client
        """
        config = self._controllers[controller_id]
        
        # Set up event loop for async calls
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            controller = loop.run_until_complete(get_unifi_api(config))
            yield controller
        finally:
            # Clean up
            loop.close()

    def _sync_networks(self):
        """Reconcile Neutron's VLAN networks/subnets with the UniFi controller.

        Called periodically (if CONF.unifi.sync_startup) via the
        FixedIntervalLoopingCall started in initialize(), including a
        first call shortly after startup that serves as the "sync on
        startup" pass -- see initialize()'s comment for why that first
        call can't happen synchronously during driver initialization
        itself. Idempotent and best-effort throughout: a single bad
        resource or a transient UniFi/Neutron API error must never abort
        the whole pass or crash the loop -- the next cycle simply retries.
        """
        LOG.debug("Sync: starting reconciliation pass")
        # Everything below is inside one try/except on purpose: this method
        # is called directly from a FixedIntervalLoopingCall, which stops
        # the loop entirely if the callable it wraps ever raises -- so an
        # uncaught exception here wouldn't just skip one cycle, it would
        # silently disable all future reconciliation for the life of the
        # process. Nothing after this point may propagate out of this
        # method, including plugin/context setup, not just the per-resource
        # work below (which has its own finer-grained try/except so one bad
        # network or subnet doesn't even take down the rest of its cycle).
        try:
            plugin = directory.get_plugin(plugin_constants.CORE)
            ctx = n_context.get_admin_context()

            try:
                networks = plugin.get_networks(ctx)
            except Exception as e:
                LOG.error('Sync: failed to list Neutron networks, skipping '
                         'this reconciliation cycle: %s', e)
                return

            vlan_networks = [
                n for n in networks
                if n.get('provider:network_type') == 'vlan' and not n.get('router:external')
            ]
            # Only from a *successful* listing above -- an empty/partial set
            # from a failed listing would otherwise make every UniFi network
            # look orphaned to _cleanup_orphaned_networks.
            known_network_ids = {n['id'] for n in vlan_networks}

            with self._get_controller() as controller:
                loop = asyncio.get_event_loop()
                loop.run_until_complete(controller.networks.update())
                loop.run_until_complete(controller.firewall_zones.update())

                # NAT66 lookup structures, built once per pass rather than
                # per subnet: a name->network map covering every networkconf
                # (WAN uplinks and VPN tunnels are both networkconf entries,
                # confirmed live -- purpose "wan" and "vpn-client"
                # respectively), and the sole WAN if there's exactly one
                # (the auto-detect fallback; deliberately never auto-detects
                # a VPN tunnel, only ever used when explicitly named).
                all_networks_by_name = {
                    net.name: net for _, net in controller.networks.items()
                }
                wan_networks = [
                    net for _, net in controller.networks.items() if net.purpose == 'wan'
                ]
                default_wan_id = wan_networks[0].id if len(wan_networks) == 1 else None

                nat_policies = None
                try:
                    # ApiRequestV2.decode() always wraps a V2 response as
                    # {"meta": ..., "data": [...]}, even though a bare GET
                    # against this endpoint returns a plain JSON array --
                    # confirmed by reading the installed aiounifi package
                    # directly. Unwrap it here; every other place in this
                    # file that touches a V2 endpoint goes through
                    # aiounifi's own cached APIHandler (.items()), which
                    # already does this unwrapping internally, so this is
                    # the first call site in this driver that has to do it
                    # by hand.
                    nat_policies = loop.run_until_complete(
                        controller.request(NatPolicyListRequest.create())
                    ).get('data', [])
                except Exception as e:
                    LOG.error('Sync: failed to list NAT policies, skipping '
                             'NAT66 sync this cycle: %s', e)

                known_tagged_subnet_ids = set()

                for network in vlan_networks:
                    segmentation_id = network.get('provider:segmentation_id')
                    if not segmentation_id:
                        continue

                    network_id = network['id']
                    try:
                        self._reconcile_network(controller, loop, network, refresh=False)
                    except Exception as e:
                        LOG.error('Sync: failed to reconcile network %s (VLAN %s): %s',
                                 network_id, segmentation_id, e)
                        continue

                    try:
                        subnets = plugin.get_subnets(
                            ctx, filters={'network_id': [network_id]})
                    except Exception as e:
                        LOG.error('Sync: failed to list subnets for network %s: %s',
                                 network_id, e)
                        continue

                    for subnet in subnets:
                        subnet_id = subnet['id']
                        try:
                            self._reconcile_subnet(
                                controller, loop, subnet, network, refresh=False)
                        except Exception as e:
                            LOG.error('Sync: failed to reconcile subnet %s: %s',
                                     subnet_id, e)
                            continue

                        if subnet.get('ip_version') == 6 and nat_policies is not None:
                            tagged = CONF.unifi.nat66_tag in (subnet.get('tags') or [])
                            try:
                                if tagged:
                                    known_tagged_subnet_ids.add(subnet_id)
                                    egress_interface_id = self._resolve_nat66_egress_interface(
                                        subnet, default_wan_id, all_networks_by_name)
                                    if egress_interface_id:
                                        self._reconcile_nat66_policy(
                                            controller, loop, subnet,
                                            egress_interface_id, nat_policies)
                                    else:
                                        LOG.warning(
                                            'Sync: subnet %s is tagged for NAT66 but no '
                                            'egress interface could be resolved -- '
                                            'leaving any existing policy unchanged',
                                            subnet_id)
                                else:
                                    self._remove_nat66_policy(
                                        controller, loop, subnet_id, nat_policies)
                            except Exception as e:
                                LOG.error('Sync: failed to reconcile NAT66 policy for '
                                         'subnet %s: %s', subnet_id, e)

                self._cleanup_orphaned_networks(controller, loop, known_network_ids)
                if nat_policies is not None:
                    try:
                        self._cleanup_orphaned_nat66_policies(
                            controller, loop, nat_policies, known_tagged_subnet_ids)
                    except Exception as e:
                        LOG.error('Sync: NAT66 orphan cleanup failed: %s', e)

        except Exception as e:
            LOG.error('Sync: reconciliation pass failed: %s', e)

    def _cleanup_orphaned_networks(self, controller, loop, known_network_ids):
        """Delete UniFi networks this driver created whose Neutron network is gone.

        Only ever touches UniFi networks whose name matches this driver's
        own naming convention (OpenStack-<uuid>-VLAN<id>, see
        _ORPHAN_NETWORK_NAME_RE) -- that's the safety boundary that keeps
        this from ever considering a manually created UniFi network for
        deletion. known_network_ids must come from a successful Neutron
        network listing in the same reconciliation pass; see the caller.

        Args:
            controller: An active UniFi controller client
            loop: The asyncio event loop to run requests on
            known_network_ids: Set of Neutron network ids currently known
                to exist (VLAN, non-external)
        """
        for _, unifi_network in list(controller.networks.items()):
            match = self._ORPHAN_NETWORK_NAME_RE.match(unifi_network.name)
            if not match:
                continue

            network_id = match.group('netid')
            if network_id in known_network_ids:
                continue

            try:
                self._unassign_network_from_default_zone(controller, loop, unifi_network.id)
                loop.run_until_complete(
                    controller.request(NetworkDeleteRequest.create(unifi_network.id))
                )
                LOG.info('Sync: deleted orphaned UniFi network %s (%r) -- no '
                        'matching Neutron network %s', unifi_network.id,
                        unifi_network.name, network_id)
            except Exception as e:
                LOG.error('Sync: failed to delete orphaned UniFi network %s (%r): %s',
                         unifi_network.id, unifi_network.name, e)

    def _resolve_nat66_egress_interface(self, subnet, default_wan_id, all_networks_by_name):
        """Resolve which UniFi network/VPN tunnel a subnet's NAT66 policy should egress through.

        Resolution order:
        1. A per-subnet CONF.unifi.nat66_egress_tag_prefix tag, matched by
           exact name against every networkconf (WAN uplinks and VPN
           tunnels are both networkconf entries -- confirmed live,
           purpose "wan" and "vpn-client" respectively -- so this works
           for either without needing to know which kind it is).
        2. CONF.unifi.nat66_egress_interface (site-wide default), same
           any-purpose name match.
        3. The sole purpose=="wan" network, if there's exactly one.
           Deliberately never auto-detects a VPN tunnel -- only used when
           explicitly named via (1) or (2).

        Returns None if nothing resolves. Callers decide what that means
        for their own call site (skip create/update; a removal doesn't
        need this at all).

        Args:
            subnet: The Neutron subnet dict
            default_wan_id: The sole WAN network's UniFi _id, or None if
                zero or more than one WAN network exists
            all_networks_by_name: dict of {network name: aiounifi Network}
                covering every networkconf, built once per reconciliation
                pass
        """
        tag_prefix = CONF.unifi.nat66_egress_tag_prefix
        for tag in subnet.get('tags') or []:
            if tag.startswith(tag_prefix):
                name = tag[len(tag_prefix):]
                match = all_networks_by_name.get(name)
                if match:
                    return match.id
                LOG.warning('Sync: subnet %s requests NAT66 egress interface %r '
                           'via tag, but no UniFi network or VPN tunnel with '
                           'that exact name exists', subnet['id'], name)
                return None

        configured_name = CONF.unifi.nat66_egress_interface
        if configured_name:
            match = all_networks_by_name.get(configured_name)
            if match:
                return match.id
            LOG.warning('Sync: nat66_egress_interface %r not found among UniFi '
                       'networks/VPN tunnels', configured_name)
            return None

        return default_wan_id

    def _nat66_policy_description(self, subnet_id):
        """Naming convention used to find this driver's own NAT66 policies.

        The `description` field is the only reliable identity anchor
        available on a NAT policy -- there's no dedicated field to stash
        a Neutron subnet id the way networkconf's `name` does for VLANs.
        """
        return f"OpenStack-NAT66-{subnet_id}"

    def _reconcile_nat66_policy(self, controller, loop, subnet, egress_interface_id, nat_policies):
        """Ensure a NAT66 masquerade policy exists and is correct for `subnet`.

        Only called when the subnet is tagged for NAT66 and an egress
        interface was successfully resolved for it -- removal (untagged)
        is handled separately by _remove_nat66_policy, and "tagged but
        unresolvable" is handled by the caller leaving things alone
        entirely, never here.

        Args:
            controller: An active UniFi controller client
            loop: The asyncio event loop to run requests on
            subnet: The Neutron subnet dict (ip_version 6)
            egress_interface_id: The UniFi networkconf _id to masquerade behind
            nat_policies: The full NAT policy list fetched once for this
                reconciliation pass (may be slightly stale by the time a
                later subnet in the same pass reads it, which is fine --
                each subnet's description key is unique, so this never
                causes cross-subnet interference)
        """
        subnet_id = subnet['id']
        cidr = subnet.get('cidr')
        if not cidr:
            return

        description = self._nat66_policy_description(subnet_id)
        existing = next(
            (p for p in nat_policies if p.get('description') == description), None)

        desired_source_filter = {
            'filter_type': 'ADDRESS_AND_PORT',
            'address': cidr,
            'firewall_group_ids': [],
            'invert_address': False,
            'invert_port': False,
        }

        if existing:
            if (existing.get('source_filter', {}).get('address') == cidr
                    and existing.get('out_interface') == egress_interface_id
                    and existing.get('enabled')):
                return

            # Minimal payload confirmed live: only these fields need to be
            # sent on write, plus _id for the update path -- is_predefined/
            # setting_preference are confirmed GET-only (a from-scratch
            # PUT omitting them, built explicitly like this rather than
            # via dict(existing), is what was actually tested live; naively
            # copying `existing` would silently reintroduce them and risk
            # the same "Unrecognized field" rejection firewall zones hit
            # for their own GET-only fields -- see nat_policy.py's module
            # docstring). rule_index is the one field pulled from
            # `existing` on purpose: _write_nat66_policy_with_retry needs
            # a starting guess and handles it being wrong or absent.
            updated = {
                '_id': existing['_id'],
                'description': description,
                'destination_filter': existing.get('destination_filter') or {
                    'filter_type': 'NONE', 'firewall_group_ids': [],
                    'invert_address': False, 'invert_port': False,
                },
                'enabled': True,
                'exclude': existing.get('exclude', False),
                'ip_version': 'IPV6',
                'logging': existing.get('logging', False),
                'out_interface': egress_interface_id,
                'pppoe_use_base_interface': existing.get('pppoe_use_base_interface', False),
                'protocol': existing.get('protocol', 'all'),
                'rule_index': existing.get('rule_index'),
                'source_filter': desired_source_filter,
                'type': 'MASQUERADE',
            }
            self._write_nat66_policy_with_retry(
                controller, loop, NatPolicyUpdateRequest.create, updated)
            LOG.info('Sync: corrected drifted NAT66 policy for subnet %s', subnet_id)
            return

        new_policy = {
            'description': description,
            'destination_filter': {
                'filter_type': 'NONE',
                'firewall_group_ids': [],
                'invert_address': False,
                'invert_port': False,
            },
            'enabled': True,
            'exclude': False,
            'ip_version': 'IPV6',
            'logging': False,
            'out_interface': egress_interface_id,
            'pppoe_use_base_interface': False,
            'protocol': 'all',
            'source_filter': desired_source_filter,
            'type': 'MASQUERADE',
        }
        created = self._write_nat66_policy_with_retry(
            controller, loop, NatPolicyCreateRequest.create, new_policy)
        # Feed the real created object (server-assigned _id included) back
        # into the shared per-pass nat_policies list, so a second subnet
        # newly tagged in this same cycle sees it (both when computing
        # its own initial rule_index guess, and, more importantly, so
        # this one doesn't look orphaned to _cleanup_orphaned_nat66_policies
        # later in the same pass, which only sees this pass's original
        # snapshot otherwise).
        if created:
            nat_policies.append(created)
        LOG.info('Sync: created NAT66 masquerade policy for subnet %s (%s)',
                subnet_id, cidr)

    def _write_nat66_policy_with_retry(self, controller, loop, request_factory, policy, max_attempts=50):
        """POST/PUT a NAT policy, incrementing rule_index on a collision.

        Confirmed live, twice: (1) omitting rule_index on create does NOT
        get the UDM-SE to auto-assign a free slot the way omitting _id
        does -- it silently defaults to a value that collides with an
        already-existing policy as soon as more than one exists
        ("api.err.NatRuleInvalidParameters: duplicate rule_index", HTTP
        400). (2) Computing a value from what GET reports isn't reliable
        either: a policy already occupies its own slot on the server the
        moment it's rejected an explicit rule_index far outside any
        value another policy's GET response ever showed -- GET simply
        doesn't always echo rule_index back, even when the server has one
        recorded for that policy. Rather than trying to predict a safe
        value from unreliable data, start from a guess (0, or one past
        the highest value any policy's GET response *does* report) and
        let the server's own rejection tell us definitively when a slot
        is taken, retrying with the next integer until one succeeds.
        Order doesn't matter for masquerade rules targeting disjoint
        subnet CIDRs, only uniqueness does, so any free slot is fine.

        Args:
            controller: An active UniFi controller client
            loop: The asyncio event loop to run requests on
            request_factory: NatPolicyCreateRequest.create or
                NatPolicyUpdateRequest.create
            policy: The policy dict to write (without rule_index, or with
                a starting guess -- either way it may be overwritten
                before the request that actually succeeds)

        Returns:
            The written policy as the server returned it, or None if the
            response didn't include one.

        Raises:
            AiounifiException: if no free rule_index was found within
                max_attempts, or the server rejected the write for any
                other reason.
        """
        policy = dict(policy)
        if policy.get('rule_index') is None:
            policy['rule_index'] = 0

        for _ in range(max_attempts):
            try:
                response = loop.run_until_complete(
                    controller.request(request_factory(TypedNatPolicy(policy))))
                return (response.get('data') or [None])[0]
            except AiounifiException as e:
                error_data = e.args[0] if e.args else {}
                message = error_data.get('message', '') if isinstance(error_data, dict) else ''
                if 'duplicate rule_index' not in message:
                    raise
                policy['rule_index'] += 1

        raise AiounifiException(
            f"Could not find a free NAT policy rule_index after {max_attempts} attempts")

    def _remove_nat66_policy(self, controller, loop, subnet_id, nat_policies):
        """Delete subnet_id's NAT66 policy if one exists (subnet is untagged).

        Args:
            controller: An active UniFi controller client
            loop: The asyncio event loop to run requests on
            subnet_id: The Neutron subnet id
            nat_policies: The full NAT policy list fetched once for this
                reconciliation pass -- mutated in place on a successful
                delete (the entry removed) so the orphan-cleanup pass
                later in the same cycle doesn't also try to delete the
                same now-gone policy and log a spurious 404.
        """
        description = self._nat66_policy_description(subnet_id)
        existing = next(
            (p for p in nat_policies if p.get('description') == description), None)
        if not existing:
            return

        loop.run_until_complete(
            controller.request(NatPolicyDeleteRequest.create(existing['_id'])))
        nat_policies.remove(existing)
        LOG.info('Sync: removed NAT66 policy for subnet %s (untagged)', subnet_id)

    def _cleanup_orphaned_nat66_policies(self, controller, loop, nat_policies, known_tagged_subnet_ids):
        """Delete NAT66 policies whose subnet no longer exists in Neutron.

        Only ever touches policies whose description matches this
        driver's own naming convention (_NAT66_POLICY_DESCRIPTION_RE) --
        the safety boundary against ever touching a manually created NAT
        policy, same reasoning as _cleanup_orphaned_networks. A subnet
        that still exists but had its nat66_tag removed is already
        handled by _remove_nat66_policy in the main per-subnet loop; this
        is specifically for a subnet deleted from Neutron entirely, which
        never appears in that loop at all and would otherwise leave its
        NAT policy dangling forever.

        known_tagged_subnet_ids must come from a successful pass over
        every currently-existing subnet in the same reconciliation cycle
        (see the caller) -- a partial set would otherwise make every
        currently-tagged subnet's policy look orphaned.
        """
        for policy in nat_policies:
            description = policy.get('description', '')
            match = self._NAT66_POLICY_DESCRIPTION_RE.match(description)
            if not match:
                continue

            subnet_id = match.group('subnetid')
            if subnet_id in known_tagged_subnet_ids:
                continue

            try:
                loop.run_until_complete(
                    controller.request(NatPolicyDeleteRequest.create(policy['_id'])))
                LOG.info('Sync: deleted orphaned NAT66 policy for subnet %s (%r)',
                        subnet_id, description)
            except Exception as e:
                LOG.error('Sync: failed to delete orphaned NAT66 policy for '
                         'subnet %s (%r): %s', subnet_id, description, e)

    def create_network_precommit(self, context):
        """Allocate resources for a new network.

        :param context: NetworkContext instance describing the new
        network.

        Create a new network, allocating resources as necessary in the
        database. Called inside transaction context on session. Call
        cannot block.  Raising an exception will result in a rollback
        of the current transaction.
        """
        # Nothing to do for network precommit
        pass

    def create_network_postcommit(self, context):
        """Create a network.

        :param context: NetworkContext instance describing the new
        network.

        Called after the transaction commits. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance. Raising an exception will
        cause the deletion of the resource.
        """
        network = context.current
        # Only handle networks with segmentation ID (VLANs)
        network_id = network['id']
        
        # Skip non-VLAN networks or external networks
        if network.get('provider:network_type') != 'vlan' or network.get('router:external'):
            return
            
        segmentation_id = network.get('provider:segmentation_id')
        if not segmentation_id:
            return
            
        try:
            with self._get_controller() as controller:
                loop = asyncio.get_event_loop()
                self._reconcile_network(controller, loop, network)
        except Exception as e:
            LOG.error('Failed to create network %s (VLAN %s) in UniFi controller: %s',
                     network_id, segmentation_id, e)
            raise

    def update_network_precommit(self, context):
        """Update resources of a network.

        :param context: NetworkContext instance describing the new
        state of the network, as well as the original state prior
        to the update_network call.

        Update values of a network, updating the associated resources
        in the database. Called inside transaction context on session.
        Raising an exception will result in rollback of the
        transaction.

        update_network_precommit is called for all changes to the
        network state. It is up to the mechanism driver to ignore
        state or state changes that it does not know or care about.
        """
        # Nothing to do for network update precommit
        pass

    def update_network_postcommit(self, context):
        """Update a network.

        :param context: NetworkContext instance describing the new
        state of the network, as well as the original state prior
        to the update_network call.

        Called after the transaction commits. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance. Raising an exception will
        cause the deletion of the resource.

        update_network_postcommit is called for all changes to the
        network state.  It is up to the mechanism driver to ignore
        state or state changes that it does not know or care about.
        """
        # Check if segmentation_id has changed
        network = context.current
        original_network = context.original
        
        if (network.get('provider:network_type') != 'vlan' or 
                original_network.get('provider:network_type') != 'vlan'):
            return
            
        new_segmentation_id = network.get('provider:segmentation_id')
        old_segmentation_id = original_network.get('provider:segmentation_id')
        
        # If VLAN ID hasn't changed, nothing to do
        if new_segmentation_id == old_segmentation_id:
            return
            
        network_id = network['id']
        
        try:
            with self._get_controller() as controller:
                loop = asyncio.get_event_loop()

                # Find the network with the old VLAN ID. NOTE: this now
                # goes through _unifi_network_for_vlan (default
                # refresh=True) instead of reading controller.networks.items()
                # directly without ever calling .update() first, as this
                # code previously did -- on the fresh connection
                # _get_controller() opens per hook invocation, that meant
                # the cache was always empty here, so old_network was
                # always None and the delete-old-network branch below
                # never actually ran. Using the shared helper fixes that
                # as a side effect of the refactor.
                old_network = self._unifi_network_for_vlan(controller, loop, old_segmentation_id)
                if old_network:
                    # Delete old network
                    loop.run_until_complete(
                        controller.request(NetworkDeleteRequest.create(old_network.id))
                    )

                # Create the network fresh under the new VLAN ID (and
                # assign it to the default firewall zone, same as any
                # other create).
                self._reconcile_network(controller, loop, network)

                LOG.info('Network %s updated from VLAN %s to VLAN %s in UniFi controller',
                         network_id, old_segmentation_id, new_segmentation_id)

        except Exception as e:
            LOG.error('Failed to update network %s from VLAN %s to VLAN %s: %s',
                     network_id, old_segmentation_id, new_segmentation_id, e)
            raise

    def delete_network_precommit(self, context):
        """Delete resources for a network.

        :param context: NetworkContext instance describing the current
        state of the network, prior to the call to delete it.

        Delete network resources previously allocated by this
        mechanism driver for a network. Called inside transaction
        context on session. Runtime errors are not expected, but
        raising an exception will result in rollback of the
        transaction.
        """
        # Nothing to do for network delete precommit
        pass

    def delete_network_postcommit(self, context):
        """Delete a network.

        :param context: NetworkContext instance describing the current
        state of the network, prior to the call to delete it.

        Called after the transaction commits. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance. Runtime errors are not
        expected, and will not prevent the resource from being
        deleted.
        """
        network = context.current
        
        # Only handle networks with segmentation ID (VLANs)
        if network.get('provider:network_type') != 'vlan':
            return
            
        segmentation_id = network.get('provider:segmentation_id')
        if not segmentation_id:
            return
            
        network_id = network['id']
        
        try:
            with self._get_controller() as controller:
                loop = asyncio.get_event_loop()
                
                # Find the network with this VLAN ID
                loop.run_until_complete(controller.networks.update())
                networks = controller.networks.items()
                
                target_network = None
                for k, net in networks:
                    if (hasattr(net, 'vlan') and 
                        net.vlan == segmentation_id):
                        target_network = net
                        break
                
                if target_network:
                    self._unassign_network_from_default_zone(controller, loop, target_network.id)

                    # Delete network in UniFi controller
                    loop.run_until_complete(
                        controller.request(NetworkDeleteRequest.create(target_network.id))
                    )
                    LOG.info('Network %s (VLAN %s) has been deleted from UniFi controller',
                             network_id, segmentation_id)
                else:
                    LOG.debug('Network %s (VLAN %s) not found in UniFi controller',
                             network_id, segmentation_id)
                    
        except Exception as e:
            # Log but don't raise to prevent network deletion from failing
            LOG.error('Failed to delete network %s (VLAN %s) from UniFi controller: %s',
                     network_id, segmentation_id, e)

    def create_subnet_precommit(self, context):
        """Allocate resources for a new subnet.

        :param context: SubnetContext instance describing the new
            subnet.

        Create a new subnet, allocating resources as necessary in the
        database. Called inside transaction context on session. Call
        cannot block.  Raising an exception will result in a rollback
        of the current transaction.
        """
        # Nothing to do for subnet precommit
        pass

    def create_subnet_postcommit(self, context):
        """Create a subnet.

        :param context: SubnetContext instance describing the new
            subnet.

        Called after the transaction commits. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance. Raising an exception will
        cause the deletion of the resource.
        """
        subnet = context.current
        network = context.network.current

        # Only handle networks with segmentation ID (VLANs)
        if network.get('provider:network_type') != 'vlan' or network.get('router:external'):
            return

        segmentation_id = network.get('provider:segmentation_id')
        if not segmentation_id:
            return

        subnet_id = subnet['id']

        try:
            with self._get_controller() as controller:
                loop = asyncio.get_event_loop()
                self._reconcile_subnet(controller, loop, subnet, network)
        except Exception as e:
            LOG.error('Failed to sync subnet %s (VLAN %s) to UniFi controller: %s',
                     subnet_id, segmentation_id, e)
            raise

    def update_subnet_precommit(self, context):
        """Update resources of a subnet.

        :param context: SubnetContext instance describing the new
            state of the subnet, as well as the original state prior
            to the update_subnet call.

        Update values of a subnet, updating the associated resources
        in the database. Called inside transaction context on session.
        Raising an exception will result in rollback of the
        transaction.

        update_subnet_precommit is called for all changes to the
        subnet state. It is up to the mechanism driver to ignore
        state or state changes that it does not know or care about.
        """
        # Nothing to do for subnet update precommit
        pass

    def update_subnet_postcommit(self, context):
        """Update a subnet.

        :param context: SubnetContext instance describing the new
            state of the subnet, as well as the original state prior
            to the update_subnet call.

        Called after the transaction commits. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance. Raising an exception will
        cause the deletion of the resource.

        update_subnet_postcommit is called for all changes to the
        subnet state.  It is up to the mechanism driver to ignore
        state or state changes that it does not know or care about.
        """
        subnet = context.current
        network = context.network.current

        # Only handle networks with segmentation ID (VLANs)
        if network.get('provider:network_type') != 'vlan' or network.get('router:external'):
            return

        segmentation_id = network.get('provider:segmentation_id')
        if not segmentation_id:
            return

        subnet_id = subnet['id']

        try:
            with self._get_controller() as controller:
                loop = asyncio.get_event_loop()
                self._reconcile_subnet(controller, loop, subnet, network)
        except Exception as e:
            LOG.error('Failed to sync subnet %s (VLAN %s) to UniFi controller: %s',
                     subnet_id, segmentation_id, e)
            raise

    def delete_subnet_precommit(self, context):
        """Delete resources for a subnet.

        :param context: SubnetContext instance describing the current
            state of the subnet, prior to the call to delete it.

        Delete subnet resources previously allocated by this
        mechanism driver for a subnet. Called inside transaction
        context on session. Runtime errors are not expected, but
        raising an exception will result in rollback of the
        transaction.
        """
        # Nothing to do for subnet delete precommit
        pass

    def delete_subnet_postcommit(self, context):
        """Delete a subnet.

        :param context: SubnetContext instance describing the current
            state of the subnet, prior to the call to delete it.

        Called after the transaction commits. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance. Runtime errors are not
        expected, and will not prevent the resource from being
        deleted.
        """
        subnet = context.current
        network = context.network.current

        # Only handle networks with segmentation ID (VLANs)
        if network.get('provider:network_type') != 'vlan' or network.get('router:external'):
            return

        segmentation_id = network.get('provider:segmentation_id')
        if not segmentation_id:
            return

        subnet_id = subnet['id']

        try:
            with self._get_controller() as controller:
                loop = asyncio.get_event_loop()

                unifi_network = self._unifi_network_for_vlan(controller, loop, segmentation_id)
                if not unifi_network:
                    LOG.debug('Subnet %s: no UniFi network found for VLAN %s, nothing to clear',
                             subnet_id, segmentation_id)
                    return

                update_data = dict(unifi_network.raw)
                update_data.update(self._subnet_clear_fields(subnet))

                loop.run_until_complete(
                    controller.request(NetworkUpdateRequest.create(Network(TypedNetwork(update_data))))
                )

                LOG.info('Subnet %s has been cleared from UniFi network %s (VLAN %s)',
                         subnet_id, unifi_network.id, segmentation_id)

        except Exception as e:
            # Log but don't raise to prevent subnet deletion from failing
            LOG.error('Failed to clear subnet %s (VLAN %s) from UniFi controller: %s',
                     subnet_id, segmentation_id, e)

    def _assign_network_to_default_zone(self, controller, loop, network_id):
        """Assign a UniFi network to the configured default firewall zone.

        Firewall zone membership lives on the zone object itself
        (`network_ids`), not on the network's own networkconf, so this
        looks up the configured zone by name and adds the network's ID
        to it if it isn't already there. No-op if
        CONF.unifi.default_firewall_zone is unset.

        Best-effort: any failure is logged, never raised, so a firewall
        zone problem (a typo'd zone name, an API quirk) can't take down
        the core network/subnet sync that already succeeded.

        Args:
            controller: An active UniFi controller client
            loop: The asyncio event loop to run requests on
            network_id: The UniFi network's _id to assign
        """
        zone_name = CONF.unifi.default_firewall_zone
        if not zone_name:
            return

        try:
            loop.run_until_complete(controller.firewall_zones.update())
            zone = next(
                (z for _, z in controller.firewall_zones.items() if z.name == zone_name),
                None
            )
            if not zone:
                LOG.warning('Cannot assign network %s to firewall zone: zone %r not found',
                           network_id, zone_name)
                return

            if network_id in zone.network_ids:
                return

            # The zone PUT endpoint rejects any field beyond _id/name/
            # network_ids as "unrecognized" (confirmed live) -- every
            # other field GET returns (attr_no_edit, cloud_template,
            # default_zone, external_id, zone_key, ...) is read-only and
            # must be omitted entirely, not just filtered individually.
            updated_zone = {
                '_id': zone.id,
                'name': zone.name,
                'network_ids': list(zone.network_ids) + [network_id],
            }

            loop.run_until_complete(
                controller.request(FirewallZoneUpdateRequest.create(TypedFirewallZone(updated_zone)))
            )
            LOG.info('Assigned UniFi network %s to firewall zone %s', network_id, zone_name)
        except Exception as e:
            LOG.error('Failed to assign network %s to firewall zone %s: %s',
                     network_id, zone_name, e)

    def _unassign_network_from_default_zone(self, controller, loop, network_id):
        """Remove a UniFi network from the configured default firewall zone.

        Best-effort: any failure is logged, never raised -- this runs
        during network deletion, and must never block a resource from
        being removed (matching delete_network_postcommit's own
        non-raising convention).

        Args:
            controller: An active UniFi controller client
            loop: The asyncio event loop to run requests on
            network_id: The UniFi network's _id to remove
        """
        zone_name = CONF.unifi.default_firewall_zone
        if not zone_name:
            return

        try:
            loop.run_until_complete(controller.firewall_zones.update())
            zone = next(
                (z for _, z in controller.firewall_zones.items() if z.name == zone_name),
                None
            )
            if not zone or network_id not in zone.network_ids:
                return

            # See _assign_network_to_default_zone: only _id/name/network_ids
            # are accepted on write, everything else GET returns is
            # read-only and must be omitted entirely.
            updated_zone = {
                '_id': zone.id,
                'name': zone.name,
                'network_ids': [nid for nid in zone.network_ids if nid != network_id],
            }

            loop.run_until_complete(
                controller.request(FirewallZoneUpdateRequest.create(TypedFirewallZone(updated_zone)))
            )
            LOG.info('Removed UniFi network %s from firewall zone %s', network_id, zone_name)
        except Exception as e:
            LOG.error('Failed to remove network %s from firewall zone %s: %s',
                     network_id, zone_name, e)

    def _unifi_network_for_vlan(self, controller, loop, segmentation_id, refresh=True):
        """Find the UniFi network config matching a Neutron VLAN segment.

        Args:
            controller: An active UniFi controller client
            loop: The asyncio event loop to run requests on
            segmentation_id: The Neutron VLAN segmentation ID
            refresh: Whether to call controller.networks.update() first.
                The bulk reconciliation pass in _sync_networks refreshes
                the cache once up front for the whole pass and passes
                refresh=False here to avoid a redundant full-list fetch
                per network; every single-event postcommit hook keeps the
                original refresh=True behavior.

        Returns:
            The matching aiounifi Network, or None if not found
        """
        if refresh:
            loop.run_until_complete(controller.networks.update())
        return next(
            (net for _, net in controller.networks.items()
             if hasattr(net, 'vlan') and net.vlan == segmentation_id),
            None
        )

    def _subnet_unifi_fields(self, subnet):
        """Build the UniFi networkconf fields for a Neutron subnet.

        Args:
            subnet: The Neutron subnet dict (context.current)

        Returns:
            A dict of TypedNetwork fields to merge into the UniFi
            network config for the subnet's IP/DHCP settings.
        """
        cidr = subnet.get('cidr')
        gateway_ip = subnet.get('gateway_ip')
        enable_dhcp = bool(subnet.get('enable_dhcp'))
        allocation_pools = subnet.get('allocation_pools') or []
        dns_nameservers = subnet.get('dns_nameservers') or []

        fields = {}

        if subnet.get('ip_version') == 6:
            if cidr and gateway_ip:
                prefixlen = ipaddress.ip_network(cidr, strict=False).prefixlen
                fields['ipv6_subnet'] = f'{gateway_ip}/{prefixlen}'
            elif cidr:
                fields['ipv6_subnet'] = cidr

            fields['ipv6_interface_type'] = 'static' if gateway_ip else 'none'
            fields['ipv6_ra_enabled'] = bool(gateway_ip)

            fields['dhcpdv6_enabled'] = enable_dhcp
            if enable_dhcp and allocation_pools:
                fields['dhcpdv6_start'] = allocation_pools[0]['start']
                fields['dhcpdv6_stop'] = allocation_pools[0]['end']

            # Neutron's own DHCPv6-stateful subnet is the only source of
            # truth for addresses on this network. Without this, RA-enabled
            # clients also self-assign a SLAAC address from the advertised
            # prefix -- one OVN's port security never authorized, so any
            # traffic that happens to pick it as its source address gets
            # silently dropped.
            fields['dhcpdv6_allow_slaac'] = False

            fields['dhcpdv6_dns_auto'] = not dns_nameservers
            for i, dns in enumerate(dns_nameservers[:4], start=1):
                fields[f'dhcpdv6_dns_{i}'] = dns
        else:
            if cidr and gateway_ip:
                prefixlen = ipaddress.ip_network(cidr, strict=False).prefixlen
                fields['ip_subnet'] = f'{gateway_ip}/{prefixlen}'

            fields['dhcpd_gateway_enabled'] = bool(gateway_ip)
            if gateway_ip:
                fields['dhcpd_gateway'] = gateway_ip

            fields['dhcpd_enabled'] = enable_dhcp
            if enable_dhcp and allocation_pools:
                fields['dhcpd_start'] = allocation_pools[0]['start']
                fields['dhcpd_stop'] = allocation_pools[0]['end']

            fields['dhcpd_dns_enabled'] = bool(dns_nameservers)
            for i, dns in enumerate(dns_nameservers[:4], start=1):
                fields[f'dhcpd_dns_{i}'] = dns

        return fields

    def _subnet_clear_fields(self, subnet):
        """Build UniFi networkconf fields that disable a deleted subnet's DHCP/gateway.

        Leaves ip_subnet/ipv6_subnet in place rather than clearing them
        outright, since an empty subnet field can leave the UniFi network
        config in an invalid state; disabling DHCP and the gateway is
        sufficient to stop it acting on the removed subnet.

        Args:
            subnet: The Neutron subnet dict (context.current)

        Returns:
            A dict of TypedNetwork fields to merge into the UniFi
            network config.
        """
        if subnet.get('ip_version') == 6:
            return {'dhcpdv6_enabled': False, 'ipv6_ra_enabled': False}
        return {
            'dhcpd_enabled': False,
            'dhcpd_gateway_enabled': False,
            'dhcpd_dns_enabled': False,
        }

    def _reconcile_network(self, controller, loop, network, refresh=True):
        """Ensure a UniFi network exists and is correctly named for this VLAN.

        Shared by create_network_postcommit/update_network_postcommit
        (single-event, each wraps this in their own try/except/raise) and
        _sync_networks (bulk periodic pass, catches and continues past a
        single network's failure instead). Only fixes presence and a
        drifted name -- other fields (e.g. purpose) are left alone even if
        changed manually in the UniFi UI, since correcting every field on
        every cycle risks fighting an intentional human change.

        Args:
            controller: An active UniFi controller client
            loop: The asyncio event loop to run requests on
            network: The Neutron network dict
            refresh: Passed through to the initial existence check (see
                _unifi_network_for_vlan) -- a post-write lookup always
                refreshes regardless, since the cache is stale the moment
                this function writes anything.
        """
        network_id = network['id']
        segmentation_id = network['provider:segmentation_id']
        expected_name = f"OpenStack-{network_id}-VLAN{segmentation_id}"

        unifi_network = self._unifi_network_for_vlan(
            controller, loop, segmentation_id, refresh=refresh)
        wrote = False

        if not unifi_network:
            vlan_data = TypedNetwork({
                "_id": network_id,
                "site_id": "default",
                "name": expected_name,
                "purpose": "corporate",
                "vlan": segmentation_id,
                "vlan_enabled": True,
                "enabled": True
            })
            loop.run_until_complete(
                controller.request(NetworkCreateRequest.create(Network(vlan_data)))
            )
            LOG.info('Network %s (VLAN %s) has been created in UniFi controller',
                     network_id, segmentation_id)
            wrote = True
        elif unifi_network.name != expected_name:
            updated = dict(unifi_network.raw)
            updated['name'] = expected_name
            loop.run_until_complete(
                controller.request(NetworkUpdateRequest.create(Network(TypedNetwork(updated))))
            )
            LOG.info('Corrected drifted name for UniFi network %s (VLAN %s): %r -> %r',
                     unifi_network.id, segmentation_id, unifi_network.name, expected_name)
            wrote = True
        else:
            LOG.debug('Network %s (VLAN %s) already exists in UniFi controller',
                     network_id, segmentation_id)

        if wrote:
            # A write just happened -- the cache is stale regardless of
            # what `refresh` said, and we need the authoritative
            # post-write object (in particular its real _id if this was
            # a create; see the module-level note on UniFi assigning its
            # own _id).
            unifi_network = self._unifi_network_for_vlan(
                controller, loop, segmentation_id, refresh=True)

        if unifi_network:
            self._assign_network_to_default_zone(controller, loop, unifi_network.id)
            self._reconcile_ipv4_disablement(controller, loop, network, unifi_network)

    def _reconcile_ipv4_disablement(self, controller, loop, network, unifi_network):
        """Disable UniFi's IPv4 DHCP/auto-scale for an IPv4-less network.

        A UniFi network always provisions an IPv4 config section by
        default, even for a Neutron network that only ever gets an IPv6
        subnet -- left alone, that means a live, Neutron-unmanaged IPv4
        DHCP server (and UniFi's own subnet auto-expansion feature) keeps
        running on the segment. Best-effort: swallows its own failures
        rather than blocking the rest of network reconciliation.

        Args:
            controller: An active UniFi controller client
            loop: The asyncio event loop to run requests on
            network: The Neutron network dict
            unifi_network: The corresponding UniFi network object
        """
        try:
            plugin = directory.get_plugin(plugin_constants.CORE)
            ctx = n_context.get_admin_context()
            subnets = plugin.get_subnets(
                ctx, filters={'network_id': [network['id']]})
        except Exception as e:
            LOG.warning('Could not check subnets for network %s to reconcile '
                       'its IPv4 DHCP/auto-scale state: %s', network['id'], e)
            return

        if any(s.get('ip_version') == 4 for s in subnets):
            return

        raw = unifi_network.raw
        if raw.get('dhcpd_enabled') is False and raw.get('auto_scale_enabled') is False:
            return

        updated = dict(raw)
        updated['dhcpd_enabled'] = False
        updated['auto_scale_enabled'] = False
        loop.run_until_complete(
            controller.request(NetworkUpdateRequest.create(Network(TypedNetwork(updated))))
        )
        LOG.info('Disabled IPv4 DHCP/auto-scale for IPv6-only UniFi network %s (VLAN %s)',
                 unifi_network.id, network.get('provider:segmentation_id'))

    def _reconcile_subnet(self, controller, loop, subnet, network, refresh=True):
        """Ensure a UniFi network's subnet/DHCP fields match this Neutron subnet.

        Shared by create_subnet_postcommit/update_subnet_postcommit
        (single-event) and _sync_networks (bulk periodic pass). Callers
        are responsible for the vlan/external network filter and their
        own error handling (raise vs. log-and-continue).

        Args:
            controller: An active UniFi controller client
            loop: The asyncio event loop to run requests on
            subnet: The Neutron subnet dict
            network: The Neutron network dict the subnet belongs to
        """
        segmentation_id = network['provider:segmentation_id']
        subnet_id = subnet['id']

        unifi_network = self._unifi_network_for_vlan(
            controller, loop, segmentation_id, refresh=refresh)
        if not unifi_network:
            LOG.warning('Cannot sync subnet %s: no UniFi network found for VLAN %s',
                       subnet_id, segmentation_id)
            return

        update_data = dict(unifi_network.raw)
        update_data.update(self._subnet_unifi_fields(subnet))

        loop.run_until_complete(
            controller.request(NetworkUpdateRequest.create(Network(TypedNetwork(update_data))))
        )

        LOG.info('Subnet %s has been synced to UniFi network %s (VLAN %s)',
                 subnet_id, unifi_network.id, segmentation_id)

    @staticmethod
    def _eui64_address(mac, cidr):
        """Compute a port's deterministic SLAAC (modified EUI-64) address.

        Only meaningful for a /64 -- SLAAC's EUI-64 interface identifier
        is only well-defined for a 64-bit prefix, and this is the exact
        addressing scheme a client's kernel derives on its own from a
        Router Advertisement, independent of anything Neutron does.

        Args:
            mac: The port's MAC address (colon-separated hex).
            cidr: The subnet's CIDR (e.g. "fd97:45c2:b3a1:1000::/64").

        Returns:
            The computed address as a string, or None if cidr isn't a /64.
        """
        network = ipaddress.ip_network(cidr, strict=False)
        if network.prefixlen != 64:
            return None
        mac_bytes = [int(b, 16) for b in mac.split(':')]
        mac_bytes[0] ^= 0x02
        interface_id = int.from_bytes(
            bytes(mac_bytes[0:3] + [0xff, 0xfe] + mac_bytes[3:6]), 'big')
        return str(ipaddress.IPv6Address(int(network.network_address) | interface_id))

    def _authorize_port_slaac_addresses(self, port, network):
        """Pre-authorize a port's inevitable SLAAC address with OVN.

        The UDM-SE always runs its DHCPv6 in "ra-only" mode (confirmed by
        reading its generated dnsmasq config directly -- true
        DHCPv6-stateful leasing never actually happens on this platform,
        regardless of any API field), so every IPv6-capable client on a
        network we've made the UDM-SE the gateway for also self-assigns a
        SLAAC address from the advertised prefix, in addition to its real
        Neutron-assigned address. OVN's port security only authorizes a
        port's actual fixed_ips, so traffic sourced from that SLAAC
        address is silently dropped with no error anywhere -- fix that by
        pre-authorizing the deterministic EUI-64 address as an
        allowed_address_pair. Best-effort: swallows its own failures
        rather than blocking port creation/update.

        Args:
            port: The Neutron port dict (context.current)
            network: The Neutron network dict the port belongs to
        """
        if network.get('provider:network_type') != 'vlan':
            return
        if not network.get('provider:segmentation_id'):
            return

        mac = port.get('mac_address')
        subnet_ids = {ip['subnet_id'] for ip in port.get('fixed_ips', [])
                     if ip.get('subnet_id')}
        if not mac or not subnet_ids:
            return

        try:
            plugin = directory.get_plugin(plugin_constants.CORE)
            ctx = n_context.get_admin_context()
            subnets = plugin.get_subnets(ctx, filters={'id': list(subnet_ids)})
        except Exception as e:
            LOG.warning('Could not look up subnets for port %s to authorize '
                       'its SLAAC address: %s', port['id'], e)
            return

        fixed_ips = {ip['ip_address'] for ip in port.get('fixed_ips', [])}
        existing_pairs = {
            pair['ip_address'] for pair in port.get('allowed_address_pairs', [])
        }

        new_pairs = []
        for subnet in subnets:
            if subnet.get('ip_version') != 6 or not subnet.get('gateway_ip'):
                continue
            slaac_ip = self._eui64_address(mac, subnet['cidr'])
            if slaac_ip and slaac_ip not in existing_pairs and slaac_ip not in fixed_ips:
                new_pairs.append({'ip_address': slaac_ip, 'mac_address': mac})

        if not new_pairs:
            return

        try:
            plugin.update_port(
                n_context.get_admin_context(), port['id'],
                {'port': {'allowed_address_pairs':
                    list(port.get('allowed_address_pairs', [])) + new_pairs}})
            LOG.info('Authorized SLAAC address(es) %s for port %s',
                     [p['ip_address'] for p in new_pairs], port['id'])
        except Exception as e:
            LOG.warning('Failed to authorize SLAAC address for port %s: %s',
                       port['id'], e)

    def create_port_precommit(self, context):
        """Allocate resources for a new port.

        :param context: PortContext instance describing the port.

        Create a new port, allocating resources as necessary in the
        database. Called inside transaction context on session. Call
        cannot block.  Raising an exception will result in a rollback
        of the current transaction.
        """
        # Nothing to do for port precommit
        pass

    def create_port_postcommit(self, context):
        """Create a port.

        :param context: PortContext instance describing the port.

        Called after the transaction completes. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance. Raising an exception will
        result in the deletion of the resource.
        """
        port = context.current
        network = context.network.current
        segments = context.segments_to_bind
        
        # DNS records can be created regardless of physical port binding
        if dns_apidef.DNSNAME in port and port.get(dns_apidef.DNSNAME):
            self.dns_handler.create_port_dns_records(context._plugin_context, port, network)

        # SLAAC address authorization applies to any port on a managed VLAN
        # network (VM ports included) -- must run before the
        # binding:profile check below, which only concerns the
        # baremetal/switch-port-configuration logic further down.
        self._authorize_port_slaac_addresses(port, network)

        # Skip ports that don't have binding:profile
        if not port.get('binding:profile'):
            return

        # Only process ports with local_link_information
        local_link_info = port['binding:profile'].get('local_link_information')
        if not local_link_info or not isinstance(local_link_info, list):
            return

        # Only handle networks with segmentation ID (VLANs)
        if network.get('provider:network_type') != 'vlan':
            return
            
        segmentation_id = network.get('provider:segmentation_id')
        if not segmentation_id:
            return
            
        port_id = port['id']
        
        # Process each link (switch port)
        for link in local_link_info:
            switch_id = link.get('switch_id')
            port_id_on_switch = link.get('port_id')
            
            if not switch_id or not port_id_on_switch:
                continue
                
            # Try to find and configure the switch port
            try:
                self._configure_port(switch_id, port_id_on_switch, 
                                    port_id, segmentation_id)
                
                # Store port mapping for later use
                self.port_mappings[port_id] = {
                    'switch_id': switch_id,
                    'port_id': port_id_on_switch,
                    'vlan_id': segmentation_id
                }
                
            except Exception as e:
                LOG.error('Failed to configure port %s on switch %s: %s',
                         port_id_on_switch, switch_id, e)
                raise

    def update_port_precommit(self, context):
        """Update resources of a port.

        :param context: PortContext instance describing the new
        state of the port, as well as the original state prior
        to the update_port call.

        Called inside transaction context on session to complete a
        port update as defined by this mechanism driver. Raising an
        exception will result in rollback of the transaction.

        update_port_precommit is called for all port updates. It is up
        to the mechanism driver to ignore state or state changes that
        it does not know or care about.
        """
        # Nothing to do for port update precommit
        pass

    def update_port_postcommit(self, context):
        """Update a port.

        :param context: PortContext instance describing the new
        state of the port, as well as the original state prior
        to the update_port call.

        Called after the transaction completes. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance. Raising an exception will
        cause the deletion of the resource.

        update_port_postcommit is called for all port updates. It is up
        to the mechanism driver to ignore state or state changes that
        it does not know or care about.
        """
        port = context.current
        original_port = context.original
        network = context.network.current
    
        # Check if DNS name has changed
        if (dns_apidef.DNSNAME in port and port.get(dns_apidef.DNSNAME)) or \
        (dns_apidef.DNSNAME in original_port and original_port.get(dns_apidef.DNSNAME)):
            self.dns_handler.update_port_dns_records(
                context._plugin_context, port, network, original_port)

        # See create_port_postcommit's identical call for why this must run
        # before the binding:profile check below.
        self._authorize_port_slaac_addresses(port, network)

        # Skip ports that don't have binding:profile
        if not port.get('binding:profile'):
            return
            
        # Only process ports with local_link_information
        local_link_info = port['binding:profile'].get('local_link_information')
        if not local_link_info or not isinstance(local_link_info, list):
            return
            
        # Skip if network hasn't changed
        if port['network_id'] == original_port['network_id']:
            # Check if binding profile has changed
            old_link_info = original_port.get('binding:profile', {}).get('local_link_information', [])
            if local_link_info == old_link_info:
                return
            
        # Only handle networks with segmentation ID (VLANs)
        if network.get('provider:network_type') != 'vlan':
            return
            
        segmentation_id = network.get('provider:segmentation_id')
        if not segmentation_id:
            return
            
        port_id = port['id']
        
        # Process each link (switch port)
        for link in local_link_info:
            switch_id = link.get('switch_id')
            port_id_on_switch = link.get('port_id')
            
            if not switch_id or not port_id_on_switch:
                continue
                
            # Try to find and configure the switch port
            try:
                # Check if we need to unconfigure the old port
                old_mapping = self.port_mappings.get(port_id)
                if old_mapping and (old_mapping['switch_id'] != switch_id or 
                                    old_mapping['port_id'] != port_id_on_switch):
                    self._unconfigure_port(old_mapping['switch_id'], 
                                         old_mapping['port_id'])
                
                self._configure_port(switch_id, port_id_on_switch, 
                                   port_id, segmentation_id)
                
                # Update port mapping
                self.port_mappings[port_id] = {
                    'switch_id': switch_id,
                    'port_id': port_id_on_switch,
                    'vlan_id': segmentation_id
                }
                
            except Exception as e:
                LOG.error('Failed to update port %s on switch %s: %s',
                         port_id_on_switch, switch_id, e)
                raise

    def delete_port_precommit(self, context):
        """Delete resources of a port.

        :param context: PortContext instance describing the current
        state of the port, prior to the call to delete it.

        Called inside transaction context on session. Call cannot
        block.  Raising an exception will result in rollback of the
        transaction.
        """
        # Nothing to do for port delete precommit
        pass

    def delete_port_postcommit(self, context):
        """Delete a port.

        :param context: PortContext instance describing the current
        state of the port, prior to the call to delete it.

        Called after the transaction completes. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance. Runtime errors are not
        expected, and will not prevent the resource from being
        deleted.
        """
        port = context.current
        port_id = port['id']
        network = context.network.current

        if dns_apidef.DNSNAME in port and port.get(dns_apidef.DNSNAME):
            self.dns_handler.delete_port_dns_records(
                context._plugin_context, port, network)

        # Check if we have a mapping for this port
        mapping = self.port_mappings.get(port_id)
        if not mapping:
            return
            
        # Try to unconfigure the port
        try:
            self._unconfigure_port(mapping['switch_id'], mapping['port_id'])
            # Remove mapping
            del self.port_mappings[port_id]
        except Exception as e:
            # Log but don't raise to avoid preventing port deletion
            LOG.error('Failed to unconfigure port %s on switch %s: %s',
                     mapping['port_id'], mapping['switch_id'], e)

    def bind_port(self, context) -> None:
        """Attempt to bind a port.

        :param context: PortContext instance describing the port

        This method is called outside any transaction to attempt to
        establish a port binding using this mechanism driver. Bindings
        may be created at each of multiple levels of a hierarchical
        network, and are established from the top level downward. At
        each level, the mechanism driver determines whether it can
        bind to any segment in the segments_to_bind for a given level.
        If at least one binding is successful, it continues up the
        hierarchy, first binding to the top level segment before
        proceeding downward to the next level.

        """
        # Check if this is a port we should try to bind
        port = context.current
        binding_profile = port.get('binding:profile', {})
        local_link_info = binding_profile.get('local_link_information')
        
        # If no local_link_information, we can't bind
        if not local_link_info:
            return
            
        # Get segments to try binding
        segments_to_bind = context.segments_to_bind
        if not segments_to_bind:
            LOG.debug("No segments to bind for port %s", port['id'])
            return
            
        for segment in segments_to_bind:
            # We only support binding VLAN segments
            if segment[api.NETWORK_TYPE] != 'vlan':
                continue
                
            # Check if we can find this switch
            for link in local_link_info:
                switch_id = link.get('switch_id')
                port_id_on_switch = link.get('port_id')
                
                # Verify we can handle this switch
                if not self._is_switch_supported(switch_id):
                    continue
                    
                # We can bind this segment
                context.set_binding(
                    segment[api.ID],
                    portbindings.VIF_TYPE_OTHER,
                    self.vif_details,
                    status=n_const.PORT_STATUS_ACTIVE
                )
                
                LOG.debug("Bound port %s to segment %s on switch %s, port %s",
                         port['id'], segment[api.ID], switch_id, port_id_on_switch)
                
    def _is_switch_supported(self, switch_id):
        """Check if a switch is supported by this driver.
        
        Args:
            switch_id: The MAC address of the switch
            
        Returns:
            True if the switch is supported
        """
        # Try to find this switch in the UniFi controller
        try:
            with self._get_controller() as controller:
                loop = asyncio.get_event_loop()
                
                # Fetch devices and look for this switch
                loop.run_until_complete(controller.devices.update())
                devices = controller.devices.items()
                for _, device in devices:
                    if hasattr(device, 'mac') and device.mac == switch_id:
                        if hasattr(device, 'type') and device.type == 'usw':
                            # Found a UniFi switch with this ID
                            return True
                            
                return False
                
        except Exception as e:
            LOG.error("Failed to check if switch %s is supported: %s", switch_id, e)
            return False

    def _configure_port(self, switch_id, port_id, neutron_port_id, vlan_id):
        """Configure a port with the specified VLAN.
        
        Args:
            switch_id: The MAC address of the switch
            port_id: The port ID on the switch
            neutron_port_id: The Neutron port ID
            vlan_id: The VLAN ID to set
            
        Returns:
            True if successful
        """
        LOG.debug("Configuring port %s on switch %s with VLAN %s",
                 port_id, switch_id, vlan_id)
                 
        try:
            with self._get_controller() as controller:
                loop = asyncio.get_event_loop()
                
                # Find the network with this VLAN ID
                loop.run_until_complete(controller.networks.update())
                network = next((net for _, net in controller.networks.items() if hasattr(net, 'vlan') and net.vlan == vlan_id), None)
                if not network:
                    raise exceptions.UnifiException(
                        f"Network with VLAN {vlan_id} not found in UniFi controller")
                network_id = network.id
                LOG.debug("Found network %s with VLAN %s", network_id, vlan_id)
                
                # Find this switch
                loop.run_until_complete(controller.devices.update())
                devices = controller.devices.items()
                switch = next((d for _, d in devices if hasattr(d, 'mac') and d.mac == switch_id), None)
                
                if not switch:
                    raise exceptions.CannotConnect(
                        f"Switch {switch_id} not found in UniFi controller")
                
                # Get port_idx from port_id (could be a name or number)
                try:
                    port_idx = int(port_id)
                except ValueError:
                    # Try to find port by name
                    port = next((p for p in switch.port_table 
                               if hasattr(p, 'name') and p["name"] == port_id), None)
                    if port and hasattr(port, 'port_idx'):
                        port_idx = port.get("port_idx")
                    else:
                        raise exceptions.UnifiException(
                            f"Port {port_id} not found on switch {switch_id}")
                
                if port_idx is None:
                    raise exceptions.UnifiException(
                        f"Port {port_id} not found on switch {switch_id}")
                    
                port_conf = TypedDevicePortOverrides({
                    "port_idx": int(port_idx),
                    "name": CONF.unifi.port_name_format.format(
                        port_id=neutron_port_id,
                        network_id=vlan_id,  # Using VLAN ID as network ID
                        segmentation_id=vlan_id
                    ),
                    # "port_vlan_enabled": True,
                    "native_networkconf_id": network_id
                })
                
                # Add QoS configuration if enabled
                # TODO: Create qos_profile and assign it via port profile
                # if CONF.unifi.enable_qos:
                #     port_conf["tx_rate_limit_enabled"] = True
                #     port_conf["tx_rate_limit_kbps_cfg"] = CONF.unifi.default_bandwidth_limit
                    
                # Add storm control if enabled
                if CONF.unifi.enable_storm_control:
                    if CONF.unifi.storm_control_broadcasting > 0:
                        port_conf["stormctrl_bcast_enabled"] = True
                        port_conf["stormctrl_bcast_rate"] = CONF.unifi.storm_control_broadcasting
                    if CONF.unifi.storm_control_multicasting > 0:
                        port_conf["stormctrl_mcast_enabled"] = True
                        port_conf["stormctrl_mcast_rate"] = CONF.unifi.storm_control_multicasting
                    if CONF.unifi.storm_control_unknown_unicast > 0:
                        port_conf["stormctrl_ucast_enabled"] = True
                        port_conf["stormctrl_ucast_rate"] = CONF.unifi.storm_control_unknown_unicast
                
                # Add port security if enabled
                if CONF.unifi.enable_port_security:
                    port_conf["dot1x_ctrl"] = "force_authorized"
                    port_conf["stp_port_mode"] = True
                
                # Send configuration to controller
                for attempt in range(CONF.unifi.port_setup_retry_count):
                    try:
                        loop.run_until_complete(
                            controller.request(
                                DeviceSetPortProfileRequest.create(
                                    device=switch,
                                    port_override=port_conf,
                                )
                            )
                        )
                        LOG.info("Configured port %s on switch %s with VLAN %s",
                                port_id, switch_id, vlan_id)
                        return True
                    except Exception as e:
                        if attempt < CONF.unifi.port_setup_retry_count - 1:
                            LOG.warning("Failed to configure port, retrying: %s", e)
                            time.sleep(CONF.unifi.port_setup_retry_interval)
                        else:
                            raise
                    
        except Exception as e:
            LOG.error("Failed to configure port %s on switch %s: %s",
                    port_id, switch_id, e)
            raise exceptions.UnifiNetmikoConfigError()

    def _unconfigure_port(self, switch_id, port_id):
        """Reset a port to default configuration.
        
        Args:
            switch_id: The MAC address of the switch
            port_id: The port ID on the switch
            
        Returns:
            True if successful
        """
        LOG.debug("Unconfiguring port %s on switch %s", port_id, switch_id)
                 
        try:
            with self._get_controller() as controller:
                loop = asyncio.get_event_loop()
                
                # Set VLAN ID to Default (1)
                vlan_id = 1  # Default VLAN ID
                LOG.debug("Resetting port %s on switch %s to VLAN %s",
                        port_id, switch_id, vlan_id)
                
                # Find the network with this VLAN ID
                loop.run_until_complete(controller.networks.update())
                network = next((net for _, net in controller.networks.items() if hasattr(net, 'vlan') and net.vlan == vlan_id), None)
                if not network:
                    raise exceptions.UnifiException(
                        f"Network with VLAN {vlan_id} not found in UniFi controller")
                network_id = network.id
                LOG.debug("Found network %s with VLAN %s", network_id, vlan_id)
                
                # Find this switch
                loop.run_until_complete(controller.devices.update())
                devices = controller.devices.items()
                switch = next((d for _, d in devices if hasattr(d, 'mac') and d.mac == switch_id), None)
                
                if not switch:
                    raise exceptions.CannotConnect(
                        f"Switch {switch_id} not found in UniFi controller")
                
                # Get port_idx from port_id (could be a name or number)
                try:
                    port_idx = int(port_id)
                except ValueError:
                    # Try to find port by name
                    port = next((p for p in switch.port_table 
                               if hasattr(p, 'name') and p.get("name") == port_id), None)
                    if port:
                        port_idx = port.get("port_idx")
                    else:
                        raise exceptions.UnifiException(
                            f"Port {port_id} not found on switch {switch_id}")
                
                if port_idx is None:
                    raise exceptions.UnifiException(
                        f"Port {port_id} not found on switch {switch_id}")

                # Create port configuration to reset
                port_conf = TypedDevicePortOverrides({
                    "port_idx": int(port_idx),
                    "name": f"Port {port_idx}",  # Reset to default name
                    "native_networkconf_id": network_id # Reset to default network
                })
                
                # Send configuration to controller
                for attempt in range(CONF.unifi.port_setup_retry_count):
                    try:
                        loop.run_until_complete(
                            controller.request(
                                DeviceSetPortProfileRequest.create(
                                    device=switch,
                                    port_override=port_conf,
                                )
                            )
                        )
                        LOG.info("Reset port %s on switch %s to default configuration",
                                port_id, switch_id)
                        return True
                    except Exception as e:
                        if attempt < CONF.unifi.port_setup_retry_count - 1:
                            LOG.warning("Failed to configure port, retrying: %s", e)
                            time.sleep(CONF.unifi.port_setup_retry_interval)
                        else:
                            raise
                
        except Exception as e:
            LOG.error("Failed to reset port %s on switch %s: %s",
                     port_id, switch_id, e)
            # Log but don't raise to avoid preventing port deletion
            return False

    def subports_added(self, context, port, subports):
        """Tell the agent about new subports to add.

        :param context: Request context
        :param port: Port dictionary
        :subports: List with subports
        """

        LOG.debug("Adding subports %s to port %s", subports, port['id'])
        # Call trunk driver to handle subports
        if self.trunk_driver:
            self.trunk_driver._handler.subports_added(resources.SUBPORTS,
                                                    events.AFTER_CREATE,
                                                    None,
                                                    {'states': [port],
                                                     'metadata': {'subports': subports}})

    def subports_deleted(self, context, port, subports):
        """Tell the agent about subports to delete.

        :param context: Request context
        :param port: Port dictionary
        :subports: List with subports
        """
        LOG.debug("Deleting subports %s from port %s", subports, port['id'])
        # Call trunk driver to handle subports
        if self.trunk_driver:
            self.trunk_driver._handler.subports_deleted(resources.SUBPORTS,
                                                      events.AFTER_DELETE,
                                                      None,
                                                      {'states': [port],
                                                       'metadata': {'subports': subports}})

    def _is_port_supported(self, port):
        """Check if a port is supported by this driver.
        
        Args:
            port: The neutron port object
            
        Returns:
            True if the port is supported by this driver
        """
        # Check if the port has binding information
        if not port.get('binding:profile'):
            return False
            
        # Check if it has local link information
        local_link_info = port['binding:profile'].get('local_link_information')
        if not local_link_info or not isinstance(local_link_info, list):
            return False
            
        # At least one switch must be supported
        for link in local_link_info:
            switch_id = link.get('switch_id')
            if self._is_switch_supported(switch_id):
                return True
                
        return False
