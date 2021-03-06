# Copyright 2016 VMware, Inc.  All rights reserved.
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

import logging
import netaddr

from neutron.callbacks import registry
from neutron_lib import constants as const
from oslo_config import cfg

from vmware_nsx._i18n import _LE, _LI
from vmware_nsx.common import nsx_constants
from vmware_nsx.common import utils as nsx_utils
from vmware_nsx.nsxlib.v3 import native_dhcp
from vmware_nsx.nsxlib.v3 import resources
from vmware_nsx.shell.admin.plugins.common import constants
from vmware_nsx.shell.admin.plugins.common import formatters
from vmware_nsx.shell.admin.plugins.common import utils as admin_utils
from vmware_nsx.shell.admin.plugins.nsxv3.resources import utils
import vmware_nsx.shell.resources as shell

LOG = logging.getLogger(__name__)
neutron_client = utils.NeutronDbClient()


@admin_utils.output_header
def list_dhcp_bindings(resource, event, trigger, **kwargs):
    """List DHCP bindings in Neutron."""

    comp_ports = [port for port in neutron_client.get_ports()
                  if port['device_owner'].startswith(
                      const.DEVICE_OWNER_COMPUTE_PREFIX)]
    LOG.info(formatters.output_formatter(constants.DHCP_BINDING, comp_ports,
                                         ['id', 'mac_address', 'fixed_ips']))


@admin_utils.output_header
def nsx_update_dhcp_bindings(resource, event, trigger, **kwargs):
    """Resync DHCP bindings for NSXv3 CrossHairs."""

    nsx_version = utils.get_connected_nsxlib().get_version()
    if not nsx_utils.is_nsx_version_1_1_0(nsx_version):
        LOG.info(_LI("This utility is not available for NSX version %s"),
                 nsx_version)
        return

    dhcp_profile_uuid = None
    if kwargs.get('property'):
        properties = admin_utils.parse_multi_keyval_opt(kwargs['property'])
        dhcp_profile_uuid = properties.get('dhcp_profile_uuid')
    if not dhcp_profile_uuid:
        LOG.error(_LE("dhcp_profile_uuid is not defined"))
        return

    cfg.CONF.set_override('dhcp_agent_notification', False)
    cfg.CONF.set_override('native_dhcp_metadata', True, 'nsx_v3')
    cfg.CONF.set_override('dhcp_profile_uuid', dhcp_profile_uuid, 'nsx_v3')

    nsx_client = utils.get_nsxv3_client()
    port_resource = resources.LogicalPort(nsx_client)
    dhcp_server_resource = resources.LogicalDhcpServer(nsx_client)

    port_bindings = {}    # lswitch_id: [(port_id, mac, ip), ...]
    server_bindings = {}  # lswitch_id: dhcp_server_id
    ports = neutron_client.get_ports()
    for port in ports:
        device_owner = port['device_owner']
        if (device_owner != const.DEVICE_OWNER_DHCP and
            not device_owner.startswith(const.DEVICE_OWNER_COMPUTE_PREFIX)):
            continue
        for fixed_ip in port['fixed_ips']:
            if netaddr.IPNetwork(fixed_ip['ip_address']).version == 6:
                continue
            network_id = port['network_id']
            subnet = neutron_client.get_subnet(fixed_ip['subnet_id'])
            if device_owner == const.DEVICE_OWNER_DHCP:
                # For each DHCP-enabled network, create a logical DHCP server
                # and update the attachment type to DHCP on the corresponding
                # logical port of the Neutron DHCP port.
                network = neutron_client.get_network(port['network_id'])
                server_data = native_dhcp.build_dhcp_server_config(
                    network, subnet, port, 'admin')
                dhcp_server = dhcp_server_resource.create(**server_data)
                LOG.info(_LI("Created logical DHCP server %(server)s for "
                             "network %(network)s"),
                         {'server': dhcp_server['id'],
                          'network': port['network_id']})
                # Add DHCP service binding in neutron DB.
                neutron_client.add_dhcp_service_binding(
                    network['id'], port['id'], dhcp_server['id'])
                # Update logical port for DHCP purpose.
                lswitch_id, lport_id = (
                    neutron_client.get_lswitch_and_lport_id(port['id']))
                port_resource.update(
                    lport_id, dhcp_server['id'],
                    attachment_type=nsx_constants.ATTACHMENT_DHCP)
                server_bindings[lswitch_id] = dhcp_server['id']
                LOG.info(_LI("Updated DHCP logical port %(port)s for "
                             "network %(network)s"),
                         {'port': lport_id, 'network': port['network_id']})
            elif subnet['enable_dhcp']:
                # Store (mac, ip) binding of each compute port in a
                # DHCP-enabled subnet.
                lswitch_id = neutron_client.net_id_to_lswitch_id(network_id)
                bindings = port_bindings.get(lswitch_id, [])
                bindings.append((port['id'], port['mac_address'],
                                 fixed_ip['ip_address']))
                port_bindings[lswitch_id] = bindings
            break  # process only the first IPv4 address

    # Populate mac/IP bindings in each logical DHCP server.
    for lswitch_id, bindings in port_bindings.items():
        dhcp_server_id = server_bindings.get(lswitch_id)
        if not dhcp_server_id:
            continue
        for (port_id, mac, ip) in bindings:
            hostname = 'host-%s' % ip.replace('.', '-')
            options = {'option121': {'static_routes': [
                {'network': '%s' % cfg.CONF.nsx_v3.native_metadata_route,
                 'next_hop': ip}]}}
            dhcp_server_resource.create_binding(
                dhcp_server_id, mac, ip, hostname,
                cfg.CONF.nsx_v3.dhcp_lease_time, options)
            LOG.info(_LI("Added DHCP binding (mac: %(mac)s, ip: %(ip)s) "
                         "for neutron port %(port)s"),
                     {'mac': mac, 'ip': ip, 'port': port_id})


registry.subscribe(list_dhcp_bindings,
                   constants.DHCP_BINDING,
                   shell.Operations.LIST.value)
registry.subscribe(nsx_update_dhcp_bindings,
                   constants.DHCP_BINDING,
                   shell.Operations.NSX_UPDATE.value)
