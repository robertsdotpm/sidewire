from aionetiface import *
import hashlib
import math
import copy
import asyncio

def rendezvous_hash(nic, pub_key_hex, servers):
    process_list = []
    for af in nic.supported():
        for host in servers[af]:
            # Record server to score.
            server = copy.deepcopy(servers[af][host])
            process_list.append(server)

            # Starting value to run through scoring func.
            h = hashlib.sha256(
                bytes([int(af)]) + 
                h_to_b(pub_key_hex) + 
                to_b(host)
            ).digest()

            # Convert hex to an integer.
            int_hash = int.from_bytes(h, 'big')

            """
            Converts massive int 256 bit value into range from 1 to < 1
            as a decimal. This is used for the next trick with log.
            """
            one_or_less = (int_hash + 1) / (2 ** 256)

            """
            When a number is less than one: math.log expands differences.
            They all fit on the same line so large numbers don't adversely
            effect clustering of final values. The field ends up being fair.
            """
            even_playing_field = -math.log(one_or_less)

            # optional weighting:
            # score /= server.get("weight", 1)

            # Record score for server.
            server["score"] = even_playing_field

    # Now sort process list by smallest scores first.
    return sorted(process_list, key=lambda v: v["score"])

async def ensure_clients_connected(clients, timeout=4):
    async def worker(client):
        try:
            if client.dispatcher_task is None:
                await asyncio.wait_for(
                    client.connect(),
                    timeout
                )
            
            return True
        except Exception:
            log_exception()
            return False

    tasks = [worker(client) for client in clients]
    await asyncio.gather(*tasks)

async def find_dest_in_servers(clients, dest_pub_hex, timeout=3):
    async def worker(client):
        if client.dispatcher_task is None:
            return None
        
        _, ack = client.queue_msg("hello", dest_pub_hex)
        try:
            await asyncio.wait_for(ack, timeout)
            return client
        except asyncio.TimeoutError:
            return None
    
    tasks = [worker(client) for client in clients]
    results = await asyncio.gather(*tasks)
    return strip_none(results)

async def get_dest_clients(nic, dest_pub_hex, servers, clients, n=3, max_servers=20):
    clients = []
    sorted_servers = rendezvous_hash(nic, dest_pub_hex, servers)
    for server in sorted_servers:
        af = server["af"]
        host = server["host"]
        client = clients[af][host]
        clients.append(client)

    # Build a list of clients that converge with dest_pub_hex.
    found_clients = []
    tried_servers = 0
    while len(found_clients) < n and tried_servers < max_servers:
        # Build next list of candidates from head of clients.
        candidates = []

        # Calculate batch size with respect to n, remaining, and max_servers.
        batch_size = min(
            n - len(found_clients),
            len(clients),
            max_servers - tried_servers
        )
        if not batch_size:
            break

        # Try the next list of servers to find dest at.
        for _ in range(0, batch_size):
            candidates.append(clients.pop(0))

        # Update tried server count.
        tried_servers += batch_size
    
        # Ensure clients are connected.
        await ensure_clients_connected(candidates)

        # Check to see if dest is on any of the candidate servers.
        results = await find_dest_in_servers(candidates, dest_pub_hex)
        if results:
            found_clients += results

    return found_clients