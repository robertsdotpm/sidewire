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

async def mqtt_send_msg_and_handle(msg_list, msg_handler, do_close=False, attempts=3, interval=2, keep_alive=30, timeout=5, ignore_timeout=False, min_sleep=0, ignore_acked=False):
    # Create client and install a message handler.
    client = get_mqtt_client()
    client.add_msg_handler(msg_handler)

    # Connect client to server.
    await client.connect(attempts, interval, keep_alive, ignore_acked)
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
        print(recv_list)


    # test send recv multi

    # Check other servers work

    # test retransmit works if other client disconnects (simulate it being down for a moment.)

    # Check that ping is working.

# TODO: handle disconnect message.
# todo write docs at top of different files

if __name__ == '__main__':
    main()


