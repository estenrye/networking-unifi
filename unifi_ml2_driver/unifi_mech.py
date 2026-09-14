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
import threading
import time

from neutron.db import provisioning_blocks
from neutron_lib.api.definitions import dns as dns_apidef
from neutron_lib.api.definitions import portbindings
from neutron_lib import constants as n_const
from neutron_lib.callbacks import resources, events
from neutron_lib.plugins.ml2 import api
from neutron_lib.plugins import directory
from oslo_config import cfg
from oslo_log import log as logging

from unifi_ml2_driver import exceptions
from unifi_ml2_driver.dns_handler import UnifiDnsHandler
from unifi_ml2_driver.unifi_api import get_unifi_api
from aiounifi.models.network import NetworkCreateRequest, NetworkDeleteRequest, NetworkUpdateRequest, Network, TypedNetwork
from aiounifi.models.firewall_zone import FirewallZoneUpdateRequest, TypedFirewallZone
from aiounifi.models.device import Device, DeviceListRequest, TypedDevicePortOverrides, DeviceSetPortProfileRequest
from unifi_ml2_driver import trunk_driver

LOG = logging.getLogger(__name__)

from unifi_ml2_driver.config import CONF

class UnifiMechDriver(api.MechanismDriver):
    """UniFi Mechanism Driver for ML2 plugin.

    This driver manages VLANs and port configurations on UniFi switches
    through a UniFi Network controller.
    """

    def __init__(self):
        self.controller = None
        self.switches = {}
        self.port_mappings = {}
        self.vif_details = {portbindings.VIF_DETAILS_CONNECTIVITY:
                            portbindings.CONNECTIVITY_L2}
        self.trunk_driver = None
        self._controllers = {}
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

        if not CONF.unifi.username or not CONF.unifi.password:
            LOG.warning("UniFi credentials not configured. Driver disabled.")
            return

        # Test connection to controller
        try:
            with self._get_controller() as controller:
                LOG.info("Successfully connected to UniFi controller at %s",
                         CONF.unifi.host)
                
                # Sync networks if needed
                if CONF.unifi.sync_startup:
                    self._sync_networks()
        except Exception as e:
            LOG.error("Failed to connect to UniFi controller: %s", e)
            # Don't raise - let the driver remain initialized but inactive

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
        """Sync networks from OpenStack to UniFi controller."""
        LOG.info("Syncing networks to UniFi controller")
        # Implementation would require access to the Neutron DB
        # Skipped for this example, but would involve:
        # 1. Getting all networks with segmentation_id (VLAN ID)
        # 2. Ensuring those VLANs exist in the UniFi controller
        pass

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
                
                # Check if network exists
                loop.run_until_complete(controller.networks.update())
                networks = controller.networks.items()
                network_exists = any(
                    net.vlan == segmentation_id for k, net in networks if hasattr(net, 'vlan')
                )
                
                if not network_exists:
                    # Create VLAN in UniFi controller
                    vlan_data = TypedNetwork({
                        "_id": network_id,
                        "site_id": "default",
                        "name": f"OpenStack-{network_id}-VLAN{segmentation_id}",
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
                else:
                    LOG.debug('Network %s (VLAN %s) already exists in UniFi controller',
                             network_id, segmentation_id)

                self._assign_network_to_default_zone(controller, loop, network_id)

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
                
                # Find the network with the old VLAN ID
                networks = controller.networks.items()
                old_network = next(
                    (net for k, net in networks 
                     if hasattr(net, 'vlan') and net.vlan == old_segmentation_id), 
                    None
                )
                
                if old_network:
                    # Delete old network
                    loop.run_until_complete(
                        controller.request(NetworkDeleteRequest.create(old_network.id))
                    )
                
                # Create new network with updated VLAN ID
                vlan_data = TypedNetwork({
                    "_id": network_id,
                    "site_id": "default",
                    "name": f"OpenStack-{network_id}-VLAN{new_segmentation_id}",
                    "purpose": "corporate",
                    "vlan": new_segmentation_id,
                    "vlan_enabled": True,
                    "enabled": True
                })
                
                loop.run_until_complete(
                    controller.request(NetworkCreateRequest.create(Network(vlan_data)))
                )
                
                LOG.info('Network %s updated from VLAN %s to VLAN %s in UniFi controller',
                         network_id, old_segmentation_id, new_segmentation_id)

                self._assign_network_to_default_zone(controller, loop, network_id)

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

                unifi_network = self._unifi_network_for_vlan(controller, loop, segmentation_id)
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

                unifi_network = self._unifi_network_for_vlan(controller, loop, segmentation_id)
                if not unifi_network:
                    LOG.warning('Cannot sync subnet %s: no UniFi network found for VLAN %s',
                               subnet_id, segmentation_id)
                    return

                update_data = dict(unifi_network.raw)
                update_data.update(self._subnet_unifi_fields(subnet))

                loop.run_until_complete(
                    controller.request(NetworkUpdateRequest.create(Network(TypedNetwork(update_data))))
                )

                LOG.info('Subnet %s has been re-synced to UniFi network %s (VLAN %s)',
                         subnet_id, unifi_network.id, segmentation_id)

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

        Args:
            controller: An active UniFi controller client
            loop: The asyncio event loop to run requests on
            network_id: The UniFi network's _id to assign
        """
        zone_name = CONF.unifi.default_firewall_zone
        if not zone_name:
            return

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

        updated_zone = dict(zone.raw)
        updated_zone['network_ids'] = list(zone.network_ids) + [network_id]

        loop.run_until_complete(
            controller.request(FirewallZoneUpdateRequest.create(TypedFirewallZone(updated_zone)))
        )
        LOG.info('Assigned UniFi network %s to firewall zone %s', network_id, zone_name)

    def _unassign_network_from_default_zone(self, controller, loop, network_id):
        """Remove a UniFi network from the configured default firewall zone.

        Args:
            controller: An active UniFi controller client
            loop: The asyncio event loop to run requests on
            network_id: The UniFi network's _id to remove
        """
        zone_name = CONF.unifi.default_firewall_zone
        if not zone_name:
            return

        loop.run_until_complete(controller.firewall_zones.update())
        zone = next(
            (z for _, z in controller.firewall_zones.items() if z.name == zone_name),
            None
        )
        if not zone or network_id not in zone.network_ids:
            return

        updated_zone = dict(zone.raw)
        updated_zone['network_ids'] = [nid for nid in zone.network_ids if nid != network_id]

        loop.run_until_complete(
            controller.request(FirewallZoneUpdateRequest.create(TypedFirewallZone(updated_zone)))
        )
        LOG.info('Removed UniFi network %s from firewall zone %s', network_id, zone_name)

    def _unifi_network_for_vlan(self, controller, loop, segmentation_id):
        """Find the UniFi network config matching a Neutron VLAN segment.

        Args:
            controller: An active UniFi controller client
            loop: The asyncio event loop to run requests on
            segmentation_id: The Neutron VLAN segmentation ID

        Returns:
            The matching aiounifi Network, or None if not found
        """
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
            if cidr:
                fields['ipv6_subnet'] = cidr

            fields['dhcpdv6_enabled'] = enable_dhcp
            if enable_dhcp and allocation_pools:
                fields['dhcpdv6_start'] = allocation_pools[0]['start']
                fields['dhcpdv6_stop'] = allocation_pools[0]['end']

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
            return {'dhcpdv6_enabled': False}
        return {
            'dhcpd_enabled': False,
            'dhcpd_gateway_enabled': False,
            'dhcpd_dns_enabled': False,
        }

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
