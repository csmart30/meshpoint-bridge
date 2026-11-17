"""Admin requests from the app: reads are answered, changes the bridge does not apply are refused."""

import asyncio
import logging
import unittest

from meshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2

from tests.test_settings_and_access import FakeClient, bridge, routing_errors

MY_NUM = 0x12345678


def admin_packet(set_field, want_ack=True, want_response=False):
    am = admin_pb2.AdminMessage()
    set_field(am)
    pkt = mesh_pb2.MeshPacket()
    pkt.id = 4242
    pkt.to = MY_NUM
    pkt.want_ack = want_ack
    pkt.decoded.portnum = portnums_pb2.ADMIN_APP
    pkt.decoded.want_response = want_response
    pkt.decoded.payload = am.SerializeToString()
    return pkt


def send(pkt, rx_only=False):
    b = bridge()
    b.my_num, b.rx_only = MY_NUM, rx_only
    c = FakeClient()
    asyncio.run(b.handle_tx(c, pkt))
    return c


def routing_packets(client):
    return [fr.packet for fr in client.sent
            if fr.HasField("packet") and fr.packet.decoded.portnum == portnums_pb2.ROUTING_APP]


def set_config(am):
    am.set_config.lora.hop_limit = 3


def set_owner(am):
    am.set_owner.long_name = "New name"


def set_time_only(am):
    am.set_time_only = 1794897338


def add_contact(am):
    am.add_contact.node_num = 0x11223344


def set_favorite(am):
    am.set_favorite_node = 0x11223344


def remove_favorite(am):
    am.remove_favorite_node = 0x11223344


def begin_edit(am):
    am.begin_edit_settings = True


def owner_request(am):
    am.get_owner_request = True


def module_config_request(am):
    am.get_module_config_request = admin_pb2.AdminMessage.MQTT_CONFIG


CHANGES = [set_config, set_owner, set_time_only, add_contact, set_favorite, remove_favorite, begin_edit]


class AdminChangeTests(unittest.TestCase):
    def test_changes_are_refused_with_not_authorized(self):
        for change in CHANGES:
            with self.subTest(change.__name__):
                c = send(admin_packet(change))
                self.assertEqual(routing_errors(c), [mesh_pb2.Routing.NOT_AUTHORIZED])
                rp = routing_packets(c)[0]
                self.assertEqual(rp.decoded.request_id, 4242)
                self.assertEqual(getattr(rp, "from"), MY_NUM)

    def test_refused_when_only_a_response_is_requested(self):
        c = send(admin_packet(set_owner, want_ack=False, want_response=True))
        self.assertEqual(routing_errors(c), [mesh_pb2.Routing.NOT_AUTHORIZED])

    def test_no_reply_when_neither_ack_nor_response_requested(self):
        c = send(admin_packet(set_owner, want_ack=False))
        self.assertEqual(routing_errors(c), [])

    def test_refused_in_receive_only_mode_too(self):
        c = send(admin_packet(set_favorite), rx_only=True)
        self.assertEqual(routing_errors(c), [mesh_pb2.Routing.NOT_AUTHORIZED])

    def test_refusal_is_logged(self):
        with self.assertLogs("bridge", logging.INFO) as logs:
            send(admin_packet(set_owner))
        self.assertIn("set_owner refused (NOT_AUTHORIZED)", "\n".join(logs.output))

    def test_time_sync_refusal_not_logged_at_info(self):
        b = bridge()
        b.my_num = MY_NUM
        logger = logging.getLogger("bridge")
        with self.assertLogs("bridge", logging.DEBUG) as logs:
            old = logger.level
            logger.setLevel(logging.DEBUG)
            try:
                asyncio.run(b.handle_tx(FakeClient(), admin_packet(set_time_only)))
            finally:
                logger.setLevel(old)
        refusals = [r for r in logs.records if "set_time_only refused" in r.getMessage()]
        self.assertEqual([r.levelno for r in refusals], [logging.DEBUG])


class AdminReadTests(unittest.TestCase):
    def test_supported_read_is_answered_without_routing_error(self):
        c = send(admin_packet(owner_request))
        self.assertEqual(routing_errors(c), [])
        replies = [fr.packet for fr in c.sent
                   if fr.HasField("packet") and fr.packet.decoded.portnum == portnums_pb2.ADMIN_APP]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].decoded.request_id, 4242)

    def test_unsupported_read_still_gets_plain_ack(self):
        c = send(admin_packet(module_config_request))
        self.assertEqual(routing_errors(c), [mesh_pb2.Routing.NONE])


if __name__ == "__main__":
    unittest.main()
