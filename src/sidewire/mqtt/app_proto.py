import hashlib
from aionetiface import async_wrap_errors, log_exception, to_b
from aionetiface.utility.signing import sha256_hex_async
from .mqtt_defs import MsgEnum
from .utils import get_msg_from_queue


# Function called for new application (type=msg) publishes.
async def process_app_msg(client, app_packet):
    """Deliver a received application message to registered handlers and enqueue an ACK back to the sender."""
    # Using the integer directly from the object.
    seq_no = app_packet.seq_no
    queue_id = app_packet.queue_id_hex
    src_pk = app_packet.src_pk_hex
    msg = app_packet.msg

    # Flow is: send 0, get ack 0, send 1 ...
    # When we receive the next message it means the other side received our ack
    # for a past message. Hence, we no longer need to keep sending those acks.
    # This code deletes any queued acks older than the current message sequence.
    # Note: only applies to sequenced messages in a queue.
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
            out, _ = client.publish(
                meta["dest_pk_hex"],
                meta["out"],
            )

            # Republish the application ack to sender.
            await async_wrap_errors(client.pipe.send(out))

            return

    # Don't allow handlers to be called for msg portions that have
    # already been processed -- we raise an error in queue func too.
    # SHA256 via the shared aionetiface.utility.signing helper so the
    # loop stays free for concurrent MQTT readers; the digest is
    # payload-size-proportional and runs on every inbound publish.
    msg_hash = await sha256_hex_async(to_b(msg))
    if msg_hash not in client.recv_msg_ids:
        # Stamp before awaiting handlers so concurrent clients sharing
        # recv_msg_ids don't slip past the check during an await yield.
        client.recv_msg_ids[msg_hash] = client.get_time()

        # Trigger registered app handlers
        for msg_handler in client.msg_handlers:
            await async_wrap_errors(msg_handler(msg, src_pk, queue_id, client))

    # Send Application ACK back to sender.  Async variant offloads
    # the ECDSA sign to the default thread pool executor so the
    # MQTT-reader loop isn't stalled per inbound MSG.
    try:
        await client.queue_msg_async("ack", src_pk, queue_id, MsgEnum.MSGACK, seq_no=seq_no)
    except (ValueError, KeyError):
        log_exception()


# Function called for new application (type=probe) publishes.
# ACKs the sender so discovery works, but never calls user handlers.
async def process_app_probe(client, app_packet):
    """Respond to a probe packet with a direct-published ACK.

    The probe round-trip is time-bounded: the originator's
    try_client awaits the ack on a fixed timeout (~15s). On slow VMs
    (XP/Vista) the dispatcher's 0.5s scan cadence + 0-2s first-
    publish jitter can push the ack publish beyond that budget,
    which manifested as bidirectional broker-set non-convergence:
    other peers' publish_for[vista] returned empty even though
    vista's protected list claimed the right brokers, because
    vista's ack was leaving the wire too late.

    Direct-publish here mirrors the send_probe outbound fix --
    the ack still gets registered in the MSGACK queue (so
    process_app_ack on the originator can resolve the future),
    but the MSGACK is shipped immediately and flagged
    probe_one_shot so the dispatcher's republish loop skips it.
    """
    # Does message already exist.
    found_msg = get_msg_from_queue(
        client,
        app_packet.queue_id_hex,
        app_packet.seq_no,
    )

    # Suppress already queued response assert errors.
    if found_msg:
        return

    # Direct-publish the MSGACK with the same shape as send_probe
    # so the dispatcher's republish_meta short-circuits via
    # probe_one_shot. Falling back to client.queue_msg here would
    # re-introduce the jitter/backoff delay that was breaking
    # cross-cohort probe acks.
    try:
        await client.send_probe_ack(
            app_packet.src_pk_hex,
            app_packet.queue_id_hex,
            seq_no=app_packet.seq_no,
        )
    except (ValueError, KeyError, OSError):
        log_exception()


def process_app_ack(client, app_packet):
    """Resolve the outstanding Future for a message when its application-level ACK arrives."""
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

        # PROBE entries are probe_one_shot and never deleted by dequeue_msg
        # (which is only called for MsgEnum.MSG). Delete them here so
        # msg_queues[PROBE] doesn't accumulate indefinitely.
        if msg_meta.get("probe_one_shot"):
            del client.msg_queues[msg_type][queue_id][seq_no]
            if not client.msg_queues[msg_type][queue_id]:
                del client.msg_queues[msg_type][queue_id]
        return
