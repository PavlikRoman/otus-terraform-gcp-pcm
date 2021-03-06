#!/usr/bin/python -tt
# ---------------------------------------------------------------------
# Copyright 2016 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ---------------------------------------------------------------------
# Description:	Google Cloud Platform - Floating IP Address (Alias)
# ---------------------------------------------------------------------

import json
import logging
import os
import sys
import time

OCF_FUNCTIONS_DIR="%s/lib/heartbeat" % os.environ.get("OCF_ROOT")
sys.path.append(OCF_FUNCTIONS_DIR)

from ocf import *
from google.oauth2 import service_account

try:
  sys.path.insert(0, '/usr/lib/resource-agents/bundled/google-cloud-sdk/lib/third_party')
  import googleapiclient.discovery
except ImportError:
  pass

if sys.version_info >= (3, 0):
  # Python 3 imports.
  import urllib.parse as urlparse
  import urllib.request as urlrequest
else:
  # Python 2 imports.
  import urllib as urlparse
  import urllib2 as urlrequest

DEFAULT_CREDENTIALS_PATH = '/gcloud_service_account.json'
CONN = None
THIS_VM = None
ALIAS = None
METADATA_SERVER = 'http://metadata.google.internal/computeMetadata/v1/'
METADATA_HEADERS = {'Metadata-Flavor': 'Google'}
METADATA = \
'''<?xml version="1.0"?>
<!DOCTYPE resource-agent SYSTEM "ra-api-1.dtd">
<resource-agent name="gcp-vpc-move-vip">
  <version>1.0</version>
  <longdesc lang="en">Floating IP Address on Google Cloud Platform - Using Alias IP address functionality to attach a secondary IP address to a running instance</longdesc>
  <shortdesc lang="en">Floating IP Address on Google Cloud Platform</shortdesc>
  <parameters>
    <parameter name="alias_ip" unique="1" required="1">
      <longdesc lang="en">IP Address to be added including CIDR. E.g 192.168.0.1/32</longdesc>
      <shortdesc lang="en">IP Address to be added including CIDR. E.g 192.168.0.1/32</shortdesc>
      <content type="string" default="" />
    </parameter>
    <parameter name="alias_range_name" unique="1" required="0">
      <longdesc lang="en">Subnet name for the Alias IP</longdesc>
      <shortdesc lang="en">Subnet name for the Alias IP</shortdesc>
      <content type="string" default="" />
    </parameter>
    <parameter name="hostlist" unique="1" required="0">
      <longdesc lang="en">List of hosts in the cluster</longdesc>
      <shortdesc lang="en">Host list</shortdesc>
      <content type="string" default="" />
    </parameter>
    <parameter name="stackdriver_logging" unique="0" required="0">
      <longdesc lang="en">If enabled (set to true), IP failover logs will be posted to stackdriver logging. Using stackdriver logging requires additional libraries (google-cloud-logging).</longdesc>
      <shortdesc lang="en">Stackdriver-logging support. Requires additional libraries (google-cloud-logging).</shortdesc>
      <content type="boolean" default="" />
    </parameter>
  </parameters>
  <actions>
    <action name="start" timeout="300s" />
    <action name="stop" timeout="15s" />
    <action name="monitor" timeout="15s" interval="60s" depth="0" />
    <action name="meta-data" timeout="15s" />
    <action name="validate-all" timeout="15s" />
  </actions>
</resource-agent>'''


def get_metadata(metadata_key, params=None, timeout=None):
  """Performs a GET request with the metadata headers.

  Args:
    metadata_key: string, the metadata to perform a GET request on.
    params: dictionary, the query parameters in the GET request.
    timeout: int, timeout in seconds for metadata requests.

  Returns:
    HTTP response from the GET request.

  Raises:
    urlerror.HTTPError: raises when the GET request fails.
  """
  timeout = timeout or 60
  metadata_url = os.path.join(METADATA_SERVER, metadata_key)
  params = urlparse.urlencode(params or {})
  url = '%s?%s' % (metadata_url, params)
  request = urlrequest.Request(url, headers=METADATA_HEADERS)
  request_opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
  return request_opener.open(request, timeout=timeout * 1.1).read()


