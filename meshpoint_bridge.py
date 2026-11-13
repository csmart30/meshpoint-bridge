#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""
meshpoint_bridge - makes a Meshpoint look like a Meshtastic node (TCP API, port 4403)
so the Meshtastic phone app can connect to it over Wi-Fi.

  Phone app <--TCP 4403 (framed protobuf)--> bridge <--REST + WebSocket :8080--> Meshpoint

Install this on the (Linux) device that runs the Meshpoint.

STATUS: Experimental / pre-release. This bridge has been tested against a live
Meshpoint installation on a RAK Miner V2. The native Meshtastic app has
successfully connected, exchanged broadcast messages on a non-primary channel, and
sent direct messages with recipient acknowledgements. See docs/TESTING.md.

This is an early community bridge, not a complete Meshtastic device implementation.
Only the packet types and admin requests documented in README.md are currently handled.

Install as a service:  sudo ./install.sh        (creates the settings file and starts the service)
Change settings:       sudo /opt/meshpoint-bridge/venv/bin/python /opt/meshpoint-bridge/meshpoint_bridge.py --setup
Show settings:         ... meshpoint_bridge.py --show-config
Probe:                 python3 meshpoint_bridge.py --probe
Phone:                 Meshtastic app > + > Network > (auto-discovered, or enter the Meshpoint's IP)
"""
import argparse
import asyncio
import base64
import getpass
import ipaddress
import json
import logging
import math
import os
import random
import re
import signal
import socket
import sys
import tempfile
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone

import aiohttp
from google.protobuf.message import DecodeError
from meshtastic.protobuf import (
    admin_pb2, channel_pb2, config_pb2, mesh_pb2, portnums_pb2, telemetry_pb2,
)

__version__ = "0.1.0"

DEFAULT_CONFIG = "/etc/meshpoint-bridge/bridge.env"
SERVICE_USER = "meshpoint-bridge"
SERVICE_UNIT = "/etc/systemd/system/meshpoint-bridge.service"
# Exit codes (the systemd unit restarts on failure, except for EXIT_CONFIG: retrying a bad
# setting or a wrong password every few seconds would only trip the dashboard lockout).
EXIT_CONFIG = 78   # EX_CONFIG: fix the settings, then start again
EXIT_RESTART = 75  # EX_TEMPFAIL: Meshpoint's identity changed; restart to pick it up

START1, START2 = 0x94, 0xC3
BROADCAST = 0xFFFFFFFF
NONCE_ONLY_CONFIG = 69420  # app asks for settings/channels only (no node list)
NONCE_ONLY_NODES = 69421   # app asks for the node list only
CONFIG_KINDS = {0: "device", 1: "position", 2: "power", 3: "network",
                4: "display", 5: "lora", 6: "bluetooth", 7: "security"}
MAX_CHANNELS = 8
REST_TIMEOUT = aiohttp.ClientTimeout(total=30)  # per REST call; the websocket has no such limit
EARLY_ACK_HOLD = 30   # seconds to keep an ack that arrived before its send request finished
DM_HISTORY_MAX = 256  # finished DMs remembered, to recognise duplicate and late acks
RELOGIN_BACKOFF = 300  # seconds to wait after a failed re-login (avoids tripping the dashboard lockout)
log = logging.getLogger("bridge")

REGION_MAP = {"US915": "US", "EU868": "EU_868", "ANZ915": "ANZ", "IN865": "IN",
              "KR920": "KR", "SG923": "SG_923"}


# ----------------------------------------------------------------- helpers
def pick(d, *keys, default=None):
    if isinstance(d, dict):
        for k in keys:
            if d.get(k) not in (None, ""):
                return d[k]
    return default


def node_num(v):
    """'!aabbccdd' / 'aabbccdd' / int / '^all' -> uint32 (or None)."""
    if v is None:
        return None
    if isinstance(v, int):
        return v & 0xFFFFFFFF
    s = str(v).strip().lower()
    if s in ("^all", "all", "broadcast"):
        return BROADCAST
    if s.startswith("!") or len(s) == 8:
        try:
            return int(s.lstrip("!"), 16)
        except ValueError:
            return None
    try:
        return int(s) & 0xFFFFFFFF
    except ValueError:
        return None


def node_id(num):
    return "!%08x" % num


def mac_node_num(base="/sys/class/net"):
    """Node number from this machine's MAC address (last 4 bytes, like real Meshtastic nodes).
    Prefers a wired Ethernet interface, then Wi-Fi. Returns None if neither has a usable MAC.
    `base` is the sysfs network directory (a parameter so tests can point it elsewhere)."""
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return None
    wired, wifi = [], []
    for name in names:
        path = os.path.join(base, name)
        if name == "lo" or "/virtual/" in os.path.realpath(path):  # skip loopback, docker, bridges, VPNs...
            continue
        try:
            with open(os.path.join(path, "address")) as f:
                mac = f.read().strip().lower()
        except OSError:
            continue
        if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac) or mac == "00:00:00:00:00:00":
            continue
        (wifi if os.path.isdir(os.path.join(path, "wireless")) else wired).append(mac)
    mac = (wired or wifi or [None])[0]
    if mac is None:
        return None
    num = int(mac.replace(":", "")[-8:], 16)
    return num if num not in (0, BROADCAST) else None


def to_epoch(v):
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:  # no zone given: assume UTC rather than this machine's local time
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def decode_psk(v):
    if not v or not isinstance(v, str):
        return b"\x01"  # Meshtastic "default key" marker
    try:
        return base64.b64decode(v, validate=True)
    except Exception:
        pass
    try:
        return bytes.fromhex(v)
    except ValueError:
        return b"\x01"


def local_ipv4s():
    """All non-loopback IPv4 addresses of this machine (uses ifaddr, which zeroconf already depends on)."""
    try:
        import ifaddr
        return [ip.ip for ad in ifaddr.get_adapters() for ip in ad.ips
                if ip.is_IPv4 and not str(ip.ip).startswith(("127.", "169.254."))]
    except Exception:  # noqa
        try:
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sk.connect(("10.255.255.255", 1))
            ip = sk.getsockname()[0]
            sk.close()
            return [ip]
        except OSError:
            return []


def parse_packet_id(v):
    """Packet ids: ints as-is, strings are hex like '00009dff'. Returns None if unusable."""
    if v is None:
        return None
    if isinstance(v, int):
        return v
    try:
        return int(str(v).lstrip("!"), 16)
    except ValueError:
        return None


def trunc_utf8(text, max_bytes):
    """Cut a string to at most max_bytes of UTF-8 without splitting a character (Meshtastic name limits)."""
    return str(text).encode()[:max_bytes].decode("utf-8", "ignore")


def finite_float(value):
    """Return a finite float for a Meshpoint metric, or None when absent/invalid."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def valid_position(lat, lon):
    """(lat, lon) as floats, or None if missing, out of range, or exactly 0,0 (treated as 'GPS not set')."""
    lat, lon = finite_float(lat), finite_float(lon)
    if lat is None or lon is None or (lat == 0 and lon == 0):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


# Fields from Meshpoint that should refresh the phone's node record when they change.
# Device metrics are exposed on NodeInfo.device_metrics; temperature and humidity
# are sent separately as TELEMETRY_APP / EnvironmentMetrics packets.
NODE_SIG_KEYS = (
    "long_name", "short_name", "latitude", "longitude", "altitude",
    "hardware_model", "public_key", "role", "last_heard",
    "latest_snr", "latest_hops", "latest_battery",
    "latest_voltage", "latest_channel_util", "latest_air_util",
)
ENVIRONMENT_SIG_KEYS = ("latest_temperature", "latest_humidity")


def signature(d, keys):
    return tuple(d.get(k) for k in keys)


def snake_upper(s):
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|[\s-]+", "_", str(s)).upper()


DEFAULT_KEY = bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901")


def expand_psk(psk):
    if len(psk) == 1:
        if psk[0] == 0:
            return b""
        k = bytearray(DEFAULT_KEY)
        k[-1] = (k[-1] + psk[0] - 1) & 0xFF
        return bytes(k)
    if 1 < len(psk) < 16:
        return psk.ljust(16, b"\0")
    if 16 < len(psk) < 32:
        return psk.ljust(32, b"\0")
    return psk


def channel_hash(name, psk):
    """The 1-byte channel hash Meshtastic puts in every packet (name XOR key bytes)."""
    h = 0
    for b in name.encode():
        h ^= b
    for b in expand_psk(psk):
        h ^= b
    return h


def preset_display_name(preset):
    return "".join(w.capitalize() for w in preset.split("_"))


def find_channels(obj):
    """Find a list of channel dicts anywhere inside the Meshpoint config."""
    if isinstance(obj, dict):
        v = obj.get("channels")
        if isinstance(v, list) and v and all(isinstance(c, dict) for c in v):
            return v
        for x in obj.values():
            r = find_channels(x)
            if r:
                return r
    return None


SECRET_KEYS = {"psk", "key", "password", "secret", "token", "jwt", "api_key", "apikey", "psk_b64", "psk_base64"}


def mask(obj):
    if isinstance(obj, dict):
        return {k: (f"<REDACTED len={len(str(v))}>" if k.lower() in SECRET_KEYS and v else mask(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask(x) for x in obj]
    return obj


def resolve_schema(spec, sch, depth=6):
    """Inline $ref targets (also inside lists such as anyOf/allOf) so the probe output is readable."""
    if depth < 0:
        return sch
    if isinstance(sch, list):
        return [resolve_schema(spec, x, depth - 1) for x in sch]
    if not isinstance(sch, dict):
        return sch
    if "$ref" in sch:
        name = sch["$ref"].split("/")[-1]
        target = spec.get("components", {}).get("schemas", {}).get(name, {})
        return {"$": name, **resolve_schema(spec, target, depth - 1)}
    return {k: resolve_schema(spec, v, depth - 1) for k, v in sch.items()}


# ----------------------------------------------------------------- settings
class ConfigError(SystemExit):
    """A settings or credential problem that restarting will not fix. Exits with EXIT_CONFIG,
    which the systemd unit does not restart (so a wrong password is not retried in a loop)."""

    def __init__(self, message):
        super().__init__(EXIT_CONFIG)
        self.message = message

    def __str__(self):
        return self.message


# (attribute, settings-file / environment key, default, kind, description)
SETTINGS = (
    ("username", "MESHPOINT_USERNAME", "admin", "str", "Meshpoint dashboard username"),
    ("password", "MESHPOINT_PASSWORD", None, "secret", "Meshpoint dashboard password (this file must stay chmod 600)"),
    ("listen_host", "BRIDGE_LISTEN_HOST", "0.0.0.0", "str",
     "Address the Meshtastic TCP API listens on (0.0.0.0 = all interfaces)"),
    ("listen_port", "BRIDGE_LISTEN_PORT", 4403, "int", "Meshtastic TCP API port"),
    ("allow", "BRIDGE_ALLOW", "", "str",
     "Comma-separated IPs/networks allowed to connect, e.g. 192.168.1.0/24 (blank = anyone)"),
    ("mdns", "BRIDGE_MDNS", True, "bool", "Advertise the bridge to phones via mDNS (1/0)"),
    ("advertise_ip", "BRIDGE_ADVERTISE_IP", None, "str", "Advertise only this IPv4 address via mDNS (blank = automatic)"),
    ("rx_only", "BRIDGE_RX_ONLY", False, "bool",
     "Receive-only: never transmit, reject sends from the app (1/0)"),
    ("node_id", "BRIDGE_NODE_ID", None, "str",
     "Node id to use only when Meshpoint reports none, e.g. !a1b2c3d4 (blank = MAC address)"),
)
TRUE_WORDS, FALSE_WORDS = ("1", "true", "yes", "on"), ("0", "false", "no", "off")


def parse_bool(value, key):
    if isinstance(value, bool):
        return value
    v = str(value).strip().lower()
    if v in TRUE_WORDS:
        return True
    if v in FALSE_WORDS:
        return False
    raise ConfigError(f"{key}: expected 1 or 0 (or yes/no), got {value!r}")


def unquote_env(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        return re.sub(r"\\(.)", r"\1", inner) if value[0] == '"' else inner
    return value


def quote_env(value):
    value = "" if value is None else str(value)
    if "\n" in value or "\r" in value:
        raise ValueError("settings values cannot contain line breaks")
    if value == "" or re.fullmatch(r"[A-Za-z0-9_./:,@!%+=-]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def read_env_file(path):
    """Read a KEY=VALUE settings file. Blank lines and lines starting with # are ignored;
    values may be wrapped in single or double quotes."""
    values = {}
    with open(path, encoding="utf-8") as f:
        for n, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, sep, value = line.partition("=")
            key = key.strip()
            if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ConfigError(f"{path}, line {n}: expected KEY=VALUE")
            values[key] = unquote_env(value.strip())
    return values


ENV_HEADER = """\
# meshpoint-bridge settings. Read at every start; change with
#   sudo /opt/meshpoint-bridge/venv/bin/python /opt/meshpoint-bridge/meshpoint_bridge.py --setup
# or edit by hand, then: sudo systemctl restart meshpoint-bridge
# Contains the dashboard password: keep it chmod 600, owned by the meshpoint-bridge user.
"""


def write_env_file(path, values):
    """Write the settings file atomically with mode 600, keeping the owner of an existing file
    (or, when run as root, giving a new file to the service user)."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o750, exist_ok=True)
    owner = None
    try:
        st = os.stat(path)
        owner = (st.st_uid, st.st_gid)
    except FileNotFoundError:
        if os.geteuid() == 0:
            try:
                import pwd
                pw = pwd.getpwnam(SERVICE_USER)
                owner = (pw.pw_uid, pw.pw_gid)
            except KeyError:
                pass
    known = {key: desc for _, key, _, _, desc in SETTINGS}
    lines = [ENV_HEADER]
    for key, desc in known.items():
        if key in values:
            lines.append(f"# {desc}\n{key}={quote_env(values[key])}\n")
    extra = [k for k in values if k not in known]
    if extra:
        lines.append("# Other settings\n" + "".join(f"{k}={quote_env(values[k])}\n" for k in extra))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".bridge-env-")
    try:
        os.fchmod(fd, 0o600)
        if owner and os.geteuid() == 0:
            os.fchown(fd, *owner)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def resolve_settings(args, file_values, environ, file_label="settings file"):
    """Fill in each setting: command-line option, then environment variable, then the settings
    file, then the built-in default. Records where each value came from in args.setting_sources."""
    sources = {}
    for attr, key, default, kind, _ in SETTINGS:
        value = getattr(args, attr, None)
        source = "command line"
        if value is None:
            if environ.get(key) not in (None, ""):
                value, source = environ[key], "environment"
            elif file_values.get(key) not in (None, ""):
                value, source = file_values[key], file_label
            else:
                value, source = default, "default"
        if isinstance(value, str):
            if kind == "int":
                try:
                    value = int(value)
                except ValueError:
                    raise ConfigError(f"{key}: expected a number, got {value!r}")
            elif kind == "bool":
                value = parse_bool(value, key)
        setattr(args, attr, value)
        sources[attr] = source
    args.setting_sources = sources
    return args


def parse_allowlist(text):
    """'192.168.1.0/24, 10.0.0.5' -> list of ip_network. Blank means no restriction."""
    nets = []
    for part in re.split(r"[,\s]+", text or ""):
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            raise ConfigError(f"BRIDGE_ALLOW: {part!r} is not an IP address or network (e.g. 192.168.1.0/24)")
    return nets


def ip_allowed(ip, nets):
    """True if `ip` may connect. An empty list allows everyone (like a stock Meshtastic node)."""
    if not nets:
        return True
    try:
        addr = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)  # ::ffff:192.168.1.5 from a dual-stack socket
    if mapped is not None:
        addr = mapped
    return any(addr.version == n.version and addr in n for n in nets)


def load_settings(args, environ=None):
    """Apply the settings file (--config, or DEFAULT_CONFIG if it exists) and validate everything."""
    environ = os.environ if environ is None else environ
    path = getattr(args, "config", None) or DEFAULT_CONFIG
    file_values = {}
    args.config_path = None
    if os.path.exists(path):
        try:
            file_values = read_env_file(path)
            args.config_path = path
        except PermissionError:
            if getattr(args, "config", None):
                raise ConfigError(f"cannot read {path}: permission denied")
            log.warning("cannot read %s (permission denied): using command-line options and environment only. "
                        "To use it from a shell, run as the service user: sudo -u %s ...", path, SERVICE_USER)
    elif getattr(args, "config", None):
        raise ConfigError(f"settings file {path} not found (create it with --setup)")
    resolve_settings(args, file_values, environ, file_label=path)
    args.allow_nets = parse_allowlist(args.allow)
    try:
        ipaddress.ip_address(args.listen_host)
    except ValueError:
        raise ConfigError(f"BRIDGE_LISTEN_HOST: {args.listen_host!r} is not an IP address")
    if args.advertise_ip:
        try:
            ipaddress.IPv4Address(args.advertise_ip)
        except ValueError:
            raise ConfigError(f"BRIDGE_ADVERTISE_IP: {args.advertise_ip!r} is not an IPv4 address")
    if args.node_id and node_num(args.node_id) in (None, 0, BROADCAST):
        raise ConfigError(f"BRIDGE_NODE_ID: {args.node_id!r} is not a node id (expected e.g. !a1b2c3d4)")
    if not 0 < args.listen_port < 65536:
        raise ConfigError(f"BRIDGE_LISTEN_PORT: {args.listen_port} is out of range")
    return args


def format_settings(args):
    """Human-readable effective settings for --show-config (password masked)."""
    src = getattr(args, "setting_sources", {})
    out = [f"meshpoint-bridge {__version__}",
           f"settings file: {args.config_path or '(none)'}",
           f"Meshpoint API: http://{args.meshpoint}"]
    for attr, key, _, kind, _ in SETTINGS:
        v = getattr(args, attr, None)
        if kind == "secret":
            shown = "(set)" if v else "(not set)"
        elif kind == "bool":
            shown = "1" if v else "0"
        else:
            shown = "" if v is None else str(v)
        out.append(f"{key:<20} = {shown:<24} [{src.get(attr, '?')}]")
    return "\n".join(out)


def resolve_identity(tx, configured_id=None, force_rx_only=False, mac_fn=mac_node_num):
    """Pick the node number the app sees. Returns (num, rx_only, source).

    Meshpoint's transmit identity always wins: packets go out on air from that id, so showing the
    app a different one would make DMs to this node look like they are for someone else.
    Without a Meshpoint transmit identity (or with transmit off) the bridge is receive-only and
    uses BRIDGE_NODE_ID, else this machine's MAC address. There is deliberately no random fallback."""
    tx = tx if isinstance(tx, dict) else {}
    mp = node_num(pick(tx, "node_id_hex", "node_id"))
    if mp in (0, BROADCAST):
        mp = None
    configured = node_num(configured_id) if configured_id else None
    rx_only = bool(force_rx_only) or mp is None or tx.get("enabled") is False
    if mp is not None:
        if configured and configured != mp:
            log.error("BRIDGE_NODE_ID %s does not match Meshpoint's node id %s: ignoring it and using %s",
                      node_id(configured), node_id(mp), node_id(mp))
        return mp, rx_only, "Meshpoint"
    if configured:
        return configured, True, "BRIDGE_NODE_ID"
    mac = mac_fn()
    if mac:
        return mac, True, "MAC address"
    raise ConfigError("Meshpoint reports no node id and this machine has no usable MAC address. "
                      "Set BRIDGE_NODE_ID in the settings file, or enable transmit on the Meshpoint.")


# ----------------------------------------------------------------- TX body
def build_tx_body(text, dest, channel, want_ack=False):
    """Body for POST /api/messages/send (SendRequest in Meshpoint's OpenAPI schema).
    destination: "broadcast" for channel messages, otherwise the node id as 8 hex digits
    (the same style /api/nodes uses). If DMs fail, try "!%08x" instead."""
    return {"text": text,
            "destination": "broadcast" if dest == BROADCAST else "%08x" % dest,
            "protocol": "meshtastic",
            "channel": channel,
            "want_ack": bool(want_ack and dest != BROADCAST)}


# ----------------------------------------------------------------- login
LOGIN_PATH = "/api/auth/login"  # confirmed on Meshpoint v0.7.9


async def try_login(session, base, username, password):
    """One login attempt. True = logged in, False = credentials rejected.
    A wrong password counts as exactly ONE failed attempt (the dashboard can lock you out).
    Network hiccups (e.g. a stale keep-alive connection) are retried and never count as a bad password."""
    if not password:
        return False
    for net_try in range(3):
        try:
            async with session.post(base + LOGIN_PATH, json={"username": username, "password": password},
                                    timeout=REST_TIMEOUT) as r:
                text = await r.text()
                log.debug("login -> %s", r.status)  # never log the body: it can contain the session token
                if r.status in (401, 403):
                    return False
                if r.status == 429:
                    raise SystemExit("Meshpoint says too many failed logins (locked out). "
                                     "Wait a few minutes, then try again.")
                if r.status >= 300:
                    raise ConfigError(f"Unexpected login response ({r.status}) from {base}{LOGIN_PATH}. "
                                      "This Meshpoint version may use a different login API.")
                try:
                    data = json.loads(text)
                except ValueError:
                    data = {}
                tok = pick(data, "access_token", "token", "jwt")
                if tok:
                    session.headers["Authorization"] = f"Bearer {tok}"
                # the cookie jar can drop cookies (IP host / Secure flag), so also pass them by hand
                jar = "; ".join(f"{k}={m.value}" for k, m in r.cookies.items())
                if jar:
                    session.headers["Cookie"] = jar
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.debug("login network error (%s), retrying", e)
            await asyncio.sleep(0.5)
            continue
        async with session.get(f"{base}/api/config", timeout=REST_TIMEOUT) as t:
            if t.status == 200:
                log.info("logged in as %s", username)
                return True
        return False
    raise SystemExit(f"Could not reach the Meshpoint at {base}. Check --meshpoint and that the service is running.")


VERIFY_MSG = ("Login failed. Please verify the username and password you use to log in to your "
              "local Meshpoint dashboard (http://<meshpoint-ip>:8080) and try again.")


async def ensure_login(session, base, args):
    """Log in to the Meshpoint dashboard (v0.7.3+ requires auth).
    Interactive flow: default user (admin) + password, again on a wrong password,
    then ask for a username and password, then give up with a hint.
    If --password / MESHPOINT_PASSWORD is given, it is tried once (no prompts).
    Returns (user, password) when a login was needed (so the bridge can log in again
    after the session expires), or None when the Meshpoint needs no login."""
    async with session.get(f"{base}/api/config", timeout=REST_TIMEOUT) as r:
        if r.status != 401:
            return None
    user = args.username or "admin"
    if args.password:
        if await try_login(session, base, user, args.password):
            return user, args.password
        raise ConfigError(VERIFY_MSG)
    if not sys.stdin.isatty():
        raise ConfigError("Meshpoint requires a login. Set MESHPOINT_PASSWORD (and MESHPOINT_USERNAME "
                          "if it is not 'admin') in the settings file (run --setup), or run interactively.")
    for attempt in (1, 2):
        pw = getpass.getpass(f"Enter dashboard password for user '{user}': ")
        if await try_login(session, base, user, pw):
            return user, pw
        print("Login failed." + (" Try again." if attempt == 1 else ""))
    user = input("Enter dashboard username: ").strip() or user
    pw = getpass.getpass(f"Enter dashboard password for user '{user}': ")
    if await try_login(session, base, user, pw):
        return user, pw
    raise ConfigError(VERIFY_MSG)


# ----------------------------------------------------------------- client
class Client:
    """One connected phone. Frames go through a bounded queue drained by a writer task,
    so a stalled phone can never block the Meshpoint -> phone event flow for everyone else."""
    QUEUE_MAX = 512
    WRITE_TIMEOUT = 15  # seconds a single drain() may take before the phone is considered dead

    def __init__(self, writer):
        self.writer = writer
        self.ready = False
        self.peer = None
        self.queue = asyncio.Queue(self.QUEUE_MAX)
        self.task = asyncio.create_task(self._writer())

    @staticmethod
    def frame(fr):
        data = fr.SerializeToString()
        return bytes([START1, START2, len(data) >> 8, len(data) & 0xFF]) + data

    async def send(self, fr):
        """Queue a frame, waiting for room (used for replies and the initial config dump)."""
        data = self.frame(fr)
        while True:
            try:
                self.queue.put_nowait(data)
                return
            except asyncio.QueueFull:
                if self.task.done():
                    raise ConnectionResetError("phone connection closed")
                await asyncio.sleep(0.05)

    def try_send(self, fr):
        """Queue a frame without waiting. False means the phone is too far behind."""
        try:
            self.queue.put_nowait(self.frame(fr))
            return True
        except asyncio.QueueFull:
            return False

    async def _writer(self):
        try:
            while True:
                self.writer.write(await self.queue.get())
                await asyncio.wait_for(self.writer.drain(), self.WRITE_TIMEOUT)
        except (OSError, asyncio.TimeoutError) as e:
            log.info("write to phone %s failed (%s); closing", self.peer, type(e).__name__)
            self.writer.close()

    def close(self):
        self.task.cancel()
        self.writer.close()


# ----------------------------------------------------------------- bridge
class Bridge:
    def __init__(self, args):
        self.args = args
        self.base = f"http://{args.meshpoint}"
        self.session = None
        self.creds = None          # (user, password) for logging in again after the session expires
        self.login_lock = asyncio.Lock()
        self.login_gen = 0         # bumped on every successful re-login
        self.next_relogin = 0.0    # monotonic time before which we won't try again
        self.clients = set()
        self.tasks = set()         # strong refs to fire-and-forget tasks
        self.nodes = {}
        self.node_sig = {}
        self.env_sig = {}
        self.my_num = 0            # set from Meshpoint's config in load_config (see resolve_identity)
        self.rx_only = False       # receive-only: nothing from the app is transmitted
        self.identity_source = None
        self.exit_code = 0         # returned by amain(); non-zero makes systemd restart the bridge
        self.main_task = None
        self.ws_connects = 0
        self.long_name, self.short_name = "Meshpoint", "MPNT"
        self.region, self.preset = "US", "LONG_FAST"
        self.hop_limit = 3
        self.hash_to_idx = {}
        self.slots = {}
        self.seen = deque(maxlen=2000)  # recent packet ids, oldest evicted first
        self.seen_set = set()
        self.last_nodeinfo_tx = 0.0
        self.own_pos = None
        self.pending = {}  # Meshpoint packet id -> (phone packet id, destination node) for DMs awaiting a real ack
        self.early_acks = {}  # Meshpoint packet id -> (source, error, monotonic time): ack seen before the send returned
        self.dm_history = OrderedDict()  # Meshpoint packet id -> (phone packet id, "done" | "timed_out")
        self.fr_id = random.randint(1, 2**31)

    # ---- Meshpoint REST (re-logs in once on a 401)
    async def _request(self, method, path, **kw):
        async with self.session.request(method, self.base + path, timeout=REST_TIMEOUT, **kw) as r:
            return r.status, await r.text()

    async def call(self, method, path, **kw):
        gen = self.login_gen
        status, text = await self._request(method, path, **kw)
        if status == 401 and await self.relogin(gen):
            status, text = await self._request(method, path, **kw)
        return status, text

    async def get_json(self, path):
        status, text = await self.call("GET", path)
        if status >= 300:
            raise RuntimeError(f"GET {path} -> {status}")
        return json.loads(text)

    async def relogin(self, seen_gen):
        """Log in again with the stored credentials. seen_gen is the login generation the failed
        request was made under, so concurrent failures trigger only one login."""
        if not self.creds:
            return False
        async with self.login_lock:
            if self.login_gen != seen_gen:
                return True  # another task already refreshed the session
            if time.monotonic() < self.next_relogin:
                return False
            log.info("Meshpoint session expired; logging in again")
            try:
                ok = await try_login(self.session, self.base, *self.creds)
            except SystemExit as e:  # try_login signals fatal problems this way
                log.warning("re-login failed: %s", e)
                ok = False
            if ok:
                self.login_gen += 1
                self.spawn(self.check_identity())
            else:
                log.warning("re-login failed; not retrying for %ds", RELOGIN_BACKOFF)
                self.next_relogin = time.monotonic() + RELOGIN_BACKOFF
            return ok

    def spawn(self, coro):
        """Run a fire-and-forget task, keeping a reference so it is not garbage-collected early."""
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def request_exit(self, code):
        """Stop the bridge with an exit code (systemd restarts it on a non-zero code)."""
        self.exit_code = code
        if self.main_task is not None:
            self.main_task.cancel()

    async def check_identity(self):
        """After a re-login or WebSocket reconnect (e.g. Meshpoint restarted), make sure Meshpoint's
        identity still matches the one the app was given. If it changed, exit so the service restarts
        and the app reconnects to a node with the right id and transmit state."""
        try:
            cfg = await self.get_json("/api/config")
            num, rx_only, _ = resolve_identity(pick(cfg, "transmit", default={}),
                                               getattr(self.args, "node_id", None),
                                               getattr(self.args, "rx_only", False))
        except ConfigError as e:
            log.error("%s", e)
            num, rx_only = None, True
        except Exception as e:  # noqa
            log.debug("identity check skipped: %r", e)
            return
        if num != self.my_num or rx_only != self.rx_only:
            log.error("Meshpoint identity changed (%s%s -> %s%s); restarting",
                      node_id(self.my_num), " RX-only" if self.rx_only else "",
                      node_id(num) if num else "none", " RX-only" if rx_only else "")
            self.request_exit(EXIT_RESTART)

    # ---- Meshpoint config
    async def load_config(self):
        cfg = await self.get_json("/api/config")
        tx = pick(cfg, "transmit", default={})
        self.my_num, self.rx_only, self.identity_source = resolve_identity(
            tx, getattr(self.args, "node_id", None), getattr(self.args, "rx_only", False))
        if self.rx_only:
            if getattr(self.args, "rx_only", False):
                why = "BRIDGE_RX_ONLY is set"
            elif tx.get("enabled") is False:
                why = "transmit is disabled on the Meshpoint"
            else:
                why = "Meshpoint reports no transmit node id"
            log.warning("RX-only mode (%s): mesh traffic is forwarded to the app, but nothing is transmitted "
                        "and sends from the app are rejected", why)
        self.long_name = pick(tx, "long_name", default=self.long_name)
        self.short_name = pick(tx, "short_name", default=self.short_name)
        self.hop_limit = int(pick(tx, "hop_limit", default=3))
        radio = pick(cfg, "radio", default={})
        reg = str(pick(radio, "region", default="US")).upper()
        self.region = REGION_MAP.get(reg, reg)
        self.preset = snake_upper(pick(radio, "current_preset", "modem_preset", "preset", default="LongFast"))
        dev = pick(cfg, "device", default={})
        pos = valid_position(pick(dev, "latitude"), pick(dev, "longitude"))
        self.own_pos = (*pos, pick(dev, "altitude")) if pos else None
        log.info("Own map pin: %s", "from Meshpoint config (device location)" if self.own_pos
                 else "none (no location in Meshpoint config)")
        default_name = preset_display_name(self.preset)
        chans = find_channels(cfg)
        slots = {}
        for n, c in enumerate(chans or []):
            idx = c.get("index", n)
            if not isinstance(idx, int) or not 0 <= idx < MAX_CHANNELS:
                continue
            name = str(pick(c, "name", default="") or "")[:11]
            psk = decode_psk(pick(c, "psk_b64", "psk", "key", "psk_base64"))
            try:
                hv = int(str(pick(c, "hash")), 16)
            except (TypeError, ValueError):
                hv = channel_hash(name or default_name, psk)
            slots[idx] = (name, psk, bool(c.get("enabled", True)), hv)
        if not slots:
            slots = {0: ("", b"\x01", True, channel_hash(default_name, b"\x01"))}
        self.slots = slots
        self.hash_to_idx = {hv: i for i, (_, _, en, hv) in sorted(slots.items()) if en}
        log.info("Identity %s from %s (%s/%s) region=%s preset=%s hop_limit=%d",
                 node_id(self.my_num), self.identity_source, self.long_name, self.short_name, self.region, self.preset, self.hop_limit)
        log.info("Channels: %s%s", [(i, n or default_name, "0x%02X" % hv, "on" if en else "off")
                                    for i, (n, _, en, hv) in sorted(slots.items())],
                 "" if chans else "   <-- no channel list found in /api/config")

    # ---- protobuf builders
    def nodeinfo(self, num, d=None):
        d = d or {}
        ni = mesh_pb2.NodeInfo()
        ni.num = num
        ni.user.id = node_id(num)
        if num == self.my_num:
            ni.user.long_name, ni.user.short_name = self.long_name, self.short_name
        else:
            ni.user.long_name = trunc_utf8(pick(d, "long_name", "longName", "name", default=node_id(num)), 39)
            ni.user.short_name = trunc_utf8(pick(d, "short_name", "shortName", default=node_id(num)[-4:]), 4)
        ni.user.hw_model = mesh_pb2.HardwareModel.PRIVATE_HW
        if num != self.my_num:
            try:
                ni.user.hw_model = int(pick(d, "hardware_model", "hw_model", default=255))
            except (TypeError, ValueError):
                pass
            try:
                pk = pick(d, "public_key")
                if pk:
                    ni.user.public_key = bytes.fromhex(pk)
            except ValueError:
                pass
            try:
                role = pick(d, "role")
                if role is not None:
                    ni.user.role = int(role)
            except (TypeError, ValueError):
                pass

            # Meshpoint's hop count maps directly to Meshtastic NodeInfo.hops_away.
            hops = pick(d, "latest_hops", "hops_away")
            if hops is not None:
                try:
                    hops = int(float(hops))
                    if hops >= 0:
                        ni.hops_away = hops
                except (TypeError, ValueError, OverflowError):
                    pass

            # DeviceMetrics is part of NodeInfo. Leave unavailable measurements
            # unset rather than fabricating zero values.
            battery = finite_float(pick(d, "latest_battery", "battery_level"))
            voltage = finite_float(pick(d, "latest_voltage", "voltage"))
            channel_util = finite_float(pick(d, "latest_channel_util", "channel_utilization"))
            air_util = finite_float(pick(d, "latest_air_util", "air_util_tx"))
            if battery is not None and battery >= 0:
                # Meshtastic uses values over 100 to indicate an externally powered node.
                ni.device_metrics.battery_level = int(round(battery))
            if voltage is not None and voltage >= 0:
                ni.device_metrics.voltage = voltage
            if channel_util is not None and 0 <= channel_util <= 100:
                ni.device_metrics.channel_utilization = channel_util
            if air_util is not None and 0 <= air_util <= 100:
                # Preserve the value Meshpoint reports; validate its displayed unit during hardware testing.
                ni.device_metrics.air_util_tx = air_util

        if num == self.my_num:  # our own node: always "just heard", pinned at the Meshpoint's registered location
            now = int(time.time())
            ni.last_heard = now
            if self.own_pos:
                lat, lon, alt = self.own_pos
                ni.position.latitude_i, ni.position.longitude_i = int(lat * 1e7), int(lon * 1e7)
                ni.position.time = now
                try:
                    if alt is not None:
                        ni.position.altitude = int(float(alt))
                except (TypeError, ValueError):
                    pass
            return ni
        ni.last_heard = to_epoch(pick(d, "last_heard", "lastHeard", "last_seen", "timestamp"))
        pos = valid_position(pick(d, "latitude", "lat"), pick(d, "longitude", "lon", "lng"))
        if pos:  # missing or 0,0 means "no GPS fix": leave the position unset
            try:
                ni.position.latitude_i, ni.position.longitude_i = int(pos[0] * 1e7), int(pos[1] * 1e7)
                ni.position.time = ni.last_heard
                if pick(d, "altitude") is not None:
                    ni.position.altitude = int(float(pick(d, "altitude")))
                if pick(d, "precision_bits") is not None:
                    ni.position.precision_bits = int(pick(d, "precision_bits"))
            except (TypeError, ValueError, OverflowError):
                pass
        snr = finite_float(pick(d, "latest_snr", "snr"))
        if snr is not None:
            ni.snr = snr
        return ni

    def new_packet(self, frm, to, portnum, payload, request_id=None, ack=False):
        """A decoded MeshPacket from `frm` to `to` with the usual id / rx_time boilerplate filled in."""
        pkt = mesh_pb2.MeshPacket()
        setattr(pkt, "from", frm)
        pkt.to = to
        pkt.id = random.randint(1, 0xFFFFFFFF)
        pkt.rx_time = int(time.time())
        if ack:
            pkt.priority = mesh_pb2.MeshPacket.ACK
        pkt.decoded.portnum = portnum
        pkt.decoded.payload = payload
        if request_id is not None:
            pkt.decoded.request_id = request_id
        return pkt

    def environment_telemetry_packet(self, num, d):
        """Build a TELEMETRY_APP packet for Meshpoint temperature/humidity readings."""
        temperature = finite_float(pick(d, "latest_temperature", "temperature"))
        humidity = finite_float(pick(d, "latest_humidity", "humidity", "relative_humidity"))
        if humidity is not None and not 0 <= humidity <= 100:
            humidity = None
        if temperature is None and humidity is None:
            return None

        heard = to_epoch(pick(d, "last_heard", "lastHeard", "last_seen", "timestamp"))
        sample_time = heard or int(time.time())
        telemetry = telemetry_pb2.Telemetry()
        telemetry.time = sample_time
        if temperature is not None:
            telemetry.environment_metrics.temperature = temperature
        if humidity is not None:
            telemetry.environment_metrics.relative_humidity = humidity

        pkt = self.new_packet(num, BROADCAST, portnums_pb2.TELEMETRY_APP, telemetry.SerializeToString())
        pkt.rx_time = sample_time
        rssi = finite_float(pick(d, "latest_rssi", "rssi"))
        snr = finite_float(pick(d, "latest_snr", "snr"))
        if rssi is not None:
            pkt.rx_rssi = int(round(rssi))
        if snr is not None:
            pkt.rx_snr = snr
        return pkt

    def routing_packet(self, frm, request_id, error=mesh_pb2.Routing.NONE):
        """A routing ack/nak that appears to come from node `frm`, answering the phone's packet `request_id`."""
        return self.new_packet(frm, self.my_num, portnums_pb2.ROUTING_APP,
                               mesh_pb2.Routing(error_reason=error).SerializeToString(),
                               request_id=request_id, ack=True)

    def ack_for(self, pkt, ok=True):
        """The bridge's own ack (or failure) for a packet the phone sent."""
        return self.routing_packet(self.my_num, pkt.id,
                                   mesh_pb2.Routing.NONE if ok else mesh_pb2.Routing.NO_INTERFACE)

    # ---- sending to phones
    def next_id(self):
        self.fr_id += 1
        return self.fr_id

    def build(self, **kw):
        fr = mesh_pb2.FromRadio()
        fr.id = self.next_id()
        for k, v in kw.items():
            getattr(fr, k).CopyFrom(v)
        return fr

    async def to_client(self, c, **kw):
        await c.send(self.build(**kw))

    def broadcast(self, **kw):
        """Queue a frame for every ready phone; one that has fallen too far behind is dropped."""
        fr = self.build(**kw)
        for c in [c for c in self.clients if c.ready]:
            if not c.try_send(fr):
                log.warning("phone %s is not keeping up (send queue full); disconnecting it", c.peer)
                self.clients.discard(c)
                c.close()

    def make_config(self, kind):
        cfg = config_pb2.Config()
        sub = getattr(cfg, kind)
        sub.SetInParent()
        if kind == "device":
            sub.role = config_pb2.Config.DeviceConfig.CLIENT
        elif kind == "lora":
            L = config_pb2.Config.LoRaConfig
            sub.use_preset = True
            sub.tx_enabled = not self.rx_only
            sub.hop_limit = self.hop_limit
            try:
                sub.region = L.RegionCode.Value(self.region)
                sub.modem_preset = L.ModemPreset.Value(self.preset)
            except ValueError:
                log.warning("unknown region/preset %s/%s", self.region, self.preset)
        return cfg

    def make_channel(self, i):
        ch = channel_pb2.Channel()
        ch.index = i
        slot = self.slots.get(i)
        if slot and slot[2]:
            ch.role = channel_pb2.Channel.PRIMARY if i == 0 else channel_pb2.Channel.SECONDARY
            ch.settings.name = slot[0]
            ch.settings.psk = slot[1]
        else:
            ch.role = channel_pb2.Channel.DISABLED
        return ch

    def make_metadata(self):
        md = mesh_pb2.DeviceMetadata()
        md.firmware_version = "2.7.0.0000000"
        md.device_state_version = 24
        md.hw_model = mesh_pb2.HardwareModel.PRIVATE_HW
        return md

    async def send_config(self, c, nonce):
        only_config = nonce == NONCE_ONLY_CONFIG
        only_nodes = nonce == NONCE_ONLY_NODES
        if not only_nodes:
            fr = mesh_pb2.FromRadio()
            fr.id = self.next_id()
            fr.my_info.my_node_num = self.my_num
            fr.my_info.min_app_version = 30200
            await c.send(fr)
            await self.to_client(c, node_info=self.nodeinfo(self.my_num))
            await self.to_client(c, metadata=self.make_metadata())
            for kind in CONFIG_KINDS.values():
                await self.to_client(c, config=self.make_config(kind))
            for i in range(MAX_CHANNELS):
                await self.to_client(c, channel=self.make_channel(i))
        if not only_config:
            for num, d in list(self.nodes.items()):
                await self.to_client(c, node_info=self.nodeinfo(num, d))
                env_packet = self.environment_telemetry_packet(num, d)
                if env_packet is not None:
                    await self.to_client(c, packet=env_packet)
        fr = mesh_pb2.FromRadio()
        fr.id = self.next_id()
        fr.config_complete_id = nonce
        await c.send(fr)
        c.ready = True
        log.info("config sent to client (nonce %s: %s)", nonce,
                 "settings only" if only_config else "nodes only (%d)" % len(self.nodes) if only_nodes
                 else "settings + %d nodes" % len(self.nodes))

    async def queue_status(self, c, pkt):
        """Tell the app its packet was accepted (the Android app waits for this before sending the next one)."""
        qs = mesh_pb2.QueueStatus()
        qs.res = 0
        qs.free = 16
        qs.maxlen = 16
        qs.mesh_packet_id = pkt.id
        await self.to_client(c, queueStatus=qs)

    async def handle_admin(self, c, pkt):
        """Answer the read-only admin requests the app makes. Settings changes are not applied
        (use the Meshpoint dashboard), but if the app asked for an ack we still send one so it
        does not sit waiting for a reply and drop the connection."""
        am = admin_pb2.AdminMessage()
        try:
            am.ParseFromString(pkt.decoded.payload)
        except DecodeError:
            return
        which = am.WhichOneof("payload_variant")
        resp = admin_pb2.AdminMessage()
        answered = True
        if which == "get_owner_request":
            resp.get_owner_response.CopyFrom(self.nodeinfo(self.my_num).user)
        elif which == "get_config_request" and am.get_config_request in CONFIG_KINDS:
            resp.get_config_response.CopyFrom(self.make_config(CONFIG_KINDS[am.get_config_request]))
        elif which == "get_channel_request" and 0 <= am.get_channel_request - 1 < MAX_CHANNELS:
            resp.get_channel_response.CopyFrom(self.make_channel(am.get_channel_request - 1))
        elif which == "get_device_metadata_request":
            resp.get_device_metadata_response.CopyFrom(self.make_metadata())
        else:
            answered = False
            if which and which.startswith(("set_", "begin_", "commit_", "remove_", "reboot", "factory", "nodedb")):
                if which != "set_time_only":
                    log.info("admin: %s NOT applied - change settings in the Meshpoint dashboard instead", which)
            else:
                log.info("admin: %s not supported", which)
        if answered:
            rp = self.new_packet(self.my_num, self.my_num, portnums_pb2.ADMIN_APP,
                                 resp.SerializeToString(), request_id=pkt.id)
            await self.to_client(c, packet=rp)
            log.debug("admin: answered %s", which)
        elif pkt.want_ack:
            await self.to_client(c, packet=self.ack_for(pkt))
            log.debug("admin: acked %s (not applied)", which)

    # ---- phone -> mesh
    async def handle_tx(self, c, pkt):
        if not pkt.HasField("decoded"):
            return
        try:
            await self.queue_status(c, pkt)
        except Exception as e:  # noqa
            log.debug("queue status failed: %s", e)
        port = pkt.decoded.portnum
        if self.rx_only and port != portnums_pb2.ADMIN_APP:  # admin requests are answered locally
            name = portnums_pb2.PortNum.Name(port) if port in portnums_pb2.PortNum.values() else str(port)
            log.info("RX-only: %s from the app NOT sent (receive-only mode never transmits)", name)
            await self.to_client(c, packet=self.ack_for(pkt, ok=False))
            return
        if port == portnums_pb2.TEXT_MESSAGE_APP:
            text = pkt.decoded.payload.decode("utf-8", "replace")
            ok, mp_id = False, None
            try:
                status, body = await self.call("POST", "/api/messages/send",
                                               json=build_tx_body(text, pkt.to, pkt.channel, pkt.want_ack))
                ok = status < 300
                log.debug("send response: %s %s", status, body[:200])
                if not ok:
                    log.warning("send failed: %s %s", status, body[:200])
                else:
                    try:
                        mp_id = parse_packet_id(pick(json.loads(body), "packet_id", "id"))
                    except ValueError:
                        pass
            except Exception as e:  # noqa
                log.warning("send error: %r", e)
            tracked = False
            if ok and pkt.want_ack and pkt.to != BROADCAST and mp_id is not None:
                # DM: wait for the recipient's real ack instead of claiming delivery right away.
                # A nearby node can answer before Meshpoint's send request returns, so check first
                # whether its ack is already here.
                early = self.take_early_ack(mp_id)
                if early:
                    self.complete_dm(mp_id, pkt.id, *early)
                else:
                    self.pending[mp_id] = (pkt.id, pkt.to)
                    self.spawn(self.ack_timeout(mp_id))
                tracked = True
            if pkt.want_ack and not tracked:
                await self.to_client(c, packet=self.ack_for(pkt, ok))
            dest = "broadcast" if pkt.to == BROADCAST else node_id(pkt.to)
            log.info("TX text -> %s ch%d ok=%s%s (%d chars)", dest, pkt.channel, ok,
                     " (waiting for recipient ack)" if tracked else "", len(text))
            log.debug("TX text -> %s: %r", dest, text)
        elif port == portnums_pb2.ADMIN_APP:
            try:
                await self.handle_admin(c, pkt)
            except Exception as e:  # noqa
                log.warning("admin handling error: %r", e)
        elif port == portnums_pb2.NODEINFO_APP:
            await self.share_nodeinfo(c, pkt)
        else:
            name = portnums_pb2.PortNum.Name(port) if port in portnums_pb2.PortNum.values() else str(port)
            if port == portnums_pb2.TRACEROUTE_APP:
                log.info("traceroute to %s NOT sent: Meshpoint's API cannot transmit traceroute packets",
                         node_id(pkt.to))
            else:
                log.info("ignoring unsupported TX portnum %s", name)
            if pkt.want_ack or port == portnums_pb2.TRACEROUTE_APP:  # fail fast instead of leaving the app waiting
                await self.to_client(c, packet=self.ack_for(pkt, ok=False))

    async def ack_timeout(self, mp_id, seconds=60):
        """No confirmation from the recipient: settle the app on 'relayed, not confirmed' instead of hanging."""
        await asyncio.sleep(seconds)
        entry = self.pending.pop(mp_id, None)
        if entry:
            log.info("no confirmation from %s within %ds; app shows 'relayed, not confirmed'",
                     node_id(entry[1]), seconds)
            self.remember_dm(mp_id, entry[0], "timed_out")
            self.broadcast(packet=self.routing_packet(self.my_num, entry[0]))

    def remember_dm(self, mp_id, phone_id, state):
        self.dm_history[mp_id] = (phone_id, state)
        self.dm_history.move_to_end(mp_id)
        while len(self.dm_history) > DM_HISTORY_MAX:
            self.dm_history.popitem(last=False)

    def complete_dm(self, mp_id, phone_id, src, err):
        """Pass the recipient's real confirmation (or failure) for a DM to the app."""
        log.info("DM %s by %s", "confirmed" if err == 0 else f"failed (error {err})", node_id(src))
        self.remember_dm(mp_id, phone_id, "done")
        self.broadcast(packet=self.routing_packet(src, phone_id, err))

    def take_early_ack(self, mp_id):
        """(source, error) of an ack that arrived before its send request finished, if any."""
        now = time.monotonic()
        for k in [k for k, v in self.early_acks.items() if now - v[2] > EARLY_ACK_HOLD]:
            del self.early_acks[k]
        early = self.early_acks.pop(mp_id, None)
        return early[:2] if early else None

    async def on_routing_event(self, d, dp):
        """An ack/nak addressed to the Meshpoint: pass the recipient's real confirmation to the app."""
        src = node_num(d.get("source_id"))
        dst = node_num(d.get("destination_id"))
        if dst != self.my_num or src is None:
            return
        req = parse_packet_id(pick(dp, "request_id", "requestId", "request_packet_id"))
        err = pick(dp, "error_reason", "error", "reason", default=0)
        try:
            err = int(err)
        except (TypeError, ValueError):
            try:
                err = mesh_pb2.Routing.Error.Value(str(err).upper())
            except ValueError:
                err = 0
        if req is None:
            log.debug("routing packet from %s without a request id: %s", node_id(src), json.dumps(dp)[:200])
            return
        entry = self.pending.pop(req, None)
        if entry:
            self.complete_dm(req, entry[0], src, err)
            return
        past = self.dm_history.get(req)
        if past:
            phone_id, state = past
            if state == "timed_out":  # the confirmation came after we stopped waiting: still tell the app
                log.info("late confirmation from %s for a DM that had timed out", node_id(src))
                self.complete_dm(req, phone_id, src, err)
            else:
                log.debug("duplicate routing packet from %s for an already confirmed DM", node_id(src))
            return
        # Possibly the ack for a DM whose send request has not returned yet: hold it briefly.
        self.take_early_ack(-1)  # prunes expired entries
        self.early_acks[req] = (src, err, time.monotonic())
        log.debug("routing packet from %s for %08x not matched yet; holding it %ds", node_id(src), req, EARLY_ACK_HOLD)

    async def share_nodeinfo(self, c, pkt):
        """The app's 'exchange user info': have the Meshpoint broadcast its own NodeInfo now.
        (The Meshpoint can't send a targeted request, so the other node may not reply right away;
        any NodeInfo we hear from it still appears in the app immediately.)"""
        ok = False
        wait = 300 - (time.time() - self.last_nodeinfo_tx)
        if wait > 0:
            log.info("user info request: Meshpoint sent its NodeInfo recently, not repeating (try again in %ds)", wait)
            ok = True
        else:
            try:
                status, body = await self.call("POST", "/api/config/nodeinfo/send")
                ok = status < 300
                log.info("user info request -> Meshpoint broadcasting its NodeInfo: %s %s", status, body[:120])
                if ok:
                    self.last_nodeinfo_tx = time.time()
            except Exception as e:  # noqa
                log.warning("nodeinfo send error: %r", e)
        if pkt.want_ack:
            await self.to_client(c, packet=self.ack_for(pkt, ok))

    # ---- mesh -> phone
    def seen_before(self, pid):
        """True if this packet id was already forwarded; otherwise remember it (bounded, oldest evicted)."""
        if pid in self.seen_set:
            return True
        if len(self.seen) == self.seen.maxlen:
            self.seen_set.discard(self.seen[0])
        self.seen.append(pid)
        self.seen_set.add(pid)
        return False

    def on_node_event(self, ptype, d, dp):
        """Heard a NodeInfo (user info) or Position packet: update that node in the app right away."""
        num = node_num(d.get("source_id"))
        if num is None or num == self.my_num:
            return
        cur = dict(self.nodes.get(num) or {"node_id": "%08x" % num})
        if ptype == "nodeinfo":
            for src, dst in (("long_name", "long_name"), ("short_name", "short_name"),
                             ("hw_model", "hardware_model"), ("public_key", "public_key"), ("role", "role")):
                if dp.get(src) not in (None, ""):
                    cur[dst] = dp[src]
            log.info("RX user info from %s: %s (%s)", node_id(num), cur.get("long_name"), cur.get("short_name"))
        else:
            pos = valid_position(dp.get("latitude"), dp.get("longitude"))
            if pos is None:  # no fix / 0,0: ignore
                return
            cur["latitude"], cur["longitude"] = pos
            for k in ("altitude", "precision_bits"):
                if dp.get(k) is not None:
                    cur[k] = dp[k]
            log.debug("RX position from %s: %s,%s", node_id(num), *pos)
        cur["last_heard"] = d.get("timestamp")
        sig = d.get("signal") or {}
        if sig.get("snr") is not None:
            cur["latest_snr"] = sig["snr"]
        self.nodes[num], self.node_sig[num] = cur, signature(cur, NODE_SIG_KEYS)
        self.broadcast(node_info=self.nodeinfo(num, cur))

    async def on_event(self, ev):
        if ev.get("type") != "packet" or not isinstance(ev.get("data"), dict):
            return  # ignore noise_floor etc.
        d = ev["data"]
        if str(d.get("protocol", "meshtastic")).lower() == "meshcore":
            return
        dp = d.get("decoded_payload")
        ptype = str(d.get("packet_type", "")).lower()
        if ptype in ("nodeinfo", "position") and isinstance(dp, dict):
            self.on_node_event(ptype, d, dp)
            return
        if ptype.startswith("routing") and isinstance(dp, dict):
            await self.on_routing_event(d, dp)
            return
        text = dp if isinstance(dp, str) else (pick(dp, "text", "message", "msg", "body") if isinstance(dp, dict) else None)
        if not isinstance(text, str):
            if "text" in str(d.get("packet_type", "")).lower():
                log.warning("text-type packet but no text field found; decoded_payload fields: %s",
                            sorted(dp) if isinstance(dp, dict) else type(dp).__name__)
            return  # only text is forwarded in this draft
        pid = str(d.get("packet_id", ""))
        if pid and self.seen_before(pid):
            return
        src = node_num(d.get("source_id"))
        dst = node_num(d.get("destination_id"))
        dst = BROADCAST if dst is None else dst
        if src is None or src == self.my_num:
            return
        if dst not in (BROADCAST, self.my_num):
            return
        chan = pick(d, "channel", "channel_index", "channel_idx")
        if chan is None:
            h = d.get("channel_hash")
            chan = self.hash_to_idx.get(h)
            if chan is None:
                if dst == self.my_num and str(h).lower() in ("0", "00", "0x0", "0x00", "none"):
                    pass  # a public-key encrypted DM carries no channel hash
                else:
                    log.warning("text on unknown channel_hash %s; known=%s (using channel 0)", h, self.hash_to_idx)
                chan = 0
        pkt = mesh_pb2.MeshPacket()
        setattr(pkt, "from", src)
        pkt.to = dst
        pkt.channel = int(chan)
        try:
            pkt.id = int(pid, 16)
        except ValueError:
            pkt.id = random.getrandbits(32)
        pkt.rx_time = to_epoch(d.get("timestamp")) or int(time.time())
        pkt.hop_limit = int(d.get("hop_limit", 3) or 0)
        sig = d.get("signal") or {}
        if sig.get("snr") is not None:
            pkt.rx_snr = float(sig["snr"])
        if sig.get("rssi") is not None:
            pkt.rx_rssi = int(float(sig["rssi"]))
        pkt.decoded.portnum = portnums_pb2.TEXT_MESSAGE_APP
        pkt.decoded.payload = text.encode()
        if isinstance(dp, dict):  # replies and tapbacks (field names unverified: check with --probe)
            reply = parse_packet_id(pick(dp, "reply_id", "replyId"))
            if reply:
                pkt.decoded.reply_id = reply & 0xFFFFFFFF
            if pick(dp, "emoji") not in (None, "", 0, False, "0"):
                pkt.decoded.emoji = 1  # flag only; the emoji glyph itself is the text payload
        self.broadcast(packet=pkt)
        log.info("RX text from %s ch%s (%d chars)", node_id(src), chan, len(text))
        log.debug("RX text from %s: %r", node_id(src), text)

    async def ws_loop(self):
        while True:
            gen = self.login_gen
            try:
                async with self.session.ws_connect(f"{self.base}/ws", heartbeat=30) as ws:
                    log.info("connected to Meshpoint websocket")
                    self.ws_connects += 1
                    if self.ws_connects > 1:  # Meshpoint may have restarted with a different identity
                        self.spawn(self.check_identity())
                    async for m in ws:
                        if m.type == aiohttp.WSMsgType.TEXT:
                            try:
                                ev = json.loads(m.data)
                            except ValueError:
                                continue
                            if isinstance(ev, dict):
                                try:  # one bad event must not drop the connection
                                    await self.on_event(ev)
                                except Exception:  # noqa
                                    log.exception("event handling failed")
            except aiohttp.WSServerHandshakeError as e:
                log.warning("ws handshake failed: %s (retrying)", e.status)
                if e.status == 401:
                    await self.relogin(gen)
            except Exception as e:  # noqa
                log.warning("ws error: %s (retrying)", e)
            await asyncio.sleep(5)

    async def poll_nodes(self):
        data = await self.get_json("/api/nodes")
        items = data if isinstance(data, list) else pick(data, "nodes", "items", "data", default=[])
        for d in items:
            if str(pick(d, "protocol", default="meshtastic")).lower() == "meshcore":
                continue
            num = node_num(pick(d, "node_id", "id", "nodeId", "num"))
            if num is None or num == self.my_num:
                continue
            old = self.nodes.get(num)
            if old and to_epoch(pick(old, "last_heard")) > to_epoch(pick(d, "last_heard")):
                d = {**d, **old}  # a live event is newer than what the API returned: keep it
            else:
                d = {**(old or {}), **d}
            self.nodes[num] = d
            sig = signature(d, NODE_SIG_KEYS)
            if self.node_sig.get(num) != sig:
                self.node_sig[num] = sig
                self.broadcast(node_info=self.nodeinfo(num, d))

            # Temperature/humidity are separate Telemetry packets. Avoid repeating
            # an unchanged sensor reading just because last_heard was refreshed.
            env_sig = signature(d, ENVIRONMENT_SIG_KEYS)
            if self.env_sig.get(num) != env_sig:
                self.env_sig[num] = env_sig
                env_packet = self.environment_telemetry_packet(num, d)
                if env_packet is not None:
                    self.broadcast(packet=env_packet)

    async def nodes_loop(self):
        while True:
            await asyncio.sleep(30)
            try:
                await self.poll_nodes()
            except Exception as e:  # noqa
                log.warning("node poll error: %r", e)

    # ---- TCP server
    async def handle_client(self, reader, writer):
        peer = writer.get_extra_info("peername")
        # Allowlist check first: a refused device must not replace (disconnect) the current phone.
        if not ip_allowed(peer[0] if peer else "", getattr(self.args, "allow_nets", None) or []):
            log.warning("refused connection from %s: not in BRIDGE_ALLOW", peer[0] if peer else "unknown address")
            writer.close()
            return
        c = Client(writer)
        # A real Meshtastic node serves one phone at a time: a new connection replaces the old one.
        for old in list(self.clients):
            log.info("closing previous phone connection %s (replaced by %s)", old.peer, peer)
            self.clients.discard(old)
            old.close()
        c.peer = peer
        self.clients.add(c)
        log.info("phone connected: %s", peer)
        sock = writer.get_extra_info("socket")
        if sock is not None:  # notice phones that vanished (killed app, Wi-Fi sleep) instead of keeping dead sockets
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 20)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 6)
            except OSError:
                pass
        reason = "closed by phone"
        try:
            while True:
                # find the 0x94 0xC3 frame start; tolerate repeated 0x94 bytes before the 0xC3
                if (await reader.readexactly(1))[0] != START1:
                    continue
                b = (await reader.readexactly(1))[0]
                while b == START1:
                    b = (await reader.readexactly(1))[0]
                if b != START2:
                    continue
                n = int.from_bytes(await reader.readexactly(2), "big")
                if n > 512:
                    continue
                data = await reader.readexactly(n)
                tr = mesh_pb2.ToRadio()
                try:
                    tr.ParseFromString(data)
                except DecodeError:
                    continue
                kind = tr.WhichOneof("payload_variant")
                log.debug("phone -> bridge: %s", kind)
                if kind == "want_config_id":
                    await self.send_config(c, tr.want_config_id)
                elif kind == "packet":
                    await self.handle_tx(c, tr.packet)
                elif kind == "heartbeat":
                    qs = mesh_pb2.QueueStatus()  # harmless proof-of-life for the app
                    qs.free, qs.maxlen = 16, 16
                    await self.to_client(c, queueStatus=qs)
                elif kind == "disconnect":
                    reason = "phone sent disconnect"
                    break
        except asyncio.CancelledError:
            reason = "bridge shutting down"
            raise
        except asyncio.IncompleteReadError:
            reason = "closed by phone"
        except OSError as e:  # covers resets and timeouts
            reason = f"connection lost ({type(e).__name__}: {e})"
        except Exception as e:  # noqa
            reason = f"error {e!r}"
            log.exception("client handler crashed")
        finally:
            was_current = c in self.clients
            self.clients.discard(c)
            c.close()
            log.info("phone disconnected %s: %s%s", peer, reason, "" if was_current else " (already removed)")

    async def run(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=10),
                                         cookie_jar=aiohttp.CookieJar(unsafe=True),
                                         connector=aiohttp.TCPConnector(force_close=True)) as self.session:
            self.main_task = asyncio.current_task()
            self.creds = await ensure_login(self.session, self.base, self.args)
            await self.log_meshpoint_version()
            await self.load_config()
            await self.poll_nodes()
            log.info("loaded %d nodes", len(self.nodes))
            host = getattr(self.args, "listen_host", "0.0.0.0")
            server = await asyncio.start_server(self.handle_client, host, self.args.listen_port)
            allow = getattr(self.args, "allow_nets", None)
            log.info("Meshtastic TCP API listening on %s:%d (no authentication, like a stock Meshtastic node; "
                     "allowed clients: %s)", host, self.args.listen_port,
                     ", ".join(map(str, allow)) if allow else "any")
            if getattr(self.args, "mdns", True):
                azc = await self.advertise()
            else:
                azc = None
                log.info("mDNS advertising is off (BRIDGE_MDNS=0): add this host's IP manually in the app")
            try:
                await asyncio.gather(server.serve_forever(), self.ws_loop(), self.nodes_loop())
            finally:
                server.close()
                for c in list(self.clients):
                    c.close()
                for t in list(self.tasks):
                    t.cancel()
                if azc:
                    try:  # send mDNS goodbyes so phones drop the entry right away
                        await azc.async_unregister_all_services()
                        await azc.async_close()
                    except Exception as e:  # noqa
                        log.debug("mDNS shutdown error: %r", e)

    async def log_meshpoint_version(self):
        """Log the Meshpoint version (best effort) so test reports and bug reports carry it."""
        try:
            info = await self.get_json("/api/device/status")
            log.info("Meshpoint version %s", pick(info, "firmware_version", "version", default="unknown"))
        except Exception as e:  # noqa
            log.debug("could not read Meshpoint version: %r", e)

    async def advertise(self):
        try:
            from zeroconf import ServiceInfo
            from zeroconf.asyncio import AsyncZeroconf
        except ImportError:
            log.info("zeroconf not installed; add this device's IP manually in the app")
            return None
        host = getattr(self.args, "listen_host", "0.0.0.0")
        if self.args.advertise_ip:
            ips = [self.args.advertise_ip]
        elif host not in ("0.0.0.0", "::", "") and ipaddress.ip_address(host).version == 4:
            ips = [host]  # bound to one address: advertise only that one
        else:
            ips = local_ipv4s()
        if not ips:
            log.warning("could not determine this machine's IP; add it manually in the app")
            return None
        name = f"{self.short_name}_{self.my_num & 0xFFFF:04x}"
        info = ServiceInfo("_meshtastic._tcp.local.", f"{name}._meshtastic._tcp.local.",
                           addresses=[socket.inet_aton(ip) for ip in ips], port=self.args.listen_port,
                           properties={"shortname": self.short_name, "id": node_id(self.my_num)})
        azc = AsyncZeroconf()
        await azc.async_register_service(info)
        log.info("advertising %s via mDNS on %s port %d (if the phone is on a different subnet/VLAN, "
                 "mDNS won't reach it: add this device's IP manually in the app)", name, ", ".join(ips),
                 self.args.listen_port)
        return azc


# ----------------------------------------------------------------- probe
async def probe(args):
    base = f"http://{args.meshpoint}"
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True),
                                     connector=aiohttp.TCPConnector(force_close=True)) as s:
        await ensure_login(s, base, args)
        try:
            async with s.get(base + "/api/config") as r:
                print("\n=== GET /api/config (secrets masked) ===")
                print(json.dumps(mask(await r.json()), indent=1)[:6000])
        except Exception as e:  # noqa
            print("config failed:", e)
        try:
            async with s.get(base + "/openapi.json") as r:
                if r.status == 200:
                    spec = await r.json()
                    paths = spec.get("paths", {})
                    print("\n=== API paths ===")
                    print(", ".join(f"{m.upper()} {p}" for p, v in sorted(paths.items()) for m in v))
                    op = paths.get("/api/messages/send", {}).get("post", {})
                    body = (op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {}))
                    print("\n=== POST /api/messages/send request body schema ===")
                    print(json.dumps(resolve_schema(spec, body), indent=1)[:3000])
                else:
                    print(f"\n(openapi.json -> {r.status}; send schema not available)")
        except Exception as e:  # noqa
            print("openapi failed:", e)
        print("\n=== WS /ws for 90s: showing packet events only (send a TEXT message from another node now) ===")
        try:
            async with s.ws_connect(base + "/ws") as ws:
                end = time.time() + 90
                while time.time() < end:
                    try:
                        m = await ws.receive(timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    if m.type == aiohttp.WSMsgType.TEXT:
                        try:
                            ev = json.loads(m.data)
                        except ValueError:
                            continue
                        if ev.get("type") == "noise_floor":
                            continue
                        dd = ev.get("data")
                        if isinstance(dd, dict):
                            dd.pop("signal", None)
                        print(json.dumps(mask(ev))[:900])
                    elif m.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
        except Exception as e:  # noqa
            print("ws failed:", e)


async def amain(args):
    """Run the bridge (or probe). SIGINT/SIGTERM cancel it cleanly so mDNS goodbyes and sockets get closed.
    Returns the process exit code."""
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    bridge = None
    try:
        if args.probe:
            await probe(args)
        else:
            bridge = Bridge(args)
            await bridge.run()
    except asyncio.CancelledError:
        log.info("shutting down")
    return bridge.exit_code if bridge else 0


# ----------------------------------------------------------------- setup
async def check_login(meshpoint, username, password):
    """Setup helper. Returns (result, config): result True = login works (or none needed),
    False = rejected, None = could not tell; config is Meshpoint's /api/config when it could be read."""
    base = f"http://{meshpoint}"

    async def read_config(s):
        try:
            async with s.get(f"{base}/api/config", timeout=REST_TIMEOUT) as r:
                return await r.json(content_type=None) if r.status == 200 else None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            return None

    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True),
                                     connector=aiohttp.TCPConnector(force_close=True)) as s:
        try:
            async with s.get(f"{base}/api/config", timeout=REST_TIMEOUT) as r:
                if r.status != 401:
                    return True, await read_config(s)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            print(f"  Could not reach the Meshpoint at {base}. Is the meshpoint service running?")
            return None, None
        try:
            ok = await try_login(s, base, username, password)
        except SystemExit as e:  # lockout, unexpected response, unreachable
            print(f"  {e}")
            return None, None
        return ok, (await read_config(s) if ok else None)


