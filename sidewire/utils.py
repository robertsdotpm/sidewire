from aionetiface import *

def find_signal_pipe(node, addr):
    our_offsets = list(node.signal_pipes)
    for offset in addr["signal"]:
        if offset in our_offsets:
            return node.signal_pipes[offset]

    return None

# Make already loaded sig pipes first to try.
def prioritize_sig_pipe_overlap(node, offsets):
    overlap = []
    non_overlap = []
    for offset in offsets:
        if offset in node.signal_pipes:
            overlap.append(offset)
        else:
            non_overlap.append(offset)

    return overlap + non_overlap

async def send_msg_over_mqtt(router, msg, relay_limit=2):
    # Else loaded from a MSN.
    buf = sig_msg_to_buf(msg)

    # Try not to load a new signal pipe if
    # one already exists for the dest.
    dest = msg.routing.dest
    offsets = dest["signal"]
    offsets = prioritize_sig_pipe_overlap(router, offsets)

    # Try signal pipes in order.
    # If connect fails try another.
    relay_count = 0
    for i in range(0, len(offsets)):
        # Get index referencing an MQTT server.
        offset = offsets[i]

        # Use existing sig pipe.
        if offset in router.signal_pipes:
            sig_pipe = router.signal_pipes[offset]

        # Or load new server offset.
        if offset not in router.signal_pipes:
            sig_pipe = await async_wrap_errors(
                load_signal_pipe(
                    router.node_id,
                    msg.routing.af,
                    offset,
                    MQTT_SERVERS,
                    router.msg_cb
                )
            )

            # Record it if success.
            if sig_pipe:
                router.signal_pipes[offset] = sig_pipe

        # Skip invalid sig pipes.
        if not sig_pipe:
            continue

        # Send message.
        print("send to ", dest["node_id"], " ", offset)
        await async_wrap_errors(
            sig_pipe.send_msg(
                buf,
                to_s(dest["node_id"])
            )
        )

        """
        Relay message across a minimum no of MQTT servers
        to help ensure message is received.
        """
        relay_count += 1
        if relay_count >= relay_limit:
            break
        
    # TODO: no paths to host.
    # Need fallback plan here.

def try_unpack_msg(buf, sk, sig_proto_map):
    print("try unpack msg = ", buf)
    buf = h_to_b(buf)

    # Try to decrypt message if its encrypted.
    is_enc = buf[0]
    if is_enc:
        # Ensure a SK is set for decryption.
        if not sk:
            raise Exception("No sk set for decryption.")

        # Will raise if it can't decrypt.
        buf = decrypt(
            sk,
            buf[1:]
        )
        log(fstr("Recv decrypted {0}", (buf,)))
    
    # Otherwise buffer is not encrypted -- use as is.
    if not is_enc:
        buf = buf[1:]

    # Unpack message into fields.
    msg_info = sig_proto_map[buf[0]]
    msg_class = msg_info[0]
    msg = msg_class.unpack(buf[1:])
    return msg

def discard_old_msg(msg, seen, f_time):
    # Old message?
    pipe_id = msg.meta.pipe_id
    if pipe_id in seen:
        log("Discard already seen msg.")
        return
    else:
        seen[pipe_id] = time.time()

    # Check TTL.
    if int(f_time()) >= msg.meta.ttl:
        log("Discard old msg.")
        return
    
    return msg

def sig_msg_to_buf(msg):
    # Else loaded from a MSN.
    dest_vk = msg.routing.dest["vk"]
    print("Dest vk = ", dest_vk)
    if dest_vk:
        assert(isinstance(dest_vk, bytes))
        buf = b"\1" + encrypt(
            dest_vk,
            msg.pack(),
        )
    else:
        buf = b"\0" + msg.pack()

    # UTF-8 messes up binary data in MQTT.
    buf = to_h(buf)
    return buf

def mqtt_enc_varint(n):
    out = b""
    while True:
        byte = n & 0x7f
        n >>= 7
        if n:
            byte |= 0x80
        out += bytes([byte])
        if not n:
            break
    return out

def mqtt_enc_str(s):
    b = s.encode("utf-8")
    return struct.pack("!H", len(b)) + b

def mqtt_decode_varint(buf):
    """Return (value, num_bytes_consumed)"""
    mul = 1
    val = 0
    consumed = 0
    for b in buf:
        val += (b & 0x7f) * mul
        consumed += 1
        if not (b & 0x80):
            return val, consumed
        mul *= 128
        if mul > 128**4:
            raise ValueError("Malformed variable-length int")
    return None, 0  # not enough bytes yet

async def handle_mqtt_packet(buf):
    pkt_type = buf[0] >> 4
    # remaining length ignored here, already parsed
    rem_len, consumed = mqtt_decode_varint(buf[1:])
    data = buf[1+consumed:]

    if pkt_type == 3:  # PUBLISH
        if len(data) < 2:
            return
        tlen = struct.unpack("!H", data[:2])[0]
        if len(data) < 2 + tlen:
            return
        topic = data[2:2+tlen].decode("utf-8", "ignore")
        msg = data[2+tlen:].decode("utf-8", "ignore")
        return {topic: msg}
        print("RECV:", topic, msg)