#!/usr/bin/env python3
"""
vhf_prop_aprs.py - Caribbean VHF/UHF propagation announcer for APRS

Polls PSKReporter for recent 2m (144-148 MHz) and 70cm (420-450 MHz) digital
mode spots (FT8, FT4, JT65, MSK144, Q65, ...), picks out long-distance paths
between the Caribbean and North America, Europe or Latin America, and
announces openings as APRS bulletins via APRS-IS and/or an RF TNC (KISS-TCP,
e.g. Direwolf).

Typical openings this catches: tropo ducting (Caribbean <-> Florida / Gulf),
TEP (Caribbean <-> South America), sporadic-E, and the rare trans-Atlantic duct.

Requires Python 3.8+ and the standard library only.

Examples
  # Look but don't transmit (prints what it would send)
  python3 vhf_prop_aprs.py --call 8P6XX --dry-run --once

  # Run continuously, announce on APRS-IS
  python3 vhf_prop_aprs.py --call 8P6XX-10

  # Also key up RF via Direwolf's KISS port
  python3 vhf_prop_aprs.py --call 8P6XX-10 --kiss-host 127.0.0.1 --kiss-port 8001

  # Test offline against a saved PSKReporter XML response
  python3 vhf_prop_aprs.py --call 8P6XX --dry-run --once --input sample.xml

Licensing: transmitting on RF/APRS-IS requires a valid amateur licence;
--call must be your own callsign.
"""

import argparse
import json
import math
import os
import socket
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field

VERSION = "1.0"
TOCALL = "APZPRP"  # APZ = experimental software tocall
PSKR_URL = "https://retrieve.pskreporter.info/query"
MIN_QUERY_INTERVAL = 300  # PSKReporter asks for no more than one query / 5 min

BANDS = {
    "2m": (144_000_000, 148_000_000),
    "70cm": (420_000_000, 450_000_000),
}

TARGET_REGIONS = ("NA", "EU", "LA")
REGION_NAMES = {"CAR": "Caribbean", "NA": "N.America", "EU": "Europe", "LA": "Lat.America"}

# Bulletin slot per (band, region) so each opening has its own line on screens
BLN_SLOT = {
    ("2m", "NA"): "1", ("2m", "EU"): "2", ("2m", "LA"): "3",
    ("70cm", "NA"): "4", ("70cm", "EU"): "5", ("70cm", "LA"): "6",
}

# --------------------------------------------------------------------------
# Region classification
# --------------------------------------------------------------------------

# DXCC prefixes (as PSKReporter's senderDXCCCode / receiverDXCCCode) and
# callsign prefixes that put a station in the Caribbean basin.
CARIBBEAN_DXCC = {
    "8P", "J3", "J6", "J7", "J8", "9Y", "V2", "V4", "VP2E", "VP2M", "VP2V",
    "KP1", "KP2", "KP4", "KP5", "FG", "FM", "FS", "FJ", "PJ2", "PJ4", "PJ5",
    "PJ7", "P4", "HI", "HH", "6Y", "CO", "C6", "ZF", "VP5", "YV0", "HK0", "KG4",
}
CARIBBEAN_CALL_PREFIXES = sorted(CARIBBEAN_DXCC | {
    "9Z", "KP3", "NP3", "WP3", "NP4", "WP4", "NP2", "WP2", "CM", "CL", "T4",
    "PJ6", "PJ8", "4V",
}, key=len, reverse=True)

# Mexico / Central & South America DXCC prefixes (subset - grid fallback
# catches the rest)
LATAM_DXCC = {
    "XE", "TI", "TG", "YN", "HR", "YS", "HP", "V3", "HK", "YV", "HC", "OA",
    "CP", "PY", "CE", "LU", "CX", "ZP", "8R", "PZ", "FY",
}


