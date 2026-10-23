# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""
tnc.py - TNC back-ends and PTT drivers.

Every TNC exposes:
    start(on_packet)   on_packet("SRC>DST,PATH:info") for each frame heard
    send(src, dst, path, info)
    stop()
    status -> dict

Back-ends
    SoundModemTNC  built-in AFSK1200 modem on a soundcard / radio USB audio
    KissTcpTNC     Direwolf, UZ7HO Soundmodem, any KISS-over-TCP TNC
    KissSerialTNC  radios with a built-in TNC (Kenwood TH-D74/D75, TM-D710),
                   Mobilinkd, hardware KISS TNCs
    NullTNC        APRS-IS only, no radio
"""

import collections
import os
import queue
import random
import re
import socket
import sys
import threading
import time

import afsk

FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD


def kiss_wrap(frame: bytes, port=0) -> bytes:
    esc = frame.replace(b"\xdb", b"\xdb\xdd").replace(b"\xc0", b"\xdb\xdc")
    return bytes([FEND, (port << 4) & 0xF0]) + esc + bytes([FEND])


class KissDecoder:
    def __init__(self):
        self.buf = bytearray()
        self.inside = False

    def feed(self, data: bytes):
        out = []
        for b in data:
            if b == FEND:
                if self.inside and len(self.buf) > 1 and (self.buf[0] & 0x0F) == 0:
                    raw = bytes(self.buf[1:])
                    out.append(raw.replace(b"\xdb\xdc", b"\xc0").replace(b"\xdb\xdd", b"\xdb"))
                self.buf = bytearray()
                self.inside = True
            elif self.inside:
                self.buf.append(b)
        return out


def _strip_fcs(frame_with_fcs: bytes) -> bytes:
    return frame_with_fcs[:-2]


# --------------------------------------------------------------------------
# PTT drivers
# --------------------------------------------------------------------------

class PTT:
    name = "VOX"
    can_tune = False      # True when the PTT link can also set frequency / mode (CAT)

    def open(self): pass
    def key(self, on: bool): pass
    def close(self): pass


class SerialPTT(PTT):
    """RTS or DTR on a serial port (Icom CI-V USB, Yaesu enhanced COM,
    Digirig, AIOC, home-brew transistor interfaces)."""

    def __init__(self, port, line="RTS", invert=False):
        self.port, self.line, self.invert = port, line.upper(), invert
        self.name = f"{self.line} on {port}"
        self.ser = None

    def open(self):
        import serial  # pyserial
        self.ser = serial.Serial()
        self.ser.port = self.port
        self.ser.rts = self.ser.dtr = False   # don't key up on open
        self.ser.open()
        self.key(False)

    def key(self, on):
        v = bool(on) ^ self.invert
        if self.line == "RTS":
            self.ser.rts, self.ser.dtr = v, False
        else:
            self.ser.dtr, self.ser.rts = v, False

    def close(self):
        if self.ser:
            try:
                self.key(False)
                self.ser.close()
            finally:
                self.ser = None


class CM108PTT(PTT):
    """GPIO on a CM108/CM119 USB audio chip (Digirig-Lite, AllStar fobs,
    many home-made interfaces). Linux: /dev/hidrawN."""

    def __init__(self, device="/dev/hidraw0", gpio=3):
        self.device, self.gpio = device, int(gpio)
        self.name = f"CM108 GPIO{gpio} on {device}"
        self.fd = None

    def open(self):
        self.fd = os.open(self.device, os.O_WRONLY)
        self.key(False)

    def key(self, on):
        bit = 1 << (self.gpio - 1)
        os.write(self.fd, bytes([0, 0, bit, bit if on else 0, 0]))

    def close(self):
        if self.fd is not None:
            try:
                self.key(False)
            finally:
                os.close(self.fd)
                self.fd = None


class RigctldPTT(PTT):
    """CAT PTT (and frequency / mode) through Hamlib's rigctld."""
    can_tune = True

    def __init__(self, host="127.0.0.1", port=4532):
        self.host, self.port = host, int(port)
        self.name = f"rigctld {host}:{port}"

    def _ask(self, cmd):
        with socket.create_connection((self.host, self.port), timeout=5) as s:
            s.sendall(cmd.encode() + b"\n")
            time.sleep(0.15)
            return s.recv(256).decode(errors="replace")

    def key(self, on):
        self._ask("T 1" if on else "T 0")

    def read_state(self):
        f = self._ask("f").split()
        m = self._ask("m").split()
        return {"hz": int(f[0]) if f and f[0].isdigit() else None,
                "mode": m[0] if m and not m[0].startswith("RPRT") else None,
                "pb": m[1] if len(m) > 1 and m[1].lstrip("-").isdigit() else "0"}

    def tune(self, hz):
        r = self._ask(f"F {int(hz)}")
        if "RPRT -" in r:
            raise IOError(f"rigctld refused frequency ({r.strip()})")
        if "RPRT -" in self._ask("M PKTFM 0"):      # FM data mode if the radio has one
            self._ask("M FM 0")

    def restore(self, st):
        if st.get("hz"):
            self._ask(f"F {st['hz']}")
        if st.get("mode"):
            self._ask(f"M {st['mode']} {st.get('pb', '0')}")


def _civ_addr(addr):
    return int(str(addr), 16) if isinstance(addr, str) else int(addr)


def civ_frame(addr, payload):
    """FE FE <radio> E0 <payload> FD"""
    return bytes([0xFE, 0xFE, _civ_addr(addr) & 0xFF, 0xE0]) + bytes(payload) + b"\xFD"


def civ_ptt_command(addr, on):
    """Icom CI-V 'transmit on/off' (command 1C 00): FE FE <radio> E0 1C 00 <01|00> FD"""
    return civ_frame(addr, [0x1C, 0x00, 0x01 if on else 0x00])


def hz_to_civ_bcd(hz):
    """Frequency in Hz -> 5 CI-V BCD bytes, least significant pair first.
    144.390 MHz -> 00 00 39 44 01"""
    s = f"{int(hz):010d}"
    return bytes((int(s[i]) << 4) | int(s[i + 1]) for i in range(8, -1, -2))


def civ_bcd_to_hz(b):
    return int("".join(f"{x:02x}" for x in reversed(bytes(b[:5]))))


def civ_replies(data, addr):
    """Payloads of frames the radio sent to the PC (skips our own echo)."""
    a = _civ_addr(addr)
    out = []
    for part in bytes(data).split(b"\xFE\xFE")[1:]:
        end = part.find(b"\xFD")
        if end >= 2 and part[0] == 0xE0 and part[1] == a:
            out.append(part[2:end])
    return out


class _CatSerialPTT(PTT):
    """Base for PTT commands sent over a radio's CAT serial port."""

    def __init__(self, port, baud):
        self.port, self.baud = port, int(baud)
        self.ser = None

    def open(self):
        import serial
        self.ser = serial.Serial()
        self.ser.port, self.ser.baudrate, self.ser.timeout = self.port, self.baud, 0.3
        self.ser.rts = self.ser.dtr = False   # never key up via RTS/DTR by accident
        self.ser.open()
        self.key(False)

    def _cmd(self, on):
        raise NotImplementedError

    def key(self, on):
        self.ser.reset_input_buffer()
        self.ser.write(self._cmd(on))
        self.ser.flush()
        time.sleep(0.05)
        self.ser.read(64)  # discard echo / acknowledgement

    def close(self):
        if self.ser:
            try:
                self.key(False)
            finally:
                self.ser.close()
                self.ser = None


class CivPTT(_CatSerialPTT):
    """Icom CI-V PTT, frequency and mode over USB or a CI-V cable
    (IC-9100, IC-7100, IC-705, IC-9700...)."""
    can_tune = True

    def __init__(self, port, baud=9600, addr="7C"):
        super().__init__(port, baud)
        self.addr = addr
        self.name = f"CI-V {addr}h on {port}"

    def _cmd(self, on):
        return civ_ptt_command(self.addr, on)

    def _ask(self, payload):
        """Send one CI-V command; return the radio's reply payload (or None)."""
        self.ser.reset_input_buffer()
        self.ser.write(civ_frame(self.addr, payload))
        self.ser.flush()
        data, t0 = b"", time.time()
        while time.time() - t0 < 0.6:
            data += self.ser.read(64)
            r = civ_replies(data, self.addr)
            if r:
                return r[0]
        return None

    def read_state(self):
        st = {}
        r = self._ask([0x03])                         # read operating frequency
        if r and r[0] == 0x03 and len(r) >= 6:
            st["hz"] = civ_bcd_to_hz(r[1:6])
        r = self._ask([0x04])                         # read mode + filter
        if r and r[0] == 0x04 and len(r) >= 3:
            st["mode"] = [r[1], r[2]]
        r = self._ask([0x1A, 0x06])                   # read DATA mode
        if r and r[:2] == b"\x1A\x06" and len(r) >= 3:
            st["data"] = list(r[2:])
        return st

    def tune(self, hz):
        r = self._ask([0x05] + list(hz_to_civ_bcd(hz)))
        if r != b"\xFB":
            raise IOError("radio did not accept the frequency (check CI-V address and baud rate)"
                          if r is None else "radio refused the frequency - is it in this radio's range?")
        self._ask([0x06, 0x05, 0x01])                 # FM, filter 1
        if self._ask([0x1A, 0x06, 0x01, 0x01]) != b"\xFB":   # DATA mode on (FM-D)
            self._ask([0x1A, 0x06, 0x01])             # radios that take a single data byte

    def restore(self, st):
        if st.get("hz"):
            self._ask([0x05] + list(hz_to_civ_bcd(st["hz"])))
        if st.get("mode"):
            self._ask([0x06] + st["mode"])
        if st.get("data"):
            self._ask([0x1A, 0x06] + st["data"])


class YaesuCatPTT(_CatSerialPTT):
    """Yaesu text CAT: PTT (TX1; / TX0;), frequency (FA) and mode (MD) -
    FT-991A, FTDX10/101, FT-710 and other newer radios."""
    can_tune = True

    def __init__(self, port, baud=38400):
        super().__init__(port, baud)
        self.name = f"Yaesu CAT on {port}"

    def _cmd(self, on):
        return b"TX1;" if on else b"TX0;"

    def _ask(self, cmd, reply=False):
        self.ser.reset_input_buffer()
        self.ser.write(cmd.encode())
        self.ser.flush()
        if not reply:
            time.sleep(0.1)
            return ""
        data, t0 = b"", time.time()
        while time.time() - t0 < 0.6 and not data.endswith(b";"):
            data += self.ser.read(32)
        return data.decode(errors="replace").strip()

    def read_state(self):
        st = {}
        f = self._ask("FA;", True)
        if f.startswith("FA") and f[2:-1].isdigit():
            st["hz"] = int(f[2:-1])
        m = self._ask("MD0;", True)
        if m.startswith("MD0") and len(m) >= 5:
            st["mode"] = m[3]
        return st

    def tune(self, hz):
        self._ask(f"FA{int(hz):09d};")
        self._ask("MD0A;")                            # DATA-FM
        f = self._ask("FA;", True)
        if f and f.startswith("FA") and f[2:-1].isdigit() and int(f[2:-1]) != int(hz):
            raise IOError(f"radio is on {int(f[2:-1]) / 1e6:.4f} MHz, not {hz / 1e6:.4f} MHz")
        if not f:
            raise IOError("no reply from the radio (check CAT port and baud rate)")

    def restore(self, st):
        if st.get("hz"):
            self._ask(f"FA{int(st['hz']):09d};")
        if st.get("mode"):
            self._ask(f"MD0{st['mode']};")


# APRS frequencies offered on the face plate (MHz, label)
APRS_FREQS = [
    ("auto", "Automatic - from my grid square / zone"),
    ("144.390", "144.390 (N. America / Caribbean)"),
    ("144.800", "144.800 (Europe / Africa)"),
    ("144.930", "144.930 (Argentina / Uruguay)"),
    ("145.570", "145.570 (Brazil)"),
    ("144.640", "144.640 (Japan / China)"),
    ("145.175", "145.175 (Australia)"),
    ("144.575", "144.575 (New Zealand)"),
    ("145.825", "145.825 (ISS digipeater)"),
]


def aprs_freq_hz(cfg):
    """The chosen APRS frequency in Hz (custom value allowed), or None if invalid."""
    v = str(cfg.get("aprs_freq") or "auto")
    if v == "auto":
        v = ZONES[zone_of(cfg)]["aprs"]
    if v == "custom":
        v = str(cfg.get("aprs_freq_custom") or "")
    try:
        mhz = float(v.replace(",", "."))
    except ValueError:
        return None
    if not band_ok(mhz, zone_of(cfg)):
        return None
    return int(round(mhz * 1e6))


def make_ptt(cfg):
    kind = cfg.get("ptt", "VOX")
    if kind == "CIV":
        return CivPTT(cfg.get("ptt_port", ""), cfg.get("cat_baud", 9600), cfg.get("civ_addr") or "7C")
    if kind == "YAESU_CAT":
        return YaesuCatPTT(cfg.get("ptt_port", ""), cfg.get("cat_baud", 38400))
    if kind in ("RTS", "DTR"):
        return SerialPTT(cfg.get("ptt_port", ""), kind, cfg.get("ptt_invert", False))
    if kind == "CM108":
        return CM108PTT(cfg.get("ptt_port") or "/dev/hidraw0", cfg.get("cm108_gpio", 3))
    if kind == "RIGCTLD":
        host, _, port = (cfg.get("ptt_port") or "127.0.0.1:4532").partition(":")
        return RigctldPTT(host, port or 4532)
    return PTT()


# --------------------------------------------------------------------------
# TNC back-ends
# --------------------------------------------------------------------------

