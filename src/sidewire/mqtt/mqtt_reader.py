from aionetiface import *
from .mqtt_defs import *
from .utils import *
from ..signing import *
from .mqtt_packet import *
from .mqtt_proto import *

# TCP streaming protocol handler for MQTT.
async def mqtt_packet_reader(client, chunk, client_tup, pipe):
    #print("got chunk = ", chunk)
    if not chunk:
        #print("not chunk")
        return

    # append incoming data
    client.buf += chunk

    # process as many complete packets as possible
    while client.buf:
        # need at least fixed header + 1 byte of remaining length
        if len(client.buf) < 2:
            print("need at least fixed header", client.buf)
            return

        # decode remaining length (starts at byte 1)
        rem_len, consumed = mqtt_decode_varint(client.buf, 1)
        if rem_len is None:
            print("rem len is none", client.buf)
            return

        # total packet size = fixed header (1) + varint + payload
        total_len = 1 + consumed + rem_len

        # wait for full packet
        if len(client.buf) < total_len:
            print("wait for full packet", client.buf)
            return

        # extract packet
        pkt_buf = client.buf[:total_len]
        client.buf = client.buf[total_len:]

        #print("pkt buf = ", pkt_buf)

        # parse + handle
        pkt = mqtt_parse_packet(pkt_buf)
        await async_wrap_errors(
            handle_mqtt_packet(client, pkt)
        )