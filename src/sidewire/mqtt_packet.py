import struct
from enum import IntEnum
from aionetiface import *

def mqtt_enc_str(s):
    b = to_b(s)
    return struct.pack("!H", len(b)) + b

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

def mqtt_build_header(remaining_length, packet_type, flags):
    packet_type_part = int(packet_type) << 4
    flags_part = flags & 0x0F
    first_byte = packet_type_part | flags_part
    return (
        bytes([first_byte]) +
        mqtt_encode_varint(remaining_length)
    )

class MQTTEnum(IntEnum):
    CONNECT = 1
    CONNACK = 2
    PUBLISH = 3
    PUBACK = 4
    PUBREC = 5
    PUBREL = 6
    PUBCOMP = 7
    SUBSCRIBE = 8
    SUBACK = 9
    UNSUBSCRIBE = 10
    UNSUBACK = 11
    PINGREQ = 12
    PINGRESP = 13
    DISCONNECT = 14

class MQTTPacket:
    def __init__(self, packet_type, flags=0):
        self.type = MQTTEnum(packet_type)
        self.flags = flags
        self.variable_header = b""
        self.payload = b""

    def set_variable_header(self, data):
        if isinstance(data, int):
            self.variable_header = struct.pack("!H", data)
        else:
            self.variable_header = data

    def set_payload(self, data):
        self.payload = data

    def set_packet_id(self, packet_id):
        self.variable_header = struct.pack("!H", packet_id)

    def build(self):
        body = self.variable_header + self.payload
        fixed = mqtt_build_header(
            len(body), # Total packet len.
            self.type, 
            self.flags
        )

        """
            b (Fixed header + flags),
            varint (total len),
            optional header: (packet-specific, e.g. packet id),
            payload bytes ...
        """
        return fixed + body
    
    def debug_print(self):
        print("mqtt pkt debug print = ")
        print(self.type)
        print(self.payload)

def mqtt_parse_publish(packet):
    body = packet.body
    offset = 0

    # Topic length
    if len(body) < 2:
        return None

    tlen = struct.unpack("!H", body[offset:offset + 2])[0]
    offset += 2

    # Topic
    if len(body) < offset + tlen:
        return None

    topic = to_s(body[offset:offset + tlen])
    offset += tlen

    # QoS > 0 => packet identifier present
    qos = (packet.flags >> 1) & 0x03
    packet_id = None

    if qos > 0:
        if len(body) < offset + 2:
            return None
        packet_id = struct.unpack("!H", body[offset:offset + 2])[0]
        offset += 2

    # Payload
    payload = to_s(body[offset:])

    return topic, payload, packet_id
    
def mqtt_parse_packet(raw):
    offset = 0

    # Fixed header byte
    first_byte = raw[offset]
    offset += 1

    packet_type = first_byte >> 4
    flags = first_byte & 0x0F

    # Remaining Length (varint)
    remaining_length, consumed = mqtt_decode_varint(raw, offset)
    offset += consumed

    # Extract full body (variable header + payload, unparsed)
    body = raw[offset:offset + remaining_length]

    pkt = MQTTPacket(packet_type, flags)

    # Do NOT attempt to interpret variable header or payload here
    pkt.body = body

    return pkt

    
if __name__ == "__main__":
    topic = "test/topic"
    packet_id = 1

    pkt = MQTTPacket(MQTTEnum.SUBSCRIBE, flags=0x02)

    # Variable header: Packet Identifier
    pkt.set_packet_id(packet_id)

    # Payload: Topic filter + QoS
    payload = mqtt_enc_str(topic) + b"\x01"  # QoS 1
    pkt.set_payload(payload)

    raw = pkt.build()

    pkt2 = mqtt_parse_packet(raw)
    print(pkt2)
    print(pkt2.packet_type)
    print(pkt2.payload)