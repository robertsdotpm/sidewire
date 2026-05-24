"""Shared helpers for sidewire's MQTT-related tests.

`pick_mqtt_servers` is consumed by both `test_sidewire.py` and
`test_mqtt_client.py`; previously each had its own copy with a
slightly different fallback list.  This module is the canonical home.
"""

from aionetiface import get_infra, IP4, UDP


def pick_mqtt_servers(count=8):
    """Return [(host, port), ...] of `count` MQTT servers from get_infra.

    Each entry is the first server in a separate group, so iterating
    these sequentially gives genuine fall-over behaviour rather than
    retrying the same node.  Falls back to a small well-known public
    broker list if get_infra has no MQTT entries.
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
