import logging
import os
import sys
import time
import re
import json

import pytest

from tests.common import config_reload
from tests.common.utilities import wait_until
from tests.common.helpers.assertions import pytest_assert

profile_format = 'pg_lossless_{}_{}_profile'
LOSSLESS_PROFILE_PATTERN = 'pg_lossless_([1-9][0-9]*000)_([1-9][0-9]*m)_profile'

DEFAULT_CABLE_LENGTH_LIST = None
DEFAULT_LOSSLESS_HEADROOM_DATA = None
DEFAULT_INGRESS_POOL_NUMBER = 0
DEFAULT_MTU = None

TESTPARAM_HEADROOM_OVERRIDE = None
TESTPARAM_LOSSLESS_PG = None

BUFFER_MODEL_DYNAMIC = True

def detect_buffer_model(duthost):
    """Detect the current buffer model (dynamic or traditional) and store it for futher use. Called only once when the module is initialized

    Args:
        duthost: The DUT host object
    """
    global BUFFER_MODEL_DYNAMIC
    buffer_model = duthost.shell('redis-cli -n 4 hget "DEVICE_METADATA|localhost" buffer_model')['stdout']
    BUFFER_MODEL_DYNAMIC = (buffer_model == 'dynamic')


def detect_ingress_pool_number(duthost):
    """Detect the number of ingress buffer pools and store it for futher use. Called only once when the module is initialized

    Args:
        duthost: The DUT host object
    """
    global DEFAULT_INGRESS_POOL_NUMBER
    pools = duthost.shell('redis-cli -n 4 keys "BUFFER_POOL|ingress*"')['stdout']
    DEFAULT_INGRESS_POOL_NUMBER = len(pools.split())


def detect_default_mtu(duthost, port_to_test):
    """Detect the mtu and store it for futher use. Called only once when the module is initialized

    Args:
        duthost: The DUT host object
    """
    global DEFAULT_MTU
    if not DEFAULT_MTU:
        logging.info("Default MTU {}".format(DEFAULT_MTU))
        DEFAULT_MTU = duthost.shell('redis-cli -n 4 hget "PORT|{}" mtu'.format(port_to_test))['stdout']


def load_lossless_headroom_data(duthost):
    """Load test parameters from the json file. Called only once when the module is initialized

    Args:
        duthost: the DUT host object
    """
    global DEFAULT_LOSSLESS_HEADROOM_DATA
    if not DEFAULT_LOSSLESS_HEADROOM_DATA:
        dut_hwsku = duthost.facts["hwsku"]
        dut_platform = duthost.facts["platform"]
        skudir = "/usr/share/sonic/device/{}/{}/".format(dut_platform, dut_hwsku)
        lines = duthost.shell('cat {}/pg_profile_lookup.ini'.format(skudir))["stdout"]
        DEFAULT_LOSSLESS_HEADROOM_DATA = {}
        for line in lines.split('\n'):
            if line[0] == '#':
                continue
            tokens = line.split()
            speed = tokens[0]
            cable_length = tokens[1]
            size = tokens[2]
            xon = tokens[3]
            xoff = tokens[4]
            if not DEFAULT_LOSSLESS_HEADROOM_DATA.get(speed):
                DEFAULT_LOSSLESS_HEADROOM_DATA[speed] = {}
            DEFAULT_LOSSLESS_HEADROOM_DATA[speed][cable_length] = {'size': size, 'xon': xon, 'xoff': xoff}
        DEFAULT_LOSSLESS_HEADROOM_DATA = DEFAULT_LOSSLESS_HEADROOM_DATA


def load_test_parameters(duthost):
    """Load test parameters from the json file. Called only once when the module is initialized

    Args:
        duthost: The DUT host object
    """
    global DEFAULT_CABLE_LENGTH_LIST
    global TESTPARAM_HEADROOM_OVERRIDE
    global TESTPARAM_LOSSLESS_PG

    param_file_name = "qos/files/dynamic_buffer_param.json"
    with open(param_file_name) as file:
        params = json.load(file)
        logging.info("Loaded test parameters {} from {}".format(params, param_file_name))
        asic_type = duthost.facts['asic_type']
        vendor_specific_param = params[asic_type]
        DEFAULT_CABLE_LENGTH_LIST = vendor_specific_param['default_cable_length']
        TESTPARAM_HEADROOM_OVERRIDE = vendor_specific_param['headroom-override']
        TESTPARAM_LOSSLESS_PG = vendor_specific_param['lossless_pg']


@pytest.fixture(scope="module", autouse=True)
def setup_module(duthost):
    """Set up module. Called only once when the module is initialized

    Args:
        duthost: The DUT host object
    """
    detect_buffer_model(duthost)
    if BUFFER_MODEL_DYNAMIC:
        detect_ingress_pool_number(duthost)
        load_lossless_headroom_data(duthost)
        load_test_parameters(duthost)

        logging.info("Cable length: default {}".format(DEFAULT_CABLE_LENGTH_LIST))
        logging.info("Ingress pool number {}".format(DEFAULT_INGRESS_POOL_NUMBER))
        logging.info("Lossless headroom data {}".format(DEFAULT_LOSSLESS_HEADROOM_DATA))
    else:
        pytest.skip("Dynamic buffer isn't enabled, skip the test")

    yield


