#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""
meshpoint_bridge - makes a Meshpoint look like a Meshtastic node (TCP API, port 4403)
so the Meshtastic phone app can connect to it over Wi-Fi.

  Phone app <--TCP 4403 (framed protobuf)--> bridge <--REST + WebSocket :8080--> Meshpoint

STATUS: Experimental / pre-release. This bridge has been tested against a live
Meshpoint installation on a RAK Miner V2. The native Meshtastic app has
successfully connected, exchanged broadcast messages on a non-primary channel, and
sent direct messages with recipient acknowledgements. See docs/TESTING.md.

This is an early community bridge, not a complete Meshtastic device implementation.
Only the packet types and admin requests documented in README.md are currently handled.

Setup:  pip install -r requirements.txt
Probe:  python3 meshpoint_bridge.py --probe
Run:    python3 meshpoint_bridge.py
Phone:  Meshtastic app > + > Network > (auto-discovered, or enter the bridge host IP)
"""
import argparse
import asyncio
import base64
import getpass
import json
import logging
import math
import os
import random
import re
import socket
import sys
import time
from datetime import datetime

import aiohttp
from google.protobuf.message import DecodeError
from meshtastic.protobuf import (
    admin_pb2, channel_pb2, config_pb2, mesh_pb2, portnums_pb2, telemetry_pb2,
)

START1, START2 = 0x94, 0xC3
BROADCAST = 0xFFFFFFFF
NONCE_ONLY_CONFIG = 69420  # app asks for settings/channels only (no node list)
NONCE_ONLY_NODES = 69421   # app asks for the node list only
CONFIG_KINDS = {0: "device", 1: "position", 2: "power", 3: "network",
                4: "display", 5: "lora", 6: "bluetooth", 7: "security"}
MAX_CHANNELS = 8
log = logging.getLogger("bridge")

REGION_MAP = {"US915": "US", "EU868": "EU_868", "ANZ915": "ANZ", "IN865": "IN",
              "KR920": "KR", "SG923": "SG_923", "EU_868": "EU_868", "SG_923": "SG_923"}


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


def to_epoch(v):
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


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

def node_sig(d):
    return json.dumps({k: d.get(k) for k in NODE_SIG_KEYS}, sort_keys=True, default=str)


def environment_sig(d):
    return json.dumps({k: d.get(k) for k in ENVIRONMENT_SIG_KEYS}, sort_keys=True, default=str)


def finite_float(value):
    """Return a finite float for a Meshpoint metric, or None when absent/invalid."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


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


