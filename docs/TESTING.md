# Hardware testing record

## Tested platform

The bridge is designed around Meshpoint's HTTP REST API and WebSocket, rather than any specific radio/concentrator hardware. It can run on the Meshpoint host or on another Python-capable machine with network access to the Meshpoint API. This test record covers only the installation below.

- Hardware: RAK Miner V2 (Meshpoint's hardware matrix refers to the prebuilt unit as RAK Hotspot V2 / RAK7248).
- Software under test: Meshpoint plus `meshpoint_bridge.py` on the same host.
- Bridge connection: Meshpoint API on the local host, Meshtastic TCP listener on port `4403`.
- Test date: November 10, 2026.
- Host OS: Debeian 13.6
- Python version: 3.13.5
- Meshpoint version: v0.7.9
- Meshtastic Moble App Version: 2.7.28 (2718)
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

- Compatibility with other Meshpoint host hardware or deployment types. The bridge is intended to be hardware-agnostic, but those combinations have not been tested here.
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
