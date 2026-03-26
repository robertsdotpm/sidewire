"""
design doesnt work. in the future messages will be received in any order
async for different events and a list of waiting events needs to try
match them.

publishing side:
    PUBACK (for QoS 1)

    
    DISCONNECT (optional but important)
    TCP close/reset
        everything should be resumable.


msg queue ...
    plugin id
        await ack from actual dest before sending next
        pop

    can continue to next msg queued for next plugin before the other returns

provides ordered guaranteed with signed ack to dest

send(msg, dest_pub_key, plugin_id)

topic (33 their pub key)
    in bytes -- double all for hex
    msg format: 33 our_pub_key, 64 sig over ( 32 plugin_id, 4 seq_no, msg )
    ^ allows ack back

do mqtt packet ides first then do the app level ack stuff
"""

import hashlib
import asyncio
from aionetiface import *
from .mqtt_defs import *
from .utils import *
from .signing import *
from .mqtt_packet import *
from .mqtt_proto import *
from .ordered_send import *

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
                self.dispatcher()
            )
        )

    def __await__(self):
        return self.connect().__await__()
    
    async def dispatcher(self, attempts=3, interval=60, keep_alive=int(MQTT_KEEP_ALIVE / 2)):
        counter = 0
        while 1:
            for msg_type in self.msg_queues:
                for plugin_id_hex in self.msg_queues[msg_type]:
                    for meta in self.msg_queues[msg_type][plugin_id_hex]:
                        # Already acked -- try next in line.
                        if meta["acked"].done():
                            continue

                        # Don't rebroadcast if too soon.
                        elapsed = int(time.time()) - meta["updated"]
                        if elapsed < interval:
                            break

                        # Give up on acking this line of messages if too many past.
                        if meta["attempts"] >= attempts:
                            break

                        # Increase state counters.
                        meta["attempts"] += 1
                        meta["updated"] = int(time.time())

                        # Broadcast new message.
                        print("dispatching ", meta)
                        buf = self.publish(meta["dest_pk_hex"], meta["out"])
                        await self.pipe.send(buf)

            await asyncio.sleep(1)
            counter += 1 % 0xFFFFFFFFFF

            # Send ping to server every so often.
            if (counter % (keep_alive + 1)) == keep_alive:
                buf = self.mqtt_keep_alive()
                await self.pipe.send(buf)

            # Pipe down -- reconnect loop.
            if self.pipe.on_close.is_set():
                print("Got con lost event")
                while 1:
                    # Close old handle.
                    if self.pipe:
                        await self.pipe.close(force=True)

                    # Connect a new one.
                    try:
                        pipe = await self.connect()
                        if pipe:
                            break
                    except (asyncio.TimeoutError, ConnectionError, OSError):
                        # Server still down.
                        pass

                    await asyncio.sleep(60)

    
    async def connect(self):
        # Protocol-specific used for the session.
        self.client_id = rand_plain(15)

        pipe = await handle_connect(
            self,
            self.af,
            self.host,
            self.port,
            self.client_id,
            self.nic,
            keep_alive=MQTT_KEEP_ALIVE
        )

        if not pipe:
            raise Exception("could not connect.")
        
        # Start processing responses async from server.
        self.pipe = pipe

        async def handle_chunks_async(chunk, client_tup, pipe):
            return await mqtt_packet_reader(self, chunk, client_tup, pipe)

        pipe.add_msg_cb(handle_chunks_async)

        # Subscribe to messages to our own public key.
        pub_hex = to_hs(self.kp.compact_public_key)
        assert(len(pub_hex) == 66)
        await self.subscribe(pub_hex)

        return pipe

    def mqtt_keep_alive(self):
        req = MQTTPacket(MQTTEnum.PINGREQ)
        return req.build()

    async def subscribe(self, topic, timeout=4):
        assert(type(topic) == str)

        # Call function to send a subscribe packet.
        packet_id, packet_ack = packet_ack_future(self.packet_ids, MQTTEnum.SUBACK)
        await handle_subscribe(self, topic, packet_id)

        # Wait for acknowledgement from server.
        return_codes = await asyncio.wait_for(packet_ack, timeout)

        # Only accept QoS 1 -- errors or downgrades = no.
        for _, code in enumerate(return_codes):
            print("got return code = ", code)
            if code != 1: # QoS 1
                raise Exception("Invalid sub ack code " + str(code))

    def publish(self, topic, payload):
        assert(is_ascii(topic))
        assert(is_ascii(payload))
        packet_id, packet_ack = packet_ack_future(self.packet_ids, MQTTEnum.PUBACK)
        buf = handle_publish(self, topic, payload, packet_id)
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
    await alice_client.connect()
    alice_client.pipe.sock.close()

    # Connect Bob client.
    bob_kp = Signing.keypair()
    bob_plugin_id = hashlib.sha256(b"bob plugin").hexdigest()
    bob_client = MQTTClient(IP4, nic, ("ovh1.p2pd.net", 1883), bob_kp)
    await bob_client.connect()

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