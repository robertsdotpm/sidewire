import hashlib
from ecdsa import VerifyingKey, SECP256k1, util
from aionetiface import *

class AppPacket:
    def __init__(self, src_pk_hex=None, sig_hex=None, pipe_id_hex=None, 
            seq_no=None, msg_type=None, msg=None):
        self.src_pk_hex = src_pk_hex
        self.sig_hex = sig_hex
        self.pipe_id_hex = pipe_id_hex
        self.seq_no = seq_no
        self.msg_type = msg_type
        self.msg = msg

    @property
    def seq_no_hex(self):
        """Formats the integer sequence number into an 8-char hex string."""
        if self.seq_no is None:
            return None
        
        # Explicit format for Python 3.5+ compatibility
        hex_string = "{:08x}".format(self.seq_no)
        return hex_string

    def pack(self, client):
        """
        Equivalent to the packing logic in ordered_ack_send.
        Constructs the full hex string to be sent over the wire.
        """
        # Validate inputs
        assert(len(self.pipe_id_hex) == 64)
        
        # Prepend application-level header to message portion.
        # msg_type is converted to hex (2 chars)
        type_as_bytes = bytes([self.msg_type])
        type_hex = to_h(type_as_bytes)
        headered_msg = type_hex + self.msg

        # Signed message section.
        signed_msg = self.pipe_id_hex + self.seq_no_hex + headered_msg
        
        # Sign the binary representation of the hex string
        signed_msg_bytes = to_b(signed_msg)
        sig = client.kp.private_key.sign(
            signed_msg_bytes,
            sigencode=util.sigencode_string
        )
        
        self.sig_hex = to_h(sig)
        assert(len(self.sig_hex) == 128)

        # Our public key (66 chars)
        compact_pk = client.kp.compact_public_key
        self.src_pk_hex = to_h(compact_pk)
        assert(len(self.src_pk_hex) == 66)

        # Full proto message to send.
        out = self.src_pk_hex + self.sig_hex + signed_msg
        assert(isinstance(out, str))
        
        # Validation: src_pk(66) + sig(128) + pipe(64) + seq(8) = 266
        header_overhead = 266
        expected_len = header_overhead + len(headered_msg)
        assert(len(out) == expected_len)
        
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
            # Common failure point if keys or signatures are malformed
            print("e: Signature verification failed")
            return None

        # Route to Application Logic
        # msg_data Layout: [pipe_id(64)][seq(8)][type(2)][msg...]
        pipe_id_hex = signed_msg_hex[:64]
        seq_no_hex = signed_msg_hex[64:72]
        app_payload = signed_msg_hex[72:]
        
        # Extract message type and actual content
        msg_type_hex = app_payload[:2]
        msg_type_bytes = h_to_b(msg_type_hex)
        msg_type = msg_type_bytes[0]
        actual_msg = app_payload[2:]

        # Convert hex sequence back to integer
        seq_no_int = int(seq_no_hex, 16)

        return cls(
            src_pk_hex=src_pk_hex,
            sig_hex=sig_hex,
            pipe_id_hex=pipe_id_hex,
            seq_no=seq_no_int,
            msg_type=msg_type,
            msg=actual_msg
        )