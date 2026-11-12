#!/usr/bin/env python3
"""
Mininet topology for the SDN adaptive load balancer.

                     POX controller (127.0.0.1:6633, out of band)
                                   |
   h4 h5 h6 h7  ---------------   s1   ---------------  h1 h2 h3
   (clients)                       |                    (TCP servers)
                                  ctl  10.0.0.254 (root namespace:
                                       servers send load reports here)

  h1 10.0.0.1  server        h4..h7 10.0.0.4-7  clients
  h2 10.0.0.2  server        VIP 10.0.0.100      (answered by controller)
  h3 10.0.0.3  server

Server links are 10 Mbit/s, client links 100 Mbit/s, so network load on a
server link can be created and measured.

Interactive use:
  sudo mn --custom topology/topology.py --topo adaptive --link tc \\
          --controller remote,ip=127.0.0.1,port=6633 --mac
"""

from mininet.node import Host
from mininet.topo import Topo


SERVERS = [("h1", "10.0.0.1"), ("h2", "10.0.0.2"), ("h3", "10.0.0.3")]
CLIENTS = [("h4", "10.0.0.4"), ("h5", "10.0.0.5"),
           ("h6", "10.0.0.6"), ("h7", "10.0.0.7")]
CONTROLLER_HOST = ("ctl", "10.0.0.254")
VIP = "10.0.0.100"

SERVER_BW = 10
CLIENT_BW = 100


class AdaptiveLoadBalancerTopo(Topo):

    def build(self, use_bw=True):

        switch = self.addSwitch("s1")

        # A short queue keeps a congested link from delaying packets for
        # seconds (which would also delay the servers' load reports).
        server_link = {"bw": SERVER_BW, "max_queue_size": 100} \
            if use_bw else {}
        client_link = {"bw": CLIENT_BW} if use_bw else {}

        # Servers first, so they get switch ports 1, 2, 3.
        for name, ip in SERVERS:
            host = self.addHost(name, ip=ip + "/24")
            self.addLink(host, switch, **server_link)

        for name, ip in CLIENTS:
            host = self.addHost(name, ip=ip + "/24")
            self.addLink(host, switch, **client_link)

        # A host in the root namespace, so processes inside Mininet can
        # reach the controller's load-report socket at 10.0.0.254:9000.
        name, ip = CONTROLLER_HOST
        ctl = self.addHost(name, cls=Host, inNamespace=False,
                           ip=ip + "/24")
        self.addLink(ctl, switch, **client_link)


topos = {
    "adaptive": lambda: AdaptiveLoadBalancerTopo()
}
