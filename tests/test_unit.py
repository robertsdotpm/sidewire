"""
Unit tests for sidewire pure functions: MQTT encoding, AppPacket
serialization, rendezvous hashing, and routing utilities.

These tests run entirely offline — no MQTT broker or network required.
"""

import struct
import unittest
from aionetiface import (
    Signing,
    IP4,
    IP6,
    to_h,
    rand_b,
)
from sidewire.mqtt.utils import (
    mqtt_encode_varint,
    mqtt_decode_varint,
    mqtt_enc_str,
    iter_all_messages,
    prune_msg_ids,
    get_msg_from_queue,
)
from sidewire.mqtt.app_packet import AppPacket
from sidewire.mqtt.mqtt_defs import MsgEnum
from sidewire.utils import interleave_buckets, get_server_score


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client_stub(kp=None, get_time=None):
    """Minimal object mimicking the fields AppPacket.pack() reads."""
    import time as _time

    class _Stub:
        pass

    stub = _Stub()
    stub.kp = kp or Signing.keypair()
    stub.get_time = get_time or _time.time
    return stub


def _make_queue_id():
    return to_h(rand_b(32))  # 64 hex chars


# ---------------------------------------------------------------------------
# MQTT varint encoding/decoding
# ---------------------------------------------------------------------------


class TestMQTTVarint(unittest.TestCase):
    def _roundtrip(self, value):
        encoded = mqtt_encode_varint(value)
        decoded, consumed = mqtt_decode_varint(encoded, 0)
        self.assertEqual(decoded, value)
        self.assertEqual(consumed, len(encoded))

    def test_zero(self):
        self._roundtrip(0)

    def test_small_values(self):
        for v in [1, 63, 127]:
            self._roundtrip(v)

    def test_two_byte_boundary(self):
        self._roundtrip(128)
        self._roundtrip(300)
        self._roundtrip(16383)

    def test_three_byte_boundary(self):
        self._roundtrip(16384)
        self._roundtrip(2097151)

    def test_four_byte_max(self):
        self._roundtrip(268435455)  # max representable in 4 bytes

    def test_incomplete_buffer_raises(self):
        with self.assertRaises(ValueError):
            mqtt_decode_varint(b"", 0)

    def test_malformed_varint_raises(self):
        # Four continuation bits in a row is not a valid MQTT varint.
        with self.assertRaises(ValueError):
            mqtt_decode_varint(b"\x80\x80\x80\x80", 0)

    def test_offset_respected(self):
        buf = b"\x00" + mqtt_encode_varint(42)
        value, _ = mqtt_decode_varint(buf, 1)
        self.assertEqual(value, 42)

    def test_encode_produces_bytes(self):
        self.assertIsInstance(mqtt_encode_varint(100), bytes)

    def test_single_byte_encoding(self):
        # Values 0-127 must encode to exactly 1 byte
        for v in [0, 1, 127]:
            self.assertEqual(len(mqtt_encode_varint(v)), 1)

    def test_two_byte_encoding(self):
        # 128 requires 2 bytes
        self.assertEqual(len(mqtt_encode_varint(128)), 2)


# ---------------------------------------------------------------------------
# MQTT string encoding
# ---------------------------------------------------------------------------


class TestMQTTEncStr(unittest.TestCase):
    def test_length_prefix_correct(self):
        s = "hello"
        encoded = mqtt_enc_str(s)
        length = struct.unpack("!H", encoded[:2])[0]
        self.assertEqual(length, len(s))

    def test_content_preserved(self):
        s = "test_topic"
        encoded = mqtt_enc_str(s)
        self.assertEqual(encoded[2:], s.encode())

    def test_empty_string(self):
        encoded = mqtt_enc_str("")
        length = struct.unpack("!H", encoded[:2])[0]
        self.assertEqual(length, 0)

    def test_returns_bytes(self):
        self.assertIsInstance(mqtt_enc_str("x"), bytes)

    def test_non_ascii(self):
        s = "héllo"
        encoded = mqtt_enc_str(s)
        length = struct.unpack("!H", encoded[:2])[0]
        # The length prefix must match the actual encoded body length
        self.assertEqual(length, len(encoded) - 2)