def check_pool_size(duthost, ingress_lossless_pool_oid, **kwargs):
    """Check whether the pool size has been updated correctedly

    The expected pool size will be calculated based on the input arguments on a per-vendor basis
    After that, it will check the expected value against the buffer pool size in BUFFER_POOL_TABLE
    and in the ASIC_DB

    Args:
        ingress_lossless_pool_oid: The SAI OID of the ingress lossless pool in ASIC_DB
        kwargs: The parameters based on which the expected pool size is calculated.
                They are represeted in form of kwargs because different vendor can require different parameters
                For Mellanox, it includes:
                 - old / new pg size
                 - old / new pg numbers
                 - current pool size
                 - the expected pool size is calculated as:
                   current_pool_size + old_pg_num * old_pg_size - new_pg_num * new_pg_size
    """
    if duthost.facts['asic_type'] == 'mellanox':
        old_headroom = int(kwargs["old_headroom"])

        if "old_pg_number" in kwargs:
            old_pg_number = int(kwargs["old_pg_number"])
        else:
            old_pg_number = 2

        if "new_pg_number" in kwargs:
            new_pg_number = int(kwargs["new_pg_number"])
        else:
            new_pg_number = old_pg_number

        if new_pg_number:
            if "new_headroom" in kwargs:
                new_headroom = int(kwargs["new_headroom"])
            else:
                new_headroom = old_headroom
            new_reserved = new_pg_number * new_headroom
        else:
            new_reserved = 0

        curr_pool_size = int(kwargs["pool_size"])

        original_memory = curr_pool_size * DEFAULT_INGRESS_POOL_NUMBER + old_headroom * old_pg_number
        expected_pool_size = (original_memory - new_reserved) / DEFAULT_INGRESS_POOL_NUMBER

    def _get_pool_size_from_asic_db(duthost, ingress_lossless_pool_oid):
        pool_sai = _compose_dict_from_cli(duthost.shell('redis-cli -n 1 hgetall ASIC_STATE:SAI_OBJECT_TYPE_BUFFER_POOL:{}'.format(ingress_lossless_pool_oid))['stdout'].split('\n'))
        return pool_sai['SAI_BUFFER_POOL_ATTR_SIZE']

    def _check_pool_size(duthost, expected_pool_size, ingress_lossless_pool_oid):
        pool_size = duthost.shell('redis-cli hget "BUFFER_POOL_TABLE:ingress_lossless_pool" size')['stdout']

        if int(pool_size) != expected_pool_size:
            return False

        if ingress_lossless_pool_oid:
            pool_size = _get_pool_size_from_asic_db(duthost, ingress_lossless_pool_oid)
            if int(pool_size) != expected_pool_size:
                return False

        return True

    pytest_assert(wait_until(20, 2, _check_pool_size, duthost, expected_pool_size, ingress_lossless_pool_oid),
                  "Pool size isn't correct in database: expected {}, size in APPL_DB {}, size in ASIC_DB {}".format(
                      expected_pool_size,
                      duthost.shell('redis-cli hget "BUFFER_POOL_TABLE:ingress_lossless_pool" size')['stdout'],
                      _get_pool_size_from_asic_db(duthost, ingress_lossless_pool_oid)))


def check_pg_profile(duthost, pg, expected_profile):
    """Check whether the profile in BUFFER_PG match the expected value in a wait_until loop with maximum timeout as 10 seconds

    Args:
        pg: The key of buffer pg in BUFFER_PG table. Format: BUFFER_PG|<port>|<pg>
        expected_profile: The name of the expected profile
    """
    def _check_pg_profile(duthost, pg, expected_profile):
        profile = duthost.shell('redis-cli hget {} profile'.format(pg))['stdout'][1:-1]
        return (profile == 'BUFFER_PROFILE_TABLE:' + expected_profile)

    pytest_assert(wait_until(10, 2, _check_pg_profile, duthost, pg, expected_profile), "Profile in PG {} isn't {}".format(pg, expected_profile))


def check_pfc_enable(duthost, port, expected_pfc_enable_map):
    """Check whether the pfc_enable map in port table is correct in a wait_until loop with maximum timeout as 10 seconds

    Args:
        port: The port to be checked
        expected_pfc_enable_map: The expected pfc_enable map
    """
    def _check_pfc_enable(duthost, port, expected_pfc_enable_map):
        pfc_enable = duthost.shell('redis-cli -n 4 hget "PORT_QOS_MAP|{}" pfc_enable'.format(port))['stdout']
        return (expected_pfc_enable_map == pfc_enable)

    pytest_assert(wait_until(10, 2, _check_pfc_enable, duthost, port, expected_pfc_enable_map),
                  "Port {} pfc enable check failed expected: {} got: {}".format(
                      port,
                      expected_pfc_enable_map,
                      duthost.shell('redis-cli -n 4 hget "PORT_QOS_MAP|{}" pfc_enable'.format(port))['stdout']))


def check_lossless_profile_removed(duthost, profile, sai_oid=None):
    """Check whether the lossless profile has been removed from APPL_DB, STATE_DB and ASIC_DB (if sai_oid provided)

    Args:
        profile: The name of the buffer profile to be checked
        sai_oid: The SAI OID in ASIC_DB of the buffer profile
                 If it is None the ASIC_DB won't be checked
    """
    profile_info = duthost.shell('redis-cli -n 6 hgetall "BUFFER_PROFILE_TABLE|{}"'.format(profile))['stdout']
    pytest_assert(not profile_info, "Profile {} isn't removed from STATE_DB".format(profile))
    profile_info = duthost.shell('redis-cli hgetall "BUFFER_PROFILE_TABLE:{}"'.format(profile))['stdout']
    pytest_assert(not profile_info, "Profile {} isn't removed from APPL_DB".format(profile))
    logging.debug('Profile {} has been removed from STATE_DB and APPL_DB'.format(profile))
    if sai_oid:
        profile_info = duthost.shell('redis-cli -n 1 hgetall {}'.format(sai_oid))['stdout']
        pytest_assert(not profile_info, "Profile {} hasn't been removed from ASIC_DB".format(sai_oid))


def fetch_initial_asic_db(duthost):
    profiles_in_asicdb = duthost.shell('redis-cli -n 1 keys "ASIC_STATE:SAI_OBJECT_TYPE_BUFFER_PROFILE*"')['stdout']
    return set(profiles_in_asicdb.split('\n'))


def _compose_dict_from_cli(fields_list):
    """Convert the out put of hgetall command to a dict object containing the field, key pairs of the database table content

    Args:
        fields_list: A list of lines, the output of redis-cli hgetall command
    """
    return dict(zip(fields_list[0::2], fields_list[1::2]))


