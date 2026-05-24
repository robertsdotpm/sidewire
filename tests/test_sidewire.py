from aionetiface import *
from aionetiface.testing import AsyncTestCase

from mqtt_test_helpers import pick_mqtt_servers


class TestSignaling(AsyncTestCase):
    async def test_something(self):
        nic = await Interface("default")
        print(nic.supported())

        last_err = None
        for server in pick_mqtt_servers():
            try:
                con = await Pipe(TCP, server, nic).connect()
                if con is not None and con.sock is not None:
                    print(con.sock)
                    return
            except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
                last_err = exc
                continue
        self.skipTest(
            "No MQTT server accepted a TCP connect (last error: {!r})".format(last_err)
        )


if __name__ == "__main__":
    unittest.main()
