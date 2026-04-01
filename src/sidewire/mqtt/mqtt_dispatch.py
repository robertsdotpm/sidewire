"""
The main class interface feeds plugin-specific queues ordered messages. Each message
has an application-level ACK attached to it. The dispatcher is a background task
that loops over all messages and republishes them so long as:
- still within msg's republish duration
- outside the last interval sent
- not already acked

The dispatcher doesn't try to maintain ordering of messages in a queue based
on next to send after a message has been "acked." The caller of send is expected
not to queue any more messages for their protocol until they receive an ack for the
current send. This keeps the dispatcher code simple.

It also allows easier optimization: eg. receiving a message implies that 
the other side also received an ack for the old message, thus we can delete
acking all past messages less than the current sequence number.

It should be noted that because this dispatcher doesnt enforce ordering calling:

for ...
    client.send

... without awaiting the returned ack means that event handlers passed to send are
run in a random order due to race conditions.
"""

import time
import asyncio
import math
from aionetiface import *
from .mqtt_connect import *
from .mqtt_packet import *


"""
Dispatcher loop is not optimized. It's not going to be used to send thousands
of msgs a second but a few messages for signaling to form connections.
Messages sent with this are minimal.

Can potentially go wrong if both sides use different keep_alive intervals
and using attempts based on the formula for interval and keep alive checks.
"""
async def dispatcher(client, republish_duration, interval, keep_alive, ignore_acked=False, reconnect_delay=0):
    republish_duration = max(republish_duration, 2 * keep_alive)
    last_ping = asyncio.get_event_loop().time()
    try:
        """
        Putting the reconnect loop at the start helps ensure that later code
        here that calls client.pipe.send succeeds, otherwise client.pipe is
        set to None and send won't exist yet.

        Pipe.send on broken con ends up calling connection_lost
        to set on_close for the pipe event.
        """
        while not client.is_closed.is_set():
            if reconnect_delay:
                await asyncio.sleep(reconnect_delay)

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

                    print("In disconnect loop?")
                    await asyncio.sleep(60)

            # Process messages in various queues.
            for msg_type in client.msg_queues:
                for pipe_id_hex in list(client.msg_queues[msg_type].keys()):
                    seq_nos = client.msg_queues[msg_type][pipe_id_hex].keys()
                    now = asyncio.get_event_loop().time()
                    for seq_no in sorted(list(seq_nos)):
                        meta = client.msg_queues[msg_type][pipe_id_hex][seq_no]
                        #print("d", msg_type, " ", meta)

                        if meta["acked"].done():
                            continue

                        # Don't rebroadcast if too soon.
                        interval_elapsed = now - meta["updated"]
                        if interval_elapsed < interval:
                            #print("ed: elapsed < interval", elapsed, " ", interval)
                            continue

                        # Republish duration exceeded.
                        total_elapsed = now - meta["created"]
                        if total_elapsed >= republish_duration:
                            #print("ed: attempts >= attempts")
                            continue

                        # Increase state counters.
                        meta["updated"] = now

                        # Broadcast new message.
                        #print("dispatching ", meta)
                        buf, _ = client.publish(meta["dest_pk_hex"], meta["out"])
                        await async_wrap_errors(
                            client.pipe.send(buf)
                        )

            # Used to know when to send a ping.
            await asyncio.sleep(0.5)

            # Send ping to server every so often.
            now = asyncio.get_event_loop().time()
            if now - last_ping >= keep_alive:
                last_ping = now
                req = MQTTPacket(MQTTEnum.PINGREQ)
                buf = req.build()
                await async_wrap_errors(client.pipe.send(buf))
    except asyncio.CancelledError:
        print("dispatcher exited.")
        # Cleanup handled by caller.
    except Exception:
        what_exception()
        log_exception()

    print("dispatcher exited cleanly.")