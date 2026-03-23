from aionetiface import *
from .mqtt_packet import *

def flat_sig_pipes(sig_pipes):
    flat = []
    for af in (IP4, IP6):
        if af in sig_pipes:
            flat += list(sig_pipes[af].values())

    return flat

# { AF: host: pipe
async def select_signal_pipes(ifs, signal_pipes, dest, load_signal_pipes, n=1):
    return flat_sig_pipes(signal_pipes) # TODO
    nic_afs = get_nic_for_af(ifs)
    for af in (IP4, IP6):
        new_pipes = await load_signal_pipes(
            af=af,
            nic=nic_afs[af],
            seed_str=dest["node_id"],
            n=n,
            filter_list=[s.dest for s in flat_sig_pipes(signal_pipes)]
        )

        # Record any loaded pipes.
        for pipe in new_pipes:
            signal_pipes[af][pipe.host] = pipe

    # TODO: for simplicity it doesn't filter pipes that don't overlap.
    # making signaling less efficent but the code simpler.
    return flat_sig_pipes(signal_pipes)

async def send_msg_over_mqtt(router, buf, dest, load_signal_pipes, relay_limit=2):
    # Try not to load a new signal pipe if
    # one already exists for the dest.
    selected_pipes = await select_signal_pipes(
        router.ifs,
        router.signal_pipes,
        dest,
        load_signal_pipes,
        relay_limit
    )

    print("selected sig pipes = ", selected_pipes)

    # Try signal pipes in order.
    # If connect fails try another.
    for sig_pipe in selected_pipes:
        # Send message.
        print("send to ", dest["node_id"], " ", sig_pipe.dest)
        await async_wrap_errors(
            sig_pipe.publish(
                to_b(dest["node_id"]),
                buf,
            )
        )

def try_unpack_msg(buf, sk, sig_proto_map):
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

def sig_msg_to_buf(msg):
    # Else loaded from a MSN.
    dest_vk = msg.routing.dest["vk"]
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
    return to_b(buf)

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
    b = to_b(s)
    return struct.pack("!H", len(b)) + b

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

async def handle_connect(self, af, host, port, client_id, node_id, nic):
    print("mqtt connect")
    route = nic.route(af)
    pipe = await Pipe(TCP, (host, port), route).connect()

    # proto name, proto level, clean session, keep alive 60s
    vh = (mqtt_enc_str("MQTT") + b"\x04" + b"\x02" + b"\x00\x3c")
    pl = mqtt_enc_str(client_id)

    # Full packet to send.
    pkt = b"\x10" + mqtt_enc_varint(len(vh) + len(pl)) + vh + pl
    await pipe.send(pkt)

    # CONNACK (fixed 4 bytes)
    got = await asyncio.wait_for(pipe.recv_n(4), 4)
    if got != b' \x02\x00\x00':
        await pipe.close()
        raise BadProtoResp("Invalid CON ACK")
    

    print("mqtt Connected success")
    return pipe



    """
    # Subscribe to client id.
    await self.subscribe(self.node_id)

    # Create processing task.
    self.pipe.add_msg_cb(self.msg_cb)
    return self
    """

"""
Handle a stream of data down a buffer for TCP,
returning the first valid MQTT packet. Surplus
data is kept on the buffer for subsequent reads.
"""
async def recv_mqtt_pkt(self):
    while 1:
        self.buf += await self.pipe.recv()
        if len(self.buf) < 2:
            await asyncio.sleep(0.4)
            continue

        # need more bytes for varint
        rem_len, consumed = mqtt_decode_varint(self.buf[1:])
        if rem_len is None:
            continue  

        # wait for full packet
        total_len = 1 + consumed + rem_len
        if len(self.buf) < total_len:
            continue  

        packet = self.buf[:total_len]
        self.buf = self.buf[total_len:]
        return packet

async def handle_subscribe(self, topic):
    pkt_id = 1
    vh = struct.pack("!H", pkt_id)
    pl = mqtt_enc_str(topic) + b"\x00"  # QoS 0
    pkt = b"\x82" + mqtt_enc_varint(len(vh) + len(pl)) + vh + pl
    await self.pipe.send(pkt)


    print("sub pkt = ", pkt)

async def handle_publish(self, topic, payload):
    pl = mqtt_enc_str(topic) + to_b(payload)
    pkt = b"\x30" + mqtt_enc_varint(len(pl)) + pl
    await self.pipe.send(pkt)

