from .utils import *
import asyncio

class SmartPipe:
    def __init__(self, router, dest_pub_hex, clients=None):
        self.router = router
        self.dest_pub_hex = dest_pub_hex
        self.clients = clients or []

    async def connect(self, msg_cb=None):
        if not len(self.clients):
            self.clients = await get_dest_clients(
                self.router.nic,
                self.dest_pub_hex,
                self.router.servers,
                self.router.clients
            )

        if msg_cb:
            for client in self.clients:
                client.add_msg_handler(msg_cb)

        return self

    async def send(self, msg, timeout=4):
        msg_id_hex = to_h(rand_b(32))

        # Create all tasks upfront
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
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED
                )

                for task in done:
                    try:
                        # Await result (will raise if failed)
                        await task

                        # Success: cancel everything else immediately
                        for p in pending:
                            p.cancel()

                        # Optional: wait for cancellations to settle
                        await asyncio.gather(*pending, return_exceptions=True)

                        return len(msg)

                    except asyncio.TimeoutError:
                        # Ignore and continue
                        pass
                    except Exception:
                        # Ignore but could log here
                        pass

                # Continue only with remaining tasks
                tasks = list(pending)

        finally:
            # Ensure all tasks are cleaned up
            for task in tasks:
                if not task.done():
                    task.cancel()

            await asyncio.gather(*tasks, return_exceptions=True)

            # Remove queued messages from all clients
            for client in self.clients:
                client.dequeue_msg(msg_id_hex)

        return 0
    
    async def close(self):
        for client in self.clients:
            await client.close()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *_):
        await self.close()
        return False