import struct
from aionetiface import *
from .mqtt_defs import *
from .utils import *

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
            len(body),
            self.type, 
            self.flags
        )

        return fixed + body
    
    def debug_print(self):
        return
        print("mqtt pkt debug print = ")
        print(self.type)
        print(self.payload)

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

def mqtt_build_header(remaining_length, packet_type, flags):
    packet_type_part = int(packet_type) << 4
    flags_part = flags & 0x0F
    first_byte = packet_type_part | flags_part
    return (
        bytes([first_byte]) +
        mqtt_encode_varint(remaining_length)
    )

def mqtt_parse_puback(packet):
    """
    Parses a PUBACK packet to extract the 2-byte Packet ID.
    Expected packet.body is usually the 2-byte Variable Header.
    """
    body = packet.body
    
    # A PUBACK variable header must be exactly 2 bytes.
    if len(body) < 2:
        return None

    # Return the raw 2 bytes as the packet_id
    packet_id = body[:2]
    return packet_id

def mqtt_parse_publish(packet):
    body = packet.body
    offset = 0

    # Topic length
    if len(body) < 2:
        return None

    # We still need to unpack the length to know how far to read the topic
    tlen = struct.unpack("!H", body[offset:offset + 2])[0]
    offset += 2

    # Topic
    if len(body) < offset + tlen:
        return None

    topic = to_s(body[offset:offset + tlen])
    offset += tlen

    # QoS > 0 => 2-byte packet identifier is present
    qos = (packet.flags >> 1) & 0x03
    packet_id = None

    if qos > 0:
        if len(body) < offset + 2:
            return None
        
        # Sliced directly as bytes (2 bytes) instead of unpacking to int
        packet_id = body[offset:offset + 2]
        offset += 2

    # Payload (everything remaining)
    payload = to_s(body[offset:])
    return topic, payload, packet_id

    
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