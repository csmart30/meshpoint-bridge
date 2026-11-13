# Meshpoint API contract used by the bridge

This document lists every part of Meshpoint's HTTP API and WebSocket that `meshpoint_bridge.py` depends on: the endpoints it calls, the fields it reads, and what it assumes about them. It is meant as the "defined Meshpoint API contract" asked for in [KMX415/meshpoint#172](https://github.com/KMX415/meshpoint/issues/172), so that a change on either side can be checked against it.

- **Verified against:** Meshpoint v0.7.9 (see [TESTING.md](TESTING.md)).
- **Base URL:** `http://127.0.0.1:8080` (the bridge runs on the Meshpoint device).
- **Scope:** read-only use of configuration and node data, sending text messages, and receiving live packets. The bridge never changes Meshpoint's configuration and never touches the radio hardware.

Where the bridge accepts several field names (written `a | b`), it uses the first one present. The alternatives exist so the bridge tolerates small API differences between Meshpoint versions; the first name is the one seen on v0.7.9.

## Summary

| Method | Path | Purpose | Required |
| --- | --- | --- | --- |
| `POST` | `/api/auth/login` | Dashboard login | Yes, when the dashboard requires a login |
| `GET` | `/api/config` | Identity, radio settings, channels, own position; also the login check | Yes |
| `GET` | `/api/nodes` | Node list, polled every 30 seconds | Yes |
| `GET` | `/api/device/status` | Meshpoint version, logged at startup | No (best effort) |
| `POST` | `/api/messages/send` | Send a text message | Yes, unless receive-only |
| `POST` | `/api/config/nodeinfo/send` | Broadcast Meshpoint's NodeInfo | No (see [Open items](#open-items)) |
| `WS` | `/ws` | Live packet events | Yes |

## Authentication

### `POST /api/auth/login`

Request body:

```json
{"username": "admin", "password": "<dashboard password>"}
```

| Response | Bridge behaviour |
| --- | --- |
| `2xx` | Logged in. A token in `access_token` \| `token` \| `jwt` is sent as `Authorization: Bearer <token>` on later requests; any cookies are also sent on later requests. |
| `401` / `403` | Wrong credentials. Exactly one attempt is counted; at startup the bridge exits with code 78 and the service does not retry. |
| `429` | Locked out. The bridge stops and asks the user to wait. |
| Other `3xx`–`5xx` | Treated as an incompatible login API (exit code 78). |

The response body is never logged, because it contains the session token.

After a `2xx` login the bridge confirms the session with `GET /api/config`; only a `200` counts as logged in.

**Session expiry:** any request that returns `401` triggers one re-login with the stored credentials, then the request is retried once. If the re-login is rejected, the bridge waits 5 minutes before trying again, to stay clear of the lockout (Meshpoint v0.7.9: 5 failed attempts lock logins for 5 minutes).

