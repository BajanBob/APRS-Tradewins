# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""Quick self-test: modem loopback, FCS, KISS framing and grid/region logic.
Run with:  python test_modem.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import afsk  # noqa: E402
import tnc  # noqa: E402
import vhf_prop_aprs as eng  # noqa: E402


def main():
    assert afsk.crc16_x25(b"123456789") == 0x906E, "FCS"
    frame = afsk.encode_ax25("8P6XX-10", "APZPRP", ["WIDE2-1"], ":BLN1VHF  :2m OPEN CAR-NA test")
    rng = np.random.default_rng(1)
    for rate in (48000, 44100):
        ok = 0
        for off in range(5):
            a = afsk.modulate(frame, rate)
            a = np.concatenate([np.zeros(500 + off * 13), a, np.zeros(800)])
            a = a + rng.normal(0, 0.2, len(a))
            d = afsk.Demodulator(rate)
            got = []
            for i in range(0, len(a), 4096):
                got += d.process(a[i:i + 4096])
            ok += frame[:-2] in got
        assert ok == 5, f"modem loopback at {rate} Hz: {ok}/5"
    assert afsk.decode_ax25(frame[:-2]) == "8P6XX-10>APZPRP,WIDE2-1::BLN1VHF  :2m OPEN CAR-NA test"
    dec = tnc.KissDecoder()
    assert dec.feed(tnc.kiss_wrap(b"\xc0\xdb" + frame[:-2])) == [b"\xc0\xdb" + frame[:-2]], "KISS escaping"
    assert eng.classify("8P6XX", "GK03", "") == "CAR"
    assert eng.classify("K4ABC", "EL96", "") == "NA"
    assert eng.classify("PY7AA", "HI21", "") == "LA"
    assert eng.classify("G4XXX", "IO91", "") == "EU"
    assert eng.aprs_passcode("N0CALL") == 13023
    assert tnc.civ_ptt_command("7C", True) == bytes.fromhex("FEFE7CE01C0001FD"), "CI-V TX on"
    assert tnc.civ_ptt_command("7C", False) == bytes.fromhex("FEFE7CE01C0000FD"), "CI-V TX off"
    test_cat()
    print("All self-tests passed")


class FakeIcom:
    """Pretends to be an IC-9100 on a CI-V port (echo + reply, like USB CI-V)."""

    def __init__(self, addr=0x7C, hz=145_500_000):
        self.addr, self.hz, self.mode, self.data, self.buf = addr, hz, [0x01, 0x01], [0x00, 0x00], b""

    def reset_input_buffer(self): self.buf = b""
    def flush(self): pass

    def write(self, b):
        self.buf += b                                   # USB CI-V echoes the command
        assert b[:2] == b"\xFE\xFE" and b[2] == self.addr and b[3] == 0xE0 and b[-1] == 0xFD
        cmd, ok = b[4:-1], b"\xFB"
        if cmd[0] == 0x05:
            self.hz = tnc.civ_bcd_to_hz(cmd[1:6]); r = ok
        elif cmd[0] == 0x03:
            r = b"\x03" + tnc.hz_to_civ_bcd(self.hz)
        elif cmd[0] == 0x06:
            self.mode = list(cmd[1:3]); r = ok
        elif cmd[0] == 0x04:
            r = bytes([0x04] + self.mode)
        elif cmd[:2] == b"\x1A\x06" and len(cmd) == 2:
            r = bytes([0x1A, 0x06] + self.data)
        elif cmd[:2] == b"\x1A\x06":
            self.data = list(cmd[2:4]); r = ok
        else:
            r = b"\xFA"
        self.buf += bytes([0xFE, 0xFE, 0xE0, self.addr]) + r + b"\xFD"

    def read(self, n):
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


class FakeYaesu:
    def __init__(self):
        self.hz, self.mode, self.buf = 7_074_000, "C", b""

    def reset_input_buffer(self): self.buf = b""
    def flush(self): pass

    def write(self, b):
        c = b.decode()
        if c == "FA;":
            self.buf += f"FA{self.hz:09d};".encode()
        elif c.startswith("FA"):
            self.hz = int(c[2:-1])
        elif c == "MD0;":
            self.buf += f"MD0{self.mode};".encode()
        elif c.startswith("MD0"):
            self.mode = c[3]

    def read(self, n):
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


def test_cat():
    assert tnc.hz_to_civ_bcd(144_390_000) == bytes.fromhex("0000394401"), "BCD 144.390"
    assert tnc.civ_bcd_to_hz(bytes.fromhex("0050824501")) == 145_825_000, "BCD 145.825"
    for v, hz in (("144.390", 144_390_000), ("145.570", 145_570_000), ("144.800", 144_800_000),
                  ("144.930", 144_930_000), ("145.825", 145_825_000)):
        assert tnc.aprs_freq_hz({"aprs_freq": v}) == hz, v
    assert tnc.aprs_freq_hz({"aprs_freq": "custom", "aprs_freq_custom": "432.500"}) == 432_500_000
    assert tnc.aprs_freq_hz({"aprs_freq": "custom", "aprs_freq_custom": "14.105"}) is None
    # IC-9100: tune, then restore on stop
    ic = tnc.CivPTT("COMx", 9600, "7C")
    ic.ser = FakeIcom()
    t = tnc.SoundModemTNC(ptt=ic, tune_hz=144_390_000)
    assert t.tune(144_390_000) and ic.ser.hz == 144_390_000, t.cat_status
    assert ic.ser.mode == [0x05, 0x01] and ic.ser.data == [0x01, 0x01], "FM-D"
    assert t.cat_saved == {"hz": 145_500_000, "mode": [0x01, 0x01], "data": [0x00, 0x00]}
    t._restore_radio()
    assert ic.ser.hz == 145_500_000 and ic.ser.mode == [0x01, 0x01] and ic.ser.data == [0x00, 0x00], "restore"
    # FT-991A text CAT
    ya = tnc.YaesuCatPTT("COMy", 38400)
    ya.ser = FakeYaesu()
    t2 = tnc.SoundModemTNC(ptt=ya, tune_hz=145_825_000)
    assert t2.tune(145_825_000) and ya.ser.hz == 145_825_000 and ya.ser.mode == "A", t2.cat_status
    t2._restore_radio()
    assert ya.ser.hz == 7_074_000 and ya.ser.mode == "C", "Yaesu restore"
    # radios without CAT just get a note
    t3 = tnc.SoundModemTNC(ptt=tnc.PTT(), tune_hz=144_390_000)
    assert not t3.tune(144_390_000) and "by hand" not in t3.cat_status and "no CAT" in t3.cat_status


if __name__ == "__main__":
    main()
