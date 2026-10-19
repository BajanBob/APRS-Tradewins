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
    print("All self-tests passed")


if __name__ == "__main__":
    main()
