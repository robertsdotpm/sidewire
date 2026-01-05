import socket
import struct


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

def dec_varint(sock):
    mul = 1
    val = 0
    while True:
        b = ord(sock.recv(1))
        val += (b & 0x7f) * mul
        if not (b & 0x80):
            return val
        mul <<= 7

def enc_str(s):
    b = s.encode("utf-8")
    return struct.pack("!H", len(b)) + b

class MQTT:
    def __init__(self, host, port=1883, client_id="min35"):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.sock = None

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port))

        vh = (
            enc_str("MQTT") +   # protocol name
            b"\x04" +           # protocol level
            b"\x02" +           # clean session
            b"\x00\x3c"         # keepalive 60s
        )

        pl = enc_str(self.client_id)
        pkt = b"\x10" + enc_varint(len(vh) + len(pl)) + vh + pl
        self.sock.sendall(pkt)

        # CONNACK (fixed 4 bytes)
        self.sock.recv(4)

    def subscribe(self, topic):
        pkt_id = 1
        vh = struct.pack("!H", pkt_id)
        pl = enc_str(topic) + b"\x00"  # QoS 0
        pkt = b"\x82" + enc_varint(len(vh) + len(pl)) + vh + pl
        self.sock.sendall(pkt)

        # SUBACK
        self.sock.recv(5)

    def publish(self, topic, payload):
        pl = enc_str(topic) + payload.encode("utf-8")
        pkt = b"\x30" + enc_varint(len(pl)) + pl
        self.sock.sendall(pkt)

    def loop(self):
        while True:
            hdr = self.sock.recv(1)
            if not hdr:
                break

            pkt_type = ord(hdr) >> 4
            rem_len = dec_varint(self.sock)

            data = b""
            while len(data) < rem_len:
                data += self.sock.recv(rem_len - len(data))

            if pkt_type == 3:  # PUBLISH
                tlen = struct.unpack("!H", data[:2])[0]
                topic = data[2:2+tlen].decode("utf-8", "ignore")
                msg = data[2+tlen:].decode("utf-8", "ignore")
                print("RECV:", topic, msg)

m = MQTT("test.mosquitto.org")
m.connect()
m.subscribe("test/min35")
m.publish("test/min35", "hello from py3.5")
m.loop()