def grid_to_latlon(grid):
    """Maidenhead locator (4/6/8 chars) -> (lat, lon) of square centre."""
    if not grid or len(grid) < 4:
        return None
    g = grid.strip()
    try:
        lon = (ord(g[0].upper()) - ord("A")) * 20 - 180
        lat = (ord(g[1].upper()) - ord("A")) * 10 - 90
        lon += int(g[2]) * 2
        lat += int(g[3]) * 1
        size_lon, size_lat = 2.0, 1.0
        if len(g) >= 6 and g[4].isalpha() and g[5].isalpha():
            lon += (ord(g[4].lower()) - ord("a")) * (2 / 24)
            lat += (ord(g[5].lower()) - ord("a")) * (1 / 24)
            size_lon, size_lat = 2 / 24, 1 / 24
        lon += size_lon / 2
        lat += size_lat / 2
    except (ValueError, IndexError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def haversine_km(a, b):
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    d = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(d))


def in_caribbean_box(lat, lon):
    # Greater + Lesser Antilles, Bahamas, ABC islands. Excludes mainland
    # Florida/Yucatan/Central America where possible.
    if 9.8 <= lat <= 27.5 and -85.5 <= lon <= -59.0:
        if lat >= 24.5 and lon < -79.8:          # Florida Keys/mainland
            return False
        if lon < -83.0 and lat < 21.0:            # Central American coast
            return False
        if lat < 11.0 and lon < -62.0 and not (lon < -68.5 and lat > 12):
            # Venezuelan coast (keep ABC islands at ~12.5N handled below)
            return False
        return True
    return False


def region_from_latlon(lat, lon):
    if in_caribbean_box(lat, lon):
        return "CAR"
    if 34.0 <= lat <= 72.0 and -25.0 <= lon <= 45.0:
        return "EU"
    if -170.0 <= lon <= -50.0:
        if lat >= 32.0:
            return "NA"
        if 24.4 <= lat < 32.0 and -98.0 < lon < -79.5:   # Florida, Gulf coast, S. Texas
            return "NA"
        if -60.0 <= lat < 32.0:
            return "LA"
    if -60.0 <= lat < 15.0 and -82.0 <= lon <= -30.0:
        return "LA"
    return None


def caribbean_by_call(call):
    c = (call or "").upper().split("/")[0]
    return any(c.startswith(p) for p in CARIBBEAN_CALL_PREFIXES)


def classify(call, grid, dxcc_code):
    """Return region code or None. Caribbean is matched first by DXCC/callsign."""
    code = (dxcc_code or "").upper()
    if code in CARIBBEAN_DXCC or (not code and caribbean_by_call(call)):
        return "CAR"
    pos = grid_to_latlon(grid)
    if code in LATAM_DXCC:
        return "LA"
    if pos:
        r = region_from_latlon(*pos)
        # Don't let a grid box label a non-Caribbean DXCC as Caribbean
        if r == "CAR" and code:
            return "NA" if code == "K" else "LA"
        return r
    return None


# --------------------------------------------------------------------------
# PSKReporter fetch / parse
# --------------------------------------------------------------------------

@dataclass
class Spot:
    tx: str
    tx_grid: str
    rx: str
    rx_grid: str
    freq: int
    mode: str
    snr: str
    ts: int
    band: str = ""
    tx_region: str = ""
    rx_region: str = ""
    km: float = 0.0


def fetch_pskreporter(window_s, contact, timeout=60):
    lo = min(b[0] for b in BANDS.values())
    hi = max(b[1] for b in BANDS.values())
    params = {
        "frange": f"{lo}-{hi}",
        "flowStartSeconds": f"-{window_s}",
        "rronly": "1",
        "noactive": "1",
        "rptlimit": "10000",
        "appcontact": contact,
    }
    url = PSKR_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": f"vhf_prop_aprs/{VERSION} ({contact})"})
    ctx = None
    try:
        import ssl
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def band_for(freq):
    for name, (lo, hi) in BANDS.items():
        if lo <= freq <= hi:
            return name
    return None