# ---------------------------------------------------------------------------
# AppPacket pack / unpack
# ---------------------------------------------------------------------------


class TestAppPacketRoundTrip(unittest.TestCase):
    def _make_packet(self, msg="hello", msg_type=MsgEnum.MSG, seq_no=1):
        queue_id = _make_queue_id()
        return AppPacket(
            queue_id_hex=queue_id,
            seq_no=seq_no,
            msg_type=msg_type,
            msg=msg,
        )

    def test_pack_unpack_basic(self):
        client = _make_client_stub()
        pkt = self._make_packet()
        packed = pkt.pack(client)
        restored = AppPacket.unpack(packed)

        self.assertIsNotNone(restored)
        self.assertEqual(restored.msg, pkt.msg)
        self.assertEqual(restored.seq_no, pkt.seq_no)
        self.assertEqual(restored.queue_id_hex, pkt.queue_id_hex)

    def test_pack_unpack_msg_type_preserved(self):
        client = _make_client_stub()
        pkt = self._make_packet(msg_type=MsgEnum.MSGACK)
        packed = pkt.pack(client)
        restored = AppPacket.unpack(packed)
        self.assertEqual(restored.msg_type, MsgEnum.MSGACK)

    def test_pack_unpack_probe(self):
        client = _make_client_stub()
        pkt = self._make_packet(msg="", msg_type=MsgEnum.PROBE)
        packed = pkt.pack(client)
        restored = AppPacket.unpack(packed)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.msg_type, MsgEnum.PROBE)

    def test_tampered_sig_returns_none(self):
        client = _make_client_stub()
        pkt = self._make_packet()
        packed = pkt.pack(client)
        # Corrupt the signature portion (bytes 33-97 in the binary layout).
        tampered = packed[:33] + (b"\x00" * 64) + packed[97:]
        self.assertIsNone(AppPacket.unpack(tampered))

    def test_wrong_public_key_returns_none(self):
        client1 = _make_client_stub()
        client2 = _make_client_stub()  # different keypair
        pkt = self._make_packet()
        packed = pkt.pack(client1)
        # Replace src_pk bytes with client2's compressed public key.
        tampered = client2.kp.compact_public_key + packed[33:]
        self.assertIsNone(AppPacket.unpack(tampered))

    def test_timestamp_stamped_once(self):
        client = _make_client_stub(get_time=lambda: 1234567890)
        pkt = self._make_packet()
        packed = pkt.pack(client)
        restored = AppPacket.unpack(packed)
        self.assertEqual(restored.timestamp, 1234567890)

    def test_timestamp_preserved_on_retransmit(self):
        client = _make_client_stub(get_time=lambda: 1000)
        pkt = self._make_packet()
        packed1 = pkt.pack(client)
        # Pretend time advanced but timestamp was already set
        client.get_time = lambda: 9999
        packed2 = pkt.pack(client)
        r1 = AppPacket.unpack(packed1)
        r2 = AppPacket.unpack(packed2)
        self.assertEqual(r1.timestamp, r2.timestamp)

    def test_seq_no_hex_format(self):
        pkt = self._make_packet(seq_no=255)
        self.assertEqual(pkt.seq_no_hex, "000000ff")

    def test_seq_no_hex_zero(self):
        pkt = self._make_packet(seq_no=0)
        self.assertEqual(pkt.seq_no_hex, "00000000")

    def test_timestamp_hex_format(self):
        pkt = self._make_packet()
        pkt.timestamp = 0x1122334455667788
        self.assertEqual(pkt.timestamp_hex, "1122334455667788")

    def test_queue_id_wrong_length_raises(self):
        client = _make_client_stub()
        pkt = AppPacket(
            queue_id_hex="tooshort",
            seq_no=1,
            msg_type=MsgEnum.MSG,
            msg="x",
        )
        with self.assertRaises(ValueError):
            pkt.pack(client)

    def test_src_pk_hex_in_packed_output(self):
        client = _make_client_stub()
        pkt = self._make_packet()
        packed = pkt.pack(client)
        self.assertIsInstance(packed, (bytes, bytearray))
        self.assertTrue(packed.startswith(client.kp.compact_public_key))

    def test_restored_src_pk_matches_signer(self):
        client = _make_client_stub()
        pkt = self._make_packet()
        packed = pkt.pack(client)
        restored = AppPacket.unpack(packed)
        self.assertEqual(restored.src_pk_hex, to_h(client.kp.compact_public_key))

    def test_empty_message_roundtrip(self):
        client = _make_client_stub()
        pkt = self._make_packet(msg="")
        packed = pkt.pack(client)
        restored = AppPacket.unpack(packed)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.msg, "")

    def test_malformed_payload_returns_none(self):
        self.assertIsNone(AppPacket.unpack(b"notvalid"))
        self.assertIsNone(AppPacket.unpack(b""))
        self.assertIsNone(AppPacket.unpack(b"x" * 10))
        # Non-bytes inputs are rejected as well.
        self.assertIsNone(AppPacket.unpack("notvalid"))


