"""
Given a remote hex pub key: determines a list of MQTT clients that the dest is on.
It uses a probabilistic algorithm based on rendezvous hashing. This approach has
several benefits:

- Neither side needs to know the others server list
- Server lists may change, offsets may change, fixed servers may go down
- Using the public key to yield a server list is adaptive to ephemeral server
faults since deterministic ordering eventually may intersect a new server
- All of this takes place without coordination
- Easy to adapt to multiple address families

We use interleaving between server address families. This means the best IPv4
server is 0, then the best IPv6, then the second best IPv4, and so on. So you
can stream this sorted list to a connect process to easily converge on connected
clients regardless of what address families a NIC supports. Convergence is still
possible if at least one address families is shared.

The sorting algorithm uses SHA256 to produce uniformly distributed values,
but using -log(U)  transforms them into an exponential distribution. This allows
fair and mathematically correct weighted selection, where servers with higher
weights are more likely to appear earlier in the ordering. The benefit is not just
spacing, but enabling proper probabilistic behavior for ranking and convergence.

Note: can just use sha256 sorted n but if weights are to be used in the future then
the math needs to be log(u, e).

If a server is down, maybe have a flag that disables it from reconnect retry in the
code otherwise it blocks the whole program for servers it already knows are down.
"""

import asyncio
import time
from aionetiface import AFGroup, IP4, IP6, Interface, async_wrap_errors, fstr, log, log_exception
from .mqtt import MQTTClient
from .smart_pipe import SmartPipe
from .utils import get_dest_clients, get_mqtt_server_list


# An MQTT pipe is closed if no queue_msg call landed on it within this
# many seconds. 5 minutes lines up well with how p2p signal sessions
# actually run -- bursts of activity lasting seconds, then idle for
# minutes-to-hours -- so a client that's quiet for 5 minutes is almost
# always done.  Reconnect cost is one MQTT CONNECT handshake (~sub-
# second), so closing aggressively is cheap; the broker rate-limit
# slot freed up matters more.  Inbound subs are NEVER closed (they
# live in protected_clients) so 5 minutes is safe for receive too.
IDLE_CLIENT_TIMEOUT = 600

# How often the idle-closer wakes up to scan client.last_send. The
# closer is cheap (just a timestamp comparison per client), so 60s is
# fine -- worst case a client stays open up to IDLE_CLIENT_TIMEOUT +
# IDLE_CLIENT_INTERVAL after its last send.
IDLE_CLIENT_INTERVAL = 60


