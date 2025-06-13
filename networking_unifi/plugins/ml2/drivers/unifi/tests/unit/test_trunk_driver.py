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

import unittest
from unittest import mock

import aiounifi
from neutron.services.trunk.drivers import base as trunk_base
from neutron_lib.api.definitions import portbindings
from neutron_lib.callbacks import events
from neutron_lib.callbacks import registry
from neutron_lib.callbacks import resources
from neutron_lib.db import api as db_api
from neutron_lib.plugins import directory
from oslo_config import cfg

from networking_unifi import trunk_driver

MECH_DRIVER_NAME = 'unifi'


class UnifiTrunkDriverFixture(unittest.TestCase):
    """Base fixture for UniFiTrunkDriver tests."""

    def setUp(self):
        """Set up test environment."""
        # Mock configuration
        self.cfg_mock = mock.patch.object(cfg, 'CONF')
        self.cfg_mock.start()

        # Set configuration values
        cfg.CONF.ml2 = mock.MagicMock()
        cfg.CONF.ml2.mechanism_drivers = ['unifi']

        # Mock plugin driver
        self.plugin_driver = mock.MagicMock()

        # Create driver instance
        self.trunk_driver = trunk_driver.UnifiTrunkDriver.create(self.plugin_driver)

    def tearDown(self):
        """Clean up test environment."""
        self.cfg_mock.stop()


class TestUnifiTrunkDriver(UnifiTrunkDriverFixture):
    """Test cases for UniFi trunk driver."""

    def test_driver_creation(self):
        """Test proper trunk driver creation."""
        self.assertEqual(self.trunk_driver.plugin_driver, self.plugin_driver)
        self.assertEqual(self.trunk_driver.agent_type, MECH_DRIVER_NAME)
        self.assertEqual(
            self.trunk_driver.interfaces,
            trunk_driver.SUPPORTED_INTERFACES
        )
        self.assertEqual(
            self.trunk_driver.segmentation_types,
            trunk_driver.SUPPORTED_SEGMENTATION_TYPES
        )

    def test_is_loaded(self):
        """Test loaded state of the driver."""
        # When mechanism driver is loaded
        self.assertTrue(self.trunk_driver.is_loaded)

        # When it's not loaded
        cfg.CONF.ml2.mechanism_drivers = ['other_driver']
        self.assertFalse(self.trunk_driver.is_loaded)

        # When mechanism_drivers not set
        cfg.CONF.ml2 = mock.MagicMock()
        delattr(cfg.CONF.ml2, 'mechanism_drivers')
        with mock.patch.object(cfg.CONF.ml2, '__getattr__',
                              side_effect=cfg.NoSuchOptError('mechanism_drivers')):
            self.assertFalse(self.trunk_driver.is_loaded)

    def test_register(self):
        """Test driver registration."""
        # Set up mocks
        mock_registry = mock.patch.object(registry, 'subscribe')
        mock_reg = mock_registry.start()

        # Call register
        self.trunk_driver.register(
            resources.TRUNK_PLUGIN, events.AFTER_INIT, mock.MagicMock())

        # Verify handler was created and subscriptions made
        self.assertIsNotNone(self.trunk_driver._handler)
        mock_reg.assert_any_call(
            self.trunk_driver._handler.subports_added,
            resources.SUBPORTS,
            events.AFTER_CREATE)
        mock_reg.assert_any_call(
            self.trunk_driver._handler.subports_deleted,
            resources.SUBPORTS,
            events.AFTER_DELETE)

        mock_registry.stop()


class TestUnifiTrunkHandler(UnifiTrunkDriverFixture):
    """Test cases for UniFi trunk handler."""

    def setUp(self):
        """Set up test environment."""
        super(TestUnifiTrunkHandler, self).setUp()

        # Create handler instance
        self.handler = trunk_driver.UnifiTrunkHandler(self.plugin_driver)

        # Mock plugin
        self.core_plugin = mock.MagicMock()
        mock_directory = mock.patch.object(
            directory, 'get_plugin', return_value=self.core_plugin)
        mock_directory.start()
        self.addCleanup(mock_directory.stop)

        # Mock context
        self.context_mock = mock.patch.object(db_api, 'CONTEXT_READER')
        self.context_mock.start()
        self.addCleanup(self.context_mock.stop)

    def test_subports_added(self):
        """Test subports_added handler."""
        # Set up mock trunk
        trunk = mock.MagicMock()
        trunk.port_id = 'parent-port-id'

        # Set up mock subports
        subports = [
            {'port_id': 'subport-1', 'segmentation_id': 100},
            {'port_id': 'subport-2', 'segmentation_id': 200}
        ]

        # Set up mock payload
        payload = mock.MagicMock()
        payload.states = [trunk]
        payload.metadata = {'subports': subports}

        # Mock port object
        parent_port = mock.MagicMock()
        parent_port_dict = {'id': 'parent-port-id'}
        self.core_plugin._make_port_dict.return_value = parent_port_dict

        # Call subports_added
        self.handler.subports_added(resources.SUBPORTS, events.AFTER_CREATE,
                                   mock.MagicMock(), payload)

        # Verify plugin driver was called
        self.plugin_driver.subports_added.assert_called_once_with(
            mock.ANY, parent_port_dict, subports
        )

    def test_subports_deleted(self):
        """Test subports_deleted handler."""
        # Set up mock trunk
        trunk = mock.MagicMock()
        trunk.port_id = 'parent-port-id'

        # Set up mock subports
        subports = [
            {'port_id': 'subport-1', 'segmentation_id': 100},
            {'port_id': 'subport-2', 'segmentation_id': 200}
        ]

        # Set up mock payload
        payload = mock.MagicMock()
        payload.states = [trunk]
        payload.metadata = {'subports': subports}

        # Mock port object
        parent_port = mock.MagicMock()
        parent_port_dict = {'id': 'parent-port-id'}
        self.core_plugin._make_port_dict.return_value = parent_port_dict

        # Call subports_deleted
        self.handler.subports_deleted(resources.SUBPORTS, events.AFTER_DELETE,
                                     mock.MagicMock(), payload)

        # Verify plugin driver was called
        self.plugin_driver.subports_deleted.assert_called_once_with(
            mock.ANY, parent_port_dict, subports
        )
