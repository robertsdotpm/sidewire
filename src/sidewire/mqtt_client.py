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
    msg format: 33 our_pub_key, 64 sig over ( 32 plugin_id, msg )
    ^ allows ack back

"""



import asyncio
from aionetiface import *
from .utils import *
from .signing import *
from .mqtt_packet import *

MQTT_KEEP_ALIVE = 60

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
        await self.subscribe(self.kp.compact_public_key)

        # Periodically send ping requests to server.
        keep_alive = int(MQTT_KEEP_ALIVE / 2)
        self.keep_alive_task = asyncio.create_task(
            repeat_every(keep_alive, self.mqtt_keep_alive)
        )

    async def subscribe(self, topic):
        if type(topic) == bytes:
            topic = to_h(topic)

        if topic in self.subscriptions:
            return
        
        await handle_subscribe(self, topic)
        self.subscriptions.add(topic)

    async def send(self, msg, dest_pk_hex, plugin_id):
        assert(len(plugin_id) == 64)
        assert(len(dest_pk_hex) == 66)

        # Signed message section.
        signed_msg = plugin_id + msg
        sig = self.kp.private_key.sign(
            signed_msg,
            sigencode=util.sigencode_string
        )
        assert(len(sig) == 128)

        # Our public key.
        src_pk_hex = to_h(self.kp.compact_public_key) # 66
        assert(len(src_pk_hex) == 66)

        # Full proto message to send.
        out = src_pk_hex + sig + plugin_id + msg
        assert(len(out) == (258 + len(msg)))
        
        # Publish the message as intended.
        await self.publish(dest_pk_hex, out)

    async def publish(self, topic, payload):
        await handle_publish(self, topic, payload)

    async def handle_mqtt_packet(self, packet):
        packet.debug_print()

        print("in handle mqtt pack")

        # Handle receive channel message.
        if MQTTEnum.PUBLISH == packet.type:
            out = mqtt_parse_publish(packet)
            if not out:
                return
            
            topic, payload, packet_id = out
            if topic not in self.subscriptions:
                return
            
            print("Got message at ", topic, " content = ", payload)

        # Handle ping from server.
        if MQTTEnum.PINGRESP == packet.type:
            print("got ping response.")
            return
    
    async def mqtt_keep_alive(self):
        req = MQTTPacket(MQTTEnum.PINGREQ)
        buf = req.build()
        await self.pipe.send(buf)

    # TCP streaming protocol handler for MQTT.
    async def mqtt_packet_reader(self, chunk, client_tup, pipe):
        print("got chunk = ", chunk)
        if not chunk:
            print("not chunk")
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

            print("pkt buf = ", pkt_buf)

            # parse + handle
            pkt = mqtt_parse_packet(pkt_buf)
            await self.handle_mqtt_packet(pkt)

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


    alice_kp = Signing.keypair()

    alice_client = MQTTClient(IP4, nic, ("ovh1.p2pd.net", 1883), alice_kp)
    await alice_client.connect()
    await asyncio.sleep(2)
    await alice_client.publish(alice_kp.compact_public_key, b"hello test")
    

    await asyncio.sleep(4)
    return


    #await m.subscribe(node_id)

    #await m.process_events()



    await m.subscribe("test/min35")
    await m.publish("test/min35", "hello from py3.5")
    await asyncio.sleep(4)

if __name__ == "__main__":
    async_run(workspace())