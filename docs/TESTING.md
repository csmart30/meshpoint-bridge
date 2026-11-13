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
- [ ] **Shutdown:** stop the bridge with Ctrl-C and with `kill -TERM`; confirm the "shutting down" log line, that the mDNS entry disappears from the phone's list, and that the listening port is released. *Nov 13: Ctrl-C logged "shutting down" and exited cleanly; the mDNS entry disappearing and `kill -TERM` are still to check.*
- [ ] **Timestamps:** confirm `last_heard` times in the app match Meshpoint's dashboard, in particular for any timestamps Meshpoint reports without a timezone.
- [ ] **Regression:** repeat the earlier broadcast, direct-message, acknowledgement, and metadata checks to confirm nothing changed. *Nov 13: broadcasts on a secondary channel, DMs with acknowledgements, received text and NodeInfo all worked; metadata display not re-checked.*

## Pending verification: service, settings and access-control update

Added after the hardening update. Items marked [x] were checked on November 13, 2026 on the RAK Miner V2 (Meshpoint v0.7.9, Meshtastic app 2.7.28 on iPhone 16e); the rest are still pending. Unit tests cover the settings file, setting precedence, the allowlist, node-ID selection, receive-only rejection and log privacy. Record the date, Meshpoint version and result next to each item.

- [x] **Install:** `sudo ./install.sh` on the RAK Miner V2 creates the `meshpoint-bridge` user, `/opt/meshpoint-bridge/venv`, `/etc/meshpoint-bridge/bridge.env` (mode 600, owned by `meshpoint-bridge`) and a running service. The app connects as before. *Nov 13: service started under systemd, logs in journald, app connected, broadcast and DM sent.*
- [x] **Upgrade from the manual install:** with an earlier manual install (a git clone) in `/opt/meshpoint-bridge`, `install.sh` replaces its program files and venv, leaves the other files alone, and the service takes over port 4403. *Nov 13: upgraded in place and started; uninstall later removed only the installed files and reported the clone's leftover files (`.git`, `docs/`, `tests/`) instead of deleting them.*
- [x] **Boot:** after a reboot the service starts after Meshpoint and the app can connect without any manual step. *Nov 13: passed.*
- [x] **Wrong password in setup:** `--setup` checks the login and stops after three wrong passwords without saving anything; running it again with the right password saves it. *Nov 13: passed.* Note: Meshpoint locks dashboard logins for 5 minutes after 5 failed attempts, so two failed setup runs in a row can trigger the lockout.
- [ ] **Wrong password in the service:** put a wrong `MESHPOINT_PASSWORD` in `/etc/meshpoint-bridge/bridge.env` and restart; the service stops with exit code 78 and is **not** restarted (one failed dashboard login). Covered by unit tests; not yet checked live.
- [x] **Meshpoint down while the bridge runs:** stop `meshpoint`; the bridge keeps running, retries the WebSocket every 5 seconds and node polling every 30 seconds, and recovers when Meshpoint starts again. *Nov 13: reconnected about 15 seconds after Meshpoint started, without a new login or a service restart; the phone stayed connected and broadcasts and replies worked straight away.*
- [ ] **Meshpoint down at startup:** with Meshpoint stopped, start the bridge service; it exits and systemd retries with growing delays (10 s up to 5 minutes, without limit) until Meshpoint is up.
- [x] **Node ID:** the app shows Meshpoint's transmit node ID (dashboard / `/api/config` `transmit.node_id_hex`). Setting a different `BRIDGE_NODE_ID` logs an error and does not change it. *Nov 13: startup log reports the identity "from Meshpoint"; the mismatched-`BRIDGE_NODE_ID` case is still to check.*
- [ ] **Identity change:** change `transmit.node_id` in Meshpoint and restart Meshpoint; the bridge logs "Meshpoint identity changed" and the service restarts it with the new ID.
- [x] **Receive-only:** with `BRIDGE_RX_ONLY=1` (and separately with transmit disabled on the Meshpoint), the app shows transmit disabled, received messages still appear, and a message sent from the app fails immediately and is not transmitted. *Nov 13 (`--rx-only`): the app showed Transmit off and "no radio interface" on send; nothing was passed to Meshpoint; received messages and NodeInfo still arrived. Transmit disabled on the Meshpoint itself is still to check.*
- [x] **Allowlist:** with `BRIDGE_ALLOW` set to the phone's network, the phone connects; a device outside it is refused (log line) and does not disconnect the phone. *Nov 13: the phone on the allowed /24 connected; a device on another subnet was refused with a log line. Refusal while the phone stays connected is covered by a unit test, not yet seen live.*
- [x] **Listen address / mDNS:** with `BRIDGE_LISTEN_HOST` set to the Meshpoint's LAN IP, the app connects on that address only; with `BRIDGE_MDNS=0`, the bridge no longer appears in the app's discovered list. *Nov 13: `--no-mdns` logged that advertising is off and the phone connected by IP; the listen-address case is still to check.*
- [x] **Log privacy:** without `-v`, sent and received messages appear in `journalctl -u meshpoint-bridge` as metadata only, with no message text. *Nov 13: confirmed for sent and received text; the text appears only with `-v`.*
- [x] **DM delivery status:** a DM to a reachable node shows as delivered; a DM to a node that does not have this Meshpoint's public key fails with "recipient needs your key" (`PKI_UNKNOWN_PUBKEY`, error 35), as on a stock node. *Nov 13: both seen. A recipient's acknowledgement can arrive before Meshpoint answers the send request; before the fix this made a delivered DM show as "relayed, not confirmed". After the fix, several DMs whose ack arrived first were all confirmed. One DM got no acknowledgement at all and correctly timed out.*
- [x] **Uninstall:** `sudo /opt/meshpoint-bridge/install.sh --uninstall` removes the service and program files and asks before deleting the settings. *Nov 13: service, unit, installed files, venv, settings (after confirming) and the service user were removed; a clean reinstall afterwards worked.*
