import struct
from aionetiface import to_s
from .mqtt_defs import MQTTEnum
from .utils import mqtt_decode_varint, mqtt_encode_varint, mqtt_enc_str


class MQTTPacket:
    """Represent a single MQTT control packet with header and body."""

    def __init__(self, packet_type, flags=0):
        """Initialize an MQTTPacket with the given type and flags."""
        self.type = MQTTEnum(packet_type)
        self.flags = flags
        self.variable_header = b""
        self.payload = b""
        self.body = b""

    def set_variable_header(self, data):
        """Set the variable header from an int (packed as big-endian short) or bytes."""
        if isinstance(data, int):
            self.variable_header = struct.pack("!H", data)
        else:
            self.variable_header = data

    def set_payload(self, data):
        """Set the packet payload bytes."""
        self.payload = data

    def set_packet_id(self, packet_id):
        """Set the variable header to a big-endian encoded packet ID."""
        self.variable_header = struct.pack("!H", packet_id)

    def build(self):
        """Serialize the packet to bytes including fixed header and body."""
        body = self.variable_header + self.payload
        fixed = mqtt_build_header(len(body), self.type, self.flags)

        return fixed + body


def mqtt_parse_packet(raw):
    """Parse raw bytes into an MQTTPacket, leaving the body unparsed."""
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
    body = raw[offset : offset + remaining_length]

    pkt = MQTTPacket(packet_type, flags)

    # Do NOT attempt to interpret variable header or payload here
    pkt.body = body
    return pkt


def mqtt_build_header(remaining_length, packet_type, flags):
    """Build the MQTT fixed header bytes for the given packet type, flags, and remaining length."""
    packet_type_part = int(packet_type) << 4
    flags_part = flags & 0x0F
    first_byte = packet_type_part | flags_part
    return bytes([first_byte]) + mqtt_encode_varint(remaining_length)


def mqtt_parse_publish(
    packet,
):
    """Parse a PUBLISH packet body into (topic, payload, packet_id); returns None on malformed input."""
    body = packet.body
    offset = 0

    # Topic length
    if len(body) < 2:
        return None

    # We still need to unpack the length to know how far to read the topic
    tlen = struct.unpack("!H", body[offset : offset + 2])[0]
    offset += 2

    # Topic
    if len(body) < offset + tlen:
        return None

    topic = to_s(body[offset : offset + tlen])
    offset += tlen

    # QoS > 0 => 2-byte packet identifier is present
    qos = (packet.flags >> 1) & 0x03
    packet_id = None

    if qos > 0:
        if len(body) < offset + 2:
            return None

        # Sliced directly as bytes (2 bytes) instead of unpacking to int
        packet_id = body[offset : offset + 2]
        offset += 2

    # Payload (remaining raw bytes — caller handles framing).
    payload = bytes(body[offset:])
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
