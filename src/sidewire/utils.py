import asyncio
from typing import Any, Dict, List, Optional
from aionetiface import (
    INFRA,
    IP4,
    IP6,
    fstr,
    h_to_b,
    log,
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


def rendezvous_hash(nic: Any, pub_key_hex: str, servers: Dict) -> Dict:
    """Return per-AF rendezvous-scored sorted lists for the given public key.

    Returns a dict {af: [servers sorted by score, best first]} with
    one entry per AF the local NIC supports. Per-AF separation
    matters for convergence: each peer's get_dest_clients walks the
    per-AF sorted list independently and takes top-N per AF, so
    two peers targeting the same pubkey converge on the SAME N
    servers per AF deterministically (modulo each peer's own
    connectivity to that AF). If one peer's IPv6 stack happens
    to be flaky during selection, only their IPv6 set shrinks --
    the IPv4 set remains intact and convergent. The previous
    interleaved-flat-list approach let an AF failure cascade into
    breaking the OTHER AF's convergence guarantee, which was the
    root cause of broker-set non-convergence we kept chasing.
    """
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

    return af_buckets


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
    log(fstr("[TRY-CLIENT] host={0} af={1} already_connected={2}",
        (client.host, client.af,
         client.dispatcher_task is not None and not client.dispatcher_task.done())))
    if client.dispatcher_task is None:
        now = client.get_time()
        if client.last_connect is not None:
            if (now - client.last_connect) < retry_duration:
                log(fstr(
                    "[TRY-CLIENT] host={0} rate-limited ({1:.0f}s remaining)",
                    (client.host, retry_duration - (now - client.last_connect)),
                ))
                return None
        log(fstr("[TRY-CLIENT] host={0} connecting (timeout={1}s)",
            (client.host, connect_timeout)))
        try:
            await asyncio.wait_for(client.connect(), connect_timeout)
        except (OSError, ConnectionError, asyncio.TimeoutError):
            client.last_connect = now
            log_exception()
            return None

    # Guard against zombie clients: dispatcher_task is non-None but the
    # task already completed (happens when is_closed was set before the
    # dispatcher started -- fixed in close_idle_clients, but log if seen).
    if client.dispatcher_task is not None and client.dispatcher_task.done():
        log(fstr("[TRY-CLIENT] host={0} zombie dispatcher_task detected (done but non-None)", (client.host,)))

    return client


async def get_dest_clients(
    nic: Any,
    dest_pub_hex: str,
    servers: Dict,
    clients_map: Dict,
    n: int = 4,
    max_servers: int = 20,
) -> List[Any]:
    """Discover up to n MQTT clients per AF that can reach the destination public key.

    Per-AF walk: each address family the local NIC supports gets
    its own top-N from the deterministic rendezvous-scored sorted
    list. Two peers targeting the same dest_pub_hex pick the SAME
    top-N per AF (modulo each peer's own connectivity), so the
    intersection between A's publish-target set and B's protected
    set is guaranteed per-AF.

    The interleaved-flat-list approach was structurally fragile:
    when one peer's IPv6 connectivity was momentarily flaky,
    af=23 candidates failed selection, the flat list rebalanced
    toward IPv4 entries past rank N -- and that rebalancing
    differed across peers, breaking convergence on the WORKING
    AF too. Per-AF independent sampling fixes that: an IPv6
    failure shrinks only the IPv6 set; the IPv4 set stays at
    deterministic top-N.

    Returns a flat list of clients (combined across AFs) so
    callers (Router.protected_clients, smart_pipe) don't need to
    change. Total returned size is up to n * len(supported AFs).
    """
    af_buckets = rendezvous_hash(nic, dest_pub_hex, servers)
    # server["af"] is the IANA protocol number from the INFRA database (always 10
    # for IPv6, 2 for IPv4). clients_map is keyed by the platform's socket.AF_*
    # constants, which differ on Windows (AF_INET6 = 23) vs Linux (AF_INET6 = 10).
    # Normalise via a lookup table so the key matches on all platforms.
    iana_to_af = {int(IP4): IP4, 10: IP6}

    found_clients = []

    # Walk each AF bucket independently. n is the per-AF top count.
    # batch_size keeps the connect attempts within an AF concurrent
    # so a slow broker doesn't sequentialise the whole walk.
    batch_size = n * 2
    for nic_af in af_buckets:
        sorted_servers = af_buckets[nic_af]
        candidate_clients = []
        for server in sorted_servers:
            af = iana_to_af.get(int(server["af"]), int(server["af"]))
            host = server["host"]
            if af not in clients_map or host not in clients_map[af]:
                continue
            client = clients_map[af][host]
            candidate_clients.append(client)

        af_found = []
        limit = min(len(candidate_clients), max_servers)
        for i in range(0, limit, batch_size):
            batch = candidate_clients[i : i + batch_size]
            results = await asyncio.gather(
                *[try_client(dest_pub_hex, c) for c in batch], return_exceptions=True
            )
            for client, result in zip(batch, results):
                if result is client:
                    af_found.append(client)
                    if len(af_found) >= n:
                        break
            if len(af_found) >= n:
                break
        log(fstr(
            "[GET-DEST-CLIENTS] dest={0} af={1} candidates={2} found={3}",
            (dest_pub_hex[:12], nic_af, len(candidate_clients), len(af_found)),
        ))
        found_clients.extend(af_found)

    log(fstr(
        "[GET-DEST-CLIENTS] dest={0} total={1}",
        (dest_pub_hex[:12], len(found_clients)),
    ))
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
