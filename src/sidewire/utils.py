from aionetiface import *
import copy
import asyncio
from .mqtt.mqtt_defs import MsgEnum

def get_server_score(af, host, pub_key_hex):
    return rendezvous_score(bytes([int(af)]), h_to_b(pub_key_hex), to_b(host))

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

async def try_client(dest_pub_hex, client, connect_timeout=3, probe_timeout=3, retry_duration=1200):
    # Connect if not already connected, with rate limiting.
    if client.dispatcher_task is None:
        now = client.get_time()
        if client.last_connect is not None:
            if (now - client.last_connect) < retry_duration:
                return None
        try:
            client.last_connect = now
            await asyncio.wait_for(client.connect(), connect_timeout)
        except Exception:
            log_exception()
            return None

    # Probe to check if dest is on this server.
    probe_queue_id = to_h(rand_b(32))
    _, ack = client.queue_msg("", dest_pub_hex, probe_queue_id, MsgEnum.PROBE)
    try:
        await asyncio.wait_for(ack, probe_timeout)
        return client
    except asyncio.TimeoutError:
        return None
    finally:
        client.dequeue_msg(probe_queue_id, msg_type=MsgEnum.PROBE)

async def get_dest_clients(nic, dest_pub_hex, servers, clients_map, n=4, max_servers=20):
    candidate_clients = []
    sorted_servers = rendezvous_hash(nic, dest_pub_hex, servers)
    for server in sorted_servers:
        af = server["af"]
        host = server["host"]
        client = clients_map[af][host]
        candidate_clients.append(client)

    # Process in batches of n * 2 to tolerate some servers being down without
    # hammering the full list. Within each batch all clients run concurrently,
    # and gather preserves order so we pick by rendezvous rank, not speed.
    batch_size = n * 2
    found_clients = []
    limit = min(len(candidate_clients), max_servers)

    for i in range(0, limit, batch_size):
        batch = candidate_clients[i:i + batch_size]
        results = await asyncio.gather(
            *[try_client(dest_pub_hex, c) for c in batch],
            return_exceptions=True
        )

        for client, result in zip(batch, results):
            if result is client:
                found_clients.append(client)
                if len(found_clients) >= n:
                    return found_clients

    return found_clients

def get_mqtt_server_list(from_infra=INFRA["MQTT"]):
    servers = {
        IP4: {},
        IP6: {},
    }

    servers["IPv4"] = servers[IP4]
    servers["IPv6"] = servers[IP6]

    # Norm server list.
    for af_txt in ("IPv4", "IPv6"):
        for server_list in from_infra[af_txt]["UDP"]:
            hosts = sorted(server_list[0]["fqns"])
            if len(hosts):
                host = hosts[0]
            else:
                host = server_list[0]["ip"]

            server_list[0]["host"] = host
            servers[af_txt][host] = server_list[0]

    return servers