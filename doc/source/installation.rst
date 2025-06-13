============
Installation
============

This section describes how to install and configure the networking-unifi plugin.

Requirements
-----------

* Python 3.12 or higher
* OpenStack Neutron 13.0.0 or higher
* UniFi Network Controller
* UniFi switches

Installation Methods
------------------

Via pip (recommended)
~~~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ pip install networking-unifi

Via Poetry
~~~~~~~~~

.. code-block:: console

   $ cd networking-unifi
   $ poetry install

Via Source
~~~~~~~~~

.. code-block:: console

   $ git clone https://github.com/ubiquity-community/networking-unifi.git
   $ cd networking-unifi
   $ pip install .

Enabling the Driver in Neutron
----------------------------

To enable the UniFi mechanism driver in Neutron's ML2 plugin, edit the
``/etc/neutron/plugins/ml2/ml2_conf.ini`` file on the Neutron server:

.. code-block:: ini

   [ml2]
   tenant_network_types = vlan
   type_drivers = local,flat,vlan,gre,vxlan
   mechanism_drivers = openvswitch,unifi

For a standard installation, you'll also need to configure the UniFi controller connection details
in a separate configuration file. Create or edit ``/etc/neutron/plugins/ml2/ml2_conf_unifi.ini``:

.. code-block:: ini

   [unifi]
   host = https://<controller-ip>
   username = <admin-username>
   password = <admin-password>
   site = default
   verify_ssl = True

After making these changes, restart the Neutron server:

.. code-block:: console

   $ systemctl restart neutron-server

or with DevStack:

.. code-block:: console

   $ systemctl restart devstack@q-svc
