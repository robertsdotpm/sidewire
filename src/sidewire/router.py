from aionetiface import *
from .mqtt import *
from .signing import *
from .utils import *

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
        found_clients = []
        while len(found_clients) < n:
            # Build next list of candidates from head of clients.
            candidates = []
            for _ in range(0, min(n - len(found_clients), len(clients))):
                candidates.append(clients.pop(0))

            # Failed to find dest on any server.
            if not len(candidates):
                return found_clients          
        
            # Ensure clients are connected.
            await ensure_clients_connected(candidates)

            # Check to see if dest is on any of the candidate servers.
            results = await find_dest_in_servers(candidates, dest_pub_hex)
            if results:
                found_clients += results

        return found_clients
            
    # Same function reusable by both sides.
    async def start(self):
        await self.get_dest_clients(self.kp.public_key_hex)
    

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
    out = await router.get_dest_clients(router.kp.public_key_hex)
    print(out)



if __name__ == "__main__":
    async_run(workspace())