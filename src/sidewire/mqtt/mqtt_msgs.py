import struct
from aionetiface import *
from .mqtt_defs import *
from .utils import *
from .mqtt_packet import *

def build_connect(client_id, keep_alive=60):
    print("mqtt connect")

    # proto name, proto level, clean session, keep alive 60s
    vh = (
        mqtt_enc_str("MQTT") + 
        b"\x04" + 
        b"\x02" + 
        struct.pack("!H", keep_alive)
    )
    pl = mqtt_enc_str(client_id)

    # Full packet to send.
    pkt = b"\x10" + mqtt_encode_varint(len(vh) + len(pl)) + vh + pl
    return pkt

def build_subscribe(topic, packet_id):
    vh = packet_id
    pl = mqtt_enc_str(topic) + b"\x01"  # QoS 1
    pkt = b"\x82" + mqtt_encode_varint(len(vh) + len(pl)) + vh + pl
    return pkt

def build_publish(topic, payload, packet_id, dup=False):
    packet_id = packet_id
    topic_bytes = mqtt_enc_str(topic)
    pl = topic_bytes + packet_id + to_b(payload)

    # Base: PUBLISH + QoS1
    header = 0x30 | (1 << 1)   # 0x32

    if dup:
        header |= 0x08  # set DUP bit

    pkt = bytes([header]) + mqtt_encode_varint(len(pl)) + pl
    return pkt

def build_ping(last_ping, keep_alive):
    """Send ping to server every so often."""
    now = asyncio.get_event_loop().time()
    if now - last_ping >= keep_alive:
        req = MQTTPacket(MQTTEnum.PINGREQ)
        buf = req.build()
        return now, buf
    
    return now, None

def build_puback(packet_id):
    """Sends the 4-byte MQTT PUBACK to the broker."""
    buf = bytes([0x40, 0x02]) + packet_id
    return buf

def build_disconnect():
    buf = bytes([0xE0, 0x00])
    return buf