from ecdsa import VerifyingKey, SECP256k1, util
from aionetiface import *

class AppPacket:
    def __init__(self, src_pk_hex=None, sig_hex=None, queue_id_hex=None,
            seq_no=None, timestamp=None, msg_type=None, msg=None):
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
        """
        Equivalent to the packing logic in ordered_ack_send.
        Constructs the full hex string to be sent over the wire.
        """
        if len(self.queue_id_hex) != 64:
            raise ValueError(f"queue_id_hex must be 64 hex chars, got {len(self.queue_id_hex)}")

        # Stamp creation time once; retransmissions reuse the same packed bytes
        # so the timestamp is always the original send time.
        if self.timestamp is None:
            self.timestamp = int(client.get_time())

        # Prepend application-level header to message portion.
        # msg_type is converted to hex (2 chars)
        type_as_bytes = bytes([self.msg_type])
        type_hex = to_h(type_as_bytes)
        headered_msg = type_hex + self.msg

        # Signed message section: queue_id(64) + seq(8) + timestamp(16) + type+msg
        signed_msg = self.queue_id_hex + self.seq_no_hex + self.timestamp_hex + headered_msg

        # Sign the binary representation of the hex string
        signed_msg_bytes = to_b(signed_msg)
        sig = client.kp.private_key.sign(
            signed_msg_bytes,
            sigencode=util.sigencode_string
        )

        self.sig_hex = to_h(sig)
        compact_pk = client.kp.compact_public_key
        self.src_pk_hex = to_h(compact_pk)

        # Full proto message to send.
        # Layout: src_pk(66) + sig(128) + queue_id(64) + seq(8) + timestamp(16) + headered_msg
        out = self.src_pk_hex + self.sig_hex + signed_msg
        return out

    @classmethod
    def unpack(cls, payload):
        """
        Equivalent to the parsing logic in handle_publish.
        Validates signatures and returns an instance of AppPacket.
        """
        # Layout: [src_pk(66)][sig(128)][signed_data...]
        try:
            src_pk_hex = payload[:66]
            sig_hex = payload[66:194]
            sig_bytes = h_to_b(sig_hex)
            signed_msg_hex = payload[194:]
            signed_msg_bytes = to_b(signed_msg_hex)
            
            # Verify ECDSA Signature
            vk_bytes = h_to_b(src_pk_hex)
            vk = VerifyingKey.from_string(vk_bytes, curve=SECP256k1)
            
            vk.verify(
                sig_bytes, 
                signed_msg_bytes, 
                sigdecode=util.sigdecode_string
            )
        except Exception:
            # Common failure point if keys or signatures are malformed.
            return None

        # Route to Application Logic
        # msg_data Layout: [queue_id(64)][seq(8)][timestamp(16)][type(2)][msg...]
        queue_id_hex = signed_msg_hex[:64]
        seq_no_hex = signed_msg_hex[64:72]
        timestamp_hex = signed_msg_hex[72:88]
        app_payload = signed_msg_hex[88:]

        # Extract message type and actual content
        msg_type_hex = app_payload[:2]
        msg_type_bytes = h_to_b(msg_type_hex)
        msg_type = msg_type_bytes[0]
        actual_msg = app_payload[2:]

        # Convert hex fields back to integers
        seq_no_int = int(seq_no_hex, 16)
        timestamp_int = int(timestamp_hex, 16)
        return cls(
            src_pk_hex=src_pk_hex,
            sig_hex=sig_hex,
            queue_id_hex=queue_id_hex,
            seq_no=seq_no_int,
            timestamp=timestamp_int,
            msg_type=msg_type,
            msg=actual_msg
        )