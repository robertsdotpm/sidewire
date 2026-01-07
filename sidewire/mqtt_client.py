import struct
from aionetiface import *

def enc_varint(n):
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

def enc_str(s):
    b = s.encode("utf-8")
    return struct.pack("!H", len(b)) + b

def decode_varint_from_buffer(buf):
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

class MQTT:
    def __init__(self, dest, client_id="min35"):
        self.host, self.port = dest
        self.client_id = client_id
        self.pipe = None
        self.buffer = b""

    def __await__(self):
        return self.connect().__await__()

    async def connect(self, nic=Interface("default")):
        self.pipe = await Pipe(TCP, (self.host, self.port), nic).connect()

        vh = (
            enc_str("MQTT") +   # protocol name
            b"\x04" +           # protocol level
            b"\x02" +           # clean session
            b"\x00\x3c"         # keepalive 60s
        )

        pl = enc_str(self.client_id)
        pkt = b"\x10" + enc_varint(len(vh) + len(pl)) + vh + pl
        await self.pipe.send(pkt)

        # CONNACK (fixed 4 bytes)
        await self.pipe.recv()

    async def subscribe(self, topic):
        pkt_id = 1
        vh = struct.pack("!H", pkt_id)
        pl = enc_str(topic) + b"\x00"  # QoS 0
        pkt = b"\x82" + enc_varint(len(vh) + len(pl)) + vh + pl
        await self.pipe.send(pkt)

        # SUBACK
        await self.pipe.recv()

    async def publish(self, topic, payload):
        pl = enc_str(topic) + payload.encode("utf-8")
        pkt = b"\x30" + enc_varint(len(pl)) + pl
        await self.pipe.send(pkt)

    async def loop(self):
        while True:
            # Receive more data into buffer
            chunk = await self.pipe.recv()
            if not chunk:
                break
            self.buffer += chunk

            while True:
                if len(self.buffer) < 2:
                    break  # not enough for fixed header

                rem_len, consumed = decode_varint_from_buffer(self.buffer[1:])
                if rem_len is None:
                    break  # need more bytes for varint

                total_len = 1 + consumed + rem_len
                if len(self.buffer) < total_len:
                    break  # wait for full packet

                packet = self.buffer[:total_len]
                self.buffer = self.buffer[total_len:]
                await self.handle_packet(packet)

    async def handle_packet(self, buf):
        pkt_type = buf[0] >> 4
        # remaining length ignored here, already parsed
        rem_len, consumed = decode_varint_from_buffer(buf[1:])
        data = buf[1+consumed:]

        if pkt_type == 3:  # PUBLISH
            if len(data) < 2:
                return
            tlen = struct.unpack("!H", data[:2])[0]
            if len(data) < 2 + tlen:
                return
            topic = data[2:2+tlen].decode("utf-8", "ignore")
            msg = data[2+tlen:].decode("utf-8", "ignore")
            print("RECV:", topic, msg)

async def workspace():
    m = MQTT("test.mosquitto.org")
    await m.connect()
    await m.subscribe("test/min35")
    await m.publish("test/min35", "hello from py3.5")
    await m.loop()

if __name__ == "__main__":
    async_run(workspace())