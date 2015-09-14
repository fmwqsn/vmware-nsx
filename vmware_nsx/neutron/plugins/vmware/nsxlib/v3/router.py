# Copyright 2015 VMware, Inc.
# All Rights Reserved
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
NSX-V3 Plugin router module
"""

import random

from neutron.common import exceptions as n_exc
from neutron.i18n import _LW
from oslo_log import log

from vmware_nsx.neutron.plugins.vmware.common import exceptions as nsx_exc
from vmware_nsx.neutron.plugins.vmware.nsxlib import v3 as nsxlib

LOG = log.getLogger(__name__)

# TODO(berlin): Remove this when we merges the edge node auto
# placement feature.
MIN_EDGE_NODE_NUM = 1

TIER0_ROUTER_LINK_PORT_NAME = "TIER0-RouterLinkPort"
TIER1_ROUTER_LINK_PORT_NAME = "TIER1-RouterLinkPort"
ROUTER_INTF_PORT_NAME = "Tier1-RouterDownLinkPort"


def validate_tier0(tier0_groups_dict, tier0_uuid):
    if tier0_uuid in tier0_groups_dict:
        return
    err_msg = None
    try:
        lrouter = nsxlib.get_logical_router(tier0_uuid)
    except nsx_exc.ResourceNotFound:
        err_msg = _("Failed to validate tier0 router %s since it is "
                    "not found at the backend") % tier0_uuid
    else:
        edge_cluster_uuid = lrouter.get('edge_cluster_id')
        if not edge_cluster_uuid:
            err_msg = _("Failed to get edge cluster uuid from tier0 "
                        "router %s at the backend") % lrouter
        else:
            edge_cluster = nsxlib.get_edge_cluster(edge_cluster_uuid)
            member_index_list = [member['member_index']
                                 for member in edge_cluster['members']]
            if len(member_index_list) < MIN_EDGE_NODE_NUM:
                err_msg = _("%(act_num)s edge members found in "
                            "edge_cluster %(cluster_id)s, however we "
                            "require at least %(exp_num)s edge nodes "
                            "in edge cluster for HA use.") % {
                    'act_num': len(member_index_list),
                    'exp_num': MIN_EDGE_NODE_NUM,
                    'cluster_id': edge_cluster_uuid}
    if err_msg:
        raise n_exc.InvalidInput(error_message=err_msg)
    else:
        tier0_groups_dict[tier0_uuid] = {
            'edge_cluster_uuid': edge_cluster_uuid,
            'member_index_list': member_index_list}


def add_router_link_port(tier1_uuid, tier0_uuid, edge_members):
    # Create Tier0 logical router link port
    tier0_link_port = nsxlib.create_logical_router_port(
        tier0_uuid, display_name=TIER0_ROUTER_LINK_PORT_NAME,
        resource_type=nsxlib.LROUTERPORT_LINK,
        logical_port_id=None,
        address_groups=None)
    linked_logical_port_id = tier0_link_port['id']

    edge_cluster_member_index = random.sample(
        edge_members, MIN_EDGE_NODE_NUM)
    # Create Tier1 logical router link port
    nsxlib.create_logical_router_port(
        tier1_uuid, display_name=TIER1_ROUTER_LINK_PORT_NAME,
        resource_type=nsxlib.LROUTERPORT_LINK,
        logical_port_id=linked_logical_port_id,
        address_groups=None,
        edge_cluster_member_index=edge_cluster_member_index)


def remove_router_link_port(tier1_uuid, tier0_uuid):
    try:
        tier1_link_port = nsxlib.get_tier1_logical_router_link_port(
            tier1_uuid)
    except nsx_exc.ResourceNotFound:
        LOG.warning(_LW("Logical router link port for tier1 router: %s "
                        "not found at the backend"), tier1_uuid)
        return
    tier1_link_port_id = tier1_link_port['id']
    tier0_link_port_id = tier1_link_port['linked_logical_router_port_id']
    nsxlib.delete_logical_router_port(tier1_link_port_id)
    nsxlib.delete_logical_router_port(tier0_link_port_id)


def update_advertisement(logical_router_id, advertise_route_nat,
                         advertise_route_connected):
    return nsxlib.update_logical_router_advertisement(
        logical_router_id,
        advertise_nat_routes=advertise_route_nat,
        advertise_connected_routes=advertise_route_connected)


def delete_gw_snat_rule(logical_router_id, gw_ip):
    return nsxlib.delete_nat_rule_by_values(logical_router_id,
                                            translated_network=gw_ip)


def add_gw_snat_rule(logical_router_id, gw_ip):
    return nsxlib.add_nat_rule(logical_router_id, action="SNAT",
                               translated_network=gw_ip,
                               rule_priority=1000)


def update_router_edge_cluster(nsx_router_id, edge_cluster_uuid):
    return nsxlib.update_logical_router(nsx_router_id,
                                        edge_cluster_id=edge_cluster_uuid)


def create_logical_router_intf_port_by_ls_id(logical_router_id,
                                             ls_id,
                                             logical_switch_port_id,
                                             address_groups):
    try:
        port = nsxlib.get_logical_router_port_by_ls_id(ls_id)
    except nsx_exc.ResourceNotFound:
        return nsxlib.create_logical_router_port(logical_router_id,
                                                 ROUTER_INTF_PORT_NAME,
                                                 nsxlib.LROUTERPORT_DOWNLINK,
                                                 logical_switch_port_id,
                                                 address_groups)
    else:
        return nsxlib.update_logical_router_port(
            port['id'], subnets=address_groups)