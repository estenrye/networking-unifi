# UniFi ML2 Driver for Neutron (estenrye fork)

This is [estenrye/networking-unifi](https://github.com/estenrye/networking-unifi),
a fork of [ubiquiti-community/networking-unifi](https://github.com/ubiquiti-community/networking-unifi)
published to PyPI as `unifi-ml2-driver-estenrye` rather than the upstream
`unifi-ml2-driver` name. Two differences from upstream, both found and
verified live against a real
[Platform9 Private Cloud Director](https://platform9.com/private-cloud-director/)
cluster by [neutron-ml2-guardian](https://github.com/estenrye/neutron-ml2-guardian):

1. Fixes `_get_controller()` raising `oslo_config.cfg.NoSuchOptError` on
   every real network create/update/delete (see
   [upstream PR #17](https://github.com/ubiquiti-community/networking-unifi/pull/17),
   open as of this writing).
2. Relaxes `requires-python` back to `>=3.10.12` from upstream's
   `>=3.12.0` -- verified the actual code (this driver's and its
   `aiohttp-unifi>=88` dependency's) runs correctly on real Python
   3.10.12 with a small `sitecustomize.py` back-porting the handful of
   3.11+-only `typing`/`enum` symbols it uses; the `>=3.12.0` constraint
   upstream declares is not something the code itself actually requires.

Same import name (`unifi_ml2_driver`) and entry point (`unifi`) as
upstream -- only the PyPI distribution name differs, so this is a
drop-in `pip install` replacement. Distributed under the same Apache
License, Version 2.0 as upstream; see LICENSE.

This is a Modular Layer 2 [Neutron Mechanism driver](https://wiki.openstack.org/wiki/Neutron/ML2) specifically designed for Ubiquiti UniFi switches. The mechanism driver is responsible for applying configuration information to UniFi hardware equipment.

The unifi-ml2-driver provides integration between OpenStack Neutron and Ubiquiti UniFi Network controllers to manage switch ports, VLANs, and other features on UniFi switches. It's designed to support use-cases like OpenStack Ironic multi-tenancy mode and abstracts applying changes to all UniFi switches managed by the UniFi controller.

UniFi ML2 Driver is distributed under the terms of the Apache License, Version 2.0. The full terms and conditions of this license are detailed in the LICENSE file.

## Project resources

- Source: [https://github.com/ubiquity-community/unifi-ml2-driver](https://github.com/ubiquity-community/unifi-ml2-driver)
- Python Package: [https://pypi.org/project/unifi-ml2-driver/](https://pypi.org/project/unifi-ml2-driver/)

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
