"""
This module implements a basic async MQTT client. It is not designed to be
generic like a regular MQTT client. Instead it supports:

    - identity management -- clients subscribe to their own ECDSA pub key hex
    - authenticated messages -- all messages are signed by the sender, replies
    are sent back to originating pub key hex channel
    - reliable delivery -- application level acks allow message delivery to be
    confirmed before moving to next message
    - sequential messaging -- sequential messaging queues allow for culmulative
    ack to keep ack spam to a minimum. Asyncio still sequences sends without
    the need to use a specific message queue so this is optional.

Technical notes:
    - client ID is intentionally random each time
    - connect clean session = True -- no old stored messages used
    - publish "dup" flag = False
    - no reused packet IDs for publish messages
    - delivery and ordering handled by application-level ack

Using the above settings means that message retransmittion is no longer managed
by the server and ends up entirely managed by the client. The MQTT server becomes
a simple proxy in such a mode. But you can build a more reliable, deterministic,
system on top of it without hidden edge-cases that might exist (e.g. storage
limits, protocol implementation differences, and so on.)

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

Everything ended up under 700 lines of code which I think is pretty good!
"""

import hashlib
import asyncio
import time
from aionetiface import *
from .mqtt_defs import *
from .utils import *
from .mqtt_packet import *
from .mqtt_proto import *
from .mqtt_ordered_send import *
from .mqtt_dispatch import *
from .mqtt_connect import *
from .mqtt_msgs import *

