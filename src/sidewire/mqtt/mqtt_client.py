"""

topic (33 their pub key)
    in bytes -- double all for hex
    msg format: 33 our_pub_key, 64 sig over ( 32 plugin_id, 4 seq_no, msg )
    ^ allows ack back

"""

import hashlib
import asyncio
from aionetiface import *
from .mqtt_defs import *
from .utils import *
from ..signing import *
from .mqtt_packet import *
from .mqtt_proto import *
from .ordered_send import *
from .mqtt_dispatch import *
from .mqtt_connect import *

MQTT_KEEP_ALIVE = 60

class MQTTClient:
    def __init__(self, af, nic, dest, kp):
        # Addressing info for connected MQTT server.
        self.af = af
        self.nic = nic
        self.dest = dest
        self.host, self.port = dest

        # Active TCP con and buffered reader.
        self.pipe = None
        self.buf = b""

        # ECDSA key pair for signing high-level sequenced messages over MQTT.
        self.kp = kp 

        # Low level
        # [packet enum][packet id] = future
        self.packet_ids = {
            MQTTEnum.SUBACK: {},
            MQTTEnum.PUBACK: {},
        }

        # Plugin message system [enum][plugin id] = list[{msg meta}] queue
        self.msg_queues = {
            MsgEnum.MSG: {},
            MsgEnum.MSGACK: {},
        } 

        # Dispatcher task.
        self.dispatcher_task = asyncio.create_task(
            async_wrap_errors(
                dispatcher(self, keep_alive=int(MQTT_KEEP_ALIVE / 2))
            )
        )

    def __await__(self):
        return self.connect().__await__()

    def mqtt_keep_alive(self):
        req = MQTTPacket(MQTTEnum.PINGREQ)
        return req.build()

    def subscribe(self, topic, timeout=4):
        assert(type(topic) == str)
        packet_id, packet_ack = packet_ack_future(self.packet_ids, MQTTEnum.SUBACK)
        buf = build_subscribe(topic, packet_id)
        return buf, packet_ack

    def publish(self, topic, payload):
        assert(is_ascii(topic))
        assert(is_ascii(payload))
        packet_id, packet_ack = packet_ack_future(self.packet_ids, MQTTEnum.PUBACK)
        buf = build_publish(topic, payload, packet_id)
        return buf
    
    def send(self, msg, dest_pk_hex, plugin_id_hex, msg_type=MsgEnum.MSG, seq_no=None):
        return ordered_send(
            self,
            msg,
            dest_pk_hex,
            plugin_id_hex,
            msg_type,
            seq_no
        )

async def workspace():
    #m = MQTTClient(IP4, nic, node_id, ("test.mosquitto.org", 1883))
    nic = Interface("default")

    #pipe = await Pipe(TCP, ("example.com", 8123), nic.route(IP4)).connect()
    #print(pipe)
    #return

    """
    pipe = await Pipe(TCP, ("example.com", 80), nic.route(IP4)).connect()
    pipe.sock.shutdown(socket.SHUT_RDWR)
    pipe.sock.close()   
    print(pipe.sock)

    ret = await pipe.send(b"test")
    # 0 on con lost
    print(ret)
    return
    """
    

    # Connect Alice client.
    alice_kp = Signing.keypair()
    alice_plugin_id = hashlib.sha256(b"alice plugin").hexdigest()
    alice_client = MQTTClient(IP4, nic, ("ovh1.p2pd.net", 1883), alice_kp)
    #await alice_client.connect()
    #alice_client.pipe.sock.close()

    # Connect Bob client.
    bob_kp = Signing.keypair()
    bob_plugin_id = hashlib.sha256(b"bob plugin").hexdigest()
    bob_client = MQTTClient(IP4, nic, ("ovh1.p2pd.net", 1883), bob_kp)
    #await bob_client.connect()

    # Send a message from alice to bob.
    bob_ack_msg = alice_client.send(
        "hello bob -- with ordering and ack", 
        # Destination channel is Bob's public key hex.
        to_hs(bob_kp.compact_public_key),
        alice_plugin_id
    )
    
    # Wait for alice to receive the message.
    await bob_ack_msg
    print("got ack from bob")
    await asyncio.sleep(4)
    return


    #await m.subscribe(node_id)

    #await m.process_events()



    await m.subscribe("test/min35")
    await m.publish("test/min35", "hello from py3.5")
    await asyncio.sleep(4)

if __name__ == "__main__":
    async_run(workspace())