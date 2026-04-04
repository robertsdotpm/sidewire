from aionetiface import *

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
    packet_id = client.get_packet_id()
    packet_ack = asyncio.Future()
    client.packet_ids[packet_type][packet_id] = packet_ack
    return packet_id, packet_ack

# No three-level nesting for all messages.
def iter_all_messages(msg_queues):
    # Queues are split by app msg type: MSG or MSGACK.
    for msg_type in msg_queues:
        # Each plugin instance has a unique pipe_id.
        for pipe_id_hex in list(msg_queues[msg_type].keys()):
            # Messages are further indexed by sequence (order.)
            queue = msg_queues[msg_type][pipe_id_hex]
            for seq_no in sorted(queue.keys()):
                yield queue[seq_no]

async def send_mqtt_puback(client, packet_id):
    """Sends the 4-byte MQTT PUBACK to the broker."""
    ack_packet = bytes([0x40, 0x02]) + packet_id
    await client.pipe.send(ack_packet)

