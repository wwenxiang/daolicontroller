import logging

from ryu import cfg
from ryu.lib import dpid as dpid_lib
from ryu.ofproto import ether
from ryu.ofproto import inet

from daolicontroller import exception
from daolicontroller import utils
from daolicontroller.lib.base import PacketBase
from daolicontroller.lib.constants import CONNECTED, DISCONNECTED

CONF = cfg.CONF
LOG = logging.getLogger(__name__)

INFILTER = [2375]
OUTFILTER = [4001]


class PacketIPv4(PacketBase):
    priority = 1

    def _redirect(self, dp, inport, outport, **kwargs):
        kwargs['eth_type'] = ether.ETH_TYPE_IP
        super(PacketIPv4, self)._redirect(dp, inport, outport, **kwargs)

    def init_flow(self, dp, gateway):
        int_port = self.port_get(dp, gateway['IntDev'])
        if not int_port:
            return False

        # Add flow where is from local host.
        self._redirect(dp, dp.ofproto.OFPP_LOCAL, int_port.port_no,
                       ipv4_src=gateway['IntIP'])

        # Add icmp flow coming from outer.
        self._redirect(dp, int_port.port_no, dp.ofproto.OFPP_LOCAL,
                       ip_proto=inet.IPPROTO_ICMP, ipv4_dst=gateway['IntIP'])

        # Add initial port flow. eg: docker socket port, etcd port.
        for port in INFILTER:
            self._redirect(dp, int_port.port_no, dp.ofproto.OFPP_LOCAL,
                           ip_proto=inet.IPPROTO_TCP, ipv4_dst=gateway['IntIP'],
                           tcp_dst=port)

        for port in OUTFILTER:
            self._redirect(dp, int_port.port_no, dp.ofproto.OFPP_LOCAL,
                           ip_proto=inet.IPPROTO_TCP, ipv4_dst=gateway['IntIP'],
                           tcp_src=port)

    def filter(self, src, dst):
        peer = "%s:%s" % (src['Id'], dst['Id'])
        try:
            action = self.client.policy(peer)
            if action == CONNECTED:
                return True
            elif action == DISCONNECTED:
                return False
        except Exception:
            return False

        if src['NetworkName'] != dst['NetworkName']:
            # returns if group exists src and dst network
            if not self.client.group(src['NetworkName'], dst['NetworkName']):
                return False

        return True

    def _firewall(self, msg, dp, in_port, pkt_ether, pkt_ipv4, pkt_tp, fw, gateway):
        ofp, ofp_parser, ofp_set, ofp_out = self.ofp_get(dp)

        container = self.get(fw['Container'])
        if not container:
            raise exception.ContainerNotFound(container=fw['Container'])

        if pkt_ipv4.proto == inet.IPPROTO_TCP:
            input_key = ofp_set(tcp_dst=fw['ServicePort'])
            output_key = ofp_set(tcp_src=pkt_tp.dst_port)
            input_kwargs = {
                    'tcp_src': pkt_tp.src_port,
                    'tcp_dst': pkt_tp.dst_port,
            }
            output_kwargs = {
                    'tcp_src': fw['ServicePort'],
                    'tcp_dst': pkt_tp.src_port,
            }
            rinput_kwargs = {
                    'tcp_src': pkt_tp.src_port,
                    'tcp_dst': fw['ServicePort'],
            }
        else:
            input_key = ofp_set(udp_dst=fw['ServicePort'])
            output_key = ofp_set(udp_src=pkt_tp.dst_port)
            input_kwargs = {
                    'udp_src': pkt_tp.src_port,
                    'udp_dst': pkt_tp.dst_port,
            }
            output_kwargs = {
                    'udp_src': fw['ServicePort'],
                    'udp_dst': pkt_tp.src_port,
            }
            rinput_kwargs = {
                    'udp_src': pkt_tp.src_port,
                    'udp_dst': fw['ServicePort'],
            }

        input_match = ofp_parser.OFPMatch(
                in_port=in_port,
                eth_type=ether.ETH_TYPE_IP,
                ip_proto=pkt_ipv4.proto,
                ipv4_src=pkt_ipv4.src,
                ipv4_dst=pkt_ipv4.dst,
                **input_kwargs)

        output_actions = [
                ofp_set(eth_src=pkt_ether.dst),
                ofp_set(eth_dst=pkt_ether.src),
                ofp_set(ipv4_src=pkt_ipv4.dst),
                ofp_set(ipv4_dst=pkt_ipv4.src)]

        output_actions.append(output_key)
        output_actions.append(ofp_out(in_port))

        if gateway['DatapathID'] != container['DataPath']:
            cgateway = self.gateway_get(container['DataPath'])
            rdp = self.ryuapp.dps[dpid_lib.str_to_dpid(container['DataPath'])]
            rofp, rofp_parser, rofp_set, rofp_out = self.ofp_get(rdp)

            gwport = self.port_get(rdp, id=container['NetworkId'])
            cport = self.port_get(rdp, id=container['EndpointID'])
            if not cport or not gwport:
                raise exception.DevicePortNotFound()

            liport = self.port_get(rdp, gateway['IntDev'])
            riport = self.port_get(rdp, cgateway['IntDev'])

            input_actions = [
                    ofp_set(eth_src=gateway['IntDev']),
                    ofp_set(eth_dst=cgateway['IntDev']),
                    ofp_set(ipv4_dst=container['VIPAddress'])]

            input_actions.append(input_key)
            input_actions.append(ofp_out(liport))

            output_match = ofp_parser.OFPMatch(
                    in_port=liport.port_no,
                    eth_type=ether.ETH_TYPE_IP,
                    ip_proto=pkt_ipv4.proto,
                    ipv4_src=container['VIPAddress'],
                    ipv4_dst=pkt_ipv4.src,
                    **output_kwargs)

            remote_input_match = rofp_parser.OFPMatch(
                    in_port=riport.port_no,
                    eth_type=ether.ETH_TYPE_IP,
                    ip_proto=pkt_ipv4.proto,
                    ipv4_src=pkt_ipv4.src,
                    ipv4_dst=container['VIPAddress'],
                    **rinput_kwargs)

            remote_input_actions = [
                    rofp_set(eth_src=gwport.hw_addr),
                    rofp_set(eth_dst=container['MacAddress']),
                    rofp_set(ipv4_dst=container['IPAddress'])]

            remote_input_actions.append(rofp_out(cport.port_no))

            remote_output_match = rofp_parser.OFPMatch(
                in_port=cport.port_no,
                eth_type=ether.ETH_TYPE_IP,
                ip_proto=pkt_ipv4.proto,
                ipv4_src=container['IPAddress'],
                ipv4_dst=pkt_ipv4.src,
                **output_kwargs)

            remote_optput_actions = [
                    rofp_set(eth_src=cgateway['IntDev']),
                    rofp_set(eth_dst=gateway['IntDev']),
                    rofp_set(ipv4_src=container['VIPAddress'])]

            input_actions.append(ofp_out(riport.port_no))

            self.add_flow(rdp, remote_output_match, remote_output_actions)
            self.add_flow(rdp, remote_input_match, remote_input_actions)
            self.packet_out(msg, dp, remote_input_actions)
        else:
            gwport = self.port_get(dp, id=container['NetworkId'])
            cport = self.port_get(dp, id=container['EndpointID'])
            if not cport or not gwport:
                raise exception.DevicePortNotFound()

            input_actions = [
                    ofp_set(eth_src=gwport.hw_addr),
                    ofp_set(eth_dst=container['MacAddress']),
                    ofp_set(ipv4_dst=container['IPAddress'])]

            input_actions.append(input_key)
            input_actions.append(ofp_out(cport.port_no))

            output_match = ofp_parser.OFPMatch(
                    in_port=cport.port_no,
                    eth_type=ether.ETH_TYPE_IP,
                    ip_proto=pkt_ipv4.proto,
                    ipv4_src=container['IPAddress'],
                    ipv4_dst=pkt_ipv4.src,
                    **output_kwargs)

            self.add_flow(dp, output_match, output_actions)
            self.add_flow(dp, input_match, input_actions)
            self.packet_out(msg, dp, input_actions)

    def firewall(self, msg, dp, in_port, pkt_ether, pkt_ipv4, pkt_tp, gateway):
        ofp, ofp_parser, ofp_set, ofp_out = self.ofp_get(dp)

        port = self.port_get(dp, gateway['IntDev'])
        if not port:
            raise exception.DevicePortNotFound()

        if in_port == port.port_no or in_port == ofp.OFPP_LOCAL:
            if pkt_ipv4.proto == inet.IPPROTO_ICMP:
                return True

            if in_port == ofp.OFPP_LOCAL:
                outport =  port.port_no
            else:
                outport =  ofp.OFPP_LOCAL

            fw = self.client.firewall(gateway['DatapathID'], pkt_tp.dst_port)
            if fw:
                self._firewall(msg, dp, in_port, pkt_ether, pkt_ipv4, pkt_tp, fw, gateway)
            else:
                kwargs = {
                    'ipv4_src': pkt_ipv4.src,
                    'ipv4_dst': pkt_ipv4.dst,
                    'timeout': CONF.timeout}
                if pkt_ipv4.proto == inet.IPPROTO_TCP:
                    kwargs['tcp_src'] = pkt_tp.src_port
                    kwargs['tcp_dst'] = pkt_tp.dst_port
                else:
                    kwargs['udp_src'] = pkt_tp.src_port
                    kwargs['udp_dst'] = pkt_tp.dst_port

                actions = [ofp_parser.OFPActionOutput(outport)]
                self._redirect(dp, in_port, outport, ip_proto=pkt_ipv4.proto, **kwargs)
                self.packet_out(msg, dp, actions)
            return True

        return False

    def run(self, msg, pkt_ether, pkt_ipv4, pkt_tp, gateway, **kwargs):
        dp = msg.datapath
        in_port = msg.match['in_port']

        try:
            ret = self.firewall(msg, dp, in_port, pkt_ether,
                                pkt_ipv4, pkt_tp, gateway)
            if ret:
                return True
        except Exception:
            return False

        src = self.get(pkt_ether.src)
        if not src:
            return False

        dst = self.get(pkt_ipv4.dst)
        if not dst:
            self.public_flow(msg, dp, pkt_ether, pkt_ipv4, in_port, src)
            return True

        if not self.filter(src, dst):
            return False

        snode, dnode = src.get('Node'), dst.get('Node')
        if not snode or not dnode:
            snode = dnode = utils.gethostname()

        # the same node
        if snode == dnode:
            dst_port = self.port_get(dp, id=dst['EndpointID'])
            if not dst_port:
                return False

            ofp, ofp_parser, ofp_set, ofp_out = self.ofp_get(dp)

            if pkt_ether.dst != dst['MacAddress']:
                submac = pkt_ether.dst
            else:
                submac = None

            def local_flow(smac, dmac, sip, dip, iport, oport):
                match = ofp_parser.OFPMatch(
                        in_port=iport,
                        eth_type=ether.ETH_TYPE_IP,
                        eth_src=smac,
                        ipv4_src=sip,
                        ipv4_dst=dip)
                actions = ([ofp_set(eth_src=submac)]
                           if submac is not None else [])
                actions.extend([ofp_set(eth_dst=dmac), ofp_out(oport)])
                self.add_flow(dp, match, actions)

                return actions

            local_flow(dst['MacAddress'], src['MacAddress'],
                       pkt_ipv4.dst, pkt_ipv4.src,
                       dst_port.port_no, in_port)
            self.packet_out(msg, dp, local_flow(
                       src['MacAddress'], dst['MacAddress'],
                       pkt_ipv4.src, pkt_ipv4.dst,
                       in_port, dst_port.port_no))
        else:
            if not dst.get('DataPath'):
                LOG.info("target ovs could not be registered.")
                return False

            self.host_flow(msg, dp, in_port, pkt_ether, pkt_ipv4, gateway, src, dst)

    def host_flow(self, msg, dp, in_port, pkt_ether, pkt_ipv4, src_gateway, src, dst):
        ofp, ofp_parser, ofp_set, ofp_out = self.ofp_get(dp)
        liport = self.port_get(dp, src_gateway['IntDev'])

        rdp = self.ryuapp.dps[dpid_lib.str_to_dpid(dst['DataPath'])]

        dst_port = self.port_get(rdp, id=dst['EndpointID'])
        if not dst_port:
            return

        dst_gateway = self.gateway_get(dst['DataPath'])
        rofp, rofp_parser, rofp_set, rofp_out = self.ofp_get(rdp)
        riport = self.port_get(rdp, dst_gateway['IntDev'])

        output_local_match = ofp_parser.OFPMatch(
                in_port=in_port,
                eth_type=ether.ETH_TYPE_IP,
                eth_src=pkt_ether.src,
                ipv4_src=pkt_ipv4.src,
                ipv4_dst=pkt_ipv4.dst)

        output_local_actions = [
                ofp_set(eth_src=liport.hw_addr),
                ofp_set(eth_dst=riport.hw_addr),
                ofp_set(ipv4_src=src['VIPAddress']),
                ofp_set(ipv4_dst=dst['VIPAddress']),
                ofp_out(liport.port_no),
        ]

        input_remote_match = rofp_parser.OFPMatch(
                in_port=riport.port_no,
                eth_type=ether.ETH_TYPE_IP,
                eth_dst=riport.hw_addr,
                #ipv4_src=pkt_ipv4.src,
                #ipv4_dst=pkt_ipv4.dst)
                ipv4_src=src['VIPAddress'],
                ipv4_dst=dst['VIPAddress'])

        if pkt_ether.dst == dst['MacAddress']:
            dst_srcmac = pkt_ether.src
        else:
            gwport = self.port_get(rdp, id=dst['NetworkId'])
            dst_srcmac = gwport.hw_addr

        input_remote_actions = [
                rofp_set(eth_src=dst_srcmac),
                rofp_set(eth_dst=dst['MacAddress']),
                rofp_set(ipv4_src=pkt_ipv4.src),
                rofp_set(ipv4_dst=pkt_ipv4.dst),
                rofp_out(dst_port.port_no)]

        output_remote_match = rofp_parser.OFPMatch(
                in_port=dst_port.port_no,
                eth_type=ether.ETH_TYPE_IP,
                eth_src=dst['MacAddress'],
                ipv4_src=pkt_ipv4.dst,
                ipv4_dst=pkt_ipv4.src)

        output_remote_actions = [
                rofp_set(eth_src=riport.hw_addr),
                rofp_set(eth_dst=liport.hw_addr),
                rofp_set(ipv4_src=dst['VIPAddress']),
                rofp_set(ipv4_dst=src['VIPAddress']),
                ofp_out(riport.port_no),
        ]

        input_local_match = ofp_parser.OFPMatch(
                in_port=liport.port_no,
                eth_type=ether.ETH_TYPE_IP,
                eth_dst=liport.hw_addr,
                #ipv4_src=pkt_ipv4.dst,
                #ipv4_dst=pkt_ipv4.src)
                ipv4_src=dst['VIPAddress'],
                ipv4_dst=src['VIPAddress'])

        input_local_actions = [
                ofp_set(eth_src=pkt_ether.dst),
                ofp_set(eth_dst=pkt_ether.src),
                ofp_set(ipv4_src=pkt_ipv4.dst),
                ofp_set(ipv4_dst=pkt_ipv4.src),
                ofp_out(in_port),
        ]

        self.add_flow(rdp, input_remote_match, input_remote_actions)
        self.add_flow(rdp, output_remote_match, output_remote_actions)
        self.add_flow(dp, input_local_match, input_local_actions)
        self.add_flow(dp, output_local_match, output_local_actions)
        self.packet_out(msg, dp, output_local_actions)

    def public_flow(self, msg, dp, pkt_ether, pkt_ipv4, in_port, src):
        ofp, ofp_parser, ofp_set, ofp_out = self.ofp_get(dp)
        gwport = self.port_get(dp, id=src['NetworkId'])
        if not gwport:
            return

        output_match = ofp_parser.OFPMatch(
                in_port=in_port,
                eth_type=ether.ETH_TYPE_IP,
                eth_src=pkt_ether.src,
                ipv4_src=pkt_ipv4.src,
                ipv4_dst=pkt_ipv4.dst)

        output_actions = [ofp_out(gwport.port_no)]

        input_match = ofp_parser.OFPMatch(
                in_port=gwport.port_no,
                eth_type=ether.ETH_TYPE_IP,
                eth_src=gwport.hw_addr,
                ipv4_src=pkt_ipv4.dst,
                ipv4_dst=pkt_ipv4.src)

        input_actions = [
                ofp_set(eth_dst=pkt_ether.src),
                ofp_out(in_port),
        ]

        self.add_flow(dp, input_match, input_actions)
        self.add_flow(dp, output_match, output_actions)
        self.packet_out(msg, dp, output_actions)

    def flow_delete(self, sid, did):
        src = self.get(sid)
        dst = self.get(did)
        if not src or not dst:
            return 

        sdp_id = dpid_lib.str_to_dpid(src['DataPath'])
        ddp_id = dpid_lib.str_to_dpid(dst['DataPath'])

        if self.ryuapp.dps.has_key(sdp_id):
            dp = self.ryuapp.dps[sdp_id]
            ofp, ofp_parser, _, _ = self.ofp_get(dp)

            port = self.port_get(dp, id=src['EndpointID'])
            if port:
                match = ofp_parser.OFPMatch(
                        in_port=port.port_no,
                        eth_type=ether.ETH_TYPE_IP,
                        ipv4_src=src['IPAddress'],
                        ipv4_dst=dst['IPAddress'])

                self.delete_flow(dp, match)

        if self.ryuapp.dps.has_key(ddp_id):
            dp = self.ryuapp.dps[sdp_id]
            ofp, ofp_parser, _, _ = self.ofp_get(dp)

            port = self.port_get(dp, id=dst['EndpointID'])
            if port:
                match = ofp_parser.OFPMatch(
                        in_port=port.port_no,
                        eth_type=ether.ETH_TYPE_IP,
                        ipv4_src=dst['IPAddress'],
                        ipv4_dst=src['IPAddress'])

                self.delete_flow(dp, match)

    def remove_flow(self, container):
        dp_id = dpid_lib.str_to_dpid(container['DataPath'])
        if dp_id in self.ryuapp.dps:
            dp = self.ryuapp.dps[dp_id]
            ofp, ofp_parser, _, _ = self.ofp_get(dp)
            match = ofp_parser.OFPMatch(
                    eth_type=ether.ETH_TYPE_IP,
                    ipv4_src=container['IPAddress'])
            self.delete_flow(dp, match)

            match = ofp_parser.OFPMatch(
                    eth_type=ether.ETH_TYPE_IP,
                    ipv4_dst=container['IPAddress'])
            self.delete_flow(dp, match)

            match = ofp_parser.OFPMatch(
                    eth_type=ether.ETH_TYPE_IP,
                    ipv4_src=container['VIPAddress'])
            self.delete_flow(dp, match)

            match = ofp_parser.OFPMatch(
                    eth_type=ether.ETH_TYPE_IP,
                    ipv4_dst=container['VIPAddress'])
            self.delete_flow(dp, match)

