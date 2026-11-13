# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the settings file, access controls, node identity / RX-only mode and log privacy."""
import asyncio
import ipaddress
import json
import logging
import os
import stat
import tempfile
import unittest
from types import SimpleNamespace

from meshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2

import meshpoint_bridge as mb
from meshpoint_bridge import (
    EXIT_CONFIG, EXIT_RESTART, Bridge, ConfigError, format_settings, ip_allowed, load_settings,
    parse_allowlist, quote_env, read_env_file, resolve_identity, resolve_settings, unquote_env,
    write_env_file,
)


def make_args(**kw):
    """Command-line args as main() would produce them before settings are applied."""
    base = dict(config=None, meshpoint="127.0.0.1:8080", username=None, password=None, listen_host=None,
                listen_port=None, allow=None, mdns=None, advertise_ip=None, rx_only=None, node_id=None,
                probe=False)
    base.update(kw)
    return SimpleNamespace(**base)


def bridge(**kw):
    args = dict(node_id=None, meshpoint="127.0.0.1:8080", rx_only=False, allow_nets=[])
    args.update(kw)
    return Bridge(SimpleNamespace(**args))


class FakeClient:
    def __init__(self):
        self.sent = []
        self.ready = True
        self.closed = False
        self.peer = ("192.168.1.20", 50000)

    async def send(self, fr):
        self.sent.append(fr)

    def try_send(self, fr):
        self.sent.append(fr)
        return True

    def close(self):
        self.closed = True


class FakeWriter:
    def __init__(self, ip):
        self.ip = ip
        self.closed = False

    def get_extra_info(self, name):
        return (self.ip, 50000) if name == "peername" else None

    def close(self):
        self.closed = True


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "bridge.env")

    def tearDown(self):
        self.dir.cleanup()

    def test_read_ignores_comments_and_handles_quotes(self):
        with open(self.path, "w") as f:
            f.write('# comment\n\n; also a comment\nexport A=1\nB = two words \nC="q \\" x"\nD=\'single $x\'\nE=\n')
        self.assertEqual(read_env_file(self.path),
                         {"A": "1", "B": "two words", "C": 'q " x', "D": "single $x", "E": ""})

    def test_bad_line_is_a_config_error(self):
        with open(self.path, "w") as f:
            f.write("not a setting\n")
        with self.assertRaises(ConfigError) as cm:
            read_env_file(self.path)
        self.assertEqual(cm.exception.code, EXIT_CONFIG)

    def test_quoting_round_trips_awkward_passwords(self):
        for value in ["simple", "", "with space", 'qu"ote', "back\\slash", "#hash", "$dollar", "it's", " pad "]:
            self.assertEqual(unquote_env(quote_env(value)), value, value)
        with self.assertRaises(ValueError):
            quote_env("two\nlines")

    def test_write_is_private_and_round_trips(self):
        values = {"MESHPOINT_USERNAME": "admin", "MESHPOINT_PASSWORD": 'p@ss "w#rd"', "BRIDGE_ALLOW": "",
                  "CUSTOM_KEY": "kept"}
        write_env_file(self.path, values)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(read_env_file(self.path), values)
        self.assertEqual([n for n in os.listdir(self.dir.name) if n.startswith(".bridge-env-")], [])