# ---------------------------------------------------------------------------
# Rendezvous / interleaving utilities
# ---------------------------------------------------------------------------


class TestInterleave(unittest.TestCase):
    def test_equal_buckets_interleaved(self):
        buckets = {
            IP4: [{"v": "a1"}, {"v": "a2"}],
            IP6: [{"v": "b1"}, {"v": "b2"}],
        }
        result = interleave_buckets(buckets)
        # First element from lower-sorted key (IP4=2, IP6=10) → IP4 first
        self.assertEqual(len(result), 4)

    def test_empty_buckets(self):
        result = interleave_buckets({IP4: [], IP6: []})
        self.assertEqual(result, [])

    def test_unequal_buckets(self):
        buckets = {
            IP4: [{"v": "a1"}, {"v": "a2"}, {"v": "a3"}],
            IP6: [{"v": "b1"}],
        }
        result = interleave_buckets(buckets)
        self.assertEqual(len(result), 4)

    def test_single_af(self):
        buckets = {IP4: [{"v": "a"}, {"v": "b"}]}
        result = interleave_buckets(buckets)
        self.assertEqual(len(result), 2)


class TestGetServerScore(unittest.TestCase):
    def test_same_inputs_same_score(self):
        kp = Signing.keypair()
        s1 = get_server_score(IP4, "server1.example.com", kp.public_key_hex)
        s2 = get_server_score(IP4, "server1.example.com", kp.public_key_hex)
        self.assertEqual(s1, s2)

    def test_different_hosts_different_scores(self):
        kp = Signing.keypair()
        s1 = get_server_score(IP4, "server1.example.com", kp.public_key_hex)
        s2 = get_server_score(IP4, "server2.example.com", kp.public_key_hex)
        self.assertNotEqual(s1, s2)

    def test_different_af_different_scores(self):
        kp = Signing.keypair()
        s1 = get_server_score(IP4, "host.example.com", kp.public_key_hex)
        s2 = get_server_score(IP6, "host.example.com", kp.public_key_hex)
        self.assertNotEqual(s1, s2)

    def test_different_keys_different_scores(self):
        kp1 = Signing.keypair()
        kp2 = Signing.keypair()
        s1 = get_server_score(IP4, "host.example.com", kp1.public_key_hex)
        s2 = get_server_score(IP4, "host.example.com", kp2.public_key_hex)
        self.assertNotEqual(s1, s2)

    def test_score_is_numeric(self):
        kp = Signing.keypair()
        score = get_server_score(IP4, "host.example.com", kp.public_key_hex)
        self.assertIsInstance(score, (int, float))


