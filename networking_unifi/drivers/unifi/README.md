# UniFi Driver for networking-baremetal

This directory contains the UniFi driver implementation that provides compatibility with the networking-baremetal framework.

## Overview

The UniFi driver (`unifi.py`) implements the same functionality as the ML2 mechanism driver (`networking_unifi/plugins/ml2/drivers/unifi/unifi_mech.py`) but structured as a networking-baremetal device driver.

## Key Components

- **`unifi.py`**: Main driver implementation
- **`exceptions.py`**: UniFi-specific exceptions
- **`config.py`**: Configuration options for the driver
- **`__init__.py`**: Package initialization

## Features

The driver supports:

- Network creation/deletion (VLAN management)
- Port configuration on UniFi switches
- Storm control settings
- Port security features
- Retry logic for API calls
- Integration with UniFi Network controllers

## Configuration

The driver uses the same configuration options as the ML2 driver:

```ini
[unifi]
host = 10.0.0.1
port = 8443
apikey = your-api-key  # or use username/password
username = admin
password = your-password
site = default
verify_ssl = True
port_name_format = openstack-port-{port_id}
enable_storm_control = False
enable_port_security = True
```

## Usage

The driver is automatically registered as a networking-baremetal driver through the entry point:

```python
networking_baremetal.drivers:
  unifi = networking_unifi.drivers.unifi.unifi:UnifiDriver
```

## Dependencies

- aiounifi library for UniFi API communication
- networking-baremetal framework
- neutron and neutron-lib
