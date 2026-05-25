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
import hashlib
from aionetiface import log, to_b
from .mqtt_defs import MsgEnum
from .app_packet import AppPacket


def ordered_ack_send(
    client,
    msg,
    dest_pk_hex,
    queue_id_hex,
    msg_type=MsgEnum.MSG,
    seq_no=None,
):
    """Queue a signed application message for ordered delivery to dest_pk_hex.

    Returns a (queue_id_hex, seq_no) key and a Future resolved when the
    remote peer sends an application-level ACK.
    """
    if len(queue_id_hex) != 64:
        raise ValueError("queue_id_hex must be 64 hex chars, got {}".format(len(queue_id_hex)))
    if len(dest_pk_hex) != 66:
        raise ValueError("dest_pk_hex must be 66 hex chars, got {}".format(len(dest_pk_hex)))

    # Duplicate messages may be a bug by the caller.
    if msg_type == MsgEnum.MSG:
        sent_msg_id = hashlib.sha256(to_b(msg)).hexdigest()
        if sent_msg_id in client.sent_msg_ids:
            er = "msg id in sent msg ids in ordered send, is this intended? "
            er += sent_msg_id
            log(er)
        else:
            client.sent_msg_ids[sent_msg_id] = client.get_time()

    # Create queue per pipe ID.
    if queue_id_hex not in client.msg_queues[msg_type]:
        client.msg_queues[msg_type][queue_id_hex] = {}

    # Ensure no collisions for new messages. Use max+1 rather than
    # len so a dequeue_msg call that deletes an entry doesn't produce
    # a seq_no that collides with the already-resolved entry.
    if seq_no is None:
        existing = client.msg_queues[msg_type][queue_id_hex]
        seq_no = (max(existing.keys()) + 1) if existing else 0

    # Pack the message using AppPacket (handles signing and header construction).
    packet = AppPacket(
        queue_id_hex=queue_id_hex, seq_no=seq_no, msg_type=msg_type, msg=msg
    )

    # This replaces the manual hex concatenation and signing block.
    out = packet.pack(client)

    # If already queued (e.g. duplicate delivery or concurrent MSG+PROBE for
    # the same queue_id/seq_no), return the existing future rather than
    # overwriting or asserting -- callers can safely await it either way.
    if seq_no in client.msg_queues[msg_type][queue_id_hex]:
        existing = client.msg_queues[msg_type][queue_id_hex][seq_no]
        return (queue_id_hex, seq_no), existing["app_ack"]

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
        "created": client.get_time(),
    }

    # Caller can await ack if they want.
    return (queue_id_hex, seq_no), app_ack


async def ordered_ack_send_async(
    client,
    msg,
    dest_pk_hex,
    queue_id_hex,
    msg_type=MsgEnum.MSG,
    seq_no=None,
):
    """Async variant of ordered_ack_send that runs the ECDSA sign on
    the default thread pool executor via AppPacket.pack_async.

    Use this from `async def` callers (e.g. process_app_msg's per-
    message ACK send) to keep the event loop free for ~2-10ms per
    publish that would otherwise be spent signing.

    Same return shape: ((queue_id_hex, seq_no), app_ack_future).
    """
    if len(queue_id_hex) != 64:
        raise ValueError("queue_id_hex must be 64 hex chars, got {}".format(len(queue_id_hex)))
    if len(dest_pk_hex) != 66:
        raise ValueError("dest_pk_hex must be 66 hex chars, got {}".format(len(dest_pk_hex)))

    if msg_type == MsgEnum.MSG:
        sent_msg_id = hashlib.sha256(to_b(msg)).hexdigest()
        if sent_msg_id in client.sent_msg_ids:
            er = "msg id in sent msg ids in ordered send (async), is this intended? "
            er += sent_msg_id
            log(er)
        else:
            client.sent_msg_ids[sent_msg_id] = client.get_time()

    if queue_id_hex not in client.msg_queues[msg_type]:
        client.msg_queues[msg_type][queue_id_hex] = {}

    if seq_no is None:
        existing = client.msg_queues[msg_type][queue_id_hex]
        seq_no = (max(existing.keys()) + 1) if existing else 0

    packet = AppPacket(
        queue_id_hex=queue_id_hex, seq_no=seq_no, msg_type=msg_type, msg=msg
    )
    # The only behavioural difference vs ordered_ack_send: pack_async
    # offloads the ECDSA sign to the default thread pool executor.
    out = await packet.pack_async(client)

    if seq_no in client.msg_queues[msg_type][queue_id_hex]:
        existing = client.msg_queues[msg_type][queue_id_hex][seq_no]
        return (queue_id_hex, seq_no), existing["app_ack"]

    app_ack = asyncio.Future()
    client.msg_queues[msg_type][queue_id_hex][seq_no] = {
        "app_ack": app_ack,
        "dest_pk_hex": dest_pk_hex,
        "seq_no": seq_no,
        "out": out,
        "updated": 0,
        "created": client.get_time(),
    }

    return (queue_id_hex, seq_no), app_ack