class SettingsPrecedenceTests(unittest.TestCase):
    def test_command_line_then_environment_then_file_then_default(self):
        args = make_args(listen_port=5000)
        file_values = {"BRIDGE_LISTEN_PORT": "4500", "BRIDGE_LISTEN_HOST": "192.168.1.2",
                       "MESHPOINT_USERNAME": "fromfile", "BRIDGE_MDNS": "0"}
        environ = {"MESHPOINT_USERNAME": "fromenv", "BRIDGE_LISTEN_HOST": ""}
        resolve_settings(args, file_values, environ, file_label="bridge.env")
        self.assertEqual(args.listen_port, 5000)
        self.assertEqual(args.username, "fromenv")
        self.assertEqual(args.listen_host, "192.168.1.2")  # empty environment value does not override
        self.assertIs(args.mdns, False)
        self.assertIs(args.rx_only, False)
        self.assertEqual(args.setting_sources["listen_port"], "command line")
        self.assertEqual(args.setting_sources["username"], "environment")
        self.assertEqual(args.setting_sources["listen_host"], "bridge.env")
        self.assertEqual(args.setting_sources["rx_only"], "default")

    def test_bad_values_are_config_errors(self):
        with self.assertRaises(ConfigError):
            resolve_settings(make_args(), {"BRIDGE_MDNS": "maybe"}, {})
        with self.assertRaises(ConfigError):
            resolve_settings(make_args(), {"BRIDGE_LISTEN_PORT": "port"}, {})

    def test_load_settings_validates(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bridge.env")
            cases = {"BRIDGE_LISTEN_HOST": "not-an-ip", "BRIDGE_ALLOW": "192.168.1.0/24, nonsense",
                     "BRIDGE_NODE_ID": "!zzzz", "BRIDGE_ADVERTISE_IP": "::1", "BRIDGE_LISTEN_PORT": "70000"}
            for key, bad in cases.items():
                write_env_file(path, {key: bad})
                with self.assertRaises(ConfigError, msg=key):
                    load_settings(make_args(config=path), environ={})
            write_env_file(path, {"BRIDGE_ALLOW": "192.168.1.0/24,10.0.0.5", "BRIDGE_RX_ONLY": "yes"})
            args = load_settings(make_args(config=path), environ={})
            self.assertEqual(args.allow_nets, [ipaddress.ip_network("192.168.1.0/24"),
                                               ipaddress.ip_network("10.0.0.5/32")])
            self.assertTrue(args.rx_only)
            self.assertEqual(args.config_path, path)

    def test_missing_explicit_config_is_an_error(self):
        with self.assertRaises(ConfigError):
            load_settings(make_args(config="/nonexistent/bridge.env"), environ={})

    def test_show_config_hides_the_password(self):
        args = resolve_settings(make_args(password="hunter2"), {}, {})
        args.config_path = None
        text = format_settings(args)
        self.assertNotIn("hunter2", text)
        self.assertIn("(set)", text)


class AllowlistTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_allowlist(""), [])
        self.assertEqual(len(parse_allowlist("192.168.1.0/24, 10.0.0.5  fd00::/8")), 3)
        with self.assertRaises(ConfigError):
            parse_allowlist("192.168.1.300")

    def test_matching(self):
        nets = parse_allowlist("192.168.1.0/24,10.0.0.5,fd00::/8")
        self.assertTrue(ip_allowed("192.168.1.77", nets))
        self.assertTrue(ip_allowed("10.0.0.5", nets))
        self.assertFalse(ip_allowed("10.0.0.6", nets))
        self.assertTrue(ip_allowed("::ffff:192.168.1.9", nets))  # IPv4 client on a dual-stack socket
        self.assertTrue(ip_allowed("fd00::1", nets))
        self.assertFalse(ip_allowed("2001:db8::1", nets))
        self.assertFalse(ip_allowed("garbage", nets))

    def test_empty_list_allows_everyone(self):
        self.assertTrue(ip_allowed("203.0.113.9", []))

    def test_refused_client_does_not_replace_the_current_phone(self):
        b = bridge(allow_nets=parse_allowlist("192.168.1.0/24"))
        current = FakeClient()
        b.clients.add(current)
        writer = FakeWriter("10.9.9.9")
        with self.assertLogs("bridge", logging.WARNING):
            asyncio.run(b.handle_client(None, writer))
        self.assertTrue(writer.closed)
        self.assertIn(current, b.clients)
        self.assertFalse(current.closed)


class IdentityTests(unittest.TestCase):
    def no_mac(self):
        return None

    def test_meshpoint_id_is_used(self):
        self.assertEqual(resolve_identity({"enabled": True, "node_id_hex": "!a1b2c3d4"}, mac_fn=self.no_mac),
                         (0xA1B2C3D4, False, "Meshpoint"))

    def test_mismatched_bridge_node_id_is_ignored_with_an_error(self):
        with self.assertLogs("bridge", logging.ERROR):
            num, rx_only, _ = resolve_identity({"node_id_hex": "!a1b2c3d4"}, "!11112222", mac_fn=self.no_mac)
        self.assertEqual((num, rx_only), (0xA1B2C3D4, False))

    def test_transmit_disabled_means_rx_only_with_meshpoint_id(self):
        self.assertEqual(resolve_identity({"enabled": False, "node_id_hex": "!a1b2c3d4"}, mac_fn=self.no_mac),
                         (0xA1B2C3D4, True, "Meshpoint"))

    def test_forced_rx_only(self):
        num, rx_only, _ = resolve_identity({"node_id_hex": "!a1b2c3d4"}, force_rx_only=True, mac_fn=self.no_mac)
        self.assertEqual((num, rx_only), (0xA1B2C3D4, True))

    def test_no_meshpoint_id_uses_bridge_node_id_then_mac(self):
        unset = {"enabled": True, "node_id": 0, "node_id_hex": "", "node_id_source": "unset"}
        self.assertEqual(resolve_identity(unset, "!0000beef", mac_fn=self.no_mac), (0xBEEF, True, "BRIDGE_NODE_ID"))
        self.assertEqual(resolve_identity(unset, None, mac_fn=lambda: 0x12345678),
                         (0x12345678, True, "MAC address"))

    def test_no_id_anywhere_fails_instead_of_inventing_one(self):
        with self.assertRaises(ConfigError) as cm:
            resolve_identity({"node_id_hex": ""}, None, mac_fn=self.no_mac)
        self.assertEqual(cm.exception.code, EXIT_CONFIG)

    def test_identity_change_requests_a_restart(self):
        b = bridge()
        b.my_num, b.rx_only = 0xA1B2C3D4, False
        cancelled = []
        b.main_task = SimpleNamespace(cancel=lambda: cancelled.append(True))

        async def get_json(path):
            return {"transmit": {"enabled": True, "node_id_hex": "!99998888"}}
        b.get_json = get_json
        with self.assertLogs("bridge", logging.ERROR):
            asyncio.run(b.check_identity())
        self.assertEqual(b.exit_code, EXIT_RESTART)
        self.assertTrue(cancelled)

    def test_unchanged_identity_keeps_running(self):
        b = bridge()
        b.my_num, b.rx_only = 0xA1B2C3D4, False

        async def get_json(path):
            return {"transmit": {"enabled": True, "node_id_hex": "!a1b2c3d4"}}
        b.get_json = get_json
        asyncio.run(b.check_identity())
        self.assertEqual(b.exit_code, 0)