def check_buffer_profile_details(duthost, initial_profiles, profile_name, profile_oid, pool_oid):
    """Check buffer profile details.

    The following items are tested:
     - Whether the headroom information, like xoff, is correct.
       This is tested by comparing with standard profile in pg_profile_lookup table
     - Whether the profile information in APPL_DB matches that in ASIC_DB

    Args:
        initial_profiles: The keys of buffer profiles in ASIC_DB at the beginning of the test
        profile_name: Name of the profile
        profile_oid: SAI OID of the profile
        pool_oid: SAI OID of ingress lossless pool
    """
    profile_appldb = _compose_dict_from_cli(duthost.shell('redis-cli hgetall BUFFER_PROFILE_TABLE:{}'.format(profile_name))['stdout'].split('\n'))
    logging.debug("APPL_DB buffer profile {}: {} ".format(profile_name, profile_appldb))

    # Check the profile against the standard value
    m = re.search(LOSSLESS_PROFILE_PATTERN, profile_name)
    if m:
        # This means it's a dynamic profile
        speed = m.group(1)
        cable_length = m.group(2)
        std_profiles_for_speed = DEFAULT_LOSSLESS_HEADROOM_DATA.get(speed)
        std_profile = std_profiles_for_speed.get(cable_length)
        if std_profile:
            # This means it's a profile with std speed and cable length. We can check whether the headroom data is correct
            pytest_assert(profile_appldb['xon'] == std_profile['xon'] and profile_appldb['xoff'] == std_profile['xoff'] and profile_appldb['size'] == std_profile['size'],
                          "Generated profile {} doesn't match the std profile {}".format(profile_appldb, std_profile))
        else:
            for std_cable_len, std_profile in std_profiles_for_speed.items():
                if int(std_cable_len[:-1]) > int(cable_length[:-1]):
                    pytest_assert(int(std_profile['xoff']) >= int(profile_appldb['xoff']),
                                  "XOFF of generated profile {} is greater than standard profile {} while its cable length is less".format(profile_appldb, std_profile))
                else:
                    pytest_assert(int(std_profile['xoff']) <= int(profile_appldb['xoff']),
                                  "XOFF of generated profile {} is less than standard profile {} while its cable length is greater".format(profile_appldb, std_profile))

    profiles_in_asicdb = set(duthost.shell('redis-cli -n 1 keys "ASIC_STATE:SAI_OBJECT_TYPE_BUFFER_PROFILE*"')['stdout'].split('\n'))
    diff = profiles_in_asicdb - initial_profiles
    if len(diff) == 1:
        profile_oid = diff.pop()
    pytest_assert(profile_oid, "Unable to fetch SAI OID for profile {}, initial SAI OID set {} current set {}".format(
        profile_name, initial_profiles, profiles_in_asicdb))

    logging.debug("Initial profiles {} and current profiles {} have the following difference(s) {}".format(initial_profiles, profiles_in_asicdb, diff))

    profile_sai = _compose_dict_from_cli(duthost.shell('redis-cli -n 1 hgetall {}'.format(profile_oid))['stdout'].split('\n'))

    logging.debug("SAI object for new profile {}: oid {} content {}".format(profile_name, profile_oid, profile_sai))

    if pool_oid == None:
        pool_oid = profile_sai['SAI_BUFFER_PROFILE_ATTR_POOL_ID']
    if profile_appldb.get('dynamic_th'):
        sai_threshold_value = profile_appldb['dynamic_th']
        sai_threshold_mode = 'SAI_BUFFER_PROFILE_THRESHOLD_MODE_DYNAMIC'
    else:
        sai_threshold_value = profile_appldb['static_th']
        sai_threshold_mode = 'SAI_BUFFER_PROFILE_THRESHOLD_MODE_STATIC'
    assert profile_sai == {'SAI_BUFFER_PROFILE_ATTR_XON_TH': profile_appldb['xon'],
                           'SAI_BUFFER_PROFILE_ATTR_XOFF_TH': profile_appldb['xoff'],
                           'SAI_BUFFER_PROFILE_ATTR_RESERVED_BUFFER_SIZE': profile_appldb['size'],
                           'SAI_BUFFER_PROFILE_ATTR_POOL_ID': pool_oid,
                           'SAI_BUFFER_PROFILE_ATTR_THRESHOLD_MODE': sai_threshold_mode,
                           'SAI_BUFFER_PROFILE_ATTR_SHARED_DYNAMIC_TH': sai_threshold_value}

    return profile_oid, pool_oid


@pytest.fixture(params=['50000', '10000'])
def speed_to_test(request):
    """Used to parametrized test cases for speeds

    Args:
        param request: The pytest request object

    Return:
        speed_to_test
    """
    return request.param


@pytest.fixture(params=['15m', '40m'])
def cable_len_to_test(request):
    """Used to parametrized test cases for cable length

    Args:
        request: The pytest request object

    Return:
        cable_len_to_test
    """
    return request.param


@pytest.fixture(params=['1500', '9100'])
def mtu_to_test(request):
    """Used to parametrized test cases for mtu

    Args:
        request: The pytest request object

    Return:
        cable_len_to_test
    """
    return request.param


@pytest.fixture()
def port_to_test(request, duthost):
    """Used to parametrized test cases for port

    Args:
        request: The pytest request object

    Return:
        port_to_test
    """
    dutLagInterfaces = []
    mgFacts = duthost.minigraph_facts(host=duthost.hostname)['ansible_facts']
    ports = mgFacts['minigraph_ports'].keys()

    for _, lag in mgFacts["minigraph_portchannels"].items():
        dutLagInterfaces += lag["members"]

    testPort = set(mgFacts["minigraph_ports"].keys())
    testPort -= set(dutLagInterfaces)

    return list(testPort)[0]


@pytest.fixture(params=['3-4', '6'])
def pg_to_test(request):
    """Used to parametrized test cases for PGs under test

    Args:
        request: The pytest request object

    Return:
        pg_to_test
    """
    return request.param


