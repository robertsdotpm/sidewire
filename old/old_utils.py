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

def flat_sig_pipes(sig_pipes):
    flat = []
    for af in (IP4, IP6):
        if af in sig_pipes:
            flat += list(sig_pipes[af].values())

    return flat

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

async def load_signal_pipes(af, nic, seed_str, n, filter_list=[]):
    # Monitor incorrectly lists TCP servers under UDP.
    # Todo: fix this.
    # TODO: this itself is random so this is not working as expected
    servers = get_infra(af, UDP, "MQTT", sample=0)
    servers = [(s[0]["fqns"][0], s[0]["port"]) for s in servers if len(s[0]["fqns"])]
    mqtt_iter = seed_iter(servers, "test") # TODO

    def select_servers(n, kv):
        return [next(mqtt_iter) for x in range(0, n) if x not in filter_list]

    c = ObjCollection(
        lambda kparams, dest=None: MQTTClient(**kparams, dest=dest),
        select_servers=select_servers
    )

    out = await c.get_n(n, kv={
            "factory": {
                "af": af,
                "nic": nic,
                "node_id": seed_str,
            }
        }
    )

    return out