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

import os
import queue
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
    ("144.390", "144.390 (N. America / Caribbean)"),
    ("144.800", "144.800 (Europe / Africa)"),
    ("144.930", "144.930"),
    ("145.570", "145.570"),
    ("145.825", "145.825 (ISS digipeater)"),
]


def aprs_freq_hz(cfg):
    """The chosen APRS frequency in Hz (custom value allowed), or None if invalid."""
    v = str(cfg.get("aprs_freq") or "144.390")
    if v == "custom":
        v = str(cfg.get("aprs_freq_custom") or "")
    try:
        mhz = float(v.replace(",", "."))
    except ValueError:
        return None
    if not (144.0 <= mhz <= 148.0 or 420.0 <= mhz <= 450.0):
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

    def _heard(self, frame_no_fcs):
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
                "tx_active": self.tx_active, "last_rx": self.last_rx, "error": self.error}


class NullTNC(BaseTNC):
    kind = "APRS-IS only"

    def send(self, src, dst, path, info):
        pass


class SoundModemTNC(BaseTNC):
    kind = "Sound modem"

    def __init__(self, in_dev=None, out_dev=None, rate=48000, ptt=None,
                 txdelay=300, txtail=50, level=0.5, busy_hold=1.5,
                 tune_hz=None, cat_restore=True):
        super().__init__()
        self.in_dev, self.out_dev, self.rate = in_dev, out_dev, int(rate)
        self.ptt = ptt or PTT()
        self.txdelay, self.txtail, self.level = txdelay, txtail, level
        self.busy_hold = busy_hold
        self.audio_level = 0.0
        self.txq = queue.Queue()
        self.demod = afsk.Demodulator(self.rate)
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
                    if self.tx_active:
                        continue  # don't decode our own transmission
                    for fr in self.demod.process(chunk):
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
            # simple channel-busy check: wait until nothing heard for busy_hold s
            t0 = time.time()
            while time.time() - self.last_rx < self.busy_hold and time.time() - t0 < 10:
                time.sleep(0.2)
            audio = afsk.modulate(frame, self.rate, self.txdelay, self.txtail, self.level)
            try:
                self.tx_active = True
                self.ptt.key(True)
                sd.play(audio, self.rate, device=self.out_dev, blocking=True)
                self.tx_count += 1
            except Exception as e:
                self.error = f"transmit: {e}"
            finally:
                try:
                    self.ptt.key(False)
                finally:
                    self.tx_active = False
            time.sleep(0.5)

    def send(self, src, dst, path, info):
        self.txq.put(afsk.encode_ax25(src, dst, path, info))

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
        d.update(audio_level=round(self.audio_level, 4), ptt=self.ptt.name,
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
        self.tx_active = True
        try:
            self._write(kiss_wrap(frame))
            self.tx_count += 1
        finally:
            self.tx_active = False


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
                             tune_hz=aprs_freq_hz(cfg) if cfg.get("cat_tune") else None,
                             cat_restore=cfg.get("cat_restore", True))
    if mode == "kiss_tcp":
        host, _, port = (cfg.get("kiss_addr") or "127.0.0.1:8001").partition(":")
        return KissTcpTNC(host, port or 8001)
    if mode == "kiss_serial":
        return KissSerialTNC(cfg.get("kiss_serial", ""), cfg.get("kiss_baud", 9600))
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
