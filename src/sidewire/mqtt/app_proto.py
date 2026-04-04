import hashlib
from aionetiface import *
from .mqtt_defs import *

# Function called for new application (type=msg) publishes.
async def process_app_msg(client, app_packet):
    # Using the integer directly from the object.
    seq_no = app_packet.seq_no
    pipe_id = app_packet.pipe_id_hex
    src_pk = app_packet.src_pk_hex
    msg = app_packet.msg

    # Don't allow handlers to be called for msg portions that have
    # already been processed -- we raise an error in queue func too.
    msg_hash = hashlib.sha256(to_b(msg)).hexdigest()
    if msg_hash in client.recv_msg_ids:
        return
    else:
        client.recv_msg_ids[msg_hash] = 1

    """
    Since flow is: send 0, get ack 0, send 1 ...
    When we receive the next message it means the other side received our ack
    for a past message. Hence, we no longer need to keep sending those acks.
    This code deletes any queued acks older than the current message sequence.
    """
    msg_ack_queues = client.msg_queues[MsgEnum.MSGACK]
    if pipe_id in msg_ack_queues:
        # List of sequenced app-level ack metas.
        ack_queue = msg_ack_queues[pipe_id]
        
        # Remove queued application acks that are no longer relevant.
        for old_seq in list(ack_queue.keys()):
            if old_seq < seq_no:
                del ack_queue[old_seq]
        
        # If we already acked this in the past reuse existing queued app ack.
        if seq_no in ack_queue:
            # Structure for an app-level ack message.
            meta = ack_queue[seq_no]
            out, packet_ack = client.publish(
                meta["dest_pk_hex"], 
                meta["out"],
            )

            # Republish the application ack to sender.
            await async_wrap_errors(
                client.pipe.send(out)
            )

            return

    # Trigger registered app handlers
    for msg_handler in client.msg_handlers:
        await async_wrap_errors(
            msg_handler(msg, src_pk, pipe_id, client)
        )

    # Send Application ACK back to sender
    try:
        client.queue_msg(
            "ack",
            src_pk,
            pipe_id,
            MsgEnum.MSGACK,
            seq_no=seq_no
        )
    except Exception:
        log_exception()

# Function called for new application (type=msgack) publishes.
def process_app_ack(client, app_packet):
    # Pull fields directly from the object
    pipe_id = app_packet.pipe_id_hex
    seq_no = app_packet.seq_no
    src_pk = app_packet.src_pk_hex

    # Check if we even have a queue for this pipe ID.
    if pipe_id not in client.msg_queues[MsgEnum.MSG]:
        return
    
    # Check sequence exists.
    msg_queue = client.msg_queues[MsgEnum.MSG][pipe_id]
    if seq_no not in msg_queue:
        return
        
    # Security: Ensure the ACK came from the person we sent the MSG to
    msg_meta = msg_queue[seq_no]
    if msg_meta["dest_pk_hex"] != src_pk:
        return
    
    # Avoid setting result on a future that is already finished
    if msg_meta["app_ack"].done():
        return
    
    # Mark the application-level future as successful
    msg_meta["app_ack"].set_result(True)