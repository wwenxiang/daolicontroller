import logging

from ryu import cfg
from ryu.lib import addrconv
from ryu.lib.packet import arp
from ryu.lib.packet import ethernet
from ryu.lib.packet import packet
from ryu.ofproto import ether


from daolicontroller.lib.base import PacketBase


CONF = cfg.CONF
LOG = logging.getLogger(__name__)

BROADCAST = 'ff:ff:ff:ff:ff:ff'


class PacketARP(PacketBase):
    priority = 1

    def _redirect(self, dp, inport, outport, **kwargs):
        kwargs['eth_type'] = ether.ETH_TYPE_ARP
        super(PacketARP, self)._redirect(dp, inport, outport, **kwargs)

    def init_flow(self, dp, gateway):
        int_port = self.port_get(dp, gateway['IntDev'])
        if not int_port:
            return False

        # broadcast
        self._redirect(dp, int_port.port_no, dp.ofproto.OFPP_LOCAL,
                       eth_dst=BROADCAST)
        self._redirect(dp, int_port.port_no, dp.ofproto.OFPP_LOCAL,
                       eth_dst=int_port.hw_addr)

        self._redirect(dp, int_port.port_no, dp.ofproto.OFPP_LOCAL,
                       arp_tpa=gateway['IntIP'])
        self._redirect(dp, dp.ofproto.OFPP_LOCAL, int_port.port_no,
                       arp_spa=gateway['IntIP'])

    def arp_response(self, msg, dp, in_port, pkt_ether, pkt_arp, address):
        ofp, ofp_parser, ofp_set, ofp_out = self.ofp_get(dp)
        pack = packet.Packet()
        pack.add_protocol(ethernet.ethernet(
            ethertype=pkt_ether.ethertype,
            dst=pkt_ether.src,
            src=address))
        pack.add_protocol(arp.arp(
            opcode=arp.ARP_REPLY,
            src_mac=address,
            src_ip=pkt_arp.dst_ip,
            dst_mac=pkt_arp.src_mac,
            dst_ip=pkt_arp.src_ip))
        pack.serialize()
        msg.buffer_id = ofp.OFP_NO_BUFFER
        msg.data = pack.data
        actions = [ofp_out(in_port)]
        self.packet_out(msg, dp, actions, in_port=ofp.OFPP_CONTROLLER)

    def arp(self, msg, dp, in_port, pkt_ether, pkt_arp):
        def wrap(address):
            self.arp_response(msg, dp, in_port, pkt_ether, pkt_arp, address)
        return wrap

    def run(self, msg, pkt_ether, pkt_arp, gateway, **kwargs):
        dp = msg.datapath
        in_port = msg.match['in_port']
        ofp, ofp_parser, ofp_set, ofp_out = self.ofp_get(dp)

        if in_port == ofp.OFPP_LOCAL:
            return False

        funcarp = self.arp(msg, dp, in_port, pkt_ether, pkt_arp)

        dst_ip = pkt_arp.dst_ip
        src = self.get(pkt_arp.src_mac)
        dst = self.get(dst_ip)

        if dst:
            funcarp(dst['MacAddress'])
        elif src is not None:
            gwport = self.port_get(dp, id=src['NetworkId'])
            if not gwport:
                return False

            funcarp(gwport.hw_addr)
