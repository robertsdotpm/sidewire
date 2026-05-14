# sidewire

Part of the Warpgate project: <https://www.warpgate.io/>

Federated, registrationless signaling for P2P apps over a shared pool of
public MQTT brokers. Two peers that know each other's public key can
exchange small signed messages over MQTT — neither side needs to coordinate
which broker the other is connected to. Rendezvous hashing on the
destination's public key produces the same broker ordering on both ends.

Used by Warpgate as the rendezvous / hole-punch signaling channel, but the
API is general-purpose: sign a message to a hex pubkey, the other side gets
it.

## Install

```sh
pip install sidewire
```

## Quick start

```python3
import asyncio
from aionetiface import Interface
from aionetiface.utility.signing import Signing
from sidewire import Router

# Time source.  Pass an NTP-corrected clock for production; time.time
# is fine for a single-host demo.  Router REQUIRES get_time (no implicit
# fallback) so cross-machine clock skew can't silently masquerade as a
# signaling bug.
import time


async def main():
    # Identity: an ECDSA secp256k1 keypair.  Persist & reuse this across
    # runs if you want a stable identity.  namebump.Keypair instances
    # work here too (Router duck-types both shapes).
    kp = Signing.keypair()

    # Message handler -- called once per inbound application message.
    # Signature: (payload_bytes, sender_pub_bytes, queue_id, mqtt_client)
    async def on_msg(msg, src_pk, queue_id, client):
        print("from {0}: {1!r}".format(src_pk.hex()[:12], msg))

    async with Router(kp, msg_handler=on_msg, get_time=time.time) as router:
        # Open a virtual pipe to a destination hex pubkey.  Discovery
        # ranks every broker in the configured pool for this dest and
        # connects to enough of them to maximise overlap with whichever
        # brokers the destination is subscribed at.
        pipe = await router.pipe(dest_pub_hex="03ab...")  # 66-char hex
        await pipe.send(b"hello")

        # router stays alive (idle-closer keeps the inbound subscription
        # open) so on_msg can fire for inbound traffic.  Block here until
        # the app wants to shut down.
        await asyncio.Event().wait()


asyncio.run(main())
```

## API

### `Router(kp, msg_handler=None, get_time=<required>, nic=None, servers=None)`

