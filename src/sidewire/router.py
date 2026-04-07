from aionetiface import *
from .mqtt import *
from .signing import *
from .utils import *
import hashlib
import math
import copy

class Router:
    def __init__(self, nic, kp, servers):
        self.nic = nic
        self.kp = kp
        self.servers = servers

        # Build list of MQTT clients from server list.
        self.clients = {IP4: {}, IP6: {}}
        for af in (IP4, IP6):
            for host in self.servers[af]:
                self.clients[af][host] = MQTTClient(
                    af, 
                    self.nic,
                    (host, self.servers[af][host]["port"]),
                    self.kp
                )

    async def get_dest_clients(self, dest_pub_hex, n=3):
        clients = []
        sorted_servers = rendezvous_hash(self.nic, dest_pub_hex, self.servers)
        for server in sorted_servers:
            af = server["af"]
            host = server["host"]
            client = self.clients[af][host]
            clients.append(client)

        # Build a list of clients that converge with dest_pub_hex.
        while len(clients):
            candidates = []
            for _ in range(0, n):
                candidates.append(clients.pop(0))

            await ensure_clients_connected(clients)
            found_dest 
    

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

    dest_clients = router.get_dest_clients(kp.public_key_hex)
    print(dest_clients)


if __name__ == "__main__":
    async_run(workspace())