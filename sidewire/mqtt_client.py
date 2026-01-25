import struct
from aionetiface import *
from .utils import *

class MQTTClient:
    def __init__(self, af, nic, node_id, dest):
        self.af = af
        self.nic = nic
        self.dest = dest
        self.host, self.port = dest
        self.node_id = node_id
        self.client_id = rand_plain(15)
        self.pipe = None
        self.buffer = b""
        self.f_proto = None

    def __await__(self):
        return self.connect().__await__()

    async def connect(self):
        print("mqtt connect")
        route = self.nic.route(self.af)
        self.pipe = await Pipe(TCP, (self.host, self.port), route).connect()

        # proto name, proto level, clean session, keep alive 60s
        vh = (mqtt_enc_str("MQTT") + b"\x04" + b"\x02" + b"\x00\x3c")
        pl = mqtt_enc_str(self.client_id)

        # Full packet to send.
        pkt = b"\x10" + mqtt_enc_varint(len(vh) + len(pl)) + vh + pl
        await self.pipe.send(pkt)

        # CONNACK (fixed 4 bytes)
        await self.pipe.recv()

        # Subscribe to client id.
        await self.subscribe(self.node_id)

        # Create processing task.
        self.pipe.add_msg_cb(self.msg_cb)
        return self

    async def publish(self, topic, payload):
        pl = mqtt_enc_str(topic) + to_b(payload)
        pkt = b"\x30" + mqtt_enc_varint(len(pl)) + pl
        await self.pipe.send(pkt)

    async def subscribe(self, topic):
        pkt_id = 1
        vh = struct.pack("!H", pkt_id)
        pl = mqtt_enc_str(topic) + b"\x00"  # QoS 0
        pkt = b"\x82" + mqtt_enc_varint(len(vh) + len(pl)) + vh + pl
        await self.pipe.send(pkt)

        # SUBACK
        await self.pipe.recv()

    # TCP streaming protocol handler for MQTT.
    async def msg_cb(self, chunk, client_tup, pipe):
        # Receive more data into buffer
        if not chunk:
            return

        # not enough for fixed header
        self.buffer += chunk
        if len(self.buffer) < 2:
            return  

        # need more bytes for varint
        rem_len, consumed = mqtt_decode_varint(self.buffer[1:])
        if rem_len is None:
            return  

        # wait for full packet
        total_len = 1 + consumed + rem_len
        if len(self.buffer) < total_len:
            return  

        packet = self.buffer[:total_len]
        self.buffer = self.buffer[total_len:]
        got = await handle_mqtt_packet(packet)

        # When a full message is assembled pass it on.
        #print("mqtt.loop = ", got)
        if got and self.f_proto:
            self.f_proto(list(got.values())[0], (), self)

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
    nic = Interface("default")
    m = MQTTClient(IP4, nic, ("test.mosquitto.org", 1883))
    await m.connect()
    await m.subscribe("test/min35")
    await m.publish("test/min35", "hello from py3.5")
    await asyncio.sleep(2)

if __name__ == "__main__":
    async_run(workspace())