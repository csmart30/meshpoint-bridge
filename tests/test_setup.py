# SPDX-License-Identifier: AGPL-3.0-only
"""--setup: shows Meshpoint's node ID instead of asking for one, and asks only when Meshpoint has none."""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

import meshpoint_bridge as mb
from meshpoint_bridge import meshpoint_identity, read_env_file, write_env_file

WITH_ID = {"transmit": {"enabled": True, "node_id_hex": "!a1b2c3d4", "long_name": "Test Node", "short_name": "TST"}}
TX_OFF = {"transmit": {"enabled": False, "node_id_hex": "!a1b2c3d4", "long_name": "Test Node", "short_name": "TST"}}
NO_ID = {"transmit": {"enabled": True, "node_id": 0, "node_id_hex": ""}}


class IdentityTests(unittest.TestCase):
    def test_reads_meshpoint_identity(self):
        self.assertEqual(meshpoint_identity(WITH_ID), (0xA1B2C3D4, "Test Node", "TST", True))
        self.assertEqual(meshpoint_identity(TX_OFF)[3], False)
        self.assertIsNone(meshpoint_identity(NO_ID)[0])
        self.assertIsNone(meshpoint_identity(None)[0])


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "bridge.env")

    def tearDown(self):
        self.dir.cleanup()

    def run_setup(self, cfg, answers, mac=0x00C0FFEE, login=True):
        """Run --setup with scripted answers; returns (saved settings, printed text, prompts asked)."""
        prompts = []
        replies = iter(answers)

        def fake_input(prompt):
            prompts.append(prompt)
            return next(replies)

        async def fake_check_login(*a):
            return login, cfg
        out = io.StringIO()
        with mock.patch("builtins.input", fake_input), mock.patch("getpass.getpass", lambda p: "pw"), \
                mock.patch.object(mb, "check_login", fake_check_login), \
                mock.patch.object(mb, "mac_node_num", lambda: mac), \
                mock.patch.object(sys.stdin, "isatty", lambda: True), redirect_stdout(out):
            mb.run_setup(SimpleNamespace(config=self.path, meshpoint="127.0.0.1:8080"))
        return read_env_file(self.path), out.getvalue(), prompts

    def test_shows_meshpoint_node_id_and_does_not_ask_for_one(self):
        # username, listen address, allowlist, mDNS, receive-only
        saved, text, prompts = self.run_setup(WITH_ID, ["", "", "", "", ""])
        self.assertIn("Meshpoint node ID: !a1b2c3d4 (Test Node/TST), transmit enabled", text)
        self.assertFalse(any("Node ID" in p for p in prompts))
        self.assertTrue(any("Receive-only" in p for p in prompts))
        self.assertEqual(saved["BRIDGE_NODE_ID"], "")

    def test_clears_a_leftover_bridge_node_id(self):
        write_env_file(self.path, {"MESHPOINT_PASSWORD": "pw", "BRIDGE_NODE_ID": "!11112222"})
        saved, text, _ = self.run_setup(WITH_ID, ["", "", "", "", ""])
        self.assertEqual(saved["BRIDGE_NODE_ID"], "")
        self.assertIn("Cleared BRIDGE_NODE_ID", text)

    def test_transmit_disabled_skips_the_receive_only_question(self):
        saved, text, prompts = self.run_setup(TX_OFF, ["", "", "", ""])
        self.assertIn("transmit disabled", text)
        self.assertFalse(any("Receive-only" in p for p in prompts))
        self.assertFalse(any("Node ID" in p for p in prompts))

    def test_no_meshpoint_id_asks_with_the_mac_as_default(self):
        # username, listen address, allowlist, mDNS, node ID (blank = MAC)
        saved, text, prompts = self.run_setup(NO_ID, ["", "", "", "", ""])
        self.assertIn("no transmit node ID", text)
        self.assertTrue(any("!00c0ffee" in p for p in prompts))
        self.assertFalse(any("Receive-only" in p for p in prompts))
        self.assertEqual(saved["BRIDGE_NODE_ID"], "")

    def test_no_id_and_no_mac_requires_a_node_id(self):
        saved, text, _ = self.run_setup(NO_ID, ["", "", "", "", "", "!0000beef"], mac=None)
        self.assertIn("cannot start", text)
        self.assertEqual(saved["BRIDGE_NODE_ID"], "!0000beef")

    def test_unreadable_config_still_asks_everything(self):
        # username, "save the unchecked password anyway?", listen, allowlist, mDNS, receive-only, node ID
        saved, text, prompts = self.run_setup(None, ["", "y", "", "", "", "", ""], login=None)
        self.assertIn("Could not read the Meshpoint node ID", text)
        self.assertTrue(any("Receive-only" in p for p in prompts))
        self.assertTrue(any("Node ID" in p for p in prompts))


if __name__ == "__main__":
    unittest.main()
