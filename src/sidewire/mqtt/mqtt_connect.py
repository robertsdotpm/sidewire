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

async def mqtt_connect(self, keep_alive):
    # Protocol-specific used for the session.
    self.client_id = rand_plain(15)
    route = self.nic.route(self.af)
    pipe = await Pipe(TCP, (self.host, self.port), route).connect()

    buf = build_connect(
        self.client_id,
        keep_alive=keep_alive
    )

    await pipe.send(buf)

    if not pipe:
        raise Exception("could not connect.")
    
    # CONNACK (fixed 4 bytes)
    got = await asyncio.wait_for(pipe.recv_n(4), 4)
    if got != b' \x02\x00\x00':
        await pipe.close()
        raise BadProtoResp("Invalid CON ACK")
    
    print("mqtt Connected success")
    
    # Start processing responses async from server.
    self.pipe = pipe

    async def handle_chunks_async(chunk, client_tup, pipe):
        return await mqtt_packet_reader(self, chunk, client_tup, pipe)

    pipe.add_msg_cb(handle_chunks_async)

    # Subscribe to messages to our own public key.
    pub_hex = to_hs(self.kp.compact_public_key)
    assert(len(pub_hex) == 66)

    # Subscribe to pub key hex.
    buf, packet_ack = self.subscribe(pub_hex)
    await pipe.send(buf)

    # Wait for acknowledgement from server.
    return_codes = await asyncio.wait_for(packet_ack, 4)

    # Only accept QoS 1 -- errors or downgrades = no.
    for _, code in enumerate(return_codes):
        print("got return code = ", code)
        if code != 1: # QoS 1
            raise Exception("Invalid sub ack code " + str(code))

    return pipe