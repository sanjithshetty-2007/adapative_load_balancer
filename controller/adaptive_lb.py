#!/usr/bin/env python3
"""
POX controller module: SDN load balancer for TCP servers.

Clients connect to one virtual IP (VIP). For every new TCP connection to
the VIP the controller picks a backend server and installs two OpenFlow
rules in the switch:

  forward: client -> VIP:8080      becomes  client -> server:8080
  reverse: server:8080 -> client   becomes  VIP:8080 -> client

All other traffic is forwarded with normal L2 learning.

Monitoring (used by adaptive mode, logged in both modes):
  * server load    - each server sends "LOAD ..." lines over a TCP socket
                     to this controller (port 9000) every 0.5 s
  * network load   - port statistics are requested from the switch every
                     second and turned into link utilisation per server
  * failures       - report connection closed, no report for 2 s
                     (heartbeat), or the server's switch port going down

Run (from the pox directory, with this folder on PYTHONPATH):
  ./pox.py openflow.of_01 --port=6633 adaptive_lb --mode=adaptive
  ./pox.py openflow.of_01 --port=6633 adaptive_lb --mode=rr
"""

import json
import os
import socket
import sys
import threading
import time

from pox.core import core
import pox.openflow.libopenflow_01 as of
from pox.lib.addresses import EthAddr, IPAddr
from pox.lib.packet.arp import arp
from pox.lib.packet.ethernet import ethernet
from pox.lib.recoco import Timer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lb_core import Pool, parse_load_line  # noqa: E402


log = core.getLogger()

LB_PRIORITY = 1000
L2_PRIORITY = 10
LB_IDLE_TIMEOUT = 10
L2_IDLE_TIMEOUT = 10


