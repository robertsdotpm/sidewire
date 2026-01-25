import hashlib
from aionetiface import *
from .utils import *
from .base_msg import *
from .mqtt_client import *

class SignalRouter():
    def __init__(self, ifs, f_time, node_id, addr_bytes, sk, proto_def):
        self.ifs = ifs
        self.proto_def = proto_def # eg: SIG_PROTO def in signal_msgs.py
        self.f_time = f_time
        self.node_id = to_s(node_id)
        self.addr_bytes = addr_bytes
        self.sk = sk
        self.vk = sk.verifying_key.to_string("compressed")
        self.seen = {}
        self.tasks = []

    def record_seen_msg(self, buf):
        msg_hash = hashlib.sha256(buf).digest()
        self.seen[msg_hash] = True

    def discard_seen_msg(self, buf):
        msg_hash = hashlib.sha256(buf).digest()
        if msg_hash in self.seen:
            raise Exception("Message already seen.")

    async def signal_msg_sender(self, msg, plugin, relay_no=2):
        msg.meta = SigMsg.Meta.from_dict({
            "ttl": int(self.f_time()) + 30,
            "pipe_id": plugin.pipe_id,
            "af": plugin.af,
            "src_buf": plugin.src_map["bytes"],
            "src_index": plugin.src_info["if_index"],
            "route_type": plugin.route_type,
            "same_machine": plugin.same_machine,
            "plugin_name": msg.meta.plugin_name,
        })

        msg.routing = SigMsg.Routing.from_dict({
            "af": plugin.af,
            "dest_buf": plugin.dest_map["bytes"],
            "dest_index": plugin.dest_info["if_index"],
        })

        # Our key for an encrypted reply.
        msg.cipher.vk = self.vk
        print("SEND ", msg.to_dict())

        # Convert to bytes.
        buf = sig_msg_to_buf(msg)
        self.record_seen_msg(buf)

        # Send signaling message using MQTT.
        await send_msg_over_mqtt(
            self, 
            buf, 
            msg.routing.dest, 
            load_signal_pipes, 
            relay_no
        )

    # Receive a signal message and pass it to a plugin.
    def msg_cb(self, buf, client_tup, pipe):
        print("in signal router msg_cb")
        buf = to_b(buf)
        self.discard_seen_msg(buf)

        msg = try_unpack_msg(buf, self.sk, self.proto_def)
        if to_s(msg.routing.dest["node_id"]) != self.node_id:
            print("invalid ndoe id")
            raise Exception("Message not meant for us.")
        
        # Check TTL.
        if int(self.f_time()) >= msg.meta.ttl:
            raise Exception("Discard old msg.")
            return
        
        print("RECV = ", msg.to_dict())

        # Raise exception if this is old.

        # Updating routing dest with current addr.
        msg.set_cur_addr(self.addr_bytes)

        print("msg meta same machine is now = ", msg.meta.same_machine)
        
        # Pass this message on to existing plugin.
        # If one doesn't exist it will be created.
        try:
            plugin = self.traversal.get_plugin(msg)
        except Exception:
            print("get plugin failed")
            what_exception()
            log_exception()
            return

        print("plugin selected = ", plugin)

        #TODO: make this pop off older items when it fills.
        self.tasks.append(
            asyncio.create_task(
                async_wrap_errors(
                    self.traversal.run_plugin(plugin, reply=msg)
                    #plugin.run(reply=msg) 
                )
            )
        )

    def set_signal_pipes(self, signal_pipes):
        self.signal_pipes = {IP4: {}, IP6: {}}
        for pipe in signal_pipes:
            self.signal_pipes[pipe.af][pipe.host] = pipe
            pipe.f_proto = self.msg_cb

    def set_traversal_manager(self, traversal):
        self.traversal = traversal
