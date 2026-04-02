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
import asyncio
from aionetiface import *
from .mqtt_defs import *
from .utils import *
from ..signing import *
from .mqtt_packet import *

async def handle_mqtt_packet(client, packet):
    """Main router for incoming MQTT packets."""
    if packet.type in (MQTTEnum.PUBACK, MQTTEnum.SUBACK):
        await handle_broker_ack(client, packet)

    elif packet.type == MQTTEnum.PUBLISH:
        await handle_publish(client, packet)

    elif packet.type == MQTTEnum.PINGRESP:
        await client.ping_handler()

async def handle_broker_ack(client, packet):
    """
    Sets subscription success
    No individual fields for ack futures from the broker so far.
    
    """
    packet_id = packet.body[:2]
    
    if packet_id not in client.packet_ids[packet.type]:
        #rint("e: packet id ")
        return

    ack_future = client.packet_ids[packet.type][packet_id]
    if ack_future.done():
        return
    
    # SUBACK includes return codes in the body starting at index 2
    if packet.type == MQTTEnum.SUBACK:
        ack_future.set_result(packet.body[2:])
    else:
        ack_future.set_result(True)

async def handle_publish(client, packet):
    """Parses incoming publish packets and verifies signatures."""
    parsed = mqtt_parse_publish(packet)
    if not parsed:
        print("e: invalid publish packet")
        return
    
    topic, payload, packet_id = parsed

    # 1. Immediate MQTT Ack to keep the broker's in-flight window open
    if packet_id:
        await send_mqtt_puback(client, packet_id)

    # 2. Key Check: Is this message meant for us?
    if h_to_b(topic) != client.kp.compact_public_key:
        print("e: Recv msg not meant for us.")
        return

    # 3. Security: Verify ECDSA Signature
    try:
        # Layout: [src_pk(66)][sig(128)][signed_data...]
        src_pk_hex = payload[:66]
        sig = h_to_b(payload[66:194])
        signed_msg_bytes = to_b(payload[194:])
        
        vk = VerifyingKey.from_string(h_to_b(src_pk_hex), curve=SECP256k1)
        vk.verify(sig, signed_msg_bytes, sigdecode=util.sigdecode_string)
    except Exception:
        print("e: Signature verification failed")
        return

    # 4. Route to Application Logic
    # msg_data Layout: [pipe_id(64)][seq(8)][type(2)][msg...]
    msg_data = payload[194:]
    pipe_id_hex = msg_data[:64]
    seq_no_hex = msg_data[64:72]
    app_payload = msg_data[72:]
    
    msg_type = h_to_b(app_payload[:2])[0]
    actual_msg = app_payload[2:]

    if msg_type == MsgEnum.MSG:
        await process_app_msg(client, actual_msg, src_pk_hex, pipe_id_hex, seq_no_hex)
    elif msg_type == MsgEnum.MSGACK:
        process_app_ack(client, pipe_id_hex, seq_no_hex, src_pk_hex)

async def send_mqtt_puback(client, packet_id):
    """Sends the 4-byte MQTT PUBACK to the broker."""
    ack_packet = bytes([0x40, 0x02]) + packet_id
    await client.pipe.send(ack_packet)

async def process_app_msg(client, msg, src_pk_hex, pipe_id_hex, seq_no_hex):
    """Processes a new application message and sends back a MSGACK."""
    seq_no = int(seq_no_hex, 16)

    # 1. Deduplication (Include seq_no in hash to allow identical text)
    msg_hash = hashlib.sha256(to_b(msg + seq_no_hex)).hexdigest()
    if msg_hash in client.sent_msg_ids:
        return
    client.sent_msg_ids[msg_hash] = 1

    # 2. Cleanup old ACKs for this pipe
    if pipe_id_hex in client.msg_queues[MsgEnum.MSGACK]:
        queue = client.msg_queues[MsgEnum.MSGACK][pipe_id_hex]
        for old_seq in list(queue.keys()):
            if old_seq < seq_no:
                del queue[old_seq]
        
        # 3. Optimization: If we already ACKed this specific seq, resend it
        if seq_no in queue:
            meta = queue[seq_no]
            out = client.publish(meta["dest_pk_hex"], meta["out"], meta["packet_id"], True)
            await async_wrap_errors(client.pipe.send(out))
            return

    # 4. Trigger registered app handlers
    for msg_handler in client.msg_handlers:
        await async_wrap_errors(msg_handler(msg, src_pk_hex, pipe_id_hex, client))

    # 5. Send Application ACK back to sender
    try:
        client.send("ack", src_pk_hex, pipe_id_hex, MsgEnum.MSGACK, seq_no=seq_no)
    except Exception:
        log_exception()

def process_app_ack(client, pipe_id_hex, seq_no_hex, src_pk_hex):
    """Handles an application-level ACK to clear the sender's queue."""
    if pipe_id_hex not in client.msg_queues[MsgEnum.MSG]:
        return
    
    seq_no = int(seq_no_hex, 16)
    msg_queue = client.msg_queues[MsgEnum.MSG][pipe_id_hex]
    
    if seq_no in msg_queue:
        msg_meta = msg_queue[seq_no]
        if msg_meta["dest_pk_hex"] != src_pk_hex:
            return
        
        if msg_meta["app_ack"].done():
            return
        
        msg_meta["app_ack"].set_result(True)

