from aionetiface import *
from .mqtt_defs import *
from .utils import *
from ..signing import *
from .mqtt_packet import *
from .mqtt_proto import *

# TCP streaming protocol handler for MQTT.
async def mqtt_packet_reader(client, chunk, client_tup, pipe):
    # Nothing received, end.
    if not chunk:
        #print("not chunk")
        return

    # Append incoming data to buffer.
    client.buf += chunk

    # Process as many complete packets as possible.
    while client.buf:
        # Need at least fixed header + 1 byte of remaining length.
        if len(client.buf) < 2:
            print("need at least fixed header", client.buf)
            return

        # Decode remaining length (starts at byte 1).
        rem_len, consumed = mqtt_decode_varint(client.buf, 1)
        if rem_len is None:
            print("rem len is none", client.buf)
            return

        # Total packet size = fixed header (1) + varint + payload.
        total_len = 1 + consumed + rem_len

        # Wait for full packet.
        if len(client.buf) < total_len:
            print("wait for full packet", client.buf)
            return

        # Extract full packet.
        pkt_buf = client.buf[:total_len]
        client.buf = client.buf[total_len:]

        #print("pkt buf = ", pkt_buf)

        # Parse + handle.
        pkt = mqtt_parse_packet(pkt_buf)
        await async_wrap_errors(
            handle_mqtt_packet(client, pkt)
        )