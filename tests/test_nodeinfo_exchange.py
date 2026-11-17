"""The app's 'exchange user info': reported as unsupported, with Meshpoint's NodeInfo broadcast as fallback."""

import asyncio
import time
import unittest

from meshtastic.protobuf import mesh_pb2, portnums_pb2

from tests.test_settings_and_access import FakeClient, bridge, routing_errors

MY_NUM = 0x12345678
OTHER = 0x11223344


def nodeinfo_request(want_ack=False, want_response=True):
    pkt = mesh_pb2.MeshPacket()
    pkt.id = 777
    pkt.to = OTHER
    pkt.want_ack = want_ack
    pkt.decoded.portnum = portnums_pb2.NODEINFO_APP
    pkt.decoded.want_response = want_response
    pkt.decoded.payload = mesh_pb2.User(long_name="Phone", short_name="PH").SerializeToString()
    return pkt


def run(pkt, status=200, last_tx=0.0, rx_only=False):
    b = bridge()
    b.my_num, b.rx_only = MY_NUM, rx_only
    b.last_nodeinfo_tx = last_tx
    calls = []

    async def call(method, path, **kw):
        calls.append((method, path))
        return status, "{}"
    b.call = call
    c = FakeClient()
    asyncio.run(b.handle_tx(c, pkt))
    return b, c, calls


class NodeInfoExchangeTests(unittest.TestCase):
    def test_app_told_unsupported_and_meshpoint_broadcasts(self):
        b, c, calls = run(nodeinfo_request())
        self.assertEqual(calls, [("POST", "/api/config/nodeinfo/send")])
        self.assertEqual(routing_errors(c), [mesh_pb2.Routing.BAD_REQUEST])
        self.assertGreater(b.last_nodeinfo_tx, 0)

    def test_reply_answers_the_request(self):
        _, c, _ = run(nodeinfo_request())
        rp = [fr.packet for fr in c.sent
              if fr.HasField("packet") and fr.packet.decoded.portnum == portnums_pb2.ROUTING_APP][0]
        self.assertEqual(rp.decoded.request_id, 777)
        self.assertEqual(getattr(rp, "from"), MY_NUM)

    def test_broadcast_not_repeated_within_five_minutes_but_still_unsupported(self):
        _, c, calls = run(nodeinfo_request(), last_tx=time.time() - 60)
        self.assertEqual(calls, [])
        self.assertEqual(routing_errors(c), [mesh_pb2.Routing.BAD_REQUEST])

    def test_failed_broadcast_still_reports_unsupported(self):
        with self.assertLogs("bridge", "WARNING"):
            b, c, _ = run(nodeinfo_request(), status=500)
        self.assertEqual(routing_errors(c), [mesh_pb2.Routing.BAD_REQUEST])
        self.assertEqual(b.last_nodeinfo_tx, 0)

    def test_want_ack_alone_also_answered(self):
        _, c, _ = run(nodeinfo_request(want_ack=True, want_response=False))
        self.assertEqual(routing_errors(c), [mesh_pb2.Routing.BAD_REQUEST])

    def test_no_reply_when_nothing_requested(self):
        _, c, calls = run(nodeinfo_request(want_ack=False, want_response=False))
        self.assertEqual(routing_errors(c), [])
        self.assertEqual(len(calls), 1)

    def test_receive_only_never_broadcasts(self):
        _, c, calls = run(nodeinfo_request(), rx_only=True)
        self.assertEqual(calls, [])
        self.assertEqual(routing_errors(c), [mesh_pb2.Routing.NO_INTERFACE])


if __name__ == "__main__":
    unittest.main()
