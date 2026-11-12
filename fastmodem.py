# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""Fast modems for a radio's 9600 data port (v3.27).

1. G3RUH 9600 baud - the standard 9600 packet modem (scrambler x^17 + x^12 + 1, NRZI, HDLC).
   Made by the sound card, so no Direwolf or hardware TNC is needed, and it works with every
   other 9600 packet station (Direwolf, Kenwood TM-D710 / TH-D74, Mobilinkd, NinoTNC ...).

2. Tradewind Fast - Tradewind-to-Tradewind only. OFDM: 114 carriers 46.875 Hz apart from 281 Hz
   to 5.6 kHz, 24 ms symbols with a 2.7 ms guard, 13 pilots for clock drift, a K=7 convolutional
   code (rate 1/2 to 3/4) and an interleaver. The sender picks QPSK, 16-QAM or 64-QAM from how
   well it hears the other station: about 4 kbit/s on a weak signal up to about 19 kbit/s on a
   strong one. Needs flat audio: the radio's DATA / 9600 port, both ends.

Both are documented, unencrypted modulations: anyone with the description can decode them.
"""
import afsk

np = afsk.np

# ==========================================================================
# 1. G3RUH 9600
# ==========================================================================
G3_BAUD = 9600


def _scramble(levels):
    lfsr, out = 0, []
    for b in levels:
        x = (b ^ (lfsr >> 16) ^ (lfsr >> 11)) & 1
        lfsr = ((lfsr << 1) | x) & 0x1FFFF
        out.append(x)
    return out


def _lowpass(rate, fc, taps):
    n = np.arange(taps) - (taps - 1) / 2
    h = 2 * fc / rate * np.sinc(2 * fc / rate * n) * np.hamming(taps)
    return h / h.sum()


def g3ruh_modulate(frame, rate=48000, txdelay_ms=300, txtail_ms=50, level=0.5, fx25=False):
    """AX.25 frame (with FCS) -> audio for the radio's 9600 data input."""
    afsk._need_numpy()
    pre = max(8, int(txdelay_ms * G3_BAUD / 8000))
    post = max(4, int(txtail_ms * G3_BAUD / 8000))
    body = afsk._frame_bits(frame)
    F = afsk.FLAG_BITS
    if fx25:
        import fx25 as fxmod
        wrapped = fxmod.encode_bits(F + body + F)
        bits = F * pre + (wrapped if wrapped else F + body) + F * post
    else:
        bits = F * pre + body + F * post
    lv, levels = 0, []
    for b in bits:                       # NRZI, then the scrambler
        if b == 0:
            lv ^= 1
        levels.append(lv)
    sym = np.array(_scramble(levels), dtype=float) * 2 - 1
    edges = np.round(np.arange(len(sym) + 1) * rate / G3_BAUD).astype(int)
    x = np.repeat(sym, np.diff(edges))
    h = _lowpass(rate, 0.6 * G3_BAUD, (int(rate / G3_BAUD) * 6) | 1)
    y = np.convolve(x, h, "same")
    y *= level / max(1e-9, np.max(np.abs(y)))
    return y.astype(np.float32)


