import argparse
from aionetiface import *
from sidewire import *

# Server used for tests.
USE_AF = None
MQTT_SERVER = ("ovh1.p2pd.net", 1883)

def get_mqtt_client(server=MQTT_SERVER):
    nic = Interface("default")
    if USE_AF:
        af = USE_AF
    else:
        af = nic.supported()[0]

    kp = Signing.keypair()
    client = MQTTClient(af, nic, server, kp)
    return client

async def mqtt_send_msg_and_handle(msg_list, msg_handler, do_close=False, republish_duration=60, interval=2, keep_alive=30, timeout=5, ignore_timeout=False, min_sleep=0, ignore_acked=False):
    # Create client and install a message handler.
    client = get_mqtt_client()
    client.add_msg_handler(msg_handler)

    # Connect client to server.
    await client.connect(republish_duration, interval, keep_alive, ignore_acked)
    if do_close:
        client.pipe.sock.close()

    # Send a message.
    pipe_id_hex = hashlib.sha256(b"pipe id").hexdigest()
    for buf in msg_list:
        _, got_ack = client.send(
            buf,
            to_hs(client.kp.compact_public_key), # To self,
            pipe_id_hex
        )

        # Sleep timeout.
        if min_sleep:
            await asyncio.sleep(min_sleep)

        # Wait for recv msg.
        if ignore_timeout:
            try:
                await asyncio.wait_for(got_ack, timeout)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.wait_for(got_ack, timeout)

    # Cleanup.
    await client.close()

class TestMQTTClient(unittest.IsolatedAsyncioTestCase):
    async def test_connect_single_success(self):
        client = get_mqtt_client()
        pipe = await client.connect()
        assert(pipe)
        await pipe.close()
        
    async def test_connect_single_fail(self):
        client = get_mqtt_client(("ovh1.p2pd.net", 80))
        pipe = None
        try:
            pipe = await client.connect()
            raise Exception("mqtt fail con test invalid")
        except asyncio.TimeoutError:
            assert(pipe is None)

    async def test_single_send_recv(self):
        buf = "msg to send"
        got_msg = asyncio.Event()

        async def msg_handler(msg, src_pk_hex, pipe_id_hex, client):
            if msg == buf:
                got_msg.set()

        await mqtt_send_msg_and_handle([buf], msg_handler)
        assert(got_msg.is_set())

    async def test_seq_send_recv(self):
        msg_list = ["this is first", "second", "third", "fourth"]
        recv_list = []

        async def msg_handler(msg, src_pk_hex, pipe_id_hex, client):
            recv_list.append(msg)

        await mqtt_send_msg_and_handle(msg_list, msg_handler)
        assert(recv_list == msg_list)

    async def test_single_send_recv_disconnect(self):
        buf = "msg to send"
        got_msg = asyncio.Event()

        async def msg_handler(msg, src_pk_hex, pipe_id_hex, client):
            if msg == buf:
                got_msg.set()

        await mqtt_send_msg_and_handle([buf], msg_handler, do_close=True)
        assert(got_msg.is_set())

    async def test_single_recv_once(self):
        msg_list = ["first msg", "first msg"]
        recv_list = []

        async def msg_handler(msg, src_pk_hex, pipe_id_hex, client):
            recv_list.append(msg)

        await mqtt_send_msg_and_handle(msg_list, msg_handler, ignore_timeout=True)

        # Part 1: no message duplication.
        assert(recv_list != msg_list)
        #assert(recv_list == msg_list)
    

        # Part 2: no duplication from rebroadcasts.
        msg_list = ["first msg"]
        recv_list = []

        async def msg_handler(msg, src_pk_hex, pipe_id_hex, client):
            recv_list.append(msg)

        # Allows time for rebroadcast.
        await mqtt_send_msg_and_handle(msg_list, msg_handler, min_sleep=6, ignore_acked=True)

        assert(recv_list == msg_list)

    async def test_ping_resp(self):
        client = get_mqtt_client()
        got_ping = []

        async def ping_handler():
            got_ping.append(True)

        client.ping_handler = ping_handler
        await client.connect(keep_alive=2)
        await asyncio.sleep(6)
        await client.close()
        assert(len(got_ping))

    """
    Asyncio is "reactive" -- so it triggers disconnect after an attempt to use a
    connection fails with broken errors. It doesn't poll for such things.
    Hence we use the ping feature to make the client reconnect earlier.

    this really means that we need attempts to be some function of interval
    and keep_alive so it ends up falling within a reconnect cycle.
    This code works but shows that the chosen consts arent good.

    """
    async def test_broken_receiver(self):
        msg_list = ["first msg"]
        recv_list = []

        async def msg_handler(msg, src_pk_hex, pipe_id_hex, client):
            recv_list.append(msg)

        alice_client = get_mqtt_client()
        bob_client = get_mqtt_client()

        # Bob handles received messages.
        bob_client.add_msg_handler(msg_handler)

        # Connect clients.
        keep_alive = 10
        await alice_client.connect()
        await bob_client.connect(reconnect_delay=2, keep_alive=keep_alive)

        # Disconnect bob.
        bob_client.pipe.sock.close()

        # Send message to bob.
        pipe_id_hex = hashlib.sha256(b"pipe id").hexdigest()
        for buf in msg_list:
            _, got_ack = alice_client.send(
                buf,
                bob_client.kp.public_key_hex,
                pipe_id_hex
            )
            
            await got_ack

        await alice_client.close()
        await bob_client.close()

    # Add all the messages concurrently with send then check result.
    async def test_seq_send_recv_revisited(self):
        msg_list = ["this is first", "second", "third", "fourth"]
        recv_list = []

        async def msg_handler(msg, src_pk_hex, pipe_id_hex, client):
            recv_list.append(msg)

        await mqtt_send_msg_and_handle(msg_list, msg_handler)
        assert(recv_list == msg_list)


    # test send recv multi
    # Check other servers work

# I dont think the dispatch loop actually waits for acks in order
    # Todo fix this -- current code has caller handling the scheduling
# TODO: handle disconnect message.
# todo write docs at top of different files

if __name__ == '__main__':
    main()


