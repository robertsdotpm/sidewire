"""
Given a message to send on a high level pipe_id and a destination pub key:
create the bytes needed to send to as a publish packet in MQTT to that
channel (dest_pk_hex is the channel.) This function can also be used for
application level ACK replies (msg_type = MsEnum.MSGACK.) So two queues
for each pipe_id_hex based on whether its a message or an ack for a msg.

topic (66 dest ecdsa pub key)
    66 our_hex_pub_key,
    128 sig over 
        ( 64 pipe_id_hex, 8 seq_no_hex, 2 msg_type_hex msg ... )

messages are queued by:
    [pipe_id_hex][int msg_type][int seq_no] = meta

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

def ordered_ack_send(client, msg, dest_pk_hex, pipe_id_hex, msg_type=MsgEnum.MSG, seq_no=None):
    assert(len(pipe_id_hex) == 64)
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
    if pipe_id_hex not in client.msg_queues[msg_type]:
        client.msg_queues[msg_type][pipe_id_hex] = {}

    # Get seq no
    if seq_no is None:
        seq_no = len(client.msg_queues[msg_type][pipe_id_hex])
    
    print("order send seq no =", seq_no)

    # Use the new class to handle packing, signing, and header construction.
    packet = AppPacket(
        pipe_id_hex=pipe_id_hex,
        seq_no=seq_no,
        msg_type=msg_type,
        msg=msg
    )
    
    # This replaces the manual hex concatenation and signing block.
    out = packet.pack(client)

    # Allow acks to be overwritten for queuing.
    # Since they're reactive to msgs.
    # if msg_type == MsgEnum.MSG:
    assert(seq_no not in client.msg_queues[msg_type][pipe_id_hex])

    # Queue message.
    now = asyncio.get_event_loop().time()
    app_ack = asyncio.Future()
    packet_id, packet_ack = packet_ack_future(client, MQTTEnum.PUBACK)
    
    # Python 3.5+ safety: checking type with isinstance is generally preferred
    assert(isinstance(packet_id, bytes))

    print("using packet id", packet_id)
    client.msg_queues[msg_type][pipe_id_hex][seq_no] = {
        "attempts": 0,
        "dest_pk_hex": dest_pk_hex,
        "seq_no": seq_no,
        "out": out,

        # Application-level ack, different from packet ack future.
        "app_ack": app_ack,
        "packet_ack": packet_ack,
        "updated": 0,
        "created": now,
        "packet_id": packet_id,
    }
    
    # Publish the message as intended.
    # if msg_type == MsgEnum.MSGACK:
    #    await client.publish(dest_pk_hex, out)

    # Caller can await ack if they want.
    return out, app_ack