import hashlib
from aionetiface import *
from .mqtt_defs import *

# Function called for new application (type=msg) publishes.
async def process_app_msg(client, app_packet):
    # Using the integer directly from the object.
    queue_id = app_packet.queue_id_hex
    src_pk = app_packet.src_pk_hex
    msg = app_packet.msg

    """
    Since flow is: send 0, get ack 0, send 1 ...
    When we receive the next message it means the other side received our ack
    for a past message. Hence, we no longer need to keep sending those acks.
    This code deletes any queued acks older than the current message sequence.
    Note: only applies to sequenced messages in a queue.
    """
    msg_ack_queues = client.msg_queues[MsgEnum.MSGACK]
    if queue_id in msg_ack_queues:
        # If we already acked this in the past reuse existing queued app ack.
        meta = msg_ack_queues[queue_id]
        out, packet_ack = client.publish(
            meta["dest_pk_hex"], 
            meta["out"],
        )

        # Republish the application ack to sender.
        await async_wrap_errors(
            client.pipe.send(out)
        )

        return

    # Don't allow handlers to be called for msg portions that have
    # already been processed -- we raise an error in queue func too.
    msg_hash = hashlib.sha256(to_b(msg)).hexdigest()
    if msg_hash not in client.recv_msg_ids:
        # Trigger registered app handlers
        for msg_handler in client.msg_handlers:
            await async_wrap_errors(
                msg_handler(msg, src_pk, queue_id, client)
            )

        client.recv_msg_ids[msg_hash] = 1

    # Send Application ACK back to sender
    try:
        client.queue_msg(
            "ack",
            src_pk,
            queue_id,
            MsgEnum.MSGACK,
        )
    except Exception:
        log_exception()

# Function called for new application (type=msgack) publishes.
def process_app_ack(client, app_packet):
    # Pull fields directly from the object
    queue_id = app_packet.queue_id_hex
    src_pk = app_packet.src_pk_hex

    # Check if we even have a queue for this pipe ID.
    if queue_id not in client.msg_queues[MsgEnum.MSG]:
        return
    
    # Security: Ensure the ACK came from the person we sent the MSG to
    msg_meta = client.msg_queues[MsgEnum.MSG][queue_id]
    if msg_meta["dest_pk_hex"] != src_pk:
        return
    
    # Avoid setting result on a future that is already finished
    if msg_meta["app_ack"].done():
        return
    
    # Mark the application-level future as successful
    msg_meta["app_ack"].set_result(True)