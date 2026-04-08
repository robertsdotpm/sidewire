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

        print(self.clients)

        for client in self.clients:
            client.add_msg_handler(msg_cb)

        return self

    async def send(self, msg, timeout=4):
        msg_id_hex = to_h(rand_b(32))

        # Schedule all attempts concurrently
        tasks = []
        for client in self.clients:
            _, ack_msg = client.queue_msg(msg, self.dest_pub_hex, msg_id_hex)
            tasks.append(
                asyncio.ensure_future(
                    asyncio.wait_for(ack_msg, timeout)
                )
            )

        # Get first completed send that's acked.
        result = 0
        try:
            while tasks:
                done, pending = await asyncio.wait(
                    tasks, 
                    return_when=asyncio.FIRST_COMPLETED
                )
                
                for task in done:
                    try:
                        # Check if this task finished successfully
                        await task 
                        result = len(msg)

                        # We found our winner! Break the inner loop
                        break 
                    except (asyncio.TimeoutError, Exception):
                        # Task error -- remove from list.
                        if task in tasks:
                            tasks.remove(task)
                
                # If we found a result, stop waiting for other tasks
                if result > 0:
                    break
        finally:
            # Cleanup: Cancel any tasks that are still running
            for task in tasks:
                if not task.done():
                    task.cancel()

            # Remove all messages from being broadcast.
            for client in self.clients:
                client.dequeue_msg(msg_id_hex)

        return result
    
    async def close(self):
        for client in self.clients:
            await client.close()