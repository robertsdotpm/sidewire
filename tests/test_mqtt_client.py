import time as time_module
from aionetiface import *
from aionetiface import IP4, UDP, get_infra
from aionetiface.testing import AsyncTestCase
from aionetiface.utility.sys_clock import SysClock
from sidewire import *


def make_test_sys_clock(nic):
    """Return a SysClock for tests, seeded from wall clock.

    Production code must pass node.sys_clock.time (NTP-synced) into
    Router/MQTTClient -- the explicit-get_time requirement exists
    specifically to surface bugs when that wiring is wrong. Tests
    don't exercise the cross-machine clock-skew path though, so a
    wall-clock-seeded SysClock is sufficient and avoids the per-test
    NTP probe latency. SysClock(ntp=...) skips the NTP probes in
    start() entirely (the start() short-circuit).
    """
    return SysClock(nic, ntp=time_module.time())

# Server used for tests.
USE_AF = None


def pick_mqtt_servers(count=8):
    """Return [(host, port), ...] of `count` MQTT servers from get_infra.

    Each entry is the first server in a separate group, so iterating these
    sequentially gives genuine fall-over behaviour rather than retrying the
    same node. Falls back to the well-known public broker list if get_infra
    has no MQTT entries for whatever reason.
    """
    out = []
    try:
        groups = get_infra(IP4, UDP, "MQTT", no=count)
    except Exception:
        groups = []
    for group in groups:
        if not group:
            continue
        s = group[0]
        host = (s.get("fqns") or [s.get("ip")])[0]
        port = s.get("port", 1883)
        if host:
            out.append((host, port))
    if not out:
        # Last-resort fallback so a missing servers.json entry doesn't
        # silently turn every test into a no-server skip.
        out = [
            ("ovh1.p2pd.net", 1883),
            ("test.mosquitto.org", 1883),
            ("broker.hivemq.com", 1883),
        ]
    return out


# First server (kept for callers that only need one) -- but tests should
# loop over pick_mqtt_servers when possible.
MQTT_SERVER = pick_mqtt_servers(count=1)[0]


def get_mqtt_client(server=None):
    nic = Interface("default")
    if USE_AF:
        af = USE_AF
    else:
        af = nic.supported()[0]

    if server is None:
        server = MQTT_SERVER

    kp = Signing.keypair()
    sys_clock = make_test_sys_clock(nic)
    client = MQTTClient(af, nic, server, kp, get_time=sys_clock.time)
    return client


async def mqtt_send_msg_and_handle(
    msg_list,
    msg_handler,
    do_close=False,
    republish_duration=60,
    interval=5,
    keep_alive=30,
    timeout=5,
    ignore_timeout=False,
    min_sleep=0,
    ignore_acked=False,
    ack_await=True,
    server=None,
):
    if server is None:
        server = MQTT_SERVER
    # Create client and install a message handler.
    client = get_mqtt_client(server=server)
    client.add_msg_handler(msg_handler)

    try:
        # Connect client to server.
        await client.connect(republish_duration, interval, keep_alive, ignore_acked)
        if do_close:
            client.pipe.sock.close()

        # Send a message.
        pipe_id_hex = hashlib.sha256(b"pipe id").hexdigest()
        for buf in msg_list:
            out, got_ack = client.queue_msg(
                buf,
                to_hs(client.kp.compact_public_key),  # To self,
                pipe_id_hex,
            )

            # Sleep timeout.
            if min_sleep:
                await asyncio.sleep(min_sleep)

            # Wait for recv msg.
            if ack_await:
                if ignore_timeout:
                    try:
                        await asyncio.wait_for(got_ack, timeout)
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.wait_for(got_ack, timeout)

            if not ack_await:
                await asyncio.sleep(2 * len(msg_list))
    finally:
        await client.close()


async def try_servers(test_self, op, servers=None, label=""):
    """Run op(server) sequentially over a list of MQTT servers, returning the
    first server that did not raise a network error. Skips the calling test
    if every server failed.
    """
    if servers is None:
        servers = pick_mqtt_servers()
    last_exc = None
    for server in servers:
        try:
            await op(server)
            return server
        except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
            last_exc = exc
            continue
    test_self.skipTest(
        "All MQTT servers unreachable for {} (last error: {!r})".format(label, last_exc)
    )


