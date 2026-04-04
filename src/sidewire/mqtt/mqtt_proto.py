"""
Contains the function for managing incoming buffer of data from the MQTT
server which is checked for partial or full MQTT packets. Once a full packet
is assembled in mqtt_packet_reader it ets passed on to handle_mqtt_packet.

The job of handle_mqtt_packet is to implement the most minimal support for
the MQTT protocol possible:
    - new received published messages
    - ping responses from the server
    - eventually disconnects
"""

import hashlib
from aionetiface import *
from .mqtt_defs import *
from .utils import *
from ..signing import *
from .mqtt_packet import *
from .app_packet import *

# mqtt_packet_reader sends full packets to this func to handle.
async def handle_mqtt_packet(client, packet):
    # MQTT server acks a publish or a channel subscribe.
    if packet.type in (MQTTEnum.PUBACK, MQTTEnum.SUBACK):
        await handle_broker_ack(client, packet)

    # We receive a new message from a topic we're subscribed to.
    elif packet.type == MQTTEnum.PUBLISH:
        await handle_publish(client, packet)

    # The server responds to our ping.
    elif packet.type == MQTTEnum.PINGRESP:
        await client.ping_handler()

"""
MQTT packets for publish, puback, subscribe, suback, have packet IDs.
The software has a table of packet IDs that point to a future.
The future is resolved for acks back from server. Currently, since
the software uses app-level ACKS no packet-level awaits are used for
these futures but acking here does mean the packet ID should be freed.
"""
async def handle_broker_ack(client, packet):
    # Extract packet ID from packet.
    packet_id = packet.body[:2]
    if packet_id not in client.packet_ids[packet.type]:
        return

    # Lookup packet future.
    ack_future = client.packet_ids[packet.type][packet_id]
    if ack_future.done():
        return
    
    # Resolve packet future.
    if packet.type == MQTTEnum.SUBACK:
        # SUBACK includes return codes in the body.
        ack_future.set_result(packet.body[2:])
    else:
        ack_future.set_result(True)

    """
    I think it should be fine to delete the packet ID reference now.
    Any futures being awaited will have a reference from the caller
    so they won't just be garbage collected. This prevents all the
    packet IDs from being filled up and used.
    """
    del client.packet_ids[packet.type][packet_id]

"""
Handles receiving a new message for our ECDSA pub hex topic sub.
Messages fall into either acks for a past message or new messages.
The software validates signatures and sets futures for acks.
"""
async def handle_publish(client, packet):
    # Publish-specific function for parsing a packet.
    parsed = mqtt_parse_publish(packet)
    if not parsed:
        print("e: invalid publish packet")
        return

    # Immediate MQTT Ack to keep the broker's in-flight window open
    topic, payload, packet_id = parsed
    if packet_id:
        await send_mqtt_puback(client, packet_id)

    # Key Check: Is this message meant for us?
    # Comparing binary to binary for safety.
    if h_to_b(topic) != client.kp.compact_public_key:
        print("e: Recv msg not meant for us.")
        return

    # Use the class to verify signature and extract fields.
    # This replaces the manual slicing and VerifyingKey logic.
    app_packet = AppPacket.unpack(payload)
    
    if app_packet is None:
        # unpack() prints its own error and returns None on sig failure
        return

    # Route to Application Logic
    # Note: We use the members from our app_packet instance now.
    if app_packet.msg_type == MsgEnum.MSG:
        await process_app_msg(
            client, 
            app_packet
        )
    elif app_packet.msg_type == MsgEnum.MSGACK:
        process_app_ack(
            client, 
            app_packet
        )

async def send_mqtt_puback(client, packet_id):
    """Sends the 4-byte MQTT PUBACK to the broker."""
    ack_packet = bytes([0x40, 0x02]) + packet_id
    await client.pipe.send(ack_packet)

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