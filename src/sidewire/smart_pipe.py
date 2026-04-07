"""
Note: we can do the first complete optimization here but not going to
bother with optimization while prototyping.
May not even use setting my own queue_ids as ordering can be done
by asyncio itself.
"""

from .utils import *
import asyncio
import copy

class SmartPipe:
    def __init__(self, router, dest_pub_hex):
        self.router = router
        self.dest_pub_hex = dest_pub_hex
        self.clients = []

    async def connect(self, msg_cb=None):
        self.clients = await get_dest_clients(
            self.router.nic,
            self.dest_pub_hex,
            self.router.servers,
            self.router.clients
        )

        for client in self.clients:
            client.add_msg_handler(msg_cb)

        return self

    async def send(self, msg, timeout=4):
        for client in self.clients:
            print("queue attempt")
            _, ack_msg = client.queue_msg(
                msg,
                self.dest_pub_hex
            )

            try:
                await asyncio.wait_for(ack_msg, timeout)
                return len(msg)
            except asyncio.TimeoutError:
                continue


        return 0
    
    async def close(self):
        for client in self.clients:
            await client.close()