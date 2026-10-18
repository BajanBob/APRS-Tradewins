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


def modulate(frame: bytes, rate=48000, txdelay_ms=300, txtail_ms=50, level=0.5):
    """AX.25 frame -> float32 mono audio (phase-continuous AFSK)."""
    _need_numpy()
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

    def __init__(self, rate=48000):
        _need_numpy()
        self.rate = rate
        self.sps = rate / BAUD
        self.n = int(round(self.sps))
        self.bpf = _bandpass(rate)
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

        levels = (mag(MARK) > mag(SPACE)).tolist()
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
