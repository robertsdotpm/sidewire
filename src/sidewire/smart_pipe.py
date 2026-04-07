"""
Note: we can do the first complete optimization here but not going to
bother with optimization while prototyping.
May not even use setting my own queue_ids as ordering can be done
by asyncio itself.
"""

from .utils import *
import asyncio

class SmartPipe:
    def __init__(self, router, dest_pub_hex):
        self.router = router
        self.dest_pub_hex = dest_pub_hex
        self.clients = []

    # TODO: register message receiver callback.
    async def connect(self, msg_cb=None):
        self.clients = await get_dest_clients(
            self.router.nic,
            self.dest_pub_hex,
            self.router.servers,
            self.router.clients
        )

        return self

    async def send(self, msg, timeout=3):
        for client in self.clients:
            _, ack_msg = await client.queue_msg(
                msg,
                self.dest_pub_hex
            )

            try:
                await asyncio.wait_for(ack_msg, timeout)
                return len(msg)
            except asyncio.TimeoutError:
                continue

        return 0