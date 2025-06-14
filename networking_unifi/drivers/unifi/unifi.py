"""
UniFi Driver for networking_unifi

This driver implements network operations for UniFi switches
managed through a UniFi Network controller.
"""

import asyncio
from contextlib import contextmanager
import functools
import time
import uuid

from neutron_lib.api.definitions import provider_net
from neutron_lib import constants as n_const
from neutron_lib.plugins.ml2 import api
from oslo_config import cfg
from oslo_log import log as logging

from networking_baremetal.drivers import base
from networking_baremetal import exceptions

from .exceptions import UnifiException, CannotConnect, AuthenticationRequired, UnifiNetmikoConfigError

CONF = cfg.CONF
LOG = logging.getLogger(__name__)


# Configuration options for UniFi driver
_UNIFI_OPTS = [
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
                help='Sync networks and ports on startup'),
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
]


def list_driver_opts():
    """Return configuration options for the driver."""
    return [('unifi', _UNIFI_OPTS)]


def error_handler(func):
    """Decorator to handle and log exceptions."""
    @functools.wraps(func)
    def wrapper(instance, *args, **kwargs):
        try:
            return func(instance, *args, **kwargs)
        except Exception as e:
            LOG.error(
                "%(function_name)s %(exception_desc)s",
                {'function_name': func.__name__,
                 'exception_desc': str(e)}
            )
            raise
    return wrapper


class UnifiApiClient(base.BaseDeviceClient):
    """UniFi API client for managing UniFi controllers."""

    def __init__(self, device):
        super().__init__(device)
        self.device = device
        self._controllers = {}

    @contextmanager
    def _get_api(self, controller_id):
        """Get a UniFi API client using the async helper.

        Args:
            controller_id: Controller identifier (usually URL)

        Returns:
            A UniFi controller client
        """
        # Set up event loop for async calls
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            controller = loop.run_until_complete(self._get_unifi_api())
            yield controller
        finally:
            # Clean up
            loop.close()

    async def _get_unifi_api(self):
        """Create a aiounifi object and verify authentication."""
        import ssl
        from aiohttp import CookieJar, ClientSession
        from aiounifi import (Unauthorized, BadGateway, Forbidden,
                              ServiceUnavailable, RequestError, ResponseError,
                              LoginRequired, AiounifiException)
        from aiounifi.controller import Controller
        from aiounifi.models.configuration import Configuration

        from typing import Literal

        ssl_context: ssl.SSLContext | Literal[False] = False
        unsafe = True
        if CONF[self.device].verify_ssl:
            unsafe = False
            ssl_context = ssl.create_default_context(
                purpose=ssl.Purpose.CLIENT_AUTH,
            )

        session = ClientSession(
            cookie_jar=CookieJar(unsafe=unsafe)
        )

        api = Controller(
            Configuration(
                session,
                host=CONF[self.device].host,
                apikey=CONF[self.device].apikey,
                username=CONF[self.device].username,
                password=CONF[self.device].password,
                port=CONF[self.device].port,
                site=CONF[self.device].site,
                ssl_context=ssl_context,
            )
        )

        if not CONF[self.device].apikey and (CONF[self.device].username and CONF[self.device].password):
            try:
                async with asyncio.timeout(10):
                    await api.login()
            except Unauthorized as err:
                LOG.warning(
                    "Connected to UniFi Network at %s but not registered: %s",
                    CONF[self.device].host,
                    err,
                )
                raise AuthenticationRequired(reason=str(err)) from err
            except (
                TimeoutError,
                BadGateway,
                Forbidden,
                ServiceUnavailable,
                RequestError,
                ResponseError,
            ) as err:
                LOG.error(
                    "Error connecting to the UniFi Network at %s: %s",
                    CONF[self.device].host, err
                )
                raise CannotConnect(reason=str(err)) from err
            except LoginRequired as err:
                LOG.warning(
                    "Connected to UniFi Network at %s but login required: %s",
                    CONF[self.device].host,
                    err,
                )
                raise AuthenticationRequired(reason=str(err)) from err
            except AiounifiException as err:
                LOG.exception("Unknown UniFi Network communication error occurred: %s", err)
                raise AuthenticationRequired(reason=str(err)) from err

        return api

    def get_capabilities(self):
        """Get capabilities from the UniFi controller."""
        # UniFi controllers have standard capabilities
        return {'unifi-network', 'vlan-management', 'port-configuration'}