class BaseTNC:
    kind = "none"

    def __init__(self):
        self.on_packet = None
        self.rx_count = 0
        self.tx_count = 0
        self.last_rx = 0.0
        self.tx_active = False
        self.error = ""
        self._stop = threading.Event()
        self.airtime = AirtimeGuard()
        self.on_frame = None

    def _heard(self, frame_no_fcs):
        if self.on_frame:                      # KISS server: pass the raw frame to other APRS apps
            try:
                self.on_frame(frame_no_fcs)
            except Exception:
                pass
        line = afsk.decode_ax25(frame_no_fcs)
        if line:
            self.rx_count += 1
            self.last_rx = time.time()
            if self.on_packet:
                self.on_packet(line)

    def start(self, on_packet):
        self.on_packet = on_packet

    def stop(self):
        self._stop.set()

    @property
    def status(self):
        return {"kind": self.kind, "rx": self.rx_count, "tx": self.tx_count,
                "tx_active": self.tx_active, "last_rx": self.last_rx, "error": self.error,
                "airtime_pct": round(100 * self.airtime.used() / self.airtime.window, 1)}


AIRTIME_WINDOW_S = 600       # this station may transmit at most...
AIRTIME_PCT = 10             # ...10% of any 10 minutes (60 s) on VHF/UHF packet
AIRTIME_PCT_ROBUST = 25      # Robust HF: one message takes 12-45 s, so 25% (150 s) there
PTT_MAX_S = {"1200": 10, "300": 20, "robust": 50}   # longest single transmission (watchdog)


class AirtimeGuard:
    """Keeps this station's share of the channel down: at most AIRTIME_PCT % of any AIRTIME_WINDOW_S."""

    def __init__(self, pct=AIRTIME_PCT, window=AIRTIME_WINDOW_S, now=time.time):
        self.budget, self.window, self.now = window * pct / 100.0, window, now
        self.log = collections.deque()      # (start time, seconds on air)
        self.lock = threading.Lock()

    def used(self):
        t = self.now()
        while self.log and t - self.log[0][0] >= self.window:
            self.log.popleft()
        return sum(d for _, d in self.log)

    def wait(self, dur):
        """Seconds to wait before a dur-second transmission fits in the budget (0 = go now)."""
        with self.lock:
            used, t = self.used(), self.now()
            if used + dur <= self.budget:
                return 0.0
            need, freed = used + dur - self.budget, 0.0
            for start, d in self.log:
                freed += d
                if freed >= need:
                    return max(0.5, start + self.window - t)
            return float(self.window)

    def add(self, dur):
        with self.lock:
            self.log.append((self.now(), dur))


def frame_airtime(nbytes, baud=1200, txdelay_ms=300, txtail_ms=50):
    """Rough on-air seconds of one AX.25 frame (bit stuffing and flags included)."""
    return (nbytes + 20) * 8 * 1.05 / baud + (txdelay_ms + txtail_ms) / 1000.0


class NullTNC(BaseTNC):
    kind = "APRS-IS only"

    def send(self, src, dst, path, info):
        pass


