import struct
from ecdsa import VerifyingKey, SECP256k1, util, BadSignatureError, MalformedPointError
from aionetiface import h_to_b, to_b, to_h, to_s


# Fixed binary sizes (bytes) for AppPacket wire format.
# src_pk: 33 bytes (compressed SECP256k1 key)
# sig:    64 bytes (fixed-width ECDSA signature)
# queue_id: 32 bytes (sha256 digest)
# seq_no: 4 bytes big-endian unsigned
# timestamp: 8 bytes big-endian unsigned
# msg_type: 1 byte unsigned
# msg: remaining bytes (length implicit from buffer)
SRC_PK_LEN = 33
SIG_LEN = 64
QUEUE_ID_LEN = 32
SEQ_NO_LEN = 4
TIMESTAMP_LEN = 8
MSG_TYPE_LEN = 1

# Offsets within the packed wire buffer.
SIG_OFFSET = SRC_PK_LEN
SIGNED_MSG_OFFSET = SRC_PK_LEN + SIG_LEN
QUEUE_ID_OFFSET = 0
SEQ_NO_OFFSET = QUEUE_ID_OFFSET + QUEUE_ID_LEN
TIMESTAMP_OFFSET = SEQ_NO_OFFSET + SEQ_NO_LEN
MSG_TYPE_OFFSET = TIMESTAMP_OFFSET + TIMESTAMP_LEN
MSG_OFFSET = MSG_TYPE_OFFSET + MSG_TYPE_LEN

# Minimum valid packet size: src_pk + sig + queue_id + seq_no + timestamp + msg_type.
MIN_PACKET_LEN = SIGNED_MSG_OFFSET + MSG_OFFSET


class AppPacket:
    """Represent a signed application-level MQTT message packet."""

    def __init__(
        self,
        src_pk_hex=None,
        sig_hex=None,
        queue_id_hex=None,
        seq_no=None,
        timestamp=None,
        msg_type=None,
        msg=None,
    ):
        """Initialize an AppPacket with optional field values."""
        self.src_pk_hex = src_pk_hex
        self.sig_hex = sig_hex
        self.queue_id_hex = queue_id_hex
        self.seq_no = seq_no
        self.timestamp = timestamp
        self.msg_type = msg_type
        self.msg = msg

    @property
    def seq_no_hex(self):
        """Formats the integer sequence number into an 8-char hex string."""
        if self.seq_no is None:
            return None
        return "{:08x}".format(self.seq_no)

    @property
    def timestamp_hex(self):
        """Formats the unix timestamp into a 16-char hex string."""
        if self.timestamp is None:
            return None
        return "{:016x}".format(self.timestamp)

    def pack(self, client):
        """Serialize the signed packet to bytes using fixed-width binary framing."""
        if len(self.queue_id_hex) != 64:
            raise ValueError(
                "queue_id_hex must be 64 hex chars, got {}".format(len(self.queue_id_hex))
            )

        # Stamp creation time once; retransmissions reuse the same packed bytes
        # so the timestamp is always the original send time.
        if self.timestamp is None:
            self.timestamp = int(client.get_time())

        queue_id_bytes = h_to_b(self.queue_id_hex)
        msg_bytes = to_b(self.msg) if self.msg else b""

        # Signed section: queue_id(32) + seq_no(4) + timestamp(8) + msg_type(1) + msg.
        signed_msg = (
            queue_id_bytes
            + struct.pack("!I", self.seq_no)
            + struct.pack("!Q", self.timestamp)
            + struct.pack("!B", self.msg_type)
            + msg_bytes
        )

        # Sign the binary representation.
        sig = client.kp.private_key.sign(signed_msg, sigencode=util.sigencode_string)
        self.sig_hex = to_h(sig)
        compact_pk = client.kp.compact_public_key
        self.src_pk_hex = to_h(compact_pk)

        # Full wire layout: src_pk(33) + sig(64) + signed_msg.
        return compact_pk + sig + signed_msg

    @classmethod
    def unpack(cls, payload):
        """Parse and verify a packed AppPacket wire buffer, returning None on any error."""
        if not isinstance(payload, (bytes, bytearray)):
            return None
        if len(payload) < MIN_PACKET_LEN:
            return None

        try:
            src_pk_bytes = bytes(payload[:SRC_PK_LEN])
            sig_bytes = bytes(payload[SIG_OFFSET:SIGNED_MSG_OFFSET])
            signed_msg = bytes(payload[SIGNED_MSG_OFFSET:])

            # Verify ECDSA signature.
            vk = VerifyingKey.from_string(src_pk_bytes, curve=SECP256k1)
            vk.verify(sig_bytes, signed_msg, sigdecode=util.sigdecode_string)
        except (BadSignatureError, MalformedPointError, ValueError):
            # Common failure point if keys or signatures are malformed.
            return None

        # Parse the signed section.
        queue_id_bytes = signed_msg[QUEUE_ID_OFFSET:QUEUE_ID_OFFSET + QUEUE_ID_LEN]
        seq_no_bytes = signed_msg[SEQ_NO_OFFSET:SEQ_NO_OFFSET + SEQ_NO_LEN]
        timestamp_bytes = signed_msg[TIMESTAMP_OFFSET:TIMESTAMP_OFFSET + TIMESTAMP_LEN]
        msg_type_byte = signed_msg[MSG_TYPE_OFFSET:MSG_TYPE_OFFSET + MSG_TYPE_LEN]
        msg_bytes = signed_msg[MSG_OFFSET:]

        seq_no = struct.unpack("!I", seq_no_bytes)[0]
        timestamp = struct.unpack("!Q", timestamp_bytes)[0]
        msg_type = struct.unpack("!B", msg_type_byte)[0]

        return cls(
            src_pk_hex=to_h(src_pk_bytes),
            sig_hex=to_h(sig_bytes),
            queue_id_hex=to_h(queue_id_bytes),
            seq_no=seq_no,
            timestamp=timestamp,
            msg_type=msg_type,
            msg=to_s(msg_bytes),
        )
