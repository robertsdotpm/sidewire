import time
import asyncio
from aionetiface import *
from .mqtt_connect import *
from .mqtt_packet import *

async def dispatcher(client, attempts=3, interval=60, keep_alive=30):
    counter = 0
    try:
        """
        Putting the reconnect loop at the start helps ensure that later code
        here that calls client.pipe.send succeeds, otherwise client.pipe is
        set to None and send won't exist yet.

        Pipe.send on broken con ends up calling connection_lost
        to set on_close for the pipe event.
        """
        while not client.is_closed.is_set():
            if client.pipe is None or client.pipe.on_close.is_set():
                print("Got con lost event")
                while not client.is_closed.is_set():
                    # Close old handle.
                    if client.pipe:
                        await client.pipe.close(force=True)

                    # Connect a new one.
                    try:
                        pipe = await mqtt_connect(client, keep_alive)
                        if pipe:
                            break
                    except (asyncio.TimeoutError, ConnectionError, OSError):
                        # Server still down.
                        pass

                    await asyncio.sleep(60)

            # Process messages in various queues.
            for msg_type in client.msg_queues:
                for pipe_id_hex in client.msg_queues[msg_type]:
                    for meta in client.msg_queues[msg_type][pipe_id_hex]:
                        # Already acked -- try next in line.
                        if meta["acked"].done():
                            continue

                        # Don't rebroadcast if too soon.
                        elapsed = int(time.time()) - meta["updated"]
                        if elapsed < interval:
                            break

                        # Give up on acking this line of messages if too many past.
                        if meta["attempts"] >= attempts:
                            break

                        # Increase state counters.
                        meta["attempts"] += 1
                        meta["updated"] = int(time.time())

                        # Broadcast new message.
                        print("dispatching ", meta)
                        buf, _ = client.publish(meta["dest_pk_hex"], meta["out"])
                        await async_wrap_errors(
                            client.pipe.send(buf)
                        )

            # Used to know when to send a ping.
            await asyncio.sleep(1)
            counter += 1 % 0xFFFFFFFFFF

            # Send ping to server every so often.
            if (counter % (keep_alive + 1)) == keep_alive:
                req = MQTTPacket(MQTTEnum.PINGREQ)
                buf = req.build()
                await async_wrap_errors(client.pipe.send(buf))
    except asyncio.CancelledError:
        print("dispatcher exited.")
        # Cleanup handled by caller.
    except Exception:
        log_exception()