def test_change_speed_cable(duthosts, rand_one_dut_hostname, conn_graph_facts, port_to_test, speed_to_test, mtu_to_test, cable_len_to_test):
    """The testcase for changing the speed and cable length of a port

    Change the variables of the port, including speed, mtu and cable length, in different ways and observe whether the DUT behaves correctly
    For any of the variable, if it matches the current port configuration, we will skip configuring it.
    If all of the speed_to_test, mtu_to_test and cable_len_to_test match the current value, the test will be skipped

    The flow of the test case:
        1. Update the port configuration according to input parameters
        2. Determine whether the profile removing behavior can be verifyed:
           If neither mtu nor cable length is default value, they will be applied on the port_to_test only,
           and the generated profile will be removed after the configuration change because the profile is referenced by this port only.
           For example:
               The mtu_to_test 1500 only applied on the port_to_test, thus the *_mtu1500_* profile is referenced by the port only
               The *_mtu1500_* mtu will be removed after the mtu of the port is updated to default value.
               In this case, we are able to verify whether the buffer profile is removed after mtu reverted or all PGs are removed.
               Other the other hand, if the mtu is 9100, the buffer profile can be referenced by many other ports and it's less possible for us to verify the removing behavior.
           We will remove and readd an extra PG 6 to verify the removing behavior as well.
        3. Each time the port configuration updated, the following items will be checked as much as possible:
            - Whether the new profile is generated in APPL_DB, STATE_DB and ASIC_DB.
            - Whether the pool size is updated in APPL_DB and ASIC_DB.
        4. Each time the PG on a port is added or removed, the following items will be checked:
            - Whether the profile referenced by PGs is as expected according to the port configuration.
            - Whether the profile is removed if all PGs are removed and we are able to check removing behavior (result of step 2).
            - Whether the pfc_enable filed of the port has been updated accordingly.

    Args:
        port_to_test: On which port will the test be performed
        speed_to_test: To what speed will the port's be changed
        mtu_to_test: To what mtu will the port's be changed
        cable_len_to_test: To what cable length will the port's be changed
    """
    duthost = duthosts[rand_one_dut_hostname]
    original_speed = duthost.shell('redis-cli -n 4 hget "PORT|{}" speed'.format(port_to_test))['stdout']
    original_cable_len = duthost.shell('redis-cli -n 4 hget "CABLE_LENGTH|AZURE" {}'.format(port_to_test))['stdout']
    profile = duthost.shell('redis-cli hget "BUFFER_PG_TABLE:{}:3-4" profile'.format(port_to_test))['stdout'][1:-1]
    detect_default_mtu(duthost, port_to_test)

    original_headroom_size = int(duthost.shell('redis-cli hget "{}" size'.format(profile))['stdout'])
    original_pool_size = int(duthost.shell('redis-cli hget BUFFER_POOL_TABLE:ingress_lossless_pool size')['stdout'])

    initial_asic_db_profiles = fetch_initial_asic_db(duthost)

    if speed_to_test == original_speed and cable_len_to_test == original_cable_len and mtu_to_test == DEFAULT_MTU:
        pytest.skip('Speed, MTU and cable length matches the default value, nothing to test, skip')

    try:
        if not speed_to_test == original_speed:
            logging.info("Changing port's speed to {}".format(speed_to_test))
            duthost.shell('config interface speed {} {}'.format(port_to_test, speed_to_test))
        if not mtu_to_test == DEFAULT_MTU:
            logging.info("Changing port's mtu to {}".format(mtu_to_test))
            duthost.shell('config interface mtu {} {}'.format(port_to_test, mtu_to_test))
        if not cable_len_to_test == original_cable_len:
            logging.info("Changing port's cable length to {}".format(cable_len_to_test))
            duthost.shell('config interface cable-length {} {}'.format(port_to_test, cable_len_to_test))

        check_profile_removed = cable_len_to_test not in DEFAULT_CABLE_LENGTH_LIST

        # Check whether profile is correct in PG table
        if mtu_to_test != DEFAULT_MTU:
            expected_profile = 'pg_lossless_{}_{}_mtu{}_profile'.format(speed_to_test, cable_len_to_test, mtu_to_test)
            check_profile_removed = True
        else:
            expected_profile = 'pg_lossless_{}_{}_profile'.format(speed_to_test, cable_len_to_test)

        logging.info('[Speed and/or cable-len and/or MTU updated] Checking whether new profile {} has been created and pfc_enable has been updated'.format(expected_profile))
        check_pg_profile(duthost, 'BUFFER_PG_TABLE:{}:3-4'.format(port_to_test), expected_profile)
        check_pfc_enable(duthost, port_to_test, '3,4')
        profile_oid, pool_oid = check_buffer_profile_details(duthost, initial_asic_db_profiles, expected_profile, None, None)
        logging.info('SAI OID for newly created profile {} ingress lossless pool {}'.format(profile_oid, pool_oid))

        # Check whether profile exist
        headroom_size = int(duthost.shell('redis-cli hget "BUFFER_PROFILE_TABLE:{}" size'.format(expected_profile))['stdout'])
        check_pool_size(duthost,
                        pool_oid,
                        pool_size = original_pool_size,
                        old_headroom = original_headroom_size,
                        new_headroom = headroom_size)

        # Remove all the lossless profile on the port
        logging.info('[Remove all lossless PGs] Checking pool size and pfc_enable')
        duthost.shell('config interface buffer priority-group lossless remove {} 3-4'.format(port_to_test))

        check_pool_size(duthost,
                        pool_oid,
                        pool_size = original_pool_size,
                        old_headroom = original_headroom_size,
                        new_pg_number = 0)

        check_pfc_enable(duthost, port_to_test, '')

        if check_profile_removed:
            logging.info('[Remove dynamic profile on PG removed] Checking whether the profile {} is removed on receiving all lossless PG removed'.format(expected_profile))
            check_lossless_profile_removed(duthost, expected_profile, profile_oid)

            # Re-add another lossless priority
            logging.info('Re-add a lossless_pg and check pool size and pfc_enable')
            duthost.shell('config interface buffer priority-group lossless add {} 6'.format(port_to_test))

            check_pool_size(duthost,
                            pool_oid,
                            pool_size = original_pool_size,
                            old_headroom = original_headroom_size,
                            new_headroom = headroom_size,
                            new_pg_number = 1)

            check_pfc_enable(duthost, port_to_test, '6')
            profile_oid, _ = check_buffer_profile_details(duthost, initial_asic_db_profiles, expected_profile, None, pool_oid)

            if cable_len_to_test != original_cable_len:
                logging.info('[Revert the cable length to the default value] Checking whether the profile is updated')
                duthost.shell('config interface cable-length {} {}'.format(port_to_test, original_cable_len))

            if mtu_to_test != DEFAULT_MTU:
                logging.info('[Revert the mtu to the default value] Checking whether the profile is updated')
                duthost.shell('config interface mtu {} {}'.format(port_to_test, DEFAULT_MTU))

            # Remove old profile on cable length change
            logging.info('[Remove dynamic profile on cable length and/or MTU updated] Checking whether the old profile is removed')
            check_lossless_profile_removed(duthost, expected_profile, profile_oid)
            expected_profile = 'pg_lossless_{}_{}_profile'.format(speed_to_test, original_cable_len)
            check_pg_profile(duthost, 'BUFFER_PG_TABLE:{}:6'.format(port_to_test), expected_profile)

            headroom_size = int(duthost.shell('redis-cli hget "BUFFER_PROFILE_TABLE:{}" size'.format(expected_profile))['stdout'])
            check_pool_size(duthost,
                            pool_oid,
                            pool_size = original_pool_size,
                            old_headroom = original_headroom_size,
                            new_headroom = headroom_size,
                            new_pg_number = 1)

            duthost.shell('config interface buffer priority-group lossless remove {} 6'.format(port_to_test))

            check_pool_size(duthost,
                            pool_oid,
                            pool_size = original_pool_size,
                            old_headroom = original_headroom_size,
                            new_pg_number = 0)
            check_pfc_enable(duthost, port_to_test, '')
        else:
            if cable_len_to_test != original_cable_len:
                logging.info('[Update cable length without any lossless pg configured]')
                duthost.shell('config interface cable-length {} {}'.format(port_to_test, original_cable_len))
            if mtu_to_test != DEFAULT_MTU:
                logging.info('[Update mtu without any lossless pg configured]')
                duthost.shell('config interface mtu {} {}'.format(port_to_test, DEFAULT_MTU))

        if speed_to_test != original_speed:
            logging.info('[Update speed without any lossless pg configured]')
            duthost.shell('config interface speed {} {}'.format(port_to_test, original_speed))

        logging.info('[Add lossless pg with speed and cable length ready]')
        duthost.shell('config interface buffer priority-group lossless add {} 3-4'.format(port_to_test))

        expected_profile = 'pg_lossless_{}_{}_profile'.format(original_speed, original_cable_len)
        check_pg_profile(duthost, 'BUFFER_PG_TABLE:{}:3-4'.format(port_to_test), expected_profile)
        check_pfc_enable(duthost, port_to_test, '3,4')

        check_pool_size(duthost,
                        pool_oid,
                        pool_size = original_pool_size,
                        old_headroom = original_headroom_size)

        logging.info('[Extra lossless PG]')
        duthost.shell('config interface buffer priority-group lossless add {} 6'.format(port_to_test))

        check_pg_profile(duthost, 'BUFFER_PG_TABLE:{}:6'.format(port_to_test), expected_profile)
        check_pfc_enable(duthost, port_to_test, '3,4,6')

        check_pool_size(duthost,
                        pool_oid,
                        pool_size = original_pool_size,
                        old_headroom = original_headroom_size,
                        new_pg_number = 3)

        logging.info('[Restore config]')
        duthost.shell('config interface buffer priority-group lossless remove {} 6'.format(port_to_test))

        check_pfc_enable(duthost, port_to_test, '3,4')

        check_pool_size(duthost,
                        pool_oid,
                        pool_size = original_pool_size,
                        old_headroom = original_headroom_size)
    finally:
        duthost.shell('config interface buffer priority-group lossless remove {}'.format(port_to_test), module_ignore_errors = True)
        duthost.shell('config interface speed {} {}'.format(port_to_test, original_speed), module_ignore_errors = True)
        duthost.shell('config interface mtu {} {}'.format(port_to_test, DEFAULT_MTU), module_ignore_errors = True)
        duthost.shell('config interface cable-length {} {}'.format(port_to_test, original_cable_len), module_ignore_errors = True)
        duthost.shell('config interface buffer priority-group lossless add {} 3-4'.format(port_to_test), module_ignore_errors = True)


