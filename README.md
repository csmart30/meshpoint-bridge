# Meshpoint Bridge

A small Python bridge that makes a [Meshpoint](https://github.com/KMX415/meshpoint) gateway appear as a Meshtastic TCP device, allowing the native Meshtastic mobile app to connect to Meshpoint over the local network.

**Project status: experimental / pre-release.** This is an early implementation with real hardware testing, but it is not a complete Meshtastic device implementation.

## Hardware compatibility and testing

**The bridge is designed to work with Meshpoint, not with a particular radio board or gateway enclosure.** It communicates with Meshpoint through its HTTP REST API and WebSocket; it does not directly access the LoRa concentrator, SPI, GPIO, or other hardware-specific interfaces. The bridge can run on the Meshpoint host or on a separate Python-capable machine that can reach the Meshpoint API and accept TCP connections from the Meshtastic app. Set `--meshpoint HOST:PORT` to point it at the Meshpoint instance.

In principle, this means it should work with any platform that runs a compatible Meshpoint release and exposes the API endpoints/events the bridge uses. Compatibility with different Meshpoint versions and deployments still needs testing. The bridge itself is hardware-agnostic; **Meshpoint's own hardware support requirements still apply**. Consult the [Meshpoint hardware matrix](https://github.com/KMX415/meshpoint/blob/main/docs/HARDWARE-MATRIX.md) and [onboarding guide](https://github.com/KMX415/meshpoint/blob/main/docs/ONBOARDING.md) for current platform support and experimental paths.

**Tested installation:** RAK Miner V2 running Meshpoint, with the bridge running on the same host and using the local Meshpoint API. Meshpoint documentation refers to the prebuilt RAK platform as the **RAK Hotspot V2 (RAK7248)**. These names are retained to identify the tested installation accurately; they are not a hardware requirement for the bridge.

On November 10, 2026, the following behavior was verified against the live installation:

- The native Meshtastic app connected to the bridge over TCP port `4403`.
- The bridge completed the app's configuration and node-list exchange (499 nodes were present in that test).
- A broadcast message was transmitted on channel index `2` (the third configured channel in that installation); the primary channel was not used for this test.
- A response received by Meshpoint was forwarded to the native app on channel index `2`.
- Direct messages were transmitted and the bridge received recipient routing acknowledgements.
- NodeInfo and position updates were received from Meshpoint and forwarded to the app.

This is a report of one working installation, not a claim that every Meshpoint version, host platform, Meshtastic app version, or packet type has been tested. The exact OS, Python, Meshpoint, and mobile-app version numbers were not recorded in the test session.

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
- Meshpoint dashboard authentication using an interactive password prompt or environment variables.
- Initial configuration and node database exchange with the Meshtastic app.
- Text-message TX through Meshpoint's REST API.
- Text-message RX through Meshpoint's WebSocket.
- Direct-message acknowledgement tracking when Meshpoint returns a correlatable packet ID and a recipient acknowledgement is observed.
- Basic NodeInfo and position forwarding.
- Read-only responses to selected admin queries.

## Current limitations / planned work

The following are **not implemented in this initial repository snapshot**:

- Five-day node-list retention with older nodes retained only when locally favorited.
- Persisting favorite/unfavorite actions from the native Meshtastic app to a bridge-local favorites file. The current bridge acknowledges `set_favorite_node` but does not persist it.
- A systemd service, boot-time enablement, installer, or protected credential-file setup.
- Increase support for more Meshtastic packet types and administration operations (traceroute, node info exchanges).

The planned favorite list will be managed by the bridge and will be independent of Meshpoint dashboard favorites. The intended retention rule is: include nodes heard in the previous five days **or** nodes favorited locally through the Meshtastic app.

See [docs/ROADMAP.md](docs/ROADMAP.md) for the development plan and [docs/TESTING.md](docs/TESTING.md) for the current hardware test record.

## Requirements

- A machine capable of running Python 3 and a working Python virtual environment. This may be the Meshpoint host or a separate machine.
- A running, compatible Meshpoint installation with its HTTP API and WebSocket reachable from the bridge.
- Network access from the phone to the bridge host on TCP port `4403`.
- Python packages listed in `requirements.txt`.

The bridge defaults to `127.0.0.1:8080` for Meshpoint and `4403` for the Meshtastic TCP listener.

## Install and run

These steps run the bridge manually. A supported systemd installer and persistent credential setup are planned but are not included yet.

```bash
sudo apt update
sudo apt install -y git python3 python3-venv


git clone https://github.com/csmart30/meshpoint-bridge.git
cd meshpoint-bridge

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Run against Meshpoint on this same host; use its LAN hostname/IP if it is elsewhere
python3 meshpoint_bridge.py --meshpoint 127.0.0.1:8080 -v
```

The first run may prompt for the Meshpoint dashboard password. The script also accepts `MESHPOINT_USERNAME` and `MESHPOINT_PASSWORD` environment variables. Do not put real credentials in this repository or in a file that Git tracks.

To inspect the Meshpoint API shape and WebSocket events, use:

```bash
python3 meshpoint_bridge.py --meshpoint 127.0.0.1:8080 --probe
```

The probe can print node and packet details. Review its output before sharing it, and redact credentials, tokens, and precise location data.

## Connect the Meshtastic app

1. Make sure the phone can reach the host running the bridge.
2. In the Meshtastic app, add a Network/TCP connection.
3. Select the bridge's mDNS-advertised node if it appears, or enter the bridge host's IP address and TCP port `4403`.
4. Keep the phone and bridge on networks that allow the connection. mDNS discovery generally does not cross routed VLANs without an mDNS reflector; manual IP entry may be needed.

## Command-line options

```text
--meshpoint HOST:PORT   Meshpoint API address (default: 127.0.0.1:8080)
--listen-port PORT      Meshtastic TCP port (default: 4403)
--advertise-ip ADDRESS  Advertise one specific IPv4 address via mDNS
--node-id NODE_ID       Override the virtual node ID
--username USERNAME     Meshpoint dashboard username
--password PASSWORD     Meshpoint dashboard password
--probe                 Inspect Meshpoint config, API schema, and WebSocket events
-v, --verbose           Enable debug logging
```

The username and password can also be supplied using `MESHPOINT_USERNAME` and `MESHPOINT_PASSWORD`.

## Security notes

- The TCP listener binds to `0.0.0.0` by default and currently has no client authentication. Restrict TCP port `4403` to trusted LAN clients with your firewall/network configuration; do not expose it directly to the internet.
- Keep Meshpoint's dashboard/API on a trusted network. The default API URL is loopback (`127.0.0.1:8080`) when the bridge runs on the Meshpoint host.
- Never commit passwords, tokens, cookies, `.env` files, runtime state, or device-specific logs. The repository `.gitignore` excludes common secret and runtime paths, but always inspect `git status` before committing.
- Normal logging currently includes message text. Be mindful when sharing logs, especially when using DMs.

## Development status

The first public repository snapshot is intended to preserve the working baseline before the next feature changes. The RAK Miner V2 installation is the current reference test environment, not a platform restriction. Functional changes should be made in separate commits and validated against the known-good TX/RX and DM behavior.

## License

No license has been selected for this repository yet. Plans to add the license type are in progress. A public GitHub repository without a license is viewable, but that does not grant general reuse rights.
