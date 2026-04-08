from aionetiface import *
import hashlib
import math
import copy
import asyncio

def get_server_score(af, host, pub_key_hex):
    """
    Calculates the rendezvous score for a specific server.
    """
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
    
    return even_playing_field

def interleave_buckets(af_buckets):
    """
    Interleaves the results to guarantee diversity in the top N.
    This ensures IPv4 and IPv6 servers both appear at the start of the list.
    """
    # Max len calculation.
    max_lengths = [len(bucket) for bucket in af_buckets.values()]
    max_len = max(max_lengths) if max_lengths else 0
    process_list = []
    for i in range(max_len):
        # Sorted keys for consistency across nodes
        for af in sorted(af_buckets.keys()):
            if i < len(af_buckets[af]):
                process_list.append(af_buckets[af][i])
    
    return process_list

def rendezvous_hash(nic, pub_key_hex, servers):
    # We use a dict to group scores by Address Family.
    af_buckets = {} 
    for af in nic.supported():
        af_buckets[af] = []
        for host in servers[af]:
            # Record server to score.
            server = copy.deepcopy(servers[af][host])

            # Record score for server using the split-out scoring function.
            server["score"] = get_server_score(af, host, pub_key_hex)
            af_buckets[af].append(server)

        # Sort each individual AF bucket by score as we finish it.
        af_buckets[af].sort(key=lambda v: v["score"])

    # Final interleaving to ensure protocol diversity.
    return interleave_buckets(af_buckets)

async def ensure_clients_connected(clients, timeout=3, retry_duration=1200):
    async def worker(client):
        # Check if already connected
        if client.dispatcher_task is not None:
            return True
        
        # Rate limiting: Check if we are allowed to retry yet
        now = asyncio.get_event_loop().time()
        if client.last_connect is not None:
            if (now - client.last_connect) < retry_duration:
                return False

        # Attempt connection
        try:
            # Update timestamp HERE to mark the start of an actual attempt
            client.last_connect = now 
            await asyncio.wait_for(client.connect(), timeout)
            return True
        except Exception:
            log_exception()
            return False

    tasks = [worker(client) for client in clients]
    return await asyncio.gather(*tasks)

async def find_dest_in_servers(clients, dest_pub_hex, timeout=3):
    async def worker(client):
        # Skip not started.
        if client.dispatcher_task is None:
            return None
        
        # Queue any arbitrary unique message.
        _, ack = client.queue_msg("hello", dest_pub_hex)

        # Wait for an acknowledgement.
        try:
            await asyncio.wait_for(ack, timeout)
            return client
        except asyncio.TimeoutError:
            print("dest in servers timeout")
            return None
    
    tasks = [worker(client) for client in clients]
    results = await asyncio.gather(*tasks)
    return strip_none(results)

async def get_dest_clients(nic, dest_pub_hex, servers, clients_map, n=4, max_servers=20):
    candidate_clients = []
    sorted_servers = rendezvous_hash(nic, dest_pub_hex, servers)
    for server in sorted_servers:
        af = server["af"]
        host = server["host"]
        client = clients_map[af][host]
        candidate_clients.append(client)

    # Build a list of clients that converge with dest_pub_hex.
    found_clients = []
    tried_servers = 0
    while len(found_clients) < n and tried_servers < max_servers:
        # Build next list of candidates from head of candidate_clients.
        candidates = []

        # Calculate batch size with respect to n, remaining, and max_servers.
        batch_size = min(
            n - len(found_clients),
            len(candidate_clients),
            max_servers - tried_servers
        )
        if not batch_size:
            break

        # Try the next list of servers to find dest at.
        for _ in range(0, batch_size):
            candidates.append(candidate_clients.pop(0))

        # Update tried server count.
        tried_servers += batch_size
    
        # Ensure clients are connected.
        await ensure_clients_connected(candidates)

        # Check to see if dest is on any of the candidate servers.
        results = await find_dest_in_servers(candidates, dest_pub_hex)
        if results:
            found_clients += results

    return found_clients