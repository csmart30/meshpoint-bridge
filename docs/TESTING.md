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
