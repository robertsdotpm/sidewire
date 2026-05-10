"""
Connects to an MQTT server and subscribes to an ECDSA public key.
Checks if subscribe and connect were successful. Exception if it wasn't.
Registers the main chunked byte reader for processing partial or full packets
when done.
"""

import asyncio
import random
from aionetiface import Pipe, TCP, rand_plain, to_hs, log, fstr
from .utils import reset_session_state
from .mqtt_reader import mqtt_packet_reader
from .mqtt_msgs import build_connect


# Connect to MQTT server, subcribe to our public key hex.
# Setup stream-based packet reconstruction handler.
async def mqtt_connect(self, keep_alive):
    """Establish a TCP connection to the MQTT broker, perform the CONNECT handshake, and subscribe to the client's public key topic."""
    # Incompatible AF.
    if self.af not in self.nic.supported():
        raise ValueError("NIC does not support address family")

    # In MQTT the client ID determines offline saved message queues.
    # Normally MQTT clients reuse IDs to get offline messages but here since
    # the software manages message delivery itself a rand id ensures a fresh state.
    self.client_id = self.client_id or rand_plain(15)
    route = self.nic.route(self.af)

    log(fstr(
        "[MQTT-CONNECT] starting host={0}:{1} af={2} client_id={3} keep_alive={4}",
        (self.host, self.port, self.af, self.client_id, keep_alive),
    ))

    # Establish TCP Connection
    pipe = await Pipe(TCP, (self.host, self.port), route).connect()
    if not pipe:
        log(fstr(
            "[MQTT-CONNECT] FAIL host={0}:{1}: TCP connect returned None",
            (self.host, self.port),
        ))
        raise ConnectionError(
            "TCP Connection failed to {}:{}".format(self.host, self.port)
        )

    log(fstr(
        "[MQTT-CONNECT] TCP up host={0}:{1}; sending CONNECT packet ({2} bytes)",
        (self.host, self.port, len(build_connect(self.client_id, keep_alive))),
    ))

    # MQTT Handshake (CONNECT/CONNACK)
    connect_buf = build_connect(self.client_id, keep_alive=keep_alive)
    await pipe.send(connect_buf)

    # Expecting a standard 4-byte CONNACK (0x20 0x02 0x00 0x00)
    try:
        connack = await asyncio.wait_for(pipe.recv_n(4), timeout=4)
    except asyncio.TimeoutError as exc:
        log(fstr(
            "[MQTT-CONNECT] FAIL host={0}:{1} client_id={2}: CONNACK timeout "
            "(broker accepted TCP + our CONNECT but did not respond within 4s)",
            (self.host, self.port, self.client_id),
        ))
        await pipe.close()
        raise ConnectionError(fstr(
            "conack timeout from {0}:{1} client_id={2}",
            (self.host, self.port, self.client_id),
        )) from exc

    # Check protocol response for conack.
    if connack != b" \x02\x00\x00":
        # fstr() doesn't support the {n!r} format spec, so pre-render
        # connack via repr() and pass the resulting string in.
        connack_repr = repr(connack)
        log(fstr(
            "[MQTT-CONNECT] FAIL host={0}:{1} client_id={2}: bad CONNACK "
            "{3} (empty bytes mean broker closed TCP after our CONNECT -- "
            "rate-limit / blacklist / malformed-packet rejection. Non-empty "
            "bytes that don't match would mean broker accepted but returned "
            "an error code.)",
            (self.host, self.port, self.client_id, connack_repr),
        ))
        await pipe.close()
        raise ConnectionError(fstr(
            "Invalid MQTT CONNACK from {0}:{1} client_id={2}: {3}",
            (self.host, self.port, self.client_id, connack_repr),
        ))

    log(fstr(
        "[MQTT-CONNECT] OK host={0}:{1} client_id={2}",
        (self.host, self.port, self.client_id),
    ))

    # Message Processing Setup
    self.pipe = pipe

    # Register the packet reader callback
    async def handle_chunks_async(chunk, client_tup, pipe):
        """Forward incoming TCP chunks to the MQTT packet reader for this client."""
        return await mqtt_packet_reader(self, chunk, client_tup, pipe)

    # Add handler to read chunks.
    pipe.add_msg_cb(handle_chunks_async)

    # Public Key Subscription
    try:
        await subscribe_to_identity(self, pipe)
    except BaseException:
        try:
            await pipe.close()
        except Exception:
            pass
        raise

    # Return connected pipe.
    return pipe


# Handles the subscription to the public key topic
async def subscribe_to_identity(self, pipe):
    """Subscribe the client to its own ECDSA public key hex topic and verify the SUBACK."""
    # Convert 33 byte compact pub key to hex.
    pub_hex = to_hs(self.kp.compact_public_key)

    # Generate sub packet and the future tracking its acknowledgement
    buf, packet_ack_future = self.subscribe(pub_hex)
    await pipe.send(buf)

    # Wait for SUBACK return codes (QoS confirmation)
    return_codes = await asyncio.wait_for(packet_ack_future, timeout=4)

    # Only accept QoS 1; reject if server downgraded or errored
    for code in return_codes:
        if code != 1:
            raise ConnectionError(
                "MQTT subscription failed: expected QoS 1, got code {}".format(code)
            )


# Ensure a connection exists before running dispatcher.
async def reconnect_loop(client, keep_alive):
    """Continuously attempt to reconnect the client with exponential backoff until a live pipe is established."""
    attempts = 0
    while not client.is_closed.is_set():
        if client.pipe and not client.pipe.on_close.is_set():
            return

        # Close old handle.
        if client.pipe:
            await client.pipe.close()

        # Reset the old session state.
        # (packet ids, buf, packet id counter, closed event.)
        reset_session_state(client)

        # Connect a new pipe.
        try:
            pipe = await mqtt_connect(client, keep_alive)
            if pipe:
                return True
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass

        # Exponential backoff with jitter: 1s, 2s, 4s, ... capped at 60s.
        cap = min(2**attempts, 60)
        delay = random.uniform(0, cap)
        log(fstr("[RECONNECT] host={0} attempt={1} backoff={2:.1f}s", (client.host, attempts, delay)))
        await asyncio.sleep(delay)
        attempts += 1