def parse_spots(xml_bytes):
    root = ET.fromstring(xml_bytes)
    spots = []
    for el in root.iter("receptionReport"):
        a = el.attrib
        try:
            freq = int(a.get("frequency", "0"))
        except ValueError:
            continue
        band = band_for(freq)
        if not band:
            continue
        tx_grid, rx_grid = a.get("senderLocator", ""), a.get("receiverLocator", "")
        p1, p2 = grid_to_latlon(tx_grid), grid_to_latlon(rx_grid)
        if not (p1 and p2):
            continue
        s = Spot(
            tx=a.get("senderCallsign", "?"), tx_grid=tx_grid,
            rx=a.get("receiverCallsign", "?"), rx_grid=rx_grid,
            freq=freq, mode=a.get("mode", "?"), snr=a.get("sNR", ""),
            ts=int(a.get("flowStartSeconds", "0") or 0), band=band,
        )
        s.tx_region = classify(s.tx, tx_grid, a.get("senderDXCCCode")) or ""
        s.rx_region = classify(s.rx, rx_grid, a.get("receiverDXCCCode")) or ""
        s.km = haversine_km(p1, p2)
        spots.append(s)
    return spots


# --------------------------------------------------------------------------
# Opening detection
# --------------------------------------------------------------------------

@dataclass
class Opening:
    band: str
    region: str
    paths: set = field(default_factory=set)
    modes: set = field(default_factory=set)
    best: Spot = None

    @property
    def score(self):
        return len(self.paths)


def detect(spots, min_km, min_paths, strong_km):
    """Group Caribbean<->target-region spots into openings per (band, region)."""
    ops = {}
    for s in spots:
        regions = {s.tx_region, s.rx_region}
        if "CAR" not in regions or s.km < min_km:
            continue
        other = (regions - {"CAR"}) or set()
        if len(other) != 1:
            continue  # intra-Caribbean or unknown far end
        region = other.pop()
        if region not in TARGET_REGIONS:
            continue
        key = (s.band, region)
        op = ops.setdefault(key, Opening(s.band, region))
        op.paths.add(tuple(sorted((s.tx.upper(), s.rx.upper()))))
        op.modes.add(s.mode)
        if op.best is None or s.km > op.best.km:
            op.best = s
    # Keep those meeting the threshold: enough distinct paths, or one very long one
    return {k: o for k, o in ops.items()
            if o.score >= min_paths or (o.best and o.best.km >= strong_km)}


def bulletin_text(op):
    b = op.best
    n = op.score
    txt = (f"{op.band} OPEN CAR-{op.region} {n} path{'s' if n != 1 else ''} "
           f"best {b.tx}>{b.rx} {int(round(b.km))}km {b.mode}")
    if len(txt) > 67:  # APRS message text limit
        txt = f"{op.band} OPEN CAR-{op.region} {n}p {b.tx}>{b.rx} {int(round(b.km))}km"
    return txt[:67]


def closed_text(band, region):
    return f"{band} CAR-{region} no recent long-haul spots - closed?"[:67]


# --------------------------------------------------------------------------
# APRS output
# --------------------------------------------------------------------------

def aprs_passcode(call):
    base = call.upper().split("-")[0]
    h = 0x73E2
    for i in range(0, len(base), 2):
        h ^= ord(base[i]) << 8
        if i + 1 < len(base):
            h ^= ord(base[i + 1])
    return h & 0x7FFF


def bln_addressee(slot, group):
    return f"BLN{slot}{group}"[:9].ljust(9)


def info_field(addressee, text):
    return f":{addressee}:{text}"


