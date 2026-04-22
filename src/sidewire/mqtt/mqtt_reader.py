from typing import Any
from aionetiface import async_wrap_errors
from .mqtt_packet import mqtt_decode_varint, mqtt_parse_packet
from .mqtt_proto import handle_mqtt_packet


# TCP streaming protocol handler for MQTT.
async def mqtt_packet_reader(
    client: Any, chunk: bytes, client_tup: Any, pipe: Any
) -> None:
    # Nothing received, end.
    if not chunk:
        return

    # Append incoming data to buffer.
    client.buf += chunk

    # Process as many complete packets as possible.
    while client.buf:
        # Need at least fixed header + 1 byte of remaining length.
        if len(client.buf) < 2:
            return

        # Decode remaining length (starts at byte 1).
        rem_len, consumed = mqtt_decode_varint(client.buf, 1)
        if rem_len is None:
            return

        # Total packet size = fixed header (1) + varint + payload.
        total_len = 1 + consumed + rem_len

        # Wait for full packet.
        if len(client.buf) < total_len:
            return

        # Extract full packet.
        pkt_buf = client.buf[:total_len]
        client.buf = client.buf[total_len:]

        # Parse + handle.
        pkt = mqtt_parse_packet(pkt_buf)
        await async_wrap_errors(handle_mqtt_packet(client, pkt))
