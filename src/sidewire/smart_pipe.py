import asyncio
from typing import Any, Callable, Dict, List, Optional
from aionetiface import log_exception, rand_b, to_h
from .utils import get_dest_clients, try_client


class SmartPipe:
    """Route messages to a destination over the best available MQTT clients."""

    def __init__(
        self,
        router: Any,
        dest_pub_hex: str,
        clients: Optional[List[Any]] = None,
        hint_brokers: Optional[List[Dict]] = None,
    ) -> None:
        """Initialize a SmartPipe with a router, destination public key, optional pre-resolved clients, and optional hint brokers."""
        self.router = router
        self.dest_pub_hex = dest_pub_hex
        self.clients = clients or []  # type: List[Any]
        # Hint brokers are {af, host, port} dicts the destination
        # advertised in its addr_bytes. We try connecting to these
        # first since the destination has guaranteed its
        # subscription is live there.
        self.hint_brokers = hint_brokers or []

    async def resolve_hint_clients(self) -> List[Any]:
        """Connect to each hint broker (if reachable) and return matching MQTTClient instances.

        Walks self.hint_brokers (the destination's advertised
        broker list) and tries to connect to each in turn. Any
        broker we can connect+subscribe to gets returned. The
        destination peer is GUARANTEED subscribed at these
        brokers because they came from the dest's own
        protected_clients list at addr-publish time.
        """
        out = []
        for hint in self.hint_brokers:
            af = hint.get("af")
            host = hint.get("host")
            port = hint.get("port")
            if af is None or not host or not port:
                continue
            af_clients = self.router.clients.get(af, {})
            client = af_clients.get(host)
            if client is None:
                # Hint broker isn't in our rendezvous-discovered
                # clients_map (we never instantiated an MQTTClient
                # for it). Skip -- if the dest is REALLY only
                # reachable via this broker the whole convergence
                # falls apart anyway, but that's a separate issue.
                continue
            connected = await try_client(self.dest_pub_hex, client)
            if connected is not None:
                out.append(connected)
        return out

    async def connect(self, msg_cb: Optional[Callable] = None) -> "SmartPipe":
        """Resolve destination clients: hint brokers first, falling back to rendezvous."""
        if not self.clients:
            # Try hint brokers first. If we get at least one, use
            # the hint set -- the dest is guaranteed subscribed.
            if self.hint_brokers:
                self.clients = await self.resolve_hint_clients()

            # Fall back to rendezvous discovery if hints didn't
            # produce any reachable clients.
            if not self.clients:
                self.clients = await get_dest_clients(
                    self.router.af_group,
                    self.dest_pub_hex,
                    self.router.servers,
                    self.router.clients,
                )

        if msg_cb:
            for client in self.clients:
                client.add_msg_handler(msg_cb)

        return self

    async def send(self, msg: str, timeout: int = 4) -> int:
        """Send a message to the destination, returning the byte length on success or 0 on failure."""
        msg_id_hex = to_h(rand_b(32))

        tasks = []
        for client in self.clients:
            _, ack_msg = client.queue_msg(msg, self.dest_pub_hex, msg_id_hex)
            task = asyncio.create_task(
                asyncio.wait_for(asyncio.shield(ack_msg), timeout)
            )
            tasks.append(task)

        try:
            while tasks:
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )

                for task in done:
                    try:
                        await task

                        for p in pending:
                            p.cancel()

                        await asyncio.gather(*pending, return_exceptions=True)

                        return len(msg)

                    except asyncio.TimeoutError:
                        pass
                    except (OSError, ConnectionError):
                        log_exception()

                tasks = list(pending)

        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

            await asyncio.gather(*tasks, return_exceptions=True)

            for client in self.clients:
                client.dequeue_msg(msg_id_hex)

        return 0

    async def close(self) -> None:
        """Evict cached clients from the router; do NOT close them.

        SmartPipe borrows MQTTClient instances from the Router's shared
        pool -- it does not own them. Calling client.close() here
        permanently poisoned those clients (is_closed stays set,
        dispatcher exits immediately on next connect) and broke all
        future sends through those brokers for the lifetime of the node.
        """
        for client in self.clients:
            self.router.evict_client_from_cache(client)

    async def __aenter__(self) -> "SmartPipe":
        """Connect on context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> bool:
        """Close on context manager exit."""
        await self.close()
        return False
