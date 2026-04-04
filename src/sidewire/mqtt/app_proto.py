import hashlib
from aionetiface import *
from .mqtt_defs import *

async def process_app_msg(client, app_packet):
    """Processes a new application message and sends back a MSGACK."""
    
    # Using the integer directly from the object
    seq_no = app_packet.seq_no
    pipe_id = app_packet.pipe_id_hex
    src_pk = app_packet.src_pk_hex
    msg = app_packet.msg

    # 1. Deduplication (Include seq_no in hash to allow identical text)
    # Using the property for the hex string version
    dedupe_str = msg + app_packet.seq_no_hex
    msg_hash = hashlib.sha256(to_b(dedupe_str)).hexdigest()
    
    if msg_hash in client.sent_msg_ids:
        return
    client.sent_msg_ids[msg_hash] = 1

    # 2. Cleanup old ACKs for this pipe
    msg_ack_queues = client.msg_queues[MsgEnum.MSGACK]
    if pipe_id in msg_ack_queues:
        queue = msg_ack_queues[pipe_id]
        
        # Cleanup logic: remove older sequences
        for old_seq in list(queue.keys()):
            if old_seq < seq_no:
                del queue[old_seq]
        
        # 3. Optimization: If we already ACKed this specific seq, resend it
        if seq_no in queue:
            meta = queue[seq_no]
            out, packet_ack = client.publish(
                meta["dest_pk_hex"], 
                meta["out"],
            )

            await async_wrap_errors(client.pipe.send(out))
            return

    # 4. Trigger registered app handlers
    for msg_handler in client.msg_handlers:
        # Passing fields directly from the packet object
        await async_wrap_errors(msg_handler(msg, src_pk, pipe_id, client))

    # 5. Send Application ACK back to sender
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

def process_app_ack(client, app_packet):
    """Handles an application-level ACK to clear the sender's queue."""
    # Pull fields directly from the object
    pipe_id = app_packet.pipe_id_hex
    seq_no = app_packet.seq_no
    src_pk = app_packet.src_pk_hex

    # Check if we even have a queue for this pipe
    if pipe_id not in client.msg_queues[MsgEnum.MSG]:
        return
    
    msg_queue = client.msg_queues[MsgEnum.MSG][pipe_id]
    
    if seq_no in msg_queue:
        msg_meta = msg_queue[seq_no]
        
        # Security: Ensure the ACK came from the person we sent the MSG to
        if msg_meta["dest_pk_hex"] != src_pk:
            return
        
        # Avoid setting result on a future that is already finished
        if msg_meta["app_ack"].done():
            return
        
        # Mark the application-level future as successful
        msg_meta["app_ack"].set_result(True)