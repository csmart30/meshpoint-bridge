# Roadmap

This roadmap records intended work. Items below should not be interpreted as implemented features.

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
4. Avoid repeated login attempts and restart loops when credentials are rejected, to reduce the risk of triggering Meshpoint's login lockout.
5. Support boot startup, clean shutdown, journal logging, and straightforward upgrades with `git pull` plus service restart.

## Later improvements

- Improve API response validation and reconnect handling.
- Normalize node IDs, packet IDs, and channel hashes consistently.
- Reconcile stale node records as Meshpoint's database changes.
- Expand selected packet-type support after the text, node, and acknowledgement paths have regression coverage.
- Document supported Meshpoint and Meshtastic app version ranges as they are tested.
