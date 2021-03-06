# Copyright (c) 2015 OpenStack Foundation.
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

import mock
import netaddr

from neutron import context
from neutron.extensions import securitygroup as secgrp

from neutron_lib import constants
from neutron_lib import exceptions as n_exc
from oslo_config import cfg
from oslo_utils import uuidutils

from vmware_nsx.common import exceptions as nsx_exc
from vmware_nsx.common import nsx_constants
from vmware_nsx.common import utils
from vmware_nsx.db import db as nsx_db
from vmware_nsx.extensions import advancedserviceproviders as as_providers
from vmware_nsx.nsxlib.v3 import resources as nsx_resources
from vmware_nsx.tests.unit.nsx_v3 import test_plugin


class NsxNativeDhcpTestCase(test_plugin.NsxV3PluginTestCaseMixin):

    def setUp(self):
        super(NsxNativeDhcpTestCase, self).setUp()
        self._orig_dhcp_agent_notification = cfg.CONF.dhcp_agent_notification
        self._orig_native_dhcp_metadata = cfg.CONF.nsx_v3.native_dhcp_metadata
        cfg.CONF.set_override('dhcp_agent_notification', False)
        cfg.CONF.set_override('native_dhcp_metadata', True, 'nsx_v3')
        self._patcher = mock.patch.object(nsx_resources.DhcpProfile, 'get')
        self._patcher.start()
        # Need to run _init_dhcp_metadata() manually because plugin was started
        # before setUp() overrides CONF.nsx_v3.native_dhcp_metadata.
        self.plugin._init_dhcp_metadata()

    def tearDown(self):
        self._patcher.stop()
        cfg.CONF.set_override('dhcp_agent_notification',
                              self._orig_dhcp_agent_notification)
        cfg.CONF.set_override('native_dhcp_metadata',
                              self._orig_native_dhcp_metadata, 'nsx_v3')
        super(NsxNativeDhcpTestCase, self).tearDown()

    def _verify_dhcp_service(self, network_id, tenant_id, enabled):
        # Verify if DHCP service is enabled on a network.
        port_res = self._list_ports('json', 200, network_id,
                                    tenant_id=tenant_id,
                                    device_owner=constants.DEVICE_OWNER_DHCP)
        port_list = self.deserialize('json', port_res)
        self.assertEqual(len(port_list['ports']) == 1, enabled)

    def _verify_dhcp_binding(self, subnet, port_data, update_data,
                             assert_data):
        # Verify if DHCP binding is updated.

        with mock.patch(
            'vmware_nsx.nsxlib.v3.resources.LogicalDhcpServer.update_binding'
        ) as update_dhcp_binding:
            device_owner = constants.DEVICE_OWNER_COMPUTE_PREFIX + 'None'
            device_id = uuidutils.generate_uuid()
            with self.port(subnet=subnet, device_owner=device_owner,
                           device_id=device_id, **port_data) as port:
                # Retrieve the DHCP binding info created in the DB for the
                # new port.
                dhcp_binding = nsx_db.get_nsx_dhcp_bindings(
                    context.get_admin_context().session, port['port']['id'])[0]
                # Update the port with provided data.
                self.plugin.update_port(
                    context.get_admin_context(), port['port']['id'],
                    update_data)
                binding_data = {'mac_address': port['port']['mac_address'],
                                'ip_address': port['port']['fixed_ips'][0][
                                    'ip_address']}
                # Extend basic binding data with to-be-asserted data.
                binding_data.update(assert_data)
                # Verify the update call.
                update_dhcp_binding.assert_called_once_with(
                    dhcp_binding['nsx_service_id'],
                    dhcp_binding['nsx_binding_id'], **binding_data)

    def test_dhcp_profile_configuration(self):
        # Test if dhcp_agent_notification and dhcp_profile_uuid are
        # configured correctly.
        orig_dhcp_agent_notification = cfg.CONF.dhcp_agent_notification
        cfg.CONF.set_override('dhcp_agent_notification', True)
        self.assertRaises(nsx_exc.NsxPluginException,
                          self.plugin._init_dhcp_metadata)
        cfg.CONF.set_override('dhcp_agent_notification',
                              orig_dhcp_agent_notification)
        orig_dhcp_profile_uuid = cfg.CONF.nsx_v3.dhcp_profile_uuid
        cfg.CONF.set_override('dhcp_profile_uuid', '', 'nsx_v3')
        self.assertRaises(cfg.RequiredOptError,
                          self.plugin._init_dhcp_metadata)
        cfg.CONF.set_override('dhcp_profile_uuid', orig_dhcp_profile_uuid,
                              'nsx_v3')

    def test_dhcp_service_with_create_network(self):
        # Test if DHCP service is disabled on a network when it is created.
        with self.network() as network:
            self._verify_dhcp_service(network['network']['id'],
                                      network['network']['tenant_id'], False)

    def test_dhcp_service_with_delete_dhcp_network(self):
        # Test if DHCP service is disabled when directly deleting a network
        # with a DHCP-enabled subnet.
        with self.network() as network:
            with self.subnet(network=network, enable_dhcp=True):
                self.plugin.delete_network(context.get_admin_context(),
                                           network['network']['id'])
                self._verify_dhcp_service(network['network']['id'],
                                          network['network']['tenant_id'],
                                          False)

    def test_dhcp_service_with_create_non_dhcp_subnet(self):
        # Test if DHCP service is disabled on a network when a DHCP-disabled
        # subnet is created.
        with self.network() as network:
            with self.subnet(network=network, enable_dhcp=False):
                self._verify_dhcp_service(network['network']['id'],
                                          network['network']['tenant_id'],
                                          False)

    def test_dhcp_service_with_create_multiple_non_dhcp_subnets(self):
        # Test if DHCP service is disabled on a network when multiple
        # DHCP-disabled subnets are created.
        with self.network() as network:
            with self.subnet(network=network, cidr='10.0.0.0/24',
                             enable_dhcp=False):
                with self.subnet(network=network, cidr='20.0.0.0/24',
                                 enable_dhcp=False):
                    self._verify_dhcp_service(network['network']['id'],
                                              network['network']['tenant_id'],
                                              False)

    def test_dhcp_service_with_create_dhcp_subnet(self):
        # Test if DHCP service is enabled on a network when a DHCP-enabled
        # subnet is created.
        with self.network() as network:
            with self.subnet(network=network, enable_dhcp=True):
                self._verify_dhcp_service(network['network']['id'],
                                          network['network']['tenant_id'],
                                          True)

    def test_dhcp_service_with_create_multiple_dhcp_subnets(self):
        # Test if multiple DHCP-enabled subnets cannot be created in a network.
        with self.network() as network:
            with self.subnet(network=network, cidr='10.0.0.0/24',
                             enable_dhcp=True):
                subnet = {'subnet': {'network_id': network['network']['id'],
                                     'cidr': '20.0.0.0/24',
                                     'enable_dhcp': True}}
                self.assertRaises(
                    n_exc.InvalidInput, self.plugin.create_subnet,
                    context.get_admin_context(), subnet)

    def test_dhcp_service_with_delete_dhcp_subnet(self):
        # Test if DHCP service is disabled on a network when a DHCP-disabled
        # subnet is deleted.
        with self.network() as network:
            with self.subnet(network=network, enable_dhcp=True) as subnet:
                self._verify_dhcp_service(network['network']['id'],
                                          network['network']['tenant_id'],
                                          True)
                self.plugin.delete_subnet(context.get_admin_context(),
                                          subnet['subnet']['id'])
                self._verify_dhcp_service(network['network']['id'],
                                          network['network']['tenant_id'],
                                          False)

    def test_dhcp_service_with_update_dhcp_subnet(self):
        # Test if DHCP service is enabled on a network when a DHCP-disabled
        # subnet is updated to DHCP-enabled.
        with self.network() as network:
            with self.subnet(network=network, enable_dhcp=False) as subnet:
                self._verify_dhcp_service(network['network']['id'],
                                          network['network']['tenant_id'],
                                          False)
                data = {'subnet': {'enable_dhcp': True}}
                self.plugin.update_subnet(context.get_admin_context(),
                                          subnet['subnet']['id'], data)
                self._verify_dhcp_service(network['network']['id'],
                                          network['network']['tenant_id'],
                                          True)

    def test_dhcp_service_with_update_multiple_dhcp_subnets(self):
        # Test if a DHCP-disabled subnet cannot be updated to DHCP-enabled
        # if a DHCP-enabled subnet already exists in the same network.
        with self.network() as network:
            with self.subnet(network=network, cidr='10.0.0.0/24',
                             enable_dhcp=True):
                with self.subnet(network=network, cidr='20.0.0.0/24',
                                 enable_dhcp=False) as subnet:
                    self._verify_dhcp_service(network['network']['id'],
                                              network['network']['tenant_id'],
                                              True)
                    data = {'subnet': {'enable_dhcp': True}}
                    self.assertRaises(
                        n_exc.InvalidInput, self.plugin.update_subnet,
                        context.get_admin_context(), subnet['subnet']['id'],
                        data)

    def test_dhcp_service_with_update_dhcp_port(self):
        # Test if DHCP server IP is updated when the corresponding DHCP port
        # IP is changed.
        with mock.patch.object(nsx_resources.LogicalDhcpServer,
                               'update') as update_logical_dhcp_server:
            with self.subnet(cidr='10.0.0.0/24', enable_dhcp=True) as subnet:
                dhcp_service = nsx_db.get_nsx_service_binding(
                    context.get_admin_context().session,
                    subnet['subnet']['network_id'], nsx_constants.SERVICE_DHCP)
                port = self.plugin.get_port(context.get_admin_context(),
                                            dhcp_service['port_id'])
                old_ip = port['fixed_ips'][0]['ip_address']
                new_ip = str(netaddr.IPAddress(old_ip) + 1)
                data = {'port': {'fixed_ips': [
                    {'subnet_id': subnet['subnet']['id'],
                     'ip_address': new_ip}]}}
                self.plugin.update_port(context.get_admin_context(),
                                        dhcp_service['port_id'], data)
                update_logical_dhcp_server.assert_called_once_with(
                    dhcp_service['nsx_service_id'], server_ip=new_ip)

    def test_dhcp_binding_with_create_port(self):
        # Test if DHCP binding is added when a compute port is created.
        with mock.patch.object(nsx_resources.LogicalDhcpServer,
                               'create_binding',
                               return_value={"id": uuidutils.generate_uuid()}
                               ) as create_dhcp_binding:
            with self.subnet(enable_dhcp=True) as subnet:
                device_owner = constants.DEVICE_OWNER_COMPUTE_PREFIX + 'None'
                device_id = uuidutils.generate_uuid()
                with self.port(subnet=subnet, device_owner=device_owner,
                               device_id=device_id) as port:
                    dhcp_service = nsx_db.get_nsx_service_binding(
                        context.get_admin_context().session,
                        subnet['subnet']['network_id'],
                        nsx_constants.SERVICE_DHCP)
                    ip = port['port']['fixed_ips'][0]['ip_address']
                    hostname = 'host-%s' % ip.replace('.', '-')
                    options = {'option121': {'static_routes': [
                        {'network': '%s' %
                         cfg.CONF.nsx_v3.native_metadata_route,
                         'next_hop': ip}]}}
                    create_dhcp_binding.assert_called_once_with(
                        dhcp_service['nsx_service_id'],
                        port['port']['mac_address'], ip, hostname,
                        cfg.CONF.nsx_v3.dhcp_lease_time, options)

    def test_dhcp_binding_with_delete_port(self):
        # Test if DHCP binding is removed when the associated compute port
        # is deleted.
        with mock.patch.object(nsx_resources.LogicalDhcpServer,
                               'delete_binding') as delete_dhcp_binding:
            with self.subnet(enable_dhcp=True) as subnet:
                device_owner = constants.DEVICE_OWNER_COMPUTE_PREFIX + 'None'
                device_id = uuidutils.generate_uuid()
                with self.port(subnet=subnet, device_owner=device_owner,
                               device_id=device_id) as port:
                    dhcp_binding = nsx_db.get_nsx_dhcp_bindings(
                        context.get_admin_context().session,
                        port['port']['id'])[0]
                    self.plugin.delete_port(
                        context.get_admin_context(), port['port']['id'])
                    delete_dhcp_binding.assert_called_once_with(
                        dhcp_binding['nsx_service_id'],
                        dhcp_binding['nsx_binding_id'])

    def test_dhcp_binding_with_update_port_delete_ip(self):
        # Test if DHCP binding is deleted when the IP of the associated
        # compute port is deleted.
        with mock.patch.object(nsx_resources.LogicalDhcpServer,
                               'delete_binding') as delete_dhcp_binding:
            with self.subnet(enable_dhcp=True) as subnet:
                device_owner = constants.DEVICE_OWNER_COMPUTE_PREFIX + 'None'
                device_id = uuidutils.generate_uuid()
                with self.port(subnet=subnet, device_owner=device_owner,
                               device_id=device_id) as port:
                    dhcp_binding = nsx_db.get_nsx_dhcp_bindings(
                        context.get_admin_context().session,
                        port['port']['id'])[0]
                    data = {'port': {'fixed_ips': [],
                                     'admin_state_up': False,
                                     secgrp.SECURITYGROUPS: []}}
                    self.plugin.update_port(
                        context.get_admin_context(), port['port']['id'], data)
                    delete_dhcp_binding.assert_called_once_with(
                        dhcp_binding['nsx_service_id'],
                        dhcp_binding['nsx_binding_id'])

    def test_dhcp_binding_with_update_port_ip(self):
        # Test if DHCP binding is updated when the IP of the associated
        # compute port is changed.
        with self.subnet(cidr='10.0.0.0/24', enable_dhcp=True) as subnet:
            port_data = {'fixed_ips': [{'subnet_id': subnet['subnet']['id'],
                                        'ip_address': '10.0.0.3'}]}
            new_ip = '10.0.0.4'
            update_data = {'port': {'fixed_ips': [
                {'subnet_id': subnet['subnet']['id'], 'ip_address': new_ip}]}}
            assert_data = {'host_name': 'host-%s' % new_ip.replace('.', '-'),
                           'ip_address': new_ip,
                           'options': {'option121': {'static_routes': [
                               {'network': '%s' %
                                cfg.CONF.nsx_v3.native_metadata_route,
                                'next_hop': new_ip}]}}}
            self._verify_dhcp_binding(subnet, port_data, update_data,
                                      assert_data)

    def test_dhcp_binding_with_update_port_mac(self):
        # Test if DHCP binding is updated when the Mac of the associated
        # compute port is changed.
        with self.subnet(enable_dhcp=True) as subnet:
            port_data = {'mac_address': '11:22:33:44:55:66'}
            new_mac = '22:33:44:55:66:77'
            update_data = {'port': {'mac_address': new_mac}}
            assert_data = {'mac_address': new_mac}
            self._verify_dhcp_binding(subnet, port_data, update_data,
                                      assert_data)

    def test_dhcp_binding_with_update_port_mac_ip(self):
        # Test if DHCP binding is updated when the IP and Mac of the associated
        # compute port are changed at the same time.
        with self.subnet(cidr='10.0.0.0/24', enable_dhcp=True) as subnet:
            port_data = {'mac_address': '11:22:33:44:55:66',
                         'fixed_ips': [{'subnet_id': subnet['subnet']['id'],
                                        'ip_address': '10.0.0.3'}]}
            new_mac = '22:33:44:55:66:77'
            new_ip = '10.0.0.4'
            update_data = {'port': {'mac_address': new_mac, 'fixed_ips': [
                {'subnet_id': subnet['subnet']['id'], 'ip_address': new_ip}]}}
            assert_data = {'host_name': 'host-%s' % new_ip.replace('.', '-'),
                           'mac_address': new_mac,
                           'ip_address': new_ip,
                           'options': {'option121': {'static_routes': [
                               {'network': '%s' %
                                cfg.CONF.nsx_v3.native_metadata_route,
                                'next_hop': new_ip}]}}}
            self._verify_dhcp_binding(subnet, port_data, update_data,
                                      assert_data)

    def test_dhcp_binding_with_update_port_name(self):
        # Test if DHCP binding is not updated when the name of the associated
        # compute port is changed.
        with mock.patch.object(nsx_resources.LogicalDhcpServer,
                               'update_binding') as update_dhcp_binding:
            with self.subnet(cidr='10.0.0.0/24', enable_dhcp=True) as subnet:
                device_owner = constants.DEVICE_OWNER_COMPUTE_PREFIX + 'None'
                device_id = uuidutils.generate_uuid()
                with self.port(subnet=subnet, device_owner=device_owner,
                               device_id=device_id, name='abc') as port:
                    data = {'port': {'name': 'xyz'}}
                    self.plugin.update_port(
                        context.get_admin_context(), port['port']['id'], data)
                    update_dhcp_binding.assert_not_called()

    def test_dhcp_binding_with_multiple_ips(self):
        # Test create/update/delete DHCP binding with multiple IPs on a
        # compute port.
        with mock.patch.object(nsx_resources.LogicalDhcpServer,
                               'create_binding',
                               side_effect=[{"id": uuidutils.generate_uuid()},
                                            {"id": uuidutils.generate_uuid()}]
                               ) as create_dhcp_binding:
            with mock.patch.object(nsx_resources.LogicalDhcpServer,
                                   'update_binding'
                                   ) as update_dhcp_binding:
                with mock.patch.object(nsx_resources.LogicalDhcpServer,
                                       'delete_binding'
                                       ) as delete_dhcp_binding:
                    with self.subnet(cidr='10.0.0.0/24', enable_dhcp=True
                                     ) as subnet:
                        device_owner = (constants.DEVICE_OWNER_COMPUTE_PREFIX +
                                        'None')
                        device_id = uuidutils.generate_uuid()
                        fixed_ips = [{'subnet_id': subnet['subnet']['id'],
                                      'ip_address': '10.0.0.3'},
                                     {'subnet_id': subnet['subnet']['id'],
                                      'ip_address': '10.0.0.4'}]
                        with self.port(subnet=subnet,
                                       device_owner=device_owner,
                                       device_id=device_id,
                                       fixed_ips=fixed_ips) as port:
                            self.assertEqual(create_dhcp_binding.call_count, 2)
                            new_fixed_ips = [
                                {'subnet_id': subnet['subnet']['id'],
                                 'ip_address': '10.0.0.5'},
                                {'subnet_id': subnet['subnet']['id'],
                                 'ip_address': '10.0.0.6'}]
                            self.plugin.update_port(
                                context.get_admin_context(),
                                port['port']['id'],
                                {'port': {'fixed_ips': new_fixed_ips}})
                            self.assertEqual(update_dhcp_binding.call_count, 2)
                            self.plugin.delete_port(
                                context.get_admin_context(),
                                port['port']['id'])
                            self.assertEqual(delete_dhcp_binding.call_count, 2)