def meshpoint_identity(cfg):
    """From Meshpoint's /api/config: (node number or None, long name, short name, transmit enabled)."""
    tx = pick(cfg, "transmit", default={}) if isinstance(cfg, dict) else {}
    tx = tx if isinstance(tx, dict) else {}
    num = node_num(pick(tx, "node_id_hex", "node_id"))
    if num in (0, BROADCAST):
        num = None
    return num, pick(tx, "long_name", default=""), pick(tx, "short_name", default=""), tx.get("enabled") is not False


def ask(prompt, default="", check=None):
    """Prompt with a default; `check` raises ConfigError (or ValueError) for invalid input."""
    while True:
        value = input(f"{prompt} [{default}]: ").strip() if default else input(f"{prompt}: ").strip()
        value = value or default
        if value == "-":  # lets the user clear an optional value
            value = ""
        try:
            if check:
                check(value)
            return value
        except (ConfigError, ValueError) as e:
            print(f"  {e}")


def ask_yes_no(prompt, default):
    while True:
        v = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        if not v:
            return default
        if v in ("y", "yes"):
            return True
        if v in ("n", "no"):
            return False


def run_setup(args):
    """Interactive first-time setup: writes the settings file the service reads at every start."""
    path = args.config or DEFAULT_CONFIG
    if not sys.stdin.isatty():
        raise ConfigError("--setup asks questions, so it must be run in a terminal")
    current = {}
    if os.path.exists(path):
        try:
            current = read_env_file(path)
        except PermissionError:
            raise ConfigError(f"cannot read {path}: run setup with sudo")
    print(f"meshpoint-bridge {__version__} setup. Settings file: {path}")
    print("Press Enter to keep the value in [brackets]; enter - to clear an optional value.\n")

    username = ask("Meshpoint dashboard username",
                   current.get("MESHPOINT_USERNAME") or os.environ.get("MESHPOINT_USERNAME") or "admin")
    saved = current.get("MESHPOINT_PASSWORD") or os.environ.get("MESHPOINT_PASSWORD")
    password = None
    cfg = None
    for attempt in range(3):  # each wrong password counts toward the dashboard lockout, so stop at 3
        pw = getpass.getpass("Meshpoint dashboard password" + (" (Enter keeps the saved one)" if saved else "") + ": ")
        pw = pw or saved
        if not pw:
            print("  A password is needed.")
            continue
        if "\n" in pw or "\r" in pw:
            print("  The password cannot contain line breaks.")
            continue
        result, cfg = asyncio.run(check_login(args.meshpoint, username, pw))
        if result:
            print("  Login OK.")
            password = pw
            break
        if result is None:
            if ask_yes_no("  Could not check the login. Save this password anyway?", False):
                password = pw
                break
            continue
        print("  Login failed: check the username and password you use for the Meshpoint dashboard.")
        saved = None
    if password is None:
        raise ConfigError("setup stopped: no working dashboard password (nothing was saved)")

    # Show the node identity the app will see. Meshpoint's transmit node ID is always used, so
    # there is only something to ask when Meshpoint has none (receive-only mode).
    mp_num, mp_long, mp_short, mp_tx = meshpoint_identity(cfg)
    if mp_num:
        names = f" ({mp_long}/{mp_short})" if mp_long or mp_short else ""
        print(f"  Meshpoint node ID: {node_id(mp_num)}{names}, transmit {'enabled' if mp_tx else 'disabled'}.")
        print("  The Meshtastic app will see this node ID.")
        if not mp_tx:
            print("  Transmit is disabled on the Meshpoint, so the bridge will run receive-only.")
    elif cfg is not None:
        print("  Meshpoint has no transmit node ID, so the bridge will run receive-only.")
    print()

    def check_host(v):
        ipaddress.ip_address(v)

    def check_node(v):
        if v and node_num(v) in (None, 0, BROADCAST):
            raise ValueError("expected a node id like !a1b2c3d4, or - for none")

    listen_host = ask("Listen address for the Meshtastic app (0.0.0.0 = all interfaces)",
                      current.get("BRIDGE_LISTEN_HOST") or "0.0.0.0", check_host)
    allow = ask("Allowed client IPs/networks, comma-separated (blank = anyone)",
                current.get("BRIDGE_ALLOW", ""), parse_allowlist)
    mdns = ask_yes_no("Advertise the bridge via mDNS so the app finds it automatically?",
                      parse_bool(current.get("BRIDGE_MDNS") or "1", "BRIDGE_MDNS"))
    saved_rx_only = parse_bool(current.get("BRIDGE_RX_ONLY") or "0", "BRIDGE_RX_ONLY")
    if (mp_num and mp_tx) or cfg is None:  # only a choice when Meshpoint can transmit (or we could not tell)
        rx_only = ask_yes_no("Receive-only (never transmit; sends from the app are rejected)?", saved_rx_only)
    else:
        rx_only = saved_rx_only  # receive-only anyway: Meshpoint cannot transmit

    if mp_num:
        node = ""  # never used while Meshpoint has a node ID
        if current.get("BRIDGE_NODE_ID"):
            print(f"  Cleared BRIDGE_NODE_ID ({current['BRIDGE_NODE_ID']}): the Meshpoint node ID is always used.")
    else:
        mac = mac_node_num()
        if cfg is None:
            print("  Could not read the Meshpoint node ID. If Meshpoint has one, it will be used;")
            print("  the node ID below is only used if it has none.")
        if mac:
            hint = f"blank = {node_id(mac)}, from this device's MAC address"
        else:
            hint = "required: this device has no usable MAC address"

        def check_needed(v):
            check_node(v)
            if not v and not mac:
                raise ValueError("enter a node ID like !a1b2c3d4; without one the bridge cannot start")
        node = ask(f"Node ID for the app to use ({hint})", current.get("BRIDGE_NODE_ID", ""), check_needed)

    values = dict(current)
    values.update({
        "MESHPOINT_USERNAME": username,
        "MESHPOINT_PASSWORD": password,
        "BRIDGE_LISTEN_HOST": listen_host,
        "BRIDGE_ALLOW": allow,
        "BRIDGE_MDNS": "1" if mdns else "0",
        "BRIDGE_RX_ONLY": "1" if rx_only else "0",
        "BRIDGE_NODE_ID": node,
    })
    try:
        write_env_file(path, values)
    except PermissionError:
        raise ConfigError(f"cannot write {path}: run setup with sudo")
    print(f"\nSaved {path} (mode 600).")
    if os.path.abspath(path) != DEFAULT_CONFIG:
        print(f"Run the bridge with these settings: python3 meshpoint_bridge.py --config {path}")
    elif os.path.exists(SERVICE_UNIT):
        print("Apply the changes with: sudo systemctl restart meshpoint-bridge")
    return 0


