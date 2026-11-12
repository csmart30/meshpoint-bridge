# Changelog

## Unreleased

### Hardening update (not yet live-verified on hardware)

Fixed:

- A malformed WebSocket event no longer drops the Meshpoint connection; each event is handled in isolation and errors are logged.
- The bridge logs in to the Meshpoint dashboard again when the session expires (REST calls and WebSocket handshake). After a failed re-login it waits 5 minutes before retrying, to avoid the dashboard lockout.
- A phone that stops reading no longer blocks Meshpoint-to-phone delivery for others. Each phone has a bounded send queue and writer task; a phone that falls behind or cannot drain within 15 seconds is disconnected.
- Meshpoint REST calls now time out after 30 seconds instead of waiting indefinitely.
- Background acknowledgement-timeout tasks are now referenced so they cannot be garbage-collected early.
- ISO timestamps without a timezone are interpreted as UTC instead of the host's local time.
- Frame resynchronisation handles repeated `0x94` bytes before `0xC3`.
- Node polling no longer overwrites newer live NodeInfo/position data with older API data.

Changed:

- The virtual node ID now defaults to the Meshpoint configuration's ID, then the Ethernet MAC address, then the Wi-Fi MAC address (random only as a last resort). Installations that previously got a random ID will see one change.
- Nodes (and the Meshpoint's own location) reporting latitude/longitude of exactly 0,0, or out-of-range values, are treated as having no position.
- Replies and tapbacks received from the mesh are forwarded with their reply ID and tapback flag. Field names are unverified.
- SIGINT/SIGTERM now shut the bridge down cleanly and unregister the mDNS service.
- The bridge is documented as Linux-only and intended to run on the device that runs Meshpoint.

Security / hygiene:

- Debug logging no longer prints the Meshpoint login response body, which can contain a session token.
- Added SPDX license identifiers to the Python files, and ignored local `*.patch`, `*.diff` and `probe*.txt` files in `.gitignore`.

Cleanup:

- Removed unused variables, merged the duplicate routing-ack builders and packet boilerplate, simplified change-detection signatures, fixed `resolve_schema` so `$ref` entries inside lists are resolved, and removed identity entries from the region map.
- Duplicate-packet tracking uses a bounded queue instead of clearing all entries at once.
- Added unit tests for position validation, timestamps, MAC-based node IDs, duplicate tracking, and schema resolution.

### Earlier unreleased changes

- Map Meshpoint `latest_hops` to Meshtastic `NodeInfo.hops_away`.
- Forward battery level, voltage, channel utilization, and transmit airtime utilization through `NodeInfo.device_metrics`.
- Forward temperature and relative humidity as Meshtastic `TELEMETRY_APP` environment metrics when Meshpoint provides them.
- Refresh node information when Meshpoint's last-heard, hop-count, SNR, or device-metric values change.
- Avoid repeatedly forwarding unchanged temperature/humidity readings during node polling.
- Add unit tests for the Meshpoint-to-Meshtastic metadata mapping.
- Live testing on the RAK Miner V2 confirmed that hop count and battery percentage appear in the native Meshtastic app.
- Recorded a hop-count discrepancy already visible in Meshpoint: the client-base node advertises Meshpoint as a direct neighbor, while the Meshpoint dashboard reports the client-base node as one hop away. The bridge passes Meshpoint's value through unchanged.
- Temperature and humidity have been verified in the native app and matched Meshpoint readings. Individual verification of voltage, channel utilization, transmit airtime utilization, and SNR remains outstanding.

## Initial repository snapshot

- Organized the original working bridge as a GitHub-ready project.
- Recorded live testing on RAK Miner V2 hardware.
- No intended runtime behavior changes in the initial repository-preparation snapshot.