class G3ruhDemodulator(afsk.Demodulator):
    """Low-pass -> DC removal -> slicer -> PLL bit clock -> descrambler -> NRZI -> HDLC (+ FX.25, bit repair)."""

    PLL_GAIN = 0.15

    def __init__(self, rate=48000, fix_bits=True, fx25_rx=True, pll_gain=None):
        super().__init__(rate, "1200", fix_bits=fix_bits, fx25_rx=fx25_rx, pll_gain=pll_gain)
        self.baud = G3_BAUD
        self.sps = rate / G3_BAUD
        self.lpf = _lowpass(rate, 0.65 * G3_BAUD, (int(rate / G3_BAUD) * 4) | 1)
        self.rawhist = np.zeros(len(self.lpf) - 1)
        self.dc_n = int(rate / G3_BAUD * 48)            # 48-bit running mean removes the radio's DC offset
        self.dchist = np.zeros(self.dc_n - 1)
        self.lfsr = 0
        self.prev_d = 0

    def process(self, samples):
        raw = np.concatenate([self.rawhist, np.asarray(samples, dtype=np.float64)])
        self.rawhist = raw[-(len(self.lpf) - 1):]
        y = np.convolve(raw, self.lpf, "valid")
        self.energy = float(np.mean(y * y)) if len(y) else 0.0
        z = np.concatenate([self.dchist, y])
        self.dchist = z[-(self.dc_n - 1):]
        mean = np.convolve(z, np.ones(self.dc_n) / self.dc_n, "valid")
        levels = (y - mean > 0).tolist()
        step, gain = 1.0 / self.sps, self.PLL_GAIN
        pll, last, lfsr, prev = self.pll, self.level, self.lfsr, self.prev_d
        for lv in levels:
            if lv != last:
                pll -= (pll - 0.5) * gain
                last = lv
            pll += step
            if pll >= 1.0:
                pll -= 1.0
                x = 1 if lv else 0
                d = (x ^ (lfsr >> 16) ^ (lfsr >> 11)) & 1
                lfsr = ((lfsr << 1) | x) & 0x1FFFF
                bit = 1 if d == prev else 0
                prev = d
                self._hdlc(bit)
                if self.fx is not None:
                    fr = self.fx.feed(bit)
                    if fr:
                        self.frames.append(fr)
        self.pll, self.level, self.lfsr, self.prev_d = pll, last, lfsr, prev
        out, self.frames = self.frames, []
        return out


class G3ruhMulti(afsk.MultiDemodulator):
    """Two clock-recovery gains in parallel; a packet either decodes counts once."""

    def __init__(self, rate=48000, fx25_rx=True, fix_bits=True):
        import collections
        self.demods = [G3ruhDemodulator(rate, fix_bits, fx25_rx and i == 0, g) for i, g in enumerate((0.15, 0.06))]
        self.recent = collections.OrderedDict()
        self.chunk = 0
        self.decoded = 0


# ==========================================================================
# 2. Tradewind Fast (OFDM)
# ==========================================================================
FS = 12000                                   # internal sample rate
N = 256                                      # FFT size: 46.875 Hz carrier spacing
CP = 32                                      # guard interval (2.67 ms)
L = N + CP
NBINS = 114                                  # carriers 6..119: 281 Hz .. 5578 Hz
_PIL_POS = list(range(0, NBINS, 10)) + [NBINS - 1]
ND = NBINS - len(_PIL_POS)                   # 101 data carriers
if np is not None:                           # (the Android build runs without numpy: no sound modem there)
    BINS = np.arange(6, 6 + NBINS)
    PILOTS = np.array(_PIL_POS)
    DATA = np.array([i for i in range(NBINS) if i not in set(_PIL_POS)])
MAX_BYTES = 511
# mode id -> (bits per carrier, puncture pattern (A row, B row), name, minimum SNR dB with margin)
MODES = {0: (2, None, "QPSK 1/2", 0), 1: (2, ((1, 1, 0), (1, 0, 1)), "QPSK 3/4", 9),
         2: (4, None, "16-QAM 1/2", 12), 3: (4, ((1, 1, 0), (1, 0, 1)), "16-QAM 3/4", 16),
         4: (6, ((1, 1), (1, 0)), "64-QAM 2/3", 21), 5: (6, ((1, 1, 0), (1, 0, 1)), "64-QAM 3/4", 24)}
_AXIS = {1: [1, -1], 2: [-3, -1, 3, 1], 3: [-7, -5, -1, -3, 7, 5, 1, 3]}  # Gray: index = bits (MSB first)
_NORM = {2: 2 ** 0.5, 4: 10 ** 0.5, 6: 42 ** 0.5}
if np is not None:
    _rng = np.random.default_rng(20261112)
    _PRE = np.exp(1j * np.pi / 2 * _rng.integers(0, 4, NBINS))           # known preamble (QPSK)
    _PIL = 1 - 2 * _rng.integers(0, 2, (64, len(_PIL_POS)))              # known pilots (BPSK), per symbol


