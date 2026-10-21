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
                 tune_hz=None, cat_restore=True, modem="1200"):
        super().__init__()
        self.modem = modem if (modem in afsk.PROFILES or modem == "robust") else "1200"
        self.in_dev, self.out_dev, self.rate = in_dev, out_dev, int(rate)
        self.ptt = ptt or PTT()
        self.txdelay, self.txtail, self.level = txdelay, txtail, level
        self.busy_hold = busy_hold
        self.audio_level = 0.0
        self.txq = queue.Queue()
        self.demod = afsk.RobustDemodulator(self.rate) if self.modem == "robust" else afsk.Demodulator(self.rate, self.modem)
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
            # simple channel-busy check: wait until nothing heard for busy_hold s
            t0 = time.time()
            while time.time() - self.last_rx < self.busy_hold and time.time() - t0 < 10:
                time.sleep(0.2)
            if self.modem == "robust":
                np = afsk.np
                audio = np.concatenate([np.zeros(int(self.rate * self.txdelay / 1000), dtype=np.float32),
                                        afsk.robust_modulate(frame, self.rate, self.level, lead_ms=50)])
            else:
                audio = afsk.modulate(frame, self.rate, self.txdelay, self.txtail, self.level, self.modem)
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
        if self.modem == "robust":         # robust frames carry the text line itself (no digipeating on HF)
            self.txq.put(f"{src}>{dst}:{info}".encode("latin-1", "replace"))
        else:
            self.txq.put(afsk.encode_ax25(src, dst, path, info))

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
                             tune_hz=(com_freq_hz(cfg) if cfg.get("op_mode") == "commercial" else aprs_freq_hz(cfg))
                             if cfg.get("cat_tune") else None,
                             cat_restore=cfg.get("cat_restore", True),
                             modem=str(cfg.get("modem") or "1200"))
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
CHAT_CALL_RE = re.compile(r"^[A-Z0-9]{1,6}(-([0-9]|1[0-5]))?$")

T_REQ, T_OK, T_CONF, T_NO, T_END = "TW-LINK?", "TW-LINK OK", "TW-LINK CONFIRMED", "TW-LINK NO", "TW-LINK END"

# link states
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


