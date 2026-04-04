"""
Given a message to send on a high level queue_id and a destination pub key:
create the bytes needed to send to as a publish packet in MQTT to that
channel (dest_pk_hex is the channel.) This function can also be used for
application level ACK replies (msg_type = MsEnum.MSGACK.) So two queues
for each queue_id_hex based on whether its a message or an ack for a msg.

topic (66 dest ecdsa pub key)
    66 our_hex_pub_key,
    128 sig over 
        ( 64 queue_id_hex, 8 seq_no_hex, 2 msg_type_hex msg ... )

messages are queued by:
    [queue_id_hex][int msg_type][int seq_no] = meta

meta:
    msg
    dest pub key
    etc
"""

import asyncio
from aionetiface import *
from .mqtt_defs import *
from .utils import *
from ..signing import *
from .mqtt_packet import *
from .mqtt_proto import *
from .app_packet import *

def ordered_ack_send(client, msg, dest_pk_hex, queue_id_hex, msg_type=MsgEnum.MSG, seq_no=None):
    assert(len(queue_id_hex) == 64)
    assert(len(dest_pk_hex) == 66)

    # Duplicate messages may be a bug by the caller.
    if msg_type == MsgEnum.MSG:
        sent_msg_id = hashlib.sha256(to_b(msg)).hexdigest()
        if sent_msg_id in client.sent_msg_ids:
            er = "msg id in sent msg ids in ordered send, is this intended? "
            er += sent_msg_id
            log(er)
        else:
            client.sent_msg_ids[sent_msg_id] = 1

    # Create queue per pipe ID.
    if queue_id_hex not in client.msg_queues[msg_type]:
        client.msg_queues[msg_type][queue_id_hex] = {}

    # Ensure no collisions for new messages.
    if seq_no is None:
        seq_no = len(client.msg_queues[msg_type][queue_id_hex])
    
    print("order send seq no =", seq_no)

    # Use the new class to handle packing, signing, and header construction.
    packet = AppPacket(
        queue_id_hex=queue_id_hex,
        seq_no=seq_no,
        msg_type=msg_type,
        msg=msg
    )
    
    # This replaces the manual hex concatenation and signing block.
    out = packet.pack(client)

    # Otherwise we'll overwrite an existing meta section.
    assert(seq_no not in client.msg_queues[msg_type][queue_id_hex])

    # Message is queued by application type, high level pipe id, then seq.
    app_ack = asyncio.Future()
    client.msg_queues[msg_type][queue_id_hex][seq_no] = {
        # Application-level ack -- allows to confirm msg delivery.
        "app_ack": app_ack,

        # Where to send the message to.
        "dest_pk_hex": dest_pk_hex,

        # Allows for sequential send and ordering.
        "seq_no": seq_no,

        # A serialized buffer of an app packet to put into a mqtt publish.
        "out": out,

        # Last updated time at 0.
        "updated": 0,

        # Created now.
        "created": asyncio.get_event_loop().time(),
    }
    
    # Caller can await ack if they want.
    return out, app_ack