class Router:
    """Coordinate multiple MQTTClient instances to route messages using rendezvous hashing."""

    def __init__(
        self,
        kp,
        msg_handler=None,
        get_time=None,
        nic=None,
        servers=None,
    ):
        """Initialize a Router with a key pair and optional server list.

        nic accepts an Interface, an AFGroup, or None. A single Interface
        is fanned across the AFs it supports; an AFGroup lets callers
        send v4 brokers via one NIC and v6 brokers via another (mobile
        CGNAT for v4, primary for v6, etc). None falls back to the
        default Interface, single-stack on whatever the OS picks.

        get_time MUST be supplied (no implicit time.time fallback)
        so the same clock source threads through to every
        MQTTClient at construction time. See MQTTClient.__init__
        rationale -- silently defaulting to wall clock hid a real
        cross-machine clock-skew bug.
        """
        if get_time is None:
            raise ValueError(
                "Router: get_time is required (pass node.sys_clock.time, "
                "not time.time directly)"
            )
        self.kp = kp
        self.servers = servers or get_mqtt_server_list()
        self.get_time = get_time
        if nic is None:
            nic = Interface("default")
        self.af_group = AFGroup(nic) if not isinstance(nic, AFGroup) else nic
        # Backwards-compat alias: callers / get_dest_clients still
        # reach for self.nic. Resolves to the v4-bound Interface when
        # the group has it, else the first Interface in the group.
        self.nic = self.af_group.get(IP4) or self.af_group.interfaces()[0]

        self.clients = {IP4: {}, IP6: {}}
        self.recv_msg_ids = {}
        for af in (IP4, IP6):
            if not self.af_group.supports(af):
                continue
            iface = self.af_group.for_af(af)
            for host in self.servers[af]:
                self.clients[af][host] = MQTTClient(
                    af,
                    iface,
                    (host, self.servers[af][host]["port"]),
                    self.kp,
                    get_time=get_time,
                )

                if msg_handler:
                    self.clients[af][host].add_msg_handler(msg_handler)

                # All clients share recv_msg_ids so duplicate detection works across them.
                self.clients[af][host].recv_msg_ids = self.recv_msg_ids

                self.clients[af][host].last_connect = None

        self.cache = {}

        # Clients in this set are exempt from the idle-closer below:
        # they're the rendezvous-hash group for our OWN public key
        # (set by start()), which is how we receive inbound messages.
        # Closing those breaks the inbound path even when nothing's
        # been sent, so they stay open for the lifetime of the Router.
        self.protected_clients = set()

        # Background task for the idle-closer. Started by start();
        # cancelled by close(). Set to None when not running so close()
        # can be called repeatedly without raising.
        self.idle_closer_task = None

    def add_msg_handler(self, msg_handler):
        """Register a message handler on all managed MQTT clients."""
        for af in self.clients:
            for host in self.clients[af]:
                self.clients[af][host].add_msg_handler(msg_handler)

    async def start(self):
        """Connect to the best MQTT servers for this node's own public key.

        The clients returned here form the *first loaded group* -- they
        carry our inbound subscription, so the idle-closer below leaves
        them alone for the Router's lifetime regardless of send activity.
        """
        clients = await get_dest_clients(
            self.af_group, self.kp.public_key_hex, self.servers, self.clients
        )

        log(fstr(
            "[ROUTER-START] pub_key={0} protected_clients={1}",
            (self.kp.public_key_hex[:12], len(clients)),
        ))

        # Pin these as the protected (inbound) set. Future start() calls
        # would extend, not replace, but in practice start() is one-shot.
        for client in clients:
            self.protected_clients.add(client)

        self.cache_clients(self.kp.public_key_hex, clients)

        # Idempotent: only spawn the idle closer once, and only after
        # we know which clients are protected. Otherwise an early tick
        # could see an empty protected set and close everything.
        if self.idle_closer_task is None or self.idle_closer_task.done():
            self.idle_closer_task = asyncio.ensure_future(
                async_wrap_errors(self.idle_closer_loop())
            )

        return clients

    async def idle_closer_loop(self):
        """Periodically close MQTT clients that haven't been used to send.

        Walks self.clients every IDLE_CLIENT_INTERVAL and closes any
        client whose last_send is older than IDLE_CLIENT_TIMEOUT,
        SKIPPING anything in self.protected_clients. Closed clients
        are also evicted from any cache entries that reference them
        so subsequent pipe() calls discover and reconnect fresh.
        """
        while True:
            try:
                await asyncio.sleep(IDLE_CLIENT_INTERVAL)
            except asyncio.CancelledError:
                return

            try:
                await self.close_idle_clients()
            except asyncio.CancelledError:
                return
            except (OSError, ConnectionError):
                log_exception()

    async def close_idle_clients(self):
        """One pass of the idle-closer; exposed for tests + manual triggers."""
        now = self.get_time()
        for af in self.clients:
            for host in self.clients[af]:
                client = self.clients[af][host]
                # Skip the inbound (start()-loaded) group and any
                # client that hasn't yet been connected (no
                # dispatcher_task means there's nothing to close).
                if client in self.protected_clients:
                    continue
                if client.dispatcher_task is None:
                    continue
                # last_send None means the client is open but never
                # used for a send -- the connect time is the activity
                # baseline so freshly-opened clients still get a full
                # idle window before being closed.
                last_active = client.last_send
                if last_active is None:
                    last_active = getattr(client, "last_connect", None) or now
                    client.last_send = last_active
                if (now - last_active) < IDLE_CLIENT_TIMEOUT:
                    continue

                print("[IDLE-CLOSER] closing idle client host={0} af={1} last_active={2:.0f}s ago".format(
                    client.host, client.af, now - last_active
                ))

                try:
                    await client.close()
                except (OSError, ConnectionError):
                    log_exception()

                # close() sets is_closed permanently, which would prevent
                # reconnection: a new dispatcher created by a future connect()
                # call exits immediately at its `while not is_closed.is_set()`
                # guard. Clear it so the client is available for reuse.
                client.is_closed.clear()

                # Reset last_send so the next idle-closer pass uses the
                # reconnect time as the activity baseline.  Without this,
                # last_send still holds the old (>300 s) timestamp and the
                # idle closer would immediately re-close a freshly-reconnected
                # client that hasn't yet sent a message.
                client.last_send = None

                # Evict cache entries that reference this client so
                # the next pipe() call rediscovers and reconnects
                # rather than handing back a closed-but-cached entry.
                self.evict_client_from_cache(client)

    def evict_client_from_cache(self, client):
        """Drop any cache entry whose client list contains *client*."""
        stale_keys = []
        for pub_key_hex, entry in self.cache.items():
            if client in entry.get("clients", ()):
                stale_keys.append(pub_key_hex)
        for k in stale_keys:
            del self.cache[k]

    async def pipe(
        self,
        dest_pub_hex,
        use_cache=False,
        expiry=3600,
        hint_brokers=None,
    ):
        """Create a SmartPipe to the destination, performing rendezvous discovery if needed.

        hint_brokers is an optional list of {af, host, port} dicts
        the destination peer advertised in its addr_bytes -- the
        brokers it is reliably connected at right now. SmartPipe
        prefers these over the rendezvous-derived candidate set
        because the destination has GUARANTEED its subscription is
        live there, sidestepping the broker-set non-convergence
        bug. Falls back to rendezvous discovery if no hints work
        (or are passed).
        """
        log(fstr(
            "[ROUTER-PIPE] dest={0} hint_count={1} use_cache={2}",
            (dest_pub_hex[:12], len(hint_brokers or []), use_cache),
        ))

        now = self.get_time()
        cached_clients = None  # type: Optional[List[Any]]

        if use_cache and dest_pub_hex in self.cache:
            entry = self.cache[dest_pub_hex]
            age = now - entry["updated"]
            if age < expiry:
                cached_clients = entry["clients"]
                log(fstr(
                    "[ROUTER-PIPE] cache HIT dest={0} age_s={1} clients={2}",
                    (dest_pub_hex[:12], int(age), len(cached_clients or [])),
                ))
            else:
                log(fstr(
                    "[ROUTER-PIPE] cache STALE dest={0} age_s={1} expiry_s={2}",
                    (dest_pub_hex[:12], int(age), expiry),
                ))
        elif use_cache:
            log(fstr(
                "[ROUTER-PIPE] cache MISS dest={0} (no entry)",
                (dest_pub_hex[:12],),
            ))

        smart_pipe = SmartPipe(
            self, dest_pub_hex, clients=cached_clients, hint_brokers=hint_brokers,
        )
        await smart_pipe.connect()

        log(fstr(
            "[ROUTER-PIPE] dest={0} clients={1}",
            (dest_pub_hex[:12], len(smart_pipe.clients)),
        ))

        if use_cache and cached_clients is None:
            self.cache_clients(dest_pub_hex, smart_pipe.clients)

        return smart_pipe

    def cache_clients(self, pub_key_hex, clients):
        """Store a client list in the discovery cache for the given public key."""
        now = self.get_time()
        self.cache[pub_key_hex] = {"updated": now, "clients": clients}

    async def close(self):
        """Close all managed MQTT client connections."""
        # Stop the idle closer first so it can't race a client.close()
        # below and double-fire on the same socket.
        if self.idle_closer_task is not None and not self.idle_closer_task.done():
            self.idle_closer_task.cancel()
            try:
                await self.idle_closer_task
            except (asyncio.CancelledError, Exception):
                pass
        self.idle_closer_task = None

        for af in self.clients:
            for host in self.clients[af]:
                client = self.clients[af][host]
                # If the idle-closer was cancelled mid-close, is_closed may
                # be set while the dispatcher/pipe are still live.  Clear it
                # so client.close() runs fully instead of returning early.
                client.is_closed.clear()
                await client.close()

    async def __aenter__(self):
        """Start the router on context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, *_):
        """Close all connections on context manager exit."""
        await self.close()
        return False

