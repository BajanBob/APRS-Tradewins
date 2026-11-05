# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""Satellite receivers for Satellite Watch Mode: audio, pictures, video and telemetry.

What each satellite sends and what receives it:

  satellite            receiver   frequency       hardware                 decoded by
  ISS                  APRS       145.825 FM      IC-9100 / FT-991A        Tradewind (sound modem)
  ISS                  SSTV       145.800 FM      IC-9100 / FT-991A        Tradewind (this file, built in)
  ISS                  HamTV      2395 MHz DVB-S  SDR + dish (+ LNB)       leandvb -> H.264 MPEG-TS -> ffmpeg (MP4)
  FUNcube-1 (AO-73)    telemetry  145.935 USB     IC-9100 / FT-991A        gr-satellites, from the recorded pass
  JY1Sat (JO-97)       telemetry  145.840 USB     IC-9100 / FT-991A        gr-satellites
  MESAT-1 (MO-122)     telemetry  435.800 USB     IC-9100 / FT-991A        gr-satellites
  Meteor-M N2-4        LRPT       137.900         RTL-SDR + V-dipole       SatDump (live, for each pass)
  GOES-19 / GOES-18    HRIT/EMWIN 1694.1          SDR + dish + LNA         SatDump (live, geostationary)

Tradewind predicts the passes, switches the radio (with Doppler correction), records the pass audio and runs
the decoders. Only standard library + numpy here; the external decoders are optional programs on the PC.
"""
import math
import os
import re
import shlex
import shutil
import struct
import subprocess
import threading
import time
import wave
import zlib

import numpy as np

C_KM_S = 299792.458

# ---------------------------------------------------------------- catalogue
# hw: radio | sdr | sdr_dish.   kind: leo | geo (geo_lon, degrees east).
CATALOG = [
    {"catnr": 25544, "name": "ISS", "kind": "leo", "receivers": [
        {"id": "iss_aprs", "label": "ISS APRS digipeater", "type": "aprs", "mhz": 145.825, "mode": "fm", "hw": "radio"},
        {"id": "iss_sstv", "label": "ISS SSTV pictures", "type": "sstv", "mhz": 145.800, "mode": "fm", "hw": "radio"},
        {"id": "iss_hamtv", "label": "ISS HamTV video (DVB-S, H.264)", "type": "hamtv", "mhz": 2395.0,
         "hw": "sdr_dish", "symrate": 2_000_000}]},
    {"catnr": 39444, "name": "FUNcube-1 (AO-73)", "kind": "leo", "receivers": [
        {"id": "ao73_tlm", "label": "FUNcube-1 telemetry (BPSK 1200)", "type": "telemetry", "mhz": 145.935,
         "mode": "usb", "hw": "radio"}]},
    {"catnr": 43803, "name": "JY1Sat (JO-97)", "kind": "leo", "receivers": [
        {"id": "jo97_tlm", "label": "JY1Sat telemetry (BPSK 1200)", "type": "telemetry", "mhz": 145.840,
         "mode": "usb", "hw": "radio"}]},
    {"catnr": 60209, "name": "MESAT-1 (MO-122)", "kind": "leo", "receivers": [
        {"id": "mo122_tlm", "label": "MESAT-1 telemetry (BPSK 1200)", "type": "telemetry", "mhz": 435.800,
         "mode": "usb", "hw": "radio"}]},
    {"catnr": 59051, "name": "Meteor-M N2-4", "kind": "leo", "receivers": [
        {"id": "meteor_lrpt", "label": "Meteor-M N2-4 LRPT pictures", "type": "lrpt", "mhz": 137.900, "hw": "sdr",
         "pipeline": "meteor_m2-x_lrpt", "samplerate": 1_024_000}]},
    {"catnr": 60133, "name": "GOES-19", "kind": "geo", "geo_lon": -75.2, "receivers": [
        {"id": "goes19_hrit", "label": "GOES-19 HRIT/EMWIN weather", "type": "hrit", "mhz": 1694.1, "hw": "sdr_dish",
         "pipeline": "goes_hrit", "samplerate": 2_400_000}]},
    {"catnr": 51850, "name": "GOES-18", "kind": "geo", "geo_lon": -137.0, "receivers": [
        {"id": "goes18_hrit", "label": "GOES-18 HRIT/EMWIN weather", "type": "hrit", "mhz": 1694.1, "hw": "sdr_dish",
         "pipeline": "goes_hrit", "samplerate": 2_400_000}]},
]
NOT_SUPPORTED = ("NOAA-20, NOAA-21 and Suomi NPP send HRD on X-band (7.8 GHz, 15-25 Mbps): that needs a tracking "
                 "dish and an X-band receiver. FOSSASAT-2E1 uses LoRa on 401.7 MHz: that needs a LoRa radio module.")


def receiver(rid):
    """(satellite, receiver) for a receiver id, or (None, None)."""
    for sat in CATALOG:
        for r in sat["receivers"]:
            if r["id"] == rid:
                return sat, r
    return None, None


def radio_receivers(catnr):
    """Receivers the IC-9100 / FT-991A can do for this satellite."""
    for sat in CATALOG:
        if sat["catnr"] == int(catnr):
            return [r for r in sat["receivers"] if r["hw"] == "radio"]
    return []


def sdr_receivers():
    return [(s, r) for s in CATALOG for r in s["receivers"] if r["hw"] in ("sdr", "sdr_dish")]


def doppler_hz(f_hz, range_rate_km_s):
    """Received frequency for a transmitter on f_hz moving away at range_rate (km/s, + = receding)."""
    return int(round(f_hz * (1.0 - range_rate_km_s / C_KM_S)))


def geo_look(obs, lon_e):
    """(azimuth, elevation, range km) of a geostationary satellite at longitude lon_e from obs=(lat, lon)."""
    import iss
    lat, lon = obs[0], obs[1]
    r = 42164.0
    sx, sy = r * math.cos(math.radians(lon_e)), r * math.sin(math.radians(lon_e))
    ox, oy, oz = iss.observer_ecef(lat, lon)
    dx, dy, dz = sx - ox, sy - oy, -oz
    la, lo = math.radians(lat), math.radians(lon)
    e = -math.sin(lo) * dx + math.cos(lo) * dy
    n = -math.sin(la) * math.cos(lo) * dx - math.sin(la) * math.sin(lo) * dy + math.cos(la) * dz
    u = math.cos(la) * math.cos(lo) * dx + math.cos(la) * math.sin(lo) * dy + math.sin(la) * dz
    return (math.degrees(math.atan2(e, n)) % 360, math.degrees(math.atan2(u, math.hypot(e, n))),
            math.sqrt(dx * dx + dy * dy + dz * dz))


# ---------------------------------------------------------------- PNG (no Pillow needed)
def write_png(path, rgb):
    """rgb: uint8 array (h, w, 3)."""
    rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(png)
    os.replace(tmp, path)


# ---------------------------------------------------------------- SSTV (built in)
BLACK, WHITE, SYNC = 1500.0, 2300.0, 1200.0
# times in ms.  layout: list of (what, ms) per line (or line pair for PD); what: sync/porch/sep/Y/R-Y/B-Y/Y2/G/B/R
SSTV_MODES = {
    95: {"name": "PD120", "w": 640, "h": 496, "pd": 121.6},
    96: {"name": "PD180", "w": 640, "h": 496, "pd": 183.04},
    44: {"name": "Martin M1", "w": 320, "h": 256, "rgb": 146.432, "sync": 4.862, "sep": 0.572},
    60: {"name": "Scottie S1", "w": 320, "h": 256, "rgb": 138.24, "sync": 9.0, "sep": 1.5},
    8: {"name": "Robot 36", "w": 320, "h": 240},
}
SSTV_BY_NAME = {v["name"]: k for k, v in SSTV_MODES.items()}


def _line_plan(vis):
    """Segments of one line (PD: one line pair) as (kind, ms, freq or None). Image segments have freq None."""
    m = SSTV_MODES[vis]
    if "pd" in m:
        d = m["pd"]
        return [("sync", 20.0, SYNC), ("porch", 2.08, BLACK), ("Y", d, None), ("R-Y", d, None),
                ("B-Y", d, None), ("Y2", d, None)]
    if m["name"] == "Martin M1":
        s, p, d = m["sync"], m["sep"], m["rgb"]
        return [("sync", s, SYNC), ("porch", p, BLACK), ("G", d, None), ("sep", p, BLACK), ("B", d, None),
                ("sep", p, BLACK), ("R", d, None), ("sep", p, BLACK)]
    if m["name"] == "Scottie S1":
        p, d = m["sep"], m["rgb"]
        return [("sep", p, BLACK), ("G", d, None), ("sep", p, BLACK), ("B", d, None), ("sync", m["sync"], SYNC),
                ("porch", p, BLACK), ("R", d, None)]
    # Robot 36: Y then one chroma per line, alternating R-Y (even) / B-Y (odd)
    return [("sync", 9.0, SYNC), ("porch", 3.0, BLACK), ("Y", 88.0, None), ("csep", 4.5, None),
            ("cporch", 1.5, 1900.0), ("C", 44.0, None)]


def _lines(vis):
    m = SSTV_MODES[vis]
    return m["h"] // 2 if "pd" in m else m["h"]


def _sync_offset_ms(vis):
    """Where the line's sync pulse starts, from the start of the line."""
    t = 0.0
    for kind, ms, _ in _line_plan(vis):
        if kind == "sync":
            return t
        t += ms
    return 0.0