def text_packet(text, to=mb.BROADCAST, want_ack=True):
    pkt = mesh_pb2.MeshPacket()
    pkt.id = 4242
    pkt.to = to
    pkt.want_ack = want_ack
    pkt.decoded.portnum = portnums_pb2.TEXT_MESSAGE_APP
    pkt.decoded.payload = text.encode()
    return pkt


def routing_errors(client):
    errors = []
    for fr in client.sent:
        if fr.HasField("packet") and fr.packet.decoded.portnum == portnums_pb2.ROUTING_APP:
            r = mesh_pb2.Routing()
            r.ParseFromString(fr.packet.decoded.payload)
            errors.append(r.error_reason)
    return errors


class RxOnlyTests(unittest.TestCase):
    def test_app_is_told_transmit_is_off(self):
        b = bridge()
        self.assertTrue(b.make_config("lora").lora.tx_enabled)
        b.rx_only = True
        self.assertFalse(b.make_config("lora").lora.tx_enabled)

    def test_sends_are_rejected_and_never_reach_meshpoint(self):
        b = bridge()
        b.my_num, b.rx_only = 0x12345678, True
        calls = []

        async def call(*a, **kw):
            calls.append(a)
            return 200, "{}"
        b.call = call
        c = FakeClient()
        asyncio.run(b.handle_tx(c, text_packet("hello mesh")))
        self.assertEqual(calls, [])
        self.assertIn(mesh_pb2.Routing.NO_INTERFACE, routing_errors(c))

    def test_admin_requests_still_answered(self):
        b = bridge()
        b.my_num, b.rx_only = 0x12345678, True
        am = admin_pb2.AdminMessage()
        am.get_owner_request = True
        pkt = mesh_pb2.MeshPacket()
        pkt.id = 99
        pkt.to = b.my_num
        pkt.decoded.portnum = portnums_pb2.ADMIN_APP
        pkt.decoded.payload = am.SerializeToString()
        c = FakeClient()
        asyncio.run(b.handle_tx(c, pkt))
        replies = [fr for fr in c.sent if fr.HasField("packet") and fr.packet.decoded.portnum == portnums_pb2.ADMIN_APP]
        self.assertEqual(len(replies), 1)


class LogPrivacyTests(unittest.TestCase):
    SECRET = "meet at the trailhead 7pm"

    def test_sent_text_not_in_info_logs(self):
        b = bridge()
        b.my_num = 0x12345678

        async def call(*a, **kw):
            return 200, json.dumps({"packet_id": "0000abcd"})
        b.call = call
        with self.assertLogs("bridge", logging.INFO) as logs:
            asyncio.run(b.handle_tx(FakeClient(), text_packet(self.SECRET, want_ack=False)))
        joined = "\n".join(logs.output)
        self.assertIn("TX text", joined)
        self.assertNotIn(self.SECRET, joined)

    def test_received_text_not_in_info_logs(self):
        b = bridge()
        b.my_num = 0x12345678
        ev = {"type": "packet", "data": {"protocol": "meshtastic", "packet_type": "text",
                                         "packet_id": "0000d00d", "source_id": "!0badc0de",
                                         "destination_id": "^all", "channel": 0,
                                         "decoded_payload": {"text": self.SECRET}}}
        with self.assertLogs("bridge", logging.INFO) as logs:
            asyncio.run(b.on_event(ev))
        joined = "\n".join(logs.output)
        self.assertIn("RX text", joined)
        self.assertNotIn(self.SECRET, joined)


if __name__ == "__main__":
    unittest.main()