class MQTTClient:
    def __init__(self, af, nic, dest, kp, get_time=time.time):
        # Addressing info for connected MQTT server.
        self.af = af
        self.nic = nic
        self.dest = dest
        self.host, self.port = dest

        # ECDSA key pair for signing high-level sequenced messages over MQTT.
        self.kp = kp 

        # Handle received messages.
        self.msg_handlers = []

        # Plugin message system [enum][pipe id][seq_no] = meta
        # Used by the dispatcher task to republish messages.
        # The packet handler code in proto also sets futures here (app level ACKs.)
        self.msg_queues = {
            MsgEnum.MSG: {},
            MsgEnum.MSGACK: {},
            MsgEnum.PROBE: {},
        }

        # Msg part unpacked from the full proto over publish.
        self.sent_msg_ids = {}
        self.recv_msg_ids = {}

        # Record in-flight packets by IDs that map to futures on server ack.
        # [packet enum][packet id] = Future
        self.packet_ids = {
            MQTTEnum.SUBACK: {},
            MQTTEnum.PUBACK: {},
        }

        # Other internal state.
        self.packet_id = 0
        self.client_id = None

        # Connection state.
        self.is_closed = asyncio.Event()
        self.pipe = None
        self.buf = b""

        # Background task for processing messages.
        self.dispatcher_task = None

        # Used to get unix timestamp.
        self.get_time = get_time

    # Receive back app protocol msgs unpacked.
    def add_msg_handler(self, msg_handler):
        self.msg_handlers.append(msg_handler)

    # Allow awaiting on the class object directly.
    def __await__(self):
        return self.connect().__await__()
    
    # Connect to MQTT server and subscribe to our own pub key hex topic.
    # Also will start a background task that dispatches messages from self.send.
    async def connect(self, republish_duration=60, interval=2, keep_alive=MQTT_KEEP_ALIVE, ignore_acked=False, reconnect_delay=0, timeout=4):
        # Re-entry guard.
        if self.dispatcher_task:
            return self.pipe

        # Store for use in timestamp validation on receive.
        self.republish_duration = max(republish_duration, 2 * keep_alive)

        # Connect with a timeout.
        try:
            pipe = await asyncio.wait_for(mqtt_connect(self, keep_alive), timeout)
        except asyncio.TimeoutError:
            raise ConnectionError("MQTT connection timeout.")

        # If it works start the background message dispatcher.
        if pipe and self.dispatcher_task is None:
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

    # Internal: used to subscribe to own pub key hex.
    # Returns packet and future to await ack for the packet from the server.
    def subscribe(self, topic):
        assert(type(topic) == str)
        packet_id, packet_ack = packet_ack_future(self, MQTTEnum.SUBACK)
        buf = build_subscribe(topic, packet_id)
        return buf, packet_ack
    
    # High level function: send a message to a dest pub key hash (topic.)
    # Puts the msg in a sequenced queue called queue_id_hex to be republished.
    # A background dispatcher task loops over these queues to repub messages.
    def queue_msg(self, msg, dest_pk_hex, queue_id_hex=None, msg_type=MsgEnum.MSG, seq_no=None):
        queue_id_hex = queue_id_hex or to_h(rand_b(32))
        return ordered_ack_send(
            self,
            msg,
            dest_pk_hex,
            queue_id_hex,
            msg_type,
            seq_no
        )
    
    # Stops broadcasting a msg.
    def dequeue_msg(self, queue_id_hex, seq_no=None, msg_type=MsgEnum.MSG):
        # Get queue by queue id.
        queue = self.msg_queues[msg_type]
        if queue_id_hex not in queue:
            return
        
        # If no seq_no set delete the whole queue.
        if seq_no is None:
            del self.msg_queues[msg_type][queue_id_hex]
            return
        
        # Otherwise: disable it to preserve seq_no offsets.
        if seq_no not in self.msg_queues[msg_type][queue_id_hex]:
            return
        
        # Get the app ack future.
        app_ack = self.msg_queues[msg_type][queue_id_hex][seq_no]["app_ack"]
        if app_ack.done():
            return
        
        app_ack.set_result(True)

    # Internal: used to publish signed messages to pub key hashed topics.
    # Returns packet and future to await ack for the packet from the server.
    def publish(self, topic, payload, dup=False):
        assert(is_ascii(topic))
        assert(is_ascii(payload))
        packet_id, packet_ack = packet_ack_future(self, MQTTEnum.PUBACK)
        buf = build_publish(topic, payload, packet_id, dup)
        return buf, packet_ack
    
    # Cleanly disconnect from MQTT server.
    async def close(self):
        # Already closed.
        if self.is_closed.is_set():
            return
        
        # Indicate closed to block other callers.
        self.is_closed.set()

        # Cancel message dispatcher bg task and wait for it to exit cleanly
        # so its CancelledError handler runs before we close the pipe.
        if self.dispatcher_task:
            self.dispatcher_task.cancel()
            try:
                await self.dispatcher_task
            except asyncio.CancelledError:
                pass
            self.dispatcher_task = None

        # Cleanly disconnect from the MQTT server.
        # The pipe sends disconnect, does shutdown, then close.
        if self.pipe:
            # Disconnect packet.
            disconnect_buf = build_disconnect()
            await async_wrap_errors(
                self.pipe.send(disconnect_buf)
            )

            # Close pipe.
            await self.pipe.close()
            self.pipe = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *_):
        await self.close()
        return False

    async def ping_handler(self):
        pass

# Example usage using -m
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
    async def msg_handler(msg, src_pk_hex, queue_id_hex, client):
        print("msg handler got ", msg, " ", src_pk_hex, " ", queue_id_hex)

    # Connect Alice client.
    alice_kp = Signing.keypair()
    alice_queue_id = hashlib.sha256(b"alice pipe").hexdigest()
    alice_client = MQTTClient(IP4, nic, ("ovh1.p2pd.net", 1883), alice_kp)
    await alice_client.connect()
    alice_client.pipe.sock.close()

    # Connect Bob client.
    bob_kp = Signing.keypair()
    bob_queue_id = hashlib.sha256(b"bob plugin").hexdigest()
    bob_client = MQTTClient(IP4, nic, ("ovh1.p2pd.net", 1883), bob_kp)
    bob_client.add_msg_handler(msg_handler)
    await bob_client.connect()

    # Send a message from alice to bob.
    _, bob_ack_msg = alice_client.queue_msg(
        "hello bob -- with ordering and ack", 
        # Destination channel is Bob's public key hex.
        to_hs(bob_kp.compact_public_key),
        alice_queue_id
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