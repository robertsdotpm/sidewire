import hashlib
from aionetiface import *
from .mqtt_defs import *
from .utils import *

# Function called for new application (type=msg) publishes.
async def process_app_msg(client, app_packet):
    # Using the integer directly from the object.
    seq_no = app_packet.seq_no
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
        # List of sequenced app-level ack metas.
        ack_queue = msg_ack_queues[queue_id]
        
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

    # Don't allow handlers to be called for msg portions that have
    # already been processed -- we raise an error in queue func too.
    msg_hash = hashlib.sha256(to_b(msg)).hexdigest()
    if msg_hash not in client.recv_msg_ids:
        # Stamp before awaiting handlers so concurrent clients sharing
        # recv_msg_ids don't slip past the check during an await yield.
        client.recv_msg_ids[msg_hash] = client.get_time()

        # Trigger registered app handlers
        for msg_handler in client.msg_handlers:
            await async_wrap_errors(
                msg_handler(msg, src_pk, queue_id, client)
            )

    # Send Application ACK back to sender
    try:
        client.queue_msg(
            "ack",
            src_pk,
            queue_id,
            MsgEnum.MSGACK,
            seq_no=seq_no
        )
    except Exception:
        log_exception()

# Function called for new application (type=probe) publishes.
# ACKs the sender so discovery works, but never calls user handlers.
async def process_app_probe(client, app_packet):
    # Does message already exist.
    found_msg = get_msg_from_queue(
        client, 
        app_packet.queue_id_hex,
        app_packet.seq_no,
    )

    # Suppress already queued response assert errors.
    if found_msg:
        return
        
    # Otherwise schedule an ack for it.
    try:
        client.queue_msg(
            "ack",
            app_packet.src_pk_hex,
            app_packet.queue_id_hex,
            MsgEnum.MSGACK,
            seq_no=app_packet.seq_no
        )
    except Exception:
        log_exception()

# Function called for new application (type=msgack) publishes.
def process_app_ack(client, app_packet):
    # Pull fields directly from the object
    queue_id = app_packet.queue_id_hex
    seq_no = app_packet.seq_no
    src_pk = app_packet.src_pk_hex

    # ACKs can resolve futures in either MSG or PROBE queues.
    for msg_type in (MsgEnum.MSG, MsgEnum.PROBE):
        if queue_id not in client.msg_queues[msg_type]:
            continue

        msg_queue = client.msg_queues[msg_type][queue_id]
        if seq_no not in msg_queue:
            continue

        # Security: Ensure the ACK came from the person we sent to.
        msg_meta = msg_queue[seq_no]
        if msg_meta["dest_pk_hex"] != src_pk:
            continue

        if msg_meta["app_ack"].done():
            return

        msg_meta["app_ack"].set_result(True)
        return