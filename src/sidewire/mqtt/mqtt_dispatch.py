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

Dispatcher loop is not optimized. It's not going to be used to send thousands
of msgs a second but a few messages for signaling to form connections.
Messages sent with this are minimal.

Can potentially go wrong if both sides use different keep_alive intervals
and using attempts based on the formula for interval and keep alive checks.
"""

import asyncio
from aionetiface import *
from .mqtt_connect import *
from .mqtt_packet import *

async def ensure_connection(client, keep_alive):
    """
    Event is set when connect_lost in protocol occurs.
    Loop forever until connection succeeds.
    """
    while not client.is_closed.is_set():
        if client.pipe and not client.pipe.on_close.is_set():
            return True

        print("Got con lost event")
        # Close old handle.
        if client.pipe:
            await client.pipe.close(force=True)

        # Connect a new one.
        try:
            pipe = await mqtt_connect(client, keep_alive)
            if pipe:
                return True
        except (asyncio.TimeoutError, ConnectionError, OSError):
            # Server still down.
            pass

        # Avoid immediately reconnecting to avoid DoS.
        print("In disconnect loop?")
        await asyncio.sleep(60)
    return False

def iter_all_messages(msg_queues):
    """Flattens the 3-level nested dict into a single generator."""
    # Queues are split by app msg type: MSG or MSGACK.
    for msg_type in msg_queues:
        # Each plugin instance has a unique pipe_id.
        for pipe_id_hex in list(msg_queues[msg_type].keys()):
            # Messages are further indexed by sequence (order.)
            queue = msg_queues[msg_type][pipe_id_hex]
            for seq_no in sorted(queue.keys()):
                yield queue[seq_no]

async def process_meta(client, meta, now, interval, republish_duration):
    """Logic for each individual meta part."""
    # Message has already been acked.
    if meta["app_ack"].done():
        return

    # Don't rebroadcast if too soon.
    interval_elapsed = now - meta["updated"]
    if interval_elapsed < interval:
        return

    # Republish duration exceeded.
    total_elapsed = now - meta["created"]
    if total_elapsed >= republish_duration:
        return

    # Increase state counters.
    meta["updated"] = now

    # Broadcast new message.
    buf, packet_ack = client.publish(
        meta["dest_pk_hex"], 
        meta["out"],
    )

    await async_wrap_errors(
        client.pipe.send(buf)
    )

async def check_and_ping(client, last_ping, keep_alive):
    """Send ping to server every so often."""
    now = asyncio.get_event_loop().time()
    if now - last_ping >= keep_alive:
        req = MQTTPacket(MQTTEnum.PINGREQ)
        buf = req.build()
        await async_wrap_errors(client.pipe.send(buf))
        return now  # Return new last_ping time
    return last_ping


"""
Putting the reconnect loop at the start helps ensure that later code
here that calls client.pipe.send succeeds, otherwise client.pipe is
set to None and send won't exist yet.

Pipe.send on broken con ends up calling connection_lost
to set on_close for the pipe event.
"""
async def dispatcher(client, republish_duration, interval, keep_alive, ignore_acked=False, reconnect_delay=0):
    republish_duration = max(republish_duration, 2 * keep_alive)
    last_ping = asyncio.get_event_loop().time()
    try:
        # Loop forever until is close is set or task cancelled.
        while not client.is_closed.is_set():
            # Conditional used for testing.
            if reconnect_delay:
                await asyncio.sleep(reconnect_delay)

            # Ensure pipe is alive before attempting to send.
            if not await ensure_connection(client, keep_alive):
                break

            now = asyncio.get_event_loop().time()

            # Process all messages in the queues.
            for meta in iter_all_messages(client.msg_queues):
                await process_meta(client, meta, now, interval, republish_duration)

            # Used to know when to send a ping.
            await asyncio.sleep(0.5)

            # Keep-alive heartbeat.
            last_ping = await check_and_ping(client, last_ping, keep_alive)

    except asyncio.CancelledError:
        print("dispatcher exited.")
        # Cleanup handled by caller.
    except Exception:
        what_exception()
        log_exception()

    print("dispatcher exited cleanly.")