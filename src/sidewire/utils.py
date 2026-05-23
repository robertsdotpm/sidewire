import asyncio
import time
from aionetiface import (
    INFRA,
    IP4,
    IP6,
    fstr,
    h_to_b,
    log,
    log_exception,
    os_net_timeouts,
    rand_b,
    rendezvous_score,
    to_b,
    to_h,
)
from .mqtt.mqtt_defs import MsgEnum


def get_server_score(af, host, pub_key_hex):
    """Compute the rendezvous score for a server given an address family, host, and public key."""
    return rendezvous_score(bytes([int(af)]), h_to_b(pub_key_hex), to_b(host))


def interleave_buckets(af_buckets):
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


def rendezvous_hash(nic, pub_key_hex, servers):
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
    dest_pub_hex,
    client,
    connect_timeout=15,
    retry_duration=1200,
):
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
        except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
            client.last_connect = now
            return None

    return client


async def get_dest_clients(
    nic,
    dest_pub_hex,
    servers,
    clients_map,
    n=4,
    max_servers=20,
    cached_hints=None,
):
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
    # Stage timeline for the router-load breakdown: setup (rendezvous
    # scoring, candidate-list build) vs the actual broker connects.
    dest_t0 = time.monotonic()

    def dest_stage(name):
        pass

    dest_stage("get_dest_enter")
    af_buckets = rendezvous_hash(nic, dest_pub_hex, servers)
    dest_stage("rendezvous_done")
    # server["af"] is the IANA protocol number from the INFRA database (always 10
    # for IPv6, 2 for IPv4). clients_map is keyed by the platform's socket.AF_*
    # constants, which differ on Windows (AF_INET6 = 23) vs Linux (AF_INET6 = 10).
    # Normalise via a lookup table so the key matches on all platforms.
    iana_to_af = {int(IP4): IP4, 10: IP6}

    found_clients = []

    # Per-AF broker walk.  `needed` is how many good clients an AF
    # wants -- scaled down for small buckets so a short list (e.g. the
    # 3-broker IPv6 bucket) doesn't chase an unreachable target and
    # stall on a dead server it can never skip past.  WALK_CAP hard-
    # bounds the wait: once it elapses we take whatever connected.
    # Both AF walks run concurrently (gather below), so the whole
    # broker phase stays ~WALK_CAP total, not WALK_CAP per AF.
    #
    # OS-scaled: 1s on modern hosts, much larger on XP/Vista -- XP's
    # MQTT handshake alone runs ~8-12s, so a 1s cap there would admit
    # zero brokers and strand signalling.
    WALK_CAP = os_net_timeouts()["broker_walk_cap"]
    # Zero-broker fallback budget.  Skip the retry entirely when the
    # primary cap is already >= 4s (Vista=6, XP=14) -- those OSes have
    # generous-enough first-pass budgets that a zero result means
    # nothing is reachable, not "we didn't wait long enough".  For
    # the LAN-fast default (2s) and Windows (4s) we lift the cap to
    # 4s for one retry before declaring total signaling outage.
    RETRY_WALK_CAP = 4.0
    retry_enabled = WALK_CAP < RETRY_WALK_CAP

    # Index cached hints by (af, host) for O(1) lookup per AF below.
    # Hints arrive sorted most-recently-successful first (load_for in
    # broker_hint_cache sorts by ts desc).  We preserve that order
    # when prepending so the parallel try_client gather launches the
    # freshest brokers first -- they're statistically the most
    # likely to still be alive.
    cached_by_af = {}
    if cached_hints:
        for hint in cached_hints:
            af = iana_to_af.get(int(hint["af"]), int(hint["af"]))
            cached_by_af.setdefault(af, []).append((hint["host"], hint))

    async def walk_af(nic_af, cap):
        """Connect `needed` brokers for one address family, capped at `cap` seconds."""
        sorted_servers = af_buckets[nic_af]
        candidate_clients = []
        seen_clients = set()

        # Cached brokers first.  These are hosts that successfully
        # became protected_clients on a previous startup -- give
        # them a head start in the first-to-complete race.  If
        # they're dead, the rendezvous tail behind them runs in
        # the same gather and the walk completes within `cap`
        # regardless.  Dedup by client identity so a cached
        # broker that's also in the rendezvous top-N doesn't get
        # two try_client tasks racing each other.
        for host, _hint in cached_by_af.get(nic_af, []):
            if nic_af not in clients_map or host not in clients_map[nic_af]:
                # Cached broker no longer in our seed list (servers.json
                # shrank).  Skip -- it can re-enter the cache only by
                # being a current candidate in a future run.
                continue
            client = clients_map[nic_af][host]
            if id(client) in seen_clients:
                continue
            seen_clients.add(id(client))
            candidate_clients.append(client)

        for server in sorted_servers:
            af = iana_to_af.get(int(server["af"]), int(server["af"]))
            host = server["host"]
            if af not in clients_map or host not in clients_map[af]:
                continue
            client = clients_map[af][host]
            if id(client) in seen_clients:
                continue
            seen_clients.add(id(client))
            candidate_clients.append(client)

        # needed = how many good brokers this AF wants, stepping up at
        # the 2 / 4 / 8 candidate-count thresholds: <2 -> 0 (skip the
        # AF, no redundancy worth stalling for), 2-3 -> 1, 4-7 -> 2,
        # 8+ -> 3.  Those thresholds are powers of two, so the step
        # count is just floor(log2(serv_no)) clamped to [0, 3] --
        # bit_length()-1 gives floor(log2) integer-exact (no float
        # rounding on exact powers of two).
        serv_no = len(candidate_clients)
        needed = min(3, max(0, serv_no.bit_length() - 1))

        if needed == 0:
            return []

        # Fire up to 10 connects; collect first-to-complete until
        # `needed` good clients are in.
        tasks = [
            asyncio.ensure_future(try_client(dest_pub_hex, c))
            for c in candidate_clients[:10]
        ]
        af_found = []

        async def collect():
            for fut in asyncio.as_completed(tasks):
                r = await fut
                if r:
                    af_found.append(r)
                    if len(af_found) >= needed:
                        return

        try:
            await asyncio.wait_for(collect(), timeout=cap)
        except asyncio.TimeoutError:
            # cap hit -- take whatever connected so far.
            pass
        finally:
            # Cancel the still-pending connects (laggards / dead
            # brokers).  Not awaited: awaiting their cancellation
            # cleanup could push the walk past the cap.  They
            # unwind and close their own half-open pipes in the
            # background.
            for t in tasks:
                if not t.done():
                    t.cancel()

        return af_found

    # Run the per-AF walks concurrently -- they are independent, so
    # router startup is max(af_v4, af_v6) instead of their sum. gather
    # preserves input order, so found_clients keeps af-bucket order.
    af_keys = list(af_buckets)
    dest_stage("walks_start")
    per_af_results = await asyncio.gather(
        *[walk_af(nic_af, WALK_CAP) for nic_af in af_keys]
    )
    dest_stage("walks_done")
    for af_found in per_af_results:
        found_clients.extend(af_found)

    # Zero-broker escape hatch: the first walk gave us nothing across
    # every AF.  Rather than let the node start with
    # protected_clients=0 (no MQTT subscriptions = no inbound signal
    # path = total signaling outage), retry once at the doubled cap.
    # The clients_map state is preserved across the retry; try_client
    # short-circuits when an MQTTClient is already connected, so any
    # in-flight CONNECT from the first pass either completed in the
    # interim (returns immediately) or gets a fresh connect attempt
    # under the larger budget.
    if not found_clients:
        if retry_enabled:
            log(fstr(
                "[GET-DEST-CLIENTS] dest={0} first walk admitted 0 brokers "
                "(cap={1}s); retrying once at {2}s before giving up",
                (dest_pub_hex[:12], WALK_CAP, RETRY_WALK_CAP),
            ))
            per_af_results = await asyncio.gather(
                *[walk_af(nic_af, RETRY_WALK_CAP) for nic_af in af_keys]
            )
            for af_found in per_af_results:
                found_clients.extend(af_found)
            if not found_clients:
                log(fstr(
                    "[GET-DEST-CLIENTS] dest={0} retry at {1}s STILL admitted "
                    "0 brokers -- node will start with no subscriptions and "
                    "cannot receive inbound signal until a future walk succeeds.",
                    (dest_pub_hex[:12], RETRY_WALK_CAP),
                ))
            else:
                log(fstr(
                    "[GET-DEST-CLIENTS] dest={0} retry recovered {1} broker(s)",
                    (dest_pub_hex[:12], len(found_clients)),
                ))
        else:
            log(fstr(
                "[GET-DEST-CLIENTS] dest={0} walk at {1}s admitted 0 brokers "
                "and primary cap already >= 4s; skipping retry. Node will "
                "start with no subscriptions and cannot receive inbound "
                "signal until a future walk succeeds.",
                (dest_pub_hex[:12], WALK_CAP),
            ))

    return found_clients


def get_mqtt_server_list(from_infra=INFRA["MQTT"]):
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
