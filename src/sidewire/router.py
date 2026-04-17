"""
Given a remote hex pub key: determines a list of MQTT clients that the dest is on.
It uses a probablistic algorithm based on rendezvous hashing. This approach has
several benefits:

- Neither side needs to know the others server list
- Server lists may change, offsets may change, fixed servers may go down
- Using the public key to yield a server list is adaptive to epemeral server
faults since deterministic ordering eventually may intersect a new server
- All of this takes place without coordination
- Easy to adapt to multiple address families

We use interleaving between server address families. This means the best IPv4
server is 0, then the best IPv6, then the second best IPv4, and so on. So you
can stream this sorted list to a connect process to easily converge on connected
clients regardless of what address families a NIC supports. Convergence is still
possible if at least one address families is shared.

The sorting algorithm uses SHA256 to produce uniformly distributed values, 
but using -log(U)  transforms them into an exponential distribution. This allows
fair and mathematically correct weighted selection, where servers with higher
weights are more likely to appear earlier in the ordering. The benefit is not just
spacing, but enabling proper probabilistic behavior for ranking and convergence.

Note: can just use sha256 sorted n but if weights are to be used in the future then
the math needs to be log(u, e).

If a server is down, maybe have a flag that disables it from reconnect retry in the
code otherwise it blocks the whole program for servers it already knows are down.
"""

import time
from aionetiface import *
from .mqtt import *
from .smart_pipe import *
from .utils import *

class Router:
    def __init__(self, kp, msg_handler=None, get_time=time.time, nic=None, servers=None):
        self.kp = kp
        self.servers = servers or get_mqtt_server_list()
        self.get_time = get_time
        self.nic = nic or Interface("default")

        # Build list of MQTT clients from server list.
        self.clients = {IP4: {}, IP6: {}}
        self.recv_msg_ids = {}
        for af in (IP4, IP6):
            for host in self.servers[af]:
                self.clients[af][host] = MQTTClient(
                    af,
                    self.nic,
                    (host, self.servers[af][host]["port"]),
                    self.kp,
                    get_time=get_time
                )

                if msg_handler:
                    self.clients[af][host].add_msg_handler(msg_handler)

                # Make all clients share the same recv_msg_ids queue.
                self.clients[af][host].recv_msg_ids = self.recv_msg_ids

                # Don't reconnect too frequently if server was down last.
                self.clients[af][host].last_connect = None

        # Pub key hex -> {"updated", "clients"}
        self.cache = {}
            
    # Same function reusable by both sides.
    async def start(self):
        clients = await get_dest_clients(
            self.nic,
            self.kp.public_key_hex,
            self.servers,
            self.clients
        )

        # Cache own pub key -> clients mapping.
        self.cache_clients(self.kp.public_key_hex, clients)
        return clients
    
    # Smart pipe intelligently routes over a set of MQTT clients.
    async def pipe(self, dest_pub_hex, use_cache=False, expiry=3600):
        now = self.get_time()
        cached_clients = None

        # Attempt to retrieve from cache
        if use_cache and dest_pub_hex in self.cache:
            entry = self.cache[dest_pub_hex]
            if (now - entry["updated"]) < expiry:
                cached_clients = entry["clients"]

        # If we have cached_clients, we pass them in to skip discovery
        smart_pipe = SmartPipe(self, dest_pub_hex, clients=cached_clients)
        
        # Connect (this performs rendezvous discovery ONLY if clients is None)
        await smart_pipe.connect()

        # Update cache if we performed a fresh discovery or need to refresh
        if use_cache and cached_clients is None:
            self.cache_clients(dest_pub_hex, smart_pipe.clients)

        return smart_pipe
    
    def cache_clients(self, pub_key_hex, clients):
        now = self.get_time()
        self.cache[pub_key_hex] = {
            "updated": now,
            "clients": clients
        }
    
    async def close(self):
        for af in self.clients:
            for host in self.clients[af]:
                client = self.clients[af][host]
                await client.close()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *_):
        await self.close()
        return False

async def workspace():
    nic = Interface("default")
    servers = get_mqtt_server_list()
    kp = Signing.keypair()
    router = Router(nic, kp, servers)
    try:
        out = await router.start()
        print("our own clients", out)

        async def msg_handler(msg, src_pk_hex, pipe_id_hex, client):
            print("got ", msg, " from ", src_pk_hex)

        # Already seen this client so shouldn't need to msg for liveliness.
        pipe = await router.pipe(router.kp.public_key_hex, msg_handler, use_cache=True)
        await pipe.send("hello world")
    finally:
        await router.close()

# TODO: dangling resource in the mqtt ping test code.

if __name__ == "__main__":
    async_run(workspace())