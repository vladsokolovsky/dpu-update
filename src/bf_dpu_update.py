#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2024, NVIDIA CORPORATION. All rights reserved.


import time
import re
import sys
import os
import json
import socket
import select
import getpass
import subprocess
import stat
import datetime
from multiprocessing import Process
from error_num import *
import random
import time


class BF_DPU_Update(object):
    module_resource = {
        'BMC'       : 'BMC_Firmware',
        'CEC'       : 'Bluefield_FW_ERoT',
        'ATF'       : 'DPU_ATF',
        'UEFI'      : 'DPU_UEFI',
        'BSP'       : 'DPU_BSP',
        'NIC'       : 'DPU_NIC',
        'NODE'      : 'DPU_NODE',
        'OFED'      : 'DPU_OFED',
        'OS'        : 'DPU_OS',
        'SYS_IMAGE' : 'DPU_SYS_IMAGE',
        'CONF_IMAGE': 'golden_image_config',
        'BOARD'     : 'DPU_BOARD'
    }


    def __init__(self, bmc_ip, bmc_port, username, password, ssh_username, ssh_password, fw_file_path, task_dir, module, oem_fru, skip_same_version, debug=False, log_file=None, use_curl=True, bfb_update_protocol = None, reset_bios = False, lfwp = False, version = None):
        self.bmc_ip            = self._parse_bmc_addr(bmc_ip)
        self.bmc_port          = bmc_port
        self.username          = username
        self.password          = password
        self.ssh_username      = ssh_username
        self.ssh_password      = ssh_password
        self.ssh               = "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
        self.fw_file_path      = fw_file_path
        self.task_dir          = task_dir
        self.module            = module
        self.oem_fru           = oem_fru
        self.skip_same_version = skip_same_version
        self.debug             = debug
        self.log_file          = log_file
        self.protocol          = 'https://'
        self.redfish_root      = '/redfish/v1'
        self.process_flag      = True
        self._http_server_process = None
        self._http_server_port_pipe = os.pipe()
        self._local_http_server_port = None
        self.use_curl          = use_curl
        self.http_accessor     = self._get_http_accessor()
        self.bfb_update_protocol = bfb_update_protocol
        self.info_data         = None
        self.reset_bios        = reset_bios
        self.lfwp              = lfwp
        self.version           = version

        # Validate log_file if provided
        if self.log_file is not None:
            accessible_file = os.access(self.log_file, os.W_OK)
            accessible_dir = os.access(os.path.abspath(os.path.dirname(self.log_file)), os.W_OK)
            if not accessible_file and not accessible_dir:
                raise Err_Exception(Err_Num.FILE_NOT_ACCESSIBLE, 'Log file: {}'.format(self.log_file))

            with open(self.log_file, 'a') as f:
                f.write('OobUpdate Version: {}\n'.format(self.version))


    def _get_prot_ip_port(self):
        port = '' if self.bmc_port is None else ':{}'.format(self.bmc_port)
        return self.protocol + self._format_ip(self.bmc_ip) + port


    def _get_url_base(self):
        return self._get_prot_ip_port() + self.redfish_root


    def _get_local_ip(self):
        if self._is_ipv4:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        else:
            s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s.connect((self.bmc_ip, 0))
        return s.getsockname()[0]


    def _get_local_user(self):
        return getpass.getuser()


    def _get_http_accessor(self):
        if self.use_curl:
            from http_accessor_curl import HTTP_Accessor
        else:
            from http_accessor_requests import HTTP_Accessor
        return HTTP_Accessor


    def _http_get(self, url, headers=None, timeout=(60, 60)):
        return self.http_accessor(url, 'GET', self.username, self.password, self.task_dir, headers, timeout).access()


    def _http_post(self, url, data, headers=None, timeout=(120, 120)):
        return self.http_accessor(url, 'POST', self.username, self.password, self.task_dir, headers, timeout).access(data)


    def _http_patch(self, url, data, headers=None, timeout=(60, 60)):
        return self.http_accessor(url, 'PATCH', self.username, self.password, self.task_dir, headers, timeout).access(data)

    def _http_put(self, url, data, headers=None, timeout=(60, 60)):
        return self.http_accessor(url, 'PUT', self.username, self.password, self.task_dir, headers, timeout).access(data)

    def _upload_file(self, url, file_path, headers=None, timeout=(60, 60)):
        return self.http_accessor(url, 'POST', self.username, self.password, self.task_dir, headers, timeout).upload_file(file_path)


    def _multi_part_push(self, url, param, headers=None, timeout=(60, 60)):
        return self.http_accessor(url, 'POST', self.username, self.password, self.task_dir, headers, timeout).multi_part_push(param)


    def _get_truncated_data(self, data):
        if len(data) > 1024:
            return data[0:1024] + '... ... [Truncated]'
        else:
            return data


    def _parse_bmc_addr(self, address):
        self.raw_bmc_addr = address

        # IPV4?
        if self._is_valid_ipv4(address):
            self._is_ipv4 = True
            return address

        # IPV6?
        if self._is_valid_ipv6(address):
            self._is_ipv4 = False
            return address

        # Host name(ipv4) ?
        ipv4 = self._get_ipv4_from_name(address)
        if ipv4 is not None:
            self._is_ipv4 = True
            return ipv4

        # Host name(ipv6) ?
        ipv6 = self._get_ipv6_from_name(address)
        if ipv6 is not None:
            self._is_ipv4 = False
            return ipv6
        raise Err_Exception(Err_Num.INVALID_BMC_ADDRESS, '{} is neither a valid IPV4/IPV6 nor a resolvable host name'.format(address))


    @staticmethod
    def _is_valid_ipv4(address):
        try:
            socket.inet_pton(socket.AF_INET, address)
            return True
        except:
            return False


    @staticmethod
    def _is_valid_ipv6(address):
        try:
            socket.inet_pton(socket.AF_INET6, address)
            return True
        except:
            return False


    @staticmethod
    def _get_ipv4_from_name(address):
        try:
            ipv4_list = socket.getaddrinfo(address, None, socket.AF_INET)
            return ipv4_list[0][4][0]
        except:
            return None


    @staticmethod
    def _get_ipv6_from_name(address):
        try:
            ipv6_list = socket.getaddrinfo(address, None, socket.AF_INET6)
            return ipv6_list[0][4][0]
        except:
            return None


    def _format_ip(self, ip):
        if self._is_ipv4:
            return ip
        else:
            return '[{}]'.format(ip)


    def check_bmc_availability(self, timeout=10):
        """
        Check if BMC is available and reachable

        Args:
            timeout (int): Timeout in seconds for the check

        Returns:
            bool: True if BMC is available, False otherwise

        Raises:
            Err_Exception: If BMC is not reachable with specific error details
        """
        try:
            self.log("Checking BMC availability at {}".format(self._format_ip(self.bmc_ip)))

            # Try a simple GET request to the redfish root endpoint
            url = self._get_prot_ip_port() + '/redfish/v1'
            response = self._http_get(url, timeout=(timeout, timeout))

            # Check if we get a valid response
            if response.status_code in [200, 401]:  # 200 = OK, 401 = Unauthorized but BMC is reachable
                self.log("BMC is available and responding")
                return True
            else:
                self.log("BMC responded with unexpected status code: {}".format(response.status_code))
                return False

        except Exception as e:
            error_msg = "BMC at {} is not reachable".format(self._format_ip(self.bmc_ip))
            print("Error: {}".format(error_msg))
            self.log("Error: {}: {}".format(error_msg, str(e)))

            # Check if it's a specific connection error
            if hasattr(e, 'err_num'):
                if e.err_num == Err_Num.BMC_CONNECTION_FAIL:
                    raise Err_Exception(Err_Num.BMC_CONNECTION_FAIL, "BMC at {} is not reachable. Please verify the IP address and network connectivity".format(self._format_ip(self.bmc_ip)))
                elif e.err_num == Err_Num.BMC_CONNECTION_RESET:
                    raise Err_Exception(Err_Num.BMC_CONNECTION_RESET, "Connection to BMC at {} was reset".format(self._format_ip(self.bmc_ip)))
                else:
                    raise e
            else:
                # Generic connection error
                raise Err_Exception(Err_Num.BMC_CONNECTION_FAIL, "Failed to connect to BMC at {}. Please verify the IP address and network connectivity".format(self._format_ip(self.bmc_ip)))


    def _validate_fru_date_format(self, date_str):
        try:
            datetime.datetime.strptime(date_str, "%d/%m/%Y %H:%M:%S")
            return True
        except ValueError:
            return False


    def log(self, msg, resp = None):
        data  = '[======== ' + msg + ' ========]: ' + '\n'
        if resp is not None:
            data += '[Request Line]: ' + '\n'
            data += str(resp.request.method) + ' ' + resp.url + '\n'
            data += '[Request Headers]:' + '\n'
            data += str(resp.request.headers) + '\n'
            data += '[Request Body]:' + '\n'
            data += self._get_truncated_data(str(resp.request.body)) + '\n'
            data += "[Response status line]:" + '\n'
            data += str(resp.status_code) + ' ' + resp.reason + '\n'
            data += "[Response Headers]:" + '\n'
            data += json.dumps(str(resp.headers), indent=4) + '\n'
            data += "[Response Body]:" + '\n'
            data += resp.text + '\n'

        data = data.replace(self.password, '<password>')
        data = data.replace(self.username, '<username>')
        if self.ssh_password is not None and self.ssh_password != '':
            data = data.replace(self.ssh_password, '<ssh_password>')
        if self.ssh_username is not None and self.ssh_username != '':
            data = data.replace(self.ssh_username, '<ssh_username>')

        if self.debug:
            print(data, end='')
        if self.log_file is not None:
            with open(self.log_file, 'a') as f:
                f.write(data)


    def _handle_status_code(self, response, acceptable_codes, err_handler=None):
        if response.status_code in acceptable_codes:
            return

        try:
            msg = response.json()['error']['message']
        except:
            try:
                msg = response.json()['Attributes@Message.ExtendedInfo'][0]['Message']
            except:
                msg = ''

        # Raise exception for different cases
        if response.status_code == 401:
            if 'Account temporarily locked out' in msg:
                raise Err_Exception(Err_Num.ACCOUNT_LOCKED, msg)
            elif 'Invalid username or password' in msg:
                raise Err_Exception(Err_Num.INVALID_USERNAME_OR_PASSWORD, msg)

        if err_handler is not None:
            err_handler(response)

        raise Err_Exception(Err_Num.INVALID_STATUS_CODE, 'status code: {}; {}'.format(response.status_code, msg))


    def get_ver_by_uri(self, uri):
        url = self._get_prot_ip_port() + uri
        response = self._http_get(url)
        self.log('Get {} Firmware Version'.format(uri.split('/')[-1]), response)
        self._handle_status_code(response, [200])

        ver = ''
        try:
            ver = response.json()['Version']
        except Exception as e:
            raise Err_Exception(Err_Num.BAD_RESPONSE_FORMAT, 'Failed to extract firmware version')

        return ver


    def _get_ver(self, module):
        return self.get_ver_by_uri(self._get_firmware_uri_by_resource(self.module_resource[module]))


    def get_ver(self, module, num_of_tries=3):
        for i in range(num_of_tries):
            try:
                ver = self._get_ver(module)
                return ver
            except Exception as e:
                if self.debug:
                    print("Exception when get version: {}".format(e))
            time.sleep(4)
        return ''


    def _extract_task_handle(self, response):
        '''
        {
            "@odata.id": "/redfish/v1/TaskService/Tasks/6",
            "@odata.type": "#Task.v1_4_3.Task",
            "Id": "6",
            "TaskState": "Running",
            "TaskStatus": "OK"
        }
        '''
        try:
            return response.json()["@odata.id"]
        except:
            raise Err_Exception(Err_Num.BAD_RESPONSE_FORMAT, 'Failed to extract task handle')


    def get_simple_update_protocols(self):
        url = self._get_url_base() + '/UpdateService'
        response = self._http_get(url)
        self.log('Get UpdateService Attribute', response)
        self._handle_status_code(response, [200])

        protocols = []
        '''
        {
          ...
          "Actions": {
            "#UpdateService.SimpleUpdate": {
              "TransferProtocol@Redfish.AllowableValues": [
                "SCP",
                "HTTP",
                "HTTPS"
              ],
            },
          }
          ...
        }
        '''
        try:
            protocols = response.json()['Actions']['#UpdateService.SimpleUpdate']['TransferProtocol@Redfish.AllowableValues']
        except Exception as e:
            raise Err_Exception(Err_Num.BAD_RESPONSE_FORMAT, 'Failed to extract SimpleUpdate protocols')
        return protocols


    def get_push_uri(self):
        url = self._get_url_base() + '/UpdateService'
        response = self._http_get(url)
        self.log('Get UpdateService Attribute', response)
        self._handle_status_code(response, [200])

        deprecated_uri = None
        multi_part_uri = None
        '''
        {
          ...
          "HttpPushUri": "/redfish/v1/UpdateService/update",
          "MultipartHttpPushUri": "/redfish/v1/UpdateService/update-multipart",
          ...
        }
        '''
        try:
            deprecated_uri = response.json()['HttpPushUri']
        except:
            deprecated_uri = None
        try:
            multi_part_uri = response.json()['MultipartHttpPushUri']
        except:
            multi_part_uri = None
        return (multi_part_uri, deprecated_uri)


    def get_update_service_state(self):
        url = self._get_url_base() + '/UpdateService'
        response = self._http_get(url)
        self.log('Get UpdateService state', response)
        self._handle_status_code(response, [200])

        state = ''
        '''
        {
          ...
          "Status": {
            "Conditions": [],
            "State": "Enabled"
          }
        }
        '''
        try:
            state = response.json()['Status']['State']
        except Exception as e:
            raise Err_Exception(Err_Num.BAD_RESPONSE_FORMAT, 'Failed to extract update service state')
        return state


    def wait_update_service_ready(self):
        if 'Enabled' == self.get_update_service_state():
            return

        print("Wait for update service ready")
        timeout = 60 * 3 # Wait up to 3 minutes
        start   = int(time.time())
        end     = start + timeout
        while True:
            cur = int(time.time())
            if cur > end:
                raise Err_Exception(Err_Num.UPDATE_SERVICE_NOT_READY)
            try:
                state = self.get_update_service_state()
                if state == 'Enabled':
                    self._print_process(100)
                    break
                else:
                    self._print_process(100 * (cur - start) / timeout)
            except Exception as e:
                self._print_process(100 * (cur - start) / timeout)
            time.sleep(4)
        print()


    @staticmethod
    def _update_in_progress_err_handler(response):
        try:
            msg = response.json()['error']['message']
        except:
            msg = ''

        if response.status_code == 400:
            if 'An update is in progress' in msg:
                raise Err_Exception(Err_Num.ANOTHER_UPDATE_IS_IN_PROGRESS, 'Please try to update the firmware later')


    def simple_update(self):
        protocols_supported_by_bmc = self.get_simple_update_protocols()
        # Current script only support HTTP/SCP
        protocols = []
        if 'HTTP' in protocols_supported_by_bmc:
            protocols.append('HTTP')
        if 'SCP' in protocols_supported_by_bmc:
            protocols.append('SCP')

        # Select protocol to be used
        protocol = None
        if self.bfb_update_protocol is not None:
            # Use the protocol provided by user
            if self.bfb_update_protocol not in protocols:
                raise Err_Exception(Err_Num.NOT_SUPPORT_SIMPLE_UPDATE_PROTOCOL, '{} is not in supported BFB update protocols {}'.format(self.bfb_update_protocol, protocols))
            protocol = self.bfb_update_protocol
        else:
            # Perfer to use HTTP, if user did not provide a protocol
            if 'HTTP' in protocols:
                protocol = 'HTTP'
            elif 'SCP' in protocols:
                protocol = 'SCP'
            if protocol is None:
                raise Err_Exception(Err_Num.NOT_SUPPORT_SIMPLE_UPDATE_PROTOCOL, 'The current supported BFB update protocols are {}'.format(protocols))

        return (protocol, self.simple_update_by_protocol(protocol))


    def simple_update_by_protocol(self, protocol):
        if protocol == 'HTTP':
            return self.simple_update_by_http()
        elif protocol == 'SCP':
            return self.simple_update_by_scp()


    def get_simple_update_targets(self):
        if self.module == 'BIOS' or self.module == 'BUNDLE':
            return ['redfish/v1/UpdateService/FirmwareInventory/DPU_OS']
        elif self.module == 'CONFIG':
            return ["redfish/v1/UpdateService/FirmwareInventory/golden_image_config"]
        else:
            raise Err_Exception(Err_Num.UNSUPPORTED_MODULE, "Only BIOS and CONFIG can be updated by SimpleUpdate")


    def simple_update_impl(self, protocol, image_uri):
        url = self._get_url_base() + '/UpdateService/Actions/UpdateService.SimpleUpdate'
        headers = {
            'Content-Type'     : 'application/json'
        }
        data = {
            'TransferProtocol' : protocol,
            'ImageURI'         : image_uri,
            'Targets'          : self.get_simple_update_targets(),
            'Username'         : self._get_local_user()
        }
        response = self._http_post(url, data=json.dumps(data), headers=headers)
        self.log('Do Simple Update (Update BFB or Configurations ...)', response)
        self._handle_status_code(response, [100, 200, 202], self._update_in_progress_err_handler)
        return self._extract_task_handle(response)


    def simple_update_by_scp(self):
        self.confirm_ssh_key_with_bmc()
        print("Start to do Simple Update (SCP)")
        return self.simple_update_impl('SCP', self._format_ip(self._get_local_ip()) + '/' + os.path.abspath(self.fw_file_path))


    def run_command_on_bmc(self, command, exit_on_error=True):
        self.log("Run command on BMC: {}".format(command))
        rc, output = (0, '')
        try:
            output = subprocess.check_output(command, stderr=subprocess.STDOUT, shell=True, universal_newlines=True)
        except subprocess.CalledProcessError as e:
            rc = e.returncode
            output = e.output.strip()
        self.log('Output: {}\nError: {}'.format(output, rc))
        if rc != 0:
            if not exit_on_error:
                print("Error: Failed to run command on BMC: {}".format(output))
            else:
                raise Err_Exception(output, 'Command "{}" failed with return code {}'.format(command, rc))
        return output


    def http_server(self):
        debug = self.debug
        from http.server import HTTPServer, SimpleHTTPRequestHandler
        class _SimpleHTTPRequestHandler(SimpleHTTPRequestHandler):
            def log_message(self, format, *args):
                if debug:
                    super().log_message(format, *args)

        abs_dir = os.path.dirname(os.path.abspath(self.fw_file_path))
        os.chdir(abs_dir)
        if self._is_ipv4:
            _HTTPServer = HTTPServer
        else:
            class HTTPServerV6(HTTPServer):
                address_family = socket.AF_INET6
            _HTTPServer = HTTPServerV6

        httpd = _HTTPServer((self._get_local_ip(), 0), _SimpleHTTPRequestHandler)
        port = httpd.server_address[1]
        write_fd = self._http_server_port_pipe[1]
        os.write(write_fd, bytes(str(port), 'utf-8'))
        httpd.serve_forever()


    def create_http_server_thread(self):
        import threading
        thread = threading.Thread(target=self.http_server, daemon=True)
        thread.start()


    def create_http_server_process(self):
        self._http_server_process = Process(target=self.http_server)
        self._http_server_process.daemon = True
        self._http_server_process.start()


    def read_http_server_port(self):
        read_fd = self._http_server_port_pipe[0]
        timeout = 60 # Seconds
        ready_to_read, _, _ = select.select([read_fd], [], [], timeout)
        if read_fd in ready_to_read:
            data = os.read(read_fd, 1024)
            port = int(data)
            return port
        else:
            raise Err_Exception(Err_Num.FAILED_TO_START_HTTP_SERVER)


    def simple_update_by_http(self):
        self.create_http_server_process()
        self._local_http_server_port = self.read_http_server_port()
        print("Start to do Simple Update (HTTP)")
        return self.simple_update_impl('HTTP', self._format_ip(self._get_local_ip()) + ':' + str(self._local_http_server_port) + '//' + os.path.basename(self.fw_file_path))


    def update_bmc_fw_multipart(self, url):
        update_params  = {
            "ForceUpdate": not self.skip_same_version
        }
        multi_part_param = {
            'UpdateParameters' : {
                'data'         : json.dumps(update_params),
                'is_file_path' : False,
                'type'         : None
            },
            'UpdateFile'       : {
                'data'         : self.fw_file_path,
                'is_file_path' : True,
                'type'         : 'application/octet-stream'
            }
        }
        response = self._multi_part_push(url, multi_part_param)

        self.log('Update Firmware', response)
        self._handle_status_code(response, [100, 200, 202], self._update_in_progress_err_handler)
        return self._extract_task_handle(response)


    def update_bmc_fw_deprecated(self, url):
        headers = {
            'Content-Type' : 'application/octet-stream'
        }
        response = self._upload_file(url, self.fw_file_path, headers=headers)
        self.log('Update Firmware', response)
        self._handle_status_code(response, [100, 200, 202], self._update_in_progress_err_handler)
        return self._extract_task_handle(response)


    def update_bmc_fw(self):
        multi_part_uri, deprecated_uri  = self.get_push_uri()
        if multi_part_uri is not None:
            task_handle = self.update_bmc_fw_multipart(self._get_prot_ip_port() + multi_part_uri)
        elif deprecated_uri is not None:
            task_handle = self.update_bmc_fw_deprecated(self._get_prot_ip_port() + deprecated_uri)
        else:
            raise Err_Exception(Err_Num.PUSH_URI_NOT_FOUND)
        return task_handle


    def _get_task_status(self, task_handle):
        url = self._get_prot_ip_port() + task_handle
        response = self._http_get(url)
        self.log('Get Task Satatus', response)
        self._handle_status_code(response, [200])

        '''
        {
            "PercentComplete": 0,
            "StartTime": "2024-06-05T13:16:37+00:00",
            "TaskMonitor": "/redfish/v1/TaskService/Tasks/11/Monitor",
            "TaskState": "Running",
            "TaskStatus": "OK"
        }
        '''
        try:
            percent = response.json()['PercentComplete']
            state   = response.json()['TaskState']
            status  = response.json()['TaskStatus']
            message = response.json()['Messages']
            payload = response.json()['Payload']
            return {'state': state, 'status': status, 'percent': percent, 'message': str(message), 'payload': payload}
        except:
            raise Err_Exception(Err_Num.BAD_RESPONSE_FORMAT, 'Failed to extract task status')


    def reboot_bmc(self):
        print("Restart BMC to make new firmware take effect")
        url = self._get_url_base() + '/Managers/Bluefield_BMC/Actions/Manager.Reset'
        headers = {
            'Content-Type' : 'application/octet-stream'
        }
        data = {
            'ResetType' : 'GracefulRestart'
        }
        response = self._http_post(url, data=json.dumps(data), headers=headers)
        self.log('Reboot BMC', response)
        self._handle_status_code(response, [200])
        self._wait_for_bmc_on()


    def reboot_cec(self):
        url = self._get_url_base() + '/Chassis/Bluefield_ERoT/Actions/Chassis.Reset'
        headers = {
            'Content-Type' : 'application/json'
        }
        data = {
            'ResetType' : 'GracefulRestart'
        }
        response = self._http_post(url, data=json.dumps(data), headers=headers)
        self.log('Reboot CEC', response)

        def err_handler(response):
            try:
                code = response.json()['error']['code']
            except:
                code = ''
            if 'ActionNotSupported' in code:
                raise Err_Exception(Err_Num.NOT_SUPPORT_CEC_RESTART, 'Please use power cycle of the whole system instead')
            elif 'ResourceNotFound' in code:
                raise Err_Exception(Err_Num.NO_PENDING_CEC_FW, 'Skip CEC reboot')
        self._handle_status_code(response, [200], err_handler)

        # Print the message, only after CEC restart really happened without exception.
        print("Restart CEC to make new firmware take effect")
        self._wait_for_bmc_on()


    def try_reboot_cec(self):
        try:
            self.reboot_cec()
        except Exception as e:
            if e.err_num == Err_Num.NOT_SUPPORT_CEC_RESTART or e.err_num == Err_Num.NO_PENDING_CEC_FW:
                print(str(e))
            else:
                raise e


    def _wait_for_bmc_on(self, show_progress=True):
        timeout = 60 * 3 # Wait up to 3 minutes
        start   = int(time.time())
        end     = start + timeout
        while True:
            cur = int(time.time())
            if cur > end:
                if show_progress:
                    self._print_process(100)
                break
            time.sleep(4)
            try:
                self._get_ver('BMC')
                self._get_ver('CEC')
                if show_progress:
                    self._print_process(100)
                break
            except Exception as e:
                if show_progress:
                    self._print_process(100 * (cur - start) / timeout)
        if show_progress:
            print()


    def _parse_bmc_version(self, version_str):
        """
        Parse BMC version string and return tuple of (major, minor, patch)
        Example: "BF-24.10-33" -> (24, 10, 33)
        """
        if not version_str or not isinstance(version_str, str):
            return (0, 0, 0)

        # Remove BF- prefix if present
        version = version_str.replace('BF-', '')

        try:
            # Split by dots and dashes to get version components
            parts = version.replace('-', '.').split('.')
            if len(parts) >= 3:
                major = int(parts[0])
                minor = int(parts[1])
                patch = int(parts[2])
                return (major, minor, patch)
        except (ValueError, IndexError):
            pass

        return (0, 0, 0)


    def _compare_bmc_versions(self, version1, version2):
        """
        Compare two BMC version strings
        Returns:
        -1 if version1 < version2
         0 if version1 == version2
         1 if version1 > version2
        """
        v1_tuple = self._parse_bmc_version(version1)
        v2_tuple = self._parse_bmc_version(version2)

        if v1_tuple < v2_tuple:
            return -1
        elif v1_tuple > v2_tuple:
            return 1
        else:
            return 0


    def clear_sel_log(self):
        """Clear the System Event Log (SEL) on BMC"""
        print("Clearing BMC SEL log")
        url = self._get_url_base() + '/Systems/Bluefield/LogServices/EventLog/Actions/LogService.ClearLog'
        headers = {
            'Content-Type': 'application/json'
        }
        data = {}

        try:
            response = self._http_post(url, data=json.dumps(data), headers=headers)
            self.log('Clear BMC SEL log', response)
            self._handle_status_code(response, [200, 204])
            print("BMC SEL log cleared successfully")
        except Exception as e:
            if self.debug:
                print("Warning: Failed to clear BMC SEL log: {}".format(e))
            # Don't fail the entire update if SEL clearing fails
            pass


    def _check_and_clear_sel_if_needed(self, old_version, new_version):
        """
        Check if SEL log should be cleared based on version upgrade/downgrade criteria
        Clear SEL if old version < BF-24.10-33 and new version >= BF-24.10-33 (upgrade)
        Clear SEL also if old version >= BF-24.10-33 and new version < BF-24.10-33 (downgrade)
        """
        threshold_version = "BF-24.10-33"

        # Check if old version is less than threshold
        old_vs_threshold = self._compare_bmc_versions(old_version, threshold_version)

        # Check if new version is greater than or equal to threshold
        new_vs_threshold = self._compare_bmc_versions(new_version, threshold_version)

        if old_vs_threshold < 0 and new_vs_threshold >= 0:
            print("BMC firmware upgraded from {} to {} (crossing BF-24.10-33 threshold)".format(old_version, new_version))
            self.clear_sel_log()
        elif old_vs_threshold >= 0 and new_vs_threshold < 0:
            print("BMC firmware downgraded from {} to {} (crossing BF-24.10-33 threshold)".format(old_version, new_version))
            self.clear_sel_log()


    def factory_reset_bmc(self):
        print("Factory reset BMC configuration")
        url = self._get_url_base() + '/Managers/Bluefield_BMC/Actions/Manager.ResetToDefaults'
        headers = {
            'Content-Type' : 'application/json'
        }
        data = {
            'ResetToDefaultsType' : 'ResetAll'
        }
        response = self._http_post(url, data=json.dumps(data), headers=headers)
        self.log('Factory Reset BMC', response)
        self._handle_status_code(response, [200])
        self._wait_for_bmc_on()


    def _set_bmc_rshim_display_level(self, value):
        self.run_command_on_bmc("sshpass -p {password} {ssh} {username}@{ip} '{command}'".format(
            ssh=self.ssh,
            password=self.ssh_password,
            username=self.ssh_username,
            ip=self.bmc_ip,
            command='/bin/bash -c "echo DISPLAY_LEVEL {value} > /dev/rshim0/misc"'.format(value=value),
            exit_on_error=False
        ))


    def get_bmc_rshim_misc(self):
        misc = self.run_command_on_bmc("sshpass -p {password} {ssh} {username}@{ip} '{command}'".format(
            ssh=self.ssh,
            password=self.ssh_password,
            username=self.ssh_username,
            ip=self.bmc_ip,
            command='/bin/bash -c "cat /dev/rshim0/misc"',
            exit_on_error=False
        ))
        return misc


    def _print_process(self, percent):
        print('\r', end='')
        flag = '|' if self.process_flag else '-'
        self.process_flag = not self.process_flag
        print('Process%s: %3d%%:'%(flag, percent), '░' * (int(percent) // 2), end='')


    def _sleep_with_process_with_percent(self, sec, start_percent=0, end_percent=100):
        for i in range(1, sec+1):
            time.sleep(1)
            self._print_process(start_percent + ((i * (end_percent - start_percent)) // sec))


    def _sleep_with_process(self, sec):
        self._sleep_with_process_with_percent(sec)
        print()


    def _extract_ver_from_fw_file(self, pattern):
        file_name = os.path.basename(self.fw_file_path)
        match     = re.search(pattern, file_name)
        substring = match.group(0)
        return substring


    def extract_cec_ver_from_fw_file(self):
        return self._extract_ver_from_fw_file(r'\d\d.\d\d.\d\d\d\d.\d\d\d\d')


    def extract_bmc_ver_from_fw_file(self):
        return self._extract_ver_from_fw_file(r'\d\d.\d\d-\d')


    def extract_atf_uefi_ver_from_fw_file(self):
        command = r'strings {} | grep -m 1 "(\(release\|debug\))"'.format(self.fw_file_path)
        process   = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = process.communicate()
        if process.returncode != 0:
            raise Err_Exception(Err_Num.FAILED_TO_GET_VER_FROM_FILE, 'Command "{}" failed with return code {}'.format(command, process.returncode))

        return str(out.decode()).strip()


    def is_fw_file_for_bmc(self):
        command  = 'strings {} | grep -i apfw'.format(self.fw_file_path)
        process  = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = process.communicate()
        if process.returncode != 0:
            return False
        return True


    def is_fw_file_for_cec(self):
        command  = 'strings {} | grep -i ecfw'.format(self.fw_file_path)
        process  = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = process.communicate()
        if process.returncode != 0:
            return False
        return True


    def is_fw_file_for_atf_uefi(self):
        try:
            self.extract_atf_uefi_ver_from_fw_file()
        except:
            return False
        return True


    def is_fw_file_for_conf(self):
        if not self.is_fw_file_for_atf_uefi():
            return False
        command  = 'strings {} | grep -i toutiao'.format(self.fw_file_path)
        process  = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = process.communicate()
        if process.returncode != 0:
            return False
        return True


    # return True:  task completed successfully
    # return False: task cancelled for skip_same_version
    def _wait_task(self, task_handle, max_second=15*60, check_step=10, err_handler=None):
        # Check the task status within a loop
        for i in range(1, max_second//check_step + 1):
            task_state = self._get_task_status(task_handle)
            if task_state['state'] != "Running":
                break
            self._print_process(task_state['percent'])
            time.sleep(check_step)

        # Check the task is completed successfully
        if task_state['state'] == 'Completed' and task_state['status'] == 'OK' and task_state['percent'] == 100:
            self._print_process(100)
            print()
        elif task_state['state'] == 'Running':
            raise Err_Exception(Err_Num.TASK_TIMEOUT, "The task {} is timeout".format(task_handle))
        else:
            if err_handler is not None:
                err_handler(task_state)

            if 'Component image is identical' in task_state['message']:
                return False
            elif 'Wait for background copy operation' in task_state['message']:
                raise Err_Exception(Err_Num.BMC_BACKGROUND_BUSY, 'Please try to update the firmware later')
            raise Err_Exception(Err_Num.TASK_FAILED, task_state['message'])
        return True

    def validate_args(self, items):
        if 'UserName' in items:
            if self.username is None:
                raise Err_Exception(Err_Num.USERNAME_NOT_GIVEN)
        if 'Password' in items:
            if self.password is None:
                raise Err_Exception(Err_Num.PASSWORD_NOT_GIVEN)
        if 'BmcIP' in items:
            if self.bmc_ip is None:
                raise Err_Exception(Err_Num.BMC_IP_NOT_GIVEN)
        if 'Module' in items:
            if self.module is None:
                raise Err_Exception(Err_Num.MODULE_NOT_GIVEN)
        if 'FwFile' in items:
            if self.fw_file_path is None:
                raise Err_Exception(Err_Num.FW_FILE_NOT_GIVEN)
            if not os.access(self.fw_file_path, os.R_OK):
                raise Err_Exception(Err_Num.FILE_NOT_ACCESSIBLE, 'Firmware file: {}'.format(self.fw_file_path))
        if 'FRU' in items:
            if not self.oem_fru:
                raise Err_Exception(Err_Num.FRU_NOT_GIVEN)


    def validate_arg_for_update(self):
        self.validate_args(['UserName', 'Password', 'BmcIP', 'Module', 'FwFile'])


    def validate_arg_for_fru(self):
        self.validate_args(['UserName', 'Password', 'BmcIP', 'Module', 'FRU'])


    def validate_arg_for_show_versions(self):
        self.validate_args(['UserName', 'Password', 'BmcIP'])


    def validate_arg_for_reset_config(self):
        self.validate_args(['UserName', 'Password', 'BmcIP', 'Module'])


    def is_bmc_background_copy_in_progress(self):
        url = self._get_url_base() + '/Chassis/Bluefield_ERoT'
        response = self._http_get(url)
        self.log('Get ERoT status', response)
        self._handle_status_code(response, [200])

        '''
        {
          ...
          "Oem": {
            "Nvidia": {
              "@odata.type": "#NvidiaChassis.v1_0_0.NvidiaChassis",
              "AutomaticBackgroundCopyEnabled": true,
              "BackgroundCopyStatus": "Completed",
              "InbandUpdatePolicyEnabled": true
            }
          },
          ...
        }
        '''
        status = ''
        try:
            status = response.json()['Oem']['Nvidia']['BackgroundCopyStatus']
        except Exception as e:
            raise Err_Exception(Err_Num.BAD_RESPONSE_FORMAT, 'Failed to extract BackgroundCopyStatus')

        if status != 'Completed':
            return True
        else:
            return False


    def update_bmc_or_cec(self, is_bmc):
        self.validate_arg_for_update()
        self.wait_update_service_ready()

        # Check firmare file is for BMC/CEC
        correct_file = self.is_fw_file_for_bmc() if is_bmc else self.is_fw_file_for_cec()
        if not correct_file:
            raise Err_Exception(Err_Num.FW_FILE_NOT_MATCH_MODULE)

        old_ver = self.get_ver('BMC') if is_bmc else self.get_ver('CEC')
        if old_ver == '':
            raise Err_Exception(Err_Num.EMPTY_FW_VER, 'Get empty {} version'.format('BMC' if is_bmc else 'CEC'))

        if self.is_bmc_background_copy_in_progress():
            raise Err_Exception(Err_Num.BMC_BACKGROUND_BUSY, 'Please try to update the firmware later')

        # Start firmware update task
        print("Start to upload firmware")
        task_handle = self.update_bmc_fw()
        ret = self._wait_task(task_handle, max_second=(20*60 if is_bmc else 4*60), check_step=(10 if is_bmc else 2))
        if not ret:
            print("Skip updating the same version: {}".format(old_ver))
            return

        # Reboot bmc/cec
        self.reboot_bmc() if is_bmc else self.try_reboot_cec()

        new_ver = self.get_ver('BMC') if is_bmc else self.get_ver('CEC')

        # Clear SEL log if BMC firmware crossed the BF-24.10-33 threshold
        if is_bmc:
            self._check_and_clear_sel_if_needed(old_ver, new_ver)

        print('OLD {} Firmware Version: \n\t{}'.format(('BMC' if is_bmc else 'CEC'), old_ver))
        print('New {} Firmware Version: \n\t{}'.format(('BMC' if is_bmc else 'CEC'), new_ver))


    def is_rshim_enabled_on_bmc(self):
        url = self._get_url_base() + '/Managers/Bluefield_BMC/Oem/Nvidia'
        headers = {
            'Content-Type' : 'application/json'
        }
        response = self._http_get(url, headers=headers)
        self.log('Get rshim enable state', response)
        self._handle_status_code(response, [200])

        try:
            return response.json()['BmcRShim']['BmcRShimEnabled']
        except:
            raise Err_Exception(Err_Num.BAD_RESPONSE_FORMAT, 'Failed to extract BmcRShimEnabled')


    def enable_rshim_on_bmc(self, enable):
        url = self._get_url_base() + '/Managers/Bluefield_BMC/Oem/Nvidia'
        headers = {
            'Content-Type' : 'application/json'
        }
        data = {
            "BmcRShim": { "BmcRShimEnabled": enable }
        }
        response = self._http_patch(url, json.dumps(data), headers=headers)
        self.log('{} rshim on BMC'.format("Enable" if enable else "Disable"), response)
        self._handle_status_code(response, [200])


    def try_enable_rshim_on_bmc(self):
        if self.is_rshim_enabled_on_bmc():
            return True
        print("Try to enable rshim on BMC")
        self.enable_rshim_on_bmc(True)
        self._sleep_with_process_with_percent(10, 0, 30)
        if self.is_rshim_enabled_on_bmc():
            self._sleep_with_process_with_percent(1, 30, 100)
            print()
            return True

        # Try again if failed
        self.enable_rshim_on_bmc(False)
        self._sleep_with_process_with_percent(10, 30, 60)
        self.enable_rshim_on_bmc(True)
        self._sleep_with_process_with_percent(10, 60, 90)
        if self.is_rshim_enabled_on_bmc():
            self._sleep_with_process_with_percent(1, 90, 100)
            print()
            return True
        print()
        return False


    def _wait_for_bios_ready(self):
        print('Wait for BIOS ready')
        timeout = 60 * 3 # Wait up to 3 minutes
        start   = int(time.time())
        end     = start + timeout
        while True:
            cur = int(time.time())
            if cur > end:
                self._print_process(100)
                break
            ver = self.get_ver('ATF')
            if ver != '':
                self._print_process(100)
                break
            else:
                self._print_process(100 * (cur - start) / timeout)
                time.sleep(4)
        print()


    def get_local_user_ssh_pub_key(self):
        command = 'ssh-keyscan {}'.format(self._get_local_ip())
        process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = process.communicate()
        if process.returncode != 0:
            raise Err_Exception(Err_Num.FAILED_TO_GET_LOCAL_KEY, 'Command "{}" failed with return code {}'.format(command, process.returncode))

        '''
        127.0.0.1 ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBLxvoG8lUk0CyiQ2Jk9IlTlrESlRtLzyIhQnPsXe5//YWl5nHa6oTSbkIlwk090tchoUi9nwFtTDE5Lihs1qJEc=
        127.0.0.1 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPzhBRfJL2pZ6LNikFnlBg7iqYXh7BDbQpfg9f1R7nch
        '''
        try:
            key_list = out.decode().split('\n')
            ret_list = []
            for key in key_list:
                if key.strip() == '':
                    continue
                ret_list.append(' '.join(key.split(' ')[1:]))
            if len(ret_list) == 0:
                raise Err_Exception(Err_Num.FAILED_TO_GET_LOCAL_KEY)
            return ret_list
        except:
            raise Err_Exception(Err_Num.FAILED_TO_GET_LOCAL_KEY, 'There may be no ssh-key locally (for user {}). Please run ssh-keygen firstly'.format(self._get_local_user()))


    def exchange_ssh_key_with_bmc(self, local_key):
        url = self._get_url_base() + "/UpdateService/Actions/Oem/NvidiaUpdateService.PublicKeyExchange"
        headers = {
            'Content-Type' : 'application/json'
        }
        msg = {
          "RemoteServerIP"        : self._get_local_ip(),
          "RemoteServerKeyString" : local_key,
        }
        response = self._http_post(url, data=json.dumps(msg), headers=headers)
        self.log('Exchange SSH key with BMC', response)
        self._handle_status_code(response, [200])

        '''
        {
            "@Message.ExtendedInfo":
            [
                {
                    "@odata.type": "#Message.v1_1_1.Message",
                    "Message": "Please add the following public
                    key info to ~/.ssh/authorized_keys on the
                    remote server",
                    "MessageArgs": [
                        "<type> <bmc_public_key> root@dpu-bmc"
                    ]
                },
                {
                    ....
                }
            ]
        }
        '''
        try:
            return response.json()['@Message.ExtendedInfo'][0]['MessageArgs'][0]
        except:
            raise Err_Exception(Err_Num.BAD_RESPONSE_FORMAT, 'Failed to extract BMC SSH key')


    def is_bmc_key_in_local_authorized_keys(self, bmc_key):
        file_path = os.path.expanduser("~") + '/.ssh/authorized_keys'
        process = subprocess.Popen('grep "{}" {}'.format(bmc_key, file_path), shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = process.communicate()
        if process.returncode != 0:
            return False
        return True


    def set_bmc_key_into_local_authorized_keys(self, bmc_key):
        file_path = os.path.expanduser("~") + '/.ssh/authorized_keys'

        # Check and set write permission for ~/.ssh/authorized_keys
        old_permission = None
        if not os.access(file_path, os.W_OK):
            old_permission = os.stat(file_path).st_mode
            os.chmod(file_path, old_permission | stat.S_IWUSR)

        # Append the bmc key into authorized_keys
        with open(file_path, 'a') as f:
            f.write(bmc_key + '\n')

        # Recover the permission
        if old_permission is not None:
            os.chmod(file_path, old_permission)
            old_permission = os.stat(file_path).st_mode


    def confirm_ssh_key_with_bmc(self):
        local_keys = self.get_local_user_ssh_pub_key()
        for local_key in local_keys:
            bmc_key = self.exchange_ssh_key_with_bmc(local_key)
            if not self.is_bmc_key_in_local_authorized_keys(bmc_key):
                self.set_bmc_key_into_local_authorized_keys(bmc_key)


    def update_bios(self):
        self.validate_arg_for_update()
        self.wait_update_service_ready()

        if not self.is_fw_file_for_atf_uefi():
            raise Err_Exception(Err_Num.FW_FILE_NOT_MATCH_MODULE)

        # Skip the same firmware version, if need
        cur_atf_ver  = self.get_ver('ATF')
        cur_uefi_ver = self.get_ver('UEFI')
        if cur_atf_ver is None or cur_uefi_ver is None:
            raise Err_Exception(Err_Num.EMPTY_FW_VER, 'Get empty ATF/UEFI version')

        # Currently, we can only extract atf version from the fw file.
        # So, only do the same_version_check on atf version. Given the
        # assumption that atf verion and uefi version should change at
        # the same time within a package.
        fw_file_atf_ver = self.extract_atf_uefi_ver_from_fw_file()
        if self.skip_same_version and cur_atf_ver in fw_file_atf_ver:
            print('Skip updating the same firmware version: ATF--{} UEFI--{}'.format(cur_atf_ver, cur_uefi_ver))
            return

        # Enable rshim on BMC
        if not self.try_enable_rshim_on_bmc():
            raise Err_Exception(Err_Num.FAILED_TO_ENABLE_BMC_RSHIM, 'Please make sure rshim on Host side is disabled')

        self._start_and_wait_simple_update_task()
        self._wait_for_bios_ready()

        # Verify new version is the same as the fw file version
        new_atf_ver  = self.get_ver('ATF')
        new_uefi_ver = self.get_ver('UEFI')
        if new_atf_ver not in fw_file_atf_ver:
            raise Err_Exception(Err_Num.NEW_VERION_CHECK_FAILED, 'New BIOS version is not the version we want to update')
        print('Old {} Firmware Version: \n\tATF--{}, UEFI--{}'.format('BIOS', cur_atf_ver, cur_uefi_ver))
        print('New {} Firmware Version: \n\tATF--{}, UEFI--{}'.format('BIOS', new_atf_ver, new_uefi_ver))
        return True


    def get_dpu_boot_state(self):
        try:
            url = self._get_url_base() + '/Systems/Bluefield'
            response = self._http_get(url)
            self.log('Get DPU(ARM) boot state', response)
            self._handle_status_code(response, [200])
        except Exception as e:
            return ''

        state = ''
        try:
            state = response.json()['BootProgress']['OemLastState']
        except Exception as e:
            # Retry in case BMC reboot is in progress
            if self.debug:
                print("BMC is rebooting.")
            self._wait_for_bmc_on(False)
            try:
                response = self._http_get(url)
                self.log('Get DPU(ARM) boot state', response)
                self._handle_status_code(response, [200])
                state = response.json()['BootProgress']['OemLastState']
            except Exception as e:
                self.log('Got exception when getting DPU(ARM) boot state {}'.format(e))
        return state


    def get_dpu_mode(self):
        # Check BMC availability before proceeding
        self.check_bmc_availability()

        try:
            url = self._get_url_base() + '/Systems/Bluefield/Oem/Nvidia'
            response = self._http_get(url)
            self.log('Get DPU(ARM) mode', response)
            self._handle_status_code(response, [200])
            mode = response.json()['Mode']
        except Exception as e:
            raise Err_Exception(Err_Num.BAD_RESPONSE_FORMAT, 'Failed to extract DPU mode')
        return mode


    def _wait_for_dpu_ready(self):
        print('Waiting for the BFB installation to finish')
        timeout = 60 * 40 # Wait up to 40 minutes
        start   = int(time.time())
        end     = start + timeout
        while True:
            cur = int(time.time())
            if cur > end:
                self._print_process(100)
                break
            state = self.get_dpu_boot_state()
            if state == 'OsIsRunning':
                self._print_process(100)
                break
            else:
                self._print_process(100 * (cur - start) / timeout)
                time.sleep(30)
        print()


    def enable_runtime_rshim(self):
        self.log("Enable runtime rshim")
        url = self._get_url_base() + '/Systems/Bluefield/Oem/Nvidia/Actions/LFWP.Set'
        headers = {
            'Content-Type' : 'application/json'
        }
        data = {
            'LFWP' : 'Enabled'
        }
        response = self._http_post(url, data=json.dumps(data), headers=headers)
        self.log('Enable runtime rshim', response)
        self._handle_status_code(response, [200])


    def disable_runtime_rshim(self):
        self.log("Disable runtime rshim")
        url = self._get_url_base() + '/Systems/Bluefield/Oem/Nvidia/Actions/LFWP.Set'
        headers = {
            'Content-Type' : 'application/json'
        }
        data = {
            'LFWP' : 'Disabled'
        }
        response = self._http_post(url, data=json.dumps(data), headers=headers)
        self.log('Disable runtime rshim', response)
        self._handle_status_code(response, [200])


    def is_lfwp_supported(self):
        """Check if LFWP.Set action is supported on the BMC"""
        try:
            url = self._get_url_base() + '/Systems/Bluefield/Oem/Nvidia'
            response = self._http_get(url)
            self.log('Check LFWP.Set support', response)
            self._handle_status_code(response, [200])

            # Check if Actions/LFWP.Set exists in the response
            if 'Actions' in response.json() and '#LFWP.Set' in response.json()['Actions']:
                return True
            return False
        except Exception as e:
            if self.debug:
                print("Error checking LFWP.Set support: {}".format(e))
            return False


    def update_bundle(self):
        if self.lfwp:
            if not self.is_lfwp_supported():
                raise Err_Exception(Err_Num.UNSUPPORTED_MODULE, 'LFWP.Set is not supported on this BMC')

        self.validate_arg_for_update()
        self.wait_update_service_ready()
        cur_vers = self._get_all_versions_internal()
        old_bmc_ver = cur_vers['BMC']

        if not self.try_enable_rshim_on_bmc():
            raise Err_Exception(Err_Num.FAILED_TO_ENABLE_BMC_RSHIM, 'Please make sure rshim on Host side is disabled')

        self._set_bmc_rshim_display_level(2)

        if self.lfwp:
            self.enable_runtime_rshim()
        self._start_and_wait_simple_update_task()
        if self.lfwp:
            self.disable_runtime_rshim()
            time.sleep(120) # Wait for NIC fw to be updated and mlxfwreset to be done
        else:
            self._wait_for_dpu_ready()

        if self.reset_bios and not self.lfwp:
            self.send_reset_bios()
            self._wait_for_dpu_ready()
            time.sleep(60) # Wait for some time before getting all fw versions

        if self.lfwp:
            print('Waiting for NIC Firmware to be updated and mlxfwreset to be done')
            misc = self.get_bmc_rshim_misc()
            start = int(time.time())
            end = start + 30*60
            while 'Runtime upgrade finished' not in misc:
                cur = int(time.time())
                if cur > end:
                    self.log('NIC Firmware update timeout')
                    break
                time.sleep(60) # Wait for NIC fw to be updated and mlxfwreset to be done
                misc = self.get_bmc_rshim_misc()
                self._print_process(100 * (cur - start) / (end - start))
            self._print_process(100)
            print()

        new_vers = self._get_all_versions_internal()
        new_bmc_ver = new_vers['BMC']
        self._check_and_clear_sel_if_needed(old_bmc_ver, new_bmc_ver)

        self.show_old_new_versions(cur_vers, new_vers, ['BMC', 'CEC', 'ATF', 'UEFI', 'NIC'])

        if self.lfwp:
            bfb_nic_fw_ver = self.get_info_data_version('NIC')
            nic_fw_ver = self.get_ver('NIC')
            if bfb_nic_fw_ver != nic_fw_ver:
                print('\nWARNING: LFWP NIC firmware update is complete. Please check the running NIC firmware version on the DPU.')

        return True


    def send_reset_bios(self):
        print("Factory reset BIOS configuration (ResetBios) (will reboot the system)")
        url = self._get_url_base() + '/Systems/Bluefield/Bios/Actions/Bios.ResetBios'
        headers = {
            'Content-Type' : 'application/json'
        }
        response = self._http_post(url, data=None, headers=headers)
        self.log('Factory Reset BIOS (ResetBios)', response)
        self._handle_status_code(response, [200])
        # ResetBios command will send config image to DPU by rshim
        # That will reset the DPU automatically. No need to reboot it again
        # self.reboot_system()
        self._wait_for_system_power_on()


    def send_reset_efi_vars(self):
        print("Factory reset EFI Var configuration (ResetEfiVars) (will reboot the system)")
        url = self._get_url_base() + '/Systems/Bluefield/Bios/Settings'
        headers = {
            'Content-Type' : 'application/json'
        }
        data = {
            'Attributes': {
                'ResetEfiVars': True,
            },
        }
        response = self._http_patch(url, data=json.dumps(data), headers=headers)
        self.log('Factory reset EFI Var (ResetEfiVars)', response)
        self._handle_status_code(response, [200])
        self.reboot_system()
        self._wait_for_system_power_on()


    def reboot_system(self):
        url = self._get_url_base() + '/Systems/Bluefield/Actions/ComputerSystem.Reset'
        headers = {
            'Content-Type' : 'application/json'
        }
        data = {
            'ResetType': 'GracefulRestart'
        }
        response = self._http_post(url, data=json.dumps(data), headers=headers)
        self.log('Reboot BIOS', response)
        self._handle_status_code(response, [200, 204])


    def get_system_power_state(self):
        try:
            url = self._get_url_base() + '/Systems/Bluefield'
            response = self._http_get(url)
            self.log('Get System State', response)
            self._handle_status_code(response, [200])
        except Exception as e:
            return ''

        state = ''
        try:
            state = response.json()['PowerState']
        except Exception as e:
            raise Err_Exception(Err_Num.BAD_RESPONSE_FORMAT, 'Failed to extract system power state')
        return state


    def _wait_for_system_power_on(self):
        pre_state = self.get_system_power_state()
        timeout = 60 * 3 # Wait up to 3 minutes
        start   = int(time.time())
        end     = start + timeout
        while True:
            cur = int(time.time())
            if cur > end:
                self._print_process(100)
                break
            new_state = self.get_system_power_state()
            # Since, after reboot command send, the state is changing as following:
            # ...->On->Paused->PoweringOn->On
            # So, we need following two conditions to judge whether the system is On again
            if new_state != pre_state and new_state  == 'On':
                self._print_process(100)
                break
            else:
                self._print_process(100 * (cur - start) / timeout)
                time.sleep(4)
            pre_state = new_state
        print()


    def update_oem_fru(self):
        """
        Update the OEM FRU data with the provided key-value pairs in the format 'Section:Key=Value'
        """
        self.validate_arg_for_fru()
        oem_fru_dict = {}
        if self.debug:
            print("OEM FRU data to be updated:", self.oem_fru)
        # Process each item in the provided OEM FRU data
        for item in self.oem_fru:
            try:
                section_key, value = item.split('=', 1)
                section, key = section_key.split(':')
                combined_key = section + key
                # Check if the value exceeds 63 characters
                if len(value) > 63:
                    raise Err_Exception(Err_Num.INVALID_INPUT_PARAMETER, "Value for {} exceeds 63 characters: {}".format(section_key, value))
                oem_fru_dict[combined_key] = value
                # Validate ManufactureDate format
                if section_key == 'Product:ManufactureDate' and value and not self._validate_fru_date_format(value):
                    raise Err_Exception(Err_Num.INVALID_INPUT_PARAMETER, "Invalid date format for ManufactureDate. Expected format: DD/MM/YYYY HH:MM:SS")
                if self.debug:
                    print("Updated FRU field: {} with value: {}".format(section_key, value))
            except ValueError:
                raise Err_Exception(Err_Num.INVALID_INPUT_PARAMETER, "Invalid format for OEM FRU data: {}. Expected format 'Section:Key=Value'".format(item))

        print("OEM FRU data to be updated:", json.dumps(oem_fru_dict, indent=4))

        # Construct the URL for the HTTP PUT request
        url = self._get_url_base() + '/Systems/Bluefield/Oem/Nvidia'
        headers = {'Content-Type': 'application/json'}

        # Send the HTTP PUT request to update the OEM FRU data
        response = self._http_put(url, data=json.dumps(oem_fru_dict), headers=headers)
        self.log('Update OEM FRU data', response)
        if response.status_code != 200:
            raise Err_Exception(Err_Num.INVALID_STATUS_CODE, "Failed to update OEM FRU data, status code: {}".format(response.status_code))
        print("OEM FRU data updated successfully.")


    def _start_and_wait_simple_update_task(self):
        protocol, task_handle = self.simple_update()

        def err_handler(task_state):
            if protocol == "SCP" and "Please provide server's public key using PublicKeyExchange" in task_state['message']:
                raise Err_Exception(Err_Num.PUBLIC_KEY_NOT_EXCHANGED)
            elif protocol == "HTTP" and "Check and restart server's web service" in task_state['message']:
                raise Err_Exception(Err_Num.HTTP_FILE_SERVER_NOT_ACCESSIBLE, "Server address: {}:{}".format(self._format_ip(self._get_local_ip()), self._local_http_server_port))

        self._wait_task(task_handle, max_second=20*60, check_step=2, err_handler=err_handler)


    def update_conf(self):
        self.validate_arg_for_update()
        self.wait_update_service_ready()

        if not self.is_fw_file_for_conf():
            raise Err_Exception(Err_Num.FW_FILE_NOT_MATCH_MODULE)

        if not self.try_enable_rshim_on_bmc():
            raise Err_Exception(Err_Num.FAILED_TO_ENABLE_BMC_RSHIM, 'Please make sure rshim on Host side is disabled')

        # 1. Update config image in DPU BMC Flash using Redfish
        self._start_and_wait_simple_update_task()

        # 2. In order to update the ARM UPVS partition and the corresponding UEFI capsule in eMMC, a factory reset should be triggered
        self.send_reset_bios()

        # 3. BMC reboot flow shall be triggered to load the new configuration
        self.reboot_bmc()


    def wait_for_background_copy(self, timeout_minutes=20):
        """
        Wait for BMC background copy operation to complete.

        Args:
            timeout_minutes (int): Maximum time to wait in minutes (default 20)

        Returns:
            None

        Raises:
            Err_Exception: If background copy doesn't complete within timeout
        """
        print("Waiting for BMC background copy to complete...")
        timeout = 60 * timeout_minutes  # Convert minutes to seconds
        start = int(time.time())
        end = start + timeout

        while True:
            cur = int(time.time())
            if cur > end:
                raise Err_Exception(Err_Num.BMC_BACKGROUND_BUSY,
                                  'BMC background copy operation did not complete within {} minutes'.format(timeout_minutes))

            if not self.is_bmc_background_copy_in_progress():
                print("BMC background copy completed")
                self._print_process(100)
                print()
                return

            # Show progress
            self._print_process(100 * (cur - start) / timeout)
            time.sleep(10)  # Check every 10 seconds


    def do_update(self):
        # Check BMC availability before starting any operations
        self.check_bmc_availability()

        if self.is_bmc_background_copy_in_progress():
            # Wait for background copy to complete before proceeding
            self.wait_for_background_copy()

        # Wait for a random time to avoid race condition
        time.sleep(random.randint(10, 30))

        try:
            last_task_info = self.get_last_task_info()
        except Exception as e:
            if self.debug:
                print("Error getting last task info: {}".format(e))
            last_task_info = None

        if last_task_info and last_task_info['state'] == 'Running':
            print("Waiting for last task to finish:\n    Id:        {}\n    TargetUri: {}".format(last_task_info['id'], last_task_info['payload']['TargetUri']))
            self.log('Last task info: {}'.format(last_task_info))
            if last_task_info['payload']['TargetUri'] == '/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate':
                self.log('SimpleUpdate task detected, waiting for 20 minutes')
                time.sleep(20*60)
            self._wait_task(last_task_info['id'], max_second=20*60, check_step=2, err_handler=None)
            time.sleep(random.randint(10, 30))
            last_task_info = self.get_last_task_info()
            if last_task_info and last_task_info['state'] == 'Running':
                raise Err_Exception(Err_Num.BMC_BACKGROUND_BUSY, 'Please try to update the {} later'.format(self.module))

        if self.module == 'BMC' or self.module == "CEC":
            self.update_bmc_or_cec((self.module == 'BMC'))
        elif self.module == 'BIOS':
            self.update_bios()
        elif self.module == 'FRU':
            self.update_oem_fru()
        elif self.module == 'CONFIG':
            self.update_conf()
        elif self.module == 'BUNDLE':
            self.update_bundle()
        else:
            raise Err_Exception(Err_Num.UNSUPPORTED_MODULE, "Unsupported module: {}".format(self.module))


    def reset_config(self):
        self.validate_arg_for_reset_config()
        # Check BMC availability before proceeding
        self.check_bmc_availability()
        if self.module == 'BMC':
            self.factory_reset_bmc()
        elif self.module == 'BIOS':
            self.send_reset_bios()
        else:
            raise Err_Exception(Err_Num.UNSUPPORTED_MODULE, "Unsupported module to reset config: {}".format(self.module))


    def _get_firmware_uri_list(self):
        url = self._get_url_base() + '/UpdateService/FirmwareInventory'
        response = self._http_get(url)
        self.log('Get firmware URI list', response)
        self._handle_status_code(response, [200])

        uri_list = []
        try:
            members = response.json()['Members']
            for member in members:
                uri_list.append(member['@odata.id'])
        except Exception as e:
            raise Err_Exception(Err_Num.BAD_RESPONSE_FORMAT, 'Failed to extract firmware URI list')
        return uri_list


    def _get_firmware_uri_by_resource(self, resource):
        return self.redfish_root + '/UpdateService/FirmwareInventory/' + resource


    def _get_firmware_module_from_uri(self, uri):
        for module, resource in self.module_resource.items():
            if uri == self._get_firmware_uri_by_resource(resource):
                return module
        return uri.split('/')[-1]


    def _get_all_versions_internal(self):
        """Internal method to get all versions without BMC availability check"""
        uri_list = self._get_firmware_uri_list()
        vers = {}
        for uri in uri_list:
            module = self._get_firmware_module_from_uri(uri)
            vers[module] = self.get_ver_by_uri(uri)
        return vers


    def get_all_versions(self):
        self.validate_arg_for_show_versions()
        return self._get_all_versions_internal()


    def show_versions(self, vers):
        for module, ver in vers.items():
            print("%17s : %50s"%(module, ver))

    def get_info_data_version(self, module):
        if not self.info_data:
            return 'NA'

        # Map module names to info_data keys
        info_module = {
            'ATF': 'BF3_ATF',
            'UEFI': 'BF3_UEFI',
            'BMC': 'BF3_BMC_FW',
            'CEC': 'BF3_CEC_FW',
            'NIC': 'BF3_NIC_FW'
        }

        for member in self.info_data["Members"]:
            if member["Name"] == info_module[module]:
                if member["Name"] == "BF3_BMC_FW":
                    member["Version"] = "BF-" + member["Version"]
                elif member["Name"] == "BF3_CEC_FW":
                    member["Version"] = member["Version"] + "_n02"
                elif member["Name"] == "BF3_ATF":
                    member["Version"] = self.extract_atf_uefi_ver_from_fw_file()
                return member["Version"]
        return 'NA'

    def show_old_new_versions(self, old_vers, new_vers, filter = []):
        print("%10s   %40s  %40s  %40s"%('', 'OLD Version', 'NEW Version', 'BFB Version'))
        for module, ver in old_vers.items():
            if len(filter) == 0 or module in filter:
                info_ver = self.get_info_data_version(module)
                print("%10s : %40s  %40s  %40s"%(module, ver, new_vers.get(module, ''), info_ver))


    def show_all_versions(self):
        # Check BMC availability before proceeding
        self.check_bmc_availability()
        vers = self._get_all_versions_internal()
        self.show_versions(vers)

    def set_info_data(self, info_data):
        self.info_data = info_data

    def get_last_task_id(self):
        """
        Get the last task ID from the BMC's TaskService.

        Returns:
            str: The last task ID, or None if no tasks found

        Raises:
            Err_Exception: If unable to get task list or parse response
        """
        # Get the list of tasks from TaskService
        url = self._get_url_base() + '/TaskService/Tasks'
        response = self._http_get(url)
        self.log('Get Task List', response)
        self._handle_status_code(response, [200])

        try:
            # Extract the task list from response
            tasks = response.json().get('Members', [])
            if not tasks:
                return None

            # Get the last task ID from the list
            last_task = tasks[-1]['@odata.id']
            last_task_id = last_task.split('/')[-1]
            return last_task_id

        except Exception as e:
            raise Err_Exception(Err_Num.BAD_RESPONSE_FORMAT, 'Failed to extract last task ID')

    def get_last_task_info(self):
        """
        Get information about the last task.

        Returns:
            dict: Task information including:
                - id: Task ID
                - state: Task state (Running/Completed/etc)
                - status: Task status (OK/Error)
                - percent: Completion percentage
                - message: Task message

        Raises:
            Err_Exception: If no tasks found or unable to get task info
        """
        task_id = self.get_last_task_id()
        if not task_id:
            return None

        # Get task status using the existing _get_task_status method
        task_uri = '/redfish/v1/TaskService/Tasks/{}'.format(task_id)
        task_state = self._get_task_status(task_uri)

        # Add task ID to the returned info
        task_state['id'] = task_uri
        return task_state
