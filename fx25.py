# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""FX.25: forward error correction wrapped around an ordinary AX.25 packet.

An FX.25 transmission is: correlation tag (64 bits) + Reed-Solomon codeblock. The codeblock's data part
holds the normal bit-stuffed AX.25 frame (flags included), so a radio that doesn't know FX.25 still
decodes the packet as usual; an FX.25 receiver can also repair byte errors with the RS check bytes.

Reed-Solomon over GF(2^8), field polynomial 0x11D, first consecutive root 1, primitive element 1
(the parameters used by FX.25 implementations). Codeword byte order: data first, then check bytes;
bytes go on air least-significant bit first, NRZI-encoded, not bit-stuffed.
"""

# Correlation tags (value sent LSB first) -> (codeblock size n, data size k)
TAGS = {
    0xB74DB7DF8A532F3E: (255, 239), 0x26FF60A600CC8FDE: (144, 128), 0xC7DC0508F3D9B09E: (80, 64),
    0x8F056EB4369660EE: (48, 32),   0x6E260B1AC5835FAE: (255, 223), 0xFF94DC634F1CFF4E: (160, 128),
    0x1EB7B9CDBC09C00E: (96, 64),   0xDBF869BD2DBB1776: (64, 32),   0x3ADB0C13DEAE2836: (255, 191),
    0xAB69DB6A543188D6: (192, 128), 0x4A4ABEC4A724B796: (128, 64),
}
TAG_MAX_BIT_ERRORS = 4               # a tag still matches with up to 4 wrong bits
CHECK_FAMILIES = {16: [(48, 32), (80, 64), (144, 128), (255, 239)],
                  32: [(64, 32), (96, 64), (160, 128), (255, 223)],
                  64: [(128, 64), (192, 128), (255, 191)]}

# ---- GF(256)
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _div(a, b):
    if b == 0:
        raise ZeroDivisionError
    return 0 if a == 0 else _EXP[(_LOG[a] - _LOG[b]) % 255]


_GEN = {}


def _generator(nroots):
    """g(x) = prod (x - a^(1+i)), i = 0..nroots-1; coefficients highest degree first."""
    if nroots not in _GEN:
        g = [1]
        for i in range(nroots):
            r = _EXP[1 + i]
            ng = [0] * (len(g) + 1)
            for j, c in enumerate(g):
                ng[j] ^= c
                ng[j + 1] ^= _mul(c, r)
            g = ng
        _GEN[nroots] = g
    return _GEN[nroots]


def rs_encode(data, nroots):
    """Systematic RS: data bytes (highest-degree coefficient first) -> check bytes."""
    g = _generator(nroots)
    rem = [0] * nroots
    for d in data:
        f = d ^ rem[0]
        rem = rem[1:] + [0]
        if f:
            for j in range(nroots):
                rem[j] ^= _mul(g[j + 1], f)
    return bytes(rem)


def rs_decode(block, nroots):
    """Correct up to nroots/2 byte errors in place. Returns (data bytes, errors fixed) or (None, -1)."""
    msg = list(block)
    n = len(msg)
    synd = []
    for i in range(nroots):
        r, a = 0, _EXP[1 + i]
        for c in msg:
            r = _mul(r, a) ^ c
        synd.append(r)
    if not any(synd):
        return bytes(msg[:n - nroots]), 0
    # Berlekamp-Massey -> error locator
    C, B, L, m, b = [1], [1], 0, 1, 1
    for k in range(nroots):
        d = synd[k]
        for i in range(1, L + 1):
            if i < len(C):
                d ^= _mul(C[i], synd[k - i])
        if d == 0:
            m += 1
            continue
        coef = _div(d, b)
        T = C[:]
        shifted = [0] * m + [_mul(coef, x) for x in B]
        if len(shifted) > len(C):
            C += [0] * (len(shifted) - len(C))
        for i, x in enumerate(shifted):
            C[i] ^= x
        if 2 * L <= k:
            L, B, b, m = k + 1 - L, T, d, 1
        else:
            m += 1
    C = C[:L + 1]
    if L == 0 or L > nroots // 2:
        return None, -1
    # Chien search: position p (0 = first byte) has locator X = a^(n-1-p)
    pos = []
    for p in range(n):
        xinv = _EXP[(255 - (n - 1 - p)) % 255]
        s = 0
        for i in range(len(C) - 1, -1, -1):
            s = _mul(s, xinv) ^ C[i]
        if s == 0:
            pos.append(p)
    if len(pos) != L:
        return None, -1
    # Forney: error values (fcr = 1)
    omega = [0] * nroots
    for i in range(nroots):
        v = 0
        for j in range(min(i, L) + 1):
            v ^= _mul(C[j], synd[i - j])
        omega[i] = v
    for p in pos:
        X = _EXP[(n - 1 - p) % 255]
        xinv = _div(1, X)
        num = 0
        for i in range(nroots - 1, -1, -1):
            num = _mul(num, xinv) ^ omega[i]
        den = 0
        for i in range(1, len(C), 2):              # formal derivative: odd terms
            den ^= _mul(C[i], _EXP[(_LOG[xinv] * (i - 1)) % 255] if xinv else 0)
        if den == 0:
            return None, -1
        msg[p] ^= _div(num, den)                    # fcr = 1: e = Omega(X^-1) / Lambda'(X^-1)
    # verify
    for i in range(nroots):
        r, a = 0, _EXP[1 + i]
        for c in msg:
            r = _mul(r, a) ^ c
        if r:
            return None, -1
    return bytes(msg[:n - nroots]), len(pos)


def _bits_to_bytes(bits):
    return bytes(sum(bits[i + k] << k for k in range(8)) for i in range(0, len(bits) - 7, 8))


def _bytes_to_bits(data):
    return [(b >> k) & 1 for b in data for k in range(8)]


def encode_bits(stuffed_bits, checks=16):
    """The bit-stuffed AX.25 frame (with opening and closing flags) -> FX.25 bits (tag + codeblock).
    Returns None if the frame is too big for FX.25."""
    nbytes = (len(stuffed_bits) + 7) // 8
    for n, k in CHECK_FAMILIES.get(checks, CHECK_FAMILIES[16]):
        if nbytes <= k:
            break
    else:
        return None
    tag = next(t for t, nk in TAGS.items() if nk == (n, k))
    flag = [0, 1, 1, 1, 1, 1, 1, 0]
    bits = list(stuffed_bits)
    while len(bits) < k * 8:                       # pad the data block with flags
        bits += flag
    data = _bits_to_bytes(bits[:k * 8])
    block = data + rs_encode(data, n - k)
    return [(tag >> i) & 1 for i in range(64)] + _bytes_to_bits(block)


class Receiver:
    """Watches the decoded (post-NRZI) bit stream for an FX.25 tag, then collects and repairs the codeblock.
    feed(bit) returns a corrected AX.25 frame (FCS stripped) when one completes, else None."""

    def __init__(self):
        self.reg = 0
        self.want = 0                               # bits still to collect for the current codeblock
        self.bits = []
        self.nk = None
        self.fixed = 0                              # frames repaired by RS
        self.decoded = 0

    def feed(self, bit):
        if self.want:
            self.bits.append(bit)
            self.want -= 1
            if self.want == 0:
                return self._finish()
            return None
        self.reg = (self.reg >> 1) | (bit << 63)
        for tag, nk in TAGS.items():
            if bin(self.reg ^ tag).count("1") <= TAG_MAX_BIT_ERRORS:
                self.nk, self.want, self.bits = nk, nk[0] * 8, []
                self.reg = 0
                break
        return None

    def _finish(self):
        n, k = self.nk
        block = _bits_to_bytes(self.bits)
        data, nfix = rs_decode(block, n - k)
        if data is None:
            return None
        frame = unstuff_frame(_bytes_to_bits(data))
        if frame is None:
            return None
        self.decoded += 1
        if nfix > 0:
            self.fixed += 1
        return frame


def unstuff_frame(bits):
    """Find the first HDLC frame between flags in a bit list, de-stuff it and check the FCS."""
    from afsk import crc16_x25
    s = "".join("1" if b else "0" for b in bits)
    start = s.find("01111110")
    while start >= 0 and s.startswith("01111110", start + 8):
        start += 8                                  # skip repeated opening flags
    if start < 0:
        return None
    i, ones, out = start + 8, 0, []
    while i < len(s):
        if s.startswith("01111110", i):
            break
        b = s[i]
        if b == "1":
            ones += 1
            out.append(1)
            if ones > 6:
                return None
        else:
            if ones == 5:
                ones = 0
                i += 1
                continue
            ones = 0
            out.append(0)
        i += 1
    else:
        return None
    if len(out) < 8 * 18 or len(out) % 8:
        return None
    by = _bits_to_bytes(out)
    body, fcs = by[:-2], by[-2] | (by[-1] << 8)
    return body if crc16_x25(body) == fcs else None
