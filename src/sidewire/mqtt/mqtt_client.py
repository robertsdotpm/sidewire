"""
This module implements a basic async MQTT client. It is not designed to be
generic like a regular MQTT client. Instead it supports:

    - identity management -- clients subscribe to their own ECDSA pub key hex
    - authenticated messages -- all messages are signed by the sender, replies
    are sent back to originating pub key hex channel
    - reliable delivery -- application level acks allow message delivery to be
    confirmed before moving to next message
    - sequential messaging -- sequential messaging queues allow for cumulative
    ack to keep ack spam to a minimum. Asyncio still sequences sends without
    the need to use a specific message queue so this is optional.

Technical notes:
    - client ID is intentionally random each time
    - connect clean session = True -- no old stored messages used
    - publish "dup" flag = False
    - no reused packet IDs for publish messages
    - delivery and ordering handled by application-level ack

Using the above settings means that message retransmission is no longer managed
by the server and ends up entirely managed by the client. The MQTT server becomes
a simple proxy in such a mode. But you can build a more reliable, deterministic,
system on top of it without hidden edge-cases that might exist (e.g. storage
limits, protocol implementation differences, and so on.)

The main class interface is designed to be sync without any network I/O. All I/O
is instead confined to a background message dispatcher written to be simple. This
makes it easier to recover from network disconnects and resume publishing. If there
is only one major place to detect network errors it makes it easier to handle errors.
The client uses a simple buffer for incoming data from TCP until any number of
full packets are ready to be processed (this is how to correctly handle TCP.)

module files:
    - schedule messages to send, connect to server - mqtt client
    - recv data and pass messages on to handlers - mqtt protocol
    - send out queued messages and reconnect on con lost - mqtt dispatcher
    - handle connection in detail - mqtt connect
    - craft and unpack various mqtt packets - mqtt packet

Everything ended up under 700 lines of code which I think is pretty good!
"""

import hashlib
import asyncio
import time
from aionetiface import (
    IP4,
    Interface,
    Signing,
    async_run,
    async_wrap_errors,
    cancel_task,
    fstr,
    is_ascii,
    log,
    rand_b,
    to_h,
    to_hs,
)
from .mqtt_defs import MQTT_KEEP_ALIVE, MsgEnum, MQTTEnum
from .utils import packet_ack_future
from .mqtt_ordered_send import ordered_ack_send
from .mqtt_dispatch import dispatcher
from .mqtt_connect import mqtt_connect
from .mqtt_msgs import build_subscribe, build_publish, build_disconnect


