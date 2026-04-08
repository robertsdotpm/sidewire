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

from aionetiface import *
from .mqtt import *
from .signing import *
from .smart_pipe import *
from .utils import *

class Router:
    def __init__(self, nic, kp, servers):
        self.nic = nic
        self.kp = kp
        self.servers = servers

        # Build list of MQTT clients from server list.
        self.clients = {IP4: {}, IP6: {}}
        self.recv_msg_ids = {}
        for af in (IP4, IP6):
            for host in self.servers[af]:
                self.clients[af][host] = MQTTClient(
                    af, 
                    self.nic,
                    (host, self.servers[af][host]["port"]),
                    self.kp
                )

                # Make all clients share the same recv_msg_ids queue.
                self.clients[af][host].recv_msg_ids = self.recv_msg_ids

                # Don't reconnect too frequently if server was down last.
                self.clients[af][host].last_connect = None
            
    # Same function reusable by both sides.
    async def start(self):
        return await get_dest_clients(
            self.nic,
            self.kp.public_key_hex,
            self.servers,
            self.clients
        )
    
    # Smart pipe intelligently routes over a set of MQTT clients.
    def pipe(self, dest_pub_hex):
        smart_pipe = SmartPipe(self, dest_pub_hex)
        return smart_pipe
    
    async def close(self):
        for af in self.clients:
            for host in self.clients[af]:
                client = self.clients[af][host]
                await client.close()
    

async def workspace():
    print("workspace.")
    nic = Interface("default")
    print(nic.supported())
    #print(INFRA["MQTT"])

    servers = {
        IP4: {},
        IP6: {},
    }
    servers["IPv4"] = servers[IP4]
    servers["IPv6"] = servers[IP6]

    # Norm server list.
    for af_txt in ("IPv4", "IPv6"):
        for server_list in INFRA["MQTT"][af_txt]["UDP"]:
            #print(server_list)
            hosts = sorted(server_list[0]["fqns"])
            if len(hosts):
                host = hosts[0]
            else:
                host = server_list[0]["ip"]

            server_list[0]["host"] = host
            servers[af_txt][host] = server_list[0]

    kp = Signing.keypair()
    router = Router(nic, kp, servers)

    #out = router.rendezvous_hash(kp.public_key_hex)
    #print(out)
    out = await router.start()
    print(out)

    async def msg_handler(msg, src_pk_hex, pipe_id_hex, client):
        print("got ", msg, " from ", src_pk_hex)

    pipe = router.pipe(router.kp.public_key_hex)
    await pipe.connect(msg_handler)
    await pipe.send("hello world")

    await router.close()



if __name__ == "__main__":
    async_run(workspace())