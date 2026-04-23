import asyncio
from typing import Any, Callable, List, Optional
from aionetiface import log_exception, rand_b, to_h
from .utils import get_dest_clients


class SmartPipe:
    """Route messages to a destination over the best available MQTT clients."""

    def __init__(
        self, router: Any, dest_pub_hex: str, clients: Optional[List[Any]] = None
    ) -> None:
        """Initialize a SmartPipe with a router, destination public key, and optional pre-resolved clients."""
        self.router = router
        self.dest_pub_hex = dest_pub_hex
        self.clients = clients or []  # type: List[Any]

    async def connect(self, msg_cb: Optional[Callable] = None) -> "SmartPipe":
        """Discover destination clients via rendezvous hash if not already resolved."""
        if not self.clients:
            self.clients = await get_dest_clients(
                self.router.nic,
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
        """Close all underlying MQTT client connections."""
        for client in self.clients:
            await client.close()

    async def __aenter__(self) -> "SmartPipe":
        """Connect on context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> bool:
        """Close on context manager exit."""
        await self.close()
        return False
