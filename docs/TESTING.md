# Hardware testing record

## Tested platform

The bridge is designed around Meshpoint's HTTP REST API and WebSocket, rather than any specific radio/concentrator hardware. It is intended to run on the Linux device that runs Meshpoint, using the local Meshpoint API. This test record covers only the installation below.

- Hardware: RAK Miner V2 (Meshpoint's hardware matrix refers to the prebuilt unit as RAK Hotspot V2 / RAK7248).
- Software under test: Meshpoint plus `meshpoint_bridge.py` on the same host.
- Bridge connection: Meshpoint API on the local host, Meshtastic TCP listener on port `4403`.
- Test date: November 10, 2026.
- Host OS: Debian 13.6
- Python version: 3.13.5
- Meshpoint version: v0.7.9
- Meshtastic Mobile App Version: 2.7.28 (2718)
- Client device: iPhone 16e

## Verified in the live test

- Bridge started and authenticated to the Meshpoint dashboard.
- Meshpoint configuration and channel data loaded.
- The bridge loaded 499 nodes for the initial node-list exchange.
- The native Meshtastic app connected and completed both configuration and node-list requests.
- A broadcast `Ping` was transmitted on channel index `2` (the third configured channel in that installation); the primary channel was not used.
- A response received by Meshpoint was forwarded to the app on channel index `2`.
- Direct messages were transmitted to a base node and recipient routing acknowledgements were observed.
- NodeInfo and position events were received from Meshpoint and forwarded to the app.

## Not established by this test

- Compatibility with other Meshpoint host hardware or deployment types. The bridge is intended to be hardware-agnostic, but those combinations have not been tested here. Running the bridge on a different machine than Meshpoint is not a supported configuration.
- Compatibility with all Meshpoint or Meshtastic app versions.
- Other Meshtastic portnums, admin operations, and device-management workflows.
- Five-day node retention, local persistent favorites, or systemd boot startup; those features are not implemented in this snapshot.

This is a concise record of the observed test session, not a formal certification or exhaustive compatibility matrix.


## Additional metadata mapping (November 10, 2026)

The metadata-mapping update was run on the live RAK Miner V2 / Meshpoint installation and checked in the native Meshtastic app. The app displayed hop count and battery percentage for nodes with those values. The code maps the following Meshpoint fields to Meshtastic protobuf fields:

- `latest_hops` to `NodeInfo.hops_away`; the bridge passes the Meshpoint-reported value through unchanged.
- `latest_battery`, `latest_voltage`, `latest_channel_util`, and `latest_air_util` to `NodeInfo.device_metrics`.
- `latest_snr` to `NodeInfo.snr`.
- `latest_temperature` and `latest_humidity` to `TELEMETRY_APP` / `EnvironmentMetrics` packets.

The app display of hop count and battery percentage has been verified. Individual live display verification of voltage, channel utilization, transmit airtime utilization, and SNR has not been recorded. Temperature and humidity were observed in the native app and matched Meshpoint readings during live testing. The metadata mapping also has four passing standard-library unit tests and passed Python syntax and whitespace checks.

### Hop-count observation

The client-base node advertises Meshpoint as one of its direct neighbors, while the Meshpoint dashboard reports the client-base node as one hop away. This discrepancy is visible in Meshpoint itself. The bridge maps `latest_hops` to `NodeInfo.hops_away` without calculating a route, subtracting a hop, or applying another correction. The cause of the discrepancy has not been established.

## Pending verification: hardening update

The following behavior was added after the November 10, 2026 session and has **not** been tested on hardware. The standard-library unit tests cover the pure helper functions (position validation, timestamp parsing, MAC-based node ID selection, duplicate tracking, schema resolution). Everything below needs a live pass; record the date, Meshpoint version, and result next to each item when done.

- [ ] **Session expiry:** invalidate the dashboard session (or wait for it to expire), then send a message from the app. Expect an "logging in again" log line and a successful send. Also confirm that a wrong stored password produces one failed login and then a 5-minute pause, not repeated attempts.
- [ ] **0,0 positions:** with a node reporting latitude/longitude of exactly 0,0, confirm the app shows no map position for it.
- [ ] **Stalled phone:** disable the phone's Wi-Fi or suspend the app mid-session, and confirm other traffic is not delayed and the dead connection is dropped.
- [ ] **Malformed event:** confirm that an event the bridge cannot handle produces an "event handling failed" log entry without a WebSocket reconnect.
- [x] **Replies and tapbacks:** checked November 11, 2026 with `--probe`. **Blocked on Meshpoint:** a tapback arrives as a plain text packet (`{"text": "😂"}`) with no `reply_id` or `emoji` field, so the app shows it as an ordinary emoji message. Meshpoint's send API also has no reply/tapback fields. See [ROADMAP.md](ROADMAP.md#replies-and-tapbacks).
- [ ] **Shutdown:** stop the bridge with Ctrl-C and with `kill -TERM`; confirm the "shutting down" log line, that the mDNS entry disappears from the phone's list, and that the listening port is released.
- [ ] **Timestamps:** confirm `last_heard` times in the app match Meshpoint's dashboard, in particular for any timestamps Meshpoint reports without a timezone.
- [ ] **Regression:** repeat the earlier broadcast, direct-message, acknowledgement, and metadata checks to confirm nothing changed.

## Pending verification: service, settings and access-control update

Added after the hardening update; not yet tested on hardware. Unit tests cover the settings file, setting precedence, the allowlist, node-ID selection, receive-only rejection and log privacy. Record the date, Meshpoint version and result next to each item.

- [ ] **Install:** `sudo ./install.sh` on the RAK Miner V2 creates the `meshpoint-bridge` user, `/opt/meshpoint-bridge/venv`, `/etc/meshpoint-bridge/bridge.env` (mode 600, owned by `meshpoint-bridge`) and a running service. The app connects as before.
- [ ] **Upgrade from the manual install:** with the earlier hand-started bridge running from `/opt/meshpoint-bridge`, the installer offers to stop it and the service takes over port 4403.
- [ ] **Boot:** after a reboot the service starts after Meshpoint and the app can connect without any manual step.
- [ ] **Wrong password:** set a wrong password with `--setup`; the service fails once with exit code 78 and is **not** restarted (one failed dashboard login, no lockout).
- [ ] **Meshpoint down:** stop `meshpoint`; the bridge exits and the service retries with growing delays, then recovers when Meshpoint starts again.
- [ ] **Node ID:** the app shows Meshpoint's transmit node ID (dashboard / `/api/config` `transmit.node_id_hex`). Setting a different `BRIDGE_NODE_ID` logs an error and does not change it.
- [ ] **Identity change:** change `transmit.node_id` in Meshpoint and restart Meshpoint; the bridge logs "Meshpoint identity changed" and the service restarts it with the new ID.
- [ ] **Receive-only:** with `BRIDGE_RX_ONLY=1` (and separately with transmit disabled on the Meshpoint), the app shows transmit disabled, received messages still appear, and a message sent from the app fails immediately and is not transmitted.
- [ ] **Allowlist:** with `BRIDGE_ALLOW` set to the phone's network, the phone connects; a device outside it is refused (log line) and does not disconnect the phone.
- [ ] **Listen address / mDNS:** with `BRIDGE_LISTEN_HOST` set to the Meshpoint's LAN IP, the app connects on that address only; with `BRIDGE_MDNS=0`, the bridge no longer appears in the app's discovered list.
- [ ] **Log privacy:** without `-v`, sent and received messages appear in `journalctl -u meshpoint-bridge` as metadata only, with no message text.
- [ ] **Uninstall:** `sudo /opt/meshpoint-bridge/install.sh --uninstall` removes the service and program files and asks before deleting the settings.
