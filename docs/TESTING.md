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

- [ ] **Stable node ID:** restart the bridge without `--node-id` and confirm the node ID in the app stays the same and matches the last four bytes of the expected MAC address (Ethernet if present, otherwise Wi-Fi). Expect the ID to change once compared with the earlier random ID.
- [ ] **Session expiry:** invalidate the dashboard session (or wait for it to expire), then send a message from the app. Expect an "logging in again" log line and a successful send. Also confirm that a wrong stored password produces one failed login and then a 5-minute pause, not repeated attempts.
- [ ] **0,0 positions:** with a node reporting latitude/longitude of exactly 0,0, confirm the app shows no map position for it.
- [ ] **Stalled phone:** disable the phone's Wi-Fi or suspend the app mid-session, and confirm other traffic is not delayed and the dead connection is dropped.
- [ ] **Malformed event:** confirm that an event the bridge cannot handle produces an "event handling failed" log entry without a WebSocket reconnect.
- [x] **Replies and tapbacks:** checked November 11, 2026 with `--probe`. **Blocked on Meshpoint:** a tapback arrives as a plain text packet (`{"text": "😂"}`) with no `reply_id` or `emoji` field, so the app shows it as an ordinary emoji message. Meshpoint's send API also has no reply/tapback fields. See [ROADMAP.md](ROADMAP.md#replies-and-tapbacks).
- [ ] **Shutdown:** stop the bridge with Ctrl-C and with `kill -TERM`; confirm the "shutting down" log line, that the mDNS entry disappears from the phone's list, and that the listening port is released.
- [ ] **Timestamps:** confirm `last_heard` times in the app match Meshpoint's dashboard, in particular for any timestamps Meshpoint reports without a timezone.
- [ ] **Regression:** repeat the earlier broadcast, direct-message, acknowledgement, and metadata checks to confirm nothing changed.