def get_instance(project, zone, instance):
  request = CONN.instances().get(
      project=project, zone=zone, instance=instance)
  return request.execute()


def get_network_ifaces(project, zone, instance):
  return get_instance(project, zone, instance)['networkInterfaces']


def wait_for_operation(project, zone, operation):
  while True:
    result = CONN.zoneOperations().get(
        project=project,
        zone=zone,
        operation=operation['name']).execute()

    if result['status'] == 'DONE':
      if 'error' in result:
        raise Exception(result['error'])
      return
    time.sleep(1)


def set_alias(project, zone, instance, alias, alias_range_name=None):
  fingerprint = get_network_ifaces(project, zone, instance)[0]['fingerprint']
  body = {
      'aliasIpRanges': [],
      'fingerprint': fingerprint
  }
  if alias:
    obj = {'ipCidrRange': alias}
    if alias_range_name:
      obj['subnetworkRangeName'] = alias_range_name
    body['aliasIpRanges'].append(obj)

  request = CONN.instances().updateNetworkInterface(
      instance=instance, networkInterface='nic0', project=project, zone=zone,
      body=body)
  operation = request.execute()
  wait_for_operation(project, zone, operation)


def get_alias(project, zone, instance):
  iface = get_network_ifaces(project, zone, instance)
  try:
    return iface[0]['aliasIpRanges'][0]['ipCidrRange']
  except KeyError:
    return ''


def get_localhost_alias():
  net_iface = get_metadata('instance/network-interfaces', {'recursive': True})
  net_iface = json.loads(net_iface.decode('utf-8'))
  try:
    return net_iface[0]['ipAliases'][0]
  except (KeyError, IndexError):
    return ''


def get_zone(project, instance):
  fl = 'name="%s"' % instance
  request = CONN.instances().aggregatedList(project=project, filter=fl)
  while request is not None:
    response = request.execute()
    zones = response.get('items', {})
    for zone in zones.values():
      for inst in zone.get('instances', []):
        if inst['name'] == instance:
          return inst['zone'].split("/")[-1]
    request = CONN.instances().aggregatedList_next(
        previous_request=request, previous_response=response)
  raise Exception("Unable to find instance %s" % (instance))


def get_instances_list(project, exclude):
  hostlist = []
  request = CONN.instances().aggregatedList(project=project)
  while request is not None:
    response = request.execute()
    zones = response.get('items', {})
    for zone in zones.values():
      for inst in zone.get('instances', []):
        if inst['name'] != exclude:
          hostlist.append(inst['name'])
    request = CONN.instances().aggregatedList_next(
        previous_request=request, previous_response=response)
  return hostlist


def gcp_alias_start(alias):
  my_alias = get_localhost_alias()
  my_zone = get_metadata('instance/zone').split('/')[-1]
  project = get_metadata('project/project-id')

  # If I already have the IP, exit. If it has an alias IP that isn't the VIP,
  # then remove it
  if my_alias == alias:
    logger.info(
        '%s already has %s attached. No action required' % (THIS_VM, alias))
    sys.exit(OCF_SUCCESS)
  elif my_alias:
    logger.info('Removing %s from %s' % (my_alias, THIS_VM))
    set_alias(project, my_zone, THIS_VM, '')

  # Loops through all hosts & remove the alias IP from the host that has it
  hostlist = os.environ.get('OCF_RESKEY_hostlist', '')
  if hostlist:
    hostlist = hostlist.replace(THIS_VM, '').split()
  else:
    hostlist = get_instances_list(project, THIS_VM)
  for host in hostlist:
    host_zone = get_zone(project, host)
    host_alias = get_alias(project, host_zone, host)
    if alias == host_alias:
      logger.info(
          '%s is attached to %s - Removing all alias IP addresses from %s' %
          (alias, host, host))
      set_alias(project, host_zone, host, '')
      break

  # add alias IP to localhost
  set_alias(
      project, my_zone, THIS_VM, alias,
      os.environ.get('OCF_RESKEY_alias_range_name'))

  # Check the IP has been added
  my_alias = get_localhost_alias()
  if alias == my_alias:
    logger.info('Finished adding %s to %s' % (alias, THIS_VM))
  elif my_alias:
    logger.error(
        'Failed to add IP. %s has an IP attached but it isn\'t %s' %
        (THIS_VM, alias))
    sys.exit(OCF_ERR_GENERIC)
  else:
    logger.error('Failed to add IP address %s to %s' % (alias, THIS_VM))
    sys.exit(OCF_ERR_GENERIC)


