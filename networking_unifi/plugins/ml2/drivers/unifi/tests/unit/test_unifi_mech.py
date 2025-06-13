# Copyright 2024 StackHPC Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import unittest
from unittest import mock

import aiounifi
from neutron_lib.api.definitions import portbindings
from neutron_lib.plugins.ml2 import api as ml2_api
from oslo_config import cfg

from networking_unifi import exceptions
from networking_unifi import unifi_mech

# Mock device data
MOCK_SWITCH_MAC = '78:45:58:ab:cd:ef'
MOCK_PORT_ID = '3'
MOCK_VLAN_ID = 100


class FakeSwitch:
    """Fake UniFi Switch for testing."""
    def __init__(self):
        self.mac = MOCK_SWITCH_MAC
        self.type = 'usw'
        self.id = '123456789'
        self.port_table = [
            {'port_idx': 1, 'name': 'Port 1', 'up': True},
            {'port_idx': 2, 'name': 'Port 2', 'up': False},
            {'port_idx': 3, 'name': 'Port 3', 'up': True}
        ]


class FakeNetwork:
    """Fake Network for testing."""
    def __init__(self, vlan=None):
        self.id = '1234'
        self.name = 'test_network'
        self.vlan = vlan


class TestUnifiMechDriver(unittest.TestCase):
    """Test cases for UniFi ML2 mechanism driver."""

    def setUp(self):
        """Set up test environment."""
        # Configure mock
        self.mock_cfg = mock.patch.object(cfg, 'CONF')
        self.mock_cfg.start()

        # Set up configuration
        unifi_group = cfg.OptGroup(name='unifi')
        cfg.CONF.register_group(unifi_group)
        cfg.CONF.register_opts(unifi_mech.unifi_opts, group='unifi')
        cfg.CONF.set_override('controller', 'https://unifi.example.com', group='unifi')
        cfg.CONF.set_override('username', 'admin', group='unifi')
        cfg.CONF.set_override('password', 'password', group='unifi')
        cfg.CONF.set_override('verify_ssl', False, group='unifi')

        # Create driver instance
        self.driver = unifi_mech.UnifiMechDriver()

        # Mock controller
        self.mock_controller = mock.MagicMock()
        self.mock_controller.__enter__.return_value = self.mock_controller

        # Mock devices for controller
        self.fake_switch = FakeSwitch()
        self.mock_controller.devices.update.return_value = asyncio.Future()
        self.mock_controller.devices.update.return_value.set_result([self.fake_switch])

        # Patch get_controller method
        self.mock_get_controller = mock.patch.object(
            self.driver, '_get_controller', return_value=self.mock_controller)
        self.mock_get_controller.start()

        # Set up port mappings
        self.driver.port_mappings = {
            'port-1': {
                'switch_id': MOCK_SWITCH_MAC,
                'port_id': MOCK_PORT_ID,
                'vlan_id': MOCK_VLAN_ID
            }
        }

    def tearDown(self):
        """Clean up test environment."""
        self.mock_cfg.stop()
        self.mock_get_controller.stop()

    def test_initialize(self):
        """Test driver initialization."""
        self.driver.initialize()
        self.mock_controller.__enter__.assert_called_once()

    def test_is_switch_supported(self):
        """Test switch support detection."""
        self.assertTrue(self.driver._is_switch_supported(MOCK_SWITCH_MAC))

        # Test with non-existent switch
        self.mock_controller.devices.update.return_value = asyncio.Future()
        self.mock_controller.devices.update.return_value.set_result([])
        self.assertFalse(self.driver._is_switch_supported('00:11:22:33:44:55'))

    def test_configure_port(self):
        """Test port configuration."""
        # Set up mock for port_conf
        self.mock_controller.devices.async_set_port_conf.return_value = asyncio.Future()
        self.mock_controller.devices.async_set_port_conf.return_value.set_result(True)

        # Test configure port
        self.driver._configure_port(MOCK_SWITCH_MAC, MOCK_PORT_ID, 'port-1', MOCK_VLAN_ID)

        # Verify port configuration was called
        self.mock_controller.devices.async_set_port_conf.assert_called_once()

        # Verify the port configuration parameters
        args = self.mock_controller.devices.async_set_port_conf.call_args[0][0]
        self.assertEqual(args['mac'], MOCK_SWITCH_MAC)
        self.assertEqual(args['port_idx'], int(MOCK_PORT_ID))
        self.assertEqual(args['port_vlan'], MOCK_VLAN_ID)
        self.assertTrue(args['port_vlan_enabled'])

    def test_configure_port_with_qos(self):
        """Test port configuration with QoS."""
        # Enable QoS
        cfg.CONF.set_override('enable_qos', True, group='unifi')
        cfg.CONF.set_override('default_bandwidth_limit', 1000000, group='unifi')

        # Set up mock for port_conf
        self.mock_controller.devices.async_set_port_conf.return_value = asyncio.Future()
        self.mock_controller.devices.async_set_port_conf.return_value.set_result(True)

        # Test configure port
        self.driver._configure_port(MOCK_SWITCH_MAC, MOCK_PORT_ID, 'port-1', MOCK_VLAN_ID)

        # Verify QoS parameters
        args = self.mock_controller.devices.async_set_port_conf.call_args[0][0]
        self.assertTrue(args['tx_rate_limit_enabled'])
        self.assertEqual(args['tx_rate_limit_kbps_cfg'], 1000000)

    def test_configure_port_with_storm_control(self):
        """Test port configuration with storm control."""
        # Enable storm control
        cfg.CONF.set_override('enable_storm_control', True, group='unifi')
        cfg.CONF.set_override('storm_control_broadcasting', 80, group='unifi')
        cfg.CONF.set_override('storm_control_multicasting', 70, group='unifi')
        cfg.CONF.set_override('storm_control_unknown_unicast', 60, group='unifi')

        # Set up mock for port_conf
        self.mock_controller.devices.async_set_port_conf.return_value = asyncio.Future()
        self.mock_controller.devices.async_set_port_conf.return_value.set_result(True)

        # Test configure port
        self.driver._configure_port(MOCK_SWITCH_MAC, MOCK_PORT_ID, 'port-1', MOCK_VLAN_ID)

        # Verify storm control parameters
        args = self.mock_controller.devices.async_set_port_conf.call_args[0][0]
        self.assertTrue(args['stormctrl_bcast_enabled'])
        self.assertEqual(args['stormctrl_bcast_rate'], 80)
        self.assertTrue(args['stormctrl_mcast_enabled'])
        self.assertEqual(args['stormctrl_mcast_rate'], 70)
        self.assertTrue(args['stormctrl_ucast_enabled'])
        self.assertEqual(args['stormctrl_ucast_rate'], 60)

    def test_configure_port_with_port_security(self):
        """Test port configuration with port security."""
        # Enable port security
        cfg.CONF.set_override('enable_port_security', True, group='unifi')

        # Set up mock for port_conf
        self.mock_controller.devices.async_set_port_conf.return_value = asyncio.Future()
        self.mock_controller.devices.async_set_port_conf.return_value.set_result(True)

        # Test configure port
        self.driver._configure_port(MOCK_SWITCH_MAC, MOCK_PORT_ID, 'port-1', MOCK_VLAN_ID)

        # Verify port security parameters
        args = self.mock_controller.devices.async_set_port_conf.call_args[0][0]
        self.assertEqual(args['dot1x_ctrl'], 'force_authorized')
        self.assertTrue(args['stp_port_fast'])
        self.assertTrue(args['stp_bpdu_guard'])
        self.assertTrue(args['stp_loop_guard'])

    def test_unconfigure_port(self):
        """Test port unconfiguration."""
        # Set up mock for port_conf
        self.mock_controller.devices.async_set_port_conf.return_value = asyncio.Future()
        self.mock_controller.devices.async_set_port_conf.return_value.set_result(True)

        # Test unconfigure port
        self.driver._unconfigure_port(MOCK_SWITCH_MAC, MOCK_PORT_ID)

        # Verify port configuration was called
        self.mock_controller.devices.async_set_port_conf.assert_called_once()

        # Verify the port configuration parameters
        args = self.mock_controller.devices.async_set_port_conf.call_args[0][0]
        self.assertEqual(args['mac'], MOCK_SWITCH_MAC)
        self.assertEqual(args['port_idx'], int(MOCK_PORT_ID))
        self.assertFalse(args['port_vlan_enabled'])
        self.assertEqual(args['tagged_vlan'], [])

    def test_bind_port(self):
        """Test port binding."""
        # Create mock for port context
        port_context = mock.MagicMock()
        port_context.segments_to_bind = [
            {
                ml2_api.ID: 'segment_id',
                ml2_api.NETWORK_TYPE: 'vlan'
            }
        ]
        port_context.current = {
            'id': 'port-id',
            'binding:profile': {
                'local_link_information': [
                    {
                        'switch_id': MOCK_SWITCH_MAC,
                        'port_id': MOCK_PORT_ID
                    }
                ]
            }
        }

        # Test bind_port
        result = self.driver.bind_port(port_context)

        # Verify binding was successful
        self.assertTrue(result)
        port_context.set_binding.assert_called_once_with(
            'segment_id', portbindings.VIF_TYPE_OTHER,
            self.driver.vif_details, status=mock.ANY
        )

    def test_bind_port_no_segments(self):
        """Test port binding with no segments."""
        # Create mock for port context
        port_context = mock.MagicMock()
        port_context.segments_to_bind = []

        # Test bind_port
        result = self.driver.bind_port(port_context)

        # Verify binding was not successful
        self.assertFalse(result)
        port_context.set_binding.assert_not_called()

    def test_bind_port_no_link_info(self):
        """Test port binding with no local link info."""
        # Create mock for port context
        port_context = mock.MagicMock()
        port_context.segments_to_bind = [
            {
                ml2_api.ID: 'segment_id',
                ml2_api.NETWORK_TYPE: 'vlan'
            }
        ]
        port_context.current = {
            'id': 'port-id',
            'binding:profile': {}
        }

        # Test bind_port
        result = self.driver.bind_port(port_context)

        # Verify binding was not successful
        self.assertFalse(result)
        port_context.set_binding.assert_not_called()
