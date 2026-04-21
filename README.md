# sidewire

A decentralized MQTT-based signaling layer for P2P applications.

sidewire routes messages between peers using rendezvous hashing over a shared pool of public MQTT brokers. Neither side needs to know which servers the other is connected to — the shared key deterministically produces the same server ordering on both ends.

## Key concepts

- **Router** — connects to MQTT brokers, sends and receives signed messages keyed by public key hex. Supports caching of peer-to-server mappings to avoid repeated discovery.
- **SmartPipe** — a virtual pipe over multiple MQTT clients that automatically routes a message to the first broker where both peers are connected.
- **AppPacket** — wire-format envelope for signed P2P messages. Includes a per-queue sequence number for ordered delivery, a message type field, and an ECDSA signature over the payload.
- **MQTTClient** — low-level async MQTT client. Handles subscribe/publish, PUBACK sequencing, and reconnect.

## Usage

```python
from aionetiface import Interface
from sidewire import Router
from sidewire.utils import get_mqtt_server_list
from aionetiface.net.signing import Signing

nic = Interface("default")
kp  = Signing.keypair()

async def on_message(msg, src_pk_hex, pipe_id_hex, client):
    print("received:", msg, "from", src_pk_hex)

async with Router(kp, msg_handler=on_message, nic=nic) as router:
    pipe = await router.pipe(dest_pub_key_hex)
    await pipe.send("hello")
```

## Server selection algorithm

Servers are ranked per destination key using the rendezvous hashing formula
`-log(H(server_id || key)) / weight`. IPv4 and IPv6 results are interleaved so
convergence is possible even when peers support different address families.

## Installation

```bash
pip install sidewire
```
