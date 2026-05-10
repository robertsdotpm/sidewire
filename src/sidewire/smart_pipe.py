import asyncio
from aionetiface import fstr, log, log_exception, rand_b, to_h
from .utils import get_dest_clients, try_client


class SmartPipe:
    """Route messages to a destination over the best available MQTT clients."""

    def __init__(
        self,
        router,
        dest_pub_hex,
        clients=None,
        hint_brokers=None,
    ):
        """Initialize a SmartPipe with a router, destination public key, optional pre-resolved clients, and optional hint brokers."""
        self.router = router
        self.dest_pub_hex = dest_pub_hex
        self.clients = clients or []  # type: List[Any]
        # Hint brokers are {af, host, port} dicts the destination
        # advertised in its addr_bytes. We try connecting to these
        # first since the destination has guaranteed its
        # subscription is live there.
        self.hint_brokers = hint_brokers or []

    async def resolve_hint_clients(self):
        """Connect to each hint broker (if reachable) and return matching MQTTClient instances.

        Walks self.hint_brokers (the destination's advertised
        broker list) and tries to connect to each in parallel. Any
        broker we can connect+subscribe to gets returned. The
        destination peer is GUARANTEED subscribed at these
        brokers because they came from the dest's own
        protected_clients list at addr-publish time.

        Connects are run in parallel (asyncio.gather) so the total
        latency is max(T1..Tn) not T1+T2+..+Tn.  The per-broker
        connect_timeout is set below the outer asyncio.wait_for
        budget in resolve_pnp_addr (10 s) so the gather always
        completes before the outer timer can cancel it.
        """
        to_try = []
        for hint in self.hint_brokers:
            af = hint.get("af")
            host = hint.get("host")
            port = hint.get("port")
            if af is None or not host or not port:
                log(fstr("[SMARTPIPE-HINT] dest={0} malformed hint af={1} host={2}",
                    (self.dest_pub_hex[:12], af, host)))
                continue
            af_clients = self.router.clients.get(af, {})
            client = af_clients.get(host)
            if client is None:
                log(fstr(
                    "[SMARTPIPE-HINT] dest={0} hint host={1} af={2} not in clients_map"
                    " (known_afs={3})",
                    (self.dest_pub_hex[:12], host, af,
                     list(self.router.clients.keys())),
                ))
                continue
            log(fstr("[SMARTPIPE-HINT] dest={0} queuing hint host={1} af={2}",
                (self.dest_pub_hex[:12], host, af)))
            to_try.append(client)

        if not to_try:
            log(fstr("[SMARTPIPE-HINT] dest={0} no hint clients found in clients_map",
                (self.dest_pub_hex[:12],)))
            return []

        results = await asyncio.gather(
            *[try_client(self.dest_pub_hex, c, connect_timeout=8) for c in to_try],
            return_exceptions=True,
        )
        out = []
        for client, result in zip(to_try, results):
            if result is client:
                out.append(client)
        log(fstr("[SMARTPIPE-HINT] dest={0} tried={1} connected={2}",
            (self.dest_pub_hex[:12], len(to_try), len(out))))
        return out

    async def connect(self, msg_cb=None):
        """Resolve destination clients: hint brokers first, falling back to rendezvous."""
        print("[SMARTPIPE-CONNECT] dest={0} enter clients={1} hints={2}".format(
            self.dest_pub_hex[:12], len(self.clients), len(self.hint_brokers)))
        if not self.clients:
            # Try hint brokers first. If we get at least one, use
            # the hint set -- the dest is guaranteed subscribed.
            if self.hint_brokers:
                self.clients = await self.resolve_hint_clients()
                log(fstr(
                    "[SMARTPIPE-CONNECT] dest={0} hint_count={1} hint_clients={2}",
                    (self.dest_pub_hex[:12], len(self.hint_brokers), len(self.clients)),
                ))

            # Fall back to rendezvous discovery if hints didn't
            # produce any reachable clients.
            if not self.clients:
                self.clients = await get_dest_clients(
                    self.router.af_group,
                    self.dest_pub_hex,
                    self.router.servers,
                    self.router.clients,
                )
                log(fstr(
                    "[SMARTPIPE-CONNECT] dest={0} rendezvous_clients={1}",
                    (self.dest_pub_hex[:12], len(self.clients)),
                ))
        else:
            log(fstr(
                "[SMARTPIPE-CONNECT] dest={0} cached_clients={1}",
                (self.dest_pub_hex[:12], len(self.clients)),
            ))

        log(fstr(
            "[SMARTPIPE-CONNECT] dest={0} total_clients={1}",
            (self.dest_pub_hex[:12], len(self.clients)),
        ))

        if msg_cb:
            for client in self.clients:
                client.add_msg_handler(msg_cb)

        return self

    async def send(self, msg, timeout=4):
        """Send a message to the destination, returning the byte length on success or 0 on failure."""
        log(fstr(
            "[SMARTPIPE-SEND] dest={0} clients={1} msg_len={2} timeout={3}s",
            (self.dest_pub_hex[:12], len(self.clients), len(msg), timeout),
        ))
        msg_id_hex = to_h(rand_b(32))

        tasks = []
        client_by_task = {}  # task -> client (for broker-attribution logs)
        for client in self.clients:
            try:
                _, ack_msg = client.queue_msg(msg, self.dest_pub_hex, msg_id_hex)
            except Exception:
                log_exception()
                continue
            task = asyncio.create_task(
                asyncio.wait_for(asyncio.shield(ack_msg), timeout)
            )
            tasks.append(task)
            client_by_task[task] = client

        try:
            while tasks:
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )

                for task in done:
                    winner = client_by_task.get(task)
                    winner_host = getattr(winner, "host", "?") if winner else "?"
                    try:
                        await task

                        log(fstr(
                            "[SMARTPIPE-SEND] dest={0} ACK from broker={1} "
                            "(cancelling {2} other(s))",
                            (
                                self.dest_pub_hex[:12], winner_host,
                                len(pending),
                            ),
                        ))

                        for p in pending:
                            p.cancel()

                        await asyncio.gather(*pending, return_exceptions=True)

                        return len(msg)

                    except asyncio.TimeoutError:
                        log(fstr(
                            "[SMARTPIPE-SEND] dest={0} broker={1} TIMEOUT (no ACK)",
                            (self.dest_pub_hex[:12], winner_host),
                        ))
                    except (OSError, ConnectionError) as exc:
                        log(fstr(
                            "[SMARTPIPE-SEND] dest={0} broker={1} ERR: {2}",
                            (self.dest_pub_hex[:12], winner_host, repr(exc)),
                        ))
                        log_exception()

                tasks = list(pending)

        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

            await asyncio.gather(*tasks, return_exceptions=True)

            for client in self.clients:
                client.dequeue_msg(msg_id_hex)

        log(fstr(
            "[SMARTPIPE-SEND] dest={0} result=0 (no ACK from any client)",
            (self.dest_pub_hex[:12],),
        ))
        return 0

    async def close(self):
        """Evict cached clients from the router; do NOT close them.

        SmartPipe borrows MQTTClient instances from the Router's shared
        pool -- it does not own them. Calling client.close() here
        permanently poisoned those clients (is_closed stays set,
        dispatcher exits immediately on next connect) and broke all
        future sends through those brokers for the lifetime of the node.
        """
        for client in self.clients:
            self.router.evict_client_from_cache(client)

    async def __aenter__(self):
        """Connect on context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, *_):
        """Close on context manager exit."""
        await self.close()
        return False
