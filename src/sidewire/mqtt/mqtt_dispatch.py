"""
The dispatcher is a background task that republishes queued messages based on a 
retry interval and total duration until an application-level ACK is received. To 
keep the code simple, the dispatcher does not enforce message ordering; instead, 
callers are required to await an ACK before sending the next message to avoid 
race conditions and out-of-order event execution.

This design allows for easy optimization, such as using sequence numbers to 
implicitly ACK and delete all older messages. The system is intended for 
low-volume signaling rather than high-throughput data. Key safety measures 
include running the reconnect loop first to ensure the communication pipe is 
initialized and handling broken connections via a connection_lost callback. 
Users should ensure both sides use consistent keep_alive intervals to avoid 
synchronization failures.
"""
import asyncio
from aionetiface import *
from .mqtt_connect import *
from .mqtt_packet import *
from .utils import *

# Cleanup handled by mqtt_client.close.
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
            await reconnect_loop(client, keep_alive)

            # Process all messages in the queues.
            now = asyncio.get_event_loop().time()
            for meta in iter_all_messages(client.msg_queues):
                await republish_meta(
                    client, 
                    meta, 
                    now, 
                    interval, 
                    republish_duration,
                    ignore_acked,
                )

            # Used to know when to send a ping.
            await asyncio.sleep(0.5)

            # Keep-alive heartbeat.
            last_ping, ping_buf = build_ping(last_ping, keep_alive)
            if ping_buf: 
                await client.pipe.send(ping_buf)
    except asyncio.CancelledError:
        print("dispatcher exited.")
    except Exception:
        log_exception()

    print("dispatcher exited cleanly.")

async def republish_meta(client, meta, now, interval, republish_duration, ignore_acked):
    # Message has already been acked.
    if ignore_acked:
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

    # Create publish packet to send.
    buf, packet_ack = client.publish(
        meta["dest_pk_hex"], 
        meta["out"],
    )

    # Broadcast new message.
    await async_wrap_errors(
        client.pipe.send(buf)
    )