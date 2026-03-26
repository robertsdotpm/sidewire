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

def get_unique_packet_id(packet_ids, packet_type):
    for _ in range(0, 10000):
        packet_id = rand_b(2)
        if packet_id not in packet_ids[packet_type]:
            return packet_id
        
    raise Exception("Could not get unique packet id")

def packet_ack_future(packet_ids, packet_type):
    packet_id = get_unique_packet_id(packet_ids, packet_type)
    packet_ack = asyncio.Future()
    packet_ids[packet_type][packet_id] = packet_ack
    return packet_id, packet_ack