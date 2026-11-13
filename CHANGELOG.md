# Changelog

## Unreleased

### Documentation

- Added `docs/API_CONTRACT.md`: every Meshpoint REST endpoint, WebSocket event and field the bridge uses, how it authenticates and handles errors, and the open items on both sides (for KMX415/meshpoint#172).

### Fixes from live testing (November 13, 2026)

- `--setup` now tells you how to apply the new settings for the file you changed: the restart command only when the service is installed, or the `--config` command for a settings file of your own.
- Messages no longer assume the bridge runs on a Raspberry Pi: the mDNS log lines say "this device's IP", the login hint points to `http://<meshpoint-ip>:8080`, and the installer no longer mentions a Pi.
- README and `docs/TESTING.md`: recorded the November 13 results, including how the iPhone app shows receive-only mode.
- `docs/TESTING.md`: recorded the service tests (upgrade over a manual install, boot, setup with a wrong password, Meshpoint restarting, uninstall and reinstall). The README notes Meshpoint's login lockout (5 failed attempts, 5 minutes).
- A DM recipient's acknowledgement that arrives before Meshpoint answers the send request is no longer lost. Previously the bridge discarded it and, 60 seconds later, told the app "relayed, not confirmed" for a message that had been delivered. Such acks are now held for 30 seconds and applied as soon as the send returns.
- A confirmation that arrives after the 60-second wait is still passed to the app; a duplicate acknowledgement (the same ack heard twice via a relay) is logged at debug level instead of "not matched".
- Incoming public-key encrypted DMs, which carry no channel hash, no longer log an "unknown channel_hash" warning.

### Service, settings and access-control update (not yet live-verified on hardware)

Added:

- `install.sh`: installs the bridge as a systemd service running as a dedicated `meshpoint-bridge` user (no login shell, no sudo rights), with its own Python environment in `/opt/meshpoint-bridge`. Re-running it upgrades in place, including an earlier manual install; `--uninstall` removes it and asks before deleting the settings.
- `systemd/meshpoint-bridge.service`: starts after Meshpoint at boot, retries with a growing delay while Meshpoint is unavailable, does not restart after a settings or password error (exit code 78), and runs sandboxed.
- Settings file `/etc/meshpoint-bridge/bridge.env` (mode 600), read at every start, created and changed with `--setup` (which checks the dashboard login before saving). Every setting can also be given as an environment variable or command-line option. Added `bridge.env.example`.
- `--show-config` (settings in effect and where each came from, password hidden) and `--version`. The bridge, Python, `meshtastic` and Meshpoint versions are logged at startup.
- Access controls that keep the stock Meshtastic no-authentication model: `BRIDGE_LISTEN_HOST` (bind address), `BRIDGE_ALLOW` (IP/network allowlist, checked before a new connection can replace the current phone) and `BRIDGE_MDNS` (turn off mDNS advertising).
- Receive-only mode: used when Meshpoint has no transmit node ID or transmit is disabled, or when `BRIDGE_RX_ONLY=1`. The app is told transmit is disabled, received traffic is still forwarded, and anything the app sends is rejected immediately without reaching Meshpoint's send API.

Changed:

- The node ID shown in the app now always comes from Meshpoint's transmit identity. A different `BRIDGE_NODE_ID` / `--node-id` is ignored with an error. The MAC-address ID is used only in receive-only mode, and the random-ID fallback was removed: with no usable ID the bridge stops with an error.
- If Meshpoint's identity changes while running (checked after a re-login or WebSocket reconnect), the bridge exits with code 75 so the service restarts it with the new identity.
- Message text is no longer logged by default: INFO logs record sender/destination, channel, length and status, and the text appears only with `-v`. A text event without a text field logs its field names instead of its contents.
- Wrong dashboard credentials, a missing password when not interactive, an unexpected login API and invalid settings now exit with code 78.
- `requirements.txt` pins the tested versions: `meshtastic` 2.7.11, `protobuf` 7.36.2, `aiohttp` 3.14.5, `zeroconf` 0.154.0.
- The README records the tested versions (Debian 13.6, Python 3.13.5, Meshpoint v0.7.9, Meshtastic app 2.7.28 on iPhone 16e) and documents installation, settings, node identity, receive-only mode and security.

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
- Replies and tapbacks received from the mesh are forwarded with their reply ID and tapback flag when Meshpoint provides them. A `--probe` check showed that Meshpoint does not currently include these fields, so this has no effect yet; an issue is being raised on Meshpoint.
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
