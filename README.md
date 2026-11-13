# Meshpoint Bridge

A small Python bridge that makes a [Meshpoint](https://github.com/KMX415/meshpoint) gateway appear as a Meshtastic TCP device, allowing the native Meshtastic mobile app to connect to Meshpoint over the local network.

**Project status: experimental / pre-release.** This is an early implementation with real hardware testing, but it is not a complete Meshtastic device implementation.

## Hardware compatibility and testing

**The bridge is designed to work with Meshpoint, not with a particular radio board or gateway enclosure.** It communicates with Meshpoint through its HTTP REST API and WebSocket; it does not directly access the LoRa concentrator, SPI, GPIO, or other hardware-specific interfaces. The bridge is installed on the Linux device that runs Meshpoint, where it talks to the local Meshpoint API at `127.0.0.1:8080`.

In principle, this means it should work with any platform that runs a compatible Meshpoint release and exposes the API endpoints/events the bridge uses. Compatibility with different Meshpoint versions and deployments still needs testing. The bridge itself is hardware-agnostic; **Meshpoint's own hardware support requirements still apply**. Consult the [Meshpoint hardware matrix](https://github.com/KMX415/meshpoint/blob/main/docs/HARDWARE-MATRIX.md) and [onboarding guide](https://github.com/KMX415/meshpoint/blob/main/docs/ONBOARDING.md) for current platform support and experimental paths.

**Tested installation:** RAK Miner V2 running Meshpoint, with the bridge running on the same host and using the local Meshpoint API. Meshpoint documentation refers to the prebuilt RAK platform as the **RAK Hotspot V2 (RAK7248)**. These names are retained to identify the tested installation accurately; they are not a hardware requirement for the bridge.

On November 10, 2026, the following behavior was verified against the live installation:

- The native Meshtastic app connected to the bridge over TCP port `4403`.
- The bridge completed the app's configuration and node-list exchange (499 nodes were present in that test).
- A broadcast message was transmitted on channel index `2` (the third configured channel in that installation); the primary channel was not used for this test.
- A response received by Meshpoint was forwarded to the native app on channel index `2`.
- Direct messages were transmitted and the bridge received recipient routing acknowledgements.
- NodeInfo and position updates were received from Meshpoint and forwarded to the app.
- A subsequent metadata update displayed hop count and battery percentage in the native Meshtastic app.
- The client-base node advertises Meshpoint as a direct neighbor, while Meshpoint's dashboard reports that node as one hop away. The bridge forwards Meshpoint's `latest_hops` value unchanged; the cause of the discrepancy is unknown.

This is a report of one working installation, not a claim that every Meshpoint version, host platform, Meshtastic app version, or packet type has been tested.

### Tested with

| Component | Version |
| --- | --- |
| Host | RAK Miner V2 (RAK7248), Debian 13.6 |
| Python | 3.13.5 |
| Meshpoint | v0.7.9 |
| Meshtastic app | 2.7.28 (2718) on iPhone 16e |
| Python packages | `meshtastic` 2.7.11, `protobuf` 7.36.2, `aiohttp` 3.14.5, `zeroconf` 0.154.0 (pinned in `requirements.txt`) |

See [docs/TESTING.md](docs/TESTING.md) for the full test record.

## How it works

```text
Meshtastic mobile app
        |
        | TCP :4403 (Meshtastic framed protobuf)
        v
  meshpoint_bridge.py
        |
        | HTTP REST + WebSocket
        v
  Meshpoint :8080
        |
        v
  LoRa concentrator / mesh
```

The bridge presents a virtual Meshtastic node to the phone. Meshpoint remains responsible for the actual radio and its packet database. The bridge reads Meshpoint data over its local REST API and WebSocket, and submits supported outgoing messages through the Meshpoint API.

## Current capabilities

- Meshtastic TCP framing on port `4403`.
- mDNS service advertisement (`_meshtastic._tcp.local.`) when `zeroconf` is installed.
- Runs as a systemd service under its own `meshpoint-bridge` user, installed and removed with `install.sh`, with settings and the dashboard password in a protected settings file created by `--setup` *(not yet live-verified)*.
- Meshpoint dashboard authentication from the settings file (or an interactive prompt when run by hand). The credentials are kept in memory so the bridge can log in again if the dashboard session expires *(not yet live-verified)*.
- Optional listen-address, client allowlist and mDNS settings (see [Security](#security)) *(not yet live-verified)*.
- Receive-only mode when Meshpoint has no transmit identity, or when forced (see [Receive-only mode](#receive-only-mode)) *(not yet live-verified)*.
- Initial configuration and node database exchange with the Meshtastic app.
- Text-message TX through Meshpoint's REST API.
- Text-message RX through Meshpoint's WebSocket.
- Direct-message acknowledgement tracking when Meshpoint returns a correlatable packet ID and a recipient acknowledgement is observed.
- NodeInfo and position forwarding, including Meshpoint-reported hop count and SNR.
- Device-metric forwarding for battery percentage, voltage, channel utilization, and transmit airtime utilization when Meshpoint supplies those values. Hop count and battery percentage have been observed in the native app; the other metrics still need individual live verification.
- Environmental temperature and relative-humidity forwarding through Meshtastic `TELEMETRY_APP`; both have been verified in the native app and matched Meshpoint readings.
- Read-only responses to selected admin queries.
- The node ID shown in the app always matches the ID Meshpoint transmits with (see [Node identity](#node-identity)) *(not yet live-verified)*.
- Nodes whose reported position is exactly 0,0 are treated as having no GPS position and are shown without a map pin *(not yet live-verified)*.
- Receive-side support for replies and tapbacks: if a Meshpoint event carries a reply ID or tapback flag, it is forwarded to the app. Meshpoint does not currently include these fields (see limitations below), so for now replies and tapbacks from the mesh appear in the app as ordinary text messages.
- Robustness: a bad WebSocket event is logged instead of dropping the connection, a phone that stops reading is disconnected without stalling other traffic, Meshpoint REST calls time out after 30 seconds, and SIGINT/SIGTERM shut the bridge down cleanly (including mDNS goodbyes) *(not yet live-verified)*.

Items marked *not yet live-verified* were added in a hardening update. They are covered by unit tests of the helper functions where practical, but have not yet been exercised on the RAK Miner V2 installation. See [docs/TESTING.md](docs/TESTING.md) for the pending checklist.

## Current limitations / planned work

The following are **not implemented yet**:

- Five-day node-list retention with older nodes retained only when locally favorited.
- Persisting favorite/unfavorite actions from the native Meshtastic app to a bridge-local favorites file. The current bridge acknowledges `set_favorite_node` but does not persist it.
- Increase support for more Meshtastic packet types and administration operations (traceroute, node info exchanges).
- Replies and tapbacks in either direction. A `--probe` capture on November 11, 2026 showed that Meshpoint's WebSocket events do not include the Meshtastic `reply_id` or `emoji` (tapback) fields, so a tapback arrives as a plain text packet containing the emoji. Meshpoint's `POST /api/messages/send` request also has no fields for them, so replies and tapbacks sent from the phone go out as plain text. This needs a change in Meshpoint; an issue is being raised on the Meshpoint repository.

The planned favorite list will be managed by the bridge and will be independent of Meshpoint dashboard favorites. The intended retention rule is: include nodes heard in the previous five days **or** nodes favorited locally through the Meshtastic app.

See [docs/ROADMAP.md](docs/ROADMAP.md) for the development plan and [docs/TESTING.md](docs/TESTING.md) for the current hardware test record.

## Requirements

- The Linux device that runs Meshpoint, with systemd and Python 3.11 or newer (`python3-venv`). Linux is required: the bridge reads `/sys/class/net` for its receive-only fallback node ID.
- A running, compatible Meshpoint installation (the bridge uses its local API at `127.0.0.1:8080`).
- Network access from the phone to the Meshpoint on TCP port `4403`.
- Internet access during installation, to download the Python packages pinned in `requirements.txt`.

## Install as a service

On the Meshpoint device:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv

git clone https://github.com/csmart30/meshpoint-bridge.git
cd meshpoint-bridge
sudo ./install.sh
```

`install.sh`:

1. creates a `meshpoint-bridge` system user (no login shell, no sudo rights);
2. copies the program to `/opt/meshpoint-bridge` and builds a Python environment there from the pinned `requirements.txt`;
3. runs `--setup`, which asks for the dashboard username and password (and checks they work), the listen address, the client allowlist, mDNS and receive-only mode, and saves them to `/etc/meshpoint-bridge/bridge.env` (mode 600, readable only by the service user);
4. installs, enables and starts `meshpoint-bridge.service`, which starts after Meshpoint at boot.

If a bridge you started by hand is still running, the installer offers to stop it so the service can use port `4403`. An earlier manual install in `/opt/meshpoint-bridge` is upgraded in place: its program files and venv are replaced, other files there are left alone.

| Task | Command |
| --- | --- |
| Follow the logs | `journalctl -u meshpoint-bridge -f` |
| Status | `systemctl status meshpoint-bridge` |
| Change settings | `sudo /opt/meshpoint-bridge/venv/bin/python /opt/meshpoint-bridge/meshpoint_bridge.py --setup`, then `sudo systemctl restart meshpoint-bridge` |
| Show the settings in effect | `sudo -u meshpoint-bridge /opt/meshpoint-bridge/venv/bin/python /opt/meshpoint-bridge/meshpoint_bridge.py --show-config` |
| Upgrade | `git pull`, then `sudo ./install.sh` again (settings are kept) |
| Uninstall | `sudo /opt/meshpoint-bridge/install.sh --uninstall` (asks before deleting the settings) |

The service restarts automatically if it fails, waiting longer between attempts (up to 5 minutes) while Meshpoint is unavailable. It does **not** restart after a settings or password error (exit code 78), because retrying a wrong password would count toward the dashboard lockout: fix it with `--setup` and restart the service.

### Running by hand (development)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 meshpoint_bridge.py -v
```

Stop the service first (`sudo systemctl stop meshpoint-bridge`) so port `4403` is free. Without access to the settings file, the bridge prompts for the dashboard password, or reads `MESHPOINT_USERNAME` / `MESHPOINT_PASSWORD` from the environment. Do not put real credentials in this repository or in a file that Git tracks.

If the dashboard session expires while the bridge is running, it logs in again with the same credentials. If that login is rejected, it waits 5 minutes before trying again, to avoid triggering Meshpoint's login lockout.

To inspect the Meshpoint API shape and WebSocket events, use:

```bash
python3 meshpoint_bridge.py --probe
```

The probe can print node and packet details. Review its output before sharing it, and redact credentials, tokens, and precise location data.

## Connect the Meshtastic app

1. Make sure the phone can reach the Meshpoint on the network (and is in `BRIDGE_ALLOW`, if you set one).
2. In the Meshtastic app, add a Network/TCP connection.
3. Select the bridge's mDNS-advertised node if it appears, or enter the Meshpoint's IP address and TCP port `4403`.
4. Keep the phone and Meshpoint on networks that allow the connection. mDNS discovery generally does not cross routed VLANs without an mDNS reflector; manual IP entry may be needed.

## Node identity

The node ID shown in the Meshtastic app is always the ID Meshpoint transmits with, read from Meshpoint's `/api/config`. Meshpoint sends every packet from that ID, so if the app showed a different one, direct messages to this node would be ignored as addressed to someone else.

- If `BRIDGE_NODE_ID` is set and differs from Meshpoint's ID, an error is logged and Meshpoint's ID is used anyway.
- If Meshpoint's ID changes while the bridge runs (detected after a re-login or WebSocket reconnect, e.g. when Meshpoint restarts), the bridge exits with code 75 and the service restarts it with the new identity.
- If Meshpoint reports **no** transmit ID, the bridge runs in [receive-only mode](#receive-only-mode) and uses `BRIDGE_NODE_ID`, or else the last four bytes of the device's wired Ethernet MAC address, then its Wi-Fi MAC address. Virtual interfaces (loopback, Docker, bridges, VPNs) are ignored.
- If none of these is available, the bridge stops with an error. There is no random ID.

## Receive-only mode

The bridge runs receive-only when:

- Meshpoint reports no transmit node ID, or transmit is disabled on the Meshpoint; or
- `BRIDGE_RX_ONLY=1` is set (for example, for a client that should only monitor the mesh).

In receive-only mode everything Meshpoint hears (text, node info, positions, telemetry) is still forwarded to the app, the app is told transmit is disabled (`lora.tx_enabled = false`), and anything the app tries to send is rejected immediately with a failure acknowledgement. Nothing is passed to Meshpoint's send API. Read-only admin requests from the app are still answered.

In the iPhone Meshtastic app (2.7.28) this shows as **Transmit** turned off in the LoRa settings, and a message sent anyway fails immediately with "no radio interface".

This deliberately differs from a stock Meshtastic node with transmit disabled: the firmware drops the packet without telling the app, which waits and eventually shows "Failed to deliver to mesh". The bridge fails the send right away (routing error `NO_INTERFACE`) so it is clear the message was not sent.

## Settings

Settings are read from `/etc/meshpoint-bridge/bridge.env` at every start (see [`bridge.env.example`](bridge.env.example)). Each setting can also be given as an environment variable of the same name or a command-line option; the order is command line, then environment, then the settings file, then the default.

| Setting | Option | Default | Meaning |
| --- | --- | --- | --- |
| `MESHPOINT_USERNAME` | `--username` | `admin` | Meshpoint dashboard username |
| `MESHPOINT_PASSWORD` | `--password` | none | Meshpoint dashboard password |
| `BRIDGE_LISTEN_HOST` | `--listen-host` | `0.0.0.0` | Address the Meshtastic TCP API listens on |
| `BRIDGE_LISTEN_PORT` | `--listen-port` | `4403` | Meshtastic TCP API port |
| `BRIDGE_ALLOW` | `--allow` | blank (anyone) | Comma-separated IPs/networks allowed to connect |
| `BRIDGE_MDNS` | `--no-mdns` | `1` | Advertise via mDNS |
| `BRIDGE_ADVERTISE_IP` | `--advertise-ip` | automatic | Advertise only this IPv4 address via mDNS |
| `BRIDGE_RX_ONLY` | `--rx-only` | `0` | Receive-only mode |
| `BRIDGE_NODE_ID` | `--node-id` | blank | Node ID used only when Meshpoint reports none |

Other options: `--config PATH` (another settings file), `--setup`, `--show-config`, `--probe`, `--version`, and `-v` / `--verbose` (debug logging, which includes message text).

## Security

**Network access.** Like a stock Meshtastic node, the bridge's TCP API on port `4403` has no authentication: any device that can reach it can connect as a client. This matches how the Meshtastic app expects to connect (it has no way to send a password). Run it only on a trusted LAN and never expose port `4403` to the internet. Unlike a real node, the bridge answers admin requests read-only, so a connected client cannot change radio or Meshpoint settings.

To limit exposure:

- `BRIDGE_LISTEN_HOST` binds the listener to one interface's address instead of all of them.
- `BRIDGE_ALLOW` restricts which IPs/networks may connect. Other devices are refused before anything is exchanged, and cannot disconnect the phone already connected.
- `BRIDGE_MDNS=0` stops advertising the service on the network.
- A host firewall rule is a good extra layer, for example `sudo ufw allow from 192.168.1.0/24 to any port 4403 proto tcp`.

**Credentials.** The dashboard password is stored in plain text in `/etc/meshpoint-bridge/bridge.env`, protected by file permissions (mode 600, owned by the `meshpoint-bridge` user). The service runs as that user with no login shell, no sudo rights and systemd sandboxing, so the bridge cannot use Meshpoint's own privileges. The credentials also stay in the bridge process's memory so it can log in again after a session expires.

**Logs.** By default, logs record message metadata (sender or destination, channel, length, delivery status) but not message text. Message text is logged only with `-v` / `--verbose`. Logs still include node names and IDs; review them before posting them publicly. Login responses are never logged.

**Repository hygiene.** Never commit passwords, tokens, cookies, `.env` files, runtime state, or device-specific logs. The repository `.gitignore` excludes common secret and runtime paths (including `*.env`), but always inspect `git status` before committing.

## Development status

The first public repository snapshot is intended to preserve the working baseline before the next feature changes. The RAK Miner V2 installation is the current reference test environment, not a platform restriction. Functional changes should be made in separate commits and validated against the known-good TX/RX and DM behavior.

## Running the tests

The unit tests use only the standard library plus the packages in `requirements.txt`:

```bash
python3 -m unittest discover -s tests -v
```

## License

This project is licensed under the GNU Affero General Public License v3.0 only (AGPL-3.0-only). See [LICENSE](LICENSE) for the full license text.