class AdaptiveLB(object):

    def __init__(self, mode, vip, vip_mac, servers, service_port,
                 report_port, stats_interval, link_mbps, events_path,
                 pool_args):

        self.mode = mode
        self.vip = IPAddr(vip)
        self.vip_mac = EthAddr(vip_mac)
        self.service_port = service_port
        self.report_port = report_port
        self.stats_interval = stats_interval
        self.link_bps = link_mbps * 1e6

        self.pool = Pool(servers, mode=mode, **pool_args)

        self.connection = None
        self.mac_to_port = {}
        self.ip_table = {}        # IPAddr -> (EthAddr, switch port)
        self.conns = {}           # (client ip, client port) -> [server, last seen]
        self.last_port_stats = {} # port -> (time, tx_bytes, rx_bytes)

        self.events_lock = threading.Lock()
        self.events_file = open(events_path, "a") if events_path else None

        core.openflow.addListeners(self)

        listener = threading.Thread(target=self._report_server, daemon=True)
        listener.start()

        self._log({"ev": "controller_start", "mode": mode, "vip": vip,
                   "servers": servers})

        log.info("Adaptive LB started: mode=%s vip=%s servers=%s",
                 mode, vip, servers)

    # ------------------------------------------------------------ logging

    def _log(self, record):

        record = dict(record)
        record["t"] = round(time.time(), 4)

        if self.events_file:
            with self.events_lock:
                self.events_file.write(json.dumps(record) + "\n")
                self.events_file.flush()

    def _handle_pool_events(self, events):
        """Log health/overload events; in adaptive mode also act on them."""

        for e in events:

            self._log(e)
            log.warning("%s %s (%s)", e["server"], e["ev"].upper(),
                        e.get("reason", ""))

            if e["ev"] == "server_down" and self.mode == "adaptive":
                self._delete_server_flows(e["server"])

    # ------------------------------------------------------------ load reports

    def _report_server(self):

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.report_port))
        srv.listen(32)

        log.info("Listening for load reports on TCP port %d",
                 self.report_port)

        while True:

            conn, addr = srv.accept()
            t = threading.Thread(target=self._report_conn,
                                 args=(conn, addr), daemon=True)
            t.start()

    def _report_conn(self, conn, addr):

        name = None

        try:

            reader = conn.makefile("r")

            for line in reader:

                parsed = parse_load_line(line)

                if parsed is None:
                    continue

                name, fields = parsed

                record = {"ev": "load", "server": name}
                record.update(fields)
                self._log(record)

                events = self.pool.on_report(name, fields)

                if events:
                    core.callLater(self._handle_pool_events, events)

        except OSError:
            pass

        finally:

            conn.close()

            if name is not None:

                events = self.pool.on_report_closed(name)

                if events:
                    core.callLater(self._handle_pool_events, events)

    # ------------------------------------------------------------ OpenFlow

    def _handle_ConnectionUp(self, event):

        self.connection = event.connection
        self._log({"ev": "switch_up", "dpid": str(event.dpid)})
        log.info("Switch %s connected", event.dpid)

        if not getattr(self, "_timer", None):
            self._timer = Timer(self.stats_interval, self._periodic,
                                recurring=True)

    def _periodic(self):

        if self.connection is None:
            return

        self.connection.send(
            of.ofp_stats_request(body=of.ofp_port_stats_request()))

        events = self.pool.check_timeouts()

        if events:
            self._handle_pool_events(events)

        # Forget connections that have been quiet for a long time.
        now = time.time()

        for key, value in list(self.conns.items()):
            if now - value[1] > 60:
                del self.conns[key]

    def _server_port(self, server):

        entry = self.ip_table.get(IPAddr(server.ip))
        return entry[1] if entry else None

    def _handle_PortStatsReceived(self, event):

        now = time.time()
        port_to_server = {}

        for s in self.pool.servers:
            p = self._server_port(s)
            if p is not None:
                port_to_server[p] = s

        snapshot = {}
        events = []

        for st in event.stats:

            prev = self.last_port_stats.get(st.port_no)
            self.last_port_stats[st.port_no] = (now, st.tx_bytes, st.rx_bytes)

            s = port_to_server.get(st.port_no)

            if prev is None or s is None:
                continue

            dt = now - prev[0]

            if dt <= 0:
                continue

            tx_bps = (st.tx_bytes - prev[1]) * 8 / dt
            rx_bps = (st.rx_bytes - prev[2]) * 8 / dt
            util = max(tx_bps, rx_bps) / self.link_bps

            events += self.pool.on_port_util(s.name, util)

            snapshot[s.name] = {"to_server_mbps": round(tx_bps / 1e6, 3),
                                "from_server_mbps": round(rx_bps / 1e6, 3),
                                "util": round(util, 3)}

        if snapshot:
            self._log({"ev": "port_stats", "ports": snapshot})
            self._log({"ev": "state", "servers": self.pool.snapshot()})

        if events:
            self._handle_pool_events(events)

    def _handle_PortStatus(self, event):

        desc = event.ofp.desc
        is_up = not (desc.state & of.OFPPS_LINK_DOWN) \
            and not (desc.config & of.OFPPC_PORT_DOWN) \
            and not event.deleted

        self._log({"ev": "port_status", "port": desc.port_no, "up": is_up})

        for s in self.pool.servers:
            if self._server_port(s) == desc.port_no:
                events = self.pool.on_port_status(s.name, is_up)
                if events:
                    self._handle_pool_events(events)

    def _handle_PacketIn(self, event):

        packet = event.parsed

        if not packet.parsed or packet.type == ethernet.LLDP_TYPE:
            return

        inport = event.port
        self.mac_to_port[packet.src] = inport

        a = packet.find("arp")
        ip = packet.find("ipv4")

        if a is not None and a.protosrc != self.vip:
            self.ip_table[a.protosrc] = (packet.src, inport)

        if ip is not None and ip.srcip != self.vip:
            self.ip_table[ip.srcip] = (packet.src, inport)

        # ARP request for the virtual IP: answer it ourselves.
        if a is not None and a.opcode == arp.REQUEST and a.protodst == self.vip:
            self._answer_vip_arp(event, packet, a)
            return

        if ip is not None and ip.dstip == self.vip:

            t = packet.find("tcp")

            if t is not None and t.dstport == self.service_port:
                self._balance(event, packet, ip, t)

            # Anything else sent to the VIP is dropped.
            return

        self._l2_forward(event, packet)

    # ------------------------------------------------------------ helpers

    def _answer_vip_arp(self, event, packet, a):

        r = arp()
        r.hwtype = a.hwtype
        r.prototype = a.prototype
        r.hwlen = a.hwlen
        r.protolen = a.protolen
        r.opcode = arp.REPLY
        r.hwdst = a.hwsrc
        r.protodst = a.protosrc
        r.protosrc = self.vip
        r.hwsrc = self.vip_mac

        e = ethernet(type=packet.type, src=self.vip_mac, dst=a.hwsrc)
        e.set_payload(r)

        msg = of.ofp_packet_out()
        msg.data = e.pack()
        msg.actions.append(of.ofp_action_output(port=of.OFPP_IN_PORT))
        msg.in_port = event.port
        event.connection.send(msg)

    def _l2_forward(self, event, packet):

        if packet.dst.is_multicast or packet.dst not in self.mac_to_port:

            msg = of.ofp_packet_out(data=event.ofp)
            msg.actions.append(of.ofp_action_output(port=of.OFPP_FLOOD))
            msg.in_port = event.port
            event.connection.send(msg)
            return

        out_port = self.mac_to_port[packet.dst]

        if out_port == event.port:
            return

        msg = of.ofp_flow_mod()
        msg.match = of.ofp_match(dl_dst=packet.dst)
        msg.priority = L2_PRIORITY
        msg.idle_timeout = L2_IDLE_TIMEOUT
        msg.actions.append(of.ofp_action_output(port=out_port))
        msg.data = event.ofp
        event.connection.send(msg)

    def _balance(self, event, packet, ip, t):

        now = time.time()
        key = (str(ip.srcip), t.srcport)
        entry = self.conns.get(key)
        server = None

        if entry is not None:

            server = self.pool.by_name[entry[0]]
            entry[1] = now

            if self.mode == "adaptive" and not server.up:
                server = None

        if server is None:

            server, info = self.pool.choose()

            if server is None:
                self._log({"ev": "no_server", "client": key[0],
                           "cport": key[1], "info": info})
                return

            self.conns[key] = [server.name, now]

            record = {"ev": "assign", "client": key[0], "cport": key[1],
                      "server": server.name}
            record.update(info)
            self._log(record)

        location = self.ip_table.get(IPAddr(server.ip))

        if location is None:
            self._log({"ev": "unknown_server_location",
                       "server": server.name})
            return

        server_mac, server_port = location
        server_ip = IPAddr(server.ip)

        fwd_actions = [of.ofp_action_dl_addr.set_dst(server_mac),
                       of.ofp_action_nw_addr.set_dst(server_ip),
                       of.ofp_action_output(port=server_port)]

        # Reverse rule first, so the server's reply never misses it.
        rev = of.ofp_flow_mod()
        rev.priority = LB_PRIORITY
        rev.idle_timeout = LB_IDLE_TIMEOUT
        rev.match = of.ofp_match(dl_type=0x0800, nw_proto=6,
                                 nw_src=server_ip, nw_dst=ip.srcip,
                                 tp_src=self.service_port, tp_dst=t.srcport)
        rev.actions = [of.ofp_action_dl_addr.set_src(self.vip_mac),
                       of.ofp_action_nw_addr.set_src(self.vip),
                       of.ofp_action_output(port=event.port)]
        event.connection.send(rev)

        fwd = of.ofp_flow_mod()
        fwd.priority = LB_PRIORITY
        fwd.idle_timeout = LB_IDLE_TIMEOUT
        fwd.match = of.ofp_match(dl_type=0x0800, nw_proto=6,
                                 nw_src=ip.srcip, nw_dst=self.vip,
                                 tp_src=t.srcport, tp_dst=self.service_port)
        fwd.actions = list(fwd_actions)
        event.connection.send(fwd)

        out = of.ofp_packet_out(data=event.ofp)
        out.actions = list(fwd_actions)
        out.in_port = event.port
        event.connection.send(out)

    def _delete_server_flows(self, name):
        """Remove the rules of a failed server so its clients get re-placed."""

        if self.connection is None:
            return

        server = self.pool.by_name[name]
        server_ip = IPAddr(server.ip)

        rev = of.ofp_flow_mod(command=of.OFPFC_DELETE)
        rev.match = of.ofp_match(dl_type=0x0800, nw_proto=6,
                                 nw_src=server_ip, tp_src=self.service_port)
        self.connection.send(rev)

        removed = 0

        for key, value in list(self.conns.items()):

            if value[0] != name:
                continue

            fwd = of.ofp_flow_mod(command=of.OFPFC_DELETE)
            fwd.match = of.ofp_match(dl_type=0x0800, nw_proto=6,
                                     nw_src=IPAddr(key[0]), nw_dst=self.vip,
                                     tp_src=key[1],
                                     tp_dst=self.service_port)
            self.connection.send(fwd)
            del self.conns[key]
            removed += 1

        self._log({"ev": "flows_deleted", "server": name,
                   "connections": removed})
        log.warning("%s FLOW RULES DELETED (%d connections will be re-placed)",
                    name, removed)


def _parse_servers(text):

    servers = []

    for item in text.split(","):
        name, ip = item.strip().split("=")
        servers.append((name, ip))

    return servers


def launch(mode="adaptive", vip="10.0.0.100", vip_mac="00:00:00:00:01:00",
           servers="h1=10.0.0.1,h2=10.0.0.2,h3=10.0.0.3",
           service_port=8080, report_port=9000, stats_interval=1.0,
           link_mbps=10, events="", overload_ms=250, overload_queue=2.0,
           overload_util=0.8, heartbeat=2.0):

    pool_args = {
        "overload_ms": float(overload_ms),
        "overload_queue": float(overload_queue),
        "overload_util": float(overload_util),
        "heartbeat_timeout": float(heartbeat),
    }

    core.registerNew(AdaptiveLB, mode, vip, vip_mac,
                     _parse_servers(servers), int(service_port),
                     int(report_port), float(stats_interval),
                     float(link_mbps), events, pool_args)
