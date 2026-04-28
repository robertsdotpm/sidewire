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
    connect_timeout: int = 30,
    probe_timeout: int = 30,
    retry_duration: int = 1200,
) -> Optional[Any]:
    """Probe a single MQTT client to verify the destination is reachable; return the client or None.

    The 15s budget for both connect and probe accommodates the full
    peer round-trip through the broker (MQTT CONNECT + SUBSCRIBE on
    the publisher, broker forwards to the destination peer,
    destination's async stack handles the PROBE message and
    publishes MSGACK, broker forwards back). On XP/Vista that
    round-trip can take 7-10s on a healthy path. Tighter timeouts
    silently exclude brokers based on flaky network state rather
    than broker identity, producing the broker-set non-convergence
    bug that breaks reverse_connect across mixed old<->modern
    pairs: each peer's probe-success subset of a 20-broker pool
    ends up narrow and biased toward "what this VM finds fast",
    and narrow biased subsets routinely don't overlap.

    15s gives healthy slow-VM round-trips room to finish without
    pathological waits on genuinely-broken paths. Reconnect cost
    on a healthy VM is still sub-second.
    """
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

    # Probe to check if dest is on this server. send_probe direct-
    # publishes the PROBE bypassing the dispatcher's backoff/jitter
    # loop -- the ordinary publish path adds 0-2s pre-publish jitter
    # and runs at 0.5s cadence which under concurrent probe load
    # silently exceeds probe_timeout and shrinks the discovered
    # broker set asymmetrically (the mechanism behind the broker-
    # set non-convergence bug).
    probe_queue_id = to_h(rand_b(32))
    _, ack = await client.send_probe(dest_pub_hex, probe_queue_id)
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
    """Discover and return up to n MQTT clients that can reach the destination public key.

    n=4 is the minimum set size we test for convergence under the
    other fixes (NTP-synced clocks, direct-publish probes,
    direct-publish MSGACKs, post-startup sync window). Earlier
    runs showed n=4 broke convergence cross-cohort because the
    probe-filter had three compounding sources of non-determinism
    (timeout-based probe drop, dispatcher-jitter-delayed publish,
    clock-skew-rejected message) that biased each peer's "first 4
    successes" toward different broker subsets. With those root
    causes addressed the rendezvous-deterministic ranking should
    converge naturally and even n=4 should produce overlap. If
    not, n is the floor we have to bump.
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
