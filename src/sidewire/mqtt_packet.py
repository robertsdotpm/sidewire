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

def mqtt_decode_varint(data, offset):
    multiplier = 1
    value = 0
    while True:
        byte = data[offset]
        offset += 1

        value += (byte & 127) * multiplier
        if (byte & 128) == 0:
            break

        multiplier *= 128

    return value, offset

def mqtt_build_header(remaining_length, packet_type, flags):
    packet_type_part = int(packet_type) << 4
    flags_part = flags & 0x0F
    first_byte = packet_type_part | flags_part
    return (
        bytes([first_byte]) +
        mqtt_encode_varint(remaining_length)
    )

class MQTTPacketType(IntEnum):
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
        self.packet_type = MQTTPacketType(packet_type)
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
            self.packet_type, 
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
        print(self.packet_type)
        print(self.payload)
    
def mqtt_parse_packet(raw):
    offset = 0

    first_byte = raw[offset]
    offset += 1

    packet_type = first_byte >> 4
    flags = first_byte & 0x0F

    remaining_length, offset = mqtt_decode_varint(raw, offset)

    body = raw[offset:offset + remaining_length]

    pkt = MQTTPacket(packet_type, flags)

    # NOTE: Generic split assumption:
    # Many packets have a 2-byte packet id at start of variable header.
    # For simplicity, we treat first 2 bytes as variable header if applicable.
    variable_header = b""
    payload = b""

    if packet_type in MQTTPacketType:
        variable_header = body[:2]
        payload = body[2:]
    else:
        # For other packet types, you’d need type-specific parsing
        variable_header = body
        payload = b""

    pkt.variable_header = variable_header
    pkt.payload = payload

    return pkt

    
if __name__ == "__main__":
    topic = "test/topic"
    packet_id = 1

    pkt = MQTTPacket(MQTTPacketType.SUBSCRIBE, flags=0x02)

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