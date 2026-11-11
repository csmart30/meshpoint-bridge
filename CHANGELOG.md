# Changelog

## Unreleased

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