The top-level connection manager. Maintains one `MQTTClient` per broker in
the pool, opens inbound subscriptions on the rendezvous-set for *your own*
pubkey at startup (these stay open for the Router's lifetime), and creates
`SmartPipe` instances on demand for outbound traffic.

| Parameter | Type | Notes |
| --- | --- | --- |
| `kp` | `Signing` or `namebump.Keypair` | Identity. `kp.public_key_hex` (or `kp.vkc.hex()`) is what peers address you by. |
| `msg_handler` | `async (msg, src_pk, queue_id, client) -> None` | Optional initial handler. Wired to every managed client. Add more later with `router.add_msg_handler(...)`. |
| `get_time` | `callable() -> float` | **Required.** Pass `node.sys_clock.time` from aionetiface for NTP-corrected wall time, or `time.time` for local-only. No implicit fallback because clock skew is a real signaling failure mode and the explicit param flushes it out at construction. |
| `nic` | `Interface` or `AFGroup` or `None` | Network interface to bind outgoing MQTT connections on. `None` → `Interface("default")`. Pass an `AFGroup` to route v4 brokers via one NIC and v6 via another (mobile CGNAT + home broadband, etc). |
| `servers` | dict or `None` | Override the broker pool. `None` reads from aionetiface's bundled `INFRA["MQTT"]` (the curated public list). |

### `await router.start()` / `await router.close()`

Connect to / disconnect from brokers. The `async with Router(...) as r:`
shape calls these for you.

### `await router.pipe(dest_pub_hex, use_cache=False, expiry=3600, hint_brokers=None)`

Returns a `SmartPipe` to the destination. The pipe is "smart" because:

- It runs rendezvous-hash discovery against the broker pool to find which
  brokers the destination is most likely subscribed at, and connects to
  those in parallel.
- If `hint_brokers` is provided (a list of `{"af", "host", "port"}` dicts —
  typically what the destination advertised in its own address bytes), it
  tries those first because the dest is **guaranteed** subscribed at any
  broker in its own protected set. Hint-based connect bypasses the
  rendezvous-set non-convergence corner case where peers compute slightly
  different broker rankings.
- `use_cache=True` caches the resolved broker set per destination so
  follow-up pipes to the same peer skip discovery; entries expire after
  `expiry` seconds.

### `await pipe.send(msg, timeout=4)`

Fan-outs the signed message to every connected broker in the pipe's set in
parallel. Returns the byte length on the **first** broker that ACKs (the
others are cancelled), or `0` if no broker ACKed within `timeout`. The
many-broker fan-out is what gets you reliability under flaky brokers
without retry loops in the caller.

### `await pipe.close()`

Evicts the pipe's clients from the router's discovery cache but does NOT
close the underlying `MQTTClient` instances — those are owned by the
Router and shared across pipes. Pipes are cheap to throw away.

### `router.add_msg_handler(handler)`

Register an additional message handler on every managed client. Handlers
are stored in a set, so calling with the same handler twice is a no-op.

## How discovery works

Each broker in the pool gets ranked per destination pubkey using

```text
score(broker, dest) = -log( H(broker_id || dest_pub_hex) ) / weight
```

Lower scores rank earlier. v4 and v6 results are interleaved so two peers
with different AF support can still converge (the v4-only peer sees v4
rankings first; the v6-only peer sees v6 first; they both eventually walk
into the same shared rendezvous set).

Why rendezvous hashing rather than a DHT or a fixed mapping:

- No coordination between peers.
- Broker join/leave is graceful — adding a broker shifts a fraction of
  rankings; removing one only affects destinations that ranked it first.
- Deterministic — same pubkey, same broker list, same ranking on every
  call, on every host. Reproducible across restarts.
- Weighted — high-capacity brokers can pull more traffic by raising
  their `weight` entry in the pool config.

## Identity & message envelope

Every outbound application message is wrapped in an `AppPacket`:

- Sequence number per (source, dest, queue) tuple for ordered delivery.
- Message type field (data / ack / control).
- ECDSA signature over the payload using the sender's keypair.

The receiver verifies the signature against the sender's pubkey before
firing the message handler. Replay protection is per-message (the
`recv_msg_ids` set tracks recently-seen message hashes across all clients
in the Router), and the signature plus per-queue sequence number make
out-of-order or duplicate ACKs harmless.

## Reliability features

- **Many-broker fan-out per send** — first ACK wins, rest cancel. Single
  flaky broker can't stall a send.
- **Inbound subscription protection** — the brokers carrying your own
  inbound subscription are exempt from the idle closer; they stay open
  for the Router's lifetime regardless of send activity.
- **Aggressive idle closer** — outbound-only clients close after
  `IDLE_CLIENT_TIMEOUT` seconds (default 600) so a long-running node
  doesn't accumulate broker rate-limit pressure across the pool. Reconnect
  is sub-second when traffic resumes.
- **Per-broker rate-limit tracking** — failed connects are rate-limited
  per host so a dead broker doesn't burn the per-call timeout budget on
  every send.

## Running with your own broker pool

`Router(..., servers=...)` accepts a dict in the same shape as
`aionetiface.INFRA["MQTT"]`:

```python3
custom_servers = {
    IP4: {
        "broker.example.com": {"port": 1883, "weight": 1.0},
        ...
    },
    IP6: {
        "broker.example.com": {"port": 1883, "weight": 1.0},
        ...
    },
}
async with Router(kp, get_time=time.time, servers=custom_servers) as r:
    ...
```

The brokers don't need to know about sidewire — they just need to be
publicly reachable, accept anonymous connections (or shared creds you
bake into the client config), and support standard MQTT 3.1.1 publish /
subscribe. Both peers must use the same broker pool config for rendezvous
ranking to converge.
