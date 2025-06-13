# UniFi ML2 Driver for Neutron

This is a Modular Layer 2 [Neutron Mechanism driver](https://wiki.openstack.org/wiki/Neutron/ML2) specifically designed for Ubiquiti UniFi switches. The mechanism driver is responsible for applying configuration information to UniFi hardware equipment.

The networking-unifi provides integration between OpenStack Neutron and Ubiquiti UniFi Network controllers to manage switch ports, VLANs, and other features on UniFi switches. It's designed to support use-cases like OpenStack Ironic multi-tenancy mode and abstracts applying changes to all UniFi switches managed by the UniFi controller.

UniFi ML2 Driver is distributed under the terms of the Apache License, Version 2.0. The full terms and conditions of this license are detailed in the LICENSE file.

## Project resources

- Source: [https://github.com/ubiquity-community/networking-unifi](https://github.com/ubiquity-community/networking-unifi)
- Python Package: [https://pypi.org/project/networking-unifi/](https://pypi.org/project/networking-unifi/)

## Features

- VLAN Networks: Creating and managing VLAN networks on UniFi switches
- Port Binding: Binding ports to specific switch ports

- Trunk Ports: Managing trunk ports with native and tagged VLANs
- Port Security: Configuring port security features (BPDU guard, loop guard)
- QoS: Bandwidth limiting on a per-port basis
- Storm Control: Limiting broadcast, multicast, and unknown unicast traffic
- Port Monitoring: Monitoring port state and updating OpenStack port status

## Requirements

- Python 3.12+
- OpenStack Neutron 13.0.0+
- aiounifi 83+
- UniFi Network Controller
- UniFi switches
