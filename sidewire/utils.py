from aionetiface import *

# { AF: host: pipe
async def select_signal_pipes(signal_pipes, dest, mqtt_cls, n=1):
    pipes = []
    tasks = []

    # Collect shared signal pipes.
    # Open any pipes to dest signal servers along the way.
    for af in (IP4, IP6):
        for signal_dest in dest["signal"][af]:
            host, port = signal_dest
            if host in signal_pipes[af]:
                pipes.append(signal_pipes[af][host])
            else:
                async def helper():
                    client = await mqtt_cls(signal_dest).connect()
                    signal_pipes[af][host] = client
                    pipes.append(client)

                tasks.append(helper())
    
    # Open the previous pipes as needed.
    remaining = n - min(n, len(pipes))
    if remaining:
        [task.close() for task in tasks[:remaining]]
        tasks = tasks[remaining:]
        await asyncio.gather(*tasks, return_exceptions=True)

    return pipes

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