class MQTTClient:
    """Async MQTT client with identity-based subscriptions and reliable delivery."""

    def __init__(
        self,
        af,
        nic,
        dest,
        kp,
        get_time=None,
    ):
        """Initialize an MQTTClient with network, destination, and key-pair settings.

        get_time MUST be supplied (no implicit time.time fallback).
        Silently using wall clock here was hiding a real clock-skew
        bug in the matrix: when XP/Vista's BIOS clocks drift hours
        from modern VMs, every cross-cohort MQTT message was being
        dropped by handle_publish's timestamp-vs-max_age check
        (~120s window). The Router patches the clock on the Router
        object after construction, but that patch never propagates
        into already-constructed MQTTClient instances. Forcing
        callers to pass get_time explicitly turns this into a loud
        failure.
        """
        if get_time is None:
            raise ValueError(
                "MQTTClient: get_time is required (pass node.sys_clock.time, "
                "not time.time directly -- the latter silently hides clock-"
                "skew bugs across the matrix)"
            )
        # Addressing info for connected MQTT server.
        self.af = af
        self.nic = nic
        self.dest = dest
        self.host, self.port = dest

        # ECDSA key pair for signing high-level sequenced messages over MQTT.
        self.kp = kp

        # Handle received messages. Set so add_msg_handler is idempotent --
        # a handler attached more than once (e.g. router cache reattach +
        # smart_pipe.connect on a reused client) doesn't fan out twice.
        self.msg_handlers = set()

        # Plugin message system [enum][pipe id][seq_no] = meta
        # Used by the dispatcher task to republish messages.
        # The packet handler code in proto also sets futures here (app level ACKs.)
        self.msg_queues = {
            MsgEnum.MSG: {},
            MsgEnum.MSGACK: {},
            MsgEnum.PROBE: {},
        }

        # Msg part unpacked from the full proto over publish.
        self.sent_msg_ids = {}
        self.recv_msg_ids = {}

        # Record in-flight packets by IDs that map to futures on server ack.
        # [packet enum][packet id] = Future
        self.packet_ids = {
            MQTTEnum.SUBACK: {},
            MQTTEnum.PUBACK: {},
        }

        # Other internal state.
        self.packet_id = 0
        self.client_id = None

        # Connection state.
        self.is_closed = asyncio.Event()
        self.pipe = None
        self.buf = b""

        # Background task for processing messages.
        self.dispatcher_task = None

        # Set in connect(); initialized here to satisfy attribute-defined-outside-init.
        self.republish_duration = 0

        # Used to get unix timestamp.
        self.get_time = get_time

        # Tracks the last queue_msg() call so the Router-level idle
        # closer can leave actively-used clients alone. None == never
        # used to send, in which case the idle-closer treats the
        # client's connect time as the activity baseline so a freshly-
        # opened-but-unused MQTTClient still gets a full idle window
        # before being considered for closure.
        self.last_send = None

    # Receive back app protocol msgs unpacked.
    def add_msg_handler(self, msg_handler):
        """Register a callback to receive decoded incoming application messages (idempotent)."""
        self.msg_handlers.add(msg_handler)

    # Allow awaiting on the class object directly.
    def __await__(self):
        """Allow the client to be directly awaited as a shorthand for connect()."""
        return self.connect().__await__()

    # Connect to MQTT server and subscribe to our own pub key hex topic.
    # Also will start a background task that dispatches messages from self.send.
    async def connect(
        self,
        republish_duration=60,
        interval=2,
        keep_alive=MQTT_KEEP_ALIVE,
        ignore_acked=False,
        reconnect_delay=0,
        timeout=4,
    ):
        """Connect to the MQTT server and start the background message dispatcher."""
        # Re-entry guard.
        if self.dispatcher_task:
            log(fstr("[MQTT-CLIENT-CONNECT] host={0} re-entry: dispatcher already running (task_done={1})", (self.host, self.dispatcher_task.done())))
            return self.pipe

        # Store for use in timestamp validation on receive.
        self.republish_duration = max(republish_duration, 2 * keep_alive)

        # Connect with a timeout.
        try:
            pipe = await asyncio.wait_for(mqtt_connect(self, keep_alive), timeout)
        except asyncio.TimeoutError as exc:
            raise ConnectionError("MQTT connection timeout.") from exc

        # Start the background dispatcher after connect() returns so that
        # self.pipe is already set when the dispatcher's reconnect loop first runs,
        # avoiding a race condition between the two.
        if pipe and self.dispatcher_task is None:
            self.dispatcher_task = asyncio.create_task(
                async_wrap_errors(
                    dispatcher(
                        self,
                        republish_duration=self.republish_duration,
                        interval=interval,
                        keep_alive=keep_alive,
                        ignore_acked=ignore_acked,
                        reconnect_delay=reconnect_delay,
                    )
                )
            )

        return pipe

    # Internal: used to subscribe to own pub key hex.
    # Returns packet and future to await ack for the packet from the server.
    def subscribe(self, topic):
        """Build a SUBSCRIBE packet and return it with a future for the server SUBACK."""
        if not isinstance(topic, str):
            raise TypeError("topic must be str, got {}".format(type(topic).__name__))
        packet_id, packet_ack = packet_ack_future(self, MQTTEnum.SUBACK)
        buf = build_subscribe(topic, packet_id)
        return buf, packet_ack

    # High level function: send a message to a dest pub key hash (topic.)
    # Puts the msg in a sequenced queue called queue_id_hex to be republished.
    # A background dispatcher task loops over these queues to repub messages.
    def queue_msg(
        self,
        msg,
        dest_pk_hex,
        queue_id_hex=None,
        msg_type=MsgEnum.MSG,
        seq_no=None,
    ):
        """Queue a signed message for reliable delivery to the given destination public key."""
        queue_id_hex = queue_id_hex or to_h(rand_b(32))
        # Track activity so the Router idle-closer can leave busy
        # clients alone. A client is "active" when something has
        # asked it to publish in the last N minutes; we use the
        # send-side timestamp (queue_msg, not the actual ACK) so
        # an offline broker still keeps its client alive while
        # the dispatcher retries in the background.
        self.last_send = self.get_time()
        return ordered_ack_send(self, msg, dest_pk_hex, queue_id_hex, msg_type, seq_no)

    async def send_probe(
        self,
        dest_pk_hex,
        queue_id_hex=None,
    ):
        """Direct-publish a PROBE bypassing the dispatcher's backoff/jitter loop.

        Probes are time-bounded: they need to leave the wire
        immediately and fail-fast on timeout. The ordinary dispatcher
        path (queue_msg -> ordered_ack_send -> republish_meta) is
        designed for app-level reliable messaging where exponential
        backoff with jitter and 60s republish_duration make sense.
        Routing probes through it adds 0-2s pre-publish jitter and
        a 0.5s scan cadence; under concurrent probe load that
        compounds into multi-second startup delays which exceed the
        try_client budget and silently shrink the discovered
        broker set, producing the broker-set non-convergence bug
        seen across mixed old<->modern peer pairs.

        We still register the meta in msg_queues[PROBE] so that
        process_app_ack can resolve the future when MSGACK arrives,
        but we mark it `probe_one_shot` so republish_meta skips it
        entirely -- the probe is sent exactly once, here.
        """
        queue_id_hex = queue_id_hex or to_h(rand_b(32))
        if len(queue_id_hex) != 64:
            raise ValueError("queue_id_hex must be 64 hex chars, got {0}".format(len(queue_id_hex)))
        if len(dest_pk_hex) != 66:
            raise ValueError("dest_pk_hex must be 66 hex chars, got {0}".format(len(dest_pk_hex)))

        self.last_send = self.get_time()

        # Register the meta the same shape as ordered_ack_send so
        # process_app_ack's lookup keys hit. Setting probe_one_shot
        # makes republish_meta short-circuit on the next dispatcher
        # iteration and never republish.
        if queue_id_hex not in self.msg_queues[MsgEnum.PROBE]:
            self.msg_queues[MsgEnum.PROBE][queue_id_hex] = {}
        seq_no = len(self.msg_queues[MsgEnum.PROBE][queue_id_hex])

        # AppPacket pack signs and serialises the probe payload.
        # Local import keeps mqtt_client free of a top-level dep on
        # app_packet (mirrors what ordered_ack_send does).
        from .app_packet import AppPacket
        packet = AppPacket(
            queue_id_hex=queue_id_hex,
            seq_no=seq_no,
            msg_type=MsgEnum.PROBE,
            msg="",
        )
        out = packet.pack(self)

        app_ack = asyncio.Future()
        self.msg_queues[MsgEnum.PROBE][queue_id_hex][seq_no] = {
            "app_ack": app_ack,
            "dest_pk_hex": dest_pk_hex,
            "seq_no": seq_no,
            "out": out,
            "updated": 0,
            "created": self.get_time(),
            "probe_one_shot": True,
        }

        # Build the PUBLISH packet and ship it directly. publish()
        # also registers a PUBACK future, but we ignore it for probes
        # -- the relevant ack is the app-level MSGACK from the dest
        # peer (resolved via process_app_ack into app_ack above).
        buf, _ = self.publish(dest_pk_hex, out)
        if self.pipe and not self.pipe.on_close.is_set():
            await self.pipe.send(buf)

        return (queue_id_hex, seq_no), app_ack

    async def send_probe_ack(
        self,
        dest_pk_hex,
        queue_id_hex,
        seq_no,
    ):
        """Direct-publish a MSGACK in response to an inbound PROBE.

        Mirror image of send_probe for the receiver side. The
        responder receives a probe, queues a MSGACK, and we want
        that MSGACK on the wire IMMEDIATELY -- the originator's
        probe_timeout is ticking. Going through queue_msg ->
        ordered_ack_send -> dispatcher would add 0-2s jitter and a
        0.5s scan cadence which, on slow VMs (XP/Vista), routinely
        pushes the ack past the originator's 15s budget and
        produces silent broker-set non-convergence.

        We still register the meta in msg_queues[MSGACK] so the
        dedup check in process_app_probe (get_msg_from_queue)
        finds it on subsequent duplicate probe deliveries; flagged
        probe_one_shot so the dispatcher's republish_meta skips
        retransmit.
        """
        self.last_send = self.get_time()

        # Register meta the same shape as ordered_ack_send so
        # subsequent dedup lookups hit. seq_no comes from the
        # incoming probe so the (queue_id, seq_no) key matches.
        if queue_id_hex not in self.msg_queues[MsgEnum.MSGACK]:
            self.msg_queues[MsgEnum.MSGACK][queue_id_hex] = {}
        if seq_no in self.msg_queues[MsgEnum.MSGACK][queue_id_hex]:
            return  # already published this ack

        from .app_packet import AppPacket
        packet = AppPacket(
            queue_id_hex=queue_id_hex,
            seq_no=seq_no,
            msg_type=MsgEnum.MSGACK,
            msg="ack",
        )
        out = packet.pack(self)

        app_ack = asyncio.Future()
        self.msg_queues[MsgEnum.MSGACK][queue_id_hex][seq_no] = {
            "app_ack": app_ack,
            "dest_pk_hex": dest_pk_hex,
            "seq_no": seq_no,
            "out": out,
            "updated": 0,
            "created": self.get_time(),
            "probe_one_shot": True,
        }

        buf, _ = self.publish(dest_pk_hex, out)
        if self.pipe and not self.pipe.on_close.is_set():
            await self.pipe.send(buf)

    # Stops broadcasting a msg.
    def dequeue_msg(
        self,
        queue_id_hex,
        seq_no=None,
        msg_type=MsgEnum.MSG,
    ):
        """Stop retransmitting a queued message, optionally scoped to a sequence number."""
        # Get queue by queue id.
        queue = self.msg_queues[msg_type]
        if queue_id_hex not in queue:
            return

        # If no seq_no set delete the whole queue.
        if seq_no is None:
            del self.msg_queues[msg_type][queue_id_hex]
            return

        # Otherwise: disable it to preserve seq_no offsets.
        if seq_no not in self.msg_queues[msg_type][queue_id_hex]:
            return

        # Get the app ack future.
        app_ack = self.msg_queues[msg_type][queue_id_hex][seq_no]["app_ack"]
        if app_ack.done():
            return

        app_ack.set_result(True)

    # Internal: used to publish signed messages to pub key hashed topics.
    # Returns packet and future to await ack for the packet from the server.
    def publish(
        self, topic, payload, dup=False
    ):
        """Build a PUBLISH packet and return it with a future for the broker PUBACK."""
        if not is_ascii(topic):
            raise ValueError("topic must be ASCII")
        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError("payload must be bytes, got {}".format(type(payload).__name__))
        packet_id, packet_ack = packet_ack_future(self, MQTTEnum.PUBACK)
        buf = build_publish(topic, payload, packet_id, dup)
        return buf, packet_ack

    # Cleanly disconnect from MQTT server.
    async def close(self):
        """Cleanly shut down the dispatcher, send DISCONNECT, and close the pipe."""
        # Already closed.
        if self.is_closed.is_set():
            log(fstr("[MQTT-CLIENT-CLOSE] host={0} already closed, skipping", (self.host,)))
            return

        log(fstr("[MQTT-CLIENT-CLOSE] host={0} closing", (self.host,)))

        # Indicate closed to block other callers.
        self.is_closed.set()

        # Cancel message dispatcher bg task and wait for it to exit cleanly
        # so its CancelledError handler runs before we close the pipe.
        await cancel_task(self.dispatcher_task)
        self.dispatcher_task = None

        # Cleanly disconnect from the MQTT server.
        # The pipe sends disconnect, does shutdown, then close.
        if self.pipe:
            # Disconnect packet.
            disconnect_buf = build_disconnect()
            await async_wrap_errors(self.pipe.send(disconnect_buf))

            # Close pipe.
            await self.pipe.close()
            self.pipe = None

    async def __aenter__(self):
        """Connect on context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, *_):
        """Close on context manager exit."""
        await self.close()
        return False

    async def ping_handler(self):
        """Handle a PINGRESP from the server.

        MQTT keepalive is confirmed purely by receipt of the PINGRESP, so
        the default behaviour is intentionally a no-op. Tests override this
        method to observe that pings are flowing.
        """
        return


async def demo_mqtt():
    """Demo: connect two clients (Alice and Bob) and send a message."""
    nic = Interface("default")

    async def msg_handler(
        msg, src_pk_hex, queue_id_hex, client
    ):
        """Print the received message, sender public key, and queue ID during the demo."""
        print("msg handler got ", msg, " ", src_pk_hex, " ", queue_id_hex)

    alice_kp = Signing.keypair()
    alice_queue_id = hashlib.sha256(b"alice pipe").hexdigest()
    alice_client = MQTTClient(IP4, nic, ("ovh1.p2pd.net", 1883), alice_kp)

    bob_kp = Signing.keypair()
    bob_client = MQTTClient(IP4, nic, ("ovh1.p2pd.net", 1883), bob_kp)
    bob_client.add_msg_handler(msg_handler)

    try:
        await alice_client.connect()
        await bob_client.connect()

        _, bob_ack = alice_client.queue_msg(
            "hello bob -- with ordering and ack",
            to_hs(bob_kp.compact_public_key),
            alice_queue_id,
        )

        await bob_ack
        print("got ack from bob")
        await asyncio.sleep(4)
    finally:
        await alice_client.close()
        await bob_client.close()


if __name__ == "__main__":
    async_run(demo_mqtt())