class AprsIs:
    def __init__(self, call, passcode, server, port):
        self.call, self.passcode, self.server, self.port = call.upper(), passcode, server, port

    def send(self, infos):
        with socket.create_connection((self.server, self.port), timeout=30) as s:
            f = s.makefile("rwb")
            f.readline()  # server banner
            login = f"user {self.call} pass {self.passcode} vers vhf_prop_aprs {VERSION}\r\n"
            f.write(login.encode()); f.flush()
            resp = f.readline().decode(errors="replace").strip()
            if "unverified" in resp.lower() or "verified" not in resp.lower():
                raise RuntimeError(f"APRS-IS login not verified: {resp}")
            for info in infos:
                pkt = f"{self.call}>{TOCALL},TCPIP*:{info}\r\n"
                f.write(pkt.encode("ascii", "replace")); f.flush()
                time.sleep(1)


def _ax25_addr(call, last=False, command=False):
    base, _, ssid = call.upper().partition("-")
    ssid = int(ssid or 0)
    b = bytes((ord(c) << 1) for c in base.ljust(6)[:6])
    sb = 0x60 | (ssid << 1) | (0x80 if command else 0) | (1 if last else 0)
    return b + bytes([sb])


def kiss_frame(src, dst, path, info):
    calls = [dst, src] + path
    addr = b""
    for i, c in enumerate(calls):
        addr += _ax25_addr(c, last=(i == len(calls) - 1), command=(i == 0))
    frame = addr + b"\x03\xf0" + info.encode("ascii", "replace")
    frame = frame.replace(b"\xdb", b"\xdb\xdd").replace(b"\xc0", b"\xdb\xdc")
    return b"\xc0\x00" + frame + b"\xc0"


class KissTcp:
    def __init__(self, call, host, port, path):
        self.call, self.host, self.port, self.path = call.upper(), host, port, path

    def send(self, infos):
        with socket.create_connection((self.host, self.port), timeout=15) as s:
            for info in infos:
                s.sendall(kiss_frame(self.call, TOCALL, self.path, info))
                time.sleep(3)


# --------------------------------------------------------------------------
# State (cooldown / de-dup)
# --------------------------------------------------------------------------

def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)


def plan_announcements(openings, state, now, repeat_s, announce_close, group):
    """Decide which bulletins to send this cycle; update state in place."""
    out = []
    active_keys = set()
    for (band, region), op in sorted(openings.items()):
        key = f"{band}:{region}"
        active_keys.add(key)
        prev = state.get(key, {})
        text = bulletin_text(op)
        is_new = not prev.get("open")
        stale = now - prev.get("last_sent", 0) >= repeat_s
        grown = op.score >= 2 * max(1, prev.get("score", 1)) and op.score >= 3
        if is_new or stale or grown:
            out.append((BLN_SLOT[(band, region)], text))
            state[key] = {"open": True, "last_sent": now, "score": op.score,
                          "opened": prev.get("opened", now) if not is_new else now}
        else:
            prev["score"] = max(prev.get("score", 0), op.score)
    for key, st in list(state.items()):
        if st.get("open") and key not in active_keys:
            band, region = key.split(":")
            if announce_close:
                out.append((BLN_SLOT[(band, region)], closed_text(band, region)))
            state[key] = {"open": False, "last_sent": now, "score": 0}
    return [(bln_addressee(slot, group), text) for slot, text in out]


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%SZ ", time.gmtime()) + msg, flush=True)


def run_cycle(args, outputs, state):
    if args.input:
        with open(args.input, "rb") as f:
            raw = f.read()
    else:
        raw = fetch_pskreporter(args.window * 60, args.contact)
    spots = parse_spots(raw)
    car = [s for s in spots if "CAR" in (s.tx_region, s.rx_region)]
    log(f"{len(spots)} 2m/70cm spots, {len(car)} touching the Caribbean")

    openings = detect(spots, args.min_km, args.min_paths, args.strong_km)
    for (band, region), op in sorted(openings.items()):
        b = op.best
        log(f"  OPENING {band} CAR<->{REGION_NAMES[region]}: {op.score} paths, "
            f"best {b.tx}({b.tx_grid}) -> {b.rx}({b.rx_grid}) {b.km:.0f} km {b.mode} {b.snr}dB")
    if not openings:
        log("  no qualifying openings")

    now = int(time.time())
    msgs = plan_announcements(openings, state, now, args.repeat * 60,
                              args.announce_close, args.group)
    infos = [info_field(addr, text) for addr, text in msgs]
    for info in infos:
        log(f"  TX {args.call.upper()}>{TOCALL}:{info}")
    if infos and not args.dry_run:
        for out in outputs:
            try:
                out.send(infos)
            except Exception as e:  # keep running if one output fails
                log(f"  ! {type(out).__name__} send failed: {e}")
    return openings


