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
from enum import IntEnum
from aionetiface import *
from .utils import *
from .signing import *
from .mqtt_packet import *

MQTT_KEEP_ALIVE = 60

class MsgEnum(IntEnum):
    MSG = 1
    MSGACK = 2

class ProtoEvent:
    def __init__(self, event_type, params):
        self.process = event_type
        self.params = params

class MQTTClient:
    def __init__(self, af, nic, dest, kp):
        self.af = af
        self.nic = nic
        self.dest = dest
        self.host, self.port = dest
        self.kp = kp # Key pair -- defines our



        self.client_id = rand_plain(15)
        self.pipe = None
        self.buf = b""
        self.subscriptions = set()
        self.keep_alive_task = None

        # [packet enum][packet id] = future
        self.packet_ids = {
            MQTTEnum.SUBACK: {},
            MQTTEnum.PUBACK: {},
        }

        # Plugin message system [plugin id] -> list[{msg meta}] queue
        self.msg_queues = {} 

    def __await__(self):
        return self.connect().__await__()
    
    async def connect(self):
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
        pipe.add_msg_cb(self.mqtt_packet_reader)

        # Subscribe to messages to our own public key.
        pub_hex = to_hs(self.kp.compact_public_key)
        assert(len(pub_hex) == 66)
        await self.subscribe(pub_hex)

        # Periodically send ping requests to server.
        keep_alive = int(MQTT_KEEP_ALIVE / 2)
        self.keep_alive_task = asyncio.create_task(
            repeat_every(keep_alive, self.mqtt_keep_alive)
        )

    async def subscribe(self, topic, timeout=4):
        assert(type(topic) == str)

        # Already subscribed.
        if topic in self.subscriptions:
            return
        
        # Call function to send a subscribe packet.
        packet_id, packet_ack = self.packet_ack_future(MQTTEnum.SUBACK)
        await handle_subscribe(self, topic, packet_id)

        # Wait for acknowledgement from server.
        return_codes = await asyncio.wait_for(packet_ack, timeout)

        # Only accept QoS 1 -- errors or downgrades = no.
        for _, code in enumerate(return_codes):
            print("got return code = ", code)
            if code != 1: # QoS 1
                raise Exception("Invalid sub ack code " + str(code))

        # Record subscription success.
        print("acked subscribe.")
        self.subscriptions.add(topic)

    async def send(self, msg, dest_pk_hex, plugin_id_hex, msg_type=MsgEnum.MSG, seq_no=None):
        assert(len(plugin_id_hex) == 64)
        assert(len(dest_pk_hex) == 66)

        # Prepend application-level header to message portion.
        msg = to_h(bytes([msg_type])) + msg

        # Create queue per plugin ID.
        if msg_type == MsgEnum.MSG:
            if plugin_id_hex not in self.msg_queues:
                self.msg_queues[plugin_id_hex] = []

        # Get seq no
        print("seq no =", seq_no)
        if seq_no is None:
            seq_no = len(self.msg_queues[plugin_id_hex])
        seq_no_hex = "{:08x}".format(seq_no)

        # Signed message section.
        signed_msg = plugin_id_hex + seq_no_hex + msg
        sig = self.kp.private_key.sign(
            to_b(signed_msg),
            sigencode=util.sigencode_string
        )
        sig_hex = to_h(sig)
        assert(len(sig_hex) == 128)

        # Our public key.
        src_pk_hex = to_h(self.kp.compact_public_key) # 66
        assert(len(src_pk_hex) == 66)

        # Full proto message to send.
        out = src_pk_hex + sig_hex + signed_msg
        assert(type(out) == str)
        assert(len(out) == (266 + len(msg)))

        # Queue message.
        ack_future = asyncio.Future()
        if msg_type == MsgEnum.MSG:
            self.msg_queues[plugin_id_hex].append({
                "attempts": 0,
                "dest_pk_hex": dest_pk_hex,
                "seq_no": seq_no,
                "out": out,
                "acked": ack_future
            })
        
        # Publish the message as intended.
        await self.publish(dest_pk_hex, out)

        # Caller can await ack if they want.
        return ack_future

    def get_unique_packet_id(self, packet_type):
        for _ in range(0, 10000):
            packet_id = rand_b(2)
            if packet_id not in self.packet_ids[packet_type]:
                return packet_id
            
        raise Exception("Could not get unique packet id")
    
    def packet_ack_future(self, packet_type):
        packet_id = self.get_unique_packet_id(packet_type)
        packet_ack = asyncio.Future()
        self.packet_ids[packet_type][packet_id] = packet_ack
        return packet_id, packet_ack

    async def publish(self, topic, payload):
        assert(is_ascii(topic))
        assert(is_ascii(payload))
        packet_id, packet_ack = self.packet_ack_future(MQTTEnum.PUBACK)
        await handle_publish(self, topic, payload, packet_id)
        return packet_ack

    async def handle_mqtt_packet(self, packet):
        #packet.debug_print()

        #print("in handle mqtt pack")

        # Main packets to 
        for packet_type in (MQTTEnum.PUBACK, MQTTEnum.SUBACK):
            if packet_type == packet.type:
                #print("got mqtt ack")
                #print("puback var header = ", packet.body)
                #print("len body = ", len(packet.body))

                #assert(len(packet.body) == 2)
                # Strip packet_id from variable header.
                packet_id = packet.body[:2]
                if packet_id not in self.packet_ids[packet_type]:
                    return

                # Future to signal ack received for certain packets.
                ack_future = self.packet_ids[packet_type][packet_id]
                if ack_future.done():
                    return
                
                # Sets the return code only for SUBACK otherwise empty str.
                ack_future.set_result(packet.body[2:])
                #print("ack packet id", packet_id)

        # Handle receive channel message.
        if MQTTEnum.PUBLISH == packet.type:
            print("got mqtt publish")
            out = mqtt_parse_publish(packet)
            print(out)

            if not out:
                print("invalid publish packet")
                return
            
            topic, payload, packet_id = out
            if topic not in self.subscriptions:
                print("topic not in subscriptions.")
                return
            
            assert(is_ascii(topic))
            assert(is_ascii(payload))
            
            compact_public_key = h_to_b(topic)
            if compact_public_key != self.kp.compact_public_key:
                print("Recv msg not meant for us.")

            print("payload = ", payload)

            src_pk_hex = payload[:66]; p = 66
            sig = h_to_b(payload[p:p + 128]); p += 128
            signed_msg = to_b(payload[p:])
            plugin_id_hex = payload[p:p + 64]; p += 64
            seq_no_hex = payload[p:p + 8]; p += 8
            msg = payload[p:]


            vk = VerifyingKey.from_string(
                h_to_b(src_pk_hex),
                curve=SECP256k1
            )

            is_valid_sig = vk.verify(
                sig,
                signed_msg,
                sigdecode=util.sigdecode_string
            )

            if not is_valid_sig:
                print("invalid sig for ", msg)
                return
            
            msg_type = h_to_b(msg[:2])[0]
            msg = msg[2:]
            print("msg type = ", msg_type)

            # If a regular message is received then send an ACK back to owner.
            if MsgEnum.MSG == msg_type:
                print("sending back ack to src")
                await self.send(
                    "ack",
                    src_pk_hex,
                    plugin_id_hex,
                    MsgEnum.MSGACK,
                    seq_no=int(seq_no_hex, 16)
                )
                
                return
            
            # Handle ACK response.
            if MsgEnum.MSGACK == msg_type:
                print("got msg ack")
                if plugin_id_hex not in self.msg_queues:
                    print("msg ack: in valid plugin id")
                    return
                
                seq_no = int(seq_no_hex, 16)
                if seq_no > len(self.msg_queues[plugin_id_hex]):
                    print("msg ack seq no invalid")
                    return
                
                msg_meta = self.msg_queues[plugin_id_hex][seq_no]
                if msg_meta["acked"].done():
                    return
                
                msg_meta["acked"].set_result(True)
                return

            print("signed message for us = ", msg)

            #print("Got message at ", topic, " content = ", payload)

        # Handle ping from server.
        if MQTTEnum.PINGRESP == packet.type:
            #print("got ping response.")
            return
    
    async def mqtt_keep_alive(self):
        req = MQTTPacket(MQTTEnum.PINGREQ)
        buf = req.build()
        await self.pipe.send(buf)

    # TCP streaming protocol handler for MQTT.
    async def mqtt_packet_reader(self, chunk, client_tup, pipe):
        #print("got chunk = ", chunk)
        if not chunk:
            #print("not chunk")
            return

        # append incoming data
        self.buf += chunk

        # process as many complete packets as possible
        while self.buf:
            # need at least fixed header + 1 byte of remaining length
            if len(self.buf) < 2:
                print("need at least fixed header", self.buf)
                return

            # decode remaining length (starts at byte 1)
            rem_len, consumed = mqtt_decode_varint(self.buf, 1)
            if rem_len is None:
                print("rem len is none", self.buf)
                return

            # total packet size = fixed header (1) + varint + payload
            total_len = 1 + consumed + rem_len

            # wait for full packet
            if len(self.buf) < total_len:
                print("wait for full packet", self.buf)
                return

            # extract packet
            pkt_buf = self.buf[:total_len]
            self.buf = self.buf[total_len:]

            #print("pkt buf = ", pkt_buf)

            # parse + handle
            pkt = mqtt_parse_packet(pkt_buf)
            await async_wrap_errors(
                self.handle_mqtt_packet(pkt)
            )

