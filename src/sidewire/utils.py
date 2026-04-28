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
    max_len = max((len(bucket) for bucket in af_buckets.values()), default=0)
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
            # Shallow copy is sufficient: we only mutate the top-level dict by
            # adding a "score" key, and nested values (host, port, fqns) are
            # never written back into the original infra table.
            server = dict(servers[af][host])

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
    connect_timeout: int = 15,
    retry_duration: int = 1200,
) -> Optional[Any]:
    """Return client if we can connect+subscribe to its broker, else None.

    No round-trip probe. The previous implementation sent a PROBE
    to dest_pub_hex and waited up to N seconds for the destination
    peer to ACK, using that round-trip as the membership filter.
    That filter was the actual root cause of broker-set non-
    convergence across the matrix: round-trip success depends on
    BOTH peers' transient network state plus the destination's
    async-loop scheduling latency, so two peers running the
    deterministic rendezvous-rank walk for the same target pubkey
    would each end up with different "successful" subsets of the
    same ranked candidate list, biased toward "brokers I happen to
    find fast on this run". Bumping N papered over the symptom but
    didn't fix the cause.

    Membership is now: "I can MQTT-CONNECT to this broker and my
    own SUBSCRIBE for self_pub_hex was acknowledged". That signal
    is local to this peer, doesn't depend on the destination peer's
    runtime state, and -- crucially -- gives every peer the SAME
    subset of the rendezvous-ranked candidates (modulo this peer's
    own connectivity). Two peers walking the same ranking for the
    same target pubkey now converge on the same broker subset
    automatically. Cross-peer publish goes through the rendezvous
    ranking the destination ALSO chose, so delivery succeeds.

    Rate limiter: only fires on actual connect FAILURE, not on every
    attempt. The previous code set last_connect = now BEFORE the
    connect attempt, so any momentary failure locked the broker
    out for retry_duration (1200s = 20min). Now last_connect is
    only set inside the except branch, so successful (or recovered)
    connects don't poison the per-broker cache.
    """
    if client.dispatcher_task is None:
        now = client.get_time()
        if client.last_connect is not None:
            if (now - client.last_connect) < retry_duration:
                return None
        try:
            await asyncio.wait_for(client.connect(), connect_timeout)
        except (OSError, ConnectionError, asyncio.TimeoutError):
            client.last_connect = now
            log_exception()
            return None

    return client


async def get_dest_clients(
    nic: Any,
    dest_pub_hex: str,
    servers: Dict,
    clients_map: Dict,
    n: int = 4,
    max_servers: int = 20,
) -> List[Any]:
    """Discover and return up to n MQTT clients that can reach the destination public key.

    n=4 is the natural minimum: ~2 brokers per AF after interleave
    gives every peer enough redundancy without keeping excessive
    idle MQTT sessions. With the round-trip probe removed from
    try_client (membership now = "I can connect+subscribe at
    this broker"), every peer walks the deterministic rendezvous
    ranking for the same target pubkey and converges on the same
    broker subset automatically -- modulo each peer's own
    connectivity, which is far more stable than the previous
    probe-round-trip filter. n=4 should now converge cleanly.
    """
    candidate_clients = []
    sorted_servers = rendezvous_hash(nic, dest_pub_hex, servers)
    # server["af"] is the IANA protocol number from the INFRA database (always 10
    # for IPv6, 2 for IPv4). clients_map is keyed by the platform's socket.AF_*
    # constants, which differ on Windows (AF_INET6 = 23) vs Linux (AF_INET6 = 10).
    # Normalise via a lookup table so the key matches on all platforms.
    iana_to_af = {int(IP4): IP4, 10: IP6}
    for server in sorted_servers:
        af = iana_to_af.get(int(server["af"]), int(server["af"]))
        host = server["host"]
        if af not in clients_map or host not in clients_map[af]:
            continue
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
            if hosts:
                host = hosts[0]
            else:
                host = server_list[0]["ip"]

            server_list[0]["host"] = host
            servers[af][host] = server_list[0]

    return servers