def format_message(addressee, text, msgno=None):
    """APRS message info field: ':ADDRESSEE:text{NN'"""
    t = text.replace("|", "/").replace("~", "-").replace("{", "(")[:MAX_TEXT]
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
    msgno = None
    if "{" in body:
        body, _, tail = body.rpartition("{")
        msgno = tail.split("}")[0][:5] or None
    return {"src": src, "to": to, "text": body, "msgno": msgno, "ack": None, "rej": None, "path": path}


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

    def _send(self, p, text, show=True, kind="chat", delays=None, forever=False):
        """Send one numbered message (retried until acked).
        delays: seconds between tries (default RETRY_S); forever: keep the last delay until acked."""
        no = self._next_no()
        entry = self._log(p, "out", text, state="sending", no=no, kind=kind, route=p["route"]) if show else \
            {"state": "sending", "no": no, "text": text}
        self.pending[no] = {"call": p["call"], "text": text, "route": p["route"], "tries": 0,
                            "next": 0, "entry": entry, "delays": list(delays or self.retry), "forever": forever,
                            "kind": kind}
        self._transmit(no)
        return entry

    def _transmit(self, no):
        o = self.pending.get(no)
        if not o:
            return
        if o.get("link") and self.links:
            self.links.lost(o["call"], o["link"])       # the previous try was not acked
        try:
            link = self.send_fn(o["call"], format_message(o["call"], o["text"], no), o["route"])
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
        if delay is None:
            self.pending.pop(no, None)
            if self.store_forward and o.get("kind") in ("chat", "relay") and not o.get("probe"):
                o["entry"]["state"] = "queued"          # store and forward: resend when they are heard
                self.outbox.append({"call": o["call"], "text": o["text"], "entry": o["entry"], "t": self.now(),
                                    "kind": o.get("kind"), "orig": o.get("orig")})
                del self.outbox[:-200]
            else:
                o["entry"]["state"] = "failed"
            if o.get("probe"):
                self._log(self._peer(o["call"]), "sys", f"Probe on link {o['probe']}: no reply")
            elif o.get("link") and self.links:
                self.links.lost(o["call"], o["link"])
            self.seq += 1
        else:
            o["next"] = self.now() + delay

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
        with self.lock:
            p = self._peer(call)
            p["route"] = route if (route in ("rf", "rf_is", "is", "auto") or str(route).startswith("link:")) else "rf"
            for o in [o for o in self.pending.values() if o["call"] == call and o["text"].startswith(T_REQ)]:
                o["entry"]["state"] = "cancelled"            # replace an earlier call to the same station
            self.pending = {n: o for n, o in self.pending.items() if o["entry"].get("state") != "cancelled"}
            delays = None
            tries = tries if isinstance(tries, int) else None
            if interval or tries or forever:
                iv = max(15, int(interval or 60))
                delays = [iv] * max(0, int(tries or 4) - 1)
                if forever:
                    delays = delays or [iv]
            p["forever"] = bool(forever)
            how = "until it is received" if forever else f"{int(tries or 4)} times" if delays is not None else ""
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
            for n in [n for n, o in self.pending.items() if o["call"] == p["call"]]:
                self.pending[n]["entry"]["state"] = "cancelled"
                del self.pending[n]
            p["forever"] = False
            if p["status"] in (OPEN, HAILING, ACCEPTED, AWAIT_CONFIRM):
                self._send(p, f"{T_END} 73 de {self.mycall}", kind="hs")
            self._set(p, ENDED, "Link ended")

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
                no = self._next_no()
                o["entry"].update(state="sending", no=no)
                self.pending[no] = {"call": call, "text": o["text"], "route": p["route"], "tries": 0, "next": 0,
                                    "entry": o["entry"], "delays": list(self.retry), "forever": False,
                                    "kind": o.get("kind") or "chat", "orig": o.get("orig")}
                self._transmit(no)

    def probe(self, call):
        """Send a short test message on every radio link and time the replies."""
        if not self.links or not self.links.links:
            raise ValueError("Probing needs two radio links: set up the second link under MENU > RADIO / TNC.")
        with self.lock:
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
        if not m or m["to"] != self.mycall or m["src"] == self.mycall:
            return False
        with self.lock:
            if m["ack"] or m["rej"]:
                no = m["ack"] or m["rej"]
                o = self.pending.pop(no, None)
                if o and o["call"] == m["src"]:
                    if self.links and o.get("link"):
                        self.links.acked(o["call"], o["link"], self.now() - o.get("t", self.now()), len(o["text"]) + 30)
                    if o.get("probe"):
                        rtt = self.now() - o.get("t", self.now())
                        info = (self.links.links.get(o["probe"], {}) if self.links else {})
                        self._log(self._peer(m["src"]), "sys", f"Probe on link {o['probe']} ({info.get('name', '')}): "
                                  f"reply in {rtt:.1f} s via {via}")
                    o["entry"]["state"] = "delivered" if m["ack"] else "rejected"
                    if o.get("kind") == "relay" and o.get("orig") and m["ack"]:
                        self._send(self._peer(o["orig"]), f"RELAY OK delivered to {o['call']}", show=False, kind="sys")
                    o["entry"]["via"] = via
                    self._peer(m["src"])["via"] = via
                    self._on_delivered(o)
                    self.seq += 1
                return True
            p = self._peer(m["src"])
            p["via"] = via
            if m["msgno"]:      # ack the way it arrived, so the ack can get back
                self._ack(m["src"], m["msgno"], route_from_via(via))
                if self.links and via.upper().startswith("RF"):
                    self.links.heard(m["src"], via.split(":")[1] if ":" in via else "A")
                key = (m["src"], m["msgno"])
                now = self.now()
                for k in [k for k, t in self.seen.items() if now - t > 1800]:
                    del self.seen[k]
                if key in self.seen:
                    return True        # a retry we already have
                self.seen[key] = now
            self._handle_text(p, m["text"].strip(), via)
            return True

    def _on_delivered(self, o):
        p = self.peers.get(o["call"])
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
        if up.startswith("RELAY OK"):
            self._log(p, "sys", text)
            return
        if up.startswith("RELAY ") and self.relay_enabled:
            parts = text.split(None, 2)
            if len(parts) == 3 and valid_call(parts[1]):
                dest, body = norm_call(parts[1]), parts[2]
                q = self._peer(dest)
                if q["route"] == "auto" and not self.links:
                    q["route"] = "rf"
                self._log(p, "sys", f"Relaying a message from {p['call']} to {dest}")
                for part in split_text(f"{body} [de {p['call']} via {self.mycall}]"):
                    e = self._send(q, part, kind="relay")
                    self.pending[e["no"]]["orig"] = p["call"]
                self._event("relay", p["call"], f"Relaying {p['call']} -> {dest}")
                return
        word = up.strip(" .!").split(" ")[0] if up else ""
        if up.startswith(T_REQ):
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
            self._set(p, DECLINED, f"{p['call']} declined")
            self._event("declined", p["call"], f"{p['call']} declined")
            return
        if up.startswith(T_END):
            self._set(p, ENDED, f"{p['call']} ended the link")
            self._event("ended", p["call"], f"{p['call']} ended the link")
            return
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
            for p in self.peers.values():
                if p["status"] == HAILING and p.get("forever"):
                    continue                                   # "keep calling" never expires
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
                    if m.get("state") == "sending":
                        m["state"] = "failed"          # unsent when Tradewind stopped
                    if m.get("state") == "queued" and m.get("dir") == "out":
                        self.outbox.append({"call": norm_call(c), "text": m["text"], "entry": m,
                                            "t": m.get("t", self.now()), "kind": "chat"})
                if p.get("status") in (HAILING, ACCEPTED, AWAIT_CONFIRM, INCOMING):
                    p["status"] = EXPIRED
                p.setdefault("unread", 0)
                p.setdefault("via", "")
                self.peers[norm_call(c)] = p
            self.seq += 1

    # ---- face plate
    @staticmethod
    def route_name(r):
        if str(r).startswith("link:"):
            return "radio link " + r[5:]
        return {"rf": "RF only", "rf_is": "RF + APRS-IS", "is": "APRS-IS only", "auto": "Auto (best radio link)"}.get(r, r)

    def state(self):
        with self.lock:
            peers = sorted(self.peers.values(), key=lambda p: -max([p["since"]] + [m["t"] for m in p["messages"]]))
            return {"seq": self.seq, "mycall": self.mycall, "events": list(self.events),
                    "outbox": len(self.outbox), "relay": self.relay_enabled,
                    "roster": sorted(({k: v for k, v in r.items() if k != "parts"} for r in self.roster.values()),
                                     key=lambda r: -r.get("t", 0)),
                    "peers": [{"call": p["call"], "status": p["status"], "status_text": STATUS_TEXT[p["status"]],
                               "route": p["route"], "via": p["via"], "unread": p["unread"],
                               "can_send": p["status"] == OPEN or bool(p.get("direct")),
                               "forever": bool(p.get("forever")),
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
