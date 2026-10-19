"""Regression tests for the code-review fixes (bugs #1-#5)."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import afsk
import app
import vhf_prop_aprs as eng  # bug #5: module must be importable (was 'vhf prop aprs.py')


def test_1_addr_ssid_star_no_crash():
    # bug #1: _addr with an SSID + '*' digi marker must not raise ValueError
    b = afsk._addr("WIDE1-1*")
    assert b[6] & 0x80, "H-bit (has-been-digipeated) should be set"


def test_1_roundtrip_digipeated_frame():
    # encode then decode a digipeated path element (SSID + '*')
    frame = afsk.encode_ax25("8P6XX-10", "APZPRP", ["WIDE1-1*", "WIDE2-1"], ":test")
    txt = afsk.decode_ax25(frame[:-2])
    assert "WIDE1-1*" in txt, txt
    # tnc2_to_frame must also accept the marker
    afsk.tnc2_to_frame("8P6XX-10>APZPRP,WIDE1-1*:test")  # no exception


def test_2_passcode_validation():
    cfg = dict(app.DEFAULTS)
    cfg["call"] = "8P6XX-10"
    cfg["grid"] = "GK03fd"
    cfg["passcode"] = "abc"
    errs = app.validate(cfg)
    assert any("passcode" in e.lower() for e in errs), errs


def test_3_malformed_content_length():
    class FakeHeaders:
        def get(self, k, d=None):
            return "not-a-number"
    class Fake:
        headers = FakeHeaders()
    assert app.Handler._body(Fake()) == {}


def test_4_latlon_rollover():
    lat, lon = app.latlon_to_aprs(13.9999999, -59.9999999)
    assert "60.00" not in lat and "60.00" not in lon, (lat, lon)
    # normal values unchanged
    assert app.latlon_to_aprs(13.10, -59.60) == ("1306.00N", "05936.00W")


def test_5_module_importable():
    assert eng.TOCALL == "APZPRP"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print(f"All {len(tests)} regression tests passed")
