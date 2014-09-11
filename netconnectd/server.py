from __future__ import (print_function, absolute_import)


import argparse
import logging
import netaddr
import re
import subprocess
import time
import threading
import wifi
import wifi.scheme
import wifi.utils


from .util import has_link, common_arguments, default_config, parse_configfile, InvalidConfig
from .protocol import (Message, StartApMessage, StopApMessage, ListWifiMessage, ConfigureWifiMessage, SelectWifiMessage,
                       StatusMessage, SuccessResponse, ErrorResponse)


iwconfig_re = re.compile('ESSID:"(?P<ssid>[^"]+)".*Access Point: (?P<address>%s).*' % wifi.utils.mac_addr_pattern , re.DOTALL)


class Server(object):

    @classmethod
    def convert_cells(cls, cells):
        result = []
        for cell in cells:
            result.append(dict(ssid=cell.ssid, channel=cell.channel, address=cell.address, encrypted=cell.encrypted, signal=cell.signal if hasattr(cell, "signal") else None))
        return result

    def __init__(self, server_address=None, wifi_if=None, wired_if=None, linkmon_enabled=True, linkmon_maxdown=3, linkmon_interval=10,
                 ap_driver="nl80211", ap_ssid=None, ap_psk=None, ap_name='netconnectd_ap', ap_channel=2, ap_ip='10.250.250.1',
                 ap_network='10.250.250.0/24', ap_range=('10.250.250.100', '10.250.250.200'), ap_forwarding=False,
                 ap_domain=None, wifi_name='netconnect_wifi', wifi_free=False, path_hostapd="/usr/sbin/hostapd",
                 path_hostapd_conf="/etc/hostapd/conf.d", path_dnsmasq="/usr/sbin/dnsmasq", path_dnsmasq_conf="/etc/dnsmasq.conf.d",
                 path_interfaces="/etc/network/interfaces"):

        self.logger = logging.getLogger(__name__)

        self.Hostapd = wifi.Hostapd.for_hostapd_and_confd(path_hostapd, path_hostapd_conf)
        self.Dnsmasq = wifi.Dnsmasq.for_dnsmasq_and_confd(path_dnsmasq, path_dnsmasq_conf)
        self.Scheme = wifi.Scheme.for_file(path_interfaces)
        self.AccessPoint = wifi.AccessPoint.for_classes(
            hostapd_cls=self.Hostapd,
            dnsmasq_cls=self.Dnsmasq,
            scheme_cls=self.Scheme
        )

        self.ap_name = ap_name
        self.wifi_if = wifi_if
        self.wifi_name = wifi_name
        self.wifi_free = wifi_free

        self.wired_if = wired_if

        self.linkmon_enabled = linkmon_enabled
        self.linkmon_maxdown = linkmon_maxdown
        self.linkmon_interval = linkmon_interval

        self.server_address = server_address

        # prepare access point configuration
        self.access_point = self.AccessPoint.for_arguments(self.wifi_if, self.ap_name,
                                                           ap_ssid, ap_channel, ap_ip, ap_network,
                                                           ap_range[0], ap_range[1], forwarding_to=wired_if if ap_forwarding else None,
                                                           hostap_options=dict(psk=ap_psk, driver=ap_driver),
                                                           dnsmasq_options=dict(domain=ap_domain))
        self.access_point.save(allow_overwrite=True)
        if self.access_point.is_running():
            self.access_point.deactivate()

        # prepare wifi configuration
        self.wifi_connection = self.Scheme.find(self.wifi_if, self.wifi_name)
        if not self.wifi_connection:
            self.logger.info("No wifi configuration available yet, will only be able to connect via wire or act as an access point for now")
            self.wifi_available = False
        else:
            self.wifi_available = True

        # wifi cell cache
        self.cells = None

        # for status messages...
        self.last_link = False
        self.last_reachable_devs = tuple()

        # prepare link monitor thread
        self.link_thread = threading.Thread(target=self._link_monitor, kwargs=dict(callback=self.on_link_change, interval=self.linkmon_interval))
        self.link_thread.daemon = True

        # we start out with a fully maxed link down count so that we will directly try to create a connection
        self.link_down_count = linkmon_maxdown


    def _link_monitor(self, interval=10, callback=None):
        former_link, reachable_devs = has_link()

        while True:
            current_link, reachable_devs = has_link()
            callback(former_link, current_link, reachable_devs)
            time.sleep(interval)
            former_link = current_link

    def _socket_monitor(self, server_address, callbacks=None):
        if not callbacks:
            callbacks = dict()

        import socket
        import os
        try:
            os.unlink(server_address)
        except OSError:
            if os.path.exists(server_address):
                raise

        self.logger.info('Starting up on %s...' % server_address)

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(server_address)
        os.chmod(server_address, 438)

        sock.listen(1)

        while True:
            self.logger.info('Waiting for connection...')
            connection, client_address = sock.accept()

            try:
                buffer = []
                while True:
                    chunk = connection.recv(16)
                    if chunk:
                        self.logger.info('Recv: %r' % chunk)
                        buffer.append(chunk)
                        if chunk.endswith('\x00'):
                            break

                data = ''.join(buffer).strip()[:-1]

                ret = False
                result = 'unknown message'

                message = Message.from_str(data)
                if message and callbacks and message.cmd in callbacks and callbacks[message.cmd]:
                    ret, result = callbacks[message.cmd](message)

                if ret:
                    response = SuccessResponse(result)
                else:
                    response = ErrorResponse(result)

                self.logger.info('Send: %s' % str(response))
                connection.sendall(str(response) + '\x00')

            except:
                self.logger.exception('Got an error while processing message from client, aborting')

                try:
                    connection.sendall(str(ErrorResponse("error while processing message from client")) + '\x00')
                except:
                    pass

    def start(self):
        if self.linkmon_enabled:
            self.link_thread.start()

        message_callbacks = dict()
        message_callbacks[StartApMessage.__cmd__] = self.on_start_ap_message
        message_callbacks[StopApMessage.__cmd__] = self.on_stop_ap_message
        message_callbacks[ListWifiMessage.__cmd__] = self.on_list_wifi_message
        message_callbacks[ConfigureWifiMessage.__cmd__] = self.on_configure_wifi_message
        message_callbacks[SelectWifiMessage.__cmd__] = self.on_select_wifi_message
        message_callbacks[StatusMessage.__cmd__] = self.on_status_message

        self._socket_monitor(self.server_address, callbacks=message_callbacks)

    def free_wifi(self):
        if self.wifi_free:
            subprocess.check_call(['nmcli', 'nm', 'wifi', 'off'])
            subprocess.check_call(['rfkill', 'unblock', 'wlan'])

    def start_ap(self):
        # do a last scan before we bring up the ap
        self.wifi_scan()

        # bring up the ap
        self.free_wifi()
        self.access_point.activate()
        self.logger.info("Started up AP")

        # make sure multicast addresses can be routed on the AP
        subprocess.check_call(['/sbin/ip', 'route', 'add', '224.0.0.0/4', 'dev', self.wifi_if])
        subprocess.check_call(['/sbin/ip', 'route', 'add', '239.255.255.250', 'dev', self.wifi_if])

        return True

    def stop_ap(self):
        self.free_wifi()
        self.access_point.deactivate()
        self.logger.info("Stopped AP")

        # make sure multicast addresses can be routed on the AP
        subprocess.check_call(['/sbin/ip', 'route', 'del', '224.0.0.0/4', 'dev', self.wifi_if])
        subprocess.check_call(['/sbin/ip', 'route', 'del', '239.255.255.250', 'dev', self.wifi_if])

        return True

    def wifi_scan(self):
        if self.access_point.is_running():
            raise RuntimeError("Can't scan for wifi cells when in ap mode")

        self.free_wifi()
        subprocess.check_call(['ifconfig', self.wifi_if, 'up'])

        self.cells = wifi.Cell.all(self.wifi_if)

        return self.__class__.convert_cells(self.cells)

    def find_cell(self, ssid, force=False):
        if not self.cells:
            if not force:
                return None

            if self.access_point.is_running():
                # ap activation includes explicit call to wifi_scan
                self.stop_ap()
                self.start_ap()
            else:
                self.wifi_scan()

        try:
            return list(filter(lambda x: x.ssid == ssid, self.cells))[0]
        except IndexError:
            return None

    def start_wifi(self):
        self.logger.debug("Connecting to wifi %s..." % self.wifi_connection_ssid)

        restart_ap = False
        if self.access_point.is_running():
            restart_ap = True
            self.access_point.deactivate()

        self.free_wifi()

        from wifi.scheme import ConnectionError

        try:
            self.wifi_connection.activate()
            self.logger.info("Connected to wifi %s" % self.wifi_connection_ssid)
            return True

        except ConnectionError:
            self.wifi_available = False
            self.logger.warn("Could not connect to wifi %s" % self.wifi_connection_ssid)
            try:
                self.wifi_connection.deactivate()

                if restart_ap:
                    self.access_point.activate()
            except:
                self.logger.warn("Could not deactivate wifi connection again, that's odd")
            return False

    def on_start_ap_message(self, message):
        if self.access_point is None:
            return False, 'access point is None'

        if self.access_point.is_running():
            return True, 'access point is already running'

        self.logger.debug("Starting ap...")
        self.start_ap()
        return True, 'started ap'

    def on_stop_ap_message(self, message):
        if self.access_point is None:
            return False, 'access point is None'

        self.logger.debug("Stopping ap...")

        if not self.access_point.is_running():
            return True, 'access point is not running'

        self.stop_ap()
        return True, 'stopped ap'

    def on_list_wifi_message(self, message):
        self.logger.debug("Listing available wifi cells...")

        if self.access_point.is_running():
            if self.cells:
                return True, self.__class__.convert_cells(self.cells)
            elif not message.force:
                return False, 'access point is running, cannot scan for wifis (use force option)'

            # cell list is refreshed upon start of ap
            self.stop_ap()
            self.start_ap()
        else:
            # we have to refresh it manually
            self.wifi_scan()

        return True, self.__class__.convert_cells(self.cells)

    def on_configure_wifi_message(self, message):

        self.logger.debug("Configuring wifi: %r..." % message)

        cell = self.find_cell(message.ssid, force=message.force)
        if cell is None:
            return False, 'could not find wifi cell with ssid %s' % message.ssid

        # if we reached this point, we got a cell, so let's save the config
        if self.wifi_connection:
            self.wifi_connection.delete()

        self.wifi_connection = self.Scheme.for_cell(self.wifi_if, self.wifi_name, cell, passkey=message.psk)
        self.wifi_connection.save(allow_overwrite=True)

        self.wifi_available = True
        self.logger.info("Saved configuration for wifi %s" % message.ssid)
        return True, 'configured wifi as "witbox_wifi"'

    def on_select_wifi_message(self, message):
        if self.wifi_connection is None:
            return False, 'wifi is not yet configured'

        if self.start_wifi():
            return True, 'connected to wifi'
        else:
            return False, 'could not connect'

    def on_status_message(self, message):
        current_ssid, current_address = self.current_wifi

        wifi = wired = ap = False
        if self.wifi_if in self.last_reachable_devs and not self.access_point.is_running() and current_ssid:
            wifi = True
        elif self.access_point.is_running():
            ap = True
        if self.wired_if in self.last_reachable_devs:
            wired = True

        return True, dict(
            link=self.last_link,
            devs=self.last_reachable_devs,
            connections=dict(
                wifi=wifi,
                ap=ap,
                wired=wired,
            ),
            wifi=dict(
                current_ssid=current_ssid,
                current_address=current_address,
                valid_config=self.wifi_available
            )
        )

    def on_link_change(self, former_link, current_link, current_devs):
        self.last_link = current_link
        self.last_reachable_devs = tuple(current_devs)

        access_point_running = self.access_point.is_running()
        if current_link or access_point_running:
            if current_link and not former_link and not access_point_running:
                self.logger.debug("Link restored!")
            self.link_down_count = 0
            return

        if self.link_down_count < self.linkmon_maxdown:
            self.logger.debug("Link down since %d retries" % self.link_down_count)
            self.link_down_count += 1
            return

        if self.wifi_connection is not None:
            self.logger.info("Link down, got a configured wifi connection, trying that")
            if self.start_wifi():
                return

        self.logger.info("Link still down, starting access point")
        self.start_ap()

    @property
    def wifi_connection_ssid(self):
        ssid = None
        for key in ("wpa-ssid", "wireless-essid"):
            if key in self.wifi_connection.options:
                ssid = self.wifi_connection.options[key]
        return ssid

    @property
    def current_wifi(self):
        iwconfig_output = subprocess.check_output(["/sbin/iwconfig", self.wifi_if])

        m = iwconfig_re.search(iwconfig_output)
        if not m:
            return None, None

        return m.group('ssid'), m.group('address')


