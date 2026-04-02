"""
This module implements a basic async MQTT client. It is not designed to be
generic like a regular MQTT client. Instead it supports:

    - identity management -- clients subscribe to their own ECDSA pub key hex
    - authenticated messages -- all messages are signed by the sender, replies
    are sent back to originating pub key hex channel
    - sequential messaging -- bare-bones MQTT doesn't have ordered messaging
    this allows MQTT to be multi-plexed and into ordered pipes
    - reliable delivery -- application level acks allow message delivery to be
    confirmed before moving to next message

The main class interface is designed to be sync without any network I/O. All I/O
is instead confined to a background message dispatcher written to be simple. This
makes it easier to recover from network disconnects and resume publishing. If there
is only one major place to detect network errors it makes it easier to handle errors.
The client uses a simple buffer for incoming data from TCP until any number of
full packets are ready to be processed (this is how to correctly handle TCP.)

module files:
    - schedule messages to send, connect to server - mqtt client
    - recv data and pass messages on to handlers - mqtt protocol
    - send out queued messages and reconnect on con lost - mqtt dispatcher
    - handle connection in detail - mqtt connect
    - craft and unpack various mqtt packets - mqtt packet
"""

import hashlib
import asyncio
from aionetiface import *
from .mqtt_defs import *
from .utils import *
from ..signing import *
from .mqtt_packet import *
from .mqtt_proto import *
from .mqtt_ordered_send import *
from .mqtt_dispatch import *
from .mqtt_connect import *

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
        # [packet enum][packet id] = True
        self.packet_ids = {
            MQTTEnum.SUBACK: {},
            MQTTEnum.PUBACK: {},
        }

        # Plugin message system [enum][pipe id][seq_no] = meta
        self.msg_queues = {
            MsgEnum.MSG: {},
            MsgEnum.MSGACK: {},
        }

        # Msg part unpacked from the full proto over publish.
        self.sent_msg_ids = {}

        # Handle received messages.
        self.msg_handlers = []

        # Dispatcher task.
        self.is_closed = asyncio.Event()
        self.dispatcher_task = None

    def __await__(self):
        return self.connect().__await__()
    
    def add_msg_handler(self, msg_handler):
        self.msg_handlers.append(msg_handler)

    async def ping_handler(self):
        pass
    
    async def connect(self, republish_duration=60, interval=5, keep_alive=MQTT_KEEP_ALIVE, ignore_acked=False, reconnect_delay=0):
        pipe = await mqtt_connect(self, keep_alive)
        if pipe:
            """
            As dispatcher also sets self.pipe for reconnect -- there is a race
            condition between connect() and reconnect loop. Starting dispatcher
            after connect solves that issue.
            """
            self.dispatcher_task = asyncio.create_task(
                async_wrap_errors(
                    dispatcher(
                        self,
                        republish_duration=republish_duration,
                        interval=interval,
                        keep_alive=keep_alive,
                        ignore_acked=ignore_acked,
                        reconnect_delay=reconnect_delay,
                    )
                )
            )

        return pipe

    def subscribe(self, topic, timeout=4):
        assert(type(topic) == str)
        packet_id, packet_ack = packet_ack_future(self.packet_ids, MQTTEnum.SUBACK)
        buf = build_subscribe(topic, packet_id)
        return buf, packet_ack

    def publish(self, topic, payload, packet_id=b"", dup=False):
        assert(is_ascii(topic))
        assert(is_ascii(payload))
        assert(type(packet_id) == bytes)
        buf = build_publish(topic, payload, packet_id, dup)
        return buf
    
    def send(self, msg, dest_pk_hex, pipe_id_hex, msg_type=MsgEnum.MSG, seq_no=None):
        return ordered_ack_send(
            self,
            msg,
            dest_pk_hex,
            pipe_id_hex,
            msg_type,
            seq_no
        )
    
    async def close(self):
        if self.is_closed.is_set():
            return
        
        self.is_closed.set()
        if self.dispatcher_task:
            self.dispatcher_task.cancel()
            self.dispatcher_task = None

        if self.pipe:
            await self.pipe.close()
            self.pipe = None

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
    
    """
    This will probably end up being a closure that has an embedded reference
    back to the plugin manager.
    """
    async def msg_handler(msg, src_pk_hex, pipe_id_hex, client):
        print("msg handler got ", msg, " ", src_pk_hex, " ", pipe_id_hex)

    # Connect Alice client.
    alice_kp = Signing.keypair()
    alice_pipe_id = hashlib.sha256(b"alice pipe").hexdigest()
    alice_client = MQTTClient(IP4, nic, ("ovh1.p2pd.net", 1883), alice_kp)
    await alice_client.connect()
    alice_client.pipe.sock.close()

    # Connect Bob client.
    bob_kp = Signing.keypair()
    bob_pipe_id = hashlib.sha256(b"bob plugin").hexdigest()
    bob_client = MQTTClient(IP4, nic, ("ovh1.p2pd.net", 1883), bob_kp)
    bob_client.add_msg_handler(msg_handler)
    await bob_client.connect()

    # Send a message from alice to bob.
    _, bob_ack_msg = alice_client.send(
        "hello bob -- with ordering and ack", 
        # Destination channel is Bob's public key hex.
        to_hs(bob_kp.compact_public_key),
        alice_pipe_id
    )
    
    # Wait for alice to receive the message.
    await bob_ack_msg
    print("got ack from bob")
    await asyncio.sleep(4)
    await alice_client.close()
    await bob_client.close()
    return


    #await m.subscribe(node_id)

    #await m.process_events()



    await m.subscribe("test/min35")
    await m.publish("test/min35", "hello from py3.5")
    await asyncio.sleep(4)

if __name__ == "__main__":
    async_run(workspace())