# ---------------------------------------------------------------------------
# Message queue iteration / pruning helpers
# ---------------------------------------------------------------------------


class TestIterAllMessages(unittest.TestCase):
    def _make_queue(self):
        return {
            MsgEnum.MSG: {},
            MsgEnum.MSGACK: {},
        }

    def test_empty_queues_yields_nothing(self):
        result = list(iter_all_messages(self._make_queue()))
        self.assertEqual(result, [])

    def test_single_message_returned(self):
        q = self._make_queue()
        q[MsgEnum.MSG]["queueabc"] = {1: "msg1"}
        result = list(iter_all_messages(q))
        self.assertEqual(result, ["msg1"])

    def test_messages_ordered_by_seq_no(self):
        q = self._make_queue()
        q[MsgEnum.MSG]["queueabc"] = {3: "msg3", 1: "msg1", 2: "msg2"}
        result = list(iter_all_messages(q))
        self.assertEqual(result, ["msg1", "msg2", "msg3"])

    def test_multiple_queues_all_returned(self):
        q = self._make_queue()
        q[MsgEnum.MSG]["q1"] = {1: "a"}
        q[MsgEnum.MSGACK]["q2"] = {1: "b"}
        result = list(iter_all_messages(q))
        self.assertIn("a", result)
        self.assertIn("b", result)


class TestPruneMsgIds(unittest.TestCase):
    def _make_client(self, republish_duration=60):
        class _Stub:
            pass

        c = _Stub()
        c.republish_duration = republish_duration
        c.recv_msg_ids = {}
        c.sent_msg_ids = {}
        return c

    def test_fresh_ids_not_pruned(self):
        client = self._make_client()
        now = 1000.0
        client.recv_msg_ids["msg1"] = now - 10
        prune_msg_ids(client, now)
        self.assertIn("msg1", client.recv_msg_ids)

    def test_expired_ids_pruned(self):
        client = self._make_client(republish_duration=60)
        now = 1000.0
        ttl = 60 * 2
        client.recv_msg_ids["old"] = now - ttl - 1
        prune_msg_ids(client, now)
        self.assertNotIn("old", client.recv_msg_ids)

    def test_sent_msg_ids_also_pruned(self):
        client = self._make_client(republish_duration=60)
        now = 1000.0
        client.sent_msg_ids["old"] = now - 200
        prune_msg_ids(client, now)
        self.assertNotIn("old", client.sent_msg_ids)

    def test_mix_pruned_and_kept(self):
        client = self._make_client(republish_duration=60)
        now = 1000.0
        client.recv_msg_ids["old"] = now - 200
        client.recv_msg_ids["fresh"] = now - 10
        prune_msg_ids(client, now)
        self.assertNotIn("old", client.recv_msg_ids)
        self.assertIn("fresh", client.recv_msg_ids)


class TestGetMsgFromQueue(unittest.TestCase):
    def _make_queue(self):
        return {
            MsgEnum.MSG: {},
            MsgEnum.MSGACK: {},
        }

    def test_existing_message_returned(self):
        class _Stub:
            msg_queues = {MsgEnum.MSGACK: {"queueabc": {1: "payload"}}}

        result = get_msg_from_queue(_Stub(), "queueabc", 1, MsgEnum.MSGACK)
        self.assertEqual(result, "payload")

    def test_missing_queue_id_returns_none(self):
        class _Stub:
            msg_queues = {MsgEnum.MSGACK: {}}

        self.assertIsNone(get_msg_from_queue(_Stub(), "nonexistent", 1, MsgEnum.MSGACK))

    def test_missing_seq_no_returns_none(self):
        class _Stub:
            msg_queues = {MsgEnum.MSGACK: {"q": {2: "x"}}}

        self.assertIsNone(get_msg_from_queue(_Stub(), "q", 999, MsgEnum.MSGACK))

    def test_found_message_returned(self):
        class _Stub:
            msg_queues = {MsgEnum.MSGACK: {"q": {1: "hello"}}}

        result = get_msg_from_queue(_Stub(), "q", 1, MsgEnum.MSGACK)
        self.assertEqual(result, "hello")


