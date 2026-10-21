# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""
afsk.py - Bell 202 AFSK 1200 baud software modem + AX.25 framing.

Pure numpy. Gives a PC soundcard (or a radio's built-in USB sound card)
the same job as a hardware TNC:

  encode_ax25()  -> AX.25 UI frame bytes (with FCS)
  modulate()     -> audio samples ready to play to the radio
  Demodulator    -> feed it received audio, get decoded frames back
  decode_ax25()  -> frame bytes -> "SRC>DST,PATH:info" (TNC2 text)
"""

try:
    import numpy as np
except ImportError:  # e.g. Android build: framing/KISS still work, the sound modem doesn't
    np = None

BAUD = 1200
MARK = 1200.0   # binary 1 (no tone change in NRZI)
SPACE = 2200.0

# Modem profiles: (baud, mark Hz, space Hz)
#   "1200": Bell 202, VHF/UHF FM packet and APRS
#   "300":  HF packet on SSB (USB), 1600/1800 Hz tones - e.g. HF APRS on 10.1476 MHz
PROFILES = {"1200": (1200, 1200.0, 2200.0), "300": (300, 1600.0, 1800.0)}


def profile(name):
    return PROFILES.get(str(name or "1200"), PROFILES["1200"])
RX_RATE = 12000  # internal demodulator rate (10 samples/bit)


# --------------------------------------------------------------------------
# AX.25 / HDLC
# --------------------------------------------------------------------------

def crc16_x25(data: bytes) -> int:
    """AX.25 FCS (CRC-16/X-25). crc16_x25(b'123456789') == 0x906E."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def _addr(call, last=False, command=False):
    base, _, ssid = call.upper().partition("-")
    ssid = int(ssid or 0) & 0x0F
    hbit = base.endswith("*")
    base = base.rstrip("*")
    b = bytes((ord(c) << 1) for c in base.ljust(6)[:6])
    sb = 0x60 | (ssid << 1) | (1 if last else 0)
    if command or hbit:
        sb |= 0x80
    return b + bytes([sb])


def encode_ax25(src, dst, path, info) -> bytes:
    """Build an AX.25 UI frame (no flags, FCS appended)."""
    calls = [dst, src] + list(path)
    addr = b"".join(_addr(c, last=(i == len(calls) - 1), command=(i == 0))
                    for i, c in enumerate(calls))
    body = addr + b"\x03\xf0" + (info.encode("latin-1", "replace") if isinstance(info, str) else info)
    fcs = crc16_x25(body)
    return body + bytes([fcs & 0xFF, fcs >> 8])


def decode_ax25(frame: bytes):
    """Frame bytes (FCS already stripped) -> TNC2 text, or None if malformed."""
    calls, i = [], 0
    while i + 7 <= len(frame):
        chunk = frame[i:i + 7]
        name = "".join(chr(c >> 1) for c in chunk[:6]).strip()
        ssid = (chunk[6] >> 1) & 0x0F
        c = name + (f"-{ssid}" if ssid else "")
        if len(calls) >= 2 and chunk[6] & 0x80:
            c += "*"
        calls.append(c)
        i += 7
        if chunk[6] & 1:
            break
    if len(calls) < 2 or i + 2 > len(frame) or frame[i] != 0x03:
        return None
    if not all(ch.isalnum() or ch in "-*" for c in calls for ch in c):
        return None
    info = frame[i + 2:].decode("latin-1", "replace").rstrip("\r\n")
    dst, src, digis = calls[0], calls[1], calls[2:]
    return f"{src}>{','.join([dst] + digis)}:{info}"


def tnc2_to_frame(line: str) -> bytes:
    """'SRC>DST,PATH:info' -> AX.25 frame bytes (for gating / beacons)."""
    head, info = line.split(":", 1)
    src, rest = head.split(">", 1)
    parts = rest.split(",")
    return encode_ax25(src, parts[0], parts[1:], info)


def _frame_bits(frame: bytes):
    bits, ones = [], 0
    for byte in frame:
        for k in range(8):           # LSB first
            b = (byte >> k) & 1
            bits.append(b)
            if b:
                ones += 1
                if ones == 5:        # bit stuffing
                    bits.append(0)
                    ones = 0
            else:
                ones = 0
    return bits


FLAG_BITS = [0, 1, 1, 1, 1, 1, 1, 0]


# --------------------------------------------------------------------------
# Modulator
# --------------------------------------------------------------------------

def _need_numpy():
    if np is None:
        raise RuntimeError("the sound modem needs numpy (pip install numpy)")


def modulate(frame: bytes, rate=48000, txdelay_ms=300, txtail_ms=50, level=0.5, modem="1200"):
    """AX.25 frame -> float32 mono audio (phase-continuous AFSK)."""
    _need_numpy()
    BAUD, MARK, SPACE = profile(modem)
    pre = max(1, int(txdelay_ms * BAUD / 8000))
    post = max(2, int(txtail_ms * BAUD / 8000))
    bits = FLAG_BITS * pre + _frame_bits(frame) + FLAG_BITS * post
    # NRZI: 0 -> change tone, 1 -> keep tone
    tones, cur = [], MARK
    for b in bits:
        if b == 0:
            cur = SPACE if cur == MARK else MARK
        tones.append(cur)
    # samples per bit with fractional accumulation (44100 etc. work too)
    edges = np.round(np.arange(len(tones) + 1) * rate / BAUD).astype(int)
    freq = np.repeat(np.array(tones), np.diff(edges))
    phase = 2 * np.pi * np.cumsum(freq) / rate
    audio = level * np.sin(phase)
    # short fade in/out to avoid clicks
    f = min(len(audio) // 10, int(rate * 0.005))
    if f:
        ramp = np.linspace(0, 1, f)
        audio[:f] *= ramp
        audio[-f:] *= ramp[::-1]
    return audio.astype(np.float32)


# --------------------------------------------------------------------------
# Demodulator
# --------------------------------------------------------------------------

def resample_to(x, src_rate, dst_rate=RX_RATE):
    """Linear resample (kept for tools/tests; the demodulator runs at any rate)."""
    if src_rate == dst_rate:
        return np.asarray(x, dtype=np.float64)
    n = int(len(x) * dst_rate / src_rate)
    t = np.arange(n) * (src_rate / dst_rate)
    return np.interp(t, np.arange(len(x)), x)


def _bandpass(rate, lo=900.0, hi=2500.0, taps=None):
    taps = taps or (int(rate / 300) | 1)
    n = np.arange(taps) - (taps - 1) / 2
    def lp(fc):
        return 2 * fc / rate * np.sinc(2 * fc / rate * n)
    h = (lp(hi) - lp(lo)) * np.hamming(taps)
    return h


class Demodulator:
    """Streaming AFSK1200 demodulator + HDLC deframer, at the soundcard's rate.

    Band-pass filter -> quadrature correlation against mark/space over one
    bit time -> digital PLL bit clock -> NRZI decode -> HDLC deframe with
    bit de-stuffing and FCS check.
    """

    PLL_GAIN = 0.2

    def __init__(self, rate=48000, modem="1200"):
        _need_numpy()
        self.rate = rate
        self.baud, self.mark, self.space = profile(modem)
        self.sps = rate / self.baud
        self.n = int(round(self.sps))
        lo, hi = min(self.mark, self.space), max(self.mark, self.space)
        pad = max(300.0, self.baud * 0.25)
        self.bpf = _bandpass(rate, lo - pad, hi + pad, taps=(int(rate / max(self.baud, 150)) * 2) | 1)
        self.rawhist = np.zeros(len(self.bpf) - 1)
        self.hist = np.zeros(self.n - 1)
        self.t = 0                 # index of first filtered sample of next chunk
        self.pll = 0.0
        self.level = 0
        self.prev_sym = 0
        self.ones = 0
        self.bits = []
        self.in_frame = False
        self.frames = []
        self.energy = 0.0

    def process(self, samples):
        raw = np.concatenate([self.rawhist, np.asarray(samples, dtype=np.float64)])
        self.rawhist = raw[-(len(self.bpf) - 1):]
        y = np.convolve(raw, self.bpf, "valid")          # len == len(samples)
        x = np.concatenate([self.hist, y])
        idx = np.arange(self.t - len(self.hist), self.t + len(y))
        self.hist = x[-(self.n - 1):]
        self.t += len(y)
        self.energy = float(np.mean(y * y)) if len(y) else 0.0
        w = 2 * np.pi / self.rate
        k = np.ones(self.n) / self.n

        def mag(f):
            re = np.convolve(x * np.cos(w * f * idx), k, "valid")
            im = np.convolve(x * np.sin(w * f * idx), k, "valid")
            return np.sqrt(re * re + im * im)

        levels = (mag(self.mark) > mag(self.space)).tolist()
        # Digital PLL: sample each bit when the phase wraps; nudge the phase so
        # level transitions fall half-way between samples.
        step, gain = 1.0 / self.sps, self.PLL_GAIN
        pll, last, prev = self.pll, self.level, self.prev_sym
        for lv in levels:
            if lv != last:
                pll -= (pll - 0.5) * gain
                last = lv
            pll += step
            if pll >= 1.0:
                pll -= 1.0
                self._hdlc(1 if lv == prev else 0)   # NRZI: no change -> 1
                prev = lv
        self.pll, self.level, self.prev_sym = pll, last, prev
        out, self.frames = self.frames, []
        return out

    def _hdlc(self, b):
        if b:
            self.ones += 1
            self.bits.append(1)
            if self.ones >= 7:                   # abort / idle
                self.bits, self.in_frame = [], False
            return
        if self.ones == 6:                       # flag 01111110
            data = self.bits[:-7]
            if self.in_frame and len(data) >= 8 * 18 and len(data) % 8 == 0:
                self._finish(data)
            self.bits, self.in_frame = [], True
        elif self.ones == 5:                     # stuffed zero
            pass
        else:
            self.bits.append(0)
        self.ones = 0

    def _finish(self, bits):
        by = bytes(sum(bits[i + k] << k for k in range(8)) for i in range(0, len(bits), 8))
        body, fcs = by[:-2], by[-2] | (by[-1] << 8)
        if crc16_x25(body) == fcs:
            self.frames.append(body)


# ==========================================================================
# Tradewind Robust: weak-signal text mode for HF / SSB (Phase 3)
# ==========================================================================
# 8-FSK, 31.25 baud, tones 31.25 Hz apart (250 Hz wide, centred ~1.5 kHz on
# USB). Rate-1/2 K=7 convolutional code (NASA 171/133) with soft-decision
# Viterbi, an interleaver and CRC-16. A 16-symbol Costas preamble gives time
# and frequency sync (+/-200 Hz tuning error is tolerated). About 44 bit/s;
# a 64-byte frame takes ~12 s. In simulation it decodes at -10 dB SNR
# (2.5 kHz noise bandwidth). Not compatible with other modes - both ends
# need Tradewind Robust.

R_RATE = 8000                      # internal processing rate
R_BAUD = 31.25
R_N = int(R_RATE / R_BAUD)         # 256 samples per symbol
R_BASE = 1375.0                    # lowest tone (Hz) = 44 x 31.25
R_TONES = 8
R_COSTAS = [3, 6, 4, 7, 2, 0, 5, 1, 1, 5, 0, 2, 7, 4, 6, 3]   # preamble tones
R_CLASSES = [32, 64, 128]          # frame sizes in bytes (len byte + payload + CRC)
R_HDR = 4                          # size-class symbols after the preamble
_G1, _G2 = 0o171, 0o133            # K=7 code polynomials
_GRAY = [0, 1, 3, 2, 6, 7, 5, 4]   # 3 bits -> tone index (Gray coded)
_IGRAY = [_GRAY.index(i) for i in range(8)]


def _conv_encode(bits):
    state, out = 0, []
    for b in list(bits) + [0] * 6:
        state = ((state << 1) | b) & 0x7F
        out.append(bin(state & _G1).count("1") & 1)
        out.append(bin(state & _G2).count("1") & 1)
    return out


def _viterbi(llr):
    """Soft Viterbi for the K=7 rate-1/2 code. llr > 0 means bit 0 more likely."""
    _need_numpy()
    n = len(llr) // 2
    ns = 64
    st = np.arange(ns)
    # for state s (6 bits, newest bit in LSB) and input b: next = ((s<<1)|b)&63, reg = (s<<1)|b (7 bits)
    reg0, reg1 = (st << 1) & 0x7F, ((st << 1) | 1) & 0x7F
    par = lambda r, g: np.array([bin(int(x) & g).count("1") & 1 for x in r])
    o0 = np.stack([par(reg0, _G1), par(reg0, _G2)], 1)
    o1 = np.stack([par(reg1, _G1), par(reg1, _G2)], 1)
    nxt0, nxt1 = reg0 & 63, reg1 & 63
    metric = np.full(ns, -1e9)
    metric[0] = 0.0
    L = np.asarray(llr, dtype=float).reshape(n, 2)
    dec = np.zeros((n, ns), dtype=np.int16)         # predecessor state for each new state
    bitsel = np.zeros((n, ns), dtype=np.int8)
    for t in range(n):
        l = L[t]
        bm0 = ((1 - 2 * o0) * l).sum(1)               # +llr when code bit 0
        bm1 = ((1 - 2 * o1) * l).sum(1)
        cand_m = np.concatenate([metric + bm0, metric + bm1])
        cand_to = np.concatenate([nxt0, nxt1])
        cand_from = np.concatenate([st, st])
        cand_b = np.concatenate([np.zeros(ns, np.int8), np.ones(ns, np.int8)])
        new = np.full(ns, -1e18)
        order = np.argsort(cand_m)                    # later (bigger) wins
        new[cand_to[order]] = cand_m[order]
        frm = np.zeros(ns, dtype=np.int16)
        bsel = np.zeros(ns, dtype=np.int8)
        frm[cand_to[order]] = cand_from[order]
        bsel[cand_to[order]] = cand_b[order]
        metric, dec[t], bitsel[t] = new, frm, bsel
    s = 0                                            # tail bits force state 0
    bits = []
    for t in range(n - 1, -1, -1):
        bits.append(int(bitsel[t][s]))
        s = int(dec[t][s])
    return bits[::-1][:-6]


def _perm(n):
    stride = 97 if n % 97 else 89
    return [(i * stride) % n for i in range(n)]


def _class_for(nbytes):
    for i, c in enumerate(R_CLASSES):
        if nbytes + 3 <= c:
            return i
    raise ValueError(f"message too long for Tradewind Robust (max {R_CLASSES[-1] - 3} bytes)")


def _frame_symbols(payload: bytes):
    cls = _class_for(len(payload))
    size = R_CLASSES[cls]
    body = bytes([len(payload)]) + payload
    body += bytes(size - 2 - len(body))
    crc = crc16_x25(body)
    body += bytes([crc & 0xFF, crc >> 8])
    bits = [(byte >> k) & 1 for byte in body for k in range(8)]
    coded = _conv_encode(bits)
    coded += [0] * ((-len(coded)) % 3)
    perm = _perm(len(coded))
    inter = [0] * len(coded)
    for i, p in enumerate(perm):
        inter[p] = coded[i]
    syms = [_GRAY[inter[i] << 2 | inter[i + 1] << 1 | inter[i + 2]] for i in range(0, len(inter), 3)]
    return R_COSTAS + [cls * 2 + 1] * R_HDR + syms


def _nsyms(cls):
    nb = (R_CLASSES[cls] * 8 + 6) * 2
    return (nb + (-nb) % 3) // 3


def robust_modulate(payload: bytes, rate=48000, level=0.5, lead_ms=200):
    """Payload bytes -> float32 audio (continuous-phase 8-FSK)."""
    _need_numpy()
    syms = _frame_symbols(payload)
    sps = rate / R_BAUD
    edges = np.round(np.arange(len(syms) + 1) * sps).astype(int)
    freq = np.repeat(R_BASE + np.array(syms) * R_BAUD, np.diff(edges))
    phase = 2 * np.pi * np.cumsum(freq) / rate
    audio = level * np.sin(phase)
    f = int(rate * 0.01)
    ramp = np.linspace(0, 1, f)
    audio[:f] *= ramp
    audio[-f:] *= ramp[::-1]
    lead = np.zeros(int(rate * lead_ms / 1000))
    return np.concatenate([lead, audio, lead]).astype(np.float32)


class RobustDemodulator:
    """Streaming receiver: buffers audio, searches for the Costas preamble over
    time and frequency, then decodes the frame with soft Viterbi + CRC."""
    MAX_OFF = 6.5                     # +/- tone spacings of tuning error searched (~200 Hz)
    THRESH = 5.0                      # preamble score threshold (noise ~2, strong ~14)

    def __init__(self, rate=48000):
        _need_numpy()
        self.rate = rate
        self.buf = np.zeros(0)
        self.pending = np.zeros(0)
        self.lp = _bandpass(rate, 50.0, 3200.0, taps=(int(rate / 400) | 1)) if rate != R_RATE else None
        self.lp_hist = np.zeros(0)
        self.done_until = 0           # absolute sample index (at R_RATE) already decoded
        self.base = 0                 # absolute index of buf[0]
        self.tried = set()
        self.last_snr = None

    def _to_internal(self, x):
        x = np.asarray(x, dtype=float)
        if self.lp is None:
            return x
        y = np.concatenate([self.lp_hist, x])
        self.lp_hist = y[-(len(self.lp) - 1):]
        y = np.convolve(y, self.lp, "valid")
        self.pending = np.concatenate([self.pending, y])
        n_out = int(len(self.pending) * R_RATE / self.rate)
        if n_out < 2:
            return np.zeros(0)
        t = np.arange(n_out) * (self.rate / R_RATE)
        out = np.interp(t, np.arange(len(self.pending)), self.pending)
        used = int(n_out * self.rate / R_RATE)
        self.pending = self.pending[used:]
        return out

    def process(self, samples):
        x = self._to_internal(samples)
        self.buf = np.concatenate([self.buf, x])
        maxlen = (len(R_COSTAS) + R_HDR + _nsyms(len(R_CLASSES) - 1) + 40) * R_N
        if len(self.buf) > maxlen + R_RATE * 5:
            cut = len(self.buf) - maxlen
            self.buf, self.base = self.buf[cut:], self.base + cut
        return self._search()

    def _search(self):
        frames = []
        pre = len(R_COSTAS) + R_HDR
        need_min = (pre + _nsyms(0)) * R_N
        if len(self.buf) < need_min:
            return frames
        hop = R_N // 8
        nstart = (len(self.buf) - pre * R_N) // hop
        if nstart <= 0:
            return frames
        starts = np.arange(nstart) * hop
        # power for every hop position, half-bin grid
        win = np.lib.stride_tricks.sliding_window_view(self.buf, R_N)[::hop]
        sp = np.abs(np.fft.rfft(win, 2 * R_N, axis=1)) ** 2
        k0 = int(round(R_BASE / (R_BAUD / 2)))        # half-bin index of tone 0
        offs = np.arange(-int(self.MAX_OFF * 2), int(self.MAX_OFF * 2) + 1)
        best = []
        nsym_pos = np.arange(len(R_COSTAS)) * 8        # symbol step = 8 hops
        tones2 = np.array(R_COSTAS) * 2
        for off in offs:
            cols = k0 + off + np.arange(R_TONES) * 2
            if cols.min() < 1 or cols.max() >= sp.shape[1]:
                continue
            P = sp[:, cols]                           # (hops, 8)
            tot = P.sum(1) + 1e-12
            frac = P / tot[:, None]
            valid = len(starts)
            if valid - nsym_pos[-1] <= 0:
                continue
            score = np.zeros(valid - nsym_pos[-1])
            for s, tn in zip(nsym_pos, np.array(R_COSTAS)):
                score += frac[s:s + len(score), tn]
            i = int(np.argmax(score))
            best.append((score[i], i, off))
            # also keep other strong peaks (several frames in the buffer)
            for j in np.argsort(score)[::-1][:6]:
                if score[j] > self.THRESH and abs(j - i) > 16:
                    best.append((score[j], int(j), off))
        best.sort(reverse=True)
        used = []
        for sc, i, off in best:
            if sc < self.THRESH:
                break
            st = int(starts[i])
            absst = self.base + st
            if any(abs(absst - u) < R_N * 4 for u in used) or absst < self.done_until:
                continue
            res = self._decode(st, off)
            if res == "wait":
                continue
            key = (absst // (R_N // 2), off)
            if res is None:
                if key not in self.tried:
                    self.tried.add(key)
                continue
            payload, end = res
            used.append(absst)
            self.done_until = self.base + end
            frames.append(payload)
        if len(self.tried) > 2000:
            self.tried.clear()
        return frames

    def _sym_energy(self, start, nsym, off):
        """Energy of the 8 tones for nsym symbols, refining timing by +/- 1/16 symbol."""
        k0 = int(round(R_BASE / (R_BAUD / 2))) + off
        cols = k0 + np.arange(R_TONES) * 2
        best = None
        for dt in (-R_N // 16, 0, R_N // 16):
            s0 = start + dt
            if s0 < 0 or s0 + nsym * R_N > len(self.buf):
                continue
            seg = self.buf[s0:s0 + nsym * R_N].reshape(nsym, R_N)
            sp = np.abs(np.fft.rfft(seg, 2 * R_N, axis=1)) ** 2
            E = sp[:, cols]
            q = E.max(1).sum()
            if best is None or q > best[0]:
                noise = np.median(sp[:, k0 - 40:k0 - 8]) if k0 > 48 else np.median(sp)
                best = (q, E, noise, s0)
        return best

    def _decode(self, st, off):
        pre = len(R_COSTAS)
        hdr = self._sym_energy(st + pre * R_N, R_HDR, off)
        if hdr is None:
            return "wait"
        Eh = hdr[1].sum(0)
        cls_tone = int(np.argmax(Eh))
        if cls_tone not in (1, 3, 5):
            return None
        cls = (cls_tone - 1) // 2
        nd = _nsyms(cls)
        if st + (pre + R_HDR + nd) * R_N + R_N // 16 > len(self.buf):
            return "wait"
        got = self._sym_energy(st + (pre + R_HDR) * R_N, nd, off)
        if got is None:
            return "wait"
        _, E, noise, s0 = got
        noise = max(noise, 1e-12)
        # max-log soft bits from noncoherent tone energies
        tone_bits = np.array([[(_IGRAY[t] >> (2 - k)) & 1 for k in range(3)] for t in range(8)])
        Z = E / noise
        llr = []
        for k in range(3):
            m0 = np.where(tone_bits[:, k] == 0, Z, -np.inf).max(1)
            m1 = np.where(tone_bits[:, k] == 1, Z, -np.inf).max(1)
            llr.append(m0 - m1)
        llr = np.stack(llr, 1).reshape(-1)
        perm = _perm(len(llr))
        deint = llr[perm]
        nbits = (R_CLASSES[cls] * 8 + 6) * 2
        bits = _viterbi(np.clip(deint[:nbits], -50, 50))
        body = bytes(sum(bits[i + k] << k for k in range(8)) for i in range(0, R_CLASSES[cls] * 8, 8))
        if crc16_x25(body[:-2]) != (body[-2] | body[-1] << 8):
            return None
        n = body[0]
        if n > len(body) - 3:
            return None
        sig = float(np.mean(E.max(1)))
        self.last_snr = 10 * np.log10(max(sig / noise - 1, 1e-3) * (R_BAUD / 2500.0) * 2) if noise > 0 else None
        end = s0 + nd * R_N
        return body[1:1 + n], end