def mode_rate(m, nbytes=300):
    """Approximate user bit rate (bit/s) of a mode for frames of nbytes."""
    bps, punct = MODES[m][0], MODES[m][1]
    ncoded = len(_puncture([0] * (2 * (nbytes * 8 + 6)), punct))
    nsym = -(-ncoded // (ND * bps))
    return int(nbytes * 8 / ((nsym + 3) * L / FS + 0.04))


def choose_mode(snr_db):
    """Fastest mode the measured SNR supports (the sender uses how well it hears the other station)."""
    if snr_db is None:
        return 3
    best = 0
    for m, spec in MODES.items():
        if snr_db >= spec[3]:
            best = m
    return best


def _bits(data):
    return [(b >> k) & 1 for b in data for k in range(8)]


def _bytes(bits):
    return bytes(sum(bits[i + k] << k for k in range(8)) for i in range(0, len(bits) - 7, 8))


def _puncture(coded, punct):
    if punct is None:
        return coded
    A, B = punct
    p = len(A)
    out = []
    for i in range(0, len(coded), 2):
        j = (i // 2) % p
        if A[j]:
            out.append(coded[i])
        if B[j]:
            out.append(coded[i + 1])
    return out


def _depuncture(llr, npairs, punct):
    if punct is None:
        return np.asarray(llr[:2 * npairs])
    A, B = punct
    p = len(A)
    out = np.zeros(2 * npairs)
    k = 0
    for i in range(npairs):
        j = i % p
        if A[j]:
            out[2 * i] = llr[k]
            k += 1
        if B[j]:
            out[2 * i + 1] = llr[k]
            k += 1
    return out


def _map(bits, bps):
    """Gray QAM, unit average power. bps bits per carrier: half on I, half on Q."""
    h = bps // 2
    lv = np.array(_AXIS[h], dtype=float)
    b = np.asarray(bits).reshape(-1, bps)
    w = 1 << np.arange(h - 1, -1, -1)
    i = lv[(b[:, :h] * w).sum(1)]
    q = lv[(b[:, h:] * w).sum(1)]
    return (i + 1j * q) / _NORM[bps]


def _demap(sym, weight, bps):
    """Max-log LLRs (>0: bit 0 more likely), scaled by each carrier's reliability."""
    h = bps // 2
    lv = np.array(_AXIS[h], dtype=float) / _NORM[bps]
    idx = np.arange(len(lv))
    out = np.zeros((len(sym), bps))
    for part, off in ((sym.real, 0), (sym.imag, h)):
        d = (part[:, None] - lv[None, :]) ** 2
        for k in range(h):
            bit = (idx >> (h - 1 - k)) & 1
            out[:, off + k] = (d[:, bit == 1].min(1) - d[:, bit == 0].min(1)) * weight
    return out.reshape(-1)


def _symbol(values, k):
    """Frequency-domain values on the data carriers (or None for the preamble) -> one time-domain symbol + guard."""
    X = np.zeros(N // 2 + 1, dtype=complex)
    if values is None:
        X[BINS] = _PRE
    else:
        X[BINS[DATA]] = values
        X[BINS[PILOTS]] = _PIL[k % 64]
    t = np.fft.irfft(X, N)
    return np.concatenate([t[-CP:], t])


def _header_bits(mode, n):
    h = [(mode >> i) & 1 for i in range(3)] + [(n >> i) & 1 for i in range(9)]
    c = (sum(h[i] << i for i in range(12)) * 7 + 5) & 15
    return h + [(c >> i) & 1 for i in range(4)]


def _resample(x, src, dst):
    """Band-limited (FFT) resampling of a whole burst."""
    if src == dst:
        return x
    n = int(round(len(x) * dst / src))
    X = np.fft.rfft(x)
    Y = np.zeros(n // 2 + 1, dtype=complex)
    k = min(len(X), len(Y))
    Y[:k] = X[:k]
    return np.fft.irfft(Y, n) * (n / len(x))


def fast_modulate(payload, rate=48000, level=0.5, mode=None, snr_db=None):
    """Bytes (an AX.25 frame with its FCS) -> one Tradewind Fast burst at the sound card's rate."""
    afsk._need_numpy()
    if not payload or len(payload) > MAX_BYTES:
        raise ValueError(f"Tradewind Fast carries 1-{MAX_BYTES} bytes per burst")
    mode = choose_mode(snr_db) if mode is None else int(mode)
    bps, punct = MODES[mode][0], MODES[mode][1]
    coded = _puncture(afsk._conv_encode(_bits(payload)), punct)
    per = ND * bps
    nsym = -(-len(coded) // per)
    perm = afsk._perm(len(coded))
    inter = [coded[p] for p in perm] + [0] * (nsym * per - len(coded))
    hb = afsk._conv_encode(_header_bits(mode, len(payload)))
    hv = 1 - 2 * np.array([hb[i % len(hb)] for i in range(ND)], dtype=float)
    syms = [_symbol(None, 0), _symbol(None, 0), _symbol(hv, 0)]
    for k in range(nsym):
        syms.append(_symbol(_map(inter[k * per:(k + 1) * per], bps), k + 1))
    x = np.concatenate(syms)
    rms = np.sqrt(np.mean(x * x))
    x = np.clip(x, -4.2 * rms, 4.2 * rms)        # limit the peaks the radio's audio stage must pass
    ramp = np.linspace(0, 1, CP)
    x = np.concatenate([np.zeros(FS // 50), x, np.zeros(FS // 50)])
    x[FS // 50:FS // 50 + CP] *= ramp
    y = _resample(x, FS, rate)
    y *= level / max(1e-9, np.max(np.abs(y)))
    return y.astype(np.float32)


class FastDemodulator:
    """Streaming Tradewind Fast receiver. process(samples) -> list of payloads (AX.25 frames without FCS)."""

    THRESH = 0.42

    def __init__(self, rate=48000):
        afsk._need_numpy()
        self.rate = rate
        self.dec = rate // FS if rate % FS == 0 else 0
        fc = 5900.0
        self.lpf = _lowpass(rate, fc, (int(rate / FS) * 24) | 1)
        self.rawhist = np.zeros(len(self.lpf) - 1)
        self.phase = 0.0                              # next input-rate position to take (non-integer ratio)
        self.tail = np.zeros(3)
        self.buf = np.zeros(0)
        self.searched = 0
        self.lock = None
        self.ref = np.concatenate([np.fft.irfft(self._pre_spec(), N)])
        self.ref /= np.linalg.norm(self.ref)
        self.energy = 0.0
        self.last_snr = None
        self.last_mode = None
        self.decoded = 0
        self.fixed = 0

    @staticmethod
    def _pre_spec():
        X = np.zeros(N // 2 + 1, dtype=complex)
        X[BINS] = _PRE
        return X

    def _to_fs(self, samples):
        raw = np.concatenate([self.rawhist, np.asarray(samples, dtype=np.float64)])
        self.rawhist = raw[-(len(self.lpf) - 1):]
        y = np.convolve(raw, self.lpf, "valid")
        self.energy = float(np.mean(y * y)) if len(y) else 0.0
        if self.dec:
            start = int(self.phase)
            out = y[start::self.dec]
            self.phase = (start - len(y)) % self.dec
            return out
        # other rates (44100 ...): cubic Lagrange interpolation, much cleaner than linear for 64-QAM
        step = self.rate / FS
        y = np.concatenate([self.tail, y])            # 3 samples kept from the last chunk
        pos = np.arange(self.phase + 1, len(y) - 2, step)
        self.phase = (pos[-1] + step - (len(y) - 3) - 1) if len(pos) else self.phase - (len(y) - 3)
        self.tail = y[-3:]
        i = np.floor(pos).astype(int)
        t = pos - i
        ym1, y0, y1, y2 = y[i - 1], y[i], y[i + 1], y[i + 2]
        return (-t * (t - 1) * (t - 2) / 6 * ym1 + (t + 1) * (t - 1) * (t - 2) / 2 * y0
                - (t + 1) * t * (t - 2) / 2 * y1 + (t + 1) * t * (t - 1) / 6 * y2)

    def process(self, samples):
        self.buf = np.concatenate([self.buf, self._to_fs(samples)])
        out = []
        while True:
            if self.lock is None and not self._search():
                break
            r = self._decode()
            if r is None:
                break                                     # wait for more audio
            if r:
                out.append(r)
        if self.lock is None and len(self.buf) > 4 * L:   # keep only what a future preamble could need
            cut = len(self.buf) - 3 * L
            self.buf = self.buf[cut:]
            self.searched = max(0, self.searched - cut)
        return out

    def _search(self):
        b = self.buf
        s0 = self.searched
        if len(b) - s0 < 2 * L + N:
            return False
        seg = b[s0:]
        nfft = 1 << int(np.ceil(np.log2(len(seg) + N)))
        c = np.fft.irfft(np.fft.rfft(seg, nfft) * np.conj(np.fft.rfft(self.ref, nfft)), nfft)[:len(seg) - N + 1]
        e = np.concatenate([[0], np.cumsum(seg * seg)])
        en = np.sqrt(np.maximum(e[N:] - e[:-N], 1e-12))
        rho = c / en
        last = len(rho) - L - 1                            # both preamble copies must be in the buffer
        if last <= 0:
            return False
        hit = np.nonzero(rho[:last] > self.THRESH)[0]
        for i in hit:
            j = i + int(np.argmax(rho[i:i + L // 2]))        # the peak near the first crossing
            if j + L < len(rho) and rho[j + L] > self.THRESH * 0.8:
                self.lock = s0 + j                           # start of the first preamble's FFT window
                self.searched = s0 + j + 1
                return True
        self.searched = s0 + max(0, last)
        return False

    def _fft(self, k):
        s = self.lock - CP // 2 + k * L
        return np.fft.rfft(self.buf[s:s + N])[BINS]

    def _decode(self):
        """None: need more samples. b'': not a frame (keep searching). bytes: a frame."""
        base = self.lock
        if base - CP // 2 + 3 * L > len(self.buf):
            return None
        Y0, Y1 = self._fft(0), self._fft(1)
        H = (Y0 + Y1) / 2 / _PRE
        noise = np.mean(np.abs(Y0 - Y1) ** 2) / 2
        p = np.abs(H) ** 2
        snr = 10 * np.log10(max(np.mean(p), 1e-12) / max(noise, 1e-12))
        w = p / (p + max(noise, 1e-12))
        hz = self._eq(self._fft(2), H, 0)
        llr = np.real(hz[DATA]) * w[DATA]
        hb_len = 2 * (16 + 6)
        acc = np.zeros(hb_len)
        for i, v in enumerate(llr):
            acc[i % hb_len] += v
        hb = afsk._viterbi(acc)
        mode = sum(hb[i] << i for i in range(3))
        n = sum(hb[3 + i] << i for i in range(9))
        if mode not in MODES or not n or _header_bits(mode, n) != hb[:16]:
            return self._unlock(b"")
        bps, punct = MODES[mode][0], MODES[mode][1]
        npairs = n * 8 + 6
        ncoded = len(_puncture([0] * (2 * npairs), punct))
        nsym = -(-ncoded // (ND * bps))
        if base - CP // 2 + (3 + nsym) * L > len(self.buf):
            return None
        soft = []
        for k in range(nsym):
            z = self._eq(self._fft(3 + k), H, k + 1)
            soft.append(_demap(z[DATA], w[DATA], bps))
        soft = np.concatenate(soft)[:ncoded]
        perm = afsk._perm(ncoded)
        de = np.zeros(ncoded)
        de[perm] = soft
        bits = afsk._viterbi(_depuncture(de, npairs, punct))
        data = _bytes(bits[:n * 8])
        end = base - CP // 2 + (3 + nsym) * L
        if len(data) >= 3 and afsk.crc16_x25(data[:-2]) == (data[-2] | (data[-1] << 8)):
            self.last_snr, self.last_mode = round(float(snr), 1), mode
            self.decoded += 1
            self.buf = self.buf[end:]
            self.searched, self.lock = 0, None
            return data[:-2]
        return self._unlock(b"")

    def _unlock(self, r):
        self.lock = None
        return r

    def _eq(self, Y, H, k):
        """Equalise one symbol and remove the phase drift the pilots show (sound-card clocks differ)."""
        z = Y / np.where(np.abs(H) > 1e-9, H, 1e-9)
        if k:
            pz = z[PILOTS] * _PIL[k % 64]
            dphi = np.angle(pz[1:] * np.conj(pz[:-1])) / np.diff(PILOTS)
            slope = float(np.median(dphi))
            ph = np.angle(np.sum(pz * np.exp(-1j * slope * PILOTS)))
            z = z * np.exp(-1j * (ph + slope * np.arange(len(BINS))))
        return z
