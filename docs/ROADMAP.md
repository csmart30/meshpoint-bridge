# Roadmap

This roadmap records intended work. Items below should not be interpreted as implemented features.

## Implemented in the current development update (partially live-verified)

- Map Meshpoint `latest_hops` into Meshtastic `NodeInfo.hops_away`.
- Map battery level, voltage, channel utilization, transmit airtime utilization, and SNR into the corresponding Meshtastic node fields.
- Forward available temperature and relative-humidity readings as `TELEMETRY_APP` environment metrics.
- Refresh node metadata when the mapped values change, without repeatedly sending unchanged temperature/humidity readings.
- Add standard-library unit tests for these mappings.

Live app testing on the RAK Miner V2 confirmed that hop count and battery percentage appear in the native Meshtastic app. The client-base node has a known hop-count discrepancy already present in Meshpoint; the bridge passes the Meshpoint value through unchanged. Temperature and humidity have been verified in the native app and matched Meshpoint readings. Individual verification of voltage, channel utilization, transmit airtime utilization, and SNR remains outstanding.

## Implemented in the hardening update (not yet live-verified)

- Isolated per-event error handling on the WebSocket; automatic re-login after session expiry with a 5-minute backoff after a rejected re-login.
- Per-phone bounded send queues, REST timeouts, and clean SIGINT/SIGTERM shutdown with mDNS goodbyes.
- MAC-based default node ID (Ethernet, then Wi-Fi), UTC handling of timezone-less timestamps, and 0,0 positions treated as unset.
- Merge of live NodeInfo/position updates with polled API data by recency, bounded duplicate-packet tracking, and receive-side reply/tapback fields.

Each of these still needs a pass on the live installation; see the checklist in [TESTING.md](TESTING.md).

## Next milestone

1. **Node retention:** include nodes whose `last_heard` value is within the previous five days.
2. **Bridge-local favorites:** handle Meshtastic `set_favorite_node` and `remove_favorite_node` admin requests; persist favorite node IDs locally on the bridge.
3. **Favorite retention rule:** include a node if it was heard within five days **or** is in the bridge-local favorites file. Favorites remain visible regardless of age.
4. **NodeInfo favorite flag:** set `NodeInfo.is_favorite` when sending a locally favorited node to the app.
5. **Persistence outside Git:** keep favorites and other runtime state under a configurable state directory (planned default: `/var/lib/meshpoint-bridge/`), never in the source checkout.
6. **Regression tests:** cover node filtering, favorite/unfavorite persistence, NodeInfo generation, and the existing text/DM acknowledgement behavior.

## Service and installation

1. Add a systemd unit that runs under a dedicated, unprivileged service account.
2. Add an installer/setup helper to create the service account, state directory, Python virtual environment, and unit.
3. Store credentials outside the repository in a root-readable environment file with restrictive permissions.
4. Avoid repeated login attempts and restart loops when credentials are rejected, to reduce the risk of triggering Meshpoint's login lockout. Partly done: a rejected re-login while running now backs off for 5 minutes. Rejected credentials at startup still exit immediately, so the service unit must not restart in a tight loop.
5. Support boot startup, journal logging, and straightforward upgrades with `git pull` plus service restart. (Clean shutdown on SIGTERM is already handled by the script.)

## Later improvements

- Improve API response validation. (Reconnect handling and REST timeouts were improved in the hardening update.)
- Normalize node IDs, packet IDs, and channel hashes consistently.
- Reconcile stale node records as Meshpoint's database changes. (Newer live events are no longer overwritten by older polled data, but nodes removed from Meshpoint are not yet removed from the bridge.)
- Expand selected packet-type support after the text, node, and acknowledgement paths have regression coverage.
- Document supported Meshpoint and Meshtastic app version ranges as they are tested.

- Items from the code review that are deliberately not done yet:
  - Add the missing `EU_433` (and other) regions to the region map.
  - Resolve the ambiguity in `decode_psk`, which tries base64 before hex, once Meshpoint's exact key format is confirmed.
  - Validate the channel `hash` format (hex or decimal) and clamp it to one byte.
  - Make `node_num` stricter about 8-character strings and handle hex IDs with stripped leading zeros.
  - Truncate the bridge's own long and short names with `trunc_utf8`, as is done for remote nodes.
  - Send module configuration, and answer more `get_*` admin requests with real responses.
  - Trim speculative alternate key names (for example `longName`, `lat`/`lng`) and simplify cookie handling once Meshpoint's API shape is confirmed.
  - Send replies and tapbacks from the phone if Meshpoint's send API supports them.
  - Decide how to handle the dashboard password on the command line (`--password` is visible in process listings; prefer the environment variable until then).

## Follow-up investigation

- Investigate incoming direct-message acknowledgements. Meshpoint logs show `TX routing ACK OK`, but the sending node does not appear to receive or accept the acknowledgement. This is being tracked separately from bridge functionality until the cause is established.