def _to_ycc(rgb):
    r, g, b = (rgb[..., i].astype(float) for i in range(3))
    y = 0.299 * r + 0.587 * g + 0.114 * b
    return y, 128 + 0.713 * (r - y), 128 + 0.564 * (b - y)


def _from_ycc(y, cr, cb):
    r = y + 1.402 * (cr - 128)
    b = y + 1.772 * (cb - 128)
    g = (y - 0.299 * r - 0.114 * b) / 0.587
    return np.clip(np.stack([r, g, b], -1), 0, 255).astype(np.uint8)


def sstv_encode(rgb, vis=95, rate=48000):
    """SSTV audio (float32, -1..1) for an image: VIS header then the picture. Used for tests and the
    'test picture' button; the ISS sends PD120 / PD180."""
    m = SSTV_MODES[vis]
    w, h = m["w"], m["h"]
    img = np.asarray(rgb, dtype=np.uint8)
    if img.shape[:2] != (h, w):                 # nearest-neighbour fit
        yi = (np.arange(h) * img.shape[0] // h)
        xi = (np.arange(w) * img.shape[1] // w)
        img = img[yi][:, xi]
    segs = [(1900.0, 300), (SYNC, 10), (1900.0, 300), (SYNC, 30)]
    bits = [(vis >> i) & 1 for i in range(7)]
    bits.append(sum(bits) % 2)
    segs += [(1100.0 if b else 1300.0, 30) for b in bits] + [(SYNC, 30)]
    freq = [np.full(int(round(ms * rate / 1000)), f, dtype=float) for f, ms in segs]
    lvl = lambda v: BLACK + np.asarray(v, float) / 255.0 * (WHITE - BLACK)   # noqa: E731
    y, cr, cb = _to_ycc(img)
    plan = _line_plan(vis)
    t_acc = sum(ms for _, ms in segs) / 1000.0
    pos = [t_acc]

    def seg(f_or_px, ms):
        """Append a constant tone or a row of pixels lasting ms, keeping exact timing (no drift)."""
        start = pos[0]
        end = start + ms / 1000.0
        n = int(round(end * rate)) - int(round(start * rate))
        if np.ndim(f_or_px) == 0:
            freq.append(np.full(n, float(f_or_px)))
        else:
            px = np.asarray(f_or_px, float)
            idx = np.minimum((np.arange(n) * len(px) // max(n, 1)), len(px) - 1)
            freq.append(lvl(px[idx]))
        pos[0] = end
    if m["name"] == "Scottie S1":
        seg(SYNC, 9.0)                                    # Scottie's starting sync
    for li in range(_lines(vis)):
        for kind, ms, f in plan:
            if f is not None:
                seg(f, ms)
            elif "pd" in m:
                r0, r1 = 2 * li, 2 * li + 1
                seg({"Y": y[r0], "R-Y": (cr[r0] + cr[r1]) / 2, "B-Y": (cb[r0] + cb[r1]) / 2, "Y2": y[r1]}[kind], ms)
            elif m["name"] == "Robot 36":
                even = li % 2 == 0
                if kind == "Y":
                    seg(y[li], ms)
                elif kind == "csep":
                    seg(BLACK if even else WHITE, ms)
                else:
                    pair = (li // 2) * 2
                    c = cr if even else cb
                    seg((c[pair] + c[min(pair + 1, h - 1)]) / 2, ms)
            else:
                seg(img[li, :, {"R": 0, "G": 1, "B": 2}[kind]], ms)
    f = np.concatenate(freq)
    phase = 2 * np.pi * np.cumsum(f) / rate
    return (0.7 * np.sin(phase)).astype(np.float32)


def _inst_freq(x, rate, block=1 << 20, pad=4096):
    """Instantaneous frequency (Hz) of the audio, band-limited to 900-2700 Hz. Works in overlapping blocks
    so a whole 10-minute pass fits in memory."""
    x = np.asarray(x, dtype=np.float32)
    n = len(x)
    out = np.empty(n, dtype=np.float32)
    nfft = block + 2 * pad
    f = np.fft.fftfreq(nfft, 1.0 / rate)
    keep = (f >= 900) & (f <= 2700)                     # positive band only: analytic signal of the SSTV band
    k = max(1, int(rate / 6000))                        # light smoothing (keeps ~6 kHz pixel rate)
    for a in range(0, n, block):
        lo, hi = max(0, a - pad), min(n, a + block + pad)
        seg = np.zeros(nfft, dtype=np.float64)
        seg[:hi - lo] = x[lo:hi]
        X = np.fft.fft(seg)
        X[~keep] = 0
        z = np.fft.ifft(X)[:hi - lo]
        d = np.angle(z[1:] * np.conj(z[:-1])) * rate / (2 * np.pi)
        d = np.concatenate([d[:1], d])
        d = np.convolve(d, np.ones(k) / k, mode="same")
        b = min(n, a + block)
        out[a:b] = d[a - lo:b - lo]
    return out


def _per_ms(fi, rate):
    """Median frequency in each millisecond (the track the VIS search runs on)."""
    per = rate / 1000.0
    wd = max(1, int(per))
    nms = int((len(fi) - wd) / per)
    out = np.empty(max(nms, 0), dtype=np.float32)
    for a in range(0, nms, 50000):
        b = min(nms, a + 50000)
        idx = (np.arange(a, b) * per).astype(np.int64)
        out[a:b] = np.median(fi[idx[:, None] + np.arange(wd)], axis=1)
    return out


def _find_vis(fm_ms):
    """Find VIS headers in a 1-ms frequency track. Returns list of (vis, image start ms)."""
    out, n = [], len(fm_ms)
    near = lambda a, f, tol=60: len(a) > 0 and abs(np.median(a) - f) < tol    # noqa: E731
    # candidates: 1200 Hz now, 1900 Hz leader 100 and 200 ms earlier
    ok = np.abs(fm_ms - SYNC) < 80
    lead = np.abs(fm_ms - 1900) < 80
    cand = np.where(ok[300:n - 300] & lead[200:n - 400] & lead[100:n - 500])[0] + 300
    next_free = 0
    for k in cand:
        if k < next_free:
            continue
        if near(fm_ms[k + 3:k + 27], SYNC) and near(fm_ms[k - 250:k - 15], 1900, 80):
            # refine: start bit begins where the frequency first drops below 1550
            s = k
            while s > k - 15 and fm_ms[s - 1] < 1550:
                s -= 1
            bits = []
            for i in range(8):
                a = s + 30 + i * 30
                med = float(np.median(fm_ms[a + 5:a + 25]))
                bits.append(1 if med < 1200 else 0)
            vis = sum(b << i for i, b in enumerate(bits[:7]))
            stop_ok = near(fm_ms[s + 270 + 5:s + 270 + 25], SYNC, 80)
            if vis in SSTV_MODES and sum(bits) % 2 == 0 and stop_ok:
                out.append((vis, s + 300))
                next_free = s + 300 + 1000
    return out


def sstv_decode(audio, rate=48000):
    """Decode every SSTV picture in the audio. Returns list of (mode name, rgb uint8 array)."""
    audio = np.asarray(audio, dtype=float)
    if len(audio) < rate:
        return []
    fi = _inst_freq(audio, rate)
    per_ms = rate / 1000.0
    fm_ms = _per_ms(fi, rate)
    pics = []
    for vis, start_ms in _find_vis(fm_ms):
        m = SSTV_MODES[vis]
        plan = _line_plan(vis)
        line_ms = sum(ms for _, ms, _ in plan)
        w, h = m["w"], m["h"]
        chans = {}
        t = start_ms + (9.0 if m["name"] == "Scottie S1" else 0.0)
        sync_off = _sync_offset_ms(vis)
        sync_ms = next(ms for kind, ms, _ in plan if kind == "sync")
        img_rows = []
        lines_done = 0
        for li in range(_lines(vis)):
            # lock to this line's sync pulse (within +-3 ms of where it should be)
            exp = t + sync_off
            a, b = int((exp - 3) * per_ms), int((exp + 3 + sync_ms) * per_ms)
            if b + int(sync_ms * per_ms) >= len(fi):
                break
            win = fi[a:b]
            ns = int(sync_ms * per_ms)
            if len(win) > ns:
                score = np.convolve((np.abs(win - SYNC) < 150).astype(float), np.ones(ns), "valid")
                best = int(np.argmax(score))
                if score[best] > 0.6 * ns:
                    t = (a + best) / per_ms - sync_off
            pos = t
            seg_vals = {}
            for kind, ms, f in plan:
                if f is None and kind != "csep":
                    npx = w
                    idx = ((pos + (np.arange(npx) + 0.5) * ms / npx) * per_ms).astype(int)
                    idx = np.clip(idx, 0, len(fi) - 1)
                    seg_vals[kind] = np.clip((fi[idx] - BLACK) / (WHITE - BLACK) * 255.0, 0, 255)
                elif kind == "csep":
                    c0 = int((pos + ms * 0.3) * per_ms)
                    seg_vals["odd"] = float(np.median(fi[c0:c0 + int(ms * 0.4 * per_ms)])) > 1900
                pos += ms
            if "pd" in m:
                img_rows.append(("pd", seg_vals))
            elif m["name"] == "Robot 36":
                img_rows.append(("robot", seg_vals))
            else:
                img_rows.append(("rgb", seg_vals))
            lines_done += 1
            t += line_ms
        if lines_done < 0.5 * _lines(vis):
            continue
        if "pd" in m:
            Y = np.zeros((h, w))
            CR = np.full((h, w), 128.0)
            CB = np.full((h, w), 128.0)
            for li, (_, v) in enumerate(img_rows):
                Y[2 * li], Y[2 * li + 1] = v["Y"], v["Y2"]
                CR[2 * li] = CR[2 * li + 1] = v["R-Y"]
                CB[2 * li] = CB[2 * li + 1] = v["B-Y"]
            rgb = _from_ycc(Y, CR, CB)
        elif m["name"] == "Robot 36":
            Y = np.zeros((h, w))
            CR = np.full((h, w), 128.0)
            CB = np.full((h, w), 128.0)
            for li, (_, v) in enumerate(img_rows):
                Y[li] = v["Y"]
                odd = v.get("odd", li % 2 == 1)
                pair = (li // 2) * 2
                tgt = CB if odd else CR
                tgt[pair] = v["C"]
                if pair + 1 < h:
                    tgt[pair + 1] = v["C"]
            rgb = _from_ycc(Y, CR, CB)
        else:
            rgb = np.zeros((h, w, 3), np.uint8)
            for li, (_, v) in enumerate(img_rows):
                rgb[li] = np.stack([v["R"], v["G"], v["B"]], -1).astype(np.uint8)
        pics.append((m["name"], rgb))
    return pics


# ---------------------------------------------------------------- recording the radio's audio during a pass
class PassRecorder:
    """Writes the received audio of one pass to a 16-bit mono WAV (tap it into SoundModemTNC.audio_taps)."""

    def __init__(self, path, rate):
        self.path, self.rate, self.lock = path, rate, threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.w = wave.open(path, "wb")
        self.w.setnchannels(1)
        self.w.setsampwidth(2)
        self.w.setframerate(rate)
        self.samples = 0

    def __call__(self, chunk):
        with self.lock:
            if self.w:
                self.w.writeframes((np.clip(np.asarray(chunk), -1, 1) * 32767).astype("<i2").tobytes())
                self.samples += len(chunk)

    def close(self):
        with self.lock:
            if self.w:
                self.w.close()
                self.w = None
        return self.samples / self.rate


def read_wav(path):
    with wave.open(path, "rb") as w:
        rate, n, sw, ch = w.getframerate(), w.getnframes(), w.getsampwidth(), w.getnchannels()
        data = w.readframes(n)
    if sw != 2:
        raise ValueError("16-bit WAV expected")
    x = np.frombuffer(data, "<i2").astype(np.float32) / 32768.0
    return (x.reshape(-1, ch)[:, 0] if ch > 1 else x), rate


# ---------------------------------------------------------------- external decoders
TOOLS = {
    "satdump": "SatDump - Meteor LRPT and GOES HRIT/EMWIN (satdump.org)",
    "gr_satellites": "gr-satellites - FUNcube-1, JO-97, MO-122 telemetry (gr-satellites.readthedocs.io)",
    "leandvb": "leandvb (leansdr) - HamTV DVB-S demodulator",
    "ffmpeg": "FFmpeg - H.264 video from HamTV",
    "rtl_sdr": "rtl-sdr tools - samples from an RTL-SDR for HamTV through a downconverter",
}


def tools_status(which=shutil.which):
    return {t: bool(which(t)) for t in TOOLS}


def gr_satellites_cmd(catnr, wav, rate, kiss_path):
    """gr-satellites accepts a NORAD number; the recorded USB audio is a real-valued WAV."""
    return ["gr_satellites", str(int(catnr)), "--wavfile", wav, "--samp_rate", str(int(rate)),
            "--kiss_out", kiss_path, "--hexdump"]


def satdump_cmd(rcv, outdir, secs, cfg):
    src = (cfg.get("sdr_source") or "rtlsdr").strip()
    return ["satdump", "live", rcv["pipeline"], outdir, "--source", src,
            "--samplerate", str(int(rcv.get("samplerate", 2_400_000))),
            "--frequency", str(int(round(rcv["mhz"] * 1e6))),
            "--gain", str(cfg.get("sdr_gain_db") or 40),
            "--timeout", str(int(secs))]


HAMTV_DEFAULT = ("rtl_sdr -f {if_hz} -s 2400000 -g {gain} - | leandvb --standard DVB-S --sr {symrate} "
                 "--cr 1/2 -f 2400000 | ffmpeg -y -loglevel error -i - -c copy {out_ts}")


def hamtv_cmd(rcv, out_ts, cfg):
    """Shell pipeline for HamTV. {if_hz} = 2395 MHz minus the downconverter LO (RTL-SDRs stop near 1.7 GHz).
    Returns (command, problem)."""
    lo = float(cfg.get("hamtv_lo_mhz") or 0)
    if_mhz = rcv["mhz"] - lo
    tpl = (cfg.get("hamtv_cmd") or "").strip() or HAMTV_DEFAULT
    if tpl == HAMTV_DEFAULT and not 24 <= if_mhz <= 1766:
        return None, (f"HamTV is on {rcv['mhz']:.0f} MHz: an RTL-SDR needs a downconverter - set its LO "
                      "(e.g. 1998 MHz for a 2.4 GHz LNB), or enter your own receive command")
    return tpl.format(if_hz=int(if_mhz * 1e6), gain=int(cfg.get("sdr_gain_db") or 40),
                      symrate=int(rcv.get("symrate", 2_000_000)), out_ts=shlex.quote(out_ts),
                      hz=int(rcv["mhz"] * 1e6)), ""


def video_finish(ts_path, ffmpeg="ffmpeg"):
    """HamTV MPEG-TS (H.264) -> MP4 the face plate can play, plus a JPEG thumbnail.
    Returns (mp4 path or None, thumbnail path or None, message)."""
    if not os.path.exists(ts_path) or os.path.getsize(ts_path) < 1000:
        return None, None, "no video received"
    base = ts_path[:-3] if ts_path.endswith(".ts") else ts_path
    mp4, jpg = base + ".mp4", base + ".jpg"
    r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", ts_path, "-c:v", "copy", "-an",
                        "-movflags", "+faststart", mp4], capture_output=True, timeout=600)
    if r.returncode != 0 or not os.path.exists(mp4):
        r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", ts_path, "-c:v", "libx264", "-preset",
                            "veryfast", "-an", "-movflags", "+faststart", mp4], capture_output=True, timeout=1800)
        if r.returncode != 0:
            return None, None, "ffmpeg: " + r.stderr.decode(errors="replace")[-200:]
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-ss", "1", "-i", mp4, "-frames:v", "1", jpg],
                   capture_output=True, timeout=120)
    return mp4, (jpg if os.path.exists(jpg) else None), "ok"


def probe_codec(path, ffmpeg="ffmpeg"):
    """Video codec name in a file (e.g. 'h264'), via ffmpeg's own report."""
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", path], capture_output=True, timeout=60)
    m = re.search(r"Video: (\w+)", r.stderr.decode(errors="replace"))
    return m.group(1) if m else ""


class Job:
    """One external decoder run (SatDump / gr-satellites / HamTV pipeline), with its output captured."""

    def __init__(self, name, cmd, cwd, shell=False, log=None):
        self.name, self.cmd, self.cwd, self.shell = name, cmd, cwd, shell
        self.log_path = log or os.path.join(cwd, "decoder.log")
        os.makedirs(cwd, exist_ok=True)
        self.started = time.time()
        self.lf = open(self.log_path, "ab")
        self.p = subprocess.Popen(cmd, cwd=cwd, shell=shell, stdout=self.lf, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL)

    def running(self):
        return self.p.poll() is None

    def stop(self, wait=10):
        if self.running():
            self.p.terminate()
            try:
                self.p.wait(wait)
            except subprocess.TimeoutExpired:
                self.p.kill()
        try:
            self.lf.close()
        except Exception:
            pass
        return self.p.returncode

    def tail(self, n=300):
        try:
            with open(self.log_path, "rb") as f:
                f.seek(max(0, os.path.getsize(self.log_path) - n))
                return f.read().decode(errors="replace").strip().splitlines()[-1:] or [""]
        except OSError:
            return [""]


MEDIA_EXT = (".png", ".jpg", ".jpeg", ".mp4", ".txt", ".kss", ".wav")


def gallery(root, limit=60):
    """Newest received files under root: list of {name, path (relative), kind, size, t}."""
    out = []
    for dp, _, fs in os.walk(root):
        for f in fs:
            if f.lower().endswith(MEDIA_EXT) and not f.endswith(".tmp"):
                p = os.path.join(dp, f)
                ext = f.lower().rsplit(".", 1)[-1]
                out.append({"name": f, "path": os.path.relpath(p, root).replace(os.sep, "/"),
                            "kind": {"png": "image", "jpg": "image", "jpeg": "image", "mp4": "video",
                                     "wav": "audio"}.get(ext, "text"),
                            "size": os.path.getsize(p), "t": os.path.getmtime(p)})
    out.sort(key=lambda x: -x["t"])
    return out[:limit]
