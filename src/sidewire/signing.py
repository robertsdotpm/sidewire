from ecdsa import VerifyingKey, SECP256k1, SigningKey, util
from aionetiface import *

class Signing():
    def __init__(self, priv=None, pub=None, curve=SECP256k1):
        self.curve = curve
        self.private_key = priv
        self.public_key = pub or priv.get_verifying_key()
        self.compact_public_key = self.public_key.to_string("compressed")
        self.public_key_hex = to_hs(self.compact_public_key)

    @staticmethod
    def keypair(curve=SECP256k1):
        priv = SigningKey.generate(curve=curve)
        pub = priv.get_verifying_key()
        return Signing(priv, pub, curve)

if __name__ == "__main__":
    kp = Signing.keypair()
    print(kp.private_key)
    print(kp.compact_public_key)

    msg = b"my test msg."
    sig = kp.private_key.sign(msg, sigencode=util.sigencode_string)
    print(len(sig))
    print(kp.public_key.verify(sig, msg, sigdecode=util.sigdecode_string))
    print(len(kp.compact_public_key))