# ---------------------------------------------------------------------------
# SmartPipe unit tests
# ---------------------------------------------------------------------------


class TestSmartPipeInit(unittest.TestCase):
    def _make_router_stub(self):
        class _Router:
            pass
        return _Router()

    def test_init_stores_router_and_dest(self):
        from sidewire.smart_pipe import SmartPipe
        router = self._make_router_stub()
        sp = SmartPipe(router, "aabbcc")
        self.assertIs(sp.router, router)
        self.assertEqual(sp.dest_pub_hex, "aabbcc")

    def test_init_empty_clients_by_default(self):
        from sidewire.smart_pipe import SmartPipe
        sp = SmartPipe(self._make_router_stub(), "aabb")
        self.assertEqual(sp.clients, [])

    def test_init_accepts_pre_resolved_clients(self):
        from sidewire.smart_pipe import SmartPipe
        mock_clients = [object(), object()]
        sp = SmartPipe(self._make_router_stub(), "aabb", clients=mock_clients)
        self.assertEqual(sp.clients, mock_clients)


class TestSmartPipeSendAllFail(unittest.IsolatedAsyncioTestCase):
    async def test_send_returns_zero_when_all_tasks_timeout(self):
        """SmartPipe.send() returns 0 when every client times out."""
        import asyncio
        from sidewire.smart_pipe import SmartPipe

        class _FakeClient:
            def queue_msg(self, msg, dest, msg_id):
                fut = asyncio.get_event_loop().create_future()
                return None, fut  # future never resolves → timeout

            def dequeue_msg(self, msg_id):
                pass

        router = object()
        sp = SmartPipe(router, "dest_hex", clients=[_FakeClient()])
        result = await sp.send("hello", timeout=0)
        self.assertEqual(result, 0)

    async def test_send_returns_message_length_on_success(self):
        """SmartPipe.send() returns byte length when one client succeeds."""
        import asyncio
        from sidewire.smart_pipe import SmartPipe

        class _FakeClient:
            def queue_msg(self, msg, dest, msg_id):
                fut = asyncio.get_event_loop().create_future()
                fut.set_result(True)
                return None, fut

            def dequeue_msg(self, msg_id):
                pass

        router = object()
        sp = SmartPipe(router, "dest_hex", clients=[_FakeClient()])
        result = await sp.send("hello world", timeout=5)
        self.assertEqual(result, len("hello world"))


# ---------------------------------------------------------------------------
# Router cache_clients tests
# ---------------------------------------------------------------------------


class TestRouterCacheClients(unittest.TestCase):
    def _make_minimal_router(self):
        """Build a Router without network connections by bypassing __init__."""
        import time
        from sidewire.router import Router
        router = object.__new__(Router)
        router.cache = {}
        router.get_time = time.time
        return router

    def test_cache_clients_stores_entry(self):
        from sidewire.router import Router
        router = self._make_minimal_router()
        clients = [object()]
        router.cache_clients("pubkey_hex", clients)
        self.assertIn("pubkey_hex", router.cache)
        self.assertIs(router.cache["pubkey_hex"]["clients"], clients)

    def test_cache_clients_updates_timestamp(self):
        import time
        router = self._make_minimal_router()
        before = time.time()
        router.cache_clients("pubkey_hex", [])
        after = time.time()
        t = router.cache["pubkey_hex"]["updated"]
        self.assertGreaterEqual(t, before)
        self.assertLessEqual(t, after)

    def test_cache_clients_overwrites_old_entry(self):
        router = self._make_minimal_router()
        first = [object()]
        second = [object()]
        router.cache_clients("key", first)
        router.cache_clients("key", second)
        self.assertIs(router.cache["key"]["clients"], second)


if __name__ == "__main__":
    unittest.main()
