#################################
Contributing to UniFi ML2 Driver
#################################

If you would like to contribute to the development of UniFi ML2 Driver project, you can submit pull requests through GitHub:

   https://github.com/ubiquity-community/networking-unifi

Contributor License Agreement
=============================

.. index::
   single: license; agreement

In order to contribute to the UniFi ML2 Driver project, you must agree to the terms of the Apache License 2.0.

Related Projects
================

   * https://docs.openstack.org/neutron/latest
   * https://docs.openstack.org/ironic/latest
   * https://github.com/aiounifi/aiounifi

Project Hosting Details
=======================

Bug tracker
    https://github.com/ubiquity-community/networking-unifi/issues

Code Hosting
    https://github.com/ubiquity-community/networking-unifi

Getting Started as a Contributor
===============================

1. Fork the repository on GitHub
2. Clone your fork locally
3. Set up a virtual environment using Poetry:

   .. code-block:: console

      $ poetry install

4. Run tests to make sure everything is working:

   .. code-block:: console

      $ poetry run pytest

5. Create a branch for your changes:

   .. code-block:: console

      $ git checkout -b feature/your-feature-name

6. Make your changes and commit them with descriptive commit messages
7. Push your changes to your fork
8. Submit a pull request

Development Environment
=====================

The UniFi ML2 Driver project uses Poetry for dependency management and packaging.

* To install development dependencies:

  .. code-block:: console

     $ poetry install

* To run tests:

  .. code-block:: console

     $ poetry run pytest

* To run linting:

  .. code-block:: console

     $ poetry run flake8 networking_unifi
     $ poetry run mypy networking_unifi

* To format code:

  .. code-block:: console

     $ poetry run black networking_unifi
     $ poetry run isort networking_unifi

Documentation
============

Documentation is built using Sphinx. To build the documentation:

.. code-block:: console

   $ cd doc
   $ pip install -r requirements.txt
   $ sphinx-build -b html source build/html
