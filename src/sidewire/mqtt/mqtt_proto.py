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

from aionetiface import *
from .mqtt_defs import *
from .utils import *
from .mqtt_packet import *
from .app_packet import *
from .app_proto import *
from .mqtt_msgs import *

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
        #print("e: invalid publish packet")
        return

    # Immediate MQTT Ack to keep the broker's in-flight window open
    topic, payload, packet_id = parsed
    if packet_id:
        puback_buf = build_puback(packet_id)
        await client.pipe.send(puback_buf)

    # Key Check: Is this message meant for us?
    # Comparing binary to binary for safety.
    if h_to_b(topic) != client.kp.compact_public_key:
        #print("e: Recv msg not meant for us.")
        return

    # Use the class to verify signature and extract fields.
    # This replaces the manual slicing and VerifyingKey logic.
    app_packet = AppPacket.unpack(payload)
    if app_packet is None:
        return

    # Reject messages older than 2x republish_duration for replay attacks.
    max_age = 2 * getattr(client, 'republish_duration', 60)
    elapsed = int(client.get_time()) - app_packet.timestamp
    if elapsed > max_age:
        return

    # Route to Application Logic
    # Note: We use the members from our app_packet instance now.
    if app_packet.msg_type == MsgEnum.MSG:
        await process_app_msg(client, app_packet)
    elif app_packet.msg_type == MsgEnum.MSGACK:
        process_app_ack(client, app_packet)
    elif app_packet.msg_type == MsgEnum.PROBE:
        await process_app_probe(client, app_packet)
