# Copyright 2026 Esten Rye
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

"""UniFi NAT policy ("Policy Engine -> Policy Table", Policy Type: NAT).

aiounifi has no model for this resource at all -- confirmed by inspecting
the installed package (no nat.py/nat_policy.py anywhere, and
aiounifi.models.firewall_policy's TypedFirewallPolicy has a different
shape entirely: zone-to-zone ALLOW/DENY rules, not
masquerade/SRC_NAT/DEST_NAT with an out_interface). The real endpoint and
schema here were found live against a real UDM-SE via the UniFi app's own
browser Network tab (GET /proxy/network/v2/api/site/{site}/nat), after
several guessed paths (trafficrules, nat-rules, policies, firewall/nat,
...) all 404'd or came back empty. Defined locally by subclassing
aiounifi's own ApiRequestV2 -- the same base class aiounifi's own
firewall_zone.py and network.py build on -- rather than waiting on an
upstream aiounifi contribution for this resource.

Only the MASQUERADE policy type is modeled: this driver only ever creates
NAT66 masquerade rules, never SRC_NAT/DEST_NAT port-forwarding rules.

Minimal write payload confirmed live (create, update, and delete all
verified against a real UDM-SE): _id, is_predefined, rule_index, and
setting_preference are GET-only/server-computed and must be omitted from
POST bodies (a fresh create works and gets its own server-assigned _id
without them); the same fields set below round-trip cleanly through PUT
once _id is added back in for the update path.
"""

from dataclasses import dataclass
from typing import NotRequired, Self, TypedDict

from aiounifi.models.api import ApiRequestV2


class TypedNatPolicyFilter(TypedDict):
    """Source/destination filter for a NAT policy."""

    filter_type: str  # "NONE" | "ADDRESS_AND_PORT"
    firewall_group_ids: NotRequired[list]
    invert_address: NotRequired[bool]
    invert_port: NotRequired[bool]
    address: NotRequired[str]


class TypedNatPolicy(TypedDict):
    """NAT policy type definition (Policy Engine -> Policy Table, Policy Type: NAT)."""

    _id: NotRequired[str]
    description: str
    destination_filter: TypedNatPolicyFilter
    enabled: bool
    exclude: bool
    ip_version: str  # "IPV4" | "IPV6"
    is_predefined: NotRequired[bool]
    logging: bool
    out_interface: str
    pppoe_use_base_interface: bool
    protocol: str
    rule_index: NotRequired[int]
    setting_preference: NotRequired[str]
    source_filter: TypedNatPolicyFilter
    type: str  # "MASQUERADE" | "SRC_NAT" | "DEST_NAT" -- this driver only ever writes "MASQUERADE"


@dataclass
class NatPolicyListRequest(ApiRequestV2):
    """Request object for listing NAT policies."""

    @classmethod
    def create(cls) -> Self:
        """Create NAT policy list request."""
        return cls(method="get", path="/nat", data=None)


@dataclass
class NatPolicyCreateRequest(ApiRequestV2):
    """Request object for creating a NAT policy."""

    @classmethod
    def create(cls, policy: TypedNatPolicy) -> Self:
        """Create NAT policy create request."""
        return cls(method="post", path="/nat", data=policy)


@dataclass
class NatPolicyUpdateRequest(ApiRequestV2):
    """Request object for updating a NAT policy."""

    @classmethod
    def create(cls, policy: TypedNatPolicy) -> Self:
        """Create NAT policy update request."""
        return cls(method="put", path=f"/nat/{policy['_id']}", data=policy)


@dataclass
class NatPolicyDeleteRequest(ApiRequestV2):
    """Request object for deleting a NAT policy."""

    @classmethod
    def create(cls, policy_id: str) -> Self:
        """Create NAT policy delete request."""
        return cls(method="delete", path=f"/nat/{policy_id}", data=None)