async def load_signal_pipes(af, nic, seed_str, n, filter_list=[]):
    # Monitor incorrectly lists TCP servers under UDP.
    # Todo: fix this.
    # TODO: this itself is random so this is not working as expected
    servers = get_infra(af, UDP, "MQTT", sample=0)
    servers = [(s[0]["fqns"][0], s[0]["port"]) for s in servers if len(s[0]["fqns"])]
    mqtt_iter = seed_iter(servers, "test") # TODO

    def select_servers(n, kv):
        return [next(mqtt_iter) for x in range(0, n) if x not in filter_list]

    c = ObjCollection(
        lambda kparams, dest=None: MQTTClient(**kparams, dest=dest),
        select_servers=select_servers
    )

    out = await c.get_n(n, kv={
            "factory": {
                "af": af,
                "nic": nic,
                "node_id": seed_str,
            }
        }
    )

    return out

async def workspace():
        #m = MQTTClient(IP4, nic, node_id, ("test.mosquitto.org", 1883))
    nic = Interface("default")


    # Connect Alice client.
    alice_kp = Signing.keypair()
    alice_plugin_id = hashlib.sha256(b"alice plugin").hexdigest()
    alice_client = MQTTClient(IP4, nic, ("ovh1.p2pd.net", 1883), alice_kp)
    await alice_client.connect()

    # Connect Bob client.
    bob_kp = Signing.keypair()
    bob_plugin_id = hashlib.sha256(b"bob plugin").hexdigest()
    bob_client = MQTTClient(IP4, nic, ("ovh1.p2pd.net", 1883), bob_kp)
    await bob_client.connect()

    # Send a message from alice to bob.
    bob_ack_msg = await alice_client.send(
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