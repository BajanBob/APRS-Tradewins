# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""
hfcall.py - HF selective calling for Tradewind (v3.18): clear-text signalling.

  * HF Selcall on the ITU-R M.493 / CCIR 493 physical layer: 100 baud FSK
    (1615 / 1785 Hz audio, higher tone = B = 0), 10-bit characters (7 info
    bits LSB first + 3 check bits MSB first that count the zeros), dot
    pattern, DX/RX time diversity with phasing (DX 125, RX 111..104) and an
    XOR error-check character. 4- or 6-digit station IDs, up to 10 self-IDs.
    Call types: Selcall, Beacon (link test with an automatic 4-tone revertive
    and a link-quality reply - not written to the call history), Emergency,
    Pagecall (short text), GPS call (position) and Telcall (phone-patch
    request). The frame structure follows M.493; the call-type codes and
    payloads are Tradewind's own, documented below. Tradewind-to-Tradewind is
    tested; interworking with other makers' OEM selcall formats is not claimed.

  * 2G ALE (MIL-STD-188-141B Appendix A / FED-STD-1045): 8-FSK, 125 baud,
    750-2500 Hz Gray-coded tones, 24-bit words (3-bit preamble + three 7-bit
    characters), extended Golay (24,12) on each 12-bit half, bit interleave +
    stuff bit = 49 bits, each word sent three times (majority vote), 392 ms per
    word. Individual scanning call, response, acknowledgement (3-way handshake),
    soundings, link quality (BER from the vote, SNR from the tones) and AMD text.
    The Golay code is the standard one (g(x) = x^11+x^9+x^7+x^6+x^5+x+1, even
    overall parity). The receiver also learns the bit-interleave variant from
    the first genuine off-air words it hears (calibration), so it locks to real
    ALE stations even where published descriptions differ.

  * ARINC 714 SELCAL (aviation): two pairs of tones from the 16-tone table,
    decoded for listening on HF aeronautical channels. Receive only - Tradewind
    never transmits SELCAL (that needs an aeronautical station licence);
    selcal_encode() exists for tests and training recordings.

  * HFCenter: runs all three decoders on the received audio, scans a channel
    list by CAT (stopping on activity), sends calls, answers beacons and ALE
    calls automatically (each switchable), and keeps a link-quality table so
    Tradewind can pick the best channel and the right modem for a station.

