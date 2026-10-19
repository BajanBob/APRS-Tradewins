# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""
wx.py - scheduled weather bulletins for APRS.

  general   Open-Meteo daily forecast for a place (default Barbados)
  marine    Open-Meteo Marine waves/swell + forecast winds, small-craft caution
  aviation  METAR + TAF for an aerodrome (default TBPB Grantley Adams) and
            active international SIGMETs for an FIR (default TTZP Piarco)
            from aviationweather.gov

Each builder returns a list of text lines of at most 67 characters, ready to
be sent as APRS bulletins (BLN1..BLN9 + group).

These are automated, unofficial summaries. Official forecasts and warnings
come from the Barbados Meteorological Services.
"""

import json
import time
import urllib.parse
import urllib.request

APRS_LINE = 67
MAX_LINES = 9
UA = "Tradewind-APRS/1.0 (amateur radio weather bulletins)"

COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

WMO = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy",
       45: "Fog", 48: "Fog", 51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
       61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Rain", 67: "Rain",
       80: "Light showers", 81: "Showers", 82: "Heavy showers",
       95: "Thunderstorms", 96: "Thunderstorms, hail", 99: "Thunderstorms, hail"}

GROUP = {"general": "WX", "marine": "MAR", "aviation": "AVN", "tropical": "TC"}

TC_CLASS = {"TD": "Tropical Depression", "STD": "Subtropical Depression", "TS": "Tropical Storm",
            "STS": "Subtropical Storm", "HU": "Hurricane", "PTC": "Post-tropical Cyclone",
            "PC": "Potential Tropical Cyclone", "TY": "Typhoon"}


def compass(deg):
    if deg is None:
        return "VRB"
    return COMPASS[int((float(deg) % 360) / 22.5 + 0.5) % 16]


def _get(url, timeout=30):
    ctx = None
    try:
        import ssl
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read().decode("utf-8", "replace")


def wrap(text, width=APRS_LINE):
    """Split text into APRS-sized lines at word boundaries."""
    out, cur = [], ""
    for word in text.split():
        while len(word) > width:                # very long token
            if cur:
                out.append(cur)
                cur = ""
            out.append(word[:width])
            word = word[width:]
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= width:
            cur += " " + word
        else:
            out.append(cur)
            cur = word
    if cur:
        out.append(cur)
    return out


def _day_label(iso_date):
    t = time.strptime(iso_date, "%Y-%m-%d")
    return time.strftime("%a %d %b", t)


def _hhmm(iso_dt):
    # "2026-10-19T05:53" -> "0553"
    return iso_dt[11:16].replace(":", "") if iso_dt and len(iso_dt) >= 16 else "--"


def _n(v, fmt="{:.0f}"):
    return "?" if v is None else fmt.format(v)


# --------------------------------------------------------------------------
# Fetchers (return parsed data, so builders can be tested offline)
# --------------------------------------------------------------------------

def fetch_forecast(lat, lon, tz="America/Barbados"):
    q = {
        "latitude": lat, "longitude": lon, "timezone": tz, "forecast_days": 2,
        "wind_speed_unit": "kn",
        "daily": ",".join([
            "weather_code", "temperature_2m_max", "temperature_2m_min",
            "precipitation_probability_max", "precipitation_sum",
            "wind_speed_10m_max", "wind_gusts_10m_max", "wind_direction_10m_dominant",
            "uv_index_max", "sunrise", "sunset"]),
    }
    return json.loads(_get("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q)))


def fetch_marine(lat, lon, tz="America/Barbados"):
    q = {
        "latitude": lat, "longitude": lon, "timezone": tz, "forecast_days": 2,
        "daily": ",".join([
            "wave_height_max", "wave_direction_dominant", "wave_period_max",
            "swell_wave_height_max", "swell_wave_direction_dominant", "swell_wave_period_max"]),
    }
    return json.loads(_get("https://marine-api.open-meteo.com/v1/marine?" + urllib.parse.urlencode(q)))


def fetch_metar(icao):
    return _get(f"https://aviationweather.gov/api/data/metar?ids={icao}&format=raw").strip()


def fetch_taf(icao):
    return _get(f"https://aviationweather.gov/api/data/taf?ids={icao}&format=raw").strip()


def fetch_sigmets():
    return json.loads(_get("https://aviationweather.gov/api/data/isigmet?format=json") or "[]")


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------

def _daily(d, key, i=0):
    try:
        return d["daily"][key][i]
    except (KeyError, IndexError, TypeError):
        return None


def build_general(fc, place="Barbados"):
    day = _daily(fc, "time") or time.strftime("%Y-%m-%d")
    sky = WMO.get(_daily(fc, "weather_code"), "Mixed")
    l1 = (f"{place} {_day_label(day)}: {sky}, Hi {_n(_daily(fc, 'temperature_2m_max'))}C "
          f"Lo {_n(_daily(fc, 'temperature_2m_min'))}C, rain "
          f"{_n(_daily(fc, 'precipitation_probability_max'))}% "
          f"{_n(_daily(fc, 'precipitation_sum'), '{:.0f}')}mm")
    l2 = (f"Wind {compass(_daily(fc, 'wind_direction_10m_dominant'))} "
          f"{_n(_daily(fc, 'wind_speed_10m_max'))}kt gust {_n(_daily(fc, 'wind_gusts_10m_max'))}kt, "
          f"UV {_n(_daily(fc, 'uv_index_max'))}, sunrise {_hhmm(_daily(fc, 'sunrise'))} "
          f"sunset {_hhmm(_daily(fc, 'sunset'))}")
    lines = wrap(l1) + wrap(l2)
    return lines[:MAX_LINES]


def build_marine(mar, fc, place="Barbados", seas_warn=2.5, wind_warn=25):
    day = _daily(mar, "time") or time.strftime("%Y-%m-%d")
    wave = _daily(mar, "wave_height_max")
    wind = _daily(fc, "wind_speed_10m_max")
    gust = _daily(fc, "wind_gusts_10m_max")
    l1 = (f"{place} marine {_day_label(day)}: seas {_n(wave, '{:.1f}')}m "
          f"{compass(_daily(mar, 'wave_direction_dominant'))}, swell "
          f"{_n(_daily(mar, 'swell_wave_height_max'), '{:.1f}')}m "
          f"{compass(_daily(mar, 'swell_wave_direction_dominant'))} "
          f"{_n(_daily(mar, 'swell_wave_period_max'))}s")
    l2 = (f"Wind {compass(_daily(fc, 'wind_direction_10m_dominant'))} {_n(wind)}kt "
          f"gust {_n(gust)}kt. Tomorrow seas "
          f"{_n(_daily(mar, 'wave_height_max', 1), '{:.1f}')}m, wind "
          f"{_n(_daily(fc, 'wind_speed_10m_max', 1))}kt")
    lines = wrap(l1) + wrap(l2)
    reasons = []
    if wave is not None and wave >= seas_warn:
        reasons.append(f"seas {wave:.1f}m")
    if wind is not None and wind >= wind_warn:
        reasons.append(f"wind {wind:.0f}kt")
    if reasons:
        lines += wrap("CAUTION small craft: " + ", ".join(reasons) +
                      ". Check official BMS marine forecast.")
    return lines[:MAX_LINES]


def sigmet_lines(sigmets, fir="TTZP", now=None):
    now = now or time.time()
    out = []
    for s in sigmets or []:
        if (s.get("firId") or "").upper() != fir.upper():
            continue
        try:
            if int(s.get("validTimeTo") or 0) < now:
                continue
        except (TypeError, ValueError):
            pass
        hz = " ".join(x for x in (s.get("qualifier"), s.get("hazard")) if x) or "hazard"
        to = s.get("validTimeTo")
        until = time.strftime("%H%MZ", time.gmtime(int(to))) if to else "?"
        top = s.get("top")
        lvl = f" to FL{int(top) // 100:03d}" if isinstance(top, (int, float)) and top else ""
        out.append(f"SIGMET {fir} {s.get('seriesId') or ''}: {hz}{lvl} until {until}".replace("  ", " "))
    return out


def build_aviation(metar, taf, sigmets, icao="TBPB", fir="TTZP"):
    lines = []
    if metar:
        lines += wrap(metar.splitlines()[0])
    else:
        lines.append(f"{icao} METAR not available")
    if taf:
        lines += wrap(" ".join(taf.split()))
    else:
        lines.append(f"{icao} TAF not available")
    sl = sigmet_lines(sigmets, fir)
    lines += sl if sl else [f"No active SIGMET for {fir} FIR"]
    if len(lines) > MAX_LINES:            # keep METAR + SIGMET, trim TAF tail
        sig = sl[:2] if sl else [lines[-1]]
        lines = lines[:MAX_LINES - len(sig)] + sig
    return lines


def build(kind, cfg):
    """Fetch data and build lines for one bulletin kind."""
    if kind == "general":
        fc = fetch_forecast(cfg["wx_lat"], cfg["wx_lon"])
        return build_general(fc, cfg["wx_place"])
    if kind == "marine":
        mar = fetch_marine(cfg["wx_marine_lat"], cfg["wx_marine_lon"])
        fc = fetch_forecast(cfg["wx_marine_lat"], cfg["wx_marine_lon"])
        return build_marine(mar, fc, cfg["wx_place"])
    if kind == "aviation":
        icao, fir = cfg["wx_icao"].upper(), cfg["wx_fir"].upper()
        metar = taf = ""
        sig = []
        errs = []
        for name, fn in (("metar", lambda: fetch_metar(icao)), ("taf", lambda: fetch_taf(icao))):
            try:
                if name == "metar":
                    metar = fn()
                else:
                    taf = fn()
            except Exception as e:
                errs.append(f"{name}: {e}")
        try:
            sig = fetch_sigmets()
        except Exception as e:
            errs.append(f"sigmet: {e}")
        if not metar and not taf and errs:
            raise RuntimeError("; ".join(errs))
        return build_aviation(metar, taf, sig, icao, fir)
    raise ValueError(kind)


# --------------------------------------------------------------------------
# Tropical cyclones (National Hurricane Center)
# --------------------------------------------------------------------------

def fetch_storms():
    data = json.loads(_get("https://www.nhc.noaa.gov/CurrentStorms.json") or "{}")
    return data.get("activeStorms") or []


def fetch_text(url):
    return _get(url) if url else ""


def _distance_bearing(lat1, lon1, lat2, lon2):
    import math
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    km = 6371.0 * 2 * math.asin(math.sqrt(a))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    brg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return km, brg


def storm_position(st):
    lat = st.get("latitudeNumeric", st.get("latitude_numeric"))
    lon = st.get("longitudeNumeric", st.get("longitude_numeric"))
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def watches_for(advisory_text, place):
    """Watch/warning types in an NHC public advisory that name `place`."""
    import re
    found, current = [], None
    head = re.compile(r"(Hurricane|Tropical Storm|Storm Surge)\s+(Watch|Warning)", re.I)
    for raw in advisory_text.splitlines():
        line = raw.strip()
        m = head.search(line)
        if m and ("in effect" in line.lower() or line.lower().startswith("a ")):
            current = f"{m.group(1).title()} {m.group(2).title()}"
            continue
        if not line:
            continue
        if current and line.startswith("*") and place.lower() in line.lower():
            if current not in found:
                found.append(current)
        if line.upper().startswith(("DISCUSSION AND OUTLOOK", "HAZARDS AFFECTING LAND")):
            current = None
    return found


def build_tropical(st, place, lat, lon, warnings=()):
    pos = storm_position(st)
    cls = (st.get("classification") or "").upper()
    name = (st.get("name") or st.get("id") or "?").upper()
    adv = (st.get("publicAdvisory") or {}).get("advNum") or "?"
    spd = st.get("movementSpeed")
    mov = f"mov {compass(st.get('movementDir'))} {spd}mph" if spd not in (None, "") else "stationary"
    l1 = (f"{cls} {name} adv {adv}: {st.get('latitude', '?')} {st.get('longitude', '?')} "
          f"{_n(st.get('intensity'))}kt {_n(st.get('pressure'))}mb {mov}")
    lines = wrap(l1)
    if pos:
        km, brg = _distance_bearing(lat, lon, pos[0], pos[1])
        lines += wrap(f"{TC_CLASS.get(cls, cls)} {name} is {km:.0f}km {compass(brg)} of {place}")
    for w in warnings:
        lines += wrap(f"{w.upper()} IN EFFECT FOR {place.upper()}")
    lines += wrap("Info nhc.noaa.gov. Follow BMS and DEM official advice.")
    return lines[:MAX_LINES]


def tropical_alerts(cfg, storms=None, text_fetch=None):
    """[(key, lines)] for Atlantic systems within cfg['tc_radius_km'] of the place,
    or whose advisory puts the place under a watch/warning."""
    storms = fetch_storms() if storms is None else storms
    text_fetch = text_fetch or fetch_text
    lat, lon, place = float(cfg["wx_lat"]), float(cfg["wx_lon"]), cfg["wx_place"]
    out = []
    for st in storms:
        if not str(st.get("id", "")).lower().startswith("al"):
            continue
        pos = storm_position(st)
        km = _distance_bearing(lat, lon, pos[0], pos[1])[0] if pos else 1e9
        adv = (st.get("publicAdvisory") or {})
        warns = []
        try:
            warns = watches_for(text_fetch(adv.get("url")), place) if adv.get("url") else []
        except Exception:
            pass
        if km <= float(cfg.get("tc_radius_km", 1500)) or warns:
            key = f"{st.get('id')}#{adv.get('advNum') or st.get('lastUpdate')}"
            out.append((key, build_tropical(st, place, lat, lon, warns)))
    return out


def parse_times(s):
    """'05:30, 12:00' -> ['05:30', '12:00'] (validated)."""
    out = []
    for part in str(s or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        hh, _, mm = part.partition(":")
        h, m = int(hh), int(mm or 0)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError(part)
        out.append(f"{h:02d}:{m:02d}")
    return out