def resolve_schema(spec, sch, depth=3):
    if not isinstance(sch, dict) or depth < 0:
        return sch
    if "$ref" in sch:
        name = sch["$ref"].split("/")[-1]
        return {"$": name, **resolve_schema(spec, spec.get("components", {}).get("schemas", {}).get(name, {}), depth - 1)}
    return {k: (resolve_schema(spec, v, depth - 1) if isinstance(v, (dict, list)) else v)
            for k, v in sch.items()} if not isinstance(sch, list) else [resolve_schema(spec, x, depth - 1) for x in sch]


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
            async with session.post(base + LOGIN_PATH, json={"username": username, "password": password}) as r:
                text = await r.text()
                log.debug("login -> %s %s", r.status, text[:120])
                if r.status in (401, 403):
                    return False
                if r.status == 429:
                    raise SystemExit("Meshpoint says too many failed logins (locked out). "
                                     "Wait a few minutes, then try again.")
                if r.status >= 300:
                    raise SystemExit(f"Unexpected login response ({r.status}) from {base}{LOGIN_PATH}. "
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
        async with session.get(f"{base}/api/config") as t:
            if t.status == 200:
                log.info("logged in as %s", username)
                return True
        return False
    raise SystemExit(f"Could not reach the Meshpoint at {base}. Check --meshpoint and that the service is running.")


VERIFY_MSG = ("Login failed. Please verify the username and password you use to log in to your "
              "local Meshpoint dashboard (http://<pi-ip>:8080) and try again.")


async def ensure_login(session, base, args):
    """Log in to the Meshpoint dashboard (v0.7.3+ requires auth).
    Interactive flow: default user (admin) + password, again on a wrong password,
    then ask for a username and password, then give up with a hint.
    If --password / MESHPOINT_PASSWORD is given, it is tried once (no prompts)."""
    async with session.get(f"{base}/api/config") as r:
        if r.status != 401:
            return  # auth not required
    user = args.username or "admin"
    if args.password:
        if await try_login(session, base, user, args.password):
            return
        raise SystemExit(VERIFY_MSG)
    if not sys.stdin.isatty():
        raise SystemExit("Meshpoint requires a login. Set MESHPOINT_PASSWORD (and MESHPOINT_USERNAME "
                         "if it is not 'admin') or run interactively.")
    for attempt in (1, 2):
        pw = getpass.getpass(f"Enter dashboard password for user '{user}': ")
        if await try_login(session, base, user, pw):
            return
        print("Login failed." + (" Try again." if attempt == 1 else ""))
    user = input("Enter dashboard username: ").strip() or user
    pw = getpass.getpass(f"Enter dashboard password for user '{user}': ")
    if await try_login(session, base, user, pw):
        return
    raise SystemExit(VERIFY_MSG)


# ----------------------------------------------------------------- client
class Client:
    def __init__(self, writer):
        self.writer = writer
        self.ready = False
        self.peer = None

    async def send(self, fr):
        data = fr.SerializeToString()
        self.writer.write(bytes([START1, START2, len(data) >> 8, len(data) & 0xFF]) + data)
        await self.writer.drain()


# ----------------------------------------------------------------- bridge
class Bridge:
    def __init__(self, args):
        self.args = args
        self.base = f"http://{args.meshpoint}"
        self.session = None
        self.clients = set()
        self.nodes = {}
        self.node_sig = {}
        self.env_sig = {}
        self.my_num = node_num(args.node_id) or 0
        self.long_name, self.short_name = "Meshpoint", "MPNT"
        self.region, self.preset = "US", "LONG_FAST"
        self.hop_limit = 3
        self.hash_to_idx = {}
        self.slots = {}
        self.default_name = "LongFast"
        self.seen_ids = set()
        self.last_nodeinfo_tx = 0.0
        self.own_pos = None
        self.pending = {}  # Meshpoint packet id -> (phone packet id, destination node) for DMs awaiting a real ack
        self.fr_id = random.randint(1, 2**31)

    # ---- Meshpoint config
    async def load_config(self):
        async with self.session.get(f"{self.base}/api/config") as r:
            cfg = await r.json()
        tx = pick(cfg, "transmit", default={})
        if not self.my_num:
            self.my_num = (node_num(pick(tx, "node_id_hex", "node_id")) or random.getrandbits(32))
        self.long_name = pick(tx, "long_name", default=self.long_name)
        self.short_name = pick(tx, "short_name", default=self.short_name)
        self.hop_limit = int(pick(tx, "hop_limit", default=3))
        radio = pick(cfg, "radio", default={})
        reg = str(pick(radio, "region", default="US")).upper()
        self.region = REGION_MAP.get(reg, reg)
        self.preset = snake_upper(pick(radio, "current_preset", "modem_preset", "preset", default="LongFast"))
        dev = pick(cfg, "device", default={})
        try:
            if pick(dev, "latitude") is not None and pick(dev, "longitude") is not None:
                self.own_pos = (float(dev["latitude"]), float(dev["longitude"]), pick(dev, "altitude"))
        except (TypeError, ValueError):
            self.own_pos = None
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
        log.info("Identity %s (%s/%s) region=%s preset=%s hop_limit=%d",
                 node_id(self.my_num), self.long_name, self.short_name, self.region, self.preset, self.hop_limit)
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
        lat, lon = pick(d, "latitude", "lat"), pick(d, "longitude", "lon", "lng")
        if lat is not None and lon is not None:
            try:
                ni.position.latitude_i, ni.position.longitude_i = int(float(lat) * 1e7), int(float(lon) * 1e7)
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

    def environment_telemetry_packet(self, num, d):
        """Build a TELEMETRY_APP packet for Meshpoint temperature/humidity readings."""
        temperature = finite_float(pick(d, "latest_temperature", "temperature"))
        humidity = finite_float(pick(d, "latest_humidity", "humidity", "relative_humidity"))
        if temperature is None and (humidity is None or not 0 <= humidity <= 100):
            return None

        heard = to_epoch(pick(d, "last_heard", "lastHeard", "last_seen", "timestamp"))
        sample_time = heard or int(time.time())
        telemetry = telemetry_pb2.Telemetry()
        telemetry.time = sample_time
        if temperature is not None:
            telemetry.environment_metrics.temperature = temperature
        if humidity is not None and 0 <= humidity <= 100:
            telemetry.environment_metrics.relative_humidity = humidity

        pkt = mesh_pb2.MeshPacket()
        setattr(pkt, "from", num)
        pkt.to = BROADCAST
        pkt.id = random.getrandbits(32)
        pkt.rx_time = sample_time
        rssi = finite_float(pick(d, "latest_rssi", "rssi"))
        snr = finite_float(pick(d, "latest_snr", "snr"))
        if rssi is not None:
            pkt.rx_rssi = int(round(rssi))
        if snr is not None:
            pkt.rx_snr = snr
        pkt.decoded.portnum = portnums_pb2.TELEMETRY_APP
        pkt.decoded.payload = telemetry.SerializeToString()
        return pkt

    def routing_ack(self, pkt):
        ack = mesh_pb2.MeshPacket()
        setattr(ack, "from", self.my_num)
        ack.to = self.my_num
        ack.id = random.getrandbits(32)
        ack.priority = mesh_pb2.MeshPacket.ACK
        ack.rx_time = int(time.time())
        ack.decoded.portnum = portnums_pb2.ROUTING_APP
        ack.decoded.request_id = pkt.id
        ack.decoded.payload = mesh_pb2.Routing(error_reason=mesh_pb2.Routing.NONE).SerializeToString()
        return ack

    def routing_packet(self, frm, request_id, error=mesh_pb2.Routing.NONE):
        """A routing ack/nak that appears to come from node `frm`, answering the phone's packet `request_id`."""
        rp = mesh_pb2.MeshPacket()
        setattr(rp, "from", frm)
        rp.to = self.my_num
        rp.id = random.getrandbits(32)
        rp.priority = mesh_pb2.MeshPacket.ACK
        rp.rx_time = int(time.time())
        rp.decoded.portnum = portnums_pb2.ROUTING_APP
        rp.decoded.request_id = request_id
        rp.decoded.payload = mesh_pb2.Routing(error_reason=error).SerializeToString()
        return rp

    def routing_error(self, pkt, reason=mesh_pb2.Routing.NO_INTERFACE):
        ack = self.routing_ack(pkt)
        ack.decoded.payload = mesh_pb2.Routing(error_reason=reason).SerializeToString()
        return ack

    # ---- sending to phones
    def next_id(self):
        self.fr_id += 1
        return self.fr_id

    async def to_client(self, c, **kw):
        fr = mesh_pb2.FromRadio()
        fr.id = self.next_id()
        for k, v in kw.items():
            getattr(fr, k).CopyFrom(v)
        await c.send(fr)

    async def broadcast(self, **kw):
        for c in [c for c in self.clients if c.ready]:
            try:
                await self.to_client(c, **kw)
            except Exception as e:  # noqa
                log.debug("client send failed: %s", e)

    def make_config(self, kind):
        cfg = config_pb2.Config()
        sub = getattr(cfg, kind)
        sub.SetInParent()
        if kind == "device":
            sub.role = config_pb2.Config.DeviceConfig.CLIENT
        elif kind == "lora":
            L = config_pb2.Config.LoRaConfig
            sub.use_preset = True
            sub.tx_enabled = True
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
            rp = mesh_pb2.MeshPacket()
            setattr(rp, "from", self.my_num)
            rp.to = self.my_num
            rp.id = random.getrandbits(32)
            rp.rx_time = int(time.time())
            rp.decoded.portnum = portnums_pb2.ADMIN_APP
            rp.decoded.payload = resp.SerializeToString()
            rp.decoded.request_id = pkt.id
            await self.to_client(c, packet=rp)
            log.debug("admin: answered %s", which)
        elif pkt.want_ack:
            await self.to_client(c, packet=self.routing_ack(pkt))
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
        if port == portnums_pb2.TEXT_MESSAGE_APP:
            text = pkt.decoded.payload.decode("utf-8", "replace")
            ok, mp_id = False, None
            try:
                async with self.session.post(f"{self.base}/api/messages/send",
                                             json=build_tx_body(text, pkt.to, pkt.channel, pkt.want_ack)) as r:
                    ok = r.status < 300
                    body = await r.text()
                    log.debug("send response: %s %s", r.status, body[:200])
                    if not ok:
                        log.warning("send failed: %s %s", r.status, body[:200])
                    else:
                        try:
                            mp_id = parse_packet_id(pick(json.loads(body), "packet_id", "id"))
                        except ValueError:
                            pass
            except Exception as e:  # noqa
                log.warning("send error: %s", e)
            tracked = False
            if ok and pkt.want_ack and pkt.to != BROADCAST and mp_id is not None:
                # DM: wait for the recipient's real ack instead of claiming delivery right away
                self.pending[mp_id] = (pkt.id, pkt.to)
                asyncio.create_task(self.ack_timeout(mp_id))
                tracked = True
            if pkt.want_ack and not tracked:
                await self.to_client(c, packet=self.routing_ack(pkt) if ok else self.routing_error(pkt))
            dest = "broadcast" if pkt.to == BROADCAST else node_id(pkt.to)
            log.info("TX text -> %s ch%d ok=%s%s: %r", dest, pkt.channel, ok,
                     " (waiting for recipient ack)" if tracked else "", text)
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
                await self.to_client(c, packet=self.routing_error(pkt))

    async def ack_timeout(self, mp_id, seconds=60):
        """No confirmation from the recipient: settle the app on 'relayed, not confirmed' instead of hanging."""
        await asyncio.sleep(seconds)
        entry = self.pending.pop(mp_id, None)
        if entry:
            log.info("no confirmation from %s within %ds; app shows 'relayed, not confirmed'",
                     node_id(entry[1]), seconds)
            await self.broadcast(packet=self.routing_packet(self.my_num, entry[0]))

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
        entry = self.pending.pop(req, None) if req is not None else None
        if not entry:
            log.info("routing packet from %s not matched to a pending DM: %s", node_id(src), json.dumps(dp)[:200])
            return
        phone_id, dest = entry
        log.info("DM %s by %s", "confirmed" if err == 0 else f"failed (error {err})", node_id(src))
        await self.broadcast(packet=self.routing_packet(src, phone_id, err))

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
                async with self.session.post(f"{self.base}/api/config/nodeinfo/send") as r:
                    ok = r.status < 300
                    body = await r.text()
                    log.info("user info request -> Meshpoint broadcasting its NodeInfo: %s %s", r.status, body[:120])
                    if ok:
                        self.last_nodeinfo_tx = time.time()
            except Exception as e:  # noqa
                log.warning("nodeinfo send error: %s", e)
        if pkt.want_ack:
            await self.to_client(c, packet=self.routing_ack(pkt) if ok else self.routing_error(pkt))

    # ---- mesh -> phone
    async def on_node_event(self, ptype, d, dp):
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
            if dp.get("latitude") is None or dp.get("longitude") is None:
                return
            cur["latitude"], cur["longitude"] = dp["latitude"], dp["longitude"]
            for k in ("altitude", "precision_bits"):
                if dp.get(k) is not None:
                    cur[k] = dp[k]
            log.debug("RX position from %s: %s,%s", node_id(num), dp["latitude"], dp["longitude"])
        cur["last_heard"] = d.get("timestamp")
        sig = d.get("signal") or {}
        if sig.get("snr") is not None:
            cur["latest_snr"] = sig["snr"]
        self.nodes[num], self.node_sig[num] = cur, node_sig(cur)
        await self.broadcast(node_info=self.nodeinfo(num, cur))

    async def on_event(self, ev):
        if ev.get("type") != "packet" or not isinstance(ev.get("data"), dict):
            return  # ignore noise_floor etc.
        d = ev["data"]
        if str(d.get("protocol", "meshtastic")).lower() == "meshcore":
            return
        dp = d.get("decoded_payload")
        ptype = str(d.get("packet_type", "")).lower()
        if ptype in ("nodeinfo", "position") and isinstance(dp, dict):
            await self.on_node_event(ptype, d, dp)
            return
        if ptype.startswith("routing") and isinstance(dp, dict):
            await self.on_routing_event(d, dp)
            return
        text = dp if isinstance(dp, str) else (pick(dp, "text", "message", "msg", "body") if isinstance(dp, dict) else None)
        if not isinstance(text, str):
            if "text" in str(d.get("packet_type", "")).lower():
                log.warning("text-type packet but no text field found; decoded_payload=%r", dp)
            return  # only text is forwarded in this draft
        pid = str(d.get("packet_id", ""))
        if pid and pid in self.seen_ids:
            return
        self.seen_ids.add(pid)
        if len(self.seen_ids) > 2000:
            self.seen_ids.clear()
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
        await self.broadcast(packet=pkt)
        log.info("RX text from %s ch%s: %r", node_id(src), chan, text[:40])

    async def ws_loop(self):
        while True:
            try:
                async with self.session.ws_connect(f"{self.base}/ws", heartbeat=30) as ws:
                    log.info("connected to Meshpoint websocket")
                    async for m in ws:
                        if m.type == aiohttp.WSMsgType.TEXT:
                            try:
                                ev = json.loads(m.data)
                            except ValueError:
                                continue
                            if isinstance(ev, dict):
                                await self.on_event(ev)
            except Exception as e:  # noqa
                log.warning("ws error: %s (retrying)", e)
            await asyncio.sleep(5)

    async def poll_nodes(self):
        async with self.session.get(f"{self.base}/api/nodes") as r:
            data = await r.json()
        items = data if isinstance(data, list) else pick(data, "nodes", "items", "data", default=[])
        for d in items:
            if str(pick(d, "protocol", default="meshtastic")).lower() == "meshcore":
                continue
            num = node_num(pick(d, "node_id", "id", "nodeId", "num"))
            if num is None or num == self.my_num:
                continue
            sig = node_sig(d)
            old_sig = self.node_sig.get(num)
            self.nodes[num] = d
            if old_sig != sig:
                self.node_sig[num] = sig
                await self.broadcast(node_info=self.nodeinfo(num, d))

            # Temperature/humidity are separate Telemetry packets. Avoid repeating
            # an unchanged sensor reading just because last_heard was refreshed.
            env_sig = environment_sig(d)
            if self.env_sig.get(num) != env_sig:
                self.env_sig[num] = env_sig
                env_packet = self.environment_telemetry_packet(num, d)
                if env_packet is not None:
                    await self.broadcast(packet=env_packet)

    async def nodes_loop(self):
        while True:
            await asyncio.sleep(30)
            try:
                await self.poll_nodes()
            except Exception as e:  # noqa
                log.warning("node poll error: %s", e)

    # ---- TCP server
    async def handle_client(self, reader, writer):
        c = Client(writer)
        peer = writer.get_extra_info("peername")
        # A real Meshtastic node serves one phone at a time: a new connection replaces the old one.
        for old in list(self.clients):
            log.info("closing previous phone connection %s (replaced by %s)", old.peer, peer)
            self.clients.discard(old)
            old.writer.close()
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
                if (await reader.readexactly(1))[0] != START1:
                    continue
                if (await reader.readexactly(1))[0] != START2:
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
        except (asyncio.IncompleteReadError, OSError) as e:  # OSError covers resets and timeouts
            reason = f"connection lost ({type(e).__name__}: {e})" if not isinstance(e, asyncio.IncompleteReadError) \
                else "closed by phone"
        except Exception as e:  # noqa
            reason = f"error {e!r}"
            log.exception("client handler crashed")
        finally:
            was_current = c in self.clients
            self.clients.discard(c)
            writer.close()
            log.info("phone disconnected %s: %s%s", peer, reason, "" if was_current else " (already replaced)")

    async def run(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=10),
                                         cookie_jar=aiohttp.CookieJar(unsafe=True),
                                         connector=aiohttp.TCPConnector(force_close=True)) as self.session:
            await ensure_login(self.session, self.base, self.args)
            await self.load_config()
            await self.poll_nodes()
            log.info("loaded %d nodes", len(self.nodes))
            server = await asyncio.start_server(self.handle_client, "0.0.0.0", self.args.listen_port)
            azc = await self.advertise()
            try:
                await asyncio.gather(server.serve_forever(), self.ws_loop(), self.nodes_loop())
            finally:
                if azc:
                    await azc.async_close()

    async def advertise(self):
        try:
            from zeroconf import ServiceInfo
            from zeroconf.asyncio import AsyncZeroconf
        except ImportError:
            log.info("zeroconf not installed; add the Pi's IP manually in the app")
            return None
        ips = [self.args.advertise_ip] if self.args.advertise_ip else local_ipv4s()
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
                 "mDNS won't reach it: add the Pi's IP manually in the app)", name, ", ".join(ips),
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
                    for pth in ("/api/messages/send",):
                        op = paths.get(pth, {}).get("post", {})
                        body = (op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {}))
                        print(f"\n=== POST {pth} request body schema ===")
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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--meshpoint", default="127.0.0.1:8080", help="host:port of the Meshpoint API")
    p.add_argument("--listen-port", type=int, default=4403)
    p.add_argument("--advertise-ip", default=None)
    p.add_argument("--node-id", default=None, help="override node id, e.g. !a1b2c3d4")
    p.add_argument("--username", default=os.environ.get("MESHPOINT_USERNAME"),
                   help="Meshpoint dashboard username (default: admin, or set MESHPOINT_USERNAME)")
    p.add_argument("--password", default=os.environ.get("MESHPOINT_PASSWORD"),
                   help="Meshpoint dashboard password (or set MESHPOINT_PASSWORD)")
    p.add_argument("--probe", action="store_true", help="dump Meshpoint API shapes and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(probe(args) if args.probe else Bridge(args).run())


if __name__ == "__main__":
    main()
