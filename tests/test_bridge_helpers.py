# SPDX-License-Identifier: AGPL-3.0-only
import os
import tempfile
import unittest
from types import SimpleNamespace

from meshpoint_bridge import (
    BROADCAST, Bridge, mac_node_num, resolve_schema, signature, to_epoch, valid_position,
)


class PositionTests(unittest.TestCase):
    def test_zero_zero_means_not_set(self):
        self.assertIsNone(valid_position(0, 0))
        self.assertIsNone(valid_position(0.0, 0.0))
        self.assertIsNone(valid_position("0", "0"))

    def test_missing_or_invalid_means_not_set(self):
        self.assertIsNone(valid_position(None, 10.0))
        self.assertIsNone(valid_position(10.0, None))
        self.assertIsNone(valid_position("abc", 1))
        self.assertIsNone(valid_position(float("nan"), 1))
        self.assertIsNone(valid_position(91, 0))
        self.assertIsNone(valid_position(0, 181))

    def test_real_positions_are_kept(self):
        self.assertEqual(valid_position(40.5, -111.9), (40.5, -111.9))
        self.assertEqual(valid_position(0, 12.5), (0.0, 12.5))  # only the exact 0,0 pair is rejected
        self.assertEqual(valid_position("40.5", "-111.9"), (40.5, -111.9))


class TimestampTests(unittest.TestCase):
    def test_naive_timestamp_is_utc(self):
        self.assertEqual(to_epoch("2026-11-10T08:40:14"), 1794300014)

    def test_zone_aware_timestamps(self):
        self.assertEqual(to_epoch("2026-11-10T08:40:14+00:00"), 1794300014)
        self.assertEqual(to_epoch("2026-11-10T08:40:14Z"), 1794300014)
        self.assertEqual(to_epoch("2026-11-10T01:40:14-07:00"), 1794300014)

    def test_numbers_and_junk(self):
        self.assertEqual(to_epoch(1794300014.9), 1794300014)
        self.assertEqual(to_epoch(None), 0)
        self.assertEqual(to_epoch("not a date"), 0)


class MacNodeNumTests(unittest.TestCase):
    def make_tree(self, ifaces):
        """ifaces: {name: (mac, kind)} with kind 'wired', 'wifi' or 'virtual'."""
        root = tempfile.mkdtemp()
        net = os.path.join(root, "class", "net")
        os.makedirs(net)
        for name, (mac, kind) in ifaces.items():
            if kind == "virtual":
                real = os.path.join(root, "devices", "virtual", "net", name)
            else:
                real = os.path.join(root, "devices", "platform", "net", name)
            os.makedirs(real)
            with open(os.path.join(real, "address"), "w") as f:
                f.write(mac + "\n")
            if kind == "wifi":
                os.makedirs(os.path.join(real, "wireless"))
            os.symlink(real, os.path.join(net, name))
        return net

    def test_ethernet_preferred_over_wifi(self):
        net = self.make_tree({"wlan0": ("aa:bb:cc:11:22:33", "wifi"),
                              "eth0": ("de:ad:be:ef:00:01", "wired")})
        self.assertEqual(mac_node_num(net), 0xBEEF0001)

    def test_wifi_used_when_no_ethernet(self):
        net = self.make_tree({"wlan0": ("aa:bb:cc:11:22:33", "wifi")})
        self.assertEqual(mac_node_num(net), 0xCC112233)

    def test_virtual_and_loopback_ignored(self):
        net = self.make_tree({"lo": ("00:00:00:00:00:00", "virtual"),
                              "docker0": ("02:42:ac:11:00:02", "virtual")})
        self.assertIsNone(mac_node_num(net))

    def test_missing_directory(self):
        self.assertIsNone(mac_node_num("/nonexistent/net"))


class BridgeStateTests(unittest.TestCase):
    def setUp(self):
        self.bridge = Bridge(SimpleNamespace(node_id=None, meshpoint="127.0.0.1:8080"))

    def test_seen_before_remembers_and_evicts_oldest(self):
        b = self.bridge
        self.assertFalse(b.seen_before("a"))
        self.assertTrue(b.seen_before("a"))
        for i in range(b.seen.maxlen):
            b.seen_before(f"id{i}")
        self.assertFalse(b.seen_before("a"))  # "a" was evicted, so it counts as new again
        self.assertEqual(len(b.seen), len(b.seen_set))

    def test_ack_for_targets_the_phone_packet(self):
        b = self.bridge
        b.my_num = 0x1234ABCD
        pkt = SimpleNamespace(id=777)
        ack = b.ack_for(pkt)
        self.assertEqual(ack.decoded.request_id, 777)
        self.assertEqual(ack.to, 0x1234ABCD)
        self.assertNotEqual(ack.to, BROADCAST)


class SmallHelperTests(unittest.TestCase):
    def test_signature_detects_changes(self):
        keys = ("a", "b")
        self.assertEqual(signature({"a": 1, "b": 2, "c": 9}, keys), signature({"a": 1, "b": 2}, keys))
        self.assertNotEqual(signature({"a": 1, "b": 2}, keys), signature({"a": 1, "b": 3}, keys))

    def test_resolve_schema_follows_refs_inside_lists(self):
        spec = {"components": {"schemas": {"Dest": {"type": "string", "enum": ["x"]}}}}
        out = resolve_schema(spec, {"anyOf": [{"$ref": "#/components/schemas/Dest"}, {"type": "null"}]})
        self.assertEqual(out["anyOf"][0], {"$": "Dest", "type": "string", "enum": ["x"]})
        self.assertEqual(out["anyOf"][1], {"type": "null"})


if __name__ == "__main__":
    unittest.main()
