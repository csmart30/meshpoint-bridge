# SPDX-License-Identifier: AGPL-3.0-only
import unittest
from types import SimpleNamespace

from meshtastic.protobuf import portnums_pb2, telemetry_pb2

from meshpoint_bridge import Bridge


class MeshpointNodeMetricsTests(unittest.TestCase):
    def setUp(self):
        self.bridge = Bridge(SimpleNamespace(node_id=None, meshpoint="127.0.0.1:8080"))

    def test_nodeinfo_maps_hops_and_device_metrics(self):
        node = self.bridge.nodeinfo(0x12345678, {
            "node_id": "12345678",
            "long_name": "Test Node",
            "short_name": "TEST",
            "last_heard": "2026-11-10T08:40:14.538387+00:00",
            "latest_hops": 3,
            "latest_snr": 7.5,
            "latest_battery": 98.0,
            "latest_voltage": 4.16,
            "latest_channel_util": 15.27,
            "latest_air_util": 0.48,
        })

        self.assertEqual(node.hops_away, 3)
        self.assertAlmostEqual(node.snr, 7.5)
        self.assertEqual(node.device_metrics.battery_level, 98)
        self.assertAlmostEqual(node.device_metrics.voltage, 4.16, places=2)
        self.assertAlmostEqual(node.device_metrics.channel_utilization, 15.27, places=2)
        self.assertAlmostEqual(node.device_metrics.air_util_tx, 0.48, places=2)

    def test_missing_metrics_are_not_fabricated(self):
        node = self.bridge.nodeinfo(0x12345678, {"long_name": "No Metrics"})

        self.assertFalse(node.HasField("device_metrics"))
        self.assertFalse(node.HasField("hops_away"))

    def test_environment_metrics_are_forwarded_as_telemetry_packet(self):
        packet = self.bridge.environment_telemetry_packet(0x12345678, {
            "last_heard": "2026-11-10T08:40:14+00:00",
            "latest_temperature": 21.5,
            "latest_humidity": 45.0,
            "latest_rssi": -88.4,
            "latest_snr": 7.5,
        })

        self.assertIsNotNone(packet)
        self.assertEqual(packet.decoded.portnum, portnums_pb2.TELEMETRY_APP)
        telemetry = telemetry_pb2.Telemetry()
        telemetry.ParseFromString(packet.decoded.payload)
        self.assertEqual(telemetry.WhichOneof("variant"), "environment_metrics")
        self.assertAlmostEqual(telemetry.environment_metrics.temperature, 21.5)
        self.assertAlmostEqual(telemetry.environment_metrics.relative_humidity, 45.0)
        self.assertEqual(packet.rx_rssi, -88)
        self.assertAlmostEqual(packet.rx_snr, 7.5)

    def test_no_environment_metrics_means_no_environment_packet(self):
        self.assertIsNone(self.bridge.environment_telemetry_packet(0x12345678, {
            "latest_temperature": None,
            "latest_humidity": None,
        }))


if __name__ == "__main__":
    unittest.main()