def start_server(config):
    kwargs = dict(
        server_address=config["socket"],
        wifi_if=config["interfaces"]["wifi"],
        wired_if=config["interfaces"]["wired"],
        linkmon_enabled=config["link_monitor"]["enabled"],
        linkmon_maxdown=config["link_monitor"]["max_link_down"],
        linkmon_interval=config["link_monitor"]["interval"],
        ap_driver=config["ap"]["driver"],
        ap_ssid=config["ap"]["ssid"],
        ap_psk=config["ap"]["psk"],
        ap_name=config["ap"]["name"],
        ap_channel=config["ap"]["channel"],
        ap_ip=config["ap"]["ip"],
        ap_network=config["ap"]["network"],
        ap_range=config["ap"]["range"],
        ap_forwarding=config["ap"]["forwarding_to_wired"],
        ap_domain=config["ap"]["domain"],
        wifi_name=config["wifi"]["name"],
        wifi_free=config["wifi"]["free"],
        path_hostapd=config["paths"]["hostapd"],
        path_hostapd_conf=config["paths"]["hostapd_conf"],
        path_dnsmasq=config["paths"]["dnsmasq"],
        path_dnsmasq_conf=config["paths"]["dnsmasq_conf"],
        path_interfaces=config["paths"]["interfaces"]
    )
    s = Server(**kwargs)
    s.start()


