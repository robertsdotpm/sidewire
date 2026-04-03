"""
Connects to an MQTT server and subscribes to an ECDSA public key.
Checks if subscribe and connect were successful. Exception if it wasn't.
Registers the main chunked byte reader for processing partial or full packets
when done.
"""

import asyncio
import hashlib
from aionetiface import *
from .mqtt_defs import *
from .utils import *
from ..signing import *
from .mqtt_packet import *
from .mqtt_ordered_send import *
from .mqtt_dispatch import *
from .mqtt_reader import *

async def mqtt_connect(self, keep_alive):
    """
    Connects to an MQTT server, validates the handshake, and 
    subscribes to the client's ECDSA public key.
    """
    self.client_id = rand_plain(15)
    route = self.nic.route(self.af)
    
    # Establish TCP Connection
    pipe = await Pipe(TCP, (self.host, self.port), route).connect()
    if not pipe:
        raise ConnectionError("TCP Connection failed to {}:{}".format(self.host, self.port))

    # MQTT Handshake (CONNECT/CONNACK)
    connect_buf = build_connect(self.client_id, keep_alive=keep_alive)
    await pipe.send(connect_buf)

    # Expecting a standard 4-byte CONNACK (0x20 0x02 0x00 0x00)
    connack = await asyncio.wait_for(pipe.recv_n(4), timeout=4)
    if connack != b' \x02\x00\x00':
        await pipe.close()
        raise BadProtoResp("Invalid MQTT CONNACK: {}".format(connack))

    print("MQTT Connected: {}".format(self.client_id))

    # Message Processing Setup
    self.pipe = pipe
    
    # Register the packet reader callback
    async def handle_chunks_async(chunk, client_tup, pipe):
        return await mqtt_packet_reader(self, chunk, client_tup, pipe)
    
    # Add handler to read chunks.
    pipe.add_msg_cb(handle_chunks_async)

    # Public Key Subscription
    await subscribe_to_identity(self, pipe)
    
    # Return connected pipe.
    return pipe

async def subscribe_to_identity(self, pipe):
    """Handles the subscription to the public key topic."""
    pub_hex = to_hs(self.kp.compact_public_key)
    
    # Generate sub packet and the future tracking its acknowledgement
    buf, packet_ack_future = self.subscribe(pub_hex)
    await pipe.send(buf)

    # Wait for SUBACK return codes (QoS confirmation)
    return_codes = await asyncio.wait_for(packet_ack_future, timeout=4)

    # Only accept QoS 1; reject if server downgraded or errored
    for code in return_codes:
        if code != 1: 
            raise Exception(
                "Subscription failed. Expected QoS 1, got code: {}".format(code)
            )
        
# Ensure a connection exists before running dispatcher.
async def ensure_connection(client, keep_alive):
    while not client.is_closed.is_set():
        # connection_lost set.
        if client.pipe and not client.pipe.on_close.is_set():
            return True

        print("Got con lost event")
        # Close old handle.
        if client.pipe:
            await client.pipe.close(force=True)

        # Connect a new one.
        try:
            pipe = await mqtt_connect(client, keep_alive)
            if pipe:
                return True
        except (asyncio.TimeoutError, ConnectionError, OSError):
            # Server still down.
            pass

        # Avoid immediately reconnecting to avoid DoS.
        print("In disconnect loop?")
        await asyncio.sleep(60)

    return False