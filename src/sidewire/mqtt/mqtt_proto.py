import hashlib
import asyncio
from aionetiface import *
from .mqtt_defs import *
from .utils import *
from ..signing import *
from .mqtt_packet import *

async def handle_mqtt_packet(client, packet):
    #packet.debug_print()

    #print("in handle mqtt pack")

    # Main packets to 
    for packet_type in (MQTTEnum.PUBACK, MQTTEnum.SUBACK):
        if packet_type == packet.type:
            #print("got mqtt ack")
            #print("puback var header = ", packet.body)
            #print("len body = ", len(packet.body))

            #assert(len(packet.body) == 2)
            # Strip packet_id from variable header.
            packet_id = packet.body[:2]
            if packet_id not in client.packet_ids[packet_type]:
                print("e: packet id not in packet ids")
                return

            # Future to signal ack received for certain packets.
            ack_future = client.packet_ids[packet_type][packet_id]
            if ack_future.done():
                print("e: ack future done.")
                return
            
            # Sets the return code only for SUBACK otherwise empty str.
            ack_future.set_result(packet.body[2:])
            #print("ack packet id", packet_id)

    # Handle receive channel message.
    if MQTTEnum.PUBLISH == packet.type:
        print("got mqtt publish")
        out = mqtt_parse_publish(packet)
        print(out)

        if not out:
            print("e: invalid publish packet")
            return
        
        topic, payload, packet_id = out
        
        # Sanity checks match client formats.
        assert(is_ascii(topic))
        assert(is_ascii(payload))
        
        # Only interested in messages for our pub key.
        compact_public_key = h_to_b(topic)
        if compact_public_key != client.kp.compact_public_key:
            print("e: Recv msg not meant for us.")

        print("payload = ", payload)

        # Unpack fields from payload.
        src_pk_hex = payload[:66]; p = 66
        sig = h_to_b(payload[p:p + 128]); p += 128
        signed_msg = to_b(payload[p:])
        pipe_id_hex = payload[p:p + 64]; p += 64
        seq_no_hex = payload[p:p + 8]; p += 8
        msg = payload[p:]

        # Convert src pub hex into valid ECDSA pub key for verifying sig.
        vk = VerifyingKey.from_string(
            h_to_b(src_pk_hex),
            curve=SECP256k1
        )

        # Verify src pks signature is correct across signed msg.
        is_valid_sig = vk.verify(
            sig,
            signed_msg,
            sigdecode=util.sigdecode_string
        )

        if not is_valid_sig:
            print("e: invalid sig for ", msg)
            return
        
        # The message we wish to send still has app-specific type.
        # Allows it to be "ack" or "msg" to avoid endless loop.
        msg_type = h_to_b(msg[:2])[0]
        msg = msg[2:]
        print("msg type = ", msg_type)

        # If a regular message is received then send an ACK back to owner.
        if MsgEnum.MSG == msg_type:
            print("sending back ack to src ", int(seq_no_hex, 16))


            """
            Pass received messages to any registered message handlers.
            Do it before sending back an ack to avoid race conditions in
            receiving more messages before processing in done.
            """
            for msg_handler in client.msg_handlers:
                await async_wrap_errors(
                    msg_handler(
                        msg,
                        src_pk_hex,
                        pipe_id_hex,
                        client
                    )
                )

            try:
                ret = client.send(
                    "ack",
                    src_pk_hex,
                    pipe_id_hex,
                    MsgEnum.MSGACK,
                    seq_no=int(seq_no_hex, 16)
                )
                print(ret)
            except Exception:
                log_exception()

            print("after sent ack to src")
            
            return
        
        # Handle ACK response.
        if MsgEnum.MSGACK == msg_type:
            print("got msg ack")

            # Message doesn't belong to any registered pipes.
            if pipe_id_hex not in client.msg_queues[MsgEnum.MSG]:
                print("e: msg ack: in valid pipe id")
                return
            
            msg_queue = client.msg_queues[MsgEnum.MSG][pipe_id_hex]
            
            # Seq no overflows message queue.
            seq_no = int(seq_no_hex, 16)
            if seq_no > len(msg_queue):
                print("e: msg ack seq no invalid")
                return
            
            # Load meta data for msg waiting for ack.
            msg_meta = msg_queue[seq_no]
            if msg_meta["acked"].done():
                print("e: msg meta acked done")
                return
            
            # Only the dest for a message should be able to ACK it.
            if msg_meta["dest_pk_hex"] != src_pk_hex:
                print("e: msg ack dest pk hex not src pk hex")
                return
            
            # Ack a message being sent to a host.
            msg_meta["acked"].set_result(True)
            return

        print("signed message for us = ", msg)

        #print("Got message at ", topic, " content = ", payload)

    # Handle ping from server.
    if MQTTEnum.PINGRESP == packet.type:
        #print("got ping response.")
        return
    
# TCP streaming protocol handler for MQTT.
async def mqtt_packet_reader(client, chunk, client_tup, pipe):
    #print("got chunk = ", chunk)
    if not chunk:
        #print("not chunk")
        return

    # append incoming data
    client.buf += chunk

    # process as many complete packets as possible
    while client.buf:
        # need at least fixed header + 1 byte of remaining length
        if len(client.buf) < 2:
            print("need at least fixed header", client.buf)
            return

        # decode remaining length (starts at byte 1)
        rem_len, consumed = mqtt_decode_varint(client.buf, 1)
        if rem_len is None:
            print("rem len is none", client.buf)
            return

        # total packet size = fixed header (1) + varint + payload
        total_len = 1 + consumed + rem_len

        # wait for full packet
        if len(client.buf) < total_len:
            print("wait for full packet", client.buf)
            return

        # extract packet
        pkt_buf = client.buf[:total_len]
        client.buf = client.buf[total_len:]

        #print("pkt buf = ", pkt_buf)

        # parse + handle
        pkt = mqtt_parse_packet(pkt_buf)
        await async_wrap_errors(
            handle_mqtt_packet(client, pkt)
        )

def build_connect(client_id, keep_alive=60):
    print("mqtt connect")

    # proto name, proto level, clean session, keep alive 60s
    vh = (
        mqtt_enc_str("MQTT") + 
        b"\x04" + 
        b"\x02" + 
        struct.pack("!H", keep_alive)
    )
    pl = mqtt_enc_str(client_id)

    # Full packet to send.
    pkt = b"\x10" + mqtt_encode_varint(len(vh) + len(pl)) + vh + pl
    return pkt

def build_subscribe(topic, packet_id):
    vh = packet_id
    pl = mqtt_enc_str(topic) + b"\x01"  # QoS 1
    pkt = b"\x82" + mqtt_encode_varint(len(vh) + len(pl)) + vh + pl
    return pkt
    print("sub pkt = ", pkt)

def build_publish(topic, payload, packet_id):
    # Build variable header:
    # Topic (UTF-8 length-prefixed) + Packet Identifier (2 bytes)
    topic_bytes = mqtt_enc_str(topic)
    pl = topic_bytes + packet_id + to_b(payload)

    # Fixed header:
    # 0x32 = PUBLISH with QoS 1 (bits 1-2 set to 01)
    pkt = b"\x32" + mqtt_encode_varint(len(pl)) + pl
    return pkt