class UnifiDriver(base.BaseDeviceDriver):
    """UniFi Device Driver for managing UniFi switches."""

    SUPPORTED_BOND_MODES: set = set()  # UniFi doesn't typically use bonding

    def __init__(self, device):
        super().__init__(device)
        self.client = UnifiApiClient(device)
        self.device = device
        self.port_mappings = {}

    def validate(self):
        """Validate the driver configuration and connectivity."""
        try:
            with self.client._get_api(self.device) as _:
                LOG.info('Device %(device)s was loaded. Controller connected successfully.',
                         {'device': self.device})
        except Exception as e:
            raise exceptions.DriverValidationError(device=self.device, err=e)

    def load_config(self):
        """Register driver specific configuration."""
        CONF.register_opts(_UNIFI_OPTS, group=self.device)

    @error_handler
    def create_network(self, context):
        """Create network on UniFi controller.

        :param context: NetworkContext instance describing the new network.
        """
        network = context.current
        network_type = network.get(provider_net.NETWORK_TYPE)

        # Only handle VLAN networks
        if network_type != 'vlan':
            return

        segmentation_id = network.get(provider_net.SEGMENTATION_ID)
        if not segmentation_id:
            return

        network_id = network['id']

        try:
            with self.client._get_api(self.device) as controller:
                loop = asyncio.get_event_loop()

                # Import required models
                from aiounifi.models.network import NetworkCreateRequest, Network, TypedNetwork

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
                        "site_id": CONF[self.device].site,
                        "name": f"OpenStack-{network_id}-VLAN{segmentation_id}",
                        "purpose": "corporate",
                        "vlan": segmentation_id,
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

        except Exception as e:
            LOG.error('Failed to create network %s (VLAN %s) in UniFi controller: %s',
                     network_id, segmentation_id, e)
            raise

    @error_handler
    def update_network(self, context):
        """Update network on UniFi controller.

        :param context: NetworkContext instance describing the network update.
        """
        network = context.current
        original_network = context.original

        if (network.get(provider_net.NETWORK_TYPE) != 'vlan' or
                original_network.get(provider_net.NETWORK_TYPE) != 'vlan'):
            return

        new_segmentation_id = network.get(provider_net.SEGMENTATION_ID)
        old_segmentation_id = original_network.get(provider_net.SEGMENTATION_ID)

        # If VLAN ID hasn't changed, nothing to do
        if new_segmentation_id == old_segmentation_id:
            return

        network_id = network['id']

        try:
            with self.client._get_api(self.device) as controller:
                loop = asyncio.get_event_loop()

                # Import required models
                from aiounifi.models.network import NetworkCreateRequest, NetworkDeleteRequest, Network, TypedNetwork

                # Find the network with the old VLAN ID
                loop.run_until_complete(controller.networks.update())
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
                    "site_id": CONF[self.device].site,
                    "name": f"OpenStack-{network_id}-VLAN{new_segmentation_id}",
                    "purpose": "corporate",
                    "vlan": new_segmentation_id,
                    "enabled": True
                })

                loop.run_until_complete(
                    controller.request(NetworkCreateRequest.create(Network(vlan_data)))
                )

                LOG.info('Network %s updated from VLAN %s to VLAN %s in UniFi controller',
                         network_id, old_segmentation_id, new_segmentation_id)

        except Exception as e:
            LOG.error('Failed to update network %s from VLAN %s to VLAN %s: %s',
                     network_id, old_segmentation_id, new_segmentation_id, e)
            raise

    @error_handler
    def delete_network(self, context):
        """Delete network from UniFi controller.

        :param context: NetworkContext instance describing the network to delete.
        """
        network = context.current

        # Only handle VLAN networks
        if network.get(provider_net.NETWORK_TYPE) != 'vlan':
            return

        segmentation_id = network.get(provider_net.SEGMENTATION_ID)
        if not segmentation_id:
            return

        network_id = network['id']

        try:
            with self.client._get_api(self.device) as controller:
                loop = asyncio.get_event_loop()

                # Import required models
                from aiounifi.models.network import NetworkDeleteRequest

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

    @error_handler
    def create_port(self, context, segment, links):
        """Create/Configure port on UniFi switch.

        :param context: PortContext instance describing the port.
        :param segment: segment dictionary describing segment to bind
        :param links: Local link information filtered for the device.
        """
        port = context.current

        # Only handle VLAN segments
        if segment[api.NETWORK_TYPE] != n_const.TYPE_VLAN:
            return

        segmentation_id = segment[api.SEGMENTATION_ID]
        port_id = port['id']

        # Process each link (switch port)
        for link in links:
            switch_id = link.get('switch_id')
            port_id_on_switch = link.get('port_id')

            if not switch_id or not port_id_on_switch:
                continue

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

    @error_handler
    def update_port(self, context, links):
        """Update port on UniFi switch.

        :param context: PortContext instance describing the port update.
        :param links: Local link information filtered for the device.
        """
        port = context.current
        original_port = context.original

        # Skip if network hasn't changed
        if port['network_id'] == original_port['network_id']:
            # Check if binding profile has changed
            old_link_info = original_port.get('binding:profile', {}).get('local_link_information', [])
            new_link_info = port.get('binding:profile', {}).get('local_link_information', [])
            if new_link_info == old_link_info:
                return

        # Only handle VLAN networks
        network = context.network.current
        if network.get(provider_net.NETWORK_TYPE) != 'vlan':
            return

        segmentation_id = network.get(provider_net.SEGMENTATION_ID)
        if not segmentation_id:
            return

        port_id = port['id']

        # Process each link (switch port)
        for link in links:
            switch_id = link.get('switch_id')
            port_id_on_switch = link.get('port_id')

            if not switch_id or not port_id_on_switch:
                continue

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

    @error_handler
    def delete_port(self, context, links, current=True):
        """Delete/Un-configure port on UniFi switch.

        :param context: PortContext instance describing the port.
        :param links: Local link information filtered for the device.
        :param current: Boolean, when true use context.current, when
            false use context.original
        """
        port = context.current if current else context.original
        port_id = port['id']

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

    def _configure_port(self, switch_id, port_id, neutron_port_id, vlan_id):
        """Configure a port with the specified VLAN.

        Args:
            switch_id: The MAC address of the switch
            port_id: The port ID on the switch
            neutron_port_id: The Neutron port ID
            vlan_id: The VLAN ID to set
        """
        LOG.debug("Configuring port %s on switch %s with VLAN %s",
                 port_id, switch_id, vlan_id)

        try:
            with self.client._get_api(self.device) as controller:
                loop = asyncio.get_event_loop()

                # Import required models
                from aiounifi.models.device import TypedDevicePortOverrides, DeviceSetPortProfileRequest

                # Find the network with this VLAN ID
                loop.run_until_complete(controller.networks.update())
                network = next((net for _, net in controller.networks.items()
                               if hasattr(net, 'vlan') and net.vlan == vlan_id), None)
                if not network:
                    raise UnifiException(
                        f"Network with VLAN {vlan_id} not found in UniFi controller")
                network_id = network.id
                LOG.debug("Found network %s with VLAN %s", network_id, vlan_id)

                # Find this switch
                loop.run_until_complete(controller.devices.update())
                devices = controller.devices.items()
                switch = next((d for _, d in devices
                              if hasattr(d, 'mac') and d.mac == switch_id), None)

                if not switch:
                    raise CannotConnect(
                        f"Switch {switch_id} not found in UniFi controller")

                # Get port_idx from port_id (could be a name or number)
                try:
                    port_idx = int(port_id)
                except ValueError:
                    # Try to find port by name
                    port = next((p for p in switch.port_table
                               if hasattr(p, 'name') and p.get("name") == port_id), None)
                    if port and hasattr(port, 'port_idx'):
                        port_idx = port.get("port_idx")
                    else:
                        raise UnifiException(
                            f"Port {port_id} not found on switch {switch_id}")

                if port_idx is None:
                    raise UnifiException(
                        f"Port {port_id} not found on switch {switch_id}")

                port_conf = TypedDevicePortOverrides({
                    "port_idx": int(port_idx),
                    "name": CONF[self.device].port_name_format.format(
                        port_id=neutron_port_id,
                        network_id=vlan_id,
                        segmentation_id=vlan_id
                    ),
                    "native_networkconf_id": network_id
                })

                # Add storm control if enabled
                if CONF[self.device].enable_storm_control:
                    if CONF[self.device].storm_control_broadcasting > 0:
                        port_conf["stormctrl_bcast_enabled"] = True
                        port_conf["stormctrl_bcast_rate"] = CONF[self.device].storm_control_broadcasting
                    if CONF[self.device].storm_control_multicasting > 0:
                        port_conf["stormctrl_mcast_enabled"] = True
                        port_conf["stormctrl_mcast_rate"] = CONF[self.device].storm_control_multicasting
                    if CONF[self.device].storm_control_unknown_unicast > 0:
                        port_conf["stormctrl_ucast_enabled"] = True
                        port_conf["stormctrl_ucast_rate"] = CONF[self.device].storm_control_unknown_unicast

                # Add port security if enabled
                if CONF[self.device].enable_port_security:
                    port_conf["dot1x_ctrl"] = "force_authorized"
                    port_conf["stp_port_mode"] = True

                # Send configuration to controller
                for attempt in range(CONF[self.device].port_setup_retry_count):
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
                        if attempt < CONF[self.device].port_setup_retry_count - 1:
                            LOG.warning("Failed to configure port, retrying: %s", e)
                            time.sleep(CONF[self.device].port_setup_retry_interval)
                        else:
                            raise

        except Exception as e:
            LOG.error("Failed to configure port %s on switch %s: %s",
                     port_id, switch_id, e)
            raise UnifiNetmikoConfigError(reason=str(e))

    def _unconfigure_port(self, switch_id, port_id):
        """Reset a port to default configuration.

        Args:
            switch_id: The MAC address of the switch
            port_id: The port ID on the switch
        """
        LOG.debug("Unconfiguring port %s on switch %s", port_id, switch_id)

        try:
            with self.client._get_api(self.device) as controller:
                loop = asyncio.get_event_loop()

                # Import required models
                from aiounifi.models.device import TypedDevicePortOverrides, DeviceSetPortProfileRequest

                # Set VLAN ID to Default (1)
                vlan_id = 1  # Default VLAN ID
                LOG.debug("Resetting port %s on switch %s to VLAN %s",
                        port_id, switch_id, vlan_id)

                # Find the network with this VLAN ID
                loop.run_until_complete(controller.networks.update())
                network = next((net for _, net in controller.networks.items()
                               if hasattr(net, 'vlan') and net.vlan == vlan_id), None)
                if not network:
                    raise UnifiException(
                        f"Network with VLAN {vlan_id} not found in UniFi controller")
                network_id = network.id
                LOG.debug("Found network %s with VLAN %s", network_id, vlan_id)

                # Find this switch
                loop.run_until_complete(controller.devices.update())
                devices = controller.devices.items()
                switch = next((d for _, d in devices
                              if hasattr(d, 'mac') and d.mac == switch_id), None)

                if not switch:
                    raise CannotConnect(
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
                        raise UnifiException(
                            f"Port {port_id} not found on switch {switch_id}")

                if port_idx is None:
                    raise UnifiException(
                        f"Port {port_id} not found on switch {switch_id}")

                # Create port configuration to reset
                port_conf = TypedDevicePortOverrides({
                    "port_idx": int(port_idx),
                    "name": f"Port {port_idx}",  # Reset to default name
                    "native_networkconf_id": network_id  # Reset to default network
                })

                # Send configuration to controller
                for attempt in range(CONF[self.device].port_setup_retry_count):
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
                        if attempt < CONF[self.device].port_setup_retry_count - 1:
                            LOG.warning("Failed to configure port, retrying: %s", e)
                            time.sleep(CONF[self.device].port_setup_retry_interval)
                        else:
                            raise

        except Exception as e:
            LOG.error("Failed to reset port %s on switch %s: %s",
                     port_id, switch_id, e)
            # Log but don't raise to avoid preventing port deletion
            return False

    @staticmethod
    def _uuid_as_hex(_uuid):
        """Convert UUID to hex format."""
        return uuid.UUID(_uuid).hex