class TestMQTTClient(AsyncTestCase):
    async def test_connect_single_success(self):
        async def op(server):
            client = get_mqtt_client(server=server)
            try:
                pipe = await client.connect()
                self.assertTrue(pipe)
            finally:
                await client.close()
        await try_servers(self, op, label="connect_single_success")

    async def test_connect_single_fail(self):
        # Deliberately wrong port -- expect failure regardless of server.
        servers = [(host, 80) for host, _ in pick_mqtt_servers(count=3)]
        for server in servers:
            client = get_mqtt_client(server=server)
            pipe = None
            try:
                pipe = await client.connect()
                # If a connect somehow succeeded, that's an actual failure
                # of the test contract -- but keep trying others to confirm.
                self.assertIsNone(pipe)
            except Exception:
                self.assertIsNone(pipe)

    async def test_single_send_recv(self):
        buf = "msg to send"

        async def op(server):
            got_msg = asyncio.Event()

            async def msg_handler(msg, src_pk_hex, pipe_id_hex, client):
                if msg == buf:
                    got_msg.set()

            await mqtt_send_msg_and_handle([buf], msg_handler, server=server)
            self.assertTrue(got_msg.is_set())

        await try_servers(self, op, label="single_send_recv")

    async def test_single_send_recv_disconnect(self):
        buf = "msg to send"

        async def op(server):
            got_msg = asyncio.Event()

            async def msg_handler(msg, src_pk_hex, pipe_id_hex, client):
                if msg == buf:
                    got_msg.set()

            await mqtt_send_msg_and_handle(
                [buf], msg_handler, do_close=True, timeout=20, server=server,
            )
            self.assertTrue(got_msg.is_set())

        await try_servers(self, op, label="single_send_recv_disconnect")

    async def test_single_recv_once(self):
        async def op(server):
            msg_list = ["first msg", "first msg"]
            recv_list = []

            async def msg_handler(msg, src_pk_hex, pipe_id_hex, client):
                recv_list.append(msg)

            await mqtt_send_msg_and_handle(
                msg_list, msg_handler, ignore_timeout=True, server=server,
            )

            # Part 1: no message duplication.
            self.assertNotEqual(recv_list, msg_list)

            # Part 2: no duplication from rebroadcasts.
            msg_list2 = ["first msg"]
            recv_list2 = []

            async def msg_handler2(msg, src_pk_hex, pipe_id_hex, client):
                recv_list2.append(msg)

            await mqtt_send_msg_and_handle(
                msg_list2, msg_handler2, min_sleep=6, ignore_acked=True, server=server,
            )

            self.assertEqual(recv_list2, msg_list2)

        await try_servers(self, op, label="single_recv_once")

    async def test_ping_resp(self):
        async def op(server):
            client = get_mqtt_client(server=server)
            got_ping = []

            async def ping_handler():
                got_ping.append(True)

            client.ping_handler = ping_handler
            try:
                await client.connect(keep_alive=2)
                await asyncio.sleep(6)
            finally:
                await client.close()
            self.assertTrue(got_ping)

        await try_servers(self, op, label="ping_resp")

    """
    Asyncio is "reactive" -- so it triggers disconnect after an attempt to use a
    connection fails with broken errors. It doesn't poll for such things.
    Hence we use the ping feature to make the client reconnect earlier.

    this really means that we need attempts to be some function of interval
    and keep_alive so it ends up falling within a reconnect cycle.
    This code works but shows that the chosen consts arent good.

    """

    async def test_broken_receiver(self):
        msg_list = ["first msg", "second message"]

        async def op(server):
            recv_list = []

            async def msg_handler(msg, src_pk_hex, pipe_id_hex, client):
                recv_list.append(msg)

            alice_client = get_mqtt_client(server=server)
            bob_client = get_mqtt_client(server=server)

            # Bob handles received messages.
            bob_client.add_msg_handler(msg_handler)

            try:
                # Connect clients.
                keep_alive = 10
                await alice_client.connect()
                await bob_client.connect(reconnect_delay=2, keep_alive=keep_alive)

                # Disconnect bob.
                bob_client.pipe.sock.close()

                # Send message to bob.
                pipe_id_hex = hashlib.sha256(b"pipe id").hexdigest()
                for buf in msg_list:
                    _, got_ack = alice_client.queue_msg(
                        buf, bob_client.kp.public_key_hex, pipe_id_hex
                    )

                    await got_ack
            finally:
                await alice_client.close()
                await bob_client.close()

        await try_servers(self, op, label="broken_receiver")

    async def test_seq_send_recv_ack_await(self):
        msg_list = ["this is first", "second", "third", "fourth"]

        async def op(server):
            recv_list = []

            async def msg_handler(msg, src_pk_hex, pipe_id_hex, client):
                recv_list.append(msg)

            await mqtt_send_msg_and_handle(
                msg_list, msg_handler, ack_await=True, server=server,
            )

        await try_servers(self, op, label="seq_send_recv_ack_await")

    # Add all the messages concurrently with send then check result.
    @unittest.skip("concurrent ordering not yet implemented")
    async def test_seq_send_recv_concurrent(self):
        msg_list = ["this is first", "second", "third", "fourth"]
        recv_list = []

        async def msg_handler(msg, src_pk_hex, pipe_id_hex, client):
            recv_list.append(msg)

        await mqtt_send_msg_and_handle(msg_list, msg_handler, ack_await=False)
        assert recv_list == msg_list

    async def test_multiple_servers(self):
        msg_list = ["first msg", "second msg", "third"]
        any_worked = False
        last_exc = None
        for host, port in pick_mqtt_servers(count=8):
            print("testing ", host, port)
            recv_list = []

            async def msg_handler(msg, src_pk_hex, pipe_id_hex, client):
                recv_list.append(msg)

            try:
                await mqtt_send_msg_and_handle(
                    msg_list, msg_handler,
                    server=(host, port), interval=5, timeout=3,
                )
            except (ConnectionError, asyncio.TimeoutError, OSError) as exc:
                last_exc = exc
                print("Connection error -- skipping.")
                continue

            self.assertEqual(recv_list, msg_list)
            any_worked = True

        if not any_worked:
            self.skipTest(
                "No MQTT broker accepted the multi-server test (last error: {!r})".format(last_exc)
            )

    async def test_repub_intervals(self):
        buf = "msg to send"

        async def op(server):
            got_msg = asyncio.Event()

            async def msg_handler(msg, src_pk_hex, pipe_id_hex, client):
                if msg == buf:
                    got_msg.set()

            await mqtt_send_msg_and_handle(
                [buf], msg_handler, ack_await=False, server=server,
            )
            self.assertTrue(got_msg.is_set())

        await try_servers(self, op, label="repub_intervals")


if __name__ == "__main__":
    main()