def _parse_buffer_profile_params(param, cmd, name):
    """A helper for test_headroom_override, parsing the parameters from the pre-provided json file

    Args:
        param: The dict containing test parameters parsed from dynamic_buffer_param.json
        return: A tuple consisting of new headroom size and cli string

    Return:
        A tuple consists of:
            - The CLI string by which a headroom-override profile can be configured
            - The size of new profile
    """
    cli_str = "config buffer profile {} {}".format(cmd, name)
    xon = ""
    if 'xon' in param:
        xon = param['xon']
        cli_str += " --xon " + xon

    xoff = ""
    if 'xoff' in param:
        xoff = param['xoff']
        cli_str += " --xoff " + xoff

    size = ""
    if 'size' in param:
        size = param['size']
        cli_str += " --size " + size
        new_headroom = int(size)
    elif xoff and xon:
        new_headroom = int(xon) + int(xoff)
    else:
        new_headroom = None

    if 'dynamic_th' in param:
        cli_str += " --dynamic_th " + param['dynamic_th']
    return cli_str, new_headroom


def test_headroom_override(duthosts, rand_one_dut_hostname, conn_graph_facts, port_to_test):
    """Test case for headroom override

    Verify the headroom override behavior.
    All arguments required for testing are fetched from a predefined json file on a per-vendor basis.
    The test will be skipped in case the arguments are not provided.

    The flow of the test case:
        1. Fetch the parameters
        2. Add the headroom override profile and apply it to PG 3-4 on port_to_test
        3. Verify:
            - Whether the profile referenced by PG is correct
            - Whether the pfc_enable matches the PG
            - Whether the buffer profile is correct deployed in APPL_DB, STATE_DB and ASIC_DB
            - Whether the pool size has been updated correctly
        4. Add PG 6, verify the related info
        5. Update the headroom override profile and verify the related info
        6. Negative test: try to remove the headroom override profile.
           Verify it is not removed because it is still being referenced.
        7. Revert the PG configurations, verify the related info

    Args:
        port_to_test: On which port will the test be performed
    """
    duthost = duthosts[rand_one_dut_hostname]
    if not TESTPARAM_HEADROOM_OVERRIDE:
        pytest.skip("Headroom override test skipped due to no parameters provided")

    original_speed = duthost.shell('redis-cli -n 4 hget "PORT|{}" speed'.format(port_to_test))['stdout']
    original_cable_len = duthost.shell('redis-cli -n 4 hget "CABLE_LENGTH|AZURE" {}'.format(port_to_test))['stdout']
    original_profile = duthost.shell('redis-cli hget "BUFFER_PG_TABLE:{}:3-4" profile'.format(port_to_test))['stdout'][1:-1]
    original_headroom_size = duthost.shell('redis-cli hget "{}" size'.format(original_profile))['stdout']
    original_pool_size = duthost.shell('redis-cli hget BUFFER_POOL_TABLE:ingress_lossless_pool size')['stdout']

    initial_asic_db_profiles = fetch_initial_asic_db(duthost)

    try:
        # Configure a static profile
        param = TESTPARAM_HEADROOM_OVERRIDE.get("add")
        if not param:
            pytest.skip('Headroom override test skipped due to no parameters for "add" command provided')
        else:
            cli_str, new_headroom = _parse_buffer_profile_params(param, "add", "headroom-override")

        logging.info("[Prepare configuration] {}".format(cli_str))
        duthost.shell(cli_str)

        logging.info("[Test: headroom override on lossless PG 3-4] Apply the profile on the PG and check pool size")
        duthost.shell('config interface buffer priority-group lossless set {} 3-4 headroom-override'.format(port_to_test))

        check_pg_profile(duthost, 'BUFFER_PG_TABLE:{}:3-4'.format(port_to_test), 'headroom-override')
        check_pfc_enable(duthost, port_to_test, '3,4')
        profile_oid, pool_oid = check_buffer_profile_details(duthost, initial_asic_db_profiles, "headroom-override", None, None)

        check_pool_size(duthost,
                        pool_oid,
                        pool_size = original_pool_size,
                        old_headroom = original_headroom_size,
                        new_headroom = new_headroom)

        # Add another headroom override
        logging.info("[Test: headroom override on more lossless PGs 6] Apply the profile on the PG and check pool size")
        duthost.shell('config interface buffer priority-group lossless add {} 6 headroom-override'.format(port_to_test))

        check_pg_profile(duthost, 'BUFFER_PG_TABLE:{}:6'.format(port_to_test), 'headroom-override')
        check_pfc_enable(duthost, port_to_test, '3,4,6')
        profile_oid, _ = check_buffer_profile_details(duthost, initial_asic_db_profiles, "headroom-override", profile_oid, pool_oid)

        check_pool_size(duthost,
                        pool_oid,
                        pool_size = original_pool_size,
                        old_headroom = original_headroom_size,
                        new_headroom = new_headroom,
                        new_pg_number = 3)

        param = TESTPARAM_HEADROOM_OVERRIDE.get("set")
        if not param:
            pytest.skip('Headroom override test skipped due to no parameters for "set" command provided')
        else:
            cli_str, new_headroom = _parse_buffer_profile_params(param, "set", "headroom-override")
        logging.info("[Test: update headroom-override profile] Update the profile and check pool size: {}".format(cli_str))
        duthost.shell(cli_str)

        check_pool_size(duthost,
                        pool_oid,
                        pool_size = original_pool_size,
                        old_headroom = original_headroom_size,
                        new_headroom = new_headroom,
                        new_pg_number = 3)

        # Recover configuration
        logging.info("[Test: static headroom being referenced can not be removed]")
        duthost.shell('config buffer profile remove headroom-override', module_ignore_errors = True)

        profile = duthost.shell('redis-cli hgetall "BUFFER_PROFILE_TABLE:headroom-override"')['stdout']
        pytest_assert(profile, 'Headroom override profile has been removed when being referenced')
        logging.info("[Recover configuration]")
        duthost.shell('config interface buffer priority-group lossless remove {}'.format(port_to_test))
        duthost.shell('config interface buffer priority-group lossless add {} 3-4'.format(port_to_test))

        check_pg_profile(duthost, 'BUFFER_PG_TABLE:{}:3-4'.format(port_to_test), original_profile.split(':')[1])
        check_pfc_enable(duthost, port_to_test, '3,4')
        check_pool_size(duthost,
                        pool_oid,
                        pool_size = original_pool_size,
                        old_headroom = original_headroom_size,
                        new_pg_number = 2)
    finally:
        duthost.shell('config interface buffer priority-group lossless remove {}'.format(port_to_test), module_ignore_errors = True)
        duthost.shell('config interface buffer priority-group lossless add {} 3-4'.format(port_to_test), module_ignore_errors = True)
        duthost.shell('config buffer profile remove headroom-override', module_ignore_errors = True)