def main():
    p = argparse.ArgumentParser(description="Announce Caribbean 2m/70cm openings on APRS from PSKReporter data")
    p.add_argument("--call", required=True, help="your callsign-SSID for APRS, e.g. 8P6XX-10")
    p.add_argument("--passcode", type=int, help="APRS-IS passcode (computed from --call if omitted)")
    p.add_argument("--contact", help="contact e-mail/callsign sent to PSKReporter (default: --call)")
    p.add_argument("--server", default="rotate.aprs2.net")
    p.add_argument("--port", type=int, default=14580)
    p.add_argument("--no-aprsis", action="store_true", help="don't send to APRS-IS")
    p.add_argument("--kiss-host", help="KISS-TCP TNC host (e.g. Direwolf) for RF transmission")
    p.add_argument("--kiss-port", type=int, default=8001)
    p.add_argument("--rf-path", default="WIDE2-1", help="RF digipeater path, comma-separated ('' for none)")
    p.add_argument("--group", default="VHF", help="bulletin group name (BLNnGROUP), up to 5 chars")
    p.add_argument("--window", type=int, default=60, help="minutes of spots to look at (default 60)")
    p.add_argument("--interval", type=int, default=10, help="minutes between checks (min 5, default 10)")
    p.add_argument("--min-km", type=float, default=800, help="minimum path length to count (default 800 km)")
    p.add_argument("--min-paths", type=int, default=2, help="distinct paths needed to call it open (default 2)")
    p.add_argument("--strong-km", type=float, default=1500,
                   help="a single path at least this long counts as open on its own (default 1500 km)")
    p.add_argument("--repeat", type=int, default=30, help="minutes between repeat bulletins while open")
    p.add_argument("--announce-close", action="store_true", help="send a bulletin when an opening fades")
    p.add_argument("--state", default=os.path.expanduser("~/.vhf_prop_aprs.json"))
    p.add_argument("--input", help="read PSKReporter XML from a file instead of the web (testing)")
    p.add_argument("--dry-run", action="store_true", help="log what would be sent, transmit nothing")
    p.add_argument("--once", action="store_true", help="run one check and exit (for cron)")
    args = p.parse_args()

    args.contact = args.contact or args.call.upper()
    args.group = args.group.upper()[:5]
    interval = max(MIN_QUERY_INTERVAL, args.interval * 60)

    outputs = []
    if not args.no_aprsis:
        pc = args.passcode if args.passcode is not None else aprs_passcode(args.call)
        outputs.append(AprsIs(args.call, pc, args.server, args.port))
    if args.kiss_host:
        path = [c.strip() for c in args.rf_path.split(",") if c.strip()]
        outputs.append(KissTcp(args.call, args.kiss_host, args.kiss_port, path))

    state = load_state(args.state)
    log(f"vhf_prop_aprs {VERSION} as {args.call.upper()} - window {args.window} min, "
        f"min {args.min_km:.0f} km / {args.min_paths} paths{' (DRY RUN)' if args.dry_run else ''}")
    while True:
        try:
            run_cycle(args, outputs, state)
            if not args.dry_run:
                save_state(args.state, state)
        except Exception as e:
            log(f"! cycle failed: {type(e).__name__}: {e}")
        if args.once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