**Credentials currently needed:** the dashboard admin username and password. A scoped token that can only read config and nodes and send messages would be preferable; see [Open items](#open-items).

The same `Authorization` header and cookies are used for the WebSocket.

## `GET /api/config`

Read at startup, and again after a re-login or WebSocket reconnect to check that Meshpoint's identity has not changed.

### Identity: `transmit`

| Field | Use |
| --- | --- |
| `transmit.node_id_hex` \| `transmit.node_id` | The node ID shown to the app. Always used when present (e.g. `"!a1b2c3d4"`, or an integer). Empty, `0` or missing means Meshpoint has no transmit identity: the bridge runs receive-only. |
| `transmit.enabled` | `false` puts the bridge in receive-only mode. Missing is treated as enabled. |
| `transmit.long_name`, `transmit.short_name` | Own node names shown in the app. |
| `transmit.hop_limit` | Reported to the app as the LoRa hop limit (default 3). |

If the reported identity (node ID or receive-only state) changes while the bridge runs, the bridge exits with code 75 and the service restarts it.

### Radio: `radio`

| Field | Use |
| --- | --- |
| `radio.region` | Reported to the app as the LoRa region (e.g. `US`). |
| `radio.current_preset` \| `radio.modem_preset` \| `radio.preset` | Reported as the modem preset (e.g. `MEDIUM_FAST`); also gives the default channel name. |

### Own position: `device`

| Field | Use |
| --- | --- |
| `device.latitude`, `device.longitude`, `device.altitude` | Own map pin in the app. Missing, out-of-range or exactly 0,0 means no pin. |

### Channels

The bridge uses the first list named `channels` found anywhere in the config object. Each entry:

| Field | Use |
| --- | --- |
| `index` | Channel slot 0–7 (defaults to the position in the list). |
| `name` | Channel name, truncated to 11 characters. Empty means the preset's default name. |
| `psk_b64` \| `psk` \| `key` \| `psk_base64` | Channel key, base64. |
| `enabled` | Disabled channels are reported as disabled and not matched. |
| `hash` | Channel hash as hex (e.g. `"0xF3"`). If missing, the bridge computes it from the name and key the same way the firmware does. |

The channel list, **including keys**, is reported to any connected app, as a stock Meshtastic node does over its TCP API. This is one reason the listener must only be reachable on a trusted network (see the README's Security section).

## `GET /api/nodes`

Polled every 30 seconds. The response is either a list of nodes, or an object with the list under `nodes` \| `items` \| `data`. Entries with `protocol: "meshcore"` are ignored. When an entry is older (by `last_heard`) than what the bridge already has from live events, the newer data is kept.

| Field | Use |
| --- | --- |
| `node_id` \| `id` \| `nodeId` \| `num` | Node number (`"!aabbccdd"`, `"aabbccdd"` or an integer). Required. |
| `long_name` \| `longName` \| `name`, `short_name` \| `shortName` | Node names. |
| `hardware_model` \| `hw_model` | Hardware model number. |
| `public_key` | Hex public key, passed to the app so it can send encrypted DMs. |
| `role` | Device role. |
| `last_heard` \| `lastHeard` \| `last_seen` \| `timestamp` | Epoch seconds or ISO 8601. Timestamps without a time zone are treated as UTC. |
| `latitude` \| `lat`, `longitude` \| `lon` \| `lng`, `altitude` | Position. 0,0 and out-of-range values are ignored. |
| `latest_hops` \| `hops_away` | Hops away. |
| `latest_snr` \| `snr` | Last SNR. |
| `latest_battery` \| `battery_level`, `latest_voltage` \| `voltage`, `latest_channel_util` \| `channel_utilization`, `latest_air_util` \| `air_util_tx` | Device metrics. |
| `latest_temperature` \| `temperature`, `latest_humidity` \| `humidity` | Environment telemetry. |

## `GET /api/device/status`

Optional. `firmware_version` \| `version` is logged at startup so test and bug reports carry the Meshpoint version. Failure is ignored.

## `POST /api/messages/send`

Sends a text message from the app. Not called in receive-only mode.

Request body:

```json
{
  "text": "Hello",
  "destination": "broadcast",
  "protocol": "meshtastic",
  "channel": 0,
  "want_ack": false
}
```

- `destination`: `"broadcast"` for a channel message, otherwise the destination node ID as 8 hex digits (e.g. `"0badc0de"`).
- `channel`: the channel index the app chose.
- `want_ack`: `true` only for DMs where the app asked for an acknowledgement.

Response: any `2xx` is treated as sent; the packet ID in `packet_id` \| `id` (8 hex digits, e.g. `"00009e72"`) is used to match the recipient's acknowledgement. Example from v0.7.9:

```json
{"success": true, "packet_id": "00009e72", "protocol": "meshtastic", "timestamp": 1794500000.0, "airtime_ms": 140, "error": ""}
```

**Timing:** this response can arrive *after* the recipient's acknowledgement has already come in over the WebSocket (Meshpoint appears to answer once the transmission is complete). The bridge holds unmatched acknowledgements for 30 seconds so this case still resolves correctly.

## `POST /api/config/nodeinfo/send`

No request body. Asks Meshpoint to broadcast its own NodeInfo; any `2xx` is treated as done. Currently called when the app asks to exchange user info with a node, at most once every 5 minutes. This is a broadcast, not the targeted request/reply the app expects, so it is due to be replaced by an "unsupported" reply until [KMX415/meshpoint#171](https://github.com/KMX415/meshpoint/issues/171) exists.

## WebSocket `/ws`

Connected with the same authentication as the REST calls, with a 30-second heartbeat. If the connection drops, the bridge reconnects every 5 seconds; after a reconnect it re-checks Meshpoint's identity through `/api/config`.

The bridge only handles messages of this shape:

```json
{"type": "packet", "data": { ... }}
```

Any other `type` is ignored, as are packets with `data.protocol` of `"meshcore"`.

### Common packet fields (`data`)

| Field | Use |
| --- | --- |
| `packet_type` | `text…`, `nodeinfo`, `position`, `routing…` (prefix match for text and routing). Other types are ignored. |
| `packet_id` | Packet ID as hex. |
| `source_id` | Sender node ID. |
| `destination_id` | `"^all"` (or `"broadcast"`) for channel traffic, otherwise a node ID. Text addressed to other nodes is not forwarded to the app. |
| `channel` \| `channel_index` \| `channel_idx` | Channel index, if Meshpoint provides it. |
| `channel_hash` | Used to find the channel index when no index is given. `0` on a packet addressed to this node is a public-key encrypted DM (no channel). |
| `hop_limit` | Remaining hop limit. |
| `timestamp` | Receive time (epoch seconds or ISO 8601; no time zone means UTC). |
| `signal.snr`, `signal.rssi` | Receive signal quality. |
| `decoded_payload` | Type-specific content, below. |

### `decoded_payload` by packet type

| Packet type | Fields |
| --- | --- |
| text | A string, or an object with `text` \| `message` \| `msg` \| `body`. Optional `reply_id` \| `replyId` and `emoji` are used when present (not sent by v0.7.9). |
| `nodeinfo` | `long_name`, `short_name`, `hw_model`, `public_key`, `role`. |
| `position` | `latitude`, `longitude`, `altitude`, `precision_bits`. No fix / 0,0 is ignored. |
| routing | `request_id` \| `requestId` \| `request_packet_id` (the acknowledged packet's ID), `error_reason` \| `error` \| `reason` (Meshtastic routing error number; 0 = delivered). |

Routing packets are matched to DMs the app sent, by `request_id` against the `packet_id` returned by `/api/messages/send`. The `error_reason` is passed to the app unchanged, so it shows the real reason, e.g. 35 (`PKI_UNKNOWN_PUBKEY`, "recipient needs your key").

## Identifier formats

- **Node IDs** are accepted as `"!aabbccdd"`, `"aabbccdd"`, or an integer.
- **Packet IDs** are accepted as hex strings (`"00009e72"`) or integers.
- The bridge sends destinations as 8 lower-case hex digits without `!`.

## Open items

These affect the contract and are tracked on the Meshpoint side or in this repository:

1. **Scoped credentials.** The bridge needs the dashboard admin password today. A token or role limited to `GET /api/config`, `GET /api/nodes`, `POST /api/messages/send` and `/ws` would reduce the risk of storing it.
2. **Reply and tapback fields.** WebSocket text events from v0.7.9 do not include the reply ID or emoji flag, so replies and tapbacks reach the app as plain messages. The bridge already reads `reply_id` and `emoji` if they are added.
3. **Inbound DM acknowledgements.** Acknowledging DMs sent *to* the Meshpoint is done by Meshpoint's radio stack, not the bridge. On v0.7.9 Meshpoint logs sending the routing ack, but senders do not appear to accept it.
4. **NodeInfo request/reply** ([#171](https://github.com/KMX415/meshpoint/issues/171)) and **traceroute** ([#170](https://github.com/KMX415/meshpoint/issues/170)) have no API yet. The bridge reports traceroute as unsupported; NodeInfo requests currently fall back to `POST /api/config/nodeinfo/send` (to be changed, see above).
5. **No configuration writes.** Settings changes the app sends (`set_config`, favorites, contacts) are not applied, because there is no API for them and the dashboard is the place to change Meshpoint settings. Reporting these honestly to the app is a planned change in this repository.
