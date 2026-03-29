from aionetiface import *
from sidewire import *

# Server used for tests.
MQTT_SERVER = ("ovh1.p2pd.net", 1883)

def get_mqtt_client(server=MQTT_SERVER):
    nic = Interface("default")
    af = nic.supported()[0]
    kp = Signing.keypair()
    client = MQTTClient(af, nic, server, kp)
    return client

class TestMQTTClient(unittest.IsolatedAsyncioTestCase):
    async def test_something(self):
        print("here")

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
        client = get_mqtt_client()
        got_msg = asyncio.Event()

        async def msg_handler(msg, src_pk_hex, pipe_id_hex, client):
            if msg == buf:
                got_msg.set()

        client.add_msg_handler(msg_handler)

        await client.connect()

        print(client)

        pipe_id_hex = hashlib.sha256(b"pipe id").hexdigest()
        _, got_ack = client.send(
            buf,
            to_hs(client.kp.compact_public_key), # To self,
            pipe_id_hex
        )

        await asyncio.wait_for(got_ack, 3)
        assert(got_msg.is_set())
        await client.close()
        
    # Test seq send recv

    # test send recv multi
        



if __name__ == '__main__':
    main()


