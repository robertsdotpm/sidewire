"""
design doesnt work. in the future messages will be received in any order
async for different events and a list of waiting events needs to try
match them.
"""


import struct
import asyncio
import itertools
from aionetiface import *
from .utils import *
from .mqtt_packet import *

EVENT_CONNECT = handle_connect
EVENT_SUBSCRIBE = handle_subscribe

class ProtoEvent:
    def __init__(self, event_type, params):
        self.process = event_type
        self.params = params

class MQTTClient:
    def __init__(self, af, nic, node_id, dest):
        self.af = af
        self.nic = nic
        self.dest = dest
        self.host, self.port = dest
        self.node_id = node_id
        self.client_id = rand_plain(15)
        self.pipe = None
        self.buf = b""
        self.f_proto = None
        self.subscriptions = set()

        self.events = asyncio.PriorityQueue()
        self.event_counter = itertools.count()

    def __await__(self):
        return self.connect().__await__()
    
    async def append_event(self, event):
        event.params["self"] = self
        await self.events.put((next(self.event_counter), event))
    
    async def process_events(self):
        while 1:
            index, event = await self.events.get()
            try:
                await event.process(**event.params)
            except Exception:
                what_exception()
                log_exception()

                # Add event back to queue.
                await self.events.put((index, event))

                # Sleep for retry.
                await asyncio.sleep(0.5)

            print(event, event.process)

    async def connect(self):
        pipe = await handle_connect(
            self,
            self.af,
            self.host,
            self.port,
            self.client_id,
            self.node_id,
            self.nic
        )

        if pipe:
            pipe.add_msg_cb(self.mqtt_packet_reader)
            self.pipe = pipe

    async def subscribe(self, topic):
        if topic in self.subscriptions:
            return
        
        await handle_subscribe(self, topic)
        self.subscriptions.add(topic)

    async def publish(self, topic, payload):
        await handle_publish(self, topic, payload)

    async def handle_mqtt_packet(self, packet):
        packet.debug_print()

        print("in handle mqtt pack")

        if MQTTEnum.PUBLISH == packet.type:
            out = mqtt_parse_publish(packet)
            if not out:
                return
            
            topic, payload, packet_id = out
            if topic not in self.subscriptions:
                return
            
            print("Got message at ", topic, " content = ", payload)

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
    nic = Interface("default")
    node_id = "node id"
    #m = MQTTClient(IP4, nic, node_id, ("test.mosquitto.org", 1883))
    m = MQTTClient(IP4, nic, node_id, ("ovh1.p2pd.net", 1883))
    await m.connect()


    #await m.subscribe(node_id)

    #await m.process_events()



    await m.subscribe("test/min35")
    await m.publish("test/min35", "hello from py3.5")
    await asyncio.sleep(4)

if __name__ == "__main__":
    async_run(workspace())