def test_lossless_pg(duthosts, rand_one_dut_hostname, conn_graph_facts, port_to_test, pg_to_test):
    """Test case for non default dynamic th

    Test case to verify the static profile with non default dynamic th
    The buffer profile will be generated automatically after the profile has been applied to the port
    The arguments required for the test are fetched from a predefiend json file on a per vendor basis.
    Not providing any of the arguments results in the test case skipped.

    The flow of the test case:
        1. Configure a headroom override profile and check it in the APPL_DB, STATE_DB and ASIC_DB
        2. Configure a non default dynamic th profile
        3. Apply the nondefault dynamic th profile to PG 3-4 and update cable length
        4. Check whether a new buffer profile is created accordingly in the APPL_DB, STATE_DB and ASIC_DB
        5. Update the PG 3-4 to the default mode: dynamic profile
           Verify whether the profile created in step 4 is removed
        6. Reconfigure it as non default dynamic th profile and check related info
        7. Update it to a headroom override profile and check related info
        8. Recover the configuration

    Args:
        port_to_test: On which port will the test be performed
        pg_to_test: To what PG will the profiles be applied
    """
    duthost = duthosts[rand_one_dut_hostname]
    original_speed = duthost.shell('redis-cli -n 4 hget "PORT|{}" speed'.format(port_to_test))['stdout']
    original_cable_len = duthost.shell('redis-cli -n 4 hget "CABLE_LENGTH|AZURE" {}'.format(port_to_test))['stdout']

    initial_asic_db_profiles = fetch_initial_asic_db(duthost)

    set_command = 'config interface buffer priority-group lossless set {} {} '.format(port_to_test, pg_to_test)
    add_command = 'config interface buffer priority-group lossless add {} {} '.format(port_to_test, pg_to_test)
    if pg_to_test == '3-4':
        first_command = set_command
    else:
        first_command = add_command

    buffer_pg = 'BUFFER_PG_TABLE:{}:{}'.format(port_to_test, pg_to_test)

    try:
        param = TESTPARAM_LOSSLESS_PG.get("headroom-override")
        if not param:
            pytest.skip('Lossless pg test skipped due to no parameters for "headroom-override" command provided')
        else:
            cli_str, new_headroom = _parse_buffer_profile_params(param, "add", "headroom-override")

        # Create profiles
        logging.info('[Preparing]: Create static buffer profile for headroom override')
        duthost.shell(cli_str)
        headroom_override_profile_oid, pool_oid = check_buffer_profile_details(duthost, initial_asic_db_profiles, "headroom-override", None, None)

        initial_asic_db_profiles = fetch_initial_asic_db(duthost)

        # This is a dynamic profile with non default dynamic-th.
        # Profile won't be created until configured on some pg
        param = TESTPARAM_LOSSLESS_PG.get("non-default-dynamic_th")
        if not param:
            pytest.skip('Lossless pg test skipped due to no parameters for "non-default-dynamic_th" command provided')
        else:
            cli_str, new_headroom = _parse_buffer_profile_params(param, "add", "non-default-dynamic_th")

        logging.info('[Preparing]: Create static buffer profile for non default dynamic_th')
        duthost.shell(cli_str)

        # Update cable length to 15m
        logging.info('[Preparing]: Update cable length')
        duthost.shell('config interface cable-length {} 15m'.format(port_to_test))
        expected_profile = 'pg_lossless_{}_15m_profile'.format(original_speed)
        check_pg_profile(duthost, 'BUFFER_PG_TABLE:{}:3-4'.format(port_to_test), expected_profile)
        profile_oid, _ = check_buffer_profile_details(duthost, initial_asic_db_profiles, expected_profile, None, pool_oid)

        # Originally, it should be a dynamic PG, update it to override
        logging.info('[Testcase: dynamic headroom => headroom override]')
        duthost.shell(first_command + 'headroom-override')
        # Check whether lossless dynamic profile is removed
        check_pg_profile(duthost, buffer_pg, 'headroom-override')
        if pg_to_test == '3-4':
            check_lossless_profile_removed(duthost, expected_profile, profile_oid)

        # Update it to non-default dynamic_th
        logging.info('[Testcase: headroom override => dynamically calculated headroom with non-default dynamic_th]')
        duthost.shell(set_command + 'non-default-dynamic_th')
        expected_nondef_profile = 'pg_lossless_{}_15m_th2_profile'.format(original_speed)
        check_pg_profile(duthost, buffer_pg, expected_nondef_profile)
        # A new profile should be created in ASIC DB
        profile_oid, _ = check_buffer_profile_details(duthost, initial_asic_db_profiles, expected_nondef_profile, None, pool_oid)

        # Update it to dynamic PG
        logging.info('[Testcase: dynamically calculated headroom with non-default dynamic_th => dynamic headroom]')
        duthost.shell(set_command)
        check_pg_profile(duthost, buffer_pg, expected_profile)
        check_lossless_profile_removed(duthost, expected_nondef_profile, profile_oid)

        # Update it to non-default dynamic_th
        logging.info('[Testcase: dynamic headroom => dynamically calculated headroom with non-default dynamic_th]')
        duthost.shell(set_command + 'non-default-dynamic_th')
        check_pg_profile(duthost, buffer_pg, expected_nondef_profile)
        # A new profile should be created in ASIC DB
        profile_oid, _ = check_buffer_profile_details(duthost, initial_asic_db_profiles, expected_nondef_profile, None, pool_oid)
        if pg_to_test == '3-4':
            # The oid can be reused by SAI. So we don't check whether profile_oid is removed.
            check_lossless_profile_removed(duthost, expected_profile)

        # Update it to headroom override
        logging.info('[Testcase: dynamically calculated headroom with non-default dynamic_th => headroom override]')
        duthost.shell(set_command + 'headroom-override')
        check_pg_profile(duthost, buffer_pg, 'headroom-override')
        check_lossless_profile_removed(duthost, expected_nondef_profile, profile_oid)

        # Update it to dynamic PG, recover
        logging.info('[Testcase: headroom override => dynamic headroom]')
        duthost.shell(set_command)
        check_pg_profile(duthost, buffer_pg, expected_profile)

        # Remove all static profiles
        logging.info('[Restoring configuration]')
        duthost.shell('config buffer profile remove headroom-override')
        duthost.shell('config buffer profile remove non-default-dynamic_th')
        check_lossless_profile_removed(duthost, 'headroom-override', headroom_override_profile_oid)
        check_lossless_profile_removed(duthost, 'non-default-dynamic_th')
    finally:
        if pg_to_test == '3-4':
            duthost.shell(set_command, module_ignore_errors = True)
        else:
            duthost.shell('config interface buffer priority-group lossless remove {} {} '.format(port_to_test, pg_to_test), module_ignore_errors = True)
        duthost.shell('config interface cable-length {} {}'.format(port_to_test, original_cable_len), module_ignore_errors = True)
        duthost.shell('config buffer profile remove headroom-override', module_ignore_errors = True)
        duthost.shell('config buffer profile remove non-default-dynamic_th', module_ignore_errors = True)


