import asyncio
from aionetiface import *
from .mqtt_defs import *
from .utils import *
from ..signing import *
from .mqtt_packet import *
from .mqtt_proto import *

def ordered_ack_send(client, msg, dest_pk_hex, pipe_id_hex, msg_type=MsgEnum.MSG, seq_no=None):
    assert(len(pipe_id_hex) == 64)
    assert(len(dest_pk_hex) == 66)

    # Prepend application-level header to message portion.
    msg = to_h(bytes([msg_type])) + msg

    # Create queue per pipe ID.
    if pipe_id_hex not in client.msg_queues[msg_type]:
        client.msg_queues[msg_type][pipe_id_hex] = {}

    # Get seq no
    print("seq no =", seq_no)
    if seq_no is None:
        seq_no = len(client.msg_queues[msg_type][pipe_id_hex])
    seq_no_hex = "{:08x}".format(seq_no)

    # Signed message section.
    signed_msg = pipe_id_hex + seq_no_hex + msg
    sig = client.kp.private_key.sign(
        to_b(signed_msg),
        sigencode=util.sigencode_string
    )
    sig_hex = to_h(sig)
    assert(len(sig_hex) == 128)

    # Our public key.
    src_pk_hex = to_h(client.kp.compact_public_key) # 66
    assert(len(src_pk_hex) == 66)

    # Full proto message to send.
    out = src_pk_hex + sig_hex + signed_msg
    assert(type(out) == str)
    assert(len(out) == (266 + len(msg)))

    # Allow acks to be overwritten for queuing.
    # Since they're reactive to msgs.
    #if msg_type == MsgEnum.MSG:
    assert(seq_no not in client.msg_queues[msg_type][pipe_id_hex])

    # Queue message.
    now = asyncio.get_event_loop().time()
    ack_future = asyncio.Future()
    client.msg_queues[msg_type][pipe_id_hex][seq_no] = {
        "attempts": 0,
        "dest_pk_hex": dest_pk_hex,
        "seq_no": seq_no,
        "out": out,
        "acked": ack_future,
        "updated": 0,
        "created": now,
    }
    
    # Publish the message as intended.
    #if msg_type == MsgEnum.MSGACK:
    #    await client.publish(dest_pk_hex, out)

    # Caller can await ack if they want.
    return out, ack_future
