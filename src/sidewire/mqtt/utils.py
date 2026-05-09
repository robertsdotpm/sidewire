import struct
import asyncio
from aionetiface import to_b
from .mqtt_defs import MsgEnum


def mqtt_encode_varint(value):
    """Encode an integer as a MQTT variable-length integer (up to 4 bytes)."""
    encoded = bytearray()
    while True:
        byte = value % 128
        value //= 128
        if value > 0:
            byte |= 0x80
        encoded.append(byte)
        if value == 0:
            break

    return bytes(encoded)


def mqtt_decode_varint(buf, offset):
    """Decode a MQTT variable-length integer from buf at offset.

    Returns (value, bytes_consumed). Raises ValueError("buffer too short")
    if the buffer truncates before the terminator bit is seen, and
    ValueError("malformed varint") if the 4-byte limit is exceeded.
    """
    multiplier = 1
    value = 0
    consumed = 0

    for i in range(4):  # max 4 bytes
        if offset + i >= len(buf):
            raise ValueError("buffer too short")

        byte = buf[offset + i]
        consumed += 1
        value += (byte & 0x7F) * multiplier
        if (byte & 0x80) == 0:
            return value, consumed

        multiplier *= 128

    raise ValueError("malformed varint")


def mqtt_enc_str(s):
    """Encode a string as MQTT UTF-8 encoded string (2-byte length prefix + UTF-8 bytes)."""
    b = to_b(s)
    return struct.pack("!H", len(b)) + b


def packet_ack_future(client, packet_type):
    """Allocate a new packet ID and register a Future to be resolved on ACK receipt."""
    packet_id = get_packet_id(client)
    packet_ack = asyncio.Future()
    client.packet_ids[packet_type][packet_id] = packet_ack
    return packet_id, packet_ack


# No three-level nesting for all messages.


def iter_all_messages(msg_queues):
    """Yield every queued message across all types, queue IDs, and sequence numbers."""
    # Queues are split by app msg type: MSG or MSGACK.
    for msg_type in msg_queues:
        # Each plugin instance has a unique queue_id.
        for queue_id_hex in list(msg_queues[msg_type].keys()):
            # Messages are further indexed by sequence (order.)
            queue = msg_queues[msg_type][queue_id_hex]
            for seq_no in sorted(queue.keys()):
                yield queue[seq_no]


# Resets protocol-level state for a fresh connection.


def reset_session_state(client):
    """Reset all protocol-level state on the client for a fresh connection."""
    # Clear the stream buffer
    client.buf = b""

    # Cancel and clear protocol-level ACKs (PUBACK/SUBACK)
    for packet_type in client.packet_ids:
        for seq_no in client.packet_ids[packet_type]:
            future = client.packet_ids[packet_type][seq_no]
            if not future.done():
                future.set_exception(ConnectionError("Connection lost for packet ACK"))

        # Fresh list of type id futures.
        client.packet_ids[packet_type] = {}

    # Reset packet ID counter for the new pipe
    client.packet_id = 0


# Simple increasing packet ID with uniqueness checks.
# Avoids zero which is an invalid packet ID.


def get_packet_id(client):
    """Return the next available 2-byte MQTT packet ID that is not currently in-flight."""
    for _ in range(65535):
        client.packet_id = (client.packet_id % 65535) + 1
        pid = struct.pack(">H", client.packet_id)
        if not any(pid in client.packet_ids[pt] for pt in client.packet_ids):
            return pid
    raise AssertionError("All 65535 MQTT packet IDs are in-flight")


# Mostly used to test ping resps are received.


async def blank_ping_handler(self):
    """No-op ping handler used as a default when no ping response processing is needed."""


def prune_msg_ids(client, now):
    """Remove expired message IDs from the client's sent and received ID tables."""
    ttl = client.republish_duration * 2
    for id_table in (
        client.recv_msg_ids,
        client.sent_msg_ids,
    ):
        for msg_id, created_time in list(id_table.items()):
            if (now - created_time) > ttl:
                del id_table[msg_id]


def get_msg_from_queue(
    client, queue_id_hex, seq_no, queue_type=MsgEnum.MSGACK
):
    """Return the queued message for queue_id_hex/seq_no, or None if not found."""
    queue = client.msg_queues[queue_type]
    if queue_id_hex not in queue:
        return None

    sub_queue = queue[queue_id_hex]
    if seq_no not in sub_queue:
        return None

    return sub_queue[seq_no]