def test_exceeding_headroom(duthosts, rand_one_dut_hostname, conn_graph_facts, port_to_test):
    """The test case for maximum headroom

    If the accumulative headroom of a port exceeds the maximum value,
    the new configuation causing the violation should not be applied to prevent orchagent from exiting

    The idea is to configure a super long cable which can cause a super large headroom thus exceeding the maximum value.
    Afterthat, verify the profile of the PG isn't changed

    Args:
        port_to_test: Port to run the test
    """
    duthost = duthosts[rand_one_dut_hostname]
    max_headroom_size = duthost.shell('redis-cli -n 6 hget "BUFFER_MAX_PARAM_TABLE|{}" max_headroom_size'.format(port_to_test))['stdout']
    if not max_headroom_size:
        pytest.skip('No max headroom found on port {}, skip'.format(port_to_test))

    original_cable_len = duthost.shell('redis-cli -n 4 hget "CABLE_LENGTH|AZURE" {}'.format(port_to_test))['stdout']
    original_speed = duthost.shell('redis-cli -n 4 hget "PORT|{}" speed'.format(port_to_test))['stdout']
    original_profile = 'pg_lossless_{}_{}_profile'.format(original_speed, original_cable_len)

    try:
        # Set to super long cable length
        logging.info('[Config a super long cable length]')
        duthost.shell('config interface cable-length {} 10000m'.format(port_to_test))

        logging.info('Verify the profile isn\'t changed')
        check_pg_profile(duthost, 'BUFFER_PG_TABLE:{}:3-4'.format(port_to_test), original_profile)
        duthost.shell('config interface cable-length {} {}'.format(port_to_test, original_cable_len))

        # add additional PG
        logging.info('[Config the cable length on the port]')
        duthost.shell('config interface cable-length {} 300m'.format(port_to_test))

        logging.info('Verify the profile has been changed')
        expected_profile = 'pg_lossless_{}_{}_profile'.format(original_speed, '300m')
        check_pg_profile(duthost, 'BUFFER_PG_TABLE:{}:3-4'.format(port_to_test), expected_profile)
        logging.info('Add another PG and make sure the system isn\'t broken')
        duthost.shell('config interface buffer priority-group lossless add {} {}'.format(port_to_test, '5-7'))

        # We can't say whether this will accumulative headroom exceed the limit, but the system should not crash
        # Leverage sanity check to verify that
        duthost.shell('config interface buffer priority-group lossless remove {} {}'.format(port_to_test, '5-7'))
        duthost.shell('config interface cable-length {} {}'.format(port_to_test, original_cable_len))

        # Static profile
        logging.info('[Config headroom override to PG 3-4]')
        duthost.shell('config buffer profile add test-headroom --xon 18432 --xoff 50000 -headroom 68432')
        duthost.shell('config interface buffer priority-group lossless set {} {} {}'.format(port_to_test, '3-4', 'test-headroom'))

        logging.info('Verify the profile is applied')
        check_pg_profile(duthost, 'BUFFER_PG_TABLE:{}:3-4'.format(port_to_test), 'test-headroom')
        duthost.shell('config interface buffer priority-group lossless add {} {} {}'.format(port_to_test, '5-7', 'test-headroom'))

        # Again, we can't say for sure whether the accumulative headroom exceeding.
        # Just make sure the system doesn't crash
        duthost.shell('config interface buffer priority-group lossless remove {} {}'.format(port_to_test, '5-7'))

        logging.info('[Update headroom override to a larger size]')
        duthost.shell('config buffer profile set test-headroom --xon 18432 --xoff 860160 -headroom 878592')

        # This should make it exceed the limit, so the profile should not applied to the APPL_DB
        size_in_appldb = duthost.shell('redis-cli hget "BUFFER_PROFILE_TABLE:test-headroom" size')['stdout']
        pytest_assert(size_in_appldb == '68432', 'The profile with a large size was applied to APPL_DB, which can make headroom exceeding')
        duthost.shell('config interface buffer priority-group lossless set {} {}'.format(port_to_test, '3-4'))
        duthost.shell('config buffer profile remove test-headroom')
        logging.info('[Clean up]')
    finally:
        duthost.shell('config interface cable-length {} {}'.format(port_to_test, original_cable_len), module_ignore_errors = True)
        duthost.shell('config interface buffer priority-group lossless remove {} 5-7'.format(port_to_test), module_ignore_errors = True)
        duthost.shell('config interface buffer priority-group lossless set {} 3-4'.format(port_to_test), module_ignore_errors = True)
        duthost.shell('config buffer profile remove test-headroom', module_ignore_errors = True)


