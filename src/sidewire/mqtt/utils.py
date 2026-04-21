import struct
import asyncio
from aionetiface import *
from .mqtt_defs import *

def mqtt_encode_varint(value):
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
    multiplier = 1
    value = 0
    consumed = 0

    for i in range(4):  # max 4 bytes
        if offset + i >= len(buf):
            return None, None

        byte = buf[offset + i]
        consumed += 1
        value += (byte & 0x7F) * multiplier
        if (byte & 0x80) == 0:
            return value, consumed

        multiplier *= 128

    raise ValueError("Malformed Remaining Length")

def mqtt_enc_str(s):
    b = to_b(s)
    return struct.pack("!H", len(b)) + b

def packet_ack_future(client, packet_type):
    packet_id = get_packet_id(client)
    packet_ack = asyncio.Future()
    client.packet_ids[packet_type][packet_id] = packet_ack
    return packet_id, packet_ack

# No three-level nesting for all messages.
def iter_all_messages(msg_queues):
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
    # Clear the stream buffer
    client.buf = b""

    # Cancel and clear protocol-level ACKs (PUBACK/SUBACK)
    for packet_type in client.packet_ids:
        for seq_no in client.packet_ids[packet_type]:
            future = client.packet_ids[packet_type][seq_no]
            if not future.done():
                future.set_exception(
                    ConnectionError("Connection lost for packet ACK")
                )

        # Fresh list of type id futures.
        client.packet_ids[packet_type] = {}

    # Reset packet ID counter for the new pipe
    client.packet_id = 0

# Simple increasing packet ID with uniqueness checks.
# Avoids zero which is an invalid packet ID.
def get_packet_id(client):
    for _ in range(65535):
        client.packet_id = (client.packet_id % 65535) + 1
        pid = struct.pack(">H", client.packet_id)
        if not any(pid in client.packet_ids[pt] for pt in client.packet_ids):
            return pid
    raise RuntimeError("All 65535 MQTT packet IDs are in-flight")
        
# Mostly used to test ping resps are received.
async def blank_ping_handler(self):
    pass

def prune_msg_ids(client, now):
    ttl = client.republish_duration * 2
    for id_table in (client.recv_msg_ids, client.sent_msg_ids,):
        for msg_id, created_time in list(id_table.items()):
            if (now - created_time) > ttl:
                del id_table[msg_id]

def get_msg_from_queue(client, queue_id_hex, seq_no, queue_type=MsgEnum.MSGACK):
    queue = client.msg_queues[queue_type]
    if queue_id_hex not in queue:
        return None
    
    sub_queue = queue[queue_id_hex]
    if seq_no not in sub_queue:
        return None
    
    return sub_queue[seq_no]