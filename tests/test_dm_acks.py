# SPDX-License-Identifier: AGPL-3.0-only
"""DM acknowledgement handling: acks that arrive early, twice, or late, and DMs without a channel hash."""
import asyncio
import json
import logging
import unittest
from types import SimpleNamespace

from meshtastic.protobuf import mesh_pb2, portnums_pb2

from meshpoint_bridge import Bridge

ME = 0x12345678
PEER = 0x0BADC0DE
MP_ID = 0x9E79      # packet id Meshpoint assigns to the DM
PHONE_ID = 4242     # packet id the app gave the DM


class FakeClient:
    def __init__(self):
        self.sent = []
        self.ready = True
        self.peer = ("192.168.1.20", 50000)

    async def send(self, fr):
        self.sent.append(fr)

    def try_send(self, fr):
        self.sent.append(fr)
        return True

    def close(self):
        pass


def make_bridge():
    b = Bridge(SimpleNamespace(node_id=None, meshpoint="127.0.0.1:8080", rx_only=False, allow_nets=[]))
    b.my_num = ME
    b.client = FakeClient()
    b.clients.add(b.client)

    async def call(*a, **kw):
        return 200, json.dumps({"success": True, "packet_id": "%08x" % MP_ID})
    b.call = call
    return b


def dm_packet():
    pkt = mesh_pb2.MeshPacket()
    pkt.id = PHONE_ID
    pkt.to = PEER
    pkt.want_ack = True
    pkt.decoded.portnum = portnums_pb2.TEXT_MESSAGE_APP
    pkt.decoded.payload = b"Hi"
    return pkt


def routing_event(req, err=0):
    d = {"source_id": "!%08x" % PEER, "destination_id": "!%08x" % ME}
    return d, {"request_id": req, "error_reason": err}


def confirmations(client):
    """(request_id, error) of every routing packet the app was sent from the DM's recipient."""
    out = []
    for fr in client.sent:
        p = fr.packet if fr.HasField("packet") else None
        if p is not None and p.decoded.portnum == portnums_pb2.ROUTING_APP and getattr(p, "from") == PEER:
            r = mesh_pb2.Routing()
            r.ParseFromString(p.decoded.payload)
            out.append((p.decoded.request_id, r.error_reason))
    return out


class DmAckTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_ack_after_send(self):
        b = make_bridge()
        await b.handle_tx(b.client, dm_packet())
        self.assertIn(MP_ID, b.pending)
        await b.on_routing_event(*routing_event(MP_ID))
        self.assertEqual(confirmations(b.client), [(PHONE_ID, 0)])
        self.assertNotIn(MP_ID, b.pending)

    async def test_ack_before_send_returns_is_not_lost(self):
        b = make_bridge()
        await b.on_routing_event(*routing_event(MP_ID))  # the recipient answered first
        await b.handle_tx(b.client, dm_packet())
        self.assertEqual(confirmations(b.client), [(PHONE_ID, 0)])
        self.assertNotIn(MP_ID, b.pending)  # nothing left waiting, so no 60 s timeout either
        self.assertEqual(b.early_acks, {})

    async def test_early_failure_is_passed_on(self):
        b = make_bridge()
        await b.on_routing_event(*routing_event(MP_ID, err=35))  # PKI_UNKNOWN_PUBKEY
        await b.handle_tx(b.client, dm_packet())
        self.assertEqual(confirmations(b.client), [(PHONE_ID, 35)])

    async def test_duplicate_ack_is_quiet(self):
        b = make_bridge()
        await b.handle_tx(b.client, dm_packet())
        await b.on_routing_event(*routing_event(MP_ID))
        with self.assertNoLogs("bridge", logging.INFO):
            await b.on_routing_event(*routing_event(MP_ID))  # the same ack heard again via a relay
        self.assertEqual(len(confirmations(b.client)), 1)

    async def test_late_ack_after_timeout_still_reaches_the_app(self):
        b = make_bridge()
        await b.handle_tx(b.client, dm_packet())
        await b.ack_timeout(MP_ID, seconds=0)
        with self.assertLogs("bridge", logging.INFO) as logs:
            await b.on_routing_event(*routing_event(MP_ID))
        self.assertIn("late confirmation", "\n".join(logs.output))
        self.assertEqual(confirmations(b.client), [(PHONE_ID, 0)])

    async def test_stale_early_acks_expire(self):
        b = make_bridge()
        await b.on_routing_event(*routing_event(MP_ID))
        src, err, t = b.early_acks[MP_ID]
        b.early_acks[MP_ID] = (src, err, t - 3600)
        self.assertIsNone(b.take_early_ack(MP_ID))


class DmChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_dm_without_channel_hash_is_not_a_warning(self):
        b = make_bridge()
        b.hash_to_idx = {0xF3: 0, 0x1F: 1}
        ev = {"type": "packet", "data": {"protocol": "meshtastic", "packet_type": "text", "packet_id": "0000d00d",
                                         "source_id": "!%08x" % PEER, "destination_id": "!%08x" % ME,
                                         "channel_hash": 0, "decoded_payload": {"text": "hi back"}}}
        with self.assertNoLogs("bridge", logging.WARNING):
            await b.on_event(ev)
        packets = [fr.packet for fr in b.client.sent if fr.HasField("packet")]
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0].to, ME)

    async def test_broadcast_on_unknown_channel_still_warns(self):
        b = make_bridge()
        b.hash_to_idx = {0xF3: 0}
        ev = {"type": "packet", "data": {"protocol": "meshtastic", "packet_type": "text", "packet_id": "0000d00e",
                                         "source_id": "!%08x" % PEER, "destination_id": "^all",
                                         "channel_hash": 0x55, "decoded_payload": {"text": "hello"}}}
        with self.assertLogs("bridge", logging.WARNING):
            await b.on_event(ev)


if __name__ == "__main__":
    unittest.main()
