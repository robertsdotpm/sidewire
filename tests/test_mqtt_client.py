import argparse
from aionetiface import *
from sidewire import *

# Server used for tests.
USE_AF = IP4
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

async def mqtt_send_msg_and_handle(msg_list, msg_handler):
    # Create client and install a message handler.
    client = get_mqtt_client()
    client.add_msg_handler(msg_handler)

    # Connect client to server.
    await client.connect()

    # Send a message.
    pipe_id_hex = hashlib.sha256(b"pipe id").hexdigest()
    for buf in msg_list:
        _, got_ack = client.send(
            buf,
            to_hs(client.kp.compact_public_key), # To self,
            pipe_id_hex
        )

        # Wait for recv msg.
        await got_ack

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

            

    # test send recv multi

    # Check other servers work

    # Test disconnect rebroadcast
        



if __name__ == '__main__':
    main()


