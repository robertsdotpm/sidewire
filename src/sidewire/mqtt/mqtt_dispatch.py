"""
The Dispatcher is a background task that republishes queued messages at a set retry interval until an application-level ACK is received. To maintain architectural simplicity, it does not enforce message ordering internally; instead, callers must await each ACK before sending the next message to prevent race conditions and out-of-order execution. While this design is optimized for low-volume signaling rather than high-throughput data, it allows for future enhancements like sequence numbers to implicitly ACK and clear older messages.

To ensure reliability, the system prioritizes running the reconnect loop first to initialize the communication pipe, handling any subsequent failures via a connection_lost callback. Because Asyncio is reactive and only identifies a broken connection after a failed interaction, a ping feature is used to trigger earlier reconnections. To avoid synchronization failures, keep-alive intervals must be consistent across both endpoints, with retry attempts tuned as a function of the interval and keep-alive duration to ensure they fall within a valid reconnect cycle.
"""
import asyncio
import random
from aionetiface import *
from .mqtt_connect import *
from .mqtt_packet import *
from .utils import *
from .mqtt_msgs import *

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
                #print(meta)
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
        #print("dispatcher exited.")
        pass
    except Exception:
        log_exception()

    #print("dispatcher exited cleanly.")

async def republish_meta(client, meta, now, interval, republish_duration, ignore_acked):
    # Message has already been acked.
    if ignore_acked:
        if meta["app_ack"].done():
            return

    # To handle multiple destinations independently, we store retry state 
    # directly in the 'meta' object for this specific message.
    attempts = meta.get("attempts", 0)

    # Calculate backoff based on attempts: interval * 2^attempts.
    # We cap the backoff at republish_duration to ensure we don't 
    # wait longer than the message's total allowed lifespan.
    backoff_limit = min(60, republish_duration) # Sane cap of 60s or duration.
    current_backoff = min(interval * (2 ** attempts), backoff_limit)

    # Apply Full Jitter: randomize the wait to prevent "thundering herd" 
    # if multiple messages were queued at once.
    jittered_interval = random.uniform(interval, current_backoff)

    # Don't rebroadcast if too soon.
    interval_elapsed = now - meta["updated"]
    if interval_elapsed < jittered_interval:
        return

    # Republish duration exceeded.
    total_elapsed = now - meta["created"]
    if total_elapsed >= republish_duration:
        return

    # Increase state counters for THIS specific message.
    meta["updated"] = now
    meta["attempts"] = attempts + 1

    # Create publish packet to send.
    buf, packet_ack = client.publish(
        meta["dest_pk_hex"], 
        meta["out"],
    )

    # Broadcast new message.
    await async_wrap_errors(
        client.pipe.send(buf)
    )