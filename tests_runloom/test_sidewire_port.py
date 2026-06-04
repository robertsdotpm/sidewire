"""
Validate the sidewire runloom sync port -- plain blocking code on runloom's
M:N scheduler, on top of the ported aionetiface + shims.

sidewire is a signed/sequenced application protocol over MQTT.  We validate:
  - import as pure sync
  - the MQTT wire codec (build -> parse roundtrip), local + deterministic
  - a REAL MQTT session against the public broker test.mosquitto.org:1883
    (no special infra): two clients connect, and one sends a signed,
    sequenced message that the other receives -- the full sidewire stack
    (mqtt client + app protocol + aionetiface Pipe) running as sync code.

Run: PYTHON_GIL=0 PYTHONPATH=<paths> python3.13t this.py [hubs]
"""
import sys
import time
import hashlib
import traceback

import runloom_boot
runloom_boot.install()

import sidewire  # noqa: E402
from sidewire import MQTTClient  # noqa: E402
from sidewire.mqtt.mqtt_msgs import build_connect, build_subscribe  # noqa: E402
from sidewire.mqtt.mqtt_packet import mqtt_parse_packet  # noqa: E402
from aionetiface import Interface, Signing, IP4, to_hs  # noqa: E402
import runloom  # noqa: E402

BROKER = ("test.mosquitto.org", 1883)
RESULTS = []


def check(name, fn):
    """Run one check, recording (name, ok, detail)."""
    sys.stderr.write("  .. running %s\n" % name)
    sys.stderr.flush()
    t0 = time.time()
    try:
        RESULTS.append((name, True, fn(), round(time.time() - t0, 2)))
    except BaseException:
        RESULTS.append((name, False, traceback.format_exc(), round(time.time() - t0, 2)))


def check_mqtt_packet_roundtrip():
    """Build MQTT CONNECT/SUBSCRIBE packets and parse them back (wire codec)."""
    connect_pkt = build_connect("runloom-client-id", keep_alive=30)
    assert connect_pkt[:1] == b"\x10", "CONNECT packet type byte wrong"
    parsed = mqtt_parse_packet(connect_pkt)
    assert parsed is not None, "CONNECT failed to parse"

    sub_pkt = build_subscribe("runloom/topic", b"\x00\x01")
    assert sub_pkt[:1] == b"\x82", "SUBSCRIBE packet type byte wrong"
    assert mqtt_parse_packet(sub_pkt) is not None, "SUBSCRIBE failed to parse"
    return "CONNECT(%d B)+SUBSCRIBE(%d B) build/parse ok" % (
        len(connect_pkt), len(sub_pkt))


def check_real_mqtt_connect():
    """Open a real MQTT session to the public broker (CONNECT/CONNACK + subscribe)."""
    nic = Interface("default")
    kp = Signing.keypair()
    client = MQTTClient(IP4, nic, BROKER, kp, get_time=time.time)
    try:
        pipe = client.connect(timeout=8)
        assert pipe is not None, "connect returned no pipe"
        return "connected to %s:%d, subscribed to own topic" % BROKER
    finally:
        client.close()


def check_real_pubsub():
    """Two clients on the real broker; one sends a signed message the other receives."""
    nic = Interface("default")
    got = {}

    def on_msg(msg, src_pk_hex, queue_id_hex, client):
        """Record a received application message."""
        got["msg"] = msg

    alice_kp = Signing.keypair()
    bob_kp = Signing.keypair()
    alice = MQTTClient(IP4, nic, BROKER, alice_kp, get_time=time.time)
    bob = MQTTClient(IP4, nic, BROKER, bob_kp, get_time=time.time)
    bob.add_msg_handler(on_msg)
    try:
        alice.connect(timeout=8)
        bob.connect(timeout=8)
        # Give SUBSCRIBE a moment to register on the broker.
        time.sleep(1.0)

        queue_id = hashlib.sha256(b"runloom-pipe").hexdigest()
        alice.queue_msg("hello-from-runloom", to_hs(bob_kp.compact_public_key), queue_id)

        # Wait for delivery (broker latency + dispatcher republish).
        deadline = time.time() + 12
        while "msg" not in got and time.time() < deadline:
            time.sleep(0.2)

        assert got.get("msg") == "hello-from-runloom", "received %r" % (got.get("msg"),)
        return "alice -> bob signed/sequenced message delivered via real broker"
    finally:
        alice.close()
        bob.close()


def main():
    """Run sidewire checks as plain sync code on the runloom scheduler."""
    check("mqtt_packet_roundtrip", check_mqtt_packet_roundtrip)
    check("real_mqtt_connect", check_real_mqtt_connect)
    check("real_pubsub", check_real_pubsub)


if __name__ == "__main__":
    hubs = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    print("=== sidewire runloom sync port: runloom_boot.run(main, hubs=%d) ===" % hubs)
    runloom_boot.run(main, hubs=hubs)
    failed = 0
    for nm, ok, detail, secs in RESULTS:
        if ok:
            print("  PASS  %-24s %5.2fs  %s" % (nm, secs, detail))
        else:
            failed += 1
            print("  FAIL  %-24s %5.2fs" % (nm, secs))
            print("        " + detail.replace("\n", "\n        "))
    print("=== %d passed, %d failed ===" % (len(RESULTS) - failed, failed))
    sys.exit(1 if failed else 0)
