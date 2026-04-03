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