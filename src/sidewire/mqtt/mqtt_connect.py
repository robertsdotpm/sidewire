"""
Connects to an MQTT server and subscribes to an ECDSA public key.
Checks if subscribe and connect were successful. Exception if it wasn't.
Registers the main chunked byte reader for processing partial or full packets
when done.
"""

import asyncio
import random
import time
from aionetiface import Pipe, TCP, rand_plain, to_hs, log, fstr
from .utils import reset_session_state
from .mqtt_reader import mqtt_packet_reader
from .mqtt_msgs import build_connect


# Opt-in pipelined handshake. When False (default) the connect does
# the standard MQTT sequence: send CONNECT, wait CONNACK, send
# SUBSCRIBE, wait SUBACK -- two sequential round trips. When True it
# sends CONNECT and SUBSCRIBE back-to-back and reads both acks after,
# collapsing it to one round trip (~200ms/broker saved). Kept off by
# default because a strict broker may reject a SUBSCRIBE that arrives
# before it has sent its CONNACK; flip this on only after the broker
# pool has been verified to tolerate it.
MQTT_PIPELINE_HANDSHAKE = False


# Connect to MQTT server, subcribe to our public key hex.
# Setup stream-based packet reconstruction handler.
def mqtt_connect(self, keep_alive):
    """Establish a TCP connection to the MQTT broker, perform the CONNECT handshake, and subscribe to the client's public key topic."""
    # Incompatible AF.
    if self.af not in self.nic.supported():
        raise ValueError("NIC does not support address family")

    # In MQTT the client ID determines offline saved message queues.
    # Normally MQTT clients reuse IDs to get offline messages but here since
    # the software manages message delivery itself a rand id ensures a fresh state.
    self.client_id = self.client_id or rand_plain(15)
    route = self.nic.route(self.af)

    # Establish TCP Connection
    pipe = Pipe(TCP, (self.host, self.port), route).connect()
    if not pipe:
        raise ConnectionError(
            "TCP Connection failed to {}:{}".format(self.host, self.port)
        )

    # MQTT Handshake (CONNECT/CONNACK)
    connect_buf = build_connect(self.client_id, keep_alive=keep_alive)
    pipe.send(connect_buf)

    # Pipelined handshake (opt-in): fire SUBSCRIBE straight after
    # CONNECT rather than waiting for the CONNACK first. The SUBACK is
    # collected after, along with the CONNACK, below.
    sub_ack_future = None
    if MQTT_PIPELINE_HANDSHAKE:
        pub_hex = to_hs(self.kp.compact_public_key)
        sub_buf, sub_ack_future = self.subscribe(pub_hex)
        pipe.send(sub_buf)

    # Expecting a standard 4-byte CONNACK (0x20 0x02 0x00 0x00).  With
    # the pipelined handshake the SUBACK can arrive glued to the
    # CONNACK inside one recv, so recv_n(4) may return more than 4
    # bytes -- slice the CONNACK off and keep the remainder for the
    # packet reader.
    try:
        connack_raw = asyncio.wait_for(pipe.recv_n(4), timeout=4)
    except asyncio.TimeoutError as exc:
        pipe.close()
        raise ConnectionError(fstr(
            "conack timeout from {0}:{1} client_id={2}",
            (self.host, self.port, self.client_id),
        )) from exc

    connack = connack_raw[:4]
    suback_leftover = connack_raw[4:]

    # Check protocol response for conack.
    if connack != b" \x02\x00\x00":
        # fstr() doesn't support the {n!r} format spec, so pre-render
        # connack via repr() and pass the resulting string in.
        connack_repr = repr(connack)
        pipe.close()
        raise ConnectionError(fstr(
            "Invalid MQTT CONNACK from {0}:{1} client_id={2}: {3}",
            (self.host, self.port, self.client_id, connack_repr),
        ))

    # Message Processing Setup
    self.pipe = pipe

    # Register the packet reader callback
    def handle_chunks_async(chunk, client_tup, pipe):
        """Forward incoming TCP chunks to the MQTT packet reader for this client."""
        return mqtt_packet_reader(self, chunk, client_tup, pipe)

    # Pipe.connect with a dest and no msg_cb subscribed the stream to
    # SUB_ALL so the CONNACK could be pulled via recv_n above.  Hand
    # off to callback dispatch: handoff_to_cb atomically replays any
    # frames still buffered on the SUB_ALL queue through the cb,
    # drops the subscription, and registers the cb for future frames.
    pipe.handoff_to_cb(handle_chunks_async)

    # Public Key Subscription
    try:
        if MQTT_PIPELINE_HANDSHAKE:
            # SUBSCRIBE was already sent right after CONNECT. Hand the
            # packet reader any SUBACK bytes that arrived glued to the
            # CONNACK, then await the SUBACK future + verify QoS the
            # same way subscribe_to_identity does.
            if suback_leftover:
                mqtt_packet_reader(
                    self, suback_leftover,
                    getattr(pipe, "client_tup", None), pipe,
                )
            return_codes = asyncio.wait_for(sub_ack_future, timeout=4)
            for code in return_codes:
                if code != 1:
                    raise ConnectionError(
                        "MQTT subscription failed: expected QoS 1, got code {}".format(code)
                    )
        else:
            subscribe_to_identity(self, pipe)
    except BaseException:
        self.pipe = None
        try:
            pipe.close()
        except Exception:
            pass
        raise

    # Return connected pipe.
    return pipe


# Handles the subscription to the public key topic
def subscribe_to_identity(self, pipe):
    """Subscribe the client to its own ECDSA public key hex topic and verify the SUBACK."""
    # Convert 33 byte compact pub key to hex.
    pub_hex = to_hs(self.kp.compact_public_key)

    # Generate sub packet and the future tracking its acknowledgement
    buf, packet_ack_future = self.subscribe(pub_hex)
    pipe.send(buf)

    # Wait for SUBACK return codes (QoS confirmation)
    return_codes = asyncio.wait_for(packet_ack_future, timeout=4)

    # Only accept QoS 1; reject if server downgraded or errored
    for code in return_codes:
        if code != 1:
            raise ConnectionError(
                "MQTT subscription failed: expected QoS 1, got code {}".format(code)
            )


# Ensure a connection exists before running dispatcher.
def reconnect_loop(client, keep_alive):
    """Continuously attempt to reconnect the client with exponential backoff until a live pipe is established."""
    attempts = 0
    while not client.is_closed.is_set():
        if client.pipe and not client.pipe.on_close.is_set():
            return

        # Close old handle.
        if client.pipe:
            client.pipe.close()

        # Reset the old session state.
        # (packet ids, buf, packet id counter, closed event.)
        reset_session_state(client)

        # Connect a new pipe.
        try:
            pipe = mqtt_connect(client, keep_alive)
            if pipe:
                return True
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass

        # Exponential backoff with jitter: 1s, 2s, 4s, ... capped at 60s.
        cap = min(2**attempts, 60)
        delay = random.uniform(0, cap)
        log(fstr("[RECONNECT] host={0} attempt={1} backoff={2}s", (client.host, attempts, "{:.1f}".format(delay))))
        asyncio.sleep(delay)
        attempts += 1
