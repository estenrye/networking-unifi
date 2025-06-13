from __future__ import annotations

import asyncio
import ssl
from types import MappingProxyType
from typing import Any, Literal

from aiohttp import CookieJar, ClientSession
import aiounifi
from aiounifi.models.configuration import Configuration

from neutron_lib.api.definitions import dns as dns_apidef

from .exceptions import AuthenticationRequired, CannotConnect

from oslo_log import log as logging

from oslo_config import cfg
LOG = logging.getLogger(__name__)
CONF = cfg.CONF

class UnifiDnsHandler(object):
    """DNS integration handler for UniFi ML2 mechanism driver.
    
    This handler is responsible for creating, updating, and deleting DNS records
    for Neutron ports in the UniFi controller DNS configuration.
    """
    
    def __init__(self, mech_driver):
        """Initialize the DNS handler.
        
        Args:
            mech_driver: The UnifiMechDriver instance
        """
        self.mech_driver = mech_driver
        self._enabled = CONF.unifi.dns_integration_enabled
        self.domain = CONF.unifi.dns_domain
        
        if self._enabled and not self.domain:
            LOG.warning("DNS integration is enabled but no domain is configured. "
                       "DNS integration will be disabled.")
            self._enabled = False
            
        if self._enabled:
            LOG.info("DNS integration enabled with domain %s", self.domain)
        
    @property
    def enabled(self):
        """Returns whether DNS integration is enabled."""
        return self._enabled
        
    def _get_dns_domain(self, network):
        """Get the DNS domain for a network.
        
        Args:
            network: The network dictionary
            
        Returns:
            The DNS domain for the network or None
        """
        if not network:
            return None
            
        # Check if network has a DNS domain specified
        network_domain = network.get(dns_apidef.DNSDOMAIN)
        if network_domain:
            return network_domain
            
        # Fall back to configured domain
        return self.domain
        
    def _build_dns_name(self, port, network):
        """Build the complete DNS name for a port.
        
        Args:
            port: The port dictionary
            network: The network dictionary
            
        Returns:
            The full DNS name or None if not applicable
        """
        port_dns_name = port.get(dns_apidef.DNSNAME)
        if not port_dns_name:
            return None
            
        domain = self._get_dns_domain(network)
        if not domain:
            return None
            
        # Format the complete DNS name
        dns_name = CONF.unifi.dns_domain_format.format(
            port_id=port.get('id', ''),
            network_id=port.get('network_id', ''),
            dns_name=port_dns_name,
            dns_domain=domain
        )
        
        return dns_name.rstrip('.')
        
    async def _create_dns_record_async(self, controller, name, ip_address, ip_version=4):
        """Create a DNS record in the UniFi controller.
        
        Args:
            controller: UniFi controller client
            name: DNS name
            ip_address: IP address
            ip_version: IP version (4 or 6)
            
        Returns:
            True if successful
        """
        record_type = 'A' if ip_version == 4 else 'AAAA'
        
        # Check if record exists
        dns_records = await controller.sites.dnsrecords.async_get()
        existing_record = next(
            (r for r in dns_records if hasattr(r, 'name') and r.name == name),
            None
        )
        
        if existing_record:
            # Update existing record if IP has changed
            if not hasattr(existing_record, 'content') or existing_record.content != ip_address:
                LOG.debug("Updating DNS record %s -> %s", name, ip_address)
                record_data = {
                    'id': existing_record.id,
                    'name': name,
                    'content': ip_address,
                    'type': record_type,
                    'ttl': 300
                }
                await controller.sites.dnsrecords.async_update_record(record_data)
        else:
            # Create new record
            LOG.debug("Creating DNS record %s -> %s", name, ip_address)
            record_data = {
                'name': name,
                'content': ip_address,
                'type': record_type,
                'ttl': 300
            }
            await controller.sites.dnsrecords.async_create_record(record_data)
        
        return True
        
    async def _delete_dns_record_async(self, controller, name):
        """Delete DNS records for a name from the UniFi controller.
        
        Args:
            controller: UniFi controller client
            name: DNS name
            
        Returns:
            True if successful
        """
        # Find records with this name
        dns_records = await controller.sites.dnsrecords.async_get()
        records_to_delete = [
            r for r in dns_records if hasattr(r, 'name') and r.name == name
        ]
        
        for record in records_to_delete:
            LOG.debug("Deleting DNS record %s (id: %s)", name, record.id)
            await controller.sites.dnsrecords.async_delete_record(record.id)
        
        return True
        
    def create_port_dns_records(self, context, port, network):
        """Create DNS records for a port.
        
        Args:
            context: Neutron request context
            port: The port dictionary
            network: The network dictionary
            
        Returns:
            True if successful
        """
        if not self.enabled:
            return True
            
        dns_name = self._build_dns_name(port, network)
        if not dns_name:
            return True
            
        try:
            with self.mech_driver._get_controller() as controller:
                loop = asyncio.get_event_loop()
                
                # Add a record for each fixed IP
                for fixed_ip in port.get('fixed_ips', []):
                    ip_address = fixed_ip.get('ip_address')
                    if not ip_address:
                        continue
                        
                    # Determine IP version
                    ip_version = 4 if ':' not in ip_address else 6
                    
                    loop.run_until_complete(
                        self._create_dns_record_async(controller, dns_name,
                                                     ip_address, ip_version)
                    )
                    LOG.info("Created DNS record for port %s: %s -> %s",
                             port['id'], dns_name, ip_address)
            
            return True
        except Exception as e:
            LOG.error("Failed to create DNS record for port %s: %s",
                     port['id'], e)
            return False
    
    def update_port_dns_records(self, context, port, network, original_port=None):
        """Update DNS records for a port.
        
        Args:
            context: Neutron request context
            port: The port dictionary
            network: The network dictionary
            original_port: The original port dictionary before the update
            
        Returns:
            True if successful
        """
        if not self.enabled:
            return True
            
        # Delete old DNS records if dns_name changed
        if original_port and (original_port.get(dns_apidef.DNSNAME) != 
                              port.get(dns_apidef.DNSNAME)):
            self.delete_port_dns_records(context, original_port, network)
            
        # Create new records
        return self.create_port_dns_records(context, port, network)
    
    def delete_port_dns_records(self, context, port, network):
        """Delete DNS records for a port.
        
        Args:
            context: Neutron request context
            port: The port dictionary
            network: The network dictionary
            
        Returns:
            True if successful
        """
        if not self.enabled:
            return True
            
        dns_name = self._build_dns_name(port, network)
        if not dns_name:
            return True
            
        try:
            with self.mech_driver._get_controller() as controller:
                loop = asyncio.get_event_loop()
                
                loop.run_until_complete(
                    self._delete_dns_record_async(controller, dns_name)
                )
                
                LOG.info("Deleted DNS records for port %s with name %s",
                         port['id'], dns_name)
            
            return True
        except Exception as e:
            LOG.error("Failed to delete DNS records for port %s: %s",
                     port['id'], e)
            return False