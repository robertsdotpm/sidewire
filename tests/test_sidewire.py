from aionetiface import *
from aionetiface import IP4, UDP, get_infra
from aionetiface.testing import AsyncTestCase


def pick_mqtt_servers(count=5):
    """Return [(host, port), ...] for `count` MQTT servers from get_infra."""
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
        out = [
            ("ovh1.p2pd.net", 1883),
            ("test.mosquitto.org", 1883),
        ]
    return out


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
