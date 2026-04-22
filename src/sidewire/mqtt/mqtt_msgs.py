import struct
from typing import Optional, Tuple, Callable
from aionetiface import to_b
from .mqtt_defs import MQTTEnum
from .utils import mqtt_encode_varint, mqtt_enc_str
from .mqtt_packet import MQTTPacket


def build_connect(client_id: str, keep_alive: int = 60) -> bytes:
    """Build a MQTT CONNECT packet for the given client ID and keep-alive interval."""
    # proto name, proto level, clean session, keep alive 60s
    vh = mqtt_enc_str("MQTT") + b"\x04" + b"\x02" + struct.pack("!H", keep_alive)
    pl = mqtt_enc_str(client_id)

    # Full packet to send.
    pkt = b"\x10" + mqtt_encode_varint(len(vh) + len(pl)) + vh + pl
    return pkt


def build_subscribe(topic: str, packet_id: bytes) -> bytes:
    """Build a MQTT SUBSCRIBE packet for a single topic at QoS 1."""
    vh = packet_id
    pl = mqtt_enc_str(topic) + b"\x01"  # QoS 1
    pkt = b"\x82" + mqtt_encode_varint(len(vh) + len(pl)) + vh + pl
    return pkt


def build_publish(
    topic: str, payload: bytes, packet_id: bytes, dup: bool = False
) -> bytes:
    """Build a MQTT PUBLISH packet at QoS 1, optionally with the DUP flag set."""
    topic_bytes = mqtt_enc_str(topic)
    pl = topic_bytes + packet_id + to_b(payload)

    # Base: PUBLISH + QoS1
    header = 0x30 | (1 << 1)  # 0x32
    if dup:
        header |= 0x08  # set DUP bit

    pkt = bytes([header]) + mqtt_encode_varint(len(pl)) + pl
    return pkt


def build_ping(
    last_ping: float, keep_alive: int, get_time: Callable[[], float]
) -> Tuple[float, Optional[bytes]]:
    """Return (now, PINGREQ bytes) if keep-alive interval has elapsed, else (last_ping, None)."""
    now = get_time()
    if now - last_ping >= keep_alive:
        req = MQTTPacket(MQTTEnum.PINGREQ)
        buf = req.build()
        return now, buf

    return last_ping, None


def build_puback(packet_id: bytes) -> bytes:
    """Build a MQTT PUBACK packet acknowledging the given packet ID."""
    buf = bytes([0x40, 0x02]) + packet_id
    return buf


def build_disconnect() -> bytes:
    """Build a MQTT DISCONNECT packet."""
    buf = bytes([0xE0, 0x00])
    return buf