def gcp_alias_stop(alias):
  my_alias = get_localhost_alias()
  my_zone = get_metadata('instance/zone').split('/')[-1]
  project = get_metadata('project/project-id')

  if my_alias == alias:
    logger.info('Removing %s from %s' % (my_alias, THIS_VM))
    set_alias(project, my_zone, THIS_VM, '')


def gcp_alias_status(alias):
  my_alias = get_localhost_alias()
  if alias == my_alias:
    logger.info('%s has the correct IP address attached' % THIS_VM)
  else:
    sys.exit(OCF_NOT_RUNNING)


def validate():
  global ALIAS
  global CONN
  global THIS_VM

  # Prepare credentials
  # Block added by mbfx
  if os.environ.get('GOOGLE_APPLICATION_CREDENTIALS') is None:
    sa_file = DEFAULT_CREDENTIALS_PATH
  else:
    sa_file = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')

  # Populate global vars
  # Block changed by mbfx 
  try:
    scopes = ['https://www.googleapis.com/auth/cloud-platform']
    credentials = service_account.Credentials.from_service_account_file(sa_file, scopes=scopes)
    CONN = googleapiclient.discovery.build('compute', 'v1', credentials=credentials)
  except Exception as e:
    logger.error('Couldn\'t connect with google api: ' + str(e))
    sys.exit(OCF_ERR_CONFIGURED)

  try:
    THIS_VM = get_metadata('instance/name')
  except Exception as e:
    logger.error('Couldn\'t get instance name, is this running inside GCE?: ' + str(e))
    sys.exit(OCF_ERR_CONFIGURED)

  ALIAS = os.environ.get('OCF_RESKEY_alias_ip')
  if not ALIAS:
    logger.error('Missing alias_ip parameter')
    sys.exit(OCF_ERR_CONFIGURED)


def configure_logs():
  # Prepare logging
  global logger
  logging.getLogger('googleapiclient').setLevel(logging.WARN)
  logging_env = os.environ.get('OCF_RESKEY_stackdriver_logging')
  if logging_env:
    logging_env = logging_env.lower()
    if any(x in logging_env for x in ['yes', 'true', 'enabled']):
      try:
        import google.cloud.logging.handlers
        client = google.cloud.logging.Client()
        handler = google.cloud.logging.handlers.CloudLoggingHandler(
            client, name=THIS_VM)
        handler.setLevel(logging.INFO)
        formatter = logging.Formatter('gcp:alias "%(message)s"')
        handler.setFormatter(formatter)
        log.addHandler(handler)
        logger = logging.LoggerAdapter(log, {'OCF_RESOURCE_INSTANCE': OCF_RESOURCE_INSTANCE})
      except ImportError:
        logger.error('Couldn\'t import google.cloud.logging, '
            'disabling Stackdriver-logging support')


def main():
  if 'meta-data' in sys.argv[1]:
    print(METADATA)
    return

  validate()
  if 'validate-all' in sys.argv[1]:
    return

  configure_logs()
  if 'start' in sys.argv[1]:
    gcp_alias_start(ALIAS)
  elif 'stop' in sys.argv[1]:
    gcp_alias_stop(ALIAS)
  elif 'status' in sys.argv[1] or 'monitor' in sys.argv[1]:
    gcp_alias_status(ALIAS)
  else:
    logger.error('no such function %s' % str(sys.argv[1]))


if __name__ == "__main__":
  main()