class NsxNativeMetadataTestCase(test_plugin.NsxV3PluginTestCaseMixin):

    def setUp(self):
        super(NsxNativeMetadataTestCase, self).setUp()
        self._orig_dhcp_agent_notification = cfg.CONF.dhcp_agent_notification
        self._orig_native_dhcp_metadata = cfg.CONF.nsx_v3.native_dhcp_metadata
        cfg.CONF.set_override('dhcp_agent_notification', False)
        cfg.CONF.set_override('native_dhcp_metadata', True, 'nsx_v3')
        self._patcher = mock.patch.object(nsx_resources.MetaDataProxy, 'get')
        self._patcher.start()
        # Need to run _init_dhcp_metadata() manually because plugin was
        # started before setUp() overrides CONF.nsx_v3.native_dhcp_metadata.
        self.plugin._init_dhcp_metadata()

    def tearDown(self):
        self._patcher.stop()
        cfg.CONF.set_override('dhcp_agent_notification',
                              self._orig_dhcp_agent_notification)
        cfg.CONF.set_override('native_dhcp_metadata',
                              self._orig_native_dhcp_metadata, 'nsx_v3')
        super(NsxNativeMetadataTestCase, self).tearDown()

    def test_metadata_proxy_configuration(self):
        # Test if dhcp_agent_notification and metadata_proxy_uuid are
        # configured correctly.
        orig_dhcp_agent_notification = cfg.CONF.dhcp_agent_notification
        cfg.CONF.set_override('dhcp_agent_notification', True)
        self.assertRaises(nsx_exc.NsxPluginException,
                          self.plugin._init_dhcp_metadata)
        cfg.CONF.set_override('dhcp_agent_notification',
                              orig_dhcp_agent_notification)
        orig_metadata_proxy_uuid = cfg.CONF.nsx_v3.metadata_proxy_uuid
        cfg.CONF.set_override('metadata_proxy_uuid', '', 'nsx_v3')
        self.assertRaises(cfg.RequiredOptError,
                          self.plugin._init_dhcp_metadata)
        cfg.CONF.set_override('metadata_proxy_uuid', orig_metadata_proxy_uuid,
                              'nsx_v3')

    def test_metadata_proxy_with_create_network(self):
        # Test if native metadata proxy is enabled on a network when it is
        # created.
        with mock.patch.object(nsx_resources.LogicalPort,
                               'create') as create_logical_port:
            with self.network() as network:
                nsx_net_id = self.plugin._get_network_nsx_id(
                    context.get_admin_context(), network['network']['id'])
                tags = utils.build_v3_tags_payload(
                    network['network'], resource_type='os-neutron-net-id',
                    project_name=None)
                name = utils.get_name_and_uuid('%s-%s' % (
                    'mdproxy', network['network']['name'] or 'network'),
                                               network['network']['id'])
                create_logical_port.assert_called_once_with(
                    nsx_net_id, cfg.CONF.nsx_v3.metadata_proxy_uuid,
                    tags=tags, name=name,
                    attachment_type=nsx_constants.ATTACHMENT_MDPROXY)

    def test_metadata_proxy_with_get_subnets(self):
        # Test if get_subnets() handles advanced-service-provider extension,
        # which is used when processing metadata requests.
        with self.network() as n1, self.network() as n2:
            with self.subnet(network=n1) as s1, self.subnet(network=n2) as s2:
                # Get all the subnets.
                subnets = self._list('subnets')['subnets']
                self.assertEqual(len(subnets), 2)
                self.assertEqual(set([s['id'] for s in subnets]),
                                 set([s1['subnet']['id'], s2['subnet']['id']]))
                lswitch_id = nsx_db.get_nsx_switch_ids(
                    context.get_admin_context().session,
                    n1['network']['id'])[0]
                # Get only the subnets associated with a particular advanced
                # service provider (i.e. logical switch).
                subnets = self._list('subnets', query_params='%s=%s' %
                                     (as_providers.ADV_SERVICE_PROVIDERS,
                                      lswitch_id))['subnets']
                self.assertEqual(len(subnets), 1)
                self.assertEqual(subnets[0]['id'], s1['subnet']['id'])
