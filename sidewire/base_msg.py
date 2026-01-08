from aionetiface import *

class SigMsg():
    @staticmethod
    def load_addr(af, addr_buf, if_index):
        # Validate src address.
        addr = parse_node_addr(
            addr_buf
        )

        # Parse af for punching.
        af = to_n(af)
        af = i_to_af(af) 

        # Validate src if index.
        if if_index not in addr[af]:
            raise Exception(fstr("bad if_i {0}", (if_index,)))
        
        return af, addr
    
    class Cipher():
        def __init__(self, vk):
            self.vk = vk

        def to_dict(self):
            return {
                "vk": to_h(self.vk)
            }
        
        @staticmethod
        def from_dict(d):
            vk = d.get("vk", "")
            vk = h_to_b(vk)
            return SigMsg.Cipher(vk)

    # Information about the message sender.
    class Meta():
        def __init__(self, ttl=0, pipe_id=b"", af=IP4, src_buf=b"", src_index=0, route_type=EXT_BIND, same_machine=False, plugin_name=None):
            # Load meta data about message.
            self.ttl = to_n(ttl)
            self.pipe_id = to_s(pipe_id)
            self.src_buf = to_s(src_buf)
            self.src_index = to_n(src_index)
            self.af = af
            self.same_machine = False
            self.route_type = route_type
            self.plugin_name = plugin_name
            if src_buf:
                self.load_src_addr()

        def load_src_addr(self):
            # Parse src_buf to addr.
            self.af, self.src = \
            SigMsg.load_addr(
                self.af,
                self.src_buf,
                self.src_index,
            )

            # Reference to the network info.
            info = self.src[self.af]
            self.src_info = info[self.src_index]

        def to_dict(self):
            return {
                "ttl": self.ttl,
                "pipe_id": self.pipe_id,
                "af": int(self.af),
                "src_buf": self.src_buf,
                "src_index": self.src_index,
                "route_type": self.route_type,
                "same_machine": self.same_machine,
                "plugin_name": self.plugin_name,
            }
        
        @staticmethod
        def from_dict(d):
            return SigMsg.Meta(
                d.get("ttl", 0),
                d.get("pipe_id", b""),
                d.get("af", IP4),
                d.get("src_buf", b""),
                d.get("src_index", 0),
                d.get("route_type", EXT_BIND),
                d.get("same_machine", False),
                d.get("plugin_name", None),
            )

    # The destination node for this msg.
    class Routing():
        def __init__(self, af=IP4, dest_buf=b"", dest_index=0):
            self.dest_buf = to_s(dest_buf)
            self.dest_index = to_n(dest_index)
            self.af = af
            if dest_buf:
                self.set_cur_dest(dest_buf)
                self.cur_dest_buf = None # set later.

        def load_if_extra(self, nics):
            if_index = self.dest_index
            self.interface = nics[if_index]

        """
        Peers usually have dynamic addresses.
        The parsed dest will reflect the updated /
        current address of the node that receives this.
        """
        def set_cur_dest(self, cur_dest_buf):
            self.cur_dest_buf = to_s(cur_dest_buf)
            self.af, self.dest = SigMsg.load_addr(
                self.af,
                cur_dest_buf,
                self.dest_index,
            )

            # Reference to the network info.
            info = self.dest[self.af]
            self.dest_info = info[self.dest_index]

        def to_dict(self):
            return {
                "af": int(self.af),
                "dest_buf": self.dest_buf,
                "dest_index": self.dest_index,
            }
        
        @staticmethod
        def from_dict(d):
            return SigMsg.Routing(
                d.get("af", IP4),
                d.get("dest_buf", b""),
                d.get("dest_index", 0),
            )

    # Abstract kinda feel.
    class Payload():
        def __init__(self):
            pass

        def to_dict(self):
            return {}
        
        @staticmethod
        def from_dict(d):
            return SigMsg.Payload()

    def __init__(self, data, enum):
        self.meta = SigMsg.Meta.from_dict(
            data.get("meta", {})
        )

        self.routing = SigMsg.Routing.from_dict(
            data.get("routing", {})
        )

        self.payload = self.Payload.from_dict(
            data.get("payload", {})
        )

        self.cipher = self.Cipher.from_dict(
            data.get("cipher", {})
        )

        self.enum = enum
            

    def to_dict(self):
        d = {
            "meta": self.meta.to_dict(),
            "routing": self.routing.to_dict(),
            "payload": self.payload.to_dict(),
            "cipher": self.cipher.to_dict(),
        }

        return d

    def pack(self, sk=None):
        return bytes([self.enum]) + \
            to_b(
                json.dumps(
                    self.to_dict()
                )
            )
    
    @classmethod
    def unpack(cls, buf):
        d = json.loads(to_s(buf))

        # Sig checks if set.
        # check node id portion matches pub portion.
        # check sig matches serialized obj.
        return cls(d)

    def set_cur_addr(self, cur_addr_buf):
        self.routing.set_cur_dest(cur_addr_buf)

        # Set same machine flag.
        sid = self.meta.src["machine_id"]
        did = self.routing.dest["machine_id"]
        if sid == did:
            self.meta.same_machine = True