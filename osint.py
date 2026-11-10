# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""Tradewind OSINT (v3.24): open, public Earth data on the live map. Receive only - nothing is transmitted.

  Imagery   NASA GIBS (free, no key): daily VIIRS and MODIS true colour, and 30 m Sentinel-2 / Landsat (HLS).
  Fires     NASA FIRMS active-fire hotspots (free MAP_KEY from firms.modaps.eosdis.nasa.gov/api/map_key).
  Storms    US National Hurricane Center current storms.
  Ships     AIS position reports from your own receiver: run rtl_ais or AIS-catcher with an RTL-SDR and let it
            send NMEA to UDP port 10110 on this computer. No internet needed.

Everything fetched is kept on disk, so the last picture still shows with no internet.
SAVE FOR OFFLINE downloads the base map and the chosen imagery around your station before a storm arrives.
"""
import csv
import io
import json
import math
import os
import socket
import threading
import time

GIBS_URL = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/{layer}/default/{date}/{tms}/{z}/{y}/{x}.{ext}"
# key: (GIBS layer id, tile matrix set, file type, deepest zoom, name on the face plate)
GIBS = {
    "viirs": ("VIIRS_SNPP_CorrectedReflectance_TrueColor", "GoogleMapsCompatible_Level9", "jpg", 9,
              "VIIRS true colour - daily, 250 m"),
    "modis": ("MODIS_Terra_CorrectedReflectance_TrueColor", "GoogleMapsCompatible_Level9", "jpg", 9,
              "MODIS Terra true colour - daily, 250 m"),
    "s30": ("HLS_S30_Nadir_BRDF_Adjusted_Reflectance", "GoogleMapsCompatible_Level12", "png", 12,
            "Sentinel-2 (HLS) - 30 m, every few days"),
    "l30": ("HLS_L30_Nadir_BRDF_Adjusted_Reflectance", "GoogleMapsCompatible_Level12", "png", 12,
            "Landsat 8/9 (HLS) - 30 m, every few days"),
}
FIRMS_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/VIIRS_SNPP_NRT/{w:.2f},{s:.2f},{e:.2f},{n:.2f}/{days}"
FIRES_EVERY_S = 30 * 60
STORMS_EVERY_S = 30 * 60
SHIP_MAX_AGE_S = 30 * 60


def gibs_tile_url(key, date, z, y, x):
    layer, tms, ext, _maxz, _name = GIBS[key]
    return GIBS_URL.format(layer=layer, tms=tms, ext=ext, date=date, z=z, y=y, x=x)


def tile_xy(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def tiles_around(lat, lon, half_deg, zooms):
    """Tiles (z, x, y) covering a box of +-half_deg around a point."""
    out = []
    for z in zooms:
        x0, y0 = tile_xy(lat + half_deg, lon - half_deg, z)
        x1, y1 = tile_xy(lat - half_deg, lon + half_deg, z)
        out += [(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    return out


def parse_firms(text):
    fires = []
    for row in csv.DictReader(io.StringIO(text or "")):
        try:
            fires.append({"lat": float(row["latitude"]), "lon": float(row["longitude"]),
                          "frp": float(row.get("frp") or 0), "conf": (row.get("confidence") or "").lower()[:1],
                          "when": f"{row.get('acq_date', '')} {str(row.get('acq_time', '')).zfill(4)[:2]}:{str(row.get('acq_time', '')).zfill(4)[2:]} UTC",
                          "dn": row.get("daynight", "")})
        except (KeyError, ValueError):
            continue
    return fires


# ---- AIS (ships) ------------------------------------------------------------------------------------

def _bits(payload):
    out = []
    for ch in payload:
        c = ord(ch) - 48
        if c > 40:
            c -= 8
        if not 0 <= c <= 63:
            raise ValueError("bad AIS character")
        out.append(format(c, "06b"))
    return "".join(out)


def _u(b, a, z):
    return int(b[a:z], 2)


def _s(b, a, z):
    v = int(b[a:z], 2)
    return v - (1 << (z - a)) if b[a] == "1" else v


def _text(b, a, z):
    s = "".join("@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"[_u(b, i, i + 6)] for i in range(a, z, 6))
    return s.replace("@", " ").strip()


def decode_ais(sentence):
    """One !AIVDM sentence -> dict (mmsi and position, or name), or None. Single-fragment messages only."""
    try:
        f = sentence.strip().split(",")
        if not f[0].endswith(("VDM", "VDO")) or f[1] != "1":
            return None
        b = _bits(f[5])
        t, mmsi = _u(b, 0, 6), _u(b, 8, 38)
        if t in (1, 2, 3) and len(b) >= 128:
            sog, lon, lat, cog = _u(b, 50, 60), _s(b, 61, 89), _s(b, 89, 116), _u(b, 116, 128)
        elif t == 18 and len(b) >= 124:
            sog, lon, lat, cog = _u(b, 46, 56), _s(b, 57, 85), _s(b, 85, 112), _u(b, 112, 124)
        elif t == 24 and len(b) >= 160 and _u(b, 38, 40) == 0:
            return {"mmsi": mmsi, "name": _text(b, 40, 160)}
        else:
            return None
        lat, lon = lat / 600000.0, lon / 600000.0
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None                                   # 91 / 181 mean "position not available"
        return {"mmsi": mmsi, "lat": round(lat, 5), "lon": round(lon, 5),
                "sog": None if sog == 1023 else sog / 10.0, "cog": None if cog >= 3600 else cog / 10.0}
    except (ValueError, IndexError):
        return None


class Osint:
    def __init__(self, cache_dir, web_get, now=time.time):
        self.dir = os.path.join(cache_dir, "osint")
        self.web_get, self.now = web_get, now
        self.lock = threading.Lock()
        self.fires, self.fires_t, self.fires_err = [], 0, ""
        self.storms, self.storms_t = [], 0
        self.ships, self.names = {}, {}
        self.ais_sock, self.ais_port, self.ais_err, self.ais_count = None, 0, "", 0
        self.save = {"busy": False, "done": 0, "total": 0, "failed": 0, "t": 0}
        self._next_fires = self._next_storms = 0
        self._load()

    # -- disk copy, for use with no internet --
    def _load(self):
        try:
            with open(os.path.join(self.dir, "latest.json"), encoding="utf-8") as f:
                d = json.load(f)
            self.fires, self.fires_t = d.get("fires", []), d.get("fires_t", 0)
            self.storms, self.storms_t = d.get("storms", []), d.get("storms_t", 0)
        except (OSError, ValueError):
            pass

    def _store(self):
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(os.path.join(self.dir, "latest.json"), "w", encoding="utf-8") as f:
                json.dump({"fires": self.fires, "fires_t": self.fires_t, "storms": self.storms, "storms_t": self.storms_t}, f)
        except OSError:
            pass

    # -- fetching --
    def refresh_fires(self, cfg, lat, lon):
        key = str(cfg.get("firms_key") or "").strip()
        if not key:
            self.fires_err = "no FIRMS key"
            return
        half = 6.0
        url = FIRMS_URL.format(key=key, w=max(-180, lon - half), s=max(-90, lat - half), e=min(180, lon + half),
                               n=min(90, lat + half), days=2)
        try:
            text = self.web_get(url).decode("utf-8", "replace")
            if "latitude" not in text[:200]:
                raise IOError(text.strip()[:80] or "no data")
            self.fires, self.fires_t, self.fires_err = parse_firms(text), self.now(), ""
            self._store()
        except Exception as e:
            self.fires_err = f"FIRMS: {e}"[:120]

    def refresh_storms(self, fetch_storms, storm_position):
        try:
            out = []
            for st in fetch_storms() or []:
                pos = storm_position(st)
                if pos:
                    out.append({"name": st.get("name", "?"), "cls": st.get("classification", ""), "lat": pos[0], "lon": pos[1],
                                "wind": st.get("intensity", ""), "move": f"{st.get('movementDir', '')} {st.get('movementSpeed', '')}".strip()})
            self.storms, self.storms_t = out, self.now()
            self._store()
        except Exception:
            pass

    def tick(self, cfg, lat, lon, fetch_storms=None, storm_position=None):
        """Call every few seconds from a background thread."""
        now = self.now()
        if cfg.get("osint_fires", True) and str(cfg.get("firms_key") or "").strip() and now >= self._next_fires:
            self._next_fires = now + FIRES_EVERY_S
            self.refresh_fires(cfg, lat, lon)
        if cfg.get("osint_storms", True) and fetch_storms and now >= self._next_storms:
            self._next_storms = now + STORMS_EVERY_S
            self.refresh_storms(fetch_storms, storm_position)
        want = int(cfg.get("ais_port") or 10110) if cfg.get("osint_ais") else 0
        if want != self.ais_port:
            self._ais_stop()
            if want:
                self._ais_start(want)

    # -- ships --
    def _ais_start(self, port):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            s.settimeout(1.0)
            self.ais_sock, self.ais_port, self.ais_err = s, port, ""
            threading.Thread(target=self._ais_loop, args=(s,), name="ais", daemon=True).start()
        except OSError as e:
            self.ais_port, self.ais_err = port, f"AIS port {port}: {e}"

    def _ais_stop(self):
        s, self.ais_sock, self.ais_port = self.ais_sock, None, 0
        if s:
            try:
                s.close()
            except OSError:
                pass

    def _ais_loop(self, s):
        while self.ais_sock is s:
            try:
                data = s.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            for line in data.decode("ascii", "replace").splitlines():
                self.feed_ais(line)

    def feed_ais(self, line):
        m = decode_ais(line)
        if not m:
            return
        with self.lock:
            self.ais_count += 1
            if "name" in m:
                self.names[m["mmsi"]] = m["name"]
            else:
                self.ships[m["mmsi"]] = dict(m, t=self.now())

    # -- save for offline --
    def save_offline(self, lat, lon, base_fetch, layer, date, image_fetch, half_deg=0.4):
        """Download the base map and the chosen imagery around a point, in the background."""
        if self.save["busy"]:
            return False
        jobs = [(base_fetch, t) for t in tiles_around(lat, lon, 4.0, range(3, 8)) + tiles_around(lat, lon, half_deg, range(8, 13))]
        if layer in GIBS and date:
            maxz = GIBS[layer][3]
            jobs += [(image_fetch, t) for t in tiles_around(lat, lon, 3.0, range(4, min(maxz, 8) + 1))
                     + tiles_around(lat, lon, half_deg, range(9, maxz + 1))]
        self.save = {"busy": True, "done": 0, "total": len(jobs), "failed": 0, "t": self.now()}

        def run():
            for fn, (z, x, y) in jobs:
                try:
                    if not fn(z, x, y):
                        self.save["failed"] += 1
                except Exception:
                    self.save["failed"] += 1
                self.save["done"] += 1
            self.save["busy"] = False
        threading.Thread(target=run, name="osint-save", daemon=True).start()
        return True

    def state(self):
        now = self.now()
        with self.lock:
            self.ships = {k: v for k, v in self.ships.items() if now - v["t"] < SHIP_MAX_AGE_S}
            ships = [dict(v, name=self.names.get(k, ""), age=int(now - v["t"])) for k, v in self.ships.items()]
        return {"fires": self.fires, "fires_age": int(now - self.fires_t) if self.fires_t else None, "fires_err": self.fires_err,
                "storms": self.storms, "storms_age": int(now - self.storms_t) if self.storms_t else None,
                "ships": ships, "ais": {"port": self.ais_port, "err": self.ais_err, "count": self.ais_count},
                "layers": [{"key": k, "name": v[4], "maxz": v[3]} for k, v in GIBS.items()], "save": dict(self.save)}