def _recovery_to_dynamic_buffer_model(duthost):
    duthost.shell('kill $(pgrep buffermgrd)')
    duthost.shell('config qos reload')
    duthost.shell('config save -y')
    config_reload(duthost, config_source='config_db')


def test_buffer_model_test(duthosts, rand_one_dut_hostname, conn_graph_facts):
    """Verify whether the buffer model is expected after configuration operations:
    The following items are verified
     - Whether the buffer model is traditional after executing config load_minigraph
     - Whether the buffer model is dynamic after recovering the buffer model to dynamic
    """
    duthost = duthosts[rand_one_dut_hostname]
    try:
        logging.info('[Config load_minigraph]')
        config_reload(duthost, config_source='minigraph')
        buffer_model = duthost.shell('redis-cli -n 4 hget "DEVICE_METADATA|localhost" buffer_model')['stdout']
        pytest_assert(buffer_model == 'traditional', 'Got buffer model {} after executing config load_minigraph, traditional expected')

        logging.info('[Recover the DUT to default buffer model]')
        _recovery_to_dynamic_buffer_model(duthost)
        buffer_model = duthost.shell('redis-cli -n 4 hget "DEVICE_METADATA|localhost" buffer_model')['stdout']
        pytest_assert(buffer_model == 'dynamic', 'Got buffer model {} after executing recovering the buffer model to dynamic')
    finally:
        _recovery_to_dynamic_buffer_model(duthost)