def main():
    p = argparse.ArgumentParser(
        description="Meshtastic TCP API bridge for a Meshpoint gateway. Settings come from the "
                    f"settings file ({DEFAULT_CONFIG}, created with --setup); options below override it.")
    p.add_argument("--version", action="version", version=f"meshpoint-bridge {__version__}")
    p.add_argument("--config", default=None, metavar="PATH",
                   help=f"settings file (default: {DEFAULT_CONFIG} if it exists)")
    p.add_argument("--setup", action="store_true", help="create or change the settings file interactively, then exit")
    p.add_argument("--show-config", action="store_true",
                   help="print the settings in effect (password hidden) and where each came from, then exit")
    p.add_argument("--probe", action="store_true", help="dump Meshpoint API shapes and exit")
    p.add_argument("--meshpoint", default="127.0.0.1:8080", help=argparse.SUPPRESS)
    p.add_argument("--username", default=None, help="Meshpoint dashboard username (MESHPOINT_USERNAME)")
    p.add_argument("--password", default=None, help="Meshpoint dashboard password (MESHPOINT_PASSWORD)")
    p.add_argument("--listen-host", default=None, help="listen address (BRIDGE_LISTEN_HOST, default 0.0.0.0)")
    p.add_argument("--listen-port", type=int, default=None, help="listen port (BRIDGE_LISTEN_PORT, default 4403)")
    p.add_argument("--allow", default=None, help="allowed client IPs/networks, comma-separated (BRIDGE_ALLOW)")
    p.add_argument("--no-mdns", dest="mdns", action="store_const", const=False, default=None,
                   help="do not advertise via mDNS (BRIDGE_MDNS=0)")
    p.add_argument("--advertise-ip", default=None, help="advertise only this IPv4 address (BRIDGE_ADVERTISE_IP)")
    p.add_argument("--rx-only", dest="rx_only", action="store_const", const=True, default=None,
                   help="receive-only: never transmit (BRIDGE_RX_ONLY=1)")
    p.add_argument("--node-id", default=None,
                   help="node id used only when Meshpoint reports none, e.g. !a1b2c3d4 (BRIDGE_NODE_ID)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging (includes message text)")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.setup:
            sys.exit(run_setup(args))
        load_settings(args)
        if args.show_config:
            print(format_settings(args))
            return
        log.info("meshpoint-bridge %s (Python %s, meshtastic %s)", __version__,
                 sys.version.split()[0], package_version("meshtastic"))
        if args.config_path:
            log.info("settings: %s", args.config_path)
        sys.exit(asyncio.run(amain(args)) or 0)
    except ConfigError as e:
        log.error("%s", e)
        sys.exit(EXIT_CONFIG)
    except KeyboardInterrupt:
        sys.exit(130)


def package_version(name):
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:  # noqa
        return "unknown"


if __name__ == "__main__":
    main()