Pure Python + numpy. Every Tradewind HF transmission is clear text and carries
the operator's callsign (amateur identification).
"""

import math
import threading
import time

try:
    import numpy as np
except ImportError:          # Android build: the HF decoders need numpy
    np = None

RATE = 8000                  # internal processing rate for all HF decoders


def _need_numpy():
    if np is None:
        raise RuntimeError("HF selcall / ALE needs numpy (pip install numpy)")


TEXT64 = "".join(chr(c) for c in range(32, 96))        # ALE expanded 64-ASCII set (space .. underscore)
BASIC38 = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@?")  # ALE basic address set


def text64(s, n=64):
    """Upper-case text limited to the 64-character set ('?' for anything else)."""
    return "".join(c if c in TEXT64 else "?" for c in str(s).upper())[:n]


# ===========================================================================
# Shared DSP helpers
# ===========================================================================

class Resampler:
    """Streaming low-pass + resample from the soundcard rate to RATE (8 kHz)."""

    def __init__(self, rate_in, rate_out=RATE):
        _need_numpy()
        self.rin, self.rout = float(rate_in), float(rate_out)
        self.ratio = self.rin / self.rout
        self.passthrough = abs(self.ratio - 1.0) < 1e-9
        taps = (int(self.rin / 1000 * 3) | 1)
        n = np.arange(taps) - (taps - 1) / 2
        fc = 0.45 * self.rout / self.rin
        h = 2 * fc * np.sinc(2 * fc * n) * np.hamming(taps)
        self.h = (h / h.sum()).astype(np.float64)
        self.hist = np.zeros(taps - 1)
        self.tail = np.zeros(0)
        self.pos = 0.0

    def process(self, x):
        x = np.asarray(x, dtype=np.float64).reshape(-1)
        if self.passthrough:
            return x
        y = np.concatenate([self.hist, x])
        self.hist = y[-(len(self.h) - 1):]
        y = np.convolve(y, self.h, "valid")
        buf = np.concatenate([self.tail, y])
        if len(buf) < 2:
            self.tail = buf
            return np.zeros(0)
        idx = np.arange(self.pos, len(buf) - 1, self.ratio)
        out = np.interp(idx, np.arange(len(buf)), buf)
        nxt = self.pos + len(idx) * self.ratio
        drop = int(nxt)
        self.tail = buf[drop:]
        self.pos = nxt - drop
        return out


def _ramp(a, rate, ms=4):
    f = min(len(a) // 4, int(rate * ms / 1000))
    if f > 1:
        r = np.linspace(0, 1, f)
        a[:f] *= r
        a[-f:] *= r[::-1]
    return a


def fsk_audio(freqs, sym_s, rate, level=0.5):
    """Phase-continuous tones: one frequency per symbol of sym_s seconds."""
    _need_numpy()
    edges = np.round(np.arange(len(freqs) + 1) * sym_s * rate).astype(int)
    f = np.repeat(np.asarray(freqs, dtype=np.float64), np.diff(edges))
    ph = 2 * np.pi * np.cumsum(f) / rate
    return _ramp(level * np.sin(ph), rate).astype(np.float32)


def tone_seq(tones, dur_s, rate, level=0.5, gap_s=0.0):
    """A short sequence of single tones (revertive chime)."""
    _need_numpy()
    out = []
    for f in tones:
        t = np.arange(int(dur_s * rate)) / rate
        out.append(_ramp(level * np.sin(2 * np.pi * f * t), rate, 8))
        if gap_s:
            out.append(np.zeros(int(gap_s * rate)))
    return np.concatenate(out).astype(np.float32)


def silence(sec, rate):
    return np.zeros(int(sec * rate), dtype=np.float32)


# ===========================================================================
# 1. HF Selcall - ITU-R M.493 / CCIR 493 physical layer
# ===========================================================================

SC_BAUD = 100.0
SC_Y = 1615.0                 # Y = binary 1 (lower tone)
SC_B = 1785.0                 # B = binary 0 (higher tone)
PH_DX = 125
PH_RX = [111, 110, 109, 108, 107, 106, 105, 104]
FS_SELECTIVE = 120
CAT_ROUTINE, CAT_SAFETY, CAT_URGENT = 100, 108, 110
EOS_REQ, EOS_ACK, EOS_NONE = 117, 122, 127
EOS_SET = (EOS_REQ, EOS_ACK, EOS_NONE)

# Tradewind call types (first symbol >= 100 after the self-ID)
CT = {"selcall": 100, "telcall": 101, "emergency": 112, "page": 113,
      "beacon": 118, "revertive": 119, "gps": 121}
CT_NAME = {v: k for k, v in CT.items()}
ALL_CALL = {"9999", "999999"}          # group "all stations" address (never auto-answered)
REVERTIVE_TONES = (1100.0, 1400.0, 1700.0, 2000.0)


def selcall_id_ok(s):
    s = str(s or "").strip()
    return s.isdigit() and len(s) in (4, 6)


def _id_syms(s):
    s = str(s).strip()
    return [int(s[i:i + 2]) for i in range(0, len(s), 2)]


def _syms_id(syms):
    return "".join(f"{v:02d}" for v in syms)


def _char_bits(v):
    info = [(v >> i) & 1 for i in range(7)]                 # LSB first
    zeros = 7 - sum(info)
    return info + [(zeros >> 2) & 1, (zeros >> 1) & 1, zeros & 1]   # check bits MSB first


def _gps_digits(lat, lon):
    q = (0 if lon >= 0 else 1) if lat >= 0 else (2 if lon >= 0 else 3)
    la, lo = abs(lat), abs(lon)
    ld, lm = int(la), int(round((la - int(la)) * 60))
    if lm == 60:
        ld, lm = ld + 1, 0
    od, om = int(lo), int(round((lo - int(lo)) * 60))
    if om == 60:
        od, om = od + 1, 0
    s = f"{q}{ld:02d}{lm:02d}{od:03d}{om:02d}"
    return [int(s[i:i + 2]) for i in range(0, 10, 2)]


def _digits_gps(pairs):
    s = "".join(f"{v:02d}" for v in pairs[:5])
    if len(s) != 10 or s == "9999999999":
        return None
    q, ld, lm, od, om = int(s[0]), int(s[1:3]), int(s[3:5]), int(s[5:8]), int(s[8:10])
    lat, lon = ld + lm / 60.0, od + om / 60.0
    if q in (2, 3):
        lat = -lat
    if q in (1, 3):
        lon = -lon
    return round(lat, 4), round(lon, 4)


def selcall_build(kind, to_id, from_id, callsign="", text="", number="", pos=None,
                  snr=None, acked=None, urgent=False):
    """Information characters of one Tradewind HF selcall (FS..EOS) and its ECC."""
    if kind not in CT:
        raise ValueError(f"unknown call type {kind}")
    if not selcall_id_ok(to_id):
        raise ValueError("the called ID must be 4 or 6 digits")
    if not selcall_id_ok(from_id):
        raise ValueError("your own selcall ID must be 4 or 6 digits")
    data = []
    if kind == "page":
        t = text64(text, 60)
        if not t.strip():
            raise ValueError("a Pagecall needs some text")
        data = [TEXT64.index(c) for c in t]
    elif kind == "telcall":
        n = "".join(c for c in str(number) if c.isdigit())[:16]
        if not n:
            raise ValueError("a Telcall needs the (abbreviated) number to dial")
        data = [int(c) for c in n]
    elif kind in ("gps", "emergency"):
        data = _gps_digits(*pos) if pos else [99] * 5
        if kind == "emergency" and text:
            data += [TEXT64.index(c) for c in text64(text, 40)]
    elif kind == "revertive":
        code = 50 if snr is None else int(max(0, min(99, round(snr) + 50)))
        data = [code, max(0, min(99, int(acked or 0) - 100))]
    idc = [TEXT64.index(c) for c in text64(callsign, 12)]
    cat = CAT_URGENT if (kind == "emergency" or urgent) else CAT_ROUTINE
    eos = EOS_ACK if kind == "revertive" else (EOS_NONE if to_id in ALL_CALL else EOS_REQ)
    body = _id_syms(to_id) + [cat] + _id_syms(from_id) + [CT[kind]] + [len(data)] + data + [len(idc)] + idc
    ecc = 0
    for v in [FS_SELECTIVE] + body + [eos]:
        ecc ^= v
    return [FS_SELECTIVE, FS_SELECTIVE] + body + [eos], ecc & 0x7F


def selcall_bits(info, ecc, dot_bits=20):
    """Dot pattern + phasing + DX/RX time-diversity characters, as bits."""
    dx = [PH_DX] * 6 + info + [ecc, info[-1], info[-1]]
    rx = PH_RX + info + [ecc]
    bits = [1 - (i & 1) for i in range(max(2, int(dot_bits)))]
    for a, b in zip(dx, rx):
        bits += _char_bits(a) + _char_bits(b)
    return bits


def selcall_audio(kind, to_id, from_id, rate, level=0.5, preamble_s=0.2, **kw):
    info, ecc = selcall_build(kind, to_id, from_id, **kw)
    bits = selcall_bits(info, ecc, dot_bits=int(preamble_s * SC_BAUD))
    a = fsk_audio([SC_Y if b else SC_B for b in bits], 1.0 / SC_BAUD, rate, level)
    return a


def revertive_audio(to_id, from_id, callsign, snr, acked, rate, level=0.5):
    """4-tone revertive chime followed by the link-quality reply frame."""
    chime = tone_seq(REVERTIVE_TONES, 0.15, rate, level)
    frame = selcall_audio("revertive", to_id, from_id, rate, level, preamble_s=0.2,
                          callsign=callsign, snr=snr, acked=acked)
    return np.concatenate([chime, silence(0.1, rate), frame])


def _parse_info(chars):
    """Split M.493 information characters (FS FS ... EOS) into Tradewind fields."""
    if len(chars) < 8 or chars[0] != FS_SELECTIVE or chars[1] != FS_SELECTIVE:
        return None
    s = chars[2:]
    i = 0
    addr = []
    while i < len(s) and s[i] < 100:
        addr.append(s[i]); i += 1
    if i >= len(s) or len(addr) not in (2, 3):
        return None
    cat = s[i]; i += 1
    frm = []
    while i < len(s) and s[i] < 100:
        frm.append(s[i]); i += 1
    if i >= len(s) or len(frm) not in (2, 3):
        return None
    ct = s[i]; i += 1
    if ct not in CT_NAME or i >= len(s):
        return None
    n = s[i]; i += 1
    data = s[i:i + n]; i += n
    if len(data) != n or i >= len(s):
        return None
    m = s[i]; i += 1
    idc = s[i:i + m]; i += m
    if len(idc) != m or i >= len(s) or s[i] not in EOS_SET or any(v > 63 for v in idc):
        return None
    out = {"kind": CT_NAME[ct], "to": _syms_id(addr), "from": _syms_id(frm),
           "urgent": cat == CAT_URGENT, "eos": s[i],
           "callsign": "".join(TEXT64[v] for v in idc).strip()}
    k = out["kind"]
    if k == "page":
        out["text"] = "".join(TEXT64[v] for v in data if v < 64)
    elif k == "telcall":
        out["number"] = "".join(str(v) for v in data if v < 10)
    elif k in ("gps", "emergency"):
        out["pos"] = _digits_gps(data[:5]) if len(data) >= 5 else None
        if k == "emergency":
            out["text"] = "".join(TEXT64[v] for v in data[5:] if v < 64)
    elif k == "revertive" and len(data) >= 2:
        out["snr_far"] = data[0] - 50
        out["acked"] = CT_NAME.get(data[1] + 100, "")
    return out


class SelcallDecoder:
    """Finds M.493-structured frames in a buffer of 8 kHz audio."""
    SPB = int(RATE / SC_BAUD)          # 80 samples per bit
    STEP = SPB // 8                    # 8 timing phases per bit

    def __init__(self):
        _need_numpy()
        self.done = []                 # absolute sample positions already decoded

    NOISE_REF = (800.0, 2600.0)        # clear bins well away from the FSK pair, for the noise level

    def _disc(self, x, ref=False):
        t = np.arange(len(x)) / RATE
        mags = []
        for f in (SC_Y, SC_B) + (self.NOISE_REF if ref else ()):
            z = x * np.exp(-2j * np.pi * f * t)
            c = np.concatenate([[0], np.cumsum(z)])
            m = np.abs(c[self.SPB:] - c[:-self.SPB]) / self.SPB
            mags.append(m[::self.STEP])
        if ref:                        # the two tones are not orthogonal (170 Hz x 10 ms = 1.7 cycles),
            return mags[0], mags[1], (mags[2] ** 2 + mags[3] ** 2) / 2   # so noise comes from the reference bins
        return mags[0], mags[1]        # 1615 (Y=1), 1785 (B=0)

    def activity(self, x):
        """Rough: is there 1615/1785 FSK in this audio?"""
        if len(x) < self.SPB * 4:
            return False
        y, b = self._disc(x)
        sig = np.maximum(y, b)
        e = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        fr = np.fft.rfftfreq(len(x), 1.0 / RATE)
        band = e[(fr > 1550) & (fr < 1850)].mean()
        rest = e[((fr > 400) & (fr < 1450)) | ((fr > 1950) & (fr < 3000))].mean() + 1e-12
        return band > 4 * rest and float(np.mean(np.abs(y - b) > 0.5 * sig)) > 0.6

    def decode(self, x, base_abs):
        """Return complete frames found in x (8 kHz). base_abs = absolute index of x[0]."""
        out = []
        if len(x) < self.SPB * 60:
            return out
        y, b, noisep = self._disc(x, ref=True)
        d = y - b
        sigp = np.maximum(y, b) ** 2
        best = None
        for p in range(8):
            bits = (d[p::8] > 0).astype(np.int64)
            if len(bits) < 200:
                continue
            w = np.lib.stride_tricks.sliding_window_view(bits, 10)
            info = w[:, :7] @ (1 << np.arange(7))
            chk = w[:, 7] * 4 + w[:, 8] * 2 + w[:, 9]
            zeros = 7 - np.array([bin(v).count("1") for v in range(128)])[info]
            ch = np.where(chk == zeros, info, -1)
            for n in np.nonzero((ch >= 104) & (ch <= 111))[0]:
                k = 111 - int(ch[n])
                base = int(n) - 20 * k - 10
                if base < 0:
                    continue
                dxn = sum(1 for j in range(6) if 0 <= base + 20 * j < len(ch) and ch[base + 20 * j] == PH_DX)
                rxn = sum(1 for j in range(8) if base + 20 * j + 10 < len(ch) and ch[base + 20 * j + 10] == 111 - j)
                if not ((dxn >= 2 and rxn >= 1) or rxn >= 3):
                    continue
                score = dxn + rxn
                if best is None or score > best[0] or (score == best[0] and base < best[2]):
                    best = (score, p, base, ch)
            if best is not None and best[1] == p:
                frame = self._read(best[3], best[2])
                if frame:
                    start_abs = base_abs + (best[2] * 8 + p) * self.STEP
                    if all(abs(start_abs - s) > self.SPB * 40 for s in self.done):
                        # SNR at the bit centres: try the 8 timing phases, keep the best-centred one
                        lo = max(0, best[2] * 8)
                        hi = min(len(sigp), lo + frame.pop("_bits") * 8)
                        m = ((hi - lo) // 8) * 8
                        S = sigp[lo:lo + m].reshape(-1, 8).mean(0)
                        Nn = noisep[lo:lo + m].reshape(-1, 8).mean(0) + 1e-12
                        ratio = float(np.max(S / Nn))
                        frame["snr"] = round(10 * math.log10(max(ratio - 1, 1e-3)) - 10 * math.log10(2500 / 100), 1)
                        frame["_abs"] = start_abs
                        self.done.append(start_abs)
                        self.done = self.done[-50:]
                        out.append(frame)
                    best = None
        return out

    @staticmethod
    def _read(ch, base):
        chars = []
        j = 0
        while True:
            pd, pr = base + 20 * (6 + j), base + 20 * (8 + j) + 10
            if pd >= len(ch):
                return None                            # not complete yet
            dx = int(ch[pd])
            rx = int(ch[pr]) if pr < len(ch) else -1
            v = dx if dx >= 0 else rx
            if v < 0:
                return None
            chars.append(v)
            if len(chars) > 3 and v in EOS_SET:
                parsed = _parse_info(chars)
                if parsed:
                    pe, re_ = base + 20 * (7 + j), base + 20 * (9 + j) + 10
                    eccs = [int(ch[q]) for q in (pe, re_) if q < len(ch) and ch[q] >= 0]
                    ecc = 0
                    for c in chars[1:]:
                        ecc ^= c
                    if eccs and (ecc & 0x7F) in eccs:
                        parsed["_bits"] = (2 * (9 + j) + 2) * 10
                        return parsed
                    return None
            if j > 200:
                return None
            j += 1


# ===========================================================================
# 2. 2G ALE - MIL-STD-188-141B Appendix A
# ===========================================================================

ALE_BAUD = 125.0
ALE_TONES = [750.0 + 250.0 * k for k in range(8)]
TRIBIT_TONE = [0, 1, 3, 2, 7, 6, 4, 5]          # 000=750 001=1000 011=1250 010=1500 110=1750 111=2000 101=2250 100=2500
TONE_TRIBIT = [TRIBIT_TONE.index(t) for t in range(8)]
PRE = {"DATA": 0, "THRU": 1, "TO": 2, "TWAS": 3, "FROM": 4, "TIS": 5, "CMD": 6, "REP": 7}
PRE_NAME = {v: k for k, v in PRE.items()}
WORD_S = 49 / ALE_BAUD                          # 0.392 s per (tripled) word on the air

# Golay (24,12): check bits of each single data bit (data bit i = bit i of the 12-bit half).
# Derived from g(x) = x^11+x^9+x^7+x^6+x^5+x+1 with an even overall parity bit; these are the
# values of the standard ALE encode table (e.g. 1 -> 0x5C7, 2 -> 0xB8D, 3 -> 0xE4A, 0xFFF -> 0xFFF).
GOLAY_UNIT = [0x5C7, 0xB8D, 0x2DE, 0x5BC, 0xB78, 0x337, 0x66D, 0xCD9, 0xC76, 0xD2B, 0xF92, 0xAE3]
_GOLAY = {}


def _golay_tables():
    if _GOLAY:
        return _GOLAY
    enc = [0] * 4096
    for d in range(1, 4096):
        low = d & -d
        enc[d] = enc[d ^ low] ^ GOLAY_UNIT[low.bit_length() - 1]
    err = [-1] * 4096
    pats = [0]
    for i in range(24):
        pats.append(1 << i)
        for j in range(i):
            pats.append((1 << i) | (1 << j))
            for k in range(j):
                pats.append((1 << i) | (1 << j) | (1 << k))
    for e in pats:
        s = enc[e >> 12] ^ (e & 0xFFF)
        if err[s] < 0:
            err[s] = e
    _GOLAY["enc"] = enc
    _GOLAY["err"] = err
    if np is not None:
        _GOLAY["enc_np"] = np.array(enc, dtype=np.int64)
        _GOLAY["err_np"] = np.array(err, dtype=np.int64)
    return _GOLAY


def golay_encode(d12):
    return ((d12 & 0xFFF) << 12) | _golay_tables()["enc"][d12 & 0xFFF]


def golay_decode(c24):
    """(data12, corrected bit count) or (None, 4) when there are 4 or more errors."""
    t = _golay_tables()
    s = t["enc"][(c24 >> 12) & 0xFFF] ^ (c24 & 0xFFF)
    e = t["err"][s]
    if e < 0:
        return None, 4
    return ((c24 ^ e) >> 12) & 0xFFF, bin(e).count("1")


# interleave variants: (A bit first, B word inverted, stuff bit at the end)
ALE_VARIANTS = [(a, b, s) for s in (True, False) for b in (True, False) for a in (True, False)]
ALE_DEFAULT_VARIANT = (True, True, True)


def _word_bits49(w24, variant=ALE_DEFAULT_VARIANT):
    a_first, b_inv, stuff_end = variant
    ga = golay_encode((w24 >> 12) & 0xFFF)
    gb = golay_encode(w24 & 0xFFF) ^ (0xFFFFFF if b_inv else 0)
    il = []
    for i in range(23, -1, -1):
        x, y = (ga >> i) & 1, (gb >> i) & 1
        il += [x, y] if a_first else [y, x]
    return il + [0] if stuff_end else [0] + il


def ale_word(pre, chars):
    """24-bit word from a preamble name and 3 characters."""
    c = (str(chars) + "@@@")[:3]
    return (PRE[pre] << 21) | (ord(c[0]) & 0x7F) << 14 | (ord(c[1]) & 0x7F) << 7 | (ord(c[2]) & 0x7F)


def ale_word_text(w):
    return PRE_NAME[(w >> 21) & 7], "".join(chr((w >> s) & 0x7F) for s in (14, 7, 0))


def ale_word_symbols(w24, variant=ALE_DEFAULT_VARIANT):
    bits = _word_bits49(w24, variant) * 3                # three copies = 147 bits = 49 tribits
    return [TRIBIT_TONE[bits[i] << 2 | bits[i + 1] << 1 | bits[i + 2]] for i in range(0, 147, 3)]


def ale_audio(words, rate, level=0.5, variant=ALE_DEFAULT_VARIANT):
    syms = []
    for w in words:
        syms += ale_word_symbols(w, variant)
    return fsk_audio([ALE_TONES[s] for s in syms], 1.0 / ALE_BAUD, rate, level)


def ale_addr_ok(a):
    a = str(a or "").upper().strip()
    return 1 <= len(a) <= 15 and all(c in BASIC38 for c in a) and "@" not in a and "?" not in a


def ale_addr(callsign):
    """ALE address from a callsign: no SSID, letters and digits only."""
    return "".join(c for c in str(callsign).upper().split("-")[0] if c.isalnum())[:15]


def ale_address_words(pre, addr):
    a = ale_addr(addr)
    a += "@" * (-len(a) % 3)
    out = []
    for i in range(0, len(a), 3):
        p = pre if i == 0 else ("DATA" if (i // 3) % 2 == 1 else "REP")
        out.append(ale_word(p, a[i:i + 3]))
    return out


def ale_message_words(text):
    t = text64(text, 45)
    t += " " * (-len(t) % 3)
    return [ale_word("CMD" if i == 0 else ("DATA" if (i // 3) % 2 == 1 else "REP"), t[i:i + 3])
            for i in range(0, len(t), 3)]


def ale_call_words(to, me, scan_s=0.0, text=""):
    """Individual scanning call: first address word repeated for the scan time, then
    the full called address, an optional AMD message, and THIS IS (response wanted)."""
    lead = ale_address_words("TO", to)
    n_scan = int(math.ceil(scan_s / WORD_S)) if scan_s > 0 else 0
    words = [lead[0]] * n_scan + lead
    if text:
        words += ale_message_words(text)
    return words + ale_address_words("TIS", me)


def ale_response_words(caller, me, text=""):
    return ale_address_words("TO", caller) + (ale_message_words(text) if text else []) + ale_address_words("TIS", me)


def ale_ack_words(called, me, text=""):
    return ale_address_words("TO", called) + (ale_message_words(text) if text else []) + ale_address_words("TWAS", me)


def ale_sound_words(me, scan_s=0.0):
    """Sounding: THIS WAS (no response wanted), repeated to cover a scan cycle."""
    w = ale_address_words("TWAS", me)
    reps = max(1, int(math.ceil(scan_s / (WORD_S * len(w))))) if scan_s > 0 else 1
    return w * reps


def ale_parse(words):
    """Turn a run of words into {'to','conclusion','kind' (TIS/TWAS),'text','frm'}."""
    segs = []
    for w in words:
        pre, ch = ale_word_text(w)
        if pre in ("TO", "TIS", "TWAS", "CMD", "THRU", "FROM"):
            if segs and segs[-1][0] == pre and pre in ("TO", "TIS", "TWAS") and segs[-1][1] == ch:
                segs[-1] = [pre, ch]                      # scanning repetition
            else:
                segs.append([pre, ch])
        elif pre in ("DATA", "REP") and segs:
            segs[-1][1] += ch
    to = [s[1] for s in segs if s[0] == "TO"]
    con = [s for s in segs if s[0] in ("TIS", "TWAS")]
    msgs = [s[1] for s in segs if s[0] == "CMD"]
    clean = lambda s: s.rstrip("@").strip()
    return {"to": clean(to[-1]) if to else "",
            "frm": clean(con[-1][1]) if con else "",
            "kind": con[-1][0] if con else "",
            "text": " ".join(m.strip() for m in msgs).strip()}


class AleDecoder:
    """8-FSK ALE word decoder over a buffer of 8 kHz audio (vectorised)."""
    SPS = int(RATE / ALE_BAUD)            # 64 samples per symbol
    STEP = SPS // 8                       # 8 timing phases per symbol

    def __init__(self, variant=None):
        _need_numpy()
        n = np.arange(self.SPS)
        self.W = np.exp(-2j * np.pi * np.outer(n, ALE_TONES) / RATE).astype(np.complex64)
        self.variant = variant            # None until calibrated (searches all variants)
        self.done = []                    # (abs_start, abs_end) of runs already reported
        tb = np.array([[(TONE_TRIBIT[t] >> s) & 1 for s in (2, 1, 0)] for t in range(8)], dtype=np.int64)
        self.TB = tb
        _golay_tables()
        ok = np.zeros(128, bool)
        ok[32:96] = True
        self.ok64 = ok
        ok38 = np.zeros(128, bool)
        for c in BASIC38:
            ok38[ord(c)] = True
        self.ok38 = ok38

    def energies(self, x):
        if len(x) < self.SPS:
            return np.zeros((0, 8))
        f = np.lib.stride_tricks.sliding_window_view(x.astype(np.float32), self.SPS)[::self.STEP]
        return np.abs(f @ self.W) ** 2

    def activity(self, x):
        E = self.energies(x)
        if len(E) < 16:
            return False
        dom = E.max(1) / (E.sum(1) + 1e-12)
        return float(np.mean(dom > 0.55)) > 0.6

    def _decode_many(self, syms, variant):
        """syms: (N,49) tone indices -> words, golay errors, vote disagreements, valid."""
        bits = self.TB[syms].reshape(len(syms), 147)
        cp = bits.reshape(len(syms), 3, 49).sum(1)
        voted = (cp >= 2).astype(np.int64)
        dis = ((cp == 1) | (cp == 2)).sum(1)
        a_first, b_inv, stuff_end = variant
        il = voted[:, :48] if stuff_end else voted[:, 1:]
        A, B = (il[:, 0::2], il[:, 1::2]) if a_first else (il[:, 1::2], il[:, 0::2])
        pw = (1 << np.arange(23, -1, -1)).astype(np.int64)
        ga, gb = A @ pw, B @ pw
        if b_inv:
            gb = gb ^ 0xFFFFFF
        enc, err = _GOLAY["enc_np"], _GOLAY["err_np"]
        res = []
        for g in (ga, gb):
            e = err[enc[(g >> 12) & 0xFFF] ^ (g & 0xFFF)]
            ok = e >= 0
            d = ((g ^ np.where(ok, e, 0)) >> 12) & 0xFFF
            ne = np.array([bin(int(v)).count("1") if v >= 0 else 4 for v in e])
            res.append((d, ok, ne))
        w = (res[0][0] << 12) | res[1][0]
        errs = res[0][2] + res[1][2]
        pre = (w >> 21) & 7
        c = [(w >> s) & 0x7F for s in (14, 7, 0)]
        addr_pre = (pre == PRE["TO"]) | (pre == PRE["TIS"]) | (pre == PRE["TWAS"]) | (pre == PRE["THRU"]) | (pre == PRE["FROM"])
        chars_ok = np.where(addr_pre, self.ok38[c[0]] & self.ok38[c[1]] & self.ok38[c[2]],
                            (self.ok64[c[0]] | ((pre == PRE["CMD"]) & (c[0] >= 97) & (c[0] <= 122)))
                            & self.ok64[c[1]] & self.ok64[c[2]])
        valid = res[0][1] & res[1][1] & (dis <= 16) & chars_ok
        return w, errs, dis, valid

    def decode(self, x, base_abs):
        """Find complete runs (>= 2 words) in x. Returns [{'words','start','end','ber','snr','variant'}]."""
        out = []
        E = self.energies(x)
        if len(E) < 49 * 8 * 2:
            return out
        top = E.argmax(1)
        dom = E.max(1) / (E.sum(1) + 1e-12)
        variants = [self.variant] if self.variant else ALE_VARIANTS
        cands = []
        for p in range(8):
            S = top[p::8]
            D = dom[p::8]
            n = len(S) - 49
            if n <= 0:
                continue
            cd = np.concatenate([[0], np.cumsum(D)])
            mean_dom = (cd[49:] - cd[:-49]) / 49
            pos = np.nonzero(mean_dom[:n] > 0.45)[0]
            if not len(pos):
                continue
            win = np.lib.stride_tricks.sliding_window_view(S, 49)[pos]
            for v in variants:
                w, errs, dis, valid = self._decode_many(win, v)
                good = {int(pos[i]): (int(w[i]), int(errs[i]), int(dis[i])) for i in np.nonzero(valid)[0]}
                for st in sorted(good):
                    if st - 49 in good:
                        continue                          # not the start of a run
                    run, k = [], st
                    while k in good:
                        run.append(good[k]); k += 49
                    if len(run) >= 2:
                        cands.append((len(run), -sum(r[1] for r in run), p, st, run, v))
        cands.sort(reverse=True)
        taken = []
        for ln, nerr, p, st, run, v in cands:
            a = base_abs + (p + 8 * st) * self.STEP
            b = a + len(run) * 49 * self.SPS
            if any(not (b <= x0 or a >= x1) for x0, x1 in taken + self.done):
                continue
            end_sym = st + 49 * len(run)
            if (p + 8 * end_sym) * self.STEP + 49 * self.SPS <= len(x):
                pass                                      # the run has ended inside the buffer
            else:
                continue                                  # may still be going: decide next time
            taken.append((a, b))
            lo = p + 8 * st
            seg = E[lo: lo + 8 * 49 * len(run): 8]
            sel = seg.max(1)
            oth = (seg.sum(1) - sel) / 7 + 1e-12
            snr = 10 * math.log10(max(float(np.mean((sel - oth) / oth)), 1e-3)) - 10 * math.log10(2500 / 125)
            ber = float(np.mean([r[2] for r in run])) / 49.0
            out.append({"words": [r[0] for r in run], "start": a, "end": b,
                        "snr": round(snr, 1), "ber": round(ber, 3), "variant": v})
        self.done = (self.done + taken)[-60:]
        return out


# ===========================================================================
# 3. ARINC 714 SELCAL (aviation) - receive only
# ===========================================================================

SELCAL_TONES = {"A": 312.6, "B": 346.7, "C": 384.6, "D": 426.6, "E": 472.9, "F": 524.8,
                "G": 582.1, "H": 645.7, "J": 716.1, "K": 794.3, "L": 881.0, "M": 977.2,
                "P": 1083.9, "Q": 1202.3, "R": 1333.5, "S": 1479.1}
SELCAL_LETTERS = list(SELCAL_TONES)


def selcal_code_ok(code):
    c = str(code or "").upper().replace("-", "")
    return len(c) == 4 and all(x in SELCAL_TONES for x in c) and len(set(c)) == 4


def selcal_encode(code, rate, level=0.4):
    """Test / training audio for a SELCAL code such as 'AB-CD' (never transmitted)."""
    _need_numpy()
    if not selcal_code_ok(code):
        raise ValueError("SELCAL code: 4 different letters from A-H, J-M, P-S, e.g. AB-CD")
    c = str(code).upper().replace("-", "")
    t = np.arange(int(rate * 1.0)) / rate
    parts = []
    for pair in (c[:2], c[2:]):
        s = sum(np.sin(2 * np.pi * SELCAL_TONES[x] * t) for x in pair) * (level / 2)
        parts += [_ramp(s, rate, 10), np.zeros(int(rate * 0.2))]
    return np.concatenate(parts[:-1]).astype(np.float32)


class SelcalDecoder:
    WIN, HOP = 800, 400                   # 0.1 s windows, 0.05 s hop at 8 kHz

    def __init__(self):
        _need_numpy()
        n = np.arange(self.WIN)
        f = np.array([SELCAL_TONES[k] for k in SELCAL_LETTERS])
        self.W = (np.exp(-2j * np.pi * np.outer(n, f) / RATE) * np.hanning(self.WIN)[:, None]).astype(np.complex64)
        self.done = []

    def decode(self, x, base_abs):
        out = []
        if len(x) < self.WIN * 30:
            return out
        fr = np.lib.stride_tricks.sliding_window_view(x.astype(np.float32), self.WIN)[::self.HOP]
        E = np.abs(fr @ self.W) ** 2
        o = np.argsort(E, 1)
        i1, i2 = o[:, -1], o[:, -2]
        e1, e2 = E[np.arange(len(E)), i1], E[np.arange(len(E)), i2]
        tot = E.sum(1) + 1e-12
        floor = np.percentile(tot, 20) + 1e-12     # quiet frames, even when tones fill much of the buffer
        good = ((e1 + e2) / tot > 0.55) & (e2 > 0.2 * e1) & (tot > 4 * floor)
        pair = np.where(good, np.minimum(i1, i2) * 16 + np.maximum(i1, i2), -1)
        runs, k = [], 0
        while k < len(pair):
            if pair[k] < 0:
                k += 1
                continue
            j = k
            while j < len(pair) and pair[j] == pair[k]:
                j += 1
            runs.append((k, j, int(pair[k])))
            k = j
        pulses = [r for r in runs if 14 <= r[1] - r[0] <= 34]   # 0.7 .. 1.7 s
        for a, b in zip(pulses, pulses[1:]):
            gap = (b[0] - a[1]) * self.HOP / RATE
            if a[2] != b[2] and 0.0 <= gap <= 0.6:
                code = "".join(SELCAL_LETTERS[a[2] // 16] + SELCAL_LETTERS[a[2] % 16]) + "-" + \
                       "".join(SELCAL_LETTERS[b[2] // 16] + SELCAL_LETTERS[b[2] % 16])
                t_abs = base_abs + a[0] * self.HOP
                if b[1] >= len(pair) - 2:
                    continue                           # second tone may still be on
                if all(abs(t_abs - d) > RATE for d in self.done) and selcal_code_ok(code):
                    self.done = (self.done + [t_abs])[-40:]
                    out.append({"code": code, "abs": t_abs})
        return out


# ===========================================================================
# Link quality -> channel and modem choice
# ===========================================================================

def recommend_modem(snr_db):
    """Best Tradewind HF modem for a measured SNR (dB in 2.5 kHz), with ~3 dB margin.
    300 baud packet needs a good path; Robust decodes to about -10 dB; Robust DX ~3 dB lower."""
    if snr_db is None:
        return None, "no measurement yet - send a Beacon call or an ALE call first"
    if snr_db >= 10:
        return "300", f"{snr_db:+.0f} dB: strong path - 300 baud HF packet (fastest)"
    if snr_db >= -7:
        return "robust", f"{snr_db:+.0f} dB: Robust (44 bit/s, works down to about -10 dB)"
    if snr_db >= -13:
        return "robust_dx", f"{snr_db:+.0f} dB: weak path - Robust DX (deepest Tradewind mode)"
    return None, f"{snr_db:+.0f} dB: below what any Tradewind modem can carry - try another channel"


def parse_channels(text):
    """'7102 40m, 10145.5, 14109:20m' -> [{'khz': 7102.0, 'label': '40m'}, ...] (HF 1.8-30 MHz)."""
    out = []
    for part in str(text or "").replace(";", ",").replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        f, _, lab = part.replace(":", " ").partition(" ")
        try:
            khz = float(f)
        except ValueError:
            raise ValueError(f"channel '{part}': give the dial frequency in kHz, e.g. 7102")
        if not 1800 <= khz <= 30000:
            raise ValueError(f"channel {khz:g} kHz is outside the HF bands (1800-30000 kHz)")
        out.append({"khz": khz, "label": lab.strip()[:12]})
    return out[:40]


# International amateur ALE channels (HFN, USB dial kHz) - verify your band plan and licence.
HFN_CHANNELS = "3596 80m, 5357 60m, 7102 40m, 10145.5 30m, 14109 20m, 18106 17m, 21096 15m, 24926 12m, 28146 10m"

# Amateur HF allocations (union of the ITU regions, kHz). Tradewind listens anywhere on HF but only
# transmits inside these - your own licence and band plan still decide what you may use.
AMATEUR_HF_KHZ = ((1800, 2000), (3500, 4000), (5330, 5410), (7000, 7300), (10100, 10150), (14000, 14350),
                  (18068, 18168), (21000, 21450), (24890, 24990), (28000, 29700))


def amateur_hf(khz):
    """True if a USB signal on this dial frequency (up to 3 kHz wide) stays inside an amateur HF band.
    None (no CAT, frequency unknown) is allowed: the operator has set the radio."""
    return khz is None or any(lo <= khz and khz + 3.0 <= hi for lo, hi in AMATEUR_HF_KHZ)


# ===========================================================================
# HFCenter - decoders, scanning, calls, automatic answers, link quality
# ===========================================================================

class HFCenter:
    HOLD_S = 8.0

    def __init__(self, cfg, transmit, tune=None, log=None, tx_active=None, rate=48000,
                 pos=None, now=time.time, threaded=True, busy=None, tx_ok=None):
        _need_numpy()
        self.cfg = cfg
        self.transmit = transmit                 # transmit(audio_float32, max_s)
        self.tune_fn = tune                      # tune(khz) -> bool, or None (no CAT)
        self.log = log or (lambda kind, text: None)
        self.tx_active = tx_active or (lambda: False)
        self.busy = busy or (lambda: False)      # radio needed elsewhere (satellite pass, chat QSY): no scanning
        self.tx_ok = tx_ok or amateur_hf         # may this channel be transmitted on?
        self.rate = int(rate)
        self.pos = pos or (lambda: None)
        self.now = now
        self.threaded = threaded
        self.call = str(cfg.get("call") or "").upper()
        self.ids = [s.strip() for s in str(cfg.get("hf_ids") or "").replace(" ", ",").split(",") if s.strip()][:10]
        addrs = [ale_addr(a) for a in str(cfg.get("hf_ale_addrs") or "").replace(" ", ",").split(",") if a.strip()]
        self.addrs = (addrs or [ale_addr(self.call)])[:10]
        self.channels = parse_channels(cfg.get("hf_channels") or "")
        self.dwell = max(0.5, float(cfg.get("hf_dwell") or 1.0))
        self.revertive = bool(cfg.get("hf_revertive", True))
        self.ale_respond = bool(cfg.get("hf_ale_respond", True))
        self.selcal_on = bool(cfg.get("hf_selcal"))
        self.sound_min = int(cfg.get("hf_sound_min") or 0)
        v = str(cfg.get("hf_ale_variant") or "")
        variant = tuple(c == "1" for c in v) if len(v) == 3 and set(v) <= {"0", "1"} else None
        self.rs = Resampler(self.rate, RATE)
        self.sc = SelcallDecoder()
        self.ale = AleDecoder(variant)
        self.selcal = SelcalDecoder()
        self.lock = threading.Lock()
        self.buf = np.zeros(0)
        self.base = 0                            # absolute 8 kHz index of buf[0]
        self.max_buf = RATE * 40
        self.scan_on = bool(cfg.get("hf_scan")) and len(self.channels) >= 2 and tune is not None
        self.ch_i = 0
        self.khz = None
        self.hold_until = 0.0
        self.next_hop = 0.0
        self.tx_until = 0.0
        self.pending = None                      # outgoing call awaiting an answer
        self.linked = None
        self.history = []                        # calls (not beacons / revertives)
        self.lqa = {}                            # (station, khz) -> record
        self.heard = {}                          # ALE / selcall stations heard
        self.selcals = []
        self.status = ""
        self.next_sound = self.now() + 60 if self.sound_min else 0
        self.events = 0
        self.variant_learned = None
        self._stop = threading.Event()
        self._last_proc = 0.0
        self._thread = None

    # ---- lifecycle
    def start(self):
        if self.threaded:                         # the radio stays where it is until a scan or a call moves it
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    def summary(self):
        parts = []
        if self.ids:
            parts.append("selcall ID " + ", ".join(self.ids))
        parts.append("ALE " + ", ".join(self.addrs))
        if self.channels:
            parts.append(f"{len(self.channels)} channels" + (f", scanning {self.dwell:g} s each" if self.scan_on else ""))
        if self.channels and not self.tune_fn:
            parts.append("no CAT link: set the radio by hand (no scanning)")
        if self.selcal_on:
            parts.append("ARINC SELCAL decode")
        return "; ".join(parts)

    def tune_to(self, khz):
        """Operator picked a channel: go there and stay (scanning pauses for a while)."""
        self.hold_until = self.now() + 120
        if not self.tune_fn:                      # no CAT: the operator has set the dial by hand
            self.khz = float(khz)
            return True
        return self._tune(float(khz))

    def sound_now(self, khz=None):
        self._go_channel(khz)
        self.sound()

    def _worker(self):
        while not self._stop.wait(0.5):
            try:
                self.tick()
            except Exception as e:                # never take the station down
                self.status = f"HF decoder error: {e}"

    # ---- audio in
    def feed(self, chunk):
        if self.tx_active() or self.now() < self.tx_until:
            return                                # never decode our own transmission
        y = self.rs.process(chunk)
        with self.lock:
            self.buf = np.concatenate([self.buf, y])
            if len(self.buf) > self.max_buf:
                cut = len(self.buf) - self.max_buf
                self.buf = self.buf[cut:]
                self.base += cut

    # ---- periodic work
    def tick(self):
        now = self.now()
        with self.lock:
            x, base = self.buf.copy(), self.base
        tail = x[-int(RATE * 0.6):] if len(x) > RATE * 0.6 else x
        if len(tail) > RATE * 0.3 and (self.ale.activity(tail) or self.sc.activity(tail)):
            self.hold_until = now + self.HOLD_S
        if len(x) > RATE:
            for fr in self.sc.decode(x, base):
                self._on_selcall(fr)
            for run in self.ale.decode(x, base):
                self._on_ale(run)
            if self.selcal_on:
                for s in self.selcal.decode(x, base):
                    self._on_selcal(s)
        self._timeouts(now)
        self._scan(now)
        if self.sound_min and now >= self.next_sound and not self.pending and now >= self.tx_until:
            self.next_sound = now + self.sound_min * 60
            try:
                self.sound()
            except ValueError as e:
                self.status = f"no sounding: {e}"

    # ---- scanning
    def _tune(self, khz):
        if self.tune_fn and self.tune_fn(khz):
            self.khz = khz
            with self.lock:                       # audio from the old channel is no use
                self.base += len(self.buf)
                self.buf = np.zeros(0)
            return True
        return False

    def _scan(self, now):
        if not self.scan_on or now < self.next_hop:
            return
        if now < self.hold_until or self.pending or now < self.tx_until or self.tx_active() or self.busy():
            return
        self.ch_i = (self.ch_i + 1) % len(self.channels)
        self._tune(self.channels[self.ch_i]["khz"])
        self.next_hop = now + self.dwell

    def set_scan(self, on):
        self.scan_on = bool(on) and len(self.channels) >= 2 and self.tune_fn is not None
        return self.scan_on

    def scan_cycle_s(self):
        return self.dwell * len(self.channels) + 0.5 if self.scan_on or self.cfg.get("hf_far_scans") else 0.0

    # ---- transmit helpers
    def _check_tx(self):
        if not self.tx_ok(self.khz):
            raise ValueError(f"{self.khz:g} kHz is outside the amateur HF bands - Tradewind only listens there")

    def _send(self, audio, why):
        self._check_tx()
        dur = len(audio) / float(self.rate)
        self.transmit(audio, max(10.0, dur + 2))
        self.tx_until = self.now() + dur + 2.5
        self.status = why

    def _auto_send(self, audio, why):
        """Automatic answer (revertive, ALE response / ack): skipped, not raised, where we may not transmit."""
        try:
            self._send(audio, why)
            return True
        except ValueError as e:
            self.status = f"{why} not sent: {e}"
            return False

    def _go_channel(self, khz):
        if khz and self.tune_fn and khz != self.khz:
            self._tune(khz)
        self.hold_until = self.now() + 30

    def _my_id(self):
        if not self.ids:
            raise ValueError("Set your own selcall ID (4 or 6 digits) under HF CALLING first.")
        return self.ids[0]

    # ---- outgoing calls
    def selcall(self, kind, to, text="", number="", khz=None):
        """Selcall / beacon / emergency / page / gps / telcall to a 4- or 6-digit ID."""
        me = self._my_id()
        to = str(to).strip()
        if kind not in ("selcall", "beacon", "emergency", "page", "gps", "telcall"):
            raise ValueError("unknown HF call type")
        if not selcall_id_ok(to):
            raise ValueError("Call a 4 or 6 digit selcall ID")
        khz = khz or self.best_channel(to) or self.khz
        self._go_channel(khz)
        pos = self.pos() if kind in ("gps", "emergency") else None
        pre = max(0.2, min(20.0, self.scan_cycle_s() or float(self.cfg.get("hf_preamble_s") or 0.2)))
        a = selcall_audio(kind, to, me, self.rate, preamble_s=pre, callsign=self.call, text=text,
                          number=number, pos=pos)
        if to not in ALL_CALL:
            self.pending = {"kind": kind, "to": to, "via": "selcall", "t": self.now(),
                            "khz": khz, "deadline": self.now() + len(a) / self.rate + 25}
        if kind != "beacon":
            self._hist("tx", kind, me, to, text or number, khz, None,
                       "sent" if to in ALL_CALL else "waiting for answer")
        self._send(a, f"{kind.upper()} to {to}" + (f" on {khz:g} kHz" if khz else ""))
        return True

    def ale_call(self, to, text="", khz=None):
        to = ale_addr(to)
        if not ale_addr_ok(to):
            raise ValueError("ALE address: 1-15 letters and digits (a callsign)")
        khz = khz or self.best_channel(to) or self.khz
        self._go_channel(khz)
        words = ale_call_words(to, self.addrs[0], self.scan_cycle_s(), text)
        a = ale_audio(words, self.rate, variant=self._tx_variant())
        self.pending = {"kind": "ale", "to": to, "via": "ale", "t": self.now(), "khz": khz,
                        "deadline": self.now() + len(a) / self.rate + 20}
        self.linked = None
        self._hist("tx", "ale call", self.addrs[0], to, text, khz, None, "calling")
        self._send(a, f"ALE call to {to}" + (f" on {khz:g} kHz" if khz else ""))
        return True

    def sound(self):
        words = ale_sound_words(self.addrs[0], self.scan_cycle_s())
        self._send(ale_audio(words, self.rate, variant=self._tx_variant()),
                   f"ALE sounding as {self.addrs[0]}")

    def _tx_variant(self):
        return self.ale.variant or ALE_DEFAULT_VARIANT

    # ---- incoming
    def _for_me(self, to_id):
        return to_id in self.ids

    def _on_selcall(self, f):
        now = self.now()
        frm, kind = f["from"], f["kind"]
        self.heard[frm] = {"station": frm, "callsign": f.get("callsign", ""), "t": now, "khz": self.khz,
                           "snr": f.get("snr"), "via": "selcall"}
        if kind == "revertive":
            p = self.pending
            if self._for_me(f["to"]) and p and p["via"] == "selcall" and p["to"] == frm:
                self._lqa(frm, p.get("khz") or self.khz, f.get("snr"), f.get("snr_far"), None, "selcall")
                if p["kind"] != "beacon":
                    self._hist_update(frm, "answered")
                self.log("HF", f"{(p['kind'] if p['kind'] != 'beacon' else 'Beacon').upper()} answered by {frm} "
                               f"{f.get('callsign', '')}: heard there at {f.get('snr_far', '?')} dB, "
                               f"here at {f.get('snr', '?')} dB")
                self.pending = None
            return
        if not (self._for_me(f["to"]) or f["to"] in ALL_CALL):
            if kind != "beacon":
                self._hist("rx", kind, frm, f["to"], f.get("text") or f.get("number") or "", self.khz,
                           f.get("snr"), "heard (not for us)", f.get("callsign", ""))
            return
        self.hold_until = now + 20
        if kind == "beacon":
            self._lqa(frm, self.khz, f.get("snr"), None, None, "selcall")
        else:
            desc = {"selcall": "Selcall", "emergency": "EMERGENCY call", "page": "Pagecall",
                    "gps": "GPS call", "telcall": "Telcall"}[kind]
            extra = f.get("text") or ""
            if kind == "telcall":
                extra = f"dial {f.get('number', '')} (phone patch request - needs a telephone gateway)"
            if f.get("pos"):
                extra = (extra + " " if extra else "") + f"at {f['pos'][0]:.3f}, {f['pos'][1]:.3f}"
            self._hist("rx", kind, frm, f["to"], extra, self.khz, f.get("snr"), "received", f.get("callsign", ""))
            self.log("ALERT" if kind == "emergency" else "HF",
                     f"{desc} from {frm} {f.get('callsign', '')}" + (f": {extra}" if extra else ""))
        if self.revertive and f["eos"] == EOS_REQ and f["to"] in self.ids:
            a = revertive_audio(frm, f["to"], self.call, f.get("snr"), CT[kind], self.rate)
            self._auto_send(a, f"revertive to {frm}")

    def _on_ale(self, run):
        now = self.now()
        msg = ale_parse(run["words"])
        if run["variant"] != ALE_DEFAULT_VARIANT and not self.ale.variant:
            pass
        if not self.ale.variant and len(run["words"]) >= 2:
            self.ale.variant = run["variant"]     # calibrated on genuine off-air words
            self.variant_learned = "".join("1" if b else "0" for b in run["variant"])
            self.log("SYS", f"ALE receiver locked to the bit-interleave used on the air ({self.variant_learned})")
        frm, to, kind = msg["frm"], msg["to"], msg["kind"]
        if frm:
            self.heard[frm] = {"station": frm, "callsign": frm, "t": now, "khz": self.khz, "snr": run["snr"],
                               "ber": run["ber"], "via": "ALE " + ("sounding" if not to else "call")}
            self._lqa(frm, self.khz, run["snr"], self._far_snr(msg["text"]), run["ber"], "ale")
        me = to in self.addrs
        p = self.pending
        if me and kind == "TIS" and p and p["via"] == "ale" and p["to"] == frm:
            # response to our call -> acknowledge -> linked
            self._auto_send(ale_audio(ale_ack_words(frm, self.addrs[0], f"SNR{run['snr']:+.0f}"), self.rate,
                                      variant=self._tx_variant()), f"ALE acknowledge to {frm}")
            self._linked(frm, run["snr"], self._far_snr(msg["text"]))
            self.pending = None
        elif me and kind == "TIS":
            self._hist("rx", "ale call", frm, to, msg["text"], self.khz, run["snr"], "incoming", frm)
            self.log("HF", f"ALE call from {frm}" + (f": {msg['text']}" if msg["text"] else ""))
            self.hold_until = now + 30
            if self.ale_respond and self._auto_send(
                    ale_audio(ale_response_words(frm, to, f"SNR{run['snr']:+.0f}"), self.rate,
                              variant=self._tx_variant()), f"ALE response to {frm}"):
                self.pending = {"kind": "ale_wait_ack", "to": frm, "via": "ale_ack", "t": now,
                                "khz": self.khz, "deadline": now + 20}
        elif me and kind == "TWAS" and p and p["via"] == "ale_ack" and p["to"] == frm:
            self._linked(frm, run["snr"], self._far_snr(msg["text"]))
            self.pending = None
        elif me and kind == "TWAS" and msg["text"]:
            self._hist("rx", "ale message", frm, to, msg["text"], self.khz, run["snr"], "received", frm)
            self.log("HF", f"ALE message from {frm}: {msg['text']}")

    def _on_selcal(self, s):
        s["t"] = self.now()
        s["khz"] = self.khz
        self.selcals = ([s] + self.selcals)[:30]
        self.log("HF", f"SELCAL {s['code']}" + (f" on {self.khz:g} kHz" if self.khz else ""))

    @staticmethod
    def _far_snr(text):
        t = str(text or "").replace(" ", "")
        i = t.find("SNR")
        if i < 0:
            return None
        try:
            return float(t[i + 3:i + 6].rstrip("?"))
        except ValueError:
            return None

    def _linked(self, station, snr, snr_far):
        self.linked = {"station": station, "khz": self.khz, "t": self.now(), "snr": snr, "snr_far": snr_far}
        m, why = recommend_modem(min(x for x in (snr, snr_far) if x is not None) if (snr is not None or snr_far is not None) else None)
        self.linked["modem"], self.linked["why"] = m, why
        self._hist_update(station, "LINKED")
        self.log("HF", f"ALE LINKED with {station}" + (f" on {self.khz:g} kHz" if self.khz else "") +
                 f" - {why}")

    def _timeouts(self, now):
        p = self.pending
        if p and now > p["deadline"]:
            self.pending = None
            if p["via"] in ("selcall", "ale"):
                if p["kind"] != "beacon":
                    self._hist_update(p["to"], "no answer")
                self.log("HF", f"No answer from {p['to']} ({p['kind']})" +
                         (f" on {p['khz']:g} kHz" if p.get("khz") else ""))

    # ---- records
    def _hist(self, d, kind, frm, to, text, khz, snr, status, callsign=""):
        self.history = ([{"t": self.now(), "dir": d, "kind": kind, "from": frm, "to": to, "text": text,
                          "khz": khz, "snr": snr, "status": status, "callsign": callsign}] + self.history)[:100]
        self.events += 1

    def _hist_update(self, station, status):
        for h in self.history:
            if h["dir"] == "tx" and h["to"] == station:
                h["status"] = status
                break
        self.events += 1

    def _lqa(self, station, khz, snr, snr_far, ber, via):
        key = (station, khz)
        self.lqa[key] = {"station": station, "khz": khz, "snr": snr, "snr_far": snr_far, "ber": ber,
                         "t": self.now(), "via": via}
        self.events += 1

    def best_channel(self, station, max_age_h=6.0):
        now = self.now()
        best = None
        for (st, khz), r in self.lqa.items():
            if st != station or khz is None or now - r["t"] > max_age_h * 3600:
                continue
            vals = [v for v in (r["snr"], r["snr_far"]) if v is not None]
            if not vals:
                continue
            score = min(vals) - (now - r["t"]) / 3600.0        # a little penalty for old measurements
            if best is None or score > best[0]:
                best = (score, khz)
        return best[1] if best else None

    def state(self):
        now = self.now()
        lq = sorted(self.lqa.values(), key=lambda r: -r["t"])[:40]
        for r in lq:
            vals = [v for v in (r["snr"], r["snr_far"]) if v is not None]
            r = r
            r["modem"], r["why"] = recommend_modem(min(vals) if vals else None)
        return {"on": True, "ids": self.ids, "addrs": self.addrs, "khz": self.khz,
                "channels": self.channels, "scan": self.scan_on, "can_tune": self.tune_fn is not None,
                "holding": now < self.hold_until, "status": self.status, "pending": self.pending,
                "linked": self.linked, "history": self.history[:50], "lqa": lq,
                "heard": sorted(self.heard.values(), key=lambda r: -r["t"])[:40],
                "selcal": self.selcals[:20], "events": self.events,
                "ale_variant": self.variant_learned or ("".join("1" if b else "0" for b in self.ale.variant)
                                                        if self.ale.variant else "")}


# ===========================================================================
# Self-test (no radio): python3 hfcall.py --selftest
# ===========================================================================

def _awgn(x, snr_db, rng):
    """Add white noise: snr_db is signal power over noise power in a 2.5 kHz bandwidth (8 kHz rate)."""
    p = float(np.mean(x ** 2))
    n_in_bw = p / (10 ** (snr_db / 10))
    sigma = math.sqrt(n_in_bw * (RATE / 2) / 2500.0)
    return x + rng.normal(0, sigma, len(x))


def selftest(verbose=True):
    _need_numpy()
    rng = np.random.default_rng(7)
    say = print if verbose else (lambda *a: None)
    # Golay: standard table values and 3-error correction
    t = _golay_tables()
    assert [t["enc"][k] for k in (1, 2, 3, 4, 7, 8, 9, 0xFFF)] == [0x5C7, 0xB8D, 0xE4A, 0x2DE, 0xC94, 0x5BC, 0x07B, 0xFFF]
    for d in (0, 1, 0x5A5, 0xFFF, 0x123):
        c = golay_encode(d)
        for e in (0, 1 << 3, (1 << 20) | (1 << 2), (1 << 23) | (1 << 11) | 1):
            assert golay_decode(c ^ e) == (d, bin(e).count("1"))
    say("Golay (24,12): standard table, corrects 3 errors")

    # ALE: call words through noise, unknown timing; all interleave variants calibrate
    words = ale_call_words("8P5AB", "8P6FB", scan_s=1.0, text="QSL?")
    for v in (ALE_DEFAULT_VARIANT, (False, False, True)):
        a = np.concatenate([silence(0.37, RATE), ale_audio(words, RATE, variant=v), silence(0.6, RATE)])
        runs = AleDecoder().decode(_awgn(a, 3, rng), 1000)
        assert runs and ale_parse(runs[0]["words"]) == {"to": "8P5AB", "frm": "8P6FB", "kind": "TIS", "text": "QSL?"}, runs
        assert runs[0]["variant"] == v
    say(f"ALE: individual scanning call decoded at +3 dB SNR, interleave calibrated; "
        f"SNR estimate {runs[0]['snr']:+.0f} dB, BER {runs[0]['ber']:.3f}")

    # Selcall: every call type round-trips through noise
    for kind, kw in (("selcall", {}), ("beacon", {}), ("page", {"text": "WATER AT SHELTER 3"}),
                     ("gps", {"pos": (13.1, -59.6)}), ("telcall", {"number": "4251234"}),
                     ("emergency", {"pos": (13.1, -59.6), "text": "MEDIC"})):
        a = np.concatenate([silence(0.23, RATE),
                            selcall_audio(kind, "1234", "567890", RATE, callsign="8P5AB", **kw), silence(0.5, RATE)])
        fr = SelcallDecoder().decode(_awgn(a, 6, rng), 0)
        assert len(fr) == 1, (kind, fr)
        f = fr[0]
        assert (f["kind"], f["to"], f["from"], f["callsign"]) == (kind, "1234", "567890", "8P5AB"), f
        if kind == "page":
            assert f["text"] == "WATER AT SHELTER 3"
        if kind == "telcall":
            assert f["number"] == "4251234"
        if kind in ("gps", "emergency"):
            assert abs(f["pos"][0] - 13.1) < 0.02 and abs(f["pos"][1] + 59.6) < 0.02
    say("HF Selcall: selcall / beacon / page / GPS / telcall / emergency round-trip at +6 dB")

    # ARINC SELCAL decode
    a = np.concatenate([silence(0.5, RATE), selcal_encode("AB-CD", RATE), silence(0.8, RATE)])
    s = SelcalDecoder().decode(_awgn(a, 10, rng), 0)
    assert [x["code"] for x in s] == ["AB-CD"], s
    say("ARINC SELCAL: AB-CD decoded (receive only)")

    # Two stations on a simulated channel: beacon + revertive, ALE call + handshake
    clock = [1000.0]
    air = []

    def make(cfg, name):
        sent = []
        hc = HFCenter(cfg, transmit=lambda a, m: sent.append(a), log=lambda k, t: None,
                      rate=RATE, now=lambda: clock[0], threaded=False)
        hc._sent = sent
        hc._name = name
        return hc

    A = make({"call": "8P5AB", "hf_ids": "1234", "hf_channels": ""}, "A")
    B = make({"call": "8P6FB", "hf_ids": "5678", "hf_channels": ""}, "B")

    def deliver(src, dst):
        while src._sent:
            a = src._sent.pop(0)
            clock[0] += len(a) / RATE + 3
            dst.tx_until = 0
            dst.feed(_awgn(np.concatenate([silence(0.3, RATE), a, silence(0.7, RATE)]), 5, rng))
            dst.tick()

    A.selcall("beacon", "5678")
    deliver(A, B)                                  # B hears beacon -> sends revertive
    assert B._sent, "B should answer the beacon with a revertive"
    A.tx_until = 0
    deliver(B, A)
    assert A.pending is None and ("5678", None) in A.lqa and A.lqa[("5678", None)]["snr_far"] is not None
    assert not any(h["kind"] == "beacon" for h in A.history + B.history), "beacons are not logged"
    A.ale_call("8P6FB", text="QRV?")
    deliver(A, B)                                  # B responds
    deliver(B, A)                                  # A acknowledges -> linked
    deliver(A, B)                                  # B gets the ack -> linked
    assert A.linked and A.linked["station"] == "8P6FB" and B.linked and B.linked["station"] == "8P5AB"
    say(f"Two-station test: beacon + revertive LQA, ALE 3-way handshake LINKED "
        f"({A.linked['why']})")
    assert recommend_modem(15)[0] == "300" and recommend_modem(0)[0] == "robust" and \
        recommend_modem(-10)[0] == "robust_dx" and recommend_modem(-20)[0] is None
    assert parse_channels("7102 40m, 10145.5:30m")[1] == {"khz": 10145.5, "label": "30m"}
    say("hfcall self-test OK")
    return True


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv or len(sys.argv) == 1:
        selftest()