def server():
    parser = argparse.ArgumentParser(parents=[common_arguments])

    def valid_ip(arg):
        arg = arg.strip()
        try:
            arg = netaddr.IPAddress(arg)
        except netaddr.AddrFormatError as e:
            raise argparse.ArgumentTypeError("%s is not a valid IP address: %s" % (arg, e.message))
        return arg

    def valid_network(arg):
        arg = arg.strip()
        try:
            arg = netaddr.IPNetwork(arg)
        except netaddr.AddrFormatError as e:
            raise argparse.ArgumentTypeError("%s is not a valid IP network address: %s" % (arg, e.message))
        return arg

    def dhcp_range(arg):
        split_arg = map(lambda x: x.strip(), arg.split(","))
        if len(split_arg) != 2:
            raise argparse.ArgumentTypeError("%s is not a valid DHCP range, please provide a comma separated list of the start and end IP" % arg)

        start, end = split_arg
        valid_ip(start)
        valid_ip(end)

        return start, end

    parser.add_argument("-F", "--foreground", action="store_true", help="Run in foreground instead of as daemon")
    parser.add_argument("-p", "--pid", default="/var/run/netconnectd.pid", help="Pidfile to use for demonizing, defaults to /var/run/netconnectd.pid")
    parser.add_argument("-d", "--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="Disable console output")
    parser.add_argument("-v", "--version", action="store_true", help="Display version information and exit")
    parser.add_argument("--logfile", default="/var/log/netconnectd.log", help="Location of logfile, defaults to /var/log/netconnectd.log")
    parser.add_argument("--interface-wifi", help="Wifi interface")
    parser.add_argument("--interface-wired", help="Wired interface")
    parser.add_argument("--linkmon-disabled", action="store_true", help="Disable link monitor")
    parser.add_argument("--linkmon-maxdown", type=int, default=3, help="Maximum number of link down detections until link monitor starts up AP, defaults to 3")
    parser.add_argument("--linkmon-interval", type=int, default=10, help="Interval of link monitor, defaults to 10")
    parser.add_argument("--ap-name", help="Name to assign to AP config, defaults to 'netconnectd_ap', you mostly won't have to set this")
    parser.add_argument("--ap-driver", help="The driver to use for the hostapd, defaults to nl80211")
    parser.add_argument("--ap-ssid", help="SSID of the AP wifi")
    parser.add_argument("--ap-psk", help="Passphrase with which to secure the AP wifi, defaults to creation of an unsecured wifi")
    parser.add_argument("--ap-channel", type=int, default=3, help="Channel on which to setup AP, defaults to 3")
    parser.add_argument("--ap-ip", type=valid_ip, help="IP of AP host in newly created network, defaults to '10.250.250.1'")
    parser.add_argument("--ap-network", type=valid_network, help="Network address (CIDR4) of network to create on AP, defaults to '10.250.250.0/24'")
    parser.add_argument("--ap-range", type=dhcp_range, help="Range of IPs to handout via DHPC on AP, comma-separated, defaults to '10.250.250.100,10.250.250.200'")
    parser.add_argument("--ap-domain", help="Domain to create on AP, disabled by default")
    parser.add_argument("--ap-forwarding", action="store_true", help="Enable forwarding from AP to wired connection, disabled by default")
    parser.add_argument("--wifi-name", help="Internal name to assign to Wifi config, defaults to 'netconnectd_wifi', you mostly won't have to set this")
    parser.add_argument("--wifi-free", action="store_true", help="Whether the wifi has to be freed from network manager before every configuration attempt, defaults to false")
    parser.add_argument("--path-hostapd", help="Path to hostapd executable, defaults to /usr/sbin/hostapd")
    parser.add_argument("--path-hostapd-conf", help="Path to hostapd configuration folder, defaults to /etc/hostapd/conf.d")
    parser.add_argument("--path-dnsmasq", help="Path to dnsmasq executable, defaults to /usr/sbin/dnsmasq")
    parser.add_argument("--path-dnsmasq-conf", help="Path to dnsmasq configuration folder, defaults to /etc/dnsmasq.conf.d")
    parser.add_argument("--path-interfaces", help="Path to interfaces configuration file, defaults to /etc/network/interfaces")
    parser.add_argument("--daemon", choices=["stop", "status"], help="Control the netconnectd daemon, supported arguments are 'stop' and 'status'.")

    args = parser.parse_args()

    if args.version:
        from ._version import get_versions
        import sys
        print("Version: %s" % get_versions()["version"])
        sys.exit(0)

    if args.daemon:
        import os
        import sys
        from .daemon import Daemon

        if args.daemon == "stop":
            # stop the daemon
            daemon = Daemon(pidfile=args.pid)
            daemon.stop()
            sys.exit(0)
        elif args.daemon == "status":
            # report the status of the daemon
            if os.path.exists(args.pid):
                with open(args.pid, "r") as f:
                    pid = f.readline().strip()

                if pid:
                    if os.path.exists(os.path.join("/proc", pid)):
                        print("Running (Pid %s)" % pid)
                        sys.exit(0)
            print ("Not running")
            sys.exit(0)

    # configure logging
    logging_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    logging.basicConfig(format=logging_format, filename=args.logfile, level=logging.DEBUG if args.debug else logging.INFO)
    if not args.quiet:
        console_handler = logging.StreamHandler()
        console_handler.formatter = logging.Formatter(fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        console_handler.level = logging.DEBUG if args.debug else logging.INFO
        logging.getLogger('').addHandler(console_handler)

    import copy
    config = copy.deepcopy(default_config)

    configfile = args.config
    if not configfile:
        configfile = "/etc/netconnectd.yaml"

    import os
    if os.path.exists(configfile):
        try:
            config = parse_configfile(configfile)
        except InvalidConfig as e:
            parser.error("Invalid configuration file: " + e.message)

    if args.address:
        config["socket"] = args.address

    if args.interface_wifi:
        config["interfaces"]["wifi"] = args.interface_wifi
    if args.interface_wired:
        config["interfaces"]["wired"] = args.interface_wired

    if args.linkmon_disabled:
        config["link_monitor"]["enabled"] = False
    else:
        if args.linkmon_maxdown:
            config["link_monitor"]["max_link_down"] = args.linkmon_maxdown
        if args.linkmon_interval:
            config["link_monitor"]["interval"] = args.linkmon_interval

    if args.ap_name:
        config["ap"]["name"] = args.ap_name
    if args.ap_driver:
        config["ap"]["driver"] = args.ap_driver
    if args.ap_ssid:
        config["ap"]["ssid"] = args.ap_ssid
    if args.ap_psk:
        config["ap"]["psk"] = args.ap_psk
    if args.ap_channel:
        config["ap"]["channel"] = args.ap_channel
    if args.ap_ip:
        config["ap"]["ip"] = args.ap_ip
    if args.ap_network:
        config["ap"]["network"] = args.ap_network
    if args.ap_range:
        config["ap"]["range"] = args.ap_range
    if args.ap_domain:
        config["ap"]["domain"] = args.ap_domain
    if args.ap_forwarding:
        config["ap"]["forward_to_wired"] = True

    if args.wifi_name:
        config["wifi"]["name"] = args.wifi_name
    if args.wifi_free:
        config["wifi"]["free"] = True

    if args.path_hostapd:
        config["paths"]["hostapd"] = args.path_hostapd
    if args.path_hostapd_conf:
        config["paths"]["hostapd_conf"] = args.path_hostapd_conf
    if args.path_dnsmasq:
        config["paths"]["dnsmasq"] = args.path_dnsmasq
    if args.path_dnsmasq_conf:
        config["paths"]["dnsmasq_conf"] = args.path_dnsmasq_conf
    if args.path_interfaces:
        config["paths"]["interfaces"] = args.path_interfaces

    # validate command line
    if not config["socket"]:
        parser.error("Socket address is missing, supply with either --address or via config file")
    if not config["interfaces"]["wifi"]:
        parser.error("Wifi interface is missing, supply with either --interface-wifi or via config file")
    if not config["ap"]["ssid"]:
        parser.error("AP SSID is missing, supply with either --ap-ssid or via config file")

    if args.foreground:
        # start directly instead of as daemon
        start_server(config)

    else:
        # start as daemon
        from .daemon import Daemon

        class ServerDaemon(Daemon):
            def run(self):
                start_server(config)

        daemon = ServerDaemon(pidfile=args.pid, umask=002)
        daemon.start()

if __name__ == '__main__':
    server()