import struct
from aionetiface import *
from .utils import *

class MQTTClient:
    def __init__(self, af, nic, dest, client_id=None):
        self.af = af
        self.nic = nic
        self.dest = dest
        self.host, self.port = dest
        self.client_id = client_id or rand_plain(15)
        self.pipe = None
        self.buffer = b""
        self.f_proto = None
        self.loop_task = None

    def __await__(self):
        return self.connect().__await__()

    async def connect(self):
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
        await self.subscribe(self.client_id)

        # Create processing task.
        self.loop_task = asyncio.create_task(self.loop())
        return self

    async def publish(self, topic, payload):
        pl = mqtt_enc_str(topic) + payload.encode("utf-8")
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

    async def loop(self):
        while True:
            # Receive more data into buffer
            chunk = await self.pipe.recv()
            if not chunk:
                break

            self.buffer += chunk
            while True:
                # not enough for fixed header
                if len(self.buffer) < 2:
                    break  

                # need more bytes for varint
                rem_len, consumed = mqtt_decode_varint(self.buffer[1:])
                if rem_len is None:
                    break  

                # wait for full packet
                total_len = 1 + consumed + rem_len
                if len(self.buffer) < total_len:
                    break  

                packet = self.buffer[:total_len]
                self.buffer = self.buffer[total_len:]
                got = await handle_mqtt_packet(packet)

                # Pass proto message on.
                print("mqtt.loop = ", got)
                if got and self.f_proto:
                    self.f_proto(list(got.values())[0], (), self)


async def load_signal_pipes(af, nic, seed_str, n):
    # Monitor incorrectly lists TCP servers under UDP.
    # Todo: fix this.
    servers = get_infra(af, UDP, "MQTT", n + 1) or get_infra(af, TCP, "MQTT", n + 1)
    servers = [(s[0]["ip"], s[0]["port"]) for s in servers]
    mqtt_iter = seed_iter(servers, seed_str)

    def select_servers(n, kv):

        return [next(mqtt_iter) for _ in range(0, n)]

    c = ObjCollection(
        lambda kparams, dest=None: MQTTClient(**kparams, dest=dest),
        select_servers=select_servers
    )

    out = await c.get_n(n, kv={
            "factory": {
                "af": af,
                "nic": nic,
            }
        }
    )

    return out

async def workspace():
    m = MQTT("test.mosquitto.org")
    await m.connect()
    await m.subscribe("test/min35")
    await m.publish("test/min35", "hello from py3.5")
    await m.loop()

if __name__ == "__main__":
    async_run(workspace())