class SoundModemTNC(BaseTNC):
    kind = "Sound modem"

    def __init__(self, in_dev=None, out_dev=None, rate=48000, ptt=None,
                 txdelay=300, txtail=50, level=0.5, busy_hold=1.5,
                 tune_hz=None, cat_restore=True, modem="1200"):
        super().__init__()
        self.modem = modem if (modem in afsk.PROFILES or modem == "robust") else "1200"
        self.in_dev, self.out_dev, self.rate = in_dev, out_dev, int(rate)
        self.ptt = ptt or PTT()
        self.txdelay, self.txtail, self.level = txdelay, txtail, level
        self.busy_hold = busy_hold
        self.audio_level = 0.0
        self.noise_floor = None    # quietest recent audio level: the reference for carrier sense
        self.txq = queue.Queue()
        self.demod = afsk.RobustDemodulator(self.rate) if self.modem == "robust" else afsk.Demodulator(self.rate, self.modem)
        if self.modem == "robust":
            self.airtime = AirtimeGuard(AIRTIME_PCT_ROBUST)
        self.tune_hz, self.cat_restore = tune_hz, cat_restore
        self.cat_saved = None
        self.cat_status = ""       # shown on the face plate
        self.cat_lock = threading.Lock()

    def start(self, on_packet):
        super().start(on_packet)
        import sounddevice as sd  # noqa: F401 (fail early with a clear error)
        self.ptt.open()
        if self.tune_hz:
            self.tune(self.tune_hz)
        threading.Thread(target=self._rx_loop, daemon=True).start()
        threading.Thread(target=self._tx_loop, daemon=True).start()

    def tune(self, hz):
        """Put the radio on the APRS frequency in FM data mode (CAT radios only).
        A CAT problem is reported but never stops the station."""
        if not self.ptt.can_tune:
            self.cat_status = "set the frequency on the radio (this PTT method has no CAT tuning)"
            return False
        with self.cat_lock:
            try:
                if self.cat_saved is None and self.cat_restore:
                    try:
                        self.cat_saved = self.ptt.read_state() or None
                    except Exception:
                        self.cat_saved = None
                self.ptt.tune(hz)
                self.cat_status = f"tuned {hz / 1e6:.3f} MHz FM data"
                return True
            except Exception as e:
                self.cat_status = f"CAT tuning failed: {e}"
                return False

    def _restore_radio(self):
        if self.cat_saved and self.cat_restore and self.ptt.can_tune:
            with self.cat_lock:
                try:
                    self.ptt.restore(self.cat_saved)
                except Exception:
                    pass

    def _rx_loop(self):
        import sounddevice as sd
        q = queue.Queue()

        def cb(indata, frames, t, st):
            q.put(indata[:, 0].copy())
        try:
            with sd.InputStream(device=self.in_dev, channels=1, samplerate=self.rate,
                                blocksize=self.rate // 10, dtype="float32", callback=cb):
                while not self._stop.is_set():
                    try:
                        chunk = q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    self.audio_level = float((chunk ** 2).mean() ** 0.5)
                    if not self.tx_active:             # follow the quiet level: down fast, up slowly
                        nf = self.noise_floor
                        self.noise_floor = self.audio_level if nf is None or self.audio_level < nf else \
                            nf + (self.audio_level - nf) * 0.002
                    if self.tx_active:
                        continue  # don't decode our own transmission
                    for fr in self.demod.process(chunk):
                        if self.modem == "robust":
                            self._heard_text(fr)
                        else:
                            self._heard(fr)
        except Exception as e:
            self.error = f"audio input: {e}"

    def _tx_loop(self):
        import sounddevice as sd
        while not self._stop.is_set():
            try:
                frame = self.txq.get(timeout=0.5)
            except queue.Empty:
                continue
            # never transmit on a busy channel: wait for no packets AND no carrier, then a random back-off
            t0 = time.time()
            while self.channel_busy() and time.time() - t0 < 20:
                time.sleep(0.2)
            if self.channel_busy():
                self.txq.put(frame)                    # still busy after 20 s: try again later
                self.error = "channel busy - waiting to transmit"
                time.sleep(1)
                continue
            time.sleep(random.uniform(0.1, 1.2))
            if self.channel_busy():
                self.txq.put(frame)
                continue
            try:
                if self.modem == "robust":
                    np = afsk.np
                    audio = np.concatenate([np.zeros(int(self.rate * self.txdelay / 1000), dtype=np.float32),
                                            afsk.robust_modulate(frame, self.rate, self.level, lead_ms=50)])
                else:
                    audio = afsk.modulate(frame, self.rate, self.txdelay, self.txtail, self.level, self.modem)
            except Exception as e:
                self.error = f"transmit: {e}"
                continue
            dur = len(audio) / float(self.rate)
            limit = PTT_MAX_S.get(self.modem, 10)
            if dur > limit:
                self.error = f"transmit refused: {dur:.0f} s is longer than the {limit} s transmit limit"
                continue
            wait = self.airtime.wait(dur)
            if wait > 0:                    # over 10% of the last 10 minutes: hold until there is room
                self.error = (f"airtime limit ({self.airtime.budget * 100 / self.airtime.window:.0f}% of 10 min)"
                              f" - next transmission in {int(wait)} s")
                if self._stop.wait(wait):
                    break
                self.error = ""
            watchdog = threading.Timer(limit + 2, self._watchdog)   # force PTT off if playback hangs
            try:
                self.tx_active = True
                watchdog.start()
                self.ptt.key(True)
                self.airtime.add(dur)
                sd.play(audio, self.rate, device=self.out_dev, blocking=True)
                self.tx_count += 1
                if self.error.startswith(("channel busy", "airtime limit")):
                    self.error = ""
            except Exception as e:
                self.error = f"transmit: {e}"
            finally:
                watchdog.cancel()
                try:
                    self.ptt.key(False)
                finally:
                    self.tx_active = False
            time.sleep(0.5)

    def channel_busy(self):
        """Busy = a packet was decoded in the last busy_hold s, or audio well above the quiet level (a carrier)."""
        if time.time() - self.last_rx < self.busy_hold:
            return True
        nf = self.noise_floor
        return nf is not None and self.audio_level > max(0.02, nf * 4)

    def _watchdog(self):
        """Transmit timer: the radio was keyed too long - unkey it whatever the audio is doing."""
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass
        try:
            self.ptt.key(False)
        except Exception:
            pass
        self.error = "transmit watchdog: PTT forced off (transmission ran too long)"

    def send(self, src, dst, path, info):
        if self.modem == "robust":         # robust frames carry the text line itself (no digipeating on HF)
            self.txq.put(f"{src}>{dst}:{info}".encode("latin-1", "replace"))
        else:
            self.txq.put(afsk.encode_ax25(src, dst, path, info))

    def send_frame(self, frame_no_fcs):
        """Transmit a raw AX.25 frame from another app (KISS server)."""
        if self.modem == "robust":
            raise IOError("KISS frames can't be sent in Robust mode")
        crc = afsk.crc16_x25(frame_no_fcs)
        self.txq.put(frame_no_fcs + bytes([crc & 0xFF, crc >> 8]))

    def _heard_text(self, payload):
        try:
            line = payload.decode("latin-1")
        except Exception:
            return
        head = line.partition(":")[0]
        if ">" not in head or not head.split(">")[0]:
            return
        self.rx_count += 1
        self.last_rx = time.time()
        if self.on_packet:
            self.on_packet(line)

    def stop(self):
        super().stop()
        try:
            self._restore_radio()
            self.ptt.close()
        except Exception:
            pass

    @property
    def status(self):
        d = super().status
        snr = getattr(self.demod, "last_snr", None)
        d.update(audio_level=round(self.audio_level, 4), ptt=self.ptt.name, modem=self.modem,
                 snr=round(snr, 1) if snr is not None else None,
                 cat=self.cat_status, freq=self.tune_hz)
        return d


class _KissStreamTNC(BaseTNC):
    def _open(self):
        raise NotImplementedError

    def _read(self):
        raise NotImplementedError

    def _write(self, data):
        raise NotImplementedError

    def start(self, on_packet):
        super().start(on_packet)
        self._open()
        threading.Thread(target=self._rx_loop, daemon=True).start()

    def _rx_loop(self):
        dec = KissDecoder()
        while not self._stop.is_set():
            try:
                data = self._read()
            except Exception as e:
                self.error = f"read: {e}"
                time.sleep(5)
                try:
                    self._open()
                    self.error = ""
                except Exception as e2:
                    self.error = f"reconnect: {e2}"
                continue
            if not data:
                continue
            for fr in dec.feed(data):
                self._heard(fr)

    def send(self, src, dst, path, info):
        frame = _strip_fcs(afsk.encode_ax25(src, dst, path, info))
        dur = frame_airtime(len(frame))
        wait = self.airtime.wait(dur)
        if wait > 0:
            raise IOError(f"airtime limit (10% of 10 min) - try again in {int(wait)} s")
        self.airtime.add(dur)
        self._send_kiss(frame)

    def send_frame(self, frame_no_fcs):
        dur = frame_airtime(len(frame_no_fcs))
        wait = self.airtime.wait(dur)
        if wait > 0:
            raise IOError(f"airtime limit (10% of 10 min) - try again in {int(wait)} s")
        self.airtime.add(dur)
        self._send_kiss(frame_no_fcs)

    def _send_kiss(self, frame):
        self.tx_active = True
        try:
            self._write(kiss_wrap(frame))
            self.tx_count += 1
        finally:
            self.tx_active = False


class SdrTNC(BaseTNC):
    """Receive-only: an RTL-SDR dongle via rtl_fm, decoded by Tradewind's own 1200-baud demodulator.
    Good for a listen-only station or an RX iGate (a Raspberry Pi + dongle + antenna)."""
    kind = "RTL-SDR (receive only)"
    RATE = 24000

    def __init__(self, freq_hz, gain="", ppm=0, index=0, rtl_fm="rtl_fm"):
        super().__init__()
        self.freq_hz, self.gain, self.ppm, self.index, self.rtl_fm = freq_hz, str(gain or ""), int(ppm or 0), int(index or 0), rtl_fm
        self.proc = None
        self.audio_level = 0.0

    def command(self):
        cmd = [self.rtl_fm, "-f", str(int(self.freq_hz)), "-M", "fm", "-s", str(self.RATE),
               "-d", str(self.index), "-p", str(self.ppm), "-E", "dc"]
        if self.gain and self.gain.lower() != "auto":
            cmd += ["-g", self.gain]
        return cmd + ["-"]

    def start(self, on_packet):
        super().start(on_packet)
        import shutil
        import subprocess
        exe = shutil.which(self.rtl_fm)
        if not exe:
            raise IOError("rtl_fm not found - install the RTL-SDR tools (Linux / Pi: sudo apt install rtl-sdr)")
        self.rtl_fm = exe
        self.proc = subprocess.Popen(self.command(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._rx_loop, daemon=True).start()

    def _rx_loop(self):
        np = afsk.np
        demod = afsk.Demodulator(self.RATE, "1200")
        block = self.RATE // 10 * 2            # 0.1 s of 16-bit samples
        try:
            while not self._stop.is_set():
                raw = self.proc.stdout.read(block)
                if not raw:
                    self.error = "rtl_fm stopped - is the dongle plugged in?"
                    break
                x = np.frombuffer(raw[:len(raw) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
                self.audio_level = float((x ** 2).mean() ** 0.5) if len(x) else 0.0
                for fr in demod.process(x):
                    self._heard(fr)
        except Exception as e:
            self.error = f"SDR: {e}"

    def send(self, src, dst, path, info):
        raise IOError("the SDR only receives - add a radio as a second link to transmit")

    def send_frame(self, frame_no_fcs):
        self.send(None, None, None, None)

    def stop(self):
        super().stop()
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass

    @property
    def status(self):
        d = super().status
        d.update(audio_level=round(self.audio_level, 4), freq=self.freq_hz)
        return d


def kiss_peer_ok(ip):
    """A KISS client may key the transmitter, so only loopback and private (LAN) addresses may connect."""
    import ipaddress
    try:
        a = ipaddress.ip_address(ip)
        if getattr(a, "ipv4_mapped", None):
            a = a.ipv4_mapped
        return a.is_loopback or (a.is_private and not a.is_link_local) or a.is_link_local
    except ValueError:
        return False


class KissServer:
    """Lets other APRS programs (Xastir, YAAC, APRSIS32, PinPoint...) use Tradewind's modem as a KISS TCP TNC.
    on_tx(frame) is called for each frame a client wants transmitted."""

    def __init__(self, host="127.0.0.1", port=8001, on_tx=None):
        self.host, self.port, self.on_tx = host, int(port), on_tx
        self.clients = []
        self.lock = threading.Lock()
        self.sock = None
        self.error = ""
        self._stop = threading.Event()

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(4)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self._stop.is_set():
            try:
                c, addr = self.sock.accept()
            except OSError:
                break
            if not kiss_peer_ok(addr[0]):            # only this computer and the home / club network
                self.error = f"refused KISS connection from {addr[0]} (not a local network address)"
                try:
                    c.close()
                except OSError:
                    pass
                continue
            with self.lock:
                self.clients.append(c)
            threading.Thread(target=self._client, args=(c,), daemon=True).start()

    def _client(self, c):
        dec = KissDecoder()
        try:
            while not self._stop.is_set():
                data = c.recv(2048)
                if not data:
                    break
                for fr in dec.feed(data):
                    if self.on_tx:
                        try:
                            self.on_tx(fr)
                        except Exception as e:
                            self.error = str(e)
        except OSError:
            pass
        with self.lock:
            if c in self.clients:
                self.clients.remove(c)
        try:
            c.close()
        except OSError:
            pass

    def broadcast(self, frame_no_fcs):
        data = kiss_wrap(frame_no_fcs)
        with self.lock:
            for c in list(self.clients):
                try:
                    c.sendall(data)
                except OSError:
                    self.clients.remove(c)

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except Exception:
            pass
        with self.lock:
            for c in self.clients:
                try:
                    c.close()
                except OSError:
                    pass
            self.clients = []


class KissTcpTNC(_KissStreamTNC):
    kind = "KISS TCP"

    def __init__(self, host="127.0.0.1", port=8001):
        super().__init__()
        self.host, self.port = host, int(port)
        self.sock = None

    def _open(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = socket.create_connection((self.host, self.port), timeout=10)
        self.sock.settimeout(1.0)

    def _read(self):
        try:
            data = self.sock.recv(4096)
        except socket.timeout:
            return b""
        if data == b"":
            raise ConnectionError("TNC closed connection")
        return data

    def _write(self, data):
        self.sock.sendall(data)

    def stop(self):
        super().stop()
        if self.sock:
            self.sock.close()


class KissSerialTNC(_KissStreamTNC):
    kind = "KISS serial"

    def __init__(self, port, baud=9600):
        super().__init__()
        self.port, self.baud = port, int(baud)
        self.ser = None

    def _open(self):
        import serial
        if self.ser:
            self.ser.close()
        self.ser = serial.Serial(self.port, self.baud, timeout=1)

    def _read(self):
        return self.ser.read(512)

    def _write(self, data):
        self.ser.write(data)

    def stop(self):
        super().stop()
        if self.ser:
            self.ser.close()


def make_tnc(cfg):
    mode = cfg.get("tnc", "none")
    if mode == "sound":
        def dev(v):
            return int(v) if str(v).isdigit() else (v or None)
        return SoundModemTNC(dev(cfg.get("audio_in")), dev(cfg.get("audio_out")),
                             cfg.get("sample_rate", 48000), make_ptt(cfg),
                             cfg.get("txdelay", 300), cfg.get("txtail", 50),
                             cfg.get("tx_level", 0.5),
                             tune_hz=(com_freq_hz(cfg) if cfg.get("op_mode") == "commercial" else aprs_freq_hz(cfg))
                             if cfg.get("cat_tune") else None,
                             cat_restore=cfg.get("cat_restore", True),
                             modem=str(cfg.get("modem") or "1200"))
    if mode == "kiss_tcp":
        host, _, port = (cfg.get("kiss_addr") or "127.0.0.1:8001").partition(":")
        return KissTcpTNC(host, port or 8001)
    if mode == "kiss_serial":
        return KissSerialTNC(cfg.get("kiss_serial", ""), cfg.get("kiss_baud", 9600))
    if mode == "sdr":
        return SdrTNC(aprs_freq_hz(cfg) or 144_390_000, cfg.get("sdr_gain", ""), cfg.get("sdr_ppm", 0),
                      cfg.get("sdr_index", 0))
    return NullTNC()


# --------------------------------------------------------------------------
# Persistent APRS-IS client (for announcements and iGate)
# --------------------------------------------------------------------------

class AprsIsClient:
    def __init__(self, call, passcode, server="rotate.aprs2.net", port=14580,
                 filt="", software="Tradewind", version="1.0"):
        self.call, self.passcode = call.upper(), passcode
        self.server, self.port, self.filt = server, int(port), filt
        self.software, self.version = software, version
        self.sock = None
        self.lock = threading.Lock()
        self.connected = False
        self.verified = False
        self.error = ""
        self._stop = threading.Event()
        self.on_line = None

    def start(self, on_line=None):
        self.on_line = on_line
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._connect()
                f = self.sock.makefile("rb")
                last_ka = time.time()
                while not self._stop.is_set():
                    line = f.readline()
                    if not line:
                        raise ConnectionError("server closed connection")
                    s = line.decode("latin-1", "replace").strip()
                    if s and not s.startswith("#") and self.on_line:
                        self.on_line(s)
                    if time.time() - last_ka > 600:
                        self._send_raw("#keepalive")
                        last_ka = time.time()
            except Exception as e:
                self.error = str(e)
            self.connected = self.verified = False
            if self.sock:
                try:
                    self.sock.close()
                except OSError:
                    pass
            self._stop.wait(30)

    def _connect(self):
        self.sock = socket.create_connection((self.server, self.port), timeout=30)
        self.sock.settimeout(300)
        self.sock.recv(512)
        login = f"user {self.call} pass {self.passcode} vers {self.software} {self.version}"
        if self.filt:
            login += f" filter {self.filt}"
        self.sock.sendall((login + "\r\n").encode())
        resp = self.sock.recv(512).decode(errors="replace")
        self.verified = " verified" in resp and "unverified" not in resp
        self.connected = True
        self.error = "" if self.verified else "login not verified - check passcode"

    def set_filter(self, filt):
        """Change the server-side filter on the fly (Wolf Mode watches DX callsigns with b/CALL*)."""
        self.filt = filt
        if self.connected:
            try:
                self._send_raw("#filter " + filt)
            except OSError:
                pass

    def _send_raw(self, line):
        with self.lock:
            if self.sock and self.connected:
                self.sock.sendall((line + "\r\n").encode("latin-1", "replace"))

    def send(self, line):
        if not self.verified:
            raise ConnectionError(self.error or "APRS-IS not connected")
        self._send_raw(line)

    def stop(self):
        self._stop.set()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


def list_audio_devices():
    try:
        import sounddevice as sd
        devs = []
        for i, d in enumerate(sd.query_devices()):
            devs.append({"index": i, "name": d["name"],
                         "inputs": d["max_input_channels"], "outputs": d["max_output_channels"]})
        return devs, ""
    except Exception as e:
        return [], f"sounddevice unavailable: {e}"


def list_serial_ports():
    try:
        from serial.tools import list_ports
        return [{"device": p.device, "desc": p.description} for p in list_ports.comports()], ""
    except Exception as e:
        ports = []
        if sys.platform.startswith("linux"):
            try:
                for d in sorted(os.listdir("/dev")):
                    if d.startswith(("ttyUSB", "ttyACM", "rfcomm", "hidraw")):
                        ports.append({"device": "/dev/" + d, "desc": ""})
            except OSError:
                pass
        return ports, f"pyserial unavailable: {e}"


# --------------------------------------------------------------------------
# Two-way APRS chat with a link handshake
# --------------------------------------------------------------------------
# Everything is a standard APRS message (":ADDRESSEE:text{NN"), acknowledged
# with "ackNN", so it works over 2m/70cm RF and APRS-IS and with any APRS
# station (plain messages from APRSdroid, a TH-D75 etc. show up too).
#
#   hailer                               called station
#   TW-LINK? ...         ------------->  "incoming request" -> Accept / Decline
#                        <-------------  TW-LINK OK ... (or a typed ACCEPT)
#   "accepted - confirm" -> Accept
#   TW-LINK CONFIRMED    ------------->  chat open on both sides
#   TW-LINK END          <------------>  either side closes the link
#
# Each step is a numbered message retried until acked, so a completed
# handshake proves a two-way path - over RF only, if chosen.


MAX_TEXT = 67                        # APRS message text limit
RETRY_S = (30, 60, 120)              # resend delays until acked
LINK_TIMEOUT_S = 15 * 60             # unanswered hail / confirmation expires
# ---- abuse limits (v3.2)
HAIL_MAX_TRIES = 5                   # a hail is sent at most 5 times...
HAIL_MIN_GAP_S = 60                  # ...at least 60 s apart
HAIL_FOREVER_MAX_S = 30 * 60         # "keep calling" gives up after 30 minutes
DECLINE_COOLDOWN_S = 15 * 60         # after a "no", wait 15 minutes before hailing them again
MUTE_HAILS, MUTE_WINDOW_S, MUTE_S = 3, 600, 3600   # >3 hails in 10 min from one station = muted 1 hour
PROBES_PER_HOUR = 4
MSG_GAP_S = 30                       # one unanswered message per station, 30 s apart at most
RELAYS_PER_HOUR = 10
OUTBOX_PER_STATION, OUTBOX_MAX = 10, 50
SMART_RELAY_WAIT_S = 240            # RF-first routing: how long a relay has to confirm delivery
SMART_RELAYS = 2                     # ...and how many recently heard Tradewind stations are asked
RELAY_TAG_RE = re.compile(r"\[de \S+ via \S+\]", re.I)
CHAT_CALL_RE = re.compile(r"^[A-Z0-9]{1,6}(-([0-9]|1[0-5]))?$")

T_REQ, T_OK, T_CONF, T_NO, T_END = "TW-LINK?", "TW-LINK OK", "TW-LINK CONFIRMED", "TW-LINK NO", "TW-LINK END"

# link states
EMAIL2 = "EMAIL-2"            # APRS-IS email gateway service (javAPRSSrvr)
MAIL_MAX_CHARS = 500          # longest email body Tradewind will send
MAIL_MAX_PARTS = 14
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
CALL_LOCAL_RE = re.compile(r"^(?=.*\d)[A-Za-z0-9]{3,7}([-._][A-Za-z0-9]{1,2})?$")


def ham_address(addr):
    """True when an email address clearly belongs to a licensed amateur: a Winlink address,
    or a callsign as the mailbox name (8p6xx@..., 8p6xx-9@...)."""
    addr = (addr or "").strip().lower()
    if not EMAIL_RE.match(addr):
        return False
    local, _, domain = addr.partition("@")
    return domain in ("winlink.org", "arrl.net") or bool(CALL_LOCAL_RE.match(local))


def _chunks(text, size):
    text = " ".join((text or "").split())
    out = []
    while text:
        if len(text) <= size:
            out.append(text)
            break
        cut = text.rfind(" ", 0, size + 1)
        cut = cut if cut > size // 2 else size
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    return out


T_QSY, T_QSY_OK, T_QSY_NO, T_QSY_CHK, T_QSY_BACK = "TW-QSY", "TW-QSY-OK", "TW-QSY-NO", "TW-QSY-CHECK", "TW-QSY-BACK"
QSY_WAIT_S = 120          # no contact on the new frequency within this time: both go back
QSY_SUGGEST_AFTER = 5     # suggest moving after this many messages on the APRS frequency
# IARU Region 2: 145.000-145.100 MHz = data (ACDS), 10 kHz channels, 12 kHz max; 145.010 protected for
# Caribbean APRS. A 1200-baud FM signal is ~10-12 kHz wide, so chat channels sit 20 kHz apart.
# 144.500-144.600 is a local-option segment (agree it nationally); 145.000-145.100 is the data segment.
QSY_DEFAULT = "144.510,144.530,144.550,144.570,144.590,145.030,145.050,145.070,145.090"
QSY_NEVER = ((144.0, 144.400), (144.400, 144.500), (145.005, 145.015), (145.790, 146.000), (435.0, 438.0))
# weak-signal CW/SSB and APRS, beacons + ISS uplink 144.490, Caribbean APRS 145.010, satellites
QSY_BUSY_S = 30 * 60      # a channel another pair moved to counts as busy this long (or until they leave)


# ---- v3.6: band-plan zones, picked from the grid square (manual override in settings) ----
SAT_NEVER = ((145.790, 146.000), (435.0, 438.0))     # satellites + ISS (145.800 voice, 145.825 APRS): never chat or hail
R1_QSY = "144.850,144.875,144.900,144.925,144.950,144.975"
ZONES = {
    # key: name, APRS frequency, default chat channels ("" = none known: set your own), segments chat must avoid
    "r2": {"name": "North America & Caribbean (IARU Region 2)", "aprs": "144.390", "qsy": QSY_DEFAULT,
           "never": QSY_NEVER, "note": "144.5 MHz channels are a local-option segment - confirm with your national society."},
    "br": {"name": "Brazil", "aprs": "145.570", "qsy": "",
           "never": ((144.0, 144.500), (145.555, 145.585)) + SAT_NEVER, "note": "No agreed chat channels known - add your own in the menu."},
    "ar": {"name": "Argentina / Uruguay", "aprs": "144.930", "qsy": "",
           "never": ((144.0, 144.500), (144.915, 144.945)) + SAT_NEVER, "note": "No agreed chat channels known - add your own in the menu."},
    "r1": {"name": "Europe, Africa & Middle East (IARU Region 1)", "aprs": "144.800", "qsy": R1_QSY,
           "never": ((144.0, 144.500), (144.785, 144.8125)) + SAT_NEVER,
           "note": "Chat channels are in the Region 1 digital segment (144.8125-144.9875), 25 kHz apart and 50 kHz clear of 144.800 - some countries use them for packet; confirm locally."},
    "jp": {"name": "Japan", "aprs": "144.640", "qsy": "",
           "never": ((144.0, 144.500), (144.625, 144.675)) + SAT_NEVER, "note": "No agreed chat channels known - add your own in the menu."},
    "cn": {"name": "China", "aprs": "144.640", "qsy": "",
           "never": ((144.0, 144.500), (144.625, 144.655)) + SAT_NEVER, "note": "No agreed chat channels known - add your own in the menu."},
    "au": {"name": "Australia", "aprs": "145.175", "qsy": "",
           "never": ((144.0, 144.500), (145.160, 145.190)) + SAT_NEVER, "note": "No agreed chat channels known - add your own in the menu."},
    "nz": {"name": "New Zealand", "aprs": "144.575", "qsy": "",
           "never": ((144.0, 144.500), (144.560, 144.590)) + SAT_NEVER, "note": "No agreed chat channels known - add your own in the menu."},
    "r3": {"name": "Other Asia-Pacific (IARU Region 3)", "aprs": "144.390", "qsy": "",
           "never": ((144.0, 144.500), (144.375, 144.405)) + SAT_NEVER,
           "note": "APRS frequency differs by country here - check yours, then set it and your chat channels in the menu."},
}
ZONE = "r2"                    # the active zone (set by the app from config)
ZONE_VHF = {"r1": (144.0, 146.0), "jp": (144.0, 146.0)}     # 2 m is 144-146 MHz here, 144-148 elsewhere
UHF = [430.0, 440.0]           # 70 cm range on the operator's licence (Barbados: 430-440; set in the menu)


def set_uhf(text):
    """'430-440' -> the 70 cm range this licence allows (kept inside 420-450)."""
    try:
        lo, hi = (float(x) for x in str(text).replace(" ", "").split("-"))
        if 420.0 <= lo < hi <= 450.0:
            UHF[:] = [lo, hi]
            return True
    except (TypeError, ValueError):
        pass
    return False


def band_ok(mhz, zone=None):
    """Inside 2 m for the zone, or inside the licensed 70 cm range."""
    lo, hi = ZONE_VHF.get(zone or ZONE, (144.0, 148.0))
    return lo <= mhz <= hi or UHF[0] <= mhz <= UHF[1]


def grid_latlon(grid):
    """Centre of a 4- or 6-character Maidenhead square, or None."""
    g = (grid or "").strip()
    if not re.match(r"^[A-Ra-r]{2}[0-9]{2}([A-Xa-x]{2})?$", g):
        return None
    g = g[:2].upper() + g[2:4] + g[4:6].lower()
    lon = (ord(g[0]) - 65) * 20 - 180 + int(g[2]) * 2
    lat = (ord(g[1]) - 65) * 10 - 90 + int(g[3])
    if len(g) == 6:
        lon += (ord(g[4]) - 97) * (5 / 60) + 2.5 / 60
        lat += (ord(g[5]) - 97) * (2.5 / 60) + 1.25 / 60
    else:
        lon, lat = lon + 1, lat + 0.5
    return lat, lon


def zone_for_grid(grid):
    """Best-guess band-plan zone for a grid square (rough boxes; the menu override fixes border cases)."""
    p = grid_latlon(grid)
    if not p:
        return "r2"
    lat, lon = p
    if -170 <= lon <= -25:                           # the Americas
        if lat < -27 and lon < -53:
            return "ar"
        if -34 <= lat <= 5.5 and -60 <= lon <= -34:
            return "br"
        return "r2"
    if 24 <= lat <= 46 and 128.5 <= lon <= 146:
        return "jp"
    if -48 <= lat <= -33 and 165 <= lon <= 179.9:
        return "nz"
    if -45 <= lat <= -10 and 112 <= lon <= 155:
        return "au"
    if 33 <= lat <= 39 and 124 <= lon <= 131.5:      # Korea
        return "r3"
    if 18 <= lat <= 54 and 73 <= lon <= 135:
        return "cn"
    if -25 <= lon <= 60 or (lat >= 40 and 60 < lon <= 180):
        return "r1"                                  # Europe, Africa, Middle East, Russia
    return "r3"


def zone_of(cfg):
    """The zone a config uses: its manual choice, or the one its grid square falls in."""
    z = str(cfg.get("zone") or "auto")
    return z if z in ZONES else zone_for_grid(cfg.get("grid", ""))


def set_zone(z):
    global ZONE
    ZONE = z if z in ZONES else "r2"


def zone_qsy(cfg):
    """Chat channels in use: the zone's list, or the operator's own list when set to manual."""
    z = zone_of(cfg)
    raw = ZONES[z]["qsy"] if (cfg.get("qsy_mode") or "auto") == "auto" else str(cfg.get("qsy_freqs") or "")
    return [f.strip() for f in raw.split(",") if f.strip() and qsy_freq_ok(f.strip(), z)]


def on_satellite(mhz):
    try:
        f = float(mhz)
    except (TypeError, ValueError):
        return False
    return any(lo <= f <= hi for lo, hi in SAT_NEVER)


def qsy_freq_ok(mhz, zone=None):
    """A chat QSY frequency must be in 2 m or 70 cm, and not on APRS, protected or satellite segments of the zone."""
    try:
        f = float(mhz)
    except (TypeError, ValueError):
        return False
    if not band_ok(f, zone):
        return False
    never = ZONES.get(zone or ZONE, ZONES["r2"])["never"]
    return not any(lo <= f <= hi for lo, hi in tuple(never) + SAT_NEVER)


IDLE, HAILING, AWAIT_CONFIRM, INCOMING, ACCEPTED, OPEN, DECLINED, ENDED, EXPIRED = (
    "idle", "hailing", "await_confirm", "incoming", "accepted", "open", "declined", "ended", "expired")
STATUS_TEXT = {
    IDLE: "No link", HAILING: "Calling - waiting for an answer",
    AWAIT_CONFIRM: "They accepted - press Accept to confirm",
    INCOMING: "Wants to chat - Accept or Decline",
    ACCEPTED: "You accepted - waiting for their confirmation",
    OPEN: "Link open - chat", DECLINED: "Declined", ENDED: "Link ended", EXPIRED: "No answer - expired",
}


def norm_call(c):
    return (c or "").strip().upper()


def valid_call(c):
    return bool(CHAT_CALL_RE.match(norm_call(c)))


def format_message(addressee, text, msgno=None, replyack=None):
    """APRS message info field: ':ADDRESSEE:text{NN', or with an APRS 1.1 reply-ack ':ADDRESSEE:text{NN}AA'
    (AA = their message we acknowledge inside this one; '' = just say we understand reply-acks)."""
    t = text.replace("|", "/").replace("~", "-").replace("{", "(").replace("}", ")")[:MAX_TEXT]
    if msgno and replyack is not None:
        return f":{norm_call(addressee):<9}:{t}{{{msgno}}}{replyack}"
    return f":{norm_call(addressee):<9}:{t}" + (f"{{{msgno}" if msgno else "")


def parse_message(line):
    """'SRC>DST,PATH:info' -> dict(src, to, text, msgno, ack, rej) or None."""
    head, sep, info = line.partition(":")
    if not sep or ">" not in head or len(info) < 11 or info[0] != ":" or info[10] != ":":
        return None
    src = head.split(">", 1)[0].rstrip("*").upper()
    path = head.split(">", 1)[1].upper()
    to = info[1:10].strip().upper()
    body = info[11:].rstrip("\r\n")
    m = re.fullmatch(r"(ack|rej)([A-Za-z0-9]{1,5})(\}.*)?", body)
    if m:
        return {"src": src, "to": to, "text": "", "msgno": None, "path": path,
                "ack": m.group(2) if m.group(1) == "ack" else None,
                "rej": m.group(2) if m.group(1) == "rej" else None}
    msgno, replyack, ra = None, None, False
    if "{" in body:
        body, _, tail = body.rpartition("{")
        msgno = tail.split("}")[0][:5] or None
        if "}" in tail:                      # APRS 1.1 reply-ack: {MM}AA
            ra = True
            replyack = tail.split("}", 1)[1].strip()[:5] or None
    return {"src": src, "to": to, "text": body, "msgno": msgno, "ack": None, "rej": None, "path": path,
            "replyack": replyack, "ra": ra}


def split_text(text):
    text = " ".join(text.split())
    out = []
    while text:
        if len(text) <= MAX_TEXT:
            out.append(text)
            break
        cut = text.rfind(" ", 0, MAX_TEXT)
        cut = cut if cut > 20 else MAX_TEXT
        out.append(text[:cut])
        text = text[cut:].strip()
    return out


def route_from_via(via):
    """Reply the way a packet arrived: 'RF' -> radio link A, 'RF:B' -> link B, 'IS' -> internet."""
    v = (via or "").upper()
    if v.startswith("RF:"):
        return "link:" + v[3:]
    return "rf" if v == "RF" else "is"


class LinkController:
    """Automatic link selection between radio paths (e.g. A = 2m 1200 AFSK on the
    built-in modem, B = 70cm 9600 G3RUH through Direwolf).

    For each station it keeps, per link: messages sent, delivered (acked),
    lost tries, round-trip time and measured throughput. It sends on the link
    with the best expected goodput (delivery rate x speed / delay), steps back
    to a slower link when errors increase, and retries the faster link again
    after a rest so the link can step back up when conditions improve.
    """
    FAIL_STREAK = 2          # lost tries in a row before falling back
    REST_S = 600             # after a fallback, try the faster link again after this long

    def __init__(self, links, now=time.time):
        self.links = dict(links)          # id -> {"name": str, "speed": bps}
        self.now = now
        self.stats = {}                   # (peer, link) -> dict

    def _st(self, peer, link):
        return self.stats.setdefault((peer, link), {"sent": 0, "ok": 0, "lost": 0, "streak": 0, "rtt": None,
                                                    "tput": None, "last_fail": 0, "last_ok": 0, "heard": 0})

    def sent(self, peer, link):
        if link in self.links:
            self._st(peer, link)["sent"] += 1

    def lost(self, peer, link):
        if link in self.links:
            st = self._st(peer, link)
            st["lost"] += 1
            st["streak"] += 1
            st["last_fail"] = self.now()

    def acked(self, peer, link, rtt, nbytes):
        if link not in self.links:
            return
        st = self._st(peer, link)
        st["ok"] += 1
        st["streak"] = 0
        st["last_ok"] = self.now()
        rtt = max(0.05, float(rtt))
        st["rtt"] = rtt if st["rtt"] is None else 0.7 * st["rtt"] + 0.3 * rtt
        tp = nbytes / rtt
        st["tput"] = tp if st["tput"] is None else 0.7 * st["tput"] + 0.3 * tp

    def heard(self, peer, link):
        if link in self.links:
            self._st(peer, link)["heard"] = self.now()

    def score(self, peer, link):
        st, speed = self._st(peer, link), self.links[link].get("speed") or 1200
        tries = st["ok"] + st["lost"]
        success = (st["ok"] + 1) / (tries + 2)          # optimistic start, learns quickly
        if st["heard"] and not tries:
            success = 0.8                                # they reached us on this link
        delay = st["rtt"] if st["rtt"] else 1.0 + 8000.0 / speed
        s = success * speed / (1.0 + delay)
        if st["streak"] >= self.FAIL_STREAK and self.now() - st["last_fail"] < self.REST_S:
            s *= 0.02                                    # fall back while this link is failing
        return s

    def choose(self, peer):
        if not self.links:
            return None
        return max(self.links, key=lambda l: (self.score(peer, l), self.links[l].get("speed") or 0))

    def summary(self, peer):
        best = self.choose(peer)
        out = []
        for l, info in sorted(self.links.items()):
            st = self._st(peer, l)
            out.append({"link": l, "name": info.get("name", l), "speed": info.get("speed"),
                        "sent": st["sent"], "ok": st["ok"], "lost": st["lost"],
                        "rtt": round(st["rtt"], 1) if st["rtt"] else None,
                        "tput": round(st["tput"]) if st["tput"] else None,
                        "failing": st["streak"] >= self.FAIL_STREAK, "best": l == best})
        return out


class ChatManager:
    """send_fn(addressee, info_field, route) transmits one APRS message.
    route: 'rf' (RF only), 'rf_is' (RF and APRS-IS) or 'is' (APRS-IS only)."""

    def __init__(self, mycall, send_fn, now=time.time):
        self.mycall = norm_call(mycall)
        self.send_fn = send_fn
        self.now = now
        self.lock = threading.RLock()
        self.peers = {}          # call -> dict(status, route, since, messages)
        self.pending = {}        # msgno -> outgoing message awaiting ack
        self.seen = {}           # (src, msgno) -> time, for duplicate suppression
        self.counter = 0
        self.seq = 0             # bumps on every change (face plate polling)
        self.events = []         # recent events for alerts: dict(seq, kind, call, text)
        self.on_log = None       # callback(direction, peer, text, via) for the comms log
        self.links = None        # LinkController when more than one radio path is set up
        self.retry = RETRY_S     # resend delays (longer for the slow HF robust mode)
        self.store_forward = True  # keep undelivered messages; resend when the station is heard again
        self.outbox = []         # undelivered messages waiting for the station to reappear
        self.relay_enabled = False # pass "RELAY <call> <text>" messages on for other stations
        self.roster = {}         # check-in net: call -> latest CHECKIN report
        self.held = []           # messages waiting their turn (one unanswered message per station)
        self.last_msg_t = {}     # call -> when the last chat message to it went out
        self.blocked = set()     # calls the operator has blocked (ignored completely)
        self.muted = {}          # call -> muted until (automatic, for hail flooding)
        self.probe_times = []
        self.relay_times = []
        self.relay_candidates = None  # callable(dest) -> recently heard Tradewind stations that can relay
        self.is_ok = None             # callable() -> True when APRS-IS can carry a message
        self.relay_wait = []          # RF-first: relayed messages waiting for "RELAY OK delivered"
        self.on_tw = None             # callable(call): a station spoke the Tradewind handshake
        self.on_qsy = None            # callable(mhz or None): retune the radio for a QSY / back to APRS
        self.qsy_delay = 6            # seconds to let the last ack go out before retuning
        self.qsy_active = None        # the peer we moved off the APRS frequency with
        self.qsy_channels = [f for f in QSY_DEFAULT.split(",")]   # the club's chat channels, in order
        self.qsy_busy = {}            # freq -> {"pair": (a, b), "t": time} heard taken by others on 144.390
        self.mail_counter = 0
        self.mail_out = {}            # our emails: id -> {to, n, acked, state, entry, call}
        self.mail_in = {}             # (from call, id) -> {parts, n, t, kind}: emails being assembled
        self.on_mail_out = None       # gateway: callable(src, to, subject, body) -> (ok, reason)
        self.mail_session_s = 600     # gateway: an email must arrive within this time of its first part
        self.mail_auto_qsy = True     # move off the APRS frequency before sending an email through a gateway
        self.on_stop = None           # callable(call): a station asked for no more messages (Wolf Mode)
        self.rf_block = None          # reason string while the radio sits on a satellite / ISS frequency
        self.stats = []               # v3.7 field test: one row per message - delivered or not, seconds, tries, route
        self.ack_hold_s = 8           # reply-acks: hold an ack this long hoping to ride in our reply (0 = off)

    # ---- helpers
    def _peer(self, call):
        call = norm_call(call)
        p = self.peers.get(call)
        if not p:
            p = self.peers[call] = {"call": call, "status": IDLE, "route": "rf",
                                    "since": self.now(), "messages": [], "via": "", "unread": 0}
        return p

    def _event(self, kind, call, text):
        self.seq += 1
        self.events.append({"seq": self.seq, "kind": kind, "call": call, "text": text, "t": self.now()})
        del self.events[:-30]

    def _set(self, p, status, note=None):
        p["status"], p["since"] = status, self.now()
        if note:
            self._log(p, "sys", note)
        self.seq += 1

    def _log(self, p, direction, text, **kw):
        m = {"dir": direction, "text": text, "t": self.now()}
        m.update(kw)
        if direction in ("in", "out") and self.on_log:
            try:
                self.on_log(direction, p["call"], text, kw.get("via") or kw.get("route", ""))
            except Exception:
                pass
        p["messages"].append(m)
        del p["messages"][:-200]
        self.seq += 1
        return m

    def _next_no(self):
        self.counter = self.counter % 999 + 1
        return str(self.counter)

    def _busy(self, call, kind="chat"):
        """True while a chat message to this station is unanswered, or the last went out < MSG_GAP_S ago.
        Email parts only wait for the previous part's ack (and for a QSY in progress)."""
        if kind == "mail":
            q = (self.peers.get(call) or {}).get("qsy") or {}
            return q.get("state") in ("offered", "accepting", "moving") or \
                any(o["call"] == call and o.get("kind") == "mail" for o in self.pending.values())
        if any((o["call"] == call and o.get("kind") in ("chat", "relay")) or
               (o.get("kind") == "relay_req" and o["smart"]["dest"] == call) for o in self.pending.values()):
            return True
        if any(w["dest"] == call for w in self.relay_wait):
            return True
        return self.now() - self.last_msg_t.get(call, -1e9) < MSG_GAP_S

    def _send(self, p, text, show=True, kind="chat", delays=None, forever=False, orig=None, entry=None,
              route=None, smart=None, released=False):
        """Send one numbered message (retried until acked).
        delays: seconds between tries (default RETRY_S); forever: keep the last delay until acked.
        Chat and relay messages wait their turn: one unanswered message per station at a time."""
        if entry is None:
            entry = self._log(p, "out", text, state="sending", kind=kind, route=p["route"]) if show else \
                {"state": "sending", "text": text}
        if kind in ("chat", "relay", "mail") and (self._busy(p["call"], kind) or
                                                  (not released and any(h["call"] == p["call"] for h in self.held))):
            entry["state"] = "waiting"
            self.held.append({"call": p["call"], "text": text, "kind": kind, "delays": delays,
                              "entry": entry, "orig": orig, "route": route, "smart": smart})
            self.seq += 1
            return entry
        if route is None and p["route"] == "smart" and smart is None:
            # RF first: a few tries direct on RF, then (chat only) up to two relays, then APRS-IS
            route, smart = "rf", {"dest": p["call"], "stage": "rf", "tried": [], "body": text,
                                  "relay": kind == "chat"}
            if not forever:
                delays = list(delays or self.retry)[:2]
        no = self._next_no()
        entry.update(state="sending", no=no)
        self.pending[no] = {"call": p["call"], "text": text, "route": route or p["route"], "tries": 0,
                            "next": 0, "entry": entry, "delays": list(delays or self.retry), "forever": forever,
                            "kind": kind, "orig": orig, "start": self.now(), "smart": smart}
        if kind in ("chat", "relay"):
            self.last_msg_t[p["call"]] = self.now()
        self._transmit(no)
        return entry

    def _release_held(self):
        for h in list(self.held):
            if h not in self.held or self._busy(h["call"], h["kind"]):
                continue
            if any(x["call"] == h["call"] for x in self.held[:self.held.index(h)]):
                continue                                  # keep each station's messages in order
            self.held.remove(h)
            if h["entry"].get("state") == "cancelled":
                continue
            self._send(self._peer(h["call"]), h["text"], kind=h["kind"], delays=h["delays"],
                       orig=h["orig"], entry=h["entry"], route=h.get("route"), smart=h.get("smart"), released=True)

    # ---- RF-first routing: direct RF -> relay through heard Tradewind stations -> APRS-IS
    def _smart_next(self, o):
        """The current step failed: move the message on to the next path. False = nothing left."""
        sm = o["smart"]
        dest, body = sm["dest"], sm["body"]
        entry = o.get("orig_entry") or o["entry"]
        p = self._peer(dest)
        room = MAX_TEXT - len(f"RELAY {dest} ") - len(f" [de {self.mycall} via XXXXXX-XX]")
        if sm["relay"] and len(body) <= room and len(sm["tried"]) < SMART_RELAYS and self.relay_candidates:
            try:
                cands = [norm_call(c) for c in self.relay_candidates(dest)]
            except Exception:
                cands = []
            cands = [c for c in cands if c not in sm["tried"] and c not in (dest, self.mycall)
                     and c not in self.blocked]
            if cands:
                r = cands[0]
                sm["tried"].append(r)
                sm["stage"] = "relay"
                self._log(p, "sys", f"No direct RF contact with {dest} - asking {r} to relay")
                entry.update(state="relaying", relay=r)
                no = self._next_no()
                self.pending[no] = {"call": r, "text": f"RELAY {dest} {body}", "route": "rf", "tries": 0,
                                    "next": 0, "entry": {"state": "sending", "text": body}, "start": self.now(),
                                    "delays": list(self.retry)[:2], "forever": False, "kind": "relay_req",
                                    "smart": sm, "orig_entry": entry}
                self._transmit(no)
                return True
        if sm["stage"] != "is" and self.is_ok and self.is_ok():
            sm["stage"] = "is"
            self._log(p, "sys", f"No RF path to {dest} - sending via APRS-IS")
            entry.update(state="sending", relay="")
            no = self._next_no()
            entry["no"] = no
            self.pending[no] = {"call": dest, "text": body, "route": "is", "tries": 0, "next": 0,
                                "entry": entry, "delays": list(self.retry), "forever": False,
                                "kind": o["kind"] if o["kind"] != "relay_req" else "chat",
                                "start": self.now(), "smart": sm}
            self._transmit(no)
            return True
        return False

    def _smart_relay_failed(self, w_or_o):
        o = w_or_o.get("o", w_or_o)
        if not self._smart_next(o):
            entry, sm = o.get("orig_entry") or o["entry"], o["smart"]
            if self.store_forward:
                entry["state"] = "queued"
                self._outbox_add({"call": sm["dest"], "text": sm["body"], "entry": entry, "t": self.now(),
                                  "kind": "chat"})
            else:
                entry["state"] = "failed"
        self.seq += 1

    def _stat(self, o, result, via):
        """Remember how a message went (for the field-test report) and show the delivery time on it."""
        if o.get("probe") or o.get("kind") not in ("chat", "mail", "relay", "relay_req", None):
            return
        e = o.get("entry") or {}
        secs = round(self.now() - e.get("t", self.now()), 1)
        if result == "delivered":
            e["secs"] = secs
        self.stats.append({"t": self.now(), "call": o["call"], "result": result, "secs": secs,
                           "tries": o.get("tries", 0), "route": str(o.get("route") or ""), "via": via or "",
                           "len": len(o.get("text") or ""), "kind": o.get("kind") or "chat"})
        del self.stats[:-500]

    def _outbox_add(self, o):
        """Store an undelivered message - at most OUTBOX_PER_STATION per station, OUTBOX_MAX in all."""
        if sum(1 for x in self.outbox if x["call"] == o["call"]) >= OUTBOX_PER_STATION:
            o["entry"]["state"] = "failed"
            return
        self.outbox.append(o)
        while len(self.outbox) > OUTBOX_MAX:
            self.outbox.pop(0)["entry"]["state"] = "failed"

    def _transmit(self, no):
        o = self.pending.get(no)
        if not o:
            return
        if o.get("link") and self.links:
            self.links.lost(o["call"], o["link"])       # the previous try was not acked
        try:
            q = self.peers.get(o["call"]) or {}
            ra = None
            if q.get("ra") and not o.get("probe"):
                hold = q.get("ack_hold")
                if hold and o["tries"] == 0:
                    o["ra_embed"] = hold["no"]       # their message's ack rides in this one
                    q["ack_hold"] = None
                ra = o.get("ra_embed") or ""
            link = self.send_fn(o["call"], format_message(o["call"], o["text"], no, ra), o["route"])
            o["entry"]["error"] = ""
            o["link"], o["t"] = (link if isinstance(link, str) else None), self.now()
            if o["link"]:
                o["entry"]["link"] = o["link"]
                if self.links:
                    self.links.sent(o["call"], o["link"])
        except Exception as e:
            o["entry"]["error"] = str(e)
            o["link"] = None
        o["tries"] += 1
        o["entry"]["tries"] = o["tries"]
        delays = o.get("delays") or RETRY_S
        delay = delays[o["tries"] - 1] if o["tries"] <= len(delays) else (delays[-1] if o.get("forever") else None)
        if o.get("forever") and self.now() - o.get("start", self.now()) >= HAIL_FOREVER_MAX_S:
            delay = None                                 # "keep calling" stops after 30 minutes
            p = self.peers.get(o["call"])
            if p:
                p["forever"] = False
                if p["status"] == HAILING:
                    self._set(p, EXPIRED, "No answer after 30 minutes - stopped calling")
        if delay is None and o.get("smart") and o["smart"]["stage"] != "is" and not o.get("forever"):
            self.pending.pop(no, None)
            self._smart_relay_failed(o)
            return
        if delay is None:
            self.pending.pop(no, None)
            if self.store_forward and o.get("kind") in ("chat", "relay", "mail") and not o.get("probe"):
                o["entry"]["state"] = "queued"          # store and forward: resend when they are heard
                self._outbox_add({"call": o["call"], "text": o["text"], "entry": o["entry"], "t": self.now(),
                                  "kind": o.get("kind"), "orig": o.get("orig")})
            else:
                o["entry"]["state"] = "failed"
            self._stat(o, "no ack", "")
            if o.get("probe"):
                self._log(self._peer(o["call"]), "sys", f"Probe on link {o['probe']}: no reply")
            elif o.get("link") and self.links:
                self.links.lost(o["call"], o["link"])
            self.seq += 1
        else:
            o["next"] = self.now() + delay

    def _flush_ack(self, p):
        h = p.get("ack_hold")
        if h:
            p["ack_hold"] = None
            self._ack(p["call"], h["no"], h["route"])

    def _ack(self, src, msgno, route):
        try:
            self.send_fn(src, format_message(src, f"ack{msgno}"), route)
        except Exception:
            pass

    # ---- user actions
    def hail(self, call, route="rf", tries=None, interval=None, forever=False):
        """Call a station. tries: how many times to send the call (default 4),
        interval: seconds between calls, forever: keep calling until it is received."""
        call = norm_call(call)
        if not valid_call(call) or call == self.mycall:
            raise ValueError("Enter the other station's callsign, e.g. 8P6XX-9")
        if self.rf_block and route != "is":
            raise ValueError(self.rf_block)
        with self.lock:
            if call in self.blocked:
                raise ValueError(f"{call} is on your block list - unblock them first.")
            p = self._peer(call)
            wait = p.get("declined_at", -1e9) + DECLINE_COOLDOWN_S - self.now()
            if wait > 0:
                raise ValueError(f"{call} declined recently - please wait {int(wait // 60) + 1} min before calling again.")
            p["route"] = route if (route in ("rf", "rf_is", "is", "auto", "smart") or str(route).startswith("link:")) else "rf"
            for o in [o for o in self.pending.values() if o["call"] == call and o["text"].startswith(T_REQ)]:
                o["entry"]["state"] = "cancelled"            # replace an earlier call to the same station
            self.pending = {n: o for n, o in self.pending.items() if o["entry"].get("state") != "cancelled"}
            tries = tries if isinstance(tries, int) else None
            tries = max(1, min(HAIL_MAX_TRIES, int(tries or 4)))
            iv = max(HAIL_MIN_GAP_S, int(interval or self.retry[0]))
            delays = [iv] * (tries - 1)
            if forever:
                delays = delays or [iv]
            p["forever"] = bool(forever)
            how = "for up to 30 minutes" if forever else f"{tries} times, {iv} s apart"
            self._set(p, HAILING, f"Calling {call} via {self.route_name(p['route'])}" + (f", {how}" if how else "") + "...")
            self._send(p, f"{T_REQ} {self.mycall} asks to chat. Reply ACCEPT or DECLINE", kind="hs",
                       delays=delays, forever=bool(forever))

    def accept(self, call):
        with self.lock:
            p = self._peer(call)
            if p["status"] == INCOMING:
                self._set(p, ACCEPTED, "You accepted - waiting for their confirmation")
                self._send(p, f"{T_OK} {self.mycall} accepted. Press Accept to confirm", kind="hs")
            elif p["status"] == AWAIT_CONFIRM:
                self._set(p, OPEN, "Link confirmed - chat open")
                self._send(p, f"{T_CONF} chat open de {self.mycall}", kind="hs")
                self._event("open", p["call"], f"Link open with {p['call']}")
            else:
                raise ValueError("There is no request to accept from this station.")

    def decline(self, call):
        with self.lock:
            p = self._peer(call)
            self._set(p, DECLINED, "You declined")
            self._send(p, f"{T_NO} {self.mycall} can't chat now. 73", kind="hs")

    def end(self, call):
        with self.lock:
            p = self._peer(call)
            self._flush_ack(p)
            for n in [n for n, o in self.pending.items() if o["call"] == p["call"]]:
                self.pending[n]["entry"]["state"] = "cancelled"
                del self.pending[n]
            for h in [h for h in self.held if h["call"] == p["call"]]:
                h["entry"]["state"] = "cancelled"
                self.held.remove(h)
            p["forever"] = False
            if p["status"] in (OPEN, HAILING, ACCEPTED, AWAIT_CONFIRM):
                self._send(p, f"{T_END} 73 de {self.mycall}", kind="hs")
            self._set(p, ENDED, "Link ended")
            if (p.get("qsy") or {}).get("state") in ("moving", "on"):
                p["qsy"].update(state="returning", tune_at=self.now() + self.qsy_delay)

    def send(self, call, text):
        with self.lock:
            p = self._peer(call)
            if p["status"] != OPEN and not p.get("direct"):
                raise ValueError("Open a link first: hail the station and wait for both sides to accept.")
            parts = split_text(text)
            if not parts:
                raise ValueError("Type a message first.")
            for part in parts:
                self._send(p, part)
            p["unread"] = 0
            if not p.get("qsy") and p["status"] == OPEN and not str(p["route"]).startswith(("is",)):
                p["sent_here"] = p.get("sent_here", 0) + len(parts)
                if p["sent_here"] == QSY_SUGGEST_AFTER and self.on_qsy and self.qsy_channels:
                    self._event("qsy_hint", p["call"], "Long chat - consider QSY to a chat frequency to keep APRS clear")

    def relay(self, relay_call, dest, text, route=None):
        """Ask another Tradewind station to pass a message on to a station you cannot reach."""
        relay_call, dest = norm_call(relay_call), norm_call(dest)
        if not (valid_call(relay_call) and valid_call(dest)):
            raise ValueError("Enter the relay station's callsign and the final destination.")
        with self.lock:
            p = self._peer(relay_call)
            p["direct"] = True
            if route:
                p["route"] = route
            room = MAX_TEXT - len(f"RELAY {dest} ")
            for part in split_text(text):
                for chunk in [part[i:i + room] for i in range(0, len(part), room)]:
                    self._send(p, f"RELAY {dest} {chunk}")

    # ---- QSY: move a long chat off the APRS frequency to a chat channel, then back
    def qsy(self, call, mhz):
        with self.lock:
            p = self._peer(call)
            if not self.on_qsy:
                raise ValueError("QSY is for amateur chats; a commercial channel stays where it is licensed.")
            if p["status"] != OPEN:
                raise ValueError("Open a chat link first, then QSY.")
            if str(mhz).strip().lower() in ("", "auto"):
                mhz = self.free_channel()
            if not qsy_freq_ok(mhz):
                raise ValueError("That frequency is not allowed for chat in your zone - pick one of the chat channels (MENU > RADIO / TNC).")
            if self.qsy_active and self.qsy_active != p["call"]:
                raise ValueError(f"Already moved with {self.qsy_active} - end that chat first.")
            f = f"{float(mhz):.3f}"
            p["qsy"] = {"freq": f, "state": "offered", "t": self.now()}
            self._log(p, "sys", f"Asking {p['call']} to move to {f} MHz...")
            self._send(p, f"{T_QSY} {f} - {self.mycall} asks to move this chat", kind="hs")

    # ---- email: through the EMAIL-2 service, or through a Tradewind gateway station
    def mail_send(self, to, subject, body, via=EMAIL2, third_party_ok=False):
        """Queue an email. via = 'EMAIL-2' or a gateway station's callsign. Returns the email id."""
        if self.rf_block:
            raise ValueError(self.rf_block)
        to, subject, body = (to or "").strip(), " ".join((subject or "").split())[:60], (body or "").strip()
        if not EMAIL_RE.match(to):
            raise ValueError("Enter a valid email address.")
        if not body:
            raise ValueError("Write the email first.")
        if len(body) > MAIL_MAX_CHARS:
            raise ValueError(f"Keep emails to {MAIL_MAX_CHARS} characters on radio.")
        if not (ham_address(to) or third_party_ok):
            raise ValueError("Email to non-hams (third-party traffic) is only allowed in a disaster session - "
                             "switch it on under EmComm, or send to a ham address such as CALL@winlink.org.")
        via = norm_call(via) or EMAIL2
        with self.lock:
            self.mail_counter = self.mail_counter % 99 + 1
            mid = str(self.mail_counter)
            p = self._peer(via)
            p["direct"] = True
            if via == EMAIL2:
                p["route"] = "is" if (self.is_ok and self.is_ok()) else "rf"
                line = (f"[{subject}] " if subject else "") + body
                parts = [f"{to} {c}" for c in _chunks(line, MAX_TEXT - len(to) - 1)]
            else:
                if p["route"] not in ("rf", "rf_is", "is", "smart") and not str(p["route"]).startswith("link:"):
                    p["route"] = "smart"
                payload = ["TO " + to] + (["SU " + subject] if subject else []) + ["BD " + c for c in _chunks(body, 50)]
                parts = [f"TWM {mid} {i + 1}/{len(payload)} {x}" for i, x in enumerate(payload)]
            if len(parts) > MAIL_MAX_PARTS:
                raise ValueError("That email is too long for radio - shorten it.")
            entry = self._log(p, "sys", f"Email {mid} to {to} ({len(parts)} part{'s' if len(parts) > 1 else ''}) - sending")
            self.mail_out[mid] = {"to": to, "n": len(parts), "acked": 0, "state": "sending", "entry": entry,
                                  "call": via, "via": via, "t": self.now()}
            if via != EMAIL2 and self.mail_auto_qsy and self.on_qsy and p["status"] == OPEN and not p.get("qsy"):
                try:
                    self.qsy(via, "auto")             # move off the APRS frequency first; parts wait for it
                except ValueError:
                    pass
            for part in parts:
                e = {"state": "sending", "text": part, "mail": mid}
                self._send(p, part, show=via == EMAIL2, kind="mail", entry=e if via != EMAIL2 else None)
                if via == EMAIL2:                     # shown in the EMAIL-2 thread; still count its acks
                    p["messages"][-1]["mail"] = mid
            self._event("mail_out", via, f"Email to {to} queued via {via}")
            return mid

    def mail_deliver(self, call, frm, subject, body):
        """Gateway: pass an incoming email to a station by radio (stored and resent if they are away)."""
        with self.lock:
            p = self._peer(call)
            p["direct"] = True
            if p["route"] == "rf":
                p["route"] = "smart"
            self.mail_counter = self.mail_counter % 99 + 1
            mid = str(self.mail_counter)
            payload = ["FR " + frm[:60]] + (["SU " + subject[:60]] if subject else []) + \
                ["BD " + c for c in _chunks(body[:MAIL_MAX_CHARS], 50)]
            payload = payload[:MAIL_MAX_PARTS]
            for i, x in enumerate(payload):
                self._send(p, f"TWI {mid} {i + 1}/{len(payload)} {x}", show=False, kind="mail")
            self._log(p, "sys", f"Gateway: passing an email from {frm} to {call} ({len(payload)} parts)")

    def _mail_part(self, p, text, via):
        """TWM (to the gateway) / TWI (from the gateway) / TWMR (gateway result). True when handled."""
        m = re.match(r"^(TWM|TWI|TWMR)\s+(\d{1,3})\s+(.*)$", text, re.S)
        if not m:
            return False
        tag, mid, rest = m.group(1), m.group(2), m.group(3)
        now = self.now()
        if tag == "TWMR":
            job = self.mail_out.get(mid)
            if job and job["call"] == p["call"]:
                ok = rest.upper().startswith("SENT")
                job["state"] = "sent" if ok else "failed: " + rest[7:].strip()
                job["entry"]["text"] = f"Email {mid} to {job['to']} - " + ("sent by the gateway" if ok else job["state"])
                self._event("mail_sent" if ok else "mail_failed", p["call"],
                            f"Email to {job['to']} " + ("sent" if ok else job["state"]))
                self.seq += 1
            return True
        pm = re.match(r"^(\d{1,2})/(\d{1,2})\s+(..)\s?(.*)$", rest, re.S)
        if not pm:
            return True
        i, n, key, val = int(pm.group(1)), int(pm.group(2)), pm.group(3).upper(), pm.group(4)
        slot = self.mail_in.setdefault((p["call"], tag, mid), {"parts": {}, "n": n, "t": now})
        slot["parts"][i] = (key, val)
        if len(slot["parts"]) < slot["n"]:
            return True
        del self.mail_in[(p["call"], tag, mid)]
        f = {"TO": "", "FR": "", "SU": "", "BD": []}
        for k in sorted(slot["parts"]):
            key, val = slot["parts"][k]
            if key == "BD":
                f["BD"].append(val)
            elif key in f:
                f[key] = val.strip()
        body = " ".join(f["BD"])
        if tag == "TWI":                               # an email for us, from the gateway
            self._log(p, "in", f"Email from {f['FR']}: " + (f"[{f['SU']}] " if f["SU"] else "") + body, via=via)
            p["unread"] += 1
            self._event("mail_in", p["call"], f"Email from {f['FR']}: {f['SU'] or body[:40]}")
            return True
        # TWM: we are the gateway
        if not self.on_mail_out:
            self._send(p, f"TWMR {mid} FAILED this station is not an email gateway", show=False, kind="sys")
            return True
        if now - slot["t"] > self.mail_session_s:
            ok, why = False, f"session time limit ({self.mail_session_s // 60} min)"
        else:
            try:
                ok, why = self.on_mail_out(p["call"], f["TO"], f["SU"], body)
            except Exception as e:
                ok, why = False, str(e)
        self._log(p, "sys", f"Gateway: email from {p['call']} to {f['TO']} - " + ("sent" if ok else f"refused ({why})"))
        self._send(p, (f"TWMR {mid} SENT" if ok else f"TWMR {mid} FAILED {why}")[:MAX_TEXT], show=False, kind="sys")
        return True

    # ---- quick messages and per-chat settings (v3.5)
    def open_direct(self, call, route="smart"):
        """Message a station without the hail handshake (plain APRS, works with any app)."""
        call = norm_call(call)
        if not valid_call(call) or call == self.mycall:
            raise ValueError("Choose another station's callsign.")
        with self.lock:
            p = self._peer(call)
            p["direct"] = True
            self.set_route(call, route)

    def set_route(self, call, route):
        with self.lock:
            p = self._peer(call)
            if route in ("rf", "is", "smart", "rf_is", "auto") or str(route).startswith("link:"):
                p["route"] = route
            self.seq += 1

    def set_nogate(self, call, on):
        with self.lock:
            self._peer(call)["nogate"] = bool(on)
            self.seq += 1

    def free_channel(self):
        """First chat channel nobody else is using (others' moves are heard on the APRS frequency)."""
        now = self.now()
        self.qsy_busy = {f: b for f, b in self.qsy_busy.items() if now - b["t"] < QSY_BUSY_S}
        mine = {(q.get("qsy") or {}).get("freq") for q in self.peers.values() if q.get("qsy")}
        if not self.qsy_channels:
            raise ValueError("No chat channels are set for your zone - add them under MENU > RADIO / TNC > Chat channels, "
                             "or keep the chat short on the APRS frequency.")
        for f in self.qsy_channels:
            f = f"{float(f):.3f}"
            if f not in self.qsy_busy and f not in mine:
                return f
        raise ValueError("All chat channels are busy - keep messages short on APRS, or try again shortly.")

    def overheard(self, m):
        """A message between OTHER stations: note chat channels they move to and leave."""
        up = (m.get("text") or "").upper()
        pair = tuple(sorted((m["src"], m["to"])))
        if up.startswith(T_QSY_OK):
            parts = up.split()
            if len(parts) > 1 and qsy_freq_ok(parts[1]):
                self.qsy_busy[f"{float(parts[1]):.3f}"] = {"pair": pair, "t": self.now()}
        elif up.startswith(T_QSY_BACK) or up.startswith(T_END):
            for f in [f for f, b in self.qsy_busy.items() if b["pair"] == pair]:
                del self.qsy_busy[f]

    def qsy_answer(self, call, yes):
        with self.lock:
            p = self._peer(call)
            q = p.get("qsy") or {}
            if q.get("state") != "incoming":
                raise ValueError("There is no frequency change to answer.")
            if yes:
                q["state"] = "accepting"
                self._log(p, "sys", f"Moving to {q['freq']} MHz once they have your answer...")
                self._send(p, f"{T_QSY_OK} {q['freq']}", kind="hs")
            else:
                p["qsy"] = None
                self._log(p, "sys", "You chose to stay on this frequency")
                self._send(p, f"{T_QSY_NO} staying here", kind="hs")
            self.seq += 1

    def qsy_back(self, call, tell=True):
        with self.lock:
            p = self._peer(call)
            q = p.get("qsy") or {}
            if q.get("state") not in ("moving", "on"):
                raise ValueError("This chat is already on the APRS frequency.")
            if tell:
                self._send(p, f"{T_QSY_BACK} returning to APRS", kind="hs")
            q.update(state="returning", tune_at=self.now() + self.qsy_delay)
            self._log(p, "sys", "Going back to the APRS frequency...")

    def _qsy_tune(self, p, mhz):
        try:
            if self.on_qsy:
                self.on_qsy(mhz)
        except Exception as e:
            self._log(p, "sys", f"Could not retune the radio: {e}")
        if mhz:
            self.qsy_active = p["call"]
            self._log(p, "sys", f"Now on {mhz} MHz - set the radio by hand if it has no CAT control")
        else:
            self.qsy_active = None
            p["qsy"] = None
            self._log(p, "sys", "Back on the APRS frequency")
        self.seq += 1

    def _qsy_heard(self, p):
        q = p.get("qsy") or {}
        if q.get("state") == "moving":
            q["state"] = "on"
            self._log(p, "sys", f"Contact on {q['freq']} MHz - chat continues here")
            self._event("qsy_on", p["call"], f"{p['call']}: chat moved to {q['freq']} MHz")

    def on_heard(self, call):
        """A station was heard again: resend anything stored for it."""
        call = norm_call(call)
        due = [o for o in self.outbox if o["call"] == call and self.now() - o["t"] > 20]
        if not due:
            return
        with self.lock:
            self.outbox = [o for o in self.outbox if o not in due]
            p = self._peer(call)
            self._log(p, "sys", f"{call} heard again - resending {len(due)} stored message{'s' if len(due) > 1 else ''}")
            for o in due:
                self._send(p, o["text"], kind=o.get("kind") or "chat", orig=o.get("orig"), entry=o["entry"])

    def probe(self, call):
        """Send a short test message on every radio link and time the replies."""
        if not self.links or not self.links.links:
            raise ValueError("Probing needs two radio links: set up the second link under MENU > RADIO / TNC.")
        with self.lock:
            now = self.now()
            self.probe_times = [t for t in self.probe_times if now - t < 3600]
            if len(self.probe_times) >= PROBES_PER_HOUR:
                wait = self.probe_times[0] + 3600 - now
                raise ValueError(f"Probe limit is {PROBES_PER_HOUR} an hour - try again in {int(wait // 60) + 1} min.")
            self.probe_times.append(now)
            p = self._peer(call)
            self._log(p, "sys", "Probing each radio link...")
            for l in sorted(self.links.links):
                no = self._next_no()
                entry = {"state": "sending", "no": no, "text": "probe"}
                self.pending[no] = {"call": p["call"], "text": f"TW-PROBE {l}", "route": f"link:{l}", "tries": 0,
                                    "next": 0, "entry": entry, "delays": [20], "forever": False, "probe": l}
                self._transmit(no)

    def mark_read(self, call):
        with self.lock:
            p = self.peers.get(norm_call(call))
            if p:
                p["unread"] = 0

    # ---- incoming packets (from RF or APRS-IS)
    def on_packet(self, line, via="RF"):
        m = parse_message(line)
        if m and m["to"] != self.mycall and m["src"] != self.mycall and m["text"]:
            try:
                self.overheard(m)
            except Exception:
                pass
        if not m or m["to"] != self.mycall or m["src"] == self.mycall:
            return False
        if m["src"] in self.blocked or self.muted.get(m["src"], 0) > self.now():
            return True                     # blocked / muted: ignored completely (not even acked)
        with self.lock:
            if m["ack"] or m["rej"]:
                return self._got_ack(m["src"], m["ack"] or m["rej"], bool(m["ack"]), via)
            p = self._peer(m["src"])
            p["via"] = via
            if m.get("ra"):
                p["ra"] = True                 # they understand reply-acks
            if m.get("replyack"):
                self._got_ack(m["src"], m["replyack"], True, via)
            if m["msgno"]:      # ack the way it arrived, so the ack can get back
                key = (m["src"], m["msgno"])
                now = self.now()
                for k in [k for k, t in self.seen.items() if now - t > 1800]:
                    del self.seen[k]
                dup = key in self.seen
                hold = (not dup and self.ack_hold_s and p.get("ra") and p["status"] == OPEN
                        and not m["text"].upper().startswith("TW-"))
                if hold:                       # wait a moment: the ack can ride in our reply
                    self._flush_ack(p)
                    p["ack_hold"] = {"no": m["msgno"], "route": route_from_via(via), "until": now + self.ack_hold_s}
                else:
                    self._ack(m["src"], m["msgno"], route_from_via(via))
                if self.links and via.upper().startswith("RF"):
                    self.links.heard(m["src"], via.split(":")[1] if ":" in via else "A")
                if dup:
                    return True        # a retry we already have
                self.seen[key] = now
            self._handle_text(p, m["text"].strip(), via)
            return True

    def _got_ack(self, src, no, ok, via):
        """An ack (or rej) for our message no, as its own packet or riding in a reply (reply-ack)."""
        o = self.pending.pop(no, None)
        if src in self.peers:
            self._qsy_heard(self.peers[src])
        if o and o["call"] == src:
            if self.links and o.get("link"):
                self.links.acked(o["call"], o["link"], self.now() - o.get("t", self.now()), len(o["text"]) + 30)
            if o.get("probe"):
                rtt = self.now() - o.get("t", self.now())
                info = (self.links.links.get(o["probe"], {}) if self.links else {})
                self._log(self._peer(src), "sys", f"Probe on link {o['probe']} ({info.get('name', '')}): "
                          f"reply in {rtt:.1f} s via {via}")
            o["entry"]["state"] = "delivered" if ok else "rejected"
            self._stat(o, "delivered" if ok else "rejected", via)
            if o.get("kind") == "mail" and o["entry"].get("mail") in self.mail_out:
                job = self.mail_out[o["entry"]["mail"]]
                job["acked"] += 1
                if job["acked"] >= job["n"] and job["state"] == "sending":
                    job["state"] = "with gateway" if job["via"] != EMAIL2 else "delivered to EMAIL-2"
                    job["entry"]["text"] = job["entry"]["text"].split(" - ")[0] + f" - {job['state']}"
                    self.seq += 1
            if o.get("kind") == "relay_req":
                if ok:            # the relay has it; now wait for "RELAY OK delivered"
                    o["orig_entry"]["state"] = "relayed"
                    self.relay_wait.append({"relay": o["call"], "dest": o["smart"]["dest"],
                                            "until": self.now() + SMART_RELAY_WAIT_S, "o": o})
                else:
                    self._smart_relay_failed(o)
                self.seq += 1
                return True
            if o.get("kind") == "relay" and o.get("orig") and ok:
                self._send(self._peer(o["orig"]), f"RELAY OK delivered to {o['call']}", show=False, kind="sys")
            o["entry"]["via"] = via
            self._peer(src)["via"] = via
            self._on_delivered(o)
            self.seq += 1
        return True

    def _on_delivered(self, o):
        p = self.peers.get(o["call"])
        if p and o["text"].startswith(T_QSY_OK) and (p.get("qsy") or {}).get("state") == "accepting":
            p["qsy"].update(state="moving", tune_at=self.now() + 2, check_until=self.now() + 2 + QSY_WAIT_S)
        if p and o["text"].startswith(T_QSY_BACK) and (p.get("qsy") or {}).get("state") == "returning":
            p["qsy"]["tune_at"] = self.now()
        if p and p["status"] == HAILING and o["text"].startswith(T_REQ):
            self._log(p, "sys", f"{p['call']} received the call - waiting for them to accept")

    def _handle_text(self, p, text, via):
        up = text.upper()
        if up.startswith("TW-PROBE"):
            return                                   # link test: the ack is the answer
        if up.startswith("CHECKIN"):
            r = self.roster.get(p["call"]) or {"call": p["call"], "parts": {}}
            m = re.match(r"CHECKIN\s+(\S+)\s+(\d+)/(\d+)\s*(.*)", text, re.I)
            if m:
                if r.get("dtg") != m.group(1):
                    r = {"call": p["call"], "parts": {}, "dtg": m.group(1)}
                r["parts"][m.group(2)] = m.group(4)
                r["text"] = " ".join(r["parts"][k] for k in sorted(r["parts"], key=int))
            else:
                r["text"] = text[7:].strip()
            r["t"] = self.now()
            st = (r["text"].split(".")[0] or "").strip()
            r["status"] = st if st in ("Safe", "Need help", "Emergency", "Evacuated") else r.get("status", "")
            self.roster[p["call"]] = r
            self.seq += 1
        if up.startswith("RELAY OK") or up.startswith("RELAY REFUSED"):
            ok = up.startswith("RELAY OK")
            for w in [w for w in self.relay_wait if w["relay"] == p["call"] and w["dest"] in up]:
                self.relay_wait.remove(w)
                e = w["o"]["orig_entry"]
                if ok:
                    e.update(state="delivered", via=f"relay {p['call']}")
                    self._log(self._peer(w["dest"]), "sys", f"Delivered to {w['dest']} through {p['call']}")
                else:
                    self._smart_relay_failed(w)
            if not ok:
                for n, o in [(n, o) for n, o in self.pending.items()
                             if o.get("kind") == "relay_req" and o["call"] == p["call"] and o["smart"]["dest"] in up]:
                    self.pending.pop(n)
                    self._smart_relay_failed(o)
            self._log(p, "sys", text)
            return
        if up.startswith("RELAY ") and self.relay_enabled:
            parts = text.split(None, 2)
            if len(parts) == 3 and valid_call(parts[1]):
                dest, body = norm_call(parts[1]), parts[2]
                now = self.now()
                self.relay_times = [t for t in self.relay_times if now - t < 3600]
                why = ("it was already relayed once" if RELAY_TAG_RE.search(body)
                       else "it would loop back" if dest in (p["call"], self.mycall)
                       else f"limit of {RELAYS_PER_HOUR} relays an hour reached" if len(self.relay_times) >= RELAYS_PER_HOUR
                       else "")
                if why:
                    self._log(p, "sys", f"Relay to {dest} refused - {why}")
                    self._send(p, f"RELAY REFUSED to {dest}: {why}"[:MAX_TEXT], show=False, kind="sys")
                    return
                self.relay_times.append(now)
                q = self._peer(dest)
                if q["route"] == "auto" and not self.links:
                    q["route"] = "rf"
                self._log(p, "sys", f"Relaying a message from {p['call']} to {dest}")
                for part in split_text(f"{body} [de {p['call']} via {self.mycall}]"):
                    self._send(q, part, kind="relay", orig=p["call"])
                self._event("relay", p["call"], f"Relaying {p['call']} -> {dest}")
                return
        self._qsy_heard(p)
        if up.startswith(("TWM ", "TWI ", "TWMR ")) and self._mail_part(p, text, via):
            p["ra"] = True
            return
        if up.startswith("TW-"):
            p["ra"] = True                     # another Tradewind station: reply-acks are understood
        if up.startswith("TW-QSY"):
            parts = text.split()
            q = p.get("qsy") or {}
            if up.startswith(T_QSY_OK):
                if q.get("state") == "offered":
                    q.update(state="moving", tune_at=self.now() + self.qsy_delay,
                             check_until=self.now() + self.qsy_delay + QSY_WAIT_S, initiator=True)
                    self._log(p, "sys", f"{p['call']} agreed - moving to {q['freq']} MHz")
            elif up.startswith(T_QSY_NO):
                p["qsy"] = None
                self._log(p, "sys", f"{p['call']} prefers to stay on this frequency")
            elif up.startswith(T_QSY_BACK):
                if q.get("state") in ("moving", "on"):
                    q.update(state="returning", tune_at=self.now() + self.qsy_delay)
                    self._log(p, "sys", f"{p['call']} is going back to the APRS frequency - you follow")
            elif up.startswith(T_QSY_CHK):
                pass                                     # the ack is the answer; contact noted above
            elif len(parts) > 1 and p["status"] == OPEN and qsy_freq_ok(parts[1]) and not self.qsy_active:
                p["qsy"] = {"freq": f"{float(parts[1]):.3f}", "state": "incoming", "t": self.now()}
                self._set(p, OPEN, f"{p['call']} asks to move this chat to {p['qsy']['freq']} MHz")
                self._event("qsy", p["call"], f"{p['call']} asks to move the chat to {p['qsy']['freq']} MHz - Move or Stay")
            if self.on_tw:
                try:
                    self.on_tw(p["call"])
                except Exception:
                    pass
            return
        if up.startswith("TW-") and self.on_tw:
            try:
                self.on_tw(p["call"])
            except Exception:
                pass
        word = up.strip(" .!").split(" ")[0] if up else ""
        if up.startswith(T_REQ):
            now = self.now()
            p["req_times"] = [t for t in p.get("req_times", []) if now - t < MUTE_WINDOW_S] + [now]
            if len(p["req_times"]) > MUTE_HAILS:
                self.muted[p["call"]] = now + MUTE_S
                p["req_times"] = []
                self._set(p, IDLE, f"{p['call']} called more than {MUTE_HAILS} times in 10 minutes - muted for 1 hour")
                self._event("muted", p["call"], f"{p['call']} muted for 1 hour (too many calls)")
                return
            if p["status"] != OPEN:
                p["route"] = route_from_via(via)
                self._set(p, INCOMING, f"{p['call']} wants to chat (heard via {via})")
                self._event("request", p["call"], f"{p['call']} wants to chat - Accept or Decline")
            return
        if up.startswith(T_CONF):
            if p["status"] in (ACCEPTED, AWAIT_CONFIRM, OPEN):
                self._set(p, OPEN, "Link confirmed - chat open")
                self._event("open", p["call"], f"Link open with {p['call']}")
            return
        if up.startswith(T_OK) or (p["status"] == HAILING and word in ("ACCEPT", "ACCEPTED", "YES", "OK", "QSL")):
            if p["status"] == HAILING:
                self._set(p, AWAIT_CONFIRM, f"{p['call']} accepted - press Accept to confirm")
                self._event("accepted", p["call"], f"{p['call']} accepted your request - press Accept")
            return
        if up.startswith(T_NO) or (p["status"] == HAILING and word in ("DECLINE", "DECLINED", "NO")):
            p["declined_at"] = self.now()
            p["forever"] = False
            for n in [n for n, o in self.pending.items() if o["call"] == p["call"] and o["text"].startswith(T_REQ)]:
                self.pending.pop(n)["entry"]["state"] = "cancelled"     # stop calling them
            self._set(p, DECLINED, f"{p['call']} declined - you can call again in 15 minutes")
            self._event("declined", p["call"], f"{p['call']} declined")
            return
        if up.startswith(T_END):
            if (p.get("qsy") or {}).get("state") in ("moving", "on"):
                p["qsy"].update(state="returning", tune_at=self.now() + self.qsy_delay)
            self._set(p, ENDED, f"{p['call']} ended the link")
            self._event("ended", p["call"], f"{p['call']} ended the link")
            return
        if up.strip(" .!") in ("STOP", "UNSUBSCRIBE") and self.on_stop:
            try:
                self.on_stop(p["call"])
            except Exception:
                pass
        self._log(p, "in", text, via=via)
        if p["status"] != OPEN:
            p["direct"] = True      # a plain APRS message (e.g. from APRSdroid): allow replies
            if p["status"] == IDLE:
                p["route"] = route_from_via(via)
        p["unread"] += 1
        self._event("message", p["call"], f"{p['call']}: {text}")

    # ---- timers (call about once a second)
    def tick(self):
        now = self.now()
        with self.lock:
            for o in [o for o in self.outbox if now - o["t"] > 86400]:
                o["entry"]["state"] = "failed"
                self.outbox.remove(o)
            for no, o in list(self.pending.items()):
                if now >= o["next"]:
                    self._transmit(no)
            for p in list(self.peers.values()):
                if p.get("ack_hold") and now >= p["ack_hold"]["until"]:
                    self._flush_ack(p)             # no reply in time: send the ack on its own
                q = p.get("qsy")
                if not q:
                    continue
                if q.get("tune_at") and now >= q["tune_at"]:
                    q["tune_at"] = None
                    if q["state"] == "returning":
                        self._qsy_tune(p, None)
                        continue
                    self._qsy_tune(p, q["freq"])
                    if q.get("initiator"):          # say hello on the new frequency
                        self._send(p, f"{T_QSY_CHK} {q['freq']}", kind="hs", delays=[20, 20, 20])
                elif q["state"] == "moving" and now >= q.get("check_until", now + 1):
                    self._log(p, "sys", f"No contact on {q['freq']} MHz - back to the APRS frequency")
                    self._event("qsy_fail", p["call"], f"No contact with {p['call']} on {q['freq']} MHz - back on APRS")
                    self._qsy_tune(p, None)
                elif q["state"] in ("offered", "incoming") and now - q["t"] > 300:
                    p["qsy"] = None                    # an unanswered offer lapses after 5 minutes
            for k in [k for k, v in self.mail_in.items() if now - v["t"] > 1800]:
                del self.mail_in[k]                    # an email that never completed
            for w in [w for w in self.relay_wait if now >= w["until"]]:
                self.relay_wait.remove(w)
                self._log(self._peer(w["dest"]), "sys", f"{w['relay']} did not confirm delivery to {w['dest']}")
                self._smart_relay_failed(w)
            self._release_held()
            for c in [c for c, t in self.muted.items() if t <= now]:
                del self.muted[c]
            for p in self.peers.values():
                if p["status"] == HAILING and p.get("forever"):
                    continue                                   # "keep calling" stops itself after 30 min
                if p["status"] in (HAILING, AWAIT_CONFIRM, INCOMING, ACCEPTED) and now - p["since"] > LINK_TIMEOUT_S:
                    self._set(p, EXPIRED, "No answer - link request expired")

    # ---- save / restore (chat survives a restart or power cut)
    def to_dict(self):
        with self.lock:
            return {"v": 1, "mycall": self.mycall, "counter": self.counter, "roster": self.roster,
                    "peers": {c: {k: v for k, v in p.items()} for c, p in self.peers.items()}}

    def load(self, d):
        if not d or norm_call(d.get("mycall")) != self.mycall:
            return
        with self.lock:
            self.counter = int(d.get("counter") or 0)
            self.roster = d.get("roster") or {}
            for c, p in (d.get("peers") or {}).items():
                for m in p.get("messages", []):
                    if m.get("state") in ("sending", "waiting"):
                        m["state"] = "failed"          # unsent when Tradewind stopped
                    if m.get("state") == "queued" and m.get("dir") == "out":
                        self._outbox_add({"call": norm_call(c), "text": m["text"], "entry": m,
                                          "t": m.get("t", self.now()), "kind": "chat"})
                if p.get("status") in (HAILING, ACCEPTED, AWAIT_CONFIRM, INCOMING):
                    p["status"] = EXPIRED
                p.setdefault("unread", 0)
                p["qsy"] = None
                p["ack_hold"] = None
                p.setdefault("via", "")
                self.peers[norm_call(c)] = p
            self.seq += 1

    # ---- face plate
    @staticmethod
    def route_name(r):
        if str(r).startswith("link:"):
            return "radio link " + r[5:]
        return {"rf": "RF only", "rf_is": "RF + APRS-IS", "is": "APRS-IS only", "auto": "Auto (best radio link)",
                "smart": "RF first, then relay, then APRS-IS"}.get(r, r)

    def state(self):
        with self.lock:
            peers = sorted(self.peers.values(), key=lambda p: -max([p["since"]] + [m["t"] for m in p["messages"]]))
            return {"seq": self.seq, "mycall": self.mycall, "events": list(self.events),
                    "outbox": len(self.outbox), "relay": self.relay_enabled, "held": len(self.held),
                    "mail": [{"id": k, "to": j["to"], "via": j["via"], "state": j["state"], "t": j["t"]}
                             for k, j in sorted(self.mail_out.items(), key=lambda kv: -kv[1]["t"])][:20],
                    "qsy_busy": sorted(f for f, b in self.qsy_busy.items() if self.now() - b["t"] < QSY_BUSY_S),
                    "blocked": sorted(self.blocked),
                    "muted": sorted(c for c, t in self.muted.items() if t > self.now()),
                    "roster": sorted(({k: v for k, v in r.items() if k != "parts"} for r in self.roster.values()),
                                     key=lambda r: -r.get("t", 0)),
                    "peers": [{"call": p["call"], "status": p["status"], "status_text": STATUS_TEXT[p["status"]],
                               "route": p["route"], "via": p["via"], "unread": p["unread"],
                               "can_send": p["status"] == OPEN or bool(p.get("direct")),
                               "forever": bool(p.get("forever")),
                               "nogate": bool(p.get("nogate")),
                               "last": (p["messages"][-1]["text"][:60] if p["messages"] else ""),
                               "last_t": (p["messages"][-1]["t"] if p["messages"] else p["since"]),
                               "qsy": {k: v for k, v in (p.get("qsy") or {}).items() if k in ("freq", "state")} or None,
                               "links": self.links.summary(p["call"]) if self.links else [],
                               "messages": [{k: v for k, v in m.items() if k != "kind"} for m in p["messages"][-100:]]}
                              for p in peers]}


# --------------------------------------------------------------------------
# Commercial / EmComm mode: encrypted chat on licensed 148-174 MHz channels
# --------------------------------------------------------------------------
# NOT for amateur bands - amateur rules forbid encryption. The station
# identifier travels in clear in the AX.25 header and in periodic ID frames;
# only the message content is encrypted.
#
# Cipher: AES-256-GCM (authenticated). Key: 256 bits derived from the
# network passphrase with scrypt (salt = network name). Each frame carries a
# fresh 96-bit nonce; the sender's ID is bound in as associated data, and a
# timestamp + nonce cache stops replays.

COM_BAND_HZ = (148_000_000, 174_000_000)
SEC_DEST = "TWSEC"          # AX.25 destination of encrypted frames
SEC_TAG = "}TWE1:"          # info-field prefix (version 1)
SEC_MAX_AGE_S = 600         # frames older / newer than this are refused


def crypto_available():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        return ""
    except Exception as e:  # not installed (e.g. the Android build)
        return f"encryption library missing ({e.__class__.__name__}) - commercial mode needs the Windows/PC version"


def derive_key(net, passphrase):
    """256-bit network key from the passphrase (scrypt, salt = network name)."""
    import hashlib
    net, passphrase = (net or "").strip(), passphrase or ""
    if len(passphrase) < 12:
        raise ValueError("Use a passphrase of at least 12 characters (longer is better).")
    if not net:
        raise ValueError("Give the network a name (every unit must use the same name).")
    return hashlib.scrypt(passphrase.encode(), salt=b"tradewind-net-v1:" + net.upper().encode(),
                          n=2 ** 15, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)


def key_fingerprint(key):
    """Short code that units compare by voice to confirm they hold the same key."""
    import hashlib
    h = hashlib.sha256(b"tradewind-fp" + key).hexdigest().upper()
    return "-".join(h[i:i + 4] for i in range(0, 16, 4))


def com_freq_hz(cfg):
    try:
        hz = int(round(float(str(cfg.get("com_freq") or "").replace(",", ".")) * 1e6))
    except ValueError:
        return None
    return hz if COM_BAND_HZ[0] <= hz <= COM_BAND_HZ[1] else None


class SecureLink:
    def __init__(self, key, now=time.time):
        if len(key) != 32:
            raise ValueError("network key must be 256 bits")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self.aead = AESGCM(key)
        self.now = now
        self.seen = {}          # nonce -> time (replay cache)

    def seal(self, src, info):
        import base64
        nonce = os.urandom(12)
        plain = f"{int(self.now())}|{info}".encode("utf-8")
        ct = self.aead.encrypt(nonce, plain, src.upper().encode())
        return SEC_TAG + base64.b64encode(nonce + ct).decode()

    def open(self, src, info):
        """Encrypted info field -> plaintext, or None if forged, wrong key or replayed."""
        import base64
        if not info.startswith(SEC_TAG):
            return None
        try:
            raw = base64.b64decode(info[len(SEC_TAG):], validate=True)
            nonce, ct = raw[:12], raw[12:]
            plain = self.aead.decrypt(nonce, ct, src.upper().encode()).decode("utf-8")
            ts, _, body = plain.partition("|")
            ts = int(ts)
        except Exception:
            return None
        now = self.now()
        if abs(now - ts) > SEC_MAX_AGE_S or nonce in self.seen:
            return None
        for n in [n for n, t in self.seen.items() if now - t > 2 * SEC_MAX_AGE_S]:
            del self.seen[n]
        self.seen[nonce] = now
        return body


def seal_close(key, data: bytes) -> bytes:
    """Encrypt data at rest (AES-256-GCM) - used for saved commercial chats."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    return b"TWS1" + nonce + AESGCM(key).encrypt(nonce, data, b"tradewind-store")


def seal_open(key, blob: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not blob.startswith(b"TWS1"):
        raise ValueError("not an encrypted Tradewind store")
    return AESGCM(key).decrypt(blob[4:16], blob[16:], b"tradewind-store")
