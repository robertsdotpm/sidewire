from aionetiface import *
import sidewire

async def is_valid_mqtt(dest):
    found_msg = asyncio.Queue()

    # Executed on receipt of a new MQTT message.
    def mqtt_proto_closure(ret):
        def mqtt_proto(payload, client_tup, signal_client):
            found_msg.put_nowait(payload)

        return mqtt_proto

    # Setup MQTT client with basic proto.
    mqtt_proto = mqtt_proto_closure(found_msg)
    peer_id = to_s(rand_plain(10))
    client = SignalMock(peer_id, mqtt_proto, dest)

    # Try to start client.
    client = await async_wrap_errors(
        client.start(),
        timeout=2
    )

    # Cannot get client reference. Return failure.
    if client is None:
        return None

    # Send message to self and try receive it.
    for _ in range(0, 3):
        await client.send_msg(peer_id, peer_id)

        # Allow time to receive responds.
        await asyncio.sleep(0.1)
        if not found_msg.empty(): break

    # Wait for a reply.
    try:
        out = await asyncio.wait_for(found_msg.get(), 4.0)
        await client.close()
        return client
    except asyncio.TimeoutError:
        return None

class TestSignaling(unittest.IsolatedAsyncioTestCase):
    async def test_node_signaling(self):
        """
        ret = await is_valid_mqtt(("119.42.55.129", 1883))
        print(ret)

        return
        """
        
        msg = "test msg"
        peerid = to_s(rand_plain(10))
        nic = await Interface()
        for af in nic.supported():
            for server in MQTT_SERVERS:
                

                if server[af] is None:
                    continue

                dest = (server[af], server["port"])
                found_msg = []

                print(dest)
                client = await sidewire.is_valid_mqtt(dest)

                if not client:
                    print(fstr("mqtt {0} {1} broken", (af, dest,)))
                    continue
                else:
                    print(fstr("mqtt {0} {1} works", (af, dest,)))
                    

                await client.close()

if __name__ == '__main__':
    main()