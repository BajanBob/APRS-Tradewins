# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""APRS DX mode: inter-island targets, beam headings, the kind of path each needs, and openings.

For each target island Tradewind works out the distance and beam heading from your grid square, whether a
2 m / 70 cm path is line-of-sight from your antenna to the island's high ground, and whether PSKReporter
shows the band open in that direction right now (spots between your area and the island, or other paths of
similar length and direction). When a target opens, Tradewind can send a DX beacon automatically.
"""
import math

# name, latitude, longitude, highest ground (m) used for the line-of-sight estimate, prefix
TARGETS = [
    ("St Vincent", 13.16, -61.23, 1234, "J8"),
    ("St Lucia", 14.01, -60.99, 950, "J6"),
    ("Grenada", 12.06, -61.75, 840, "J3"),
    ("Martinique", 14.64, -61.02, 1397, "FM"),
    ("Dominica", 15.30, -61.39, 1447, "J7"),
    ("Tobago", 11.25, -60.70, 550, "9Y"),
    ("Trinidad", 10.65, -61.50, 940, "9Y"),
    ("Guadeloupe", 16.25, -61.58, 1467, "FG"),
    ("Antigua", 17.12, -61.85, 402, "V2"),
]
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def haversine_km(a, b):
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    d = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(d))


def bearing_deg(a, b):
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    y = math.sin(lon2 - lon1) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def radio_horizon_km(h1_m, h2_m):
    """Radio horizon with the usual 4/3-earth refraction: 4.12 (sqrt h1 + sqrt h2) km."""
    return 4.12 * (math.sqrt(max(h1_m, 0)) + math.sqrt(max(h2_m, 0)))


def targets_from(me, my_height_m=30, max_km=700):
    """Targets with distance, heading and path type from my position (lat, lon)."""
    out = []
    for name, lat, lon, peak, prefix in TARGETS:
        p = (lat, lon)
        km = haversine_km(me, p)
        if km > max_km or km < 30:
            continue
        brg = bearing_deg(me, p)
        horizon = radio_horizon_km(my_height_m, peak)
        out.append({"name": name, "prefix": prefix, "lat": lat, "lon": lon, "km": round(km), "bearing": round(brg),
                     "compass": COMPASS[int((brg + 11.25) // 22.5) % 16], "horizon_km": round(horizon),
                     "path": "line of sight to high ground" if km <= horizon else "beyond the horizon: tropo duct or HF"})
    return sorted(out, key=lambda t: t["km"])


def _ang(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def openings(spots, me, targets, near_me_km=150, near_target_km=80, area_km=350):
    """Which targets look open now. spots: objects with tx_grid, rx_grid, km, band, mode, snr, tx, rx.
    Each spot counts for at most one island:
      direct       - one end in my area, the other end within 80 km of an island (the nearest island);
      similar path - one end in my area and a path at least 70% as long, within 25 degrees of an island's
                     heading (the island whose heading matches best).
    Returns {island name: evidence dict}."""
    from vhf_prop_aprs import grid_to_latlon
    found = {}
    for s in spots:
        a, b = grid_to_latlon(getattr(s, "tx_grid", "")), grid_to_latlon(getattr(s, "rx_grid", ""))
        if not a or not b:
            continue
        hit, kind = None, None
        for x, y in ((a, b), (b, a)):
            if haversine_km(x, me) > near_me_km:
                continue
            near = min(targets, key=lambda t: haversine_km(y, (t["lat"], t["lon"])), default=None)
            if near and haversine_km(y, (near["lat"], near["lon"])) <= near_target_km:
                hit, kind = near, "direct"
                break
        if not hit:
            for x, y in ((a, b), (b, a)):
                if haversine_km(x, me) > area_km:
                    continue
                brg, km = bearing_deg(x, y), haversine_km(x, y)
                fits = [t for t in targets if km >= 0.7 * t["km"] and _ang(brg, t["bearing"]) <= 25]
                if fits:
                    hit, kind = min(fits, key=lambda t: _ang(brg, t["bearing"])), "similar path"
                    break
        if not hit:
            continue
        ev = {"band": getattr(s, "band", ""), "tx": getattr(s, "tx", ""), "rx": getattr(s, "rx", ""),
              "tx_grid": getattr(s, "tx_grid", ""), "rx_grid": getattr(s, "rx_grid", ""),
              "km": round(getattr(s, "km", 0) or 0), "mode": getattr(s, "mode", ""), "snr": getattr(s, "snr", None),
              "kind": kind}
        best = found.get(hit["name"])
        if not best or (kind == "direct" and best["kind"] != "direct") or (kind == best["kind"] and ev["km"] > best["km"]):
            found[hit["name"]] = ev
    return found


def dx_beacon_text(target, cfg, freq_label, seq=1, of=1):
    """Short APRS status (at most 62 characters after '>') announcing the opening and how to reach this station.
    Robust DX gets an even shorter text so each beacon fits the 64-byte frame (about 22 s on air)."""
    modem, call, n = cfg.get("modem"), cfg.get("call", ""), f" {seq}/{of}" if of > 1 else ""
    if modem == "robust_dx":
        text = f"DX {target['name']} {call} RDX {freq_label}{n}"
    else:
        via = "R" if modem == "robust" else "APRS"
        text = f"DX OPEN {target['name']} {target['km']}km brg{target['bearing']} {call} {via} {freq_label}{n}"
    return ">" + text[:62]


def burst_plan(cfg, count=4, seconds=60):
    """When an island opens: how many DX beacons, how far apart. 4 a minute on 1200 baud and Robust;
    Robust DX takes ~22 s per beacon, so 2 fit in the minute."""
    modem = cfg.get("modem") if cfg.get("tnc") == "sound" else "1200"
    count = max(1, min(int(count or 4), 6))
    if modem == "robust_dx":
        count = min(count, 2)
    return count, seconds / count
