import copy
import asyncio
from typing import Any, Dict, List, Optional
from aionetiface import (
    INFRA,
    IP4,
    IP6,
    h_to_b,
    log_exception,
    rand_b,
    rendezvous_score,
    to_b,
    to_h,
)
from .mqtt.mqtt_defs import MsgEnum


def get_server_score(af: Any, host: str, pub_key_hex: str) -> Any:
    """Compute the rendezvous score for a server given an address family, host, and public key."""
    return rendezvous_score(bytes([int(af)]), h_to_b(pub_key_hex), to_b(host))


def interleave_buckets(af_buckets: Dict) -> List[Dict]:
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


def rendezvous_hash(nic: Any, pub_key_hex: str, servers: Dict) -> List[Dict]:
    """Return an interleaved, rendezvous-scored list of servers ranked for the given public key."""
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


async def try_client(
    dest_pub_hex: str,
    client: Any,
    connect_timeout: int = 3,
    probe_timeout: int = 3,
    retry_duration: int = 1200,
) -> Optional[Any]:
    """Probe a single MQTT client to verify the destination is reachable; return the client or None."""
    # Connect if not already connected, with rate limiting.
    if client.dispatcher_task is None:
        now = client.get_time()
        if client.last_connect is not None:
            if (now - client.last_connect) < retry_duration:
                return None
        try:
            client.last_connect = now
            await asyncio.wait_for(client.connect(), connect_timeout)
        except (OSError, ConnectionError, asyncio.TimeoutError):
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


async def get_dest_clients(
    nic: Any,
    dest_pub_hex: str,
    servers: Dict,
    clients_map: Dict,
    n: int = 4,
    max_servers: int = 20,
) -> List[Any]:
    """Discover and return up to n MQTT clients that can reach the destination public key."""
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
        batch = candidate_clients[i : i + batch_size]
        results = await asyncio.gather(
            *[try_client(dest_pub_hex, c) for c in batch], return_exceptions=True
        )

        for client, result in zip(batch, results):
            if result is client:
                found_clients.append(client)
                if len(found_clients) >= n:
                    return found_clients

    return found_clients


def get_mqtt_server_list(from_infra: Any = INFRA["MQTT"]) -> Dict:
    """Parse the INFRA MQTT server list into a dict keyed by address family and hostname."""
    af_map = {"IPv4": IP4, "IPv6": IP6}
    servers = {IP4: {}, IP6: {}}

    # Norm server list.
    for af_txt, af in af_map.items():
        for server_list in from_infra[af_txt]["UDP"]:
            hosts = sorted(server_list[0]["fqns"])
            if len(hosts):
                host = hosts[0]
            else:
                host = server_list[0]["ip"]

            server_list[0]["host"] = host
            servers[af][host] = server_list[0]

    return servers
