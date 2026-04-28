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

from typing import Any
from aionetiface import h_to_b
from .mqtt_defs import MQTTEnum, MsgEnum
from .mqtt_packet import mqtt_parse_publish
from .app_packet import AppPacket
from .app_proto import process_app_msg, process_app_ack, process_app_probe
from .mqtt_msgs import build_puback


# mqtt_packet_reader sends full packets to this func to handle.
async def handle_mqtt_packet(client: Any, packet: Any) -> None:
    """Dispatch a fully assembled MQTT packet to the appropriate handler based on its type."""
    # MQTT server acks a publish or a channel subscribe.
    if packet.type in (MQTTEnum.PUBACK, MQTTEnum.SUBACK):
        await handle_broker_ack(client, packet)

    # We receive a new message from a topic we're subscribed to.
    elif packet.type == MQTTEnum.PUBLISH:
        await handle_publish(client, packet)

    # The server responds to our ping.
    elif packet.type == MQTTEnum.PINGRESP:
        await client.ping_handler()


# MQTT packets for publish, puback, subscribe, suback, have packet IDs.
# The software has a table of packet IDs that point to a future.
# The future is resolved for acks back from server. Currently, since
# the software uses app-level ACKS no packet-level awaits are used for
# these futures but acking here does mean the packet ID should be freed.
async def handle_broker_ack(client: Any, packet: Any) -> None:
    """Resolve the pending Future for a PUBACK or SUBACK packet and free its packet ID."""
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

    # Delete the packet ID reference now. Futures being awaited will have a
    # reference from the caller so they won't be garbage collected. This
    # prevents packet IDs from being exhausted over time.
    del client.packet_ids[packet.type][packet_id]


# Handles receiving a new message for our ECDSA pub hex topic sub.
# Messages fall into either acks for a past message or new messages.
# The software validates signatures and sets futures for acks.
async def handle_publish(client: Any, packet: Any) -> None:
    """Validate, authenticate, and route an inbound PUBLISH packet to the correct application handler."""
    # Publish-specific function for parsing a packet.
    parsed = mqtt_parse_publish(packet)
    if not parsed:
        return

    # Immediate MQTT Ack to keep the broker's in-flight window open
    topic, payload, packet_id = parsed
    if packet_id:
        puback_buf = build_puback(packet_id)
        await client.pipe.send(puback_buf)

    # Key Check: Is this message meant for us?
    # Comparing binary to binary for safety.
    if h_to_b(topic) != client.kp.compact_public_key:
        return

    # Verify signature and extract fields.
    app_packet = AppPacket.unpack(payload)
    if app_packet is None:
        return

    # Reject MSG-class messages older than 2x republish_duration to
    # mitigate replay attacks against signed application payloads.
    # Probes (and their corresponding MSGACKs) are exempt: they're
    # ephemeral discovery messages with random per-call queue_id
    # dedup, no payload worth replaying, and round-trip in seconds.
    # Applying this check to probes was producing a clock-skew-
    # dependent bug -- VMs whose wall clocks drift hours apart
    # (XP/Vista BIOS drift vs modern NTP-synced) silently dropped
    # every cross-cohort probe, breaking broker-set discovery and
    # cascading into reverse_connect signal-channel failures.
    # Discovery shouldn't depend on synchronised clocks.
    if app_packet.msg_type == MsgEnum.MSG:
        max_age = 2 * getattr(client, "republish_duration", 60)
        elapsed = int(client.get_time()) - app_packet.timestamp
        if elapsed > max_age:
            return

    # Route to application logic based on message type.
    if app_packet.msg_type == MsgEnum.MSG:
        await process_app_msg(client, app_packet)
    elif app_packet.msg_type == MsgEnum.MSGACK:
        process_app_ack(client, app_packet)
    elif app_packet.msg_type == MsgEnum.PROBE:
        await process_app_probe(client, app_packet)
