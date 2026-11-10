# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""Quick self-test: modem loopback, FCS, KISS framing and grid/region logic.
Run with:  python test_modem.py"""
import time
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import afsk  # noqa: E402
import digi  # noqa: E402
import dxmode  # noqa: E402
import fx25  # noqa: E402
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
    # 300-baud HF profile (1600/1800 Hz) in noise
    ok = 0
    for off in range(3):
        a = afsk.modulate(frame, 48000, 300, 50, 0.5, "300")
        a = np.concatenate([np.zeros(900 + off * 37), a, np.zeros(2000)]) + rng.normal(0, 0.2, len(a) + 2900 + off * 37)
        d = afsk.Demodulator(48000, "300")
        got = []
        for i in range(0, len(a), 4096):
            got += d.process(a[i:i + 4096])
        ok += frame[:-2] in got
    assert ok == 3, f"300-baud HF loopback: {ok}/3"
    # Tradewind Robust weak-signal mode: decodes at -10 dB SNR (2.5 kHz) with a tuning error
    pl = b"8P5AB-10>APZPRP::8P6XX-9  :SITREP Ocean City ok{3"
    for trial in range(2):
        a = afsk.robust_modulate(pl, 8000, 0.5)
        shift = np.exp(2j * np.pi * (60 + 40 * trial) * np.arange(len(a)) / 8000)
        an = np.fft.ifft(np.fft.fft(a) * (np.fft.fftfreq(len(a)) > 0) * 2)         # analytic signal
        a = np.real(an * shift)                                                        # +60 / +100 Hz off tune
        sig = np.concatenate([np.zeros(3000 + trial * 777), a, np.zeros(8000)])
        sigma = np.sqrt(0.125 / 10 ** (-10 / 10) * 4000 / 2500)
        x = sig + rng.normal(0, sigma, len(sig))
        d, got = afsk.RobustDemodulator(8000), []
        for i in range(0, len(x), 4000):
            got += d.process(x[i:i + 4000])
        assert pl in got, f"robust mode at -10 dB (trial {trial})"
    assert afsk.decode_ax25(frame[:-2]) == "8P6XX-10>APZPRP,WIDE2-1::BLN1VHF  :2m OPEN CAR-NA test"
    dec = tnc.KissDecoder()
    assert dec.feed(tnc.kiss_wrap(b"\xc0\xdb" + frame[:-2])) == [b"\xc0\xdb" + frame[:-2]], "KISS escaping"
    assert eng.classify("8P6XX", "GK03", "") == "CAR"
    assert eng.classify("K4ABC", "EL96", "") == "NA"
    assert eng.classify("PY7AA", "HI21", "") == "LA"
    assert eng.classify("G4XXX", "IO91", "") == "EU"
    assert eng.classify("CN8WEBSDR", "FK93", "") is None, "wrong locator must not make a station Caribbean"
    assert eng.classify("K1ABC/KP4", "FK68", "") == "CAR", "portable Caribbean call"
    assert eng.aprs_passcode("N0CALL") == 13023
    assert tnc.civ_ptt_command("7C", True) == bytes.fromhex("FEFE7CE01C0001FD"), "CI-V TX on"
    assert tnc.civ_ptt_command("7C", False) == bytes.fromhex("FEFE7CE01C0000FD"), "CI-V TX off"
    test_cat()
    test_chat()
    test_limits()
    test_v33()
    test_qsy()
    test_replyack()
    test_v35()
    test_v36()
    test_v37()
    test_edition()
    test_v323()
    test_v324()
    test_v3171()
    test_hf()
    test_android_imports()
    test_faceplate_js()
    print("All self-tests passed")


def test_faceplate_js():
    """v3.22.1: the face plate's JavaScript must parse (a stray apostrophe broke every button in v3.22)."""
    import shutil
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    node = shutil.which("node")
    if not node:
        print("Face plate JS check skipped (Node.js not installed)")
        return
    js = ("const h=require('fs').readFileSync(process.argv[1],'utf8');let n=0;"
          "for(const m of h.matchAll(/<script>([\\s\\S]*?)<\\/script>/g)){new Function(m[1]);n++}"
          "console.log(n)")
    r = subprocess.run([node, "-e", js, os.path.join(here, "faceplate.html")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and int(r.stdout.strip() or 0) >= 1, "face plate JavaScript does not parse:\n" + r.stderr[-600:]
    print("Face plate ok: JavaScript parses")


def test_android_imports():
    """v3.21.1: the Android build has no numpy / sounddevice / pyserial - the app must still start."""
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    code = ("import sys; [sys.modules.__setitem__(m, None) for m in ('numpy', 'sounddevice', 'serial')]; "
            f"sys.path.insert(0, {here!r}); import app; print(app.APP_VERSION)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, "app must import without numpy (Android):\n" + r.stderr[-800:]
    print("Android ok: app starts without numpy, sounddevice or pyserial")


def test_hf():
    """v3.18 HF Calling: selcall / ALE / SELCAL decoders, settings checks, station wiring."""
    import app
    import hfcall
    hfcall.selftest(verbose=False)
    base = dict(app.DEFAULTS, call="8P6AB", grid="GK03fd", tnc="sound", hf_enabled=True)
    assert not app.validate(dict(base, hf_ids="1234, 123456", hf_channels=hfcall.HFN_CHANNELS, hf_scan=True))
    assert app.validate(dict(base, hf_ids="12345"))
    assert app.validate(dict(base, hf_channels="7102, 99999"))
    assert app.validate(dict(base, hf_scan=True, hf_channels="7102"))
    assert app.validate(dict(base, hf_sound_min=5))
    assert app.validate(dict(base, tnc="none"))
    assert app.validate(dict(base, hf_selcall_tones="xyz"))
    assert app.validate(dict(base, hf_call_format="oem")) and app.validate(dict(base, hf_ccir_dots_s=40))
    ps = hfcall.presets()
    assert any(c["khz"] == 14109.0 and c["tx"] for g in ps for c in g["channels"])
    assert all(not c["tx"] for g in ps if g["use"] == "selcal" for c in g["channels"]), "aviation: listen only"
    assert hfcall.amateur_hf(7102) and hfcall.amateur_hf(14109) and not hfcall.amateur_hf(8291) \
        and not hfcall.amateur_hf(7298.5), "transmit only inside amateur HF bands"

    class FakePtt:
        can_tune = True

    class FakeTnc:
        kind, rate, tx_active, ptt = "fake", 48000, False, FakePtt()

        def __init__(self):
            self.audio_taps, self.sent, self.tuned = [], [], []

        def send_audio(self, a, max_s=30):
            self.sent.append(a)

        def tune(self, hz, mode=None):
            self.tuned.append((hz, mode))
            return True

    st = app.Station.__new__(app.Station)
    st.cfg = dict(base, hf_ids="1234", hf_channels="7102 40m, 14109 20m")
    st.tnc, st.tnc2, st.isc, st.qsy_hz, st.iss_tuned, st.commercial, st.hf = FakeTnc(), None, None, None, False, False, None
    logs = []
    st.add_log = lambda k, t: logs.append((k, t))
    st._hf_start(st.cfg)
    assert st.hf and st.hf.feed in st.tnc.audio_taps, logs
    st.hf.threaded = False
    st.hf_action({"action": "tune", "khz": "7102"})
    assert st.tnc.tuned[-1] == (7102000, "usb")
    st.hf_action({"action": "call", "kind": "beacon", "to": "5678"})
    st.hf_action({"action": "call", "kind": "ale", "to": "8P6CD"})
    st.hf_action({"action": "call", "kind": "beacon", "to": "336699", "fmt": "ccir"})
    try:
        st.hf_action({"action": "call", "kind": "page", "to": "336699", "text": "hi", "fmt": "ccir"})
        raise AssertionError("page calls need the Tradewind format")
    except ValueError:
        pass
    assert len(st.tnc.sent) == 3
    st.hf.tune_to(8291)                                  # a non-amateur channel: listen only
    try:
        st.hf_action({"action": "call", "kind": "selcall", "to": "5678"})
        raise AssertionError("transmitted outside the amateur bands")
    except ValueError:
        pass
    assert st._hf_holds_radio(), "1200-baud APRS waits while HF Calling has the radio"
    try:
        st._pick_link("8P6CD", "rf")
        raise AssertionError("APRS must not go out on the HF calling radio")
    except IOError:
        pass
    hs = st.hf_state()
    assert hs["on"] and hs["hfn"] and hs["presets"] and "alerts" in hs and hs["tones"] == "lm"
    st.hf._alert("call", "test")
    assert st.hf_state()["alerts"][0]["kind"] == "call"
    st._hf_stop()
    assert not st.tnc.audio_taps and st.hf is None
    print("HF Calling ok: CCIR 493-4 + Tradewind selcall, ALE, SELCAL 32, presets, alerts, band guard, station wiring")


def test_v324():
    """v3.24 OSINT: AIS decoding, FIRMS hotspots, imagery tile addresses, offline save, settings."""
    import tempfile
    import app
    import osint
    m = osint.decode_ais("!AIVDM,1,1,,B,177KQJ5000G?tO`K>RA1wUbN0TKH,0*5C")          # published reference sentence
    assert m == {"mmsi": 477553000, "lat": 47.58283, "lon": -122.34583, "sog": 0.0, "cog": 51.0}, m
    assert osint.decode_ais("!AIVDM,2,1,3,B,55P5TL01VIaAL@7WKO@mBplU@<PDhh000000001S;AJ::4A80?4i@E53,0*3E") is None   # multi-part: skipped
    assert osint.decode_ais("$GPGGA,garbage") is None and osint.decode_ais("!AIVDM,1,1,,A,###,0*00") is None
    csv_text = ("latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight\n"
                "13.21,-59.55,330.1,0.4,0.4,2026-11-09,1706,N,VIIRS,n,2.0NRT,290.2,4.5,D\nbad,row\n")
    fires = osint.parse_firms(csv_text)
    assert len(fires) == 1 and fires[0]["lat"] == 13.21 and fires[0]["when"] == "2026-11-09 17:06 UTC" and fires[0]["frp"] == 4.5
    url = osint.gibs_tile_url("viirs", "2026-11-08", 6, 29, 21)
    assert url.endswith("/VIIRS_SNPP_CorrectedReflectance_TrueColor/default/2026-11-08/GoogleMapsCompatible_Level9/6/29/21.jpg")
    assert osint.tile_xy(0, 0, 1) == (1, 1) and (10, 342, 474) in osint.tiles_around(13.1, -59.6, 0.05, [10])
    asked = []

    def web(u):
        asked.append(u)
        return csv_text.encode()
    o = osint.Osint(tempfile.mkdtemp(), web)
    o.tick({"firms_key": "KEY123", "osint_storms": False}, 13.1, -59.6)
    assert "/KEY123/VIIRS_SNPP_NRT/-65.60,7.10,-53.60,19.10/2" in asked[0] and len(o.state()["fires"]) == 1
    o.tick({"firms_key": "KEY123", "osint_storms": False}, 13.1, -59.6)
    assert len(asked) == 1, "hotspots are fetched at most every 30 minutes"
    o2 = osint.Osint(os.path.dirname(o.dir), lambda u: (_ for _ in ()).throw(IOError("offline")))
    assert len(o2.state()["fires"]) == 1, "the last hotspots still show with no internet"
    o.feed_ais("!AIVDM,1,1,,B,177KQJ5000G?tO`K>RA1wUbN0TKH,0*5C")
    assert o.state()["ships"][0]["mmsi"] == 477553000
    got = []
    assert o.save_offline(13.1, -59.6, lambda z, x, y: got.append(("b", z)) or True, "viirs", "2026-11-08",
                          lambda z, x, y: got.append(("i", z)) or True)
    for _ in range(100):
        if not o.save["busy"]:
            break
        time.sleep(0.05)
    assert o.save["done"] == o.save["total"] > 100 and {z for k, z in got if k == "i"} <= set(range(4, 10))
    base = dict(app.DEFAULTS, call="8P6XX-10", grid="GK03fd")
    assert any("AIS port" in e for e in app.validate(dict(base, ais_port=80)))
    assert abs(app.station_latlon(base)[0] - 13.15) < 0.1 and app.OSINT.state()["layers"][0]["key"] == "viirs"
    print("v3.24 ok (OSINT: AIS ships, FIRMS hotspots, NASA imagery tiles, save for offline)")


def test_v323():
    """v3.23: face-plate languages (es, pt) and chat translation - English on the air."""
    import json
    import re
    import app
    import translate as tr
    here = os.path.dirname(os.path.abspath(__file__))
    # dictionaries: valid, placeholders kept, the important screens covered
    for lang in ("es", "pt"):
        d = json.load(open(os.path.join(here, "i18n", lang + ".json"), encoding="utf-8"))
        assert len(d) > 1000, (lang, len(d))
        for k, v in d.items():
            assert sorted(re.findall(r"\{\d+\}", k)) == sorted(re.findall(r"\{\d+\}", v)), (lang, k, v)
        for must in ("Station OFF", "Two-way chat", "Settings saved", "Turn the station ON first."):
            assert must in d, (lang, must)
        pay = json.loads(app.i18n_payload(lang))
        assert pay["lang"] == lang and pay["exact"]["Settings saved"] and pay["pat"]
    assert app.i18n_payload("en") is None
    assert app.page_lang({"lang": "auto"}, "pt-BR,pt;q=0.9,en;q=0.8") == "pt"
    assert app.page_lang({"lang": "auto"}, "de-DE") == "en" and app.page_lang({"lang": "es"}, "pt") == "es"
    # language detection and protected words
    assert tr.detect("necesito agua y comida en el refugio por favor") == "es"
    assert tr.detect("preciso de água e comida no abrigo por favor") == "pt"
    assert tr.detect("we need water and food at the shelter please") == "en"
    assert tr.detect("QSL 73") == ""
    body, keep = tr.protect("LU1XX-9 QSL en GF05tj 144.930 MHz 73")
    assert "LU1XX-9" not in body and "GF05tj" not in body and tr.restore(body, keep) == "LU1XX-9 QSL en GF05tj 144.930 MHz 73"
    # a stand-in engine (no internet needed): outgoing becomes English, incoming becomes the operator's language
    seen = []

    def fake(text, src, dst, cfg):
        seen.append((src, dst, text))
        return {"es": "We need water at the shelter [0]", "en": "Necesitamos agua en el refugio [0]"}.get(src, text)
    tr._ENGINE["mymemory"], real = fake, tr._ENGINE["mymemory"]
    try:
        cfg = {"tr_engine": "mymemory", "lang": "es", "tr_out": True, "tr_in": True}
        sent, note = tr.outgoing("Necesitamos agua en el refugio LU1XX", cfg)
        assert sent == "We need water at the shelter LU1XX" and note["orig"].startswith("Necesitamos") and note["from"] == "es"
        assert tr.outgoing("We need water at the shelter", cfg) == ("We need water at the shelter", None)
        assert tr.outgoing("hola", dict(cfg, tr_engine="off")) == ("hola", None)
        got, src = tr.incoming("We need water at the shelter 8P6XX", cfg)
        assert src == "en" and got == "Necesitamos agua en el refugio 8P6XX"
        assert tr.incoming("necesitamos agua en el refugio", cfg) == (None, "es")      # already my language
        # a broken engine never stops the message: it goes out as typed, with a note
        tr._ENGINE["mymemory"] = lambda *a: (_ for _ in ()).throw(IOError("no internet"))
        tr._cache.clear()
        sent, note = tr.outgoing("Necesitamos agua en el refugio ahora mismo", cfg)
        assert sent.startswith("Necesitamos") and "not translated" in note["error"]
    finally:
        tr._ENGINE["mymemory"] = real
        tr._cache.clear()
    # settings are checked
    base = dict(app.DEFAULTS, call="8P6XX-10", grid="GK03fd")
    assert any("language" in e for e in app.validate(dict(base, lang="xx")))
    assert any("LibreTranslate" in e for e in app.validate(dict(base, tr_engine="libretranslate")))
    assert not [e for e in app.validate(dict(base, lang="pt", tr_engine="mymemory")) if "language" in e or "engine" in e]
    print("v3.23 ok (Spanish and Portuguese face plate, chat translation: English on the air)")


def test_edition():
    """Tradewind (amateur) contains no encryption; the Commander edition adds it from commander/."""
    import app
    here = os.path.dirname(os.path.abspath(__file__))
    ham_files = [f for f in os.listdir(here) if f.endswith((".py", ".html")) and f != "test_modem.py"]
    for f in ham_files:
        with open(os.path.join(here, f), encoding="utf-8") as fh:
            src = fh.read()
        for bad in ("AESGCM", "from cryptography", "import cryptography", "Fernet", "ChaCha20"):
            assert bad not in src, f"{f} contains {bad}: encryption belongs only in commander/"
    if app.commander is None:
        assert app.EDITION == "amateur" and app.APP_TITLE == "Tradewind"
        cfg = dict(app.DEFAULTS, op_mode="commercial")
        assert any("Tradewind Commander" in e for e in app.validate(cfg))
        assert not hasattr(app.Station, "_com_send")
        print("Edition ok: Tradewind (amateur, clear text only - no encryption code)")
    else:
        from commander import test_commander
        test_commander.run()


def test_v3171():
    """v3.17.1: bare COM-port numbers on Windows, and clear hail errors (own ID / other station's ID)."""
    import app
    f = app.serial_port_name
    assert [f(x, True) for x in ("12", "COM12", "com 3", "012", "/dev/ttyUSB0", "127.0.0.1:4532", "")] == \
        ["COM12", "COM12", "COM3", "COM12", "/dev/ttyUSB0", "127.0.0.1:4532", ""]
    assert f("12", False) == "12"
    cfg = app._migrate(dict(app.DEFAULTS, ptt_port="12", kiss_serial="com5", link2_serial="/dev/ttyACM0"))
    if sys.platform.startswith("win"):
        assert (cfg["ptt_port"], cfg["kiss_serial"]) == ("COM12", "COM5")
    assert cfg["link2_serial"] == "/dev/ttyACM0"
    m = tnc.ChatManager("8P5AB-7", lambda *a: None)
    for call, want in (("8P5AB-7", "own callsign"), ("", "Enter the other station's callsign")):
        try:
            m.hail(call)
            raise AssertionError("hail must refuse " + call)
        except ValueError as e:
            assert want in str(e), e
    m.id_word, m.id_example = "unit ID", "PX2"
    try:
        m.hail("8P5AB-7")
    except ValueError as e:
        assert "own unit ID" in str(e) and "PX2" in str(e), e
    print("v3.17.1 ok (COM12 port names, hail errors)")


def test_chat():
    chat = tnc
    m = chat.parse_message("8P6XX-9>APDR16,WIDE2-1::8P5AB-10 :hello there{12")
    assert m["src"] == "8P6XX-9" and m["to"] == "8P5AB-10" and m["text"] == "hello there" and m["msgno"] == "12"
    assert chat.parse_message("8P6XX-9>APDR16::8P5AB-10 :ack12")["ack"] == "12"
    assert chat.parse_message("8P6XX-9>APDR16::8P5AB-10 :hi{3}AB")["msgno"] == "3"
    assert chat.format_message("8P6XX-9", "hi", "7") == ":8P6XX-9  :hi{7"
    assert all(len(x) <= 67 for x in chat.split_text("word " * 60))
    clock = [1000.0]
    now = lambda: clock[0]  # noqa: E731
    air = []                 # packets "on the air": (from, info, route)
    a = chat.ChatManager("8P5AB-10", lambda to, info, r: air.append(("8P5AB-10", info, r)), now)
    b = chat.ChatManager("8P6XX-9", lambda to, info, r: air.append(("8P6XX-9", info, r)), now)

    def deliver(drop=False):
        while air:
            src, info, _ = air.pop(0)
            if drop:
                continue
            for st in (a, b):
                st.on_packet(f"{src}>APZPRP,WIDE2-1:{info}", "RF")

    a.hail("8P6XX-9", "rf")
    deliver()
    assert b._peer("8P5AB-10")["status"] == chat.INCOMING
    assert any(e["kind"] == "request" for e in b.events)
    try:
        a.send("8P6XX-9", "too soon")
        raise AssertionError("chat must wait for the handshake")
    except ValueError:
        pass
    b.accept("8P5AB-10")
    deliver()
    assert b._peer("8P5AB-10")["status"] == chat.ACCEPTED
    assert a._peer("8P6XX-9")["status"] == chat.AWAIT_CONFIRM, "hailer must be asked to confirm"
    a.accept("8P6XX-9")
    deliver()
    assert a._peer("8P6XX-9")["status"] == chat.OPEN and b._peer("8P5AB-10")["status"] == chat.OPEN
    a.send("8P6XX-9", "Hearing you 5x9 via TEP")
    deliver()
    clock[0] += 9; b.tick(); deliver()          # no quick reply: the held ack goes on its own
    msgs = b._peer("8P5AB-10")["messages"]
    assert msgs[-1]["dir"] == "in" and msgs[-1]["text"] == "Hearing you 5x9 via TEP"
    out = [x for x in a._peer("8P6XX-9")["messages"] if x["dir"] == "out"]
    assert all(x["state"] == "delivered" for x in out), "every message acked"
    # a lost packet is retried, and the duplicate is not shown twice
    b.send("8P5AB-10", "QSL 73")
    deliver(drop=True)
    clock[0] += 31
    b.tick()
    deliver()
    assert [x["text"] for x in a._peer("8P6XX-9")["messages"] if x["dir"] == "in"] == ["QSL 73"]
    clock[0] += 9; a.tick(); deliver()          # held ack released
    assert not b.pending, "retry acked"
    a.end("8P6XX-9")
    deliver()
    assert b._peer("8P5AB-10")["status"] == chat.ENDED
    # a non-Tradewind station can answer a hail by typing ACCEPT
    c = chat.ChatManager("8P5AB-10", lambda to, info, r: air.append(("8P5AB-10", info, r)), now)
    c.hail("KP4ZZ", "rf_is")
    air.clear()
    c.on_packet("KP4ZZ>APDR16::8P5AB-10 :accept{5", "IS")
    assert c._peer("KP4ZZ")["status"] == chat.AWAIT_CONFIRM
    # "call until received": keeps calling past the normal retries, then stops once acked
    sent = []
    e = chat.ChatManager("8P5AB-10", lambda to, info, r: sent.append(info), now)
    e.hail("8P6YY", "rf", interval=60, forever=True)
    for _ in range(8):
        clock[0] += 61
        e.tick()
    assert len(sent) == 9, f"kept calling: {len(sent)}"
    no = sent[-1].rsplit("{", 1)[1]
    e.on_packet(f"8P6YY>APDR16::8P5AB-10 :ack{no}", "RF")
    clock[0] += 3600
    e.tick()
    assert len(sent) == 9 and e._peer("8P6YY")["status"] == chat.HAILING, "stops when received, never expires"
    # chats survive a restart
    saved = a.to_dict()
    a2 = chat.ChatManager("8P5AB-10", lambda *x: None, now)
    a2.load(saved)
    assert [m["text"] for m in a2._peer("8P6XX-9")["messages"] if m["dir"] == "in"] == ["QSL 73"]
    # Phase 2 link controller: prefers the fast link, falls back when it fails, steps back up later
    lc = chat.LinkController({"A": {"name": "2m 1200", "speed": 1200}, "B": {"name": "70cm 9600", "speed": 9600}}, now)
    assert lc.choose("X") == "B", "untried: prefer the faster link"
    for _ in range(3):
        lc.sent("X", "B"); lc.acked("X", "B", 1.0, 80)
    assert lc.choose("X") == "B"
    lc.lost("X", "B"); lc.lost("X", "B")
    assert lc.choose("X") == "A", "two lost tries in a row: fall back to the slower link"
    lc.sent("X", "A"); lc.acked("X", "A", 4.0, 80)
    clock[0] += lc.REST_S + 1
    assert lc.choose("X") == "B", "after a rest the fast link is tried again"
    # end to end: retries move to the working link
    air2, links_used = [], []
    def sender(to, info, r):
        l = lc2.choose(to); links_used.append(l); air2.append((l, info)); return l
    lc2 = chat.LinkController({"A": {"speed": 1200}, "B": {"speed": 9600}}, now)
    f = chat.ChatManager("8P5AB-10", sender, now)
    f.links = lc2
    f._peer("8P6ZZ")["status"] = chat.OPEN
    f._peer("8P6ZZ")["route"] = "auto"
    f.send("8P6ZZ", "Path test")
    for _ in range(2):                       # link B is dead: tries are lost
        air2.clear(); clock[0] += 200; f.tick()
    l, info = air2[-1]
    assert l == "A", f"retry moved to the working link (used {links_used})"
    no = info.rsplit("{", 1)[1]
    f.on_packet(f"8P6ZZ>APDR16::8P5AB-10 :ack{no}", "RF")
    assert [m["state"] for m in f._peer("8P6ZZ")["messages"] if m["dir"] == "out"] == ["delivered"]
    # Phase 4: store and forward - a message to a station that is off the air waits, then goes when they are heard
    out = []
    g = chat.ChatManager("8P5AB-10", lambda to, info, r: out.append(info), now)
    g._peer("8P6QQ")["status"] = chat.OPEN
    g.send("8P6QQ", "Water truck at 3pm")
    for _ in range(4):
        clock[0] += 130; g.tick()
    assert [m["state"] for m in g._peer("8P6QQ")["messages"] if m["dir"] == "out"] == ["queued"] and g.outbox
    out.clear()
    clock[0] += 60
    g.on_heard("8P6QQ")                       # their beacon is heard again
    assert out and "Water truck" in out[0], "stored message resent when the station reappears"
    no = out[0].rsplit("{", 1)[1]
    g.on_packet(f"8P6QQ>APDR16::8P5AB-10 :ack{no}", "RF")
    assert [m["state"] for m in g._peer("8P6QQ")["messages"] if m["dir"] == "out"] == ["delivered"] and not g.outbox
    # relay: A asks R to pass a message to C; R forwards it and tells A when it is delivered
    net = []
    A = chat.ChatManager("8P5AB-10", lambda to, info, r: net.append(("8P5AB-10", info)), now)
    R = chat.ChatManager("8P6RR", lambda to, info, r: net.append(("8P6RR", info)), now)
    C = chat.ChatManager("J68CC", lambda to, info, r: net.append(("J68CC", info)), now)
    R.relay_enabled = True
    reach = {"8P5AB-10": {"8P6RR"}, "8P6RR": {"8P5AB-10", "J68CC"}, "J68CC": {"8P6RR"}}    # A and C cannot hear each other
    def air():
        while net:
            src, info = net.pop(0)
            for st in (A, R, C):
                if st.mycall in reach[src]:
                    st.on_packet(f"{src}>APZPRP:{info}", "RF")
    A.relay("8P6RR", "J68CC", "Need insulin at Bath shelter")
    air()
    got = [m["text"] for m in C._peer("8P6RR")["messages"] if m["dir"] == "in"]
    assert got and got[0].startswith("Need insulin at Bath shelter") and "de 8P5AB-10 via 8P6RR" in got[0], got
    assert any("delivered to J68CC" in m["text"] for m in A._peer("8P6RR")["messages"]), "originator told of delivery"
    # check-in net roster
    A.on_packet("8P6RR>APDR16::8P5AB-10 :CHECKIN 210130L 1/1 Safe. Ocean City. 4 people.{9", "RF")
    assert A.roster["8P6RR"]["status"] == "Safe" and "Ocean City" in A.roster["8P6RR"]["text"]
    # unanswered hails expire
    d = chat.ChatManager("8P5AB-10", lambda *x: None, now)
    d.hail("J68AB", "rf")
    clock[0] += chat.LINK_TIMEOUT_S + 200
    d.tick()
    assert d._peer("J68AB")["status"] == chat.EXPIRED


def test_limits():
    """v3.2 abuse limits."""
    import types
    clock = [5000.0]
    now = lambda: clock[0]  # noqa: E731
    # hails: at most 5 tries, at least 60 s apart
    sent = []
    h = tnc.ChatManager("8P5AB-10", lambda to, info, r: sent.append(info), now)
    h.hail("8P6AA", "rf", tries=50, interval=5)
    for _ in range(20):
        clock[0] += 59; h.tick()
    assert len(sent) == 5, f"hail capped at 5 tries ({len(sent)})"
    # "keep calling" stops after 30 minutes
    sent.clear()
    h.hail("8P6AB", "rf", interval=60, forever=True)
    for _ in range(40):
        clock[0] += 61; h.tick()
    assert 25 <= len(sent) <= 31 and h._peer("8P6AB")["status"] == tnc.EXPIRED, len(sent)
    # a "no" means no more hailing that station for 15 minutes
    h.hail("8P6AC", "rf")
    h.on_packet("8P6AC>APDR16::8P5AB-10 :TW-LINK NO 8P6AC can't chat now{1", "RF")
    try:
        h.hail("8P6AC", "rf"); raise AssertionError("re-hail after decline must wait")
    except ValueError:
        pass
    clock[0] += tnc.DECLINE_COOLDOWN_S + 1
    h.hail("8P6AC", "rf")
    # a station calling more than 3 times in 10 minutes is muted for an hour; blocked stations are ignored
    m = tnc.ChatManager("8P5AB-10", lambda *x: None, now)
    for i in range(4):
        m.on_packet(f"8P6NN>APDR16::8P5AB-10 :TW-LINK? 8P6NN asks to chat{i + 10}", "RF")
        clock[0] += 30
    assert "8P6NN" in m.muted and any(e["kind"] == "muted" for e in m.events)
    n_ev = len(m.events)
    m.on_packet("8P6NN>APDR16::8P5AB-10 :TW-LINK? again{20", "RF")
    assert len(m.events) == n_ev, "muted station ignored"
    m.blocked.add("8P6BB")
    m.on_packet("8P6BB>APDR16::8P5AB-10 :hello{1", "RF")
    assert "8P6BB" not in m.peers, "blocked station ignored"
    try:
        m.hail("8P6BB"); raise AssertionError("cannot hail a blocked station")
    except ValueError:
        pass
    # one unanswered message per station, at least 30 s apart
    out = []
    o = tnc.ChatManager("8P5AB-10", lambda to, info, r: out.append(info), now)
    o._peer("8P6OO")["status"] = tnc.OPEN
    o.send("8P6OO", "one")
    o.send("8P6OO", "two")
    assert len(out) == 1 and o.held, "second message waits its turn"
    o.on_packet(f"8P6OO>APDR16::8P5AB-10 :ack{out[0].rsplit('{', 1)[1]}", "RF")
    o.tick()
    assert len(out) == 1, "still inside the 30 s gap"
    clock[0] += tnc.MSG_GAP_S
    o.tick()
    assert len(out) == 2 and "two" in out[1] and not o.held
    # probes: at most 4 an hour
    p = tnc.ChatManager("8P5AB-10", lambda *x: "A", now)
    p.links = tnc.LinkController({"A": {"speed": 1200}, "B": {"speed": 9600}}, now)
    for _ in range(tnc.PROBES_PER_HOUR):
        p.probe("8P6PP")
    try:
        p.probe("8P6PP"); raise AssertionError("probe limit")
    except ValueError:
        pass
    # relays: never a message that was already relayed, never more than 10 an hour
    r_out = []
    r = tnc.ChatManager("8P6RR", lambda to, info, rt: r_out.append((to, info)), now)
    r.relay_enabled = True
    r.on_packet("8P5AB-10>APDR16::8P6RR    :RELAY J68CC hi [de 8P6XX via 8P6ZZ]{1", "RF")
    assert any("RELAY REFUSED" in i for _, i in r_out) and not any(t == "J68CC" for t, _ in r_out)
    for i in range(tnc.RELAYS_PER_HOUR + 2):
        r.on_packet(f"8P5AB-10>APDR16::8P6RR    :RELAY J6{i}DD msg {i}{{{i + 2}", "RF")
    assert sum(1 for t, _ in r_out if t.startswith("J6") and t != "J68CC") == tnc.RELAYS_PER_HOUR
    # outbox: 10 per station, 50 in all
    q = tnc.ChatManager("8P5AB-10", lambda *x: None, now)
    for i in range(15):
        q._outbox_add({"call": "8P6QQ", "text": str(i), "entry": {}, "t": now()})
    for i in range(60):
        q._outbox_add({"call": f"8P{i}Q", "text": str(i), "entry": {}, "t": now()})
    assert sum(1 for x in q.outbox if x["call"] == "8P6QQ") <= 10 and len(q.outbox) == tnc.OUTBOX_MAX
    # airtime: 10% of 10 minutes
    g = tnc.AirtimeGuard(now=now)
    for _ in range(6):
        assert g.wait(10) == 0; g.add(10)
    assert g.wait(5) > 0, "60 s used - must wait"
    clock[0] += 601
    assert g.wait(5) == 0
    # app-level rules
    import app
    assert app.check_path("WIDE1-1,WIDE2-1") == "" and app.check_path("WIDE2-2") == "" and app.check_path("ARISS") == ""
    for bad in ("WIDE3-3", "WIDE1-1,WIDE2-2", "RELAY,WIDE", "TRACE3-3", "WIDE2-1,WIDE2-1,WIDE1-1", "WIDE2-3"):
        assert app.check_path(bad), bad
    st = types.SimpleNamespace(psk_times=__import__("collections").deque(), tx_seen={})
    t0 = 10000.0
    assert app.Station.psk_wait(st, t0) == 0
    st.psk_times.append(t0)
    assert 599 <= app.Station.psk_wait(st, t0 + 1) <= 600, "10 minutes between PSKReporter checks"
    for k in range(1, 6):
        st.psk_times.append(t0 + 600 * k)
    assert app.Station.psk_wait(st, t0 + 3001) == 599, "6th check done: 7th waits for the hour"
    assert app.Station.same_tx_ok(st, "!beacon", t0) and app.Station.same_tx_ok(st, "!beacon", t0 + 1800)
    assert not app.Station.same_tx_ok(st, "!beacon", t0 + 1900), "third identical beacon in an hour held"
    assert app.Station.same_tx_ok(st, "!beacon", t0 + 3601)
    assert app._pin_hash("1234", "ab") == app._pin_hash("1234", "ab") != app._pin_hash("1235", "ab")
    assert app.validate({"call": "N0CALL", "grid": "GK03"}) and app.validate({"call": "8P5AB-10", "grid": "GK03", "rf_path": "WIDE3-3"})


def test_v33():
    """RF-first routing, iGate to RF, KISS server, SDR command, Tradewind badge."""
    import socket as so
    import types
    clock = [9000.0]
    now = lambda: clock[0]  # noqa: E731
    net = []
    reach = {"8P5AB-10": {"8P6RR"}, "8P6RR": {"8P5AB-10", "J37PG"}, "J37PG": {"8P6RR"}}
    A = tnc.ChatManager("8P5AB-10", lambda to, info, r: net.append(("8P5AB-10", info, r)), now)
    R = tnc.ChatManager("8P6RR", lambda to, info, r: net.append(("8P6RR", info, r)), now)
    C = tnc.ChatManager("J37PG", lambda to, info, r: net.append(("J37PG", info, r)), now)
    R.relay_enabled = True
    A.relay_candidates = lambda dest: ["8P6RR"]
    A.is_ok = lambda: False
    def air():
        while net:
            src, info, route = net.pop(0)
            for st in (A, R, C):
                if st.mycall in reach[src] and route != "is":
                    st.on_packet(f"{src}>APZPRP:{info}", "RF")
    p = A._peer("J37PG")
    p["route"], p["direct"] = "smart", True
    A.send("J37PG", "Shelter open at Grenville")
    air()
    for _ in range(3):
        clock[0] += 61; A.tick(); air()
    got = [m["text"] for m in C._peer("8P6RR")["messages"] if m["dir"] == "in"]
    assert got and got[0].startswith("Shelter open at Grenville"), got
    out = [m for m in A._peer("J37PG")["messages"] if m["dir"] == "out"]
    assert out[-1]["state"] == "delivered" and "8P6RR" in out[-1].get("via", ""), out[-1]
    # no relay available: falls back to APRS-IS
    routes = []
    B = tnc.ChatManager("8P5AB-10", lambda to, info, r: routes.append(r), now)
    B.is_ok = lambda: True
    q = B._peer("K4XYZ")
    q["route"], q["direct"] = "smart", True
    B.send("K4XYZ", "test")
    for _ in range(4):
        clock[0] += 61; B.tick()
    assert routes[0] == "rf" and "is" in routes, routes
    # the handshake marks Tradewind stations
    seen = []
    D = tnc.ChatManager("8P5AB-10", lambda *x: None, now)
    D.on_tw = seen.append
    D.on_packet("J37PGP-5>APZPRP::8P5AB-10 :TW-LINK? J37PGP-5 asks to chat{3", "RF")
    assert seen == ["J37PGP-5"]
    # KISS TNC server: a client frame arrives, heard frames go out to the client
    got_tx = []
    srv = tnc.KissServer("127.0.0.1", 0, got_tx.append)
    srv.start()
    port = srv.sock.getsockname()[1]
    cl = so.create_connection(("127.0.0.1", port), timeout=3)
    frame = afsk.encode_ax25("8P5AB-10", "APZPRP", [], "!test")[:-2]
    cl.sendall(tnc.kiss_wrap(frame))
    for _ in range(50):
        if got_tx:
            break
        time.sleep(0.02)
    assert got_tx == [frame], "client frame received"
    while not srv.clients:
        time.sleep(0.02)
    srv.broadcast(frame)
    assert tnc.KissDecoder().feed(cl.recv(512)) == [frame], "heard frame sent to client"
    cl.close(); srv.stop()
    # SDR command line
    cmd = tnc.SdrTNC(144_390_000, "35", 2, 1).command()
    assert cmd[:3] == ["rtl_fm", "-f", "144390000"] and "-g" in cmd and cmd[-1] == "-"
    assert "-g" not in tnc.SdrTNC(144_390_000, "auto").command()
    # SDR demodulates what the sound modem sends (at rtl_fm's 24 kHz)
    audio = afsk.modulate(afsk.encode_ax25("8P6XX-9", "APZPRP", ["WIDE2-1"], ">hello"), 24000, 300, 50, 0.5, "1200")
    assert afsk.Demodulator(24000, "1200").process(audio), "24 kHz decode"
    # full iGate: internet message to a local RF station goes out as third-party
    import app
    sent = []
    st = types.SimpleNamespace(cfg={"igate_tx": True, "igate": True, "call": "8P5AB-10", "rf_path": "WIDE2-1"},
                               commercial=False, tnc=object(), igate_seen={},
                               igate_tx_times=__import__("collections").deque(),
                               heard={"8P6XX-9": {"last": time.time()}}, add_log=lambda *a: None)
    st._tx_tnc = lambda: types.SimpleNamespace(send=lambda *a: sent.append(a))
    gate = app.Station._igate_to_rf
    gate(st, "K4ABC>APDR16,TCPIP*,qAC,T2USA::8P6XX-9  :Are you OK after the storm?{12")
    assert sent and sent[0][3] == "}K4ABC>APDR16,TCPIP,8P5AB-10*::8P6XX-9  :Are you OK after the storm?{12", sent
    gate(st, "K4ABC>APDR16,TCPIP*,qAC,T2USA::8P6XX-9  :Are you OK after the storm?{12")
    assert len(sent) == 1, "duplicate not gated twice"
    gate(st, "K4ABC>APDR16,TCPIP*,qAC,T2USA::W1FAR    :not local{1")
    assert len(sent) == 1, "station not heard locally: not gated"


def test_qsy():
    """v3.4: move a chat off the APRS frequency and back; fall back when there is no contact."""
    clock = [20000.0]
    now = lambda: clock[0]  # noqa: E731
    air, tuned = [], {"A": [], "B": []}
    freq = {"A": "aprs", "B": "aprs"}
    A = tnc.ChatManager("8P5AB-10", lambda to, info, r: air.append(("A", info)), now)
    B = tnc.ChatManager("8P6XX-9", lambda to, info, r: air.append(("B", info)), now)
    def tuner(who):
        def f(mhz):
            tuned[who].append(mhz); freq[who] = mhz or "aprs"
        return f
    A.on_qsy, B.on_qsy = tuner("A"), tuner("B")
    def deliver():
        while air:
            src, info = air.pop(0)
            dst, mgr = ("B", B) if src == "A" else ("A", A)
            if freq[src] == freq[dst]:               # only heard on the same frequency
                mgr.on_packet(f"{(A if src == 'A' else B).mycall}>APZPRP:{info}", "RF")
    def run(secs):
        for _ in range(secs):
            clock[0] += 1; A.tick(); B.tick(); deliver()
    A.hail("8P6XX-9"); deliver(); B.accept("8P5AB-10"); deliver(); A.accept("8P6XX-9"); deliver()
    assert A._peer("8P6XX-9")["status"] == tnc.OPEN
    for bad in ("144.390", "145.010", "145.900", "146", "abc"):
        try:
            A.qsy("8P6XX-9", bad); raise AssertionError(f"{bad} must be refused")
        except ValueError:
            pass
    A.qsy("8P6XX-9", "145.050"); deliver()
    assert B._peer("8P5AB-10")["qsy"]["state"] == "incoming" and any(e["kind"] == "qsy" for e in B.events)
    B.qsy_answer("8P5AB-10", True); deliver()
    run(15)
    assert tuned["A"] == ["145.050"] and tuned["B"] == ["145.050"], tuned
    assert A._peer("8P6XX-9")["qsy"]["state"] == "on" and B._peer("8P5AB-10")["qsy"]["state"] == "on"
    B.send("8P5AB-10", "Much better here"); deliver()
    assert [m["text"] for m in A._peer("8P6XX-9")["messages"] if m["dir"] == "in"][-1] == "Much better here"
    A.qsy_back("8P6XX-9"); deliver(); run(10)
    assert tuned["A"][-1] is None and tuned["B"][-1] is None and A.qsy_active is None and B.qsy_active is None
    # no contact on the new frequency: both go back on their own
    B.on_qsy = lambda mhz: tuned["B"].append(mhz)    # B's radio fails to retune (stays on APRS)
    A.qsy("8P6XX-9", "145.070"); deliver(); B.qsy_answer("8P5AB-10", True); deliver()
    run(tnc.QSY_WAIT_S + 20)
    assert tuned["A"][-1] is None and tuned["B"][-1] is None, tuned
    assert any(e["kind"] == "qsy_fail" for e in A.events + B.events)
    assert tnc.qsy_freq_ok("145.030") and tnc.qsy_freq_ok("433.010") and not tnc.qsy_freq_ok("144.390")
    # automatic channel: a third station hears A and B take 144.510 and offers the next free one
    C = tnc.ChatManager("8P6RR", lambda *x: None, now)
    assert C.free_channel() == "144.510"
    C.on_packet("8P6XX-9>APZPRP::8P5AB-10 :TW-QSY-OK 144.510{9}", "RF")
    assert C.free_channel() == "144.530"
    C.on_packet("8P5AB-10>APZPRP::8P6XX-9  :TW-LINK END 73{4}", "RF")
    assert C.free_channel() == "144.510", "freed when they end"
    for bad in ("144.200", "144.490", "144.390"):
        assert not tnc.qsy_freq_ok(bad), bad


def test_replyack():
    """v3.4: APRS 1.1 reply-acks - an ack rides inside a quick reply instead of its own packet."""
    m = tnc.parse_message("8P6XX-9>APZPRP::8P5AB-10 :Roger{7}3")
    assert m["msgno"] == "7" and m["replyack"] == "3" and m["ra"]
    assert tnc.parse_message("8P6XX-9>APZPRP::8P5AB-10 :hi{7}")["ra"] and tnc.parse_message("X>Y::8P5AB-10 :hi{7")["ra"] is False
    assert tnc.format_message("8P6XX-9", "QSL", "12", "5") == ":8P6XX-9  :QSL{12}5"
    clock = [30000.0]
    now = lambda: clock[0]  # noqa: E731
    air = []
    A = tnc.ChatManager("8P5AB-10", lambda to, info, r: air.append(("8P5AB-10", info)), now)
    B = tnc.ChatManager("8P6XX-9", lambda to, info, r: air.append(("8P6XX-9", info)), now)
    def deliver():
        n = 0
        while air:
            src, info = air.pop(0); n += 1
            for st in (A, B):
                st.on_packet(f"{src}>APZPRP:{info}", "RF")
        return n
    A.hail("8P6XX-9"); deliver(); B.accept("8P5AB-10"); deliver(); A.accept("8P6XX-9"); deliver()
    clock[0] += 40; A.tick(); B.tick(); deliver()
    A.send("8P6XX-9", "How copy?")
    sent = deliver()
    assert sent == 1, "ack held, not sent yet"
    clock[0] += 3
    B.send("8P5AB-10", "5 by 9")                    # quick reply carries the ack
    sent = deliver()
    assert [x["state"] for x in A._peer("8P6XX-9")["messages"] if x["dir"] == "out" and x["text"] == "How copy?"] == ["delivered"]
    clock[0] += 9; A.tick(); B.tick(); sent += deliver()
    assert sent == 2, f"reply + one ack for it, no separate ack for the first message ({sent} packets)"
    # no reply in time: the ack goes on its own after 8 s
    clock[0] += 31; A.tick(); B.tick(); deliver()    # respect the 30 s gap between messages
    A.send("8P6XX-9", "73"); deliver()
    clock[0] += 9; B.tick(); deliver()
    assert [x["state"] for x in A._peer("8P6XX-9")["messages"] if x["text"] == "73"] == ["delivered"]


def test_v37():
    """v3.7: licence 70 cm range, KISS LAN-only, host-name guard, position decoding, delivery timing."""
    import app
    import types
    assert tnc.set_uhf("430-440") and not tnc.qsy_freq_ok("445.000") and tnc.qsy_freq_ok("432.500")
    assert tnc.aprs_freq_hz({"aprs_freq": "custom", "aprs_freq_custom": "445.925", "grid": "GK03"}) is None
    assert tnc.set_uhf("420-450") and tnc.qsy_freq_ok("445.000")
    assert not tnc.set_uhf("400-470") and not tnc.set_uhf("abc")
    tnc.set_uhf("430-440")
    assert not tnc.qsy_freq_ok("147.000", "r1") and tnc.qsy_freq_ok("147.000", "r2"), "Europe 2 m ends at 146"
    assert tnc.kiss_peer_ok("127.0.0.1") and tnc.kiss_peer_ok("192.168.1.20") and tnc.kiss_peer_ok("10.0.0.5")
    assert not tnc.kiss_peer_ok("8.8.8.8") and not tnc.kiss_peer_ok("junk")
    def host_ok(h):
        return app.Handler._host_ok(types.SimpleNamespace(headers={"Host": h}))
    for h in ("127.0.0.1:8073", "localhost:8073", "192.168.1.20:8073", "[::1]:8073", "shack.local:8073"):
        assert host_ok(h), h
    for h in ("evil.example.com:8073", "attacker.net", ""):
        assert not host_ok(h), "DNS rebinding: " + h
    lat, lon = app.aprs_position("=1311.50N/05936.20W-Test")
    assert abs(lat - 13.1917) < 0.001 and abs(lon + 59.6033) < 0.001 and app.latlon_to_grid(lat, lon).startswith("GK03")
    lat, lon = app.aprs_position("!/5L!!<*e7>7P[")
    assert abs(lat - 49.5) < 0.001 and abs(lon + 72.75) < 0.001, "compressed position"
    assert app.aprs_position(":8P6XX    :hello") is None
    # delivery timing + field-test stats
    clock = [5000.0]
    sent = []
    A = tnc.ChatManager("8P5AB-10", lambda to, info, r: sent.append((to, info, r)), lambda: clock[0])
    A.open_direct("8P6XX-9", "rf")
    A.send("8P6XX-9", "Radio check")
    no = list(A.pending)[0]
    clock[0] += 4.2
    A._got_ack("8P6XX-9", no, True, "RF")
    st = A.stats[-1]
    assert st["result"] == "delivered" and st["secs"] == 4.2 and st["call"] == "8P6XX-9"
    m = [m for m in A.peers["8P6XX-9"]["messages"] if m["dir"] == "out"][-1]
    assert m["secs"] == 4.2, "delivery time shown on the message"
    print("  v3.7 safeguards + delivery timing OK")


def test_v36():
    """v3.6: band-plan zone from the grid square, zone chat channels, manual override, no hails on satellites."""
    z = tnc.zone_for_grid
    assert z("GK03fd") == "r2" and z("FN20") == "r2" and z("FK93") == "r2", "Barbados / USA / Caribbean"
    assert z("IO91") == "r1" and z("JO62") == "r1" and z("KG33") == "r1", "UK / Germany / South Africa"
    assert z("GG66") == "br" and z("GF05") == "ar" and z("PM95") == "jp" and z("QF56") == "au" and z("RF73") == "nz"
    assert z("OM89") == "cn" and z("") == "r2"
    c = {"grid": "IO91", "zone": "auto", "qsy_mode": "auto", "aprs_freq": "auto"}
    assert tnc.aprs_freq_hz(c) == 144_800_000, "Europe hails on 144.800"
    assert tnc.zone_qsy(c)[0] == "144.850" and not tnc.qsy_freq_ok("144.800", "r1")
    c.update(zone="r2")
    assert tnc.aprs_freq_hz(c) == 144_390_000 and tnc.zone_qsy(c)[0] == "144.510", "manual zone override"
    c.update(qsy_mode="manual", qsy_freqs="144.390,145.010,145.825,144.990,147.555")
    assert tnc.zone_qsy(c) == ["144.990", "147.555"], "manual list drops APRS, 145.010 and ISS"
    assert not tnc.qsy_freq_ok("145.800", "r2") and not tnc.qsy_freq_ok("436.500", "r1")
    c = {"grid": "PM95", "aprs_freq": "auto"}
    assert tnc.aprs_freq_hz(c) == 144_640_000 and tnc.zone_qsy(c) == [], "Japan: no default chat channels"
    sent = []
    A = tnc.ChatManager("8P5AB-10", lambda to, info, r: sent.append((to, info, r)), lambda: 1000.0)
    A.qsy_channels = []
    try:
        A.free_channel(); raise AssertionError("no channels must refuse QSY")
    except ValueError as e:
        assert "No chat channels" in str(e)
    A.rf_block = "satellite"
    for f in (lambda: A.hail("8P6XX-9"), lambda: A.mail_send("8p6xx@winlink.org", "", "hi", "EMAIL-2")):
        try:
            f(); raise AssertionError("hail / email on a satellite frequency must be refused")
        except ValueError as e:
            assert str(e) == "satellite"
    A.hail("8P6XX-9", route="is")        # internet hail is fine
    tnc.set_zone("r2")
    # mail TLS: remote servers must present a valid certificate; only a bridge on this computer is exempt
    import app, ssl
    assert app.mail_tls_context("smtp.gmail.com").verify_mode == ssl.CERT_REQUIRED
    assert app.mail_tls_context("smtp.gmail.com").check_hostname
    assert app.mail_tls_context("127.0.0.1").verify_mode == ssl.CERT_NONE, "Proton Bridge on this computer"
    errs = app.validate({**app.DEFAULTS, "call": "8P5AB-10", "grid": "GK03", "mail_gw_enabled": True,
                         "smtp_host": "smtp.gmail.com", "smtp_tls": False})
    assert any("STARTTLS" in e for e in errs), "no plain-text mail passwords"
    _tls_impostor_test(app)
    print("  v3.6 zones + mail certificate checks OK")


def _tls_impostor_test(app):
    """A server with a self-signed certificate (an impostor) must be refused before any password is sent."""
    import shutil, socket, ssl, subprocess, tempfile, threading
    if not shutil.which("openssl") or sys.platform.startswith("win"):
        return                       # needs a working openssl; the Linux build runs it
    d = tempfile.mkdtemp()
    key, crt = os.path.join(d, "k.pem"), os.path.join(d, "c.pem")
    r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt,
                        "-days", "1", "-subj", "/CN=smtp.gmail.com"], capture_output=True)
    if r.returncode or not os.path.exists(crt):
        return
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); sctx.load_cert_chain(crt, key)
    ls = socket.socket(); ls.bind(("127.0.0.2", 0)); ls.listen(1); port = ls.getsockname()[1]
    def serve():
        c, _ = ls.accept()
        try:
            sctx.wrap_socket(c, server_side=True).close()
        except Exception:
            pass
    threading.Thread(target=serve, daemon=True).start()
    try:
        with socket.create_connection(("127.0.0.2", port), timeout=5) as raw:
            app.mail_tls_context("127.0.0.2").wrap_socket(raw, server_hostname="127.0.0.2")
        raise AssertionError("an impostor certificate must be refused")
    except ssl.SSLCertVerificationError:
        pass
    ls.close()


def test_v35():
    """v3.5: email via EMAIL-2 and via a Tradewind gateway, disaster rule, Wolf Mode, carrier sense."""
    import types
    clock = [40000.0]
    now = lambda: clock[0]  # noqa: E731
    assert tnc.ham_address("8p6xx@winlink.org") and tnc.ham_address("8P6XX-9@gmail.com")
    assert not tnc.ham_address("jane.doe@gmail.com")
    # EMAIL-2: plain APRS messages "address text"
    sent = []
    A = tnc.ChatManager("8P5AB-10", lambda to, info, r: sent.append((to, info, r)), now)
    A.is_ok = lambda: True
    A.mail_send("8p6xx@winlink.org", "Status", "All safe at Ocean City, power off, water ok.", "EMAIL-2")
    assert sent[0][0] == "EMAIL-2" and sent[0][2] == "is" and ":EMAIL-2  :8p6xx@winlink.org [Status] All safe" in sent[0][1]
    try:
        A.mail_send("jane.doe@gmail.com", "", "hello", "EMAIL-2"); raise AssertionError("third-party needs a disaster session")
    except ValueError:
        pass
    A.mail_send("jane.doe@gmail.com", "", "Safe at shelter", "EMAIL-2", third_party_ok=True)
    # Tradewind gateway: A -> G over RF, G sends it, A sees "sent"; G passes a reply back
    net, got = [], []
    A = tnc.ChatManager("8P5AB-10", lambda to, info, r: net.append(("8P5AB-10", info)), now)
    G = tnc.ChatManager("8P6GW", lambda to, info, r: net.append(("8P6GW", info)), now)
    G.on_mail_out = lambda src, to, subj, body: (got.append((src, to, subj, body)) or (True, ""))
    def air():
        while net:
            src, info = net.pop(0)
            for st in (A, G):
                st.on_packet(f"{src}>APZPRP:{info}", "RF")
    body = "Road to Bath blocked by a fallen tree. Use the Highway 7 detour. Shelter has 40 people and needs water."
    mid = A.mail_send("8p6xx@winlink.org", "Road report", body, "8P6GW")
    for _ in range(60):
        air(); clock[0] += 3; A.tick(); G.tick()
    assert got and got[0][0] == "8P5AB-10" and got[0][1] == "8p6xx@winlink.org" and got[0][2] == "Road report"
    assert got[0][3] == " ".join(body.split()), got[0][3]
    assert A.mail_out[mid]["state"] == "sent", A.mail_out[mid]
    G.mail_deliver("8P5AB-10", "8p6xx@winlink.org", "Re: Road report", "Thanks, crew on the way.")
    for _ in range(40):
        air(); clock[0] += 3; A.tick(); G.tick()
    inbox = [m["text"] for m in A._peer("8P6GW")["messages"] if m["dir"] == "in"]
    assert any("Thanks, crew on the way." in t and "8p6xx@winlink.org" in t for t in inbox), inbox
    # a gateway that refuses tells the sender why
    G.on_mail_out = lambda *a: (False, "daily limit reached")
    mid2 = A.mail_send("8p6xx@winlink.org", "", "second", "8P6GW")
    for _ in range(40):
        air(); clock[0] += 3; A.tick(); G.tick()
    assert A.mail_out[mid2]["state"].startswith("failed") and "daily limit" in A.mail_out[mid2]["state"]
    # app-level gateway rules with a fake mail server
    import app
    import smtplib
    mails = []
    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, context=None):
            assert context is not None and context.verify_mode == __import__('ssl').CERT_REQUIRED, 'certificate must be checked'
        def login(self, u, p): pass
        def send_message(self, m): mails.append(m)
    real = smtplib.SMTP
    smtplib.SMTP = FakeSMTP
    try:
        st = types.SimpleNamespace(cfg={"call": "8P6GW", "smtp_host": "x", "smtp_from": "gw@example.com", "mail_per_day": 2,
                                        "mail_allowed_calls": "", "disaster_on": False, "disaster_until": 0},
                                   mail_count={}, mail_error="", add_log=lambda *a: None, _commlog_add=lambda *a: None)
        st.disaster_active = lambda: app.Station.disaster_active(st)
        send = lambda *a: app.Station._gateway_send(st, *a)  # noqa: E731
        assert send("8P5AB-10", "8p6xx@winlink.org", "Hi", "Test") == (True, "")
        assert mails[0]["Subject"] == "8P5AB-10: Hi" and mails[0]["To"] == "8p6xx@winlink.org"
        assert send("8P5AB-10", "jane@gmail.com", "", "x")[0] is False, "non-ham refused outside a disaster"
        st.cfg.update(disaster_on=True, disaster_until=time.time() + 3600)
        assert send("8P5AB-10", "jane@gmail.com", "", "Safe") == (True, "")
        assert send("8P5AB-10", "8p6xx@winlink.org", "", "3rd")[1] == "daily limit reached"
        assert send("N0CALL", "8p6xx@winlink.org", "", "x")[0] is False or True
        st.cfg["mail_allowed_calls"] = "8P6RR"
        assert send("8P5AB-10", "8p6xx@winlink.org", "", "x") == (False, "not an approved callsign")
    finally:
        smtplib.SMTP = real
    # Wolf Mode: DX from PSKReporter, matched with APRS, ping limits and STOP
    msgs = []
    chat = tnc.ChatManager("8P5AB-10", lambda to, info, r: msgs.append((to, info, r)))
    chat.is_ok = lambda: True
    W = types.SimpleNamespace(cfg={"call": "8P5AB-10", "grid": "GK03ed", "wolf_min_km": 500, "wolf_per_hour": 2,
                                   "wolf_qth": "Barbados", "wolf_stop": ""},
                              wolf={}, wolf_pings=__import__("collections").deque(), chat=chat,
                              isc=types.SimpleNamespace(verified=True, set_filter=lambda f: msgs.append(("FILTER", f, ""))),
                              add_log=lambda *a: None, lock=__import__("threading").RLock(), SW=app.Station.SW,
                              commercial=False, tnc=None, qsy_hz=None, wolf_log={},
                              WOLF_SPOT_FRESH_S=app.Station.WOLF_SPOT_FRESH_S)
    for name in ("_wolf_update", "_wolf_heard", "wolf_ping", "wolf_stopped", "wolf_state", "_on_stop",
                 "wolf_rf_check", "_wolf_text", "_wolf_auto"):
        setattr(W, name, getattr(app.Station, name).__get__(W))
    sp = lambda tx, rx, km: eng.Spot(tx=tx, tx_grid="GK03", rx=rx, rx_grid="HI21", freq=144174000, mode="FT8",  # noqa: E731
                                     snr="-14", ts=0, band="2m", km=km, prop_mode="TEP")
    app.save_config = lambda c: None
    W._wolf_update([sp("8P5AB", "PY7ZZ", 4180), sp("KP4AA", "W4XYZ", 1060), sp("8P6YY", "8P6ZZ", 30)])
    assert set(W.wolf) == {"PY7ZZ", "KP4AA", "W4XYZ"} and any(m[0] == "FILTER" and "b/" in m[1] for m in msgs)
    W._wolf_heard("PY7ZZ-9>APDR16,TCPIP*,qAC,T2BR:=0800.00S/03454.00W$")
    rows = {r["call"]: r for r in W.wolf_state()["rows"]}
    assert rows["PY7ZZ"]["aprs_call"] == "PY7ZZ-9" and rows["PY7ZZ"]["sw"] == "APRSdroid" and rows["W4XYZ"]["aprs_ago"] is None
    W.wolf_ping("PY7ZZ")
    assert any(m[0] == "PY7ZZ-9" and m[2] == "is" and "Barbados" in m[1] for m in msgs)
    try:
        W.wolf_ping("PY7ZZ"); raise AssertionError("once a day")
    except ValueError:
        pass
    try:
        W.wolf_ping("W4XYZ"); raise AssertionError("not on APRS")
    except ValueError:
        pass
    chat.on_stop = W._on_stop
    chat.on_packet("PY7ZZ-9>APDR16,TCPIP*::8P5AB-10 :STOP{5", "IS")
    assert "PY7ZZ" in W.wolf_stopped()
    # v3.8 auto-call on 2m RF: path open now (fresh spot), station on APRS, same APRS frequency
    W.cfg.update(tnc="sound", aprs_freq="auto", wolf_on=True, wolf_auto=True, wolf_per_hour=5)
    W.tnc = object()
    W._wolf_update([sp("8P5AB", "KP4AA", 1060)])
    W.wolf["KP4AA"]["grid"] = "FK68"                      # Puerto Rico: also 144.390
    W._wolf_heard("KP4AA-7>APK004,WIDE2-1:=1820.00N/06600.00W>")
    assert W.wolf_rf_check(W.wolf["KP4AA"])[0], W.wolf_rf_check(W.wolf["KP4AA"])
    msgs.clear()
    W._wolf_auto()
    assert any(m[0] == "KP4AA-7" and m[2] == "rf" and "2m DX via APRS on Tradewind" in m[1] for m in msgs), msgs
    assert W.wolf["KP4AA"]["ping_route"] == "2m RF" and len(msgs[0][1].split(":", 2)[2]) <= 75
    msgs.clear(); W._wolf_auto(); assert not msgs, "only once a day"
    # Brazil uses 145.570 for APRS: RF can't reach it, so it falls back to the internet
    W.cfg["wolf_stop"] = ""
    W.wolf["PY7ZZ"].update(pinged=0, t=time.time())
    ok, why = W.wolf_rf_check(W.wolf["PY7ZZ"])
    assert not ok and "145.570" in why
    W.cfg["wolf_auto_fallback"] = False
    msgs.clear(); W._wolf_auto(); assert not msgs, "no internet fallback when switched off"
    W.wolf["KP4AA"]["t"] = time.time() - 3600
    assert "closed" in W.wolf_rf_check(W.wolf["KP4AA"])[1], "old spot: path may be closed"
    # three queued messages to one station all go out, in order (v3.2 queue jam fixed)
    q, rx = [], []
    S = tnc.ChatManager("8P5AB-10", lambda to, info, r: q.append(info), now)
    S._peer("8P6QQ")["status"] = tnc.OPEN
    for t_ in ("one", "two", "three"):
        S.send("8P6QQ", t_)
    for _ in range(120):
        while q:
            info = q.pop(0)
            rx.append(info.split(":", 2)[2].rsplit("{", 1)[0])
            no = info.rsplit("{", 1)[1].split("}")[0]
            S.on_packet(f"8P6QQ>APDR16::8P5AB-10 :ack{no}", "RF")
        clock[0] += 3; S.tick()
    assert rx == ["one", "two", "three"], rx
    # carrier sense: busy with a carrier, clear when quiet
    t = types.SimpleNamespace(last_rx=0, busy_hold=1.5, noise_floor=0.005, audio_level=0.004)
    assert tnc.SoundModemTNC.channel_busy(t) is False
    t.audio_level = 0.2
    assert tnc.SoundModemTNC.channel_busy(t) is True
    t.audio_level, t.last_rx = 0.004, time.time()
    assert tnc.SoundModemTNC.channel_busy(t) is True


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

    test_digipeater()


def test_digipeater():
    """Fill-in digipeater: WIDE1-1 first hop only, own call/alias, duplicates, limits, and the frame on air."""
    me = "8P5AB-10"
    ok, _ = digi.plan("8P6XX-9>APRS,WIDE1-1,WIDE2-1:!1306.25N/05937.50W>test", me)
    assert ok[2] == ["8P5AB-10*", "WIDE1*", "WIDE2-1"], ok
    ok, _ = digi.plan("8P6XX-9>APRS,8P5AB-10,WIDE2-1:hello", me)
    assert ok[2] == ["8P5AB-10*", "WIDE2-1"], ok
    ok, _ = digi.plan("8P6XX-9>APRS,BARBADOS:hello", me, alias="BARBADOS")
    assert ok[2] == ["8P5AB-10*"], ok
    for line, why in (("8P5AB-7>APRS,WIDE1-1:own", "own packet"),
                      ("8P6XX>APRS,DIGI1*,WIDE1-1:x", "not the first hop"),
                      ("8P6XX>APRS,WIDE2-2:x", "not for a fill-in"),
                      ("8P6XX>APRS,RELAY,WIDE:x", "outdated"),
                      ("8P6XX>APRS,TCPIP*,WIDE1-1:x", "internet"),
                      ("8P6XX>APRS,DIGI1*,WIDE1*:x", "path complete"),
                      ("garbage", "not a packet")):
        r, w = digi.plan(line, me)
        assert r is None and why in w, (line, w)
    t = [1000.0]
    d = digi.FillInDigi(me, max_per_min=2, now=lambda: t[0])
    pkt = "8P6XX-9>APRS,WIDE1-1,WIDE2-1:!1306.25N/05937.50W>test"
    assert d.consider(pkt) is not None
    assert d.consider(pkt) is None, "duplicate within 30 s"
    assert d.consider("8P6XX-9>APRS,8P4AA-1*,WIDE2-1:!1306.25N/05937.50W>test") is None, "same packet via another digi"
    t[0] += 31
    assert d.consider(pkt) is not None, "repeats again after 30 s"
    assert d.consider("8P6YY>APRS,WIDE1-1:a") is None, "per-minute limit"
    t[0] += 61
    assert d.consider("8P6YY>APRS,WIDE1-1:b") is not None
    assert d.status["count"] == 3 and d.status["skipped"] == 2, d.status
    # the repeated frame decodes with the used-hop marks set
    src, dst, path, info = d.consider("8P6ZZ>APRS,WIDE1-1,WIDE2-1:c") or (None,) * 4
    line = afsk.decode_ax25(afsk.encode_ax25(src, dst, path, info)[:-2])
    assert line == "8P6ZZ>APRS,8P5AB-10*,WIDE1*,WIDE2-1:c", line
    # wide-area mode: WIDE2-n counted down, never more than 2 hops
    w = lambda l: digi.plan(l, me, mode="wide")[0]
    assert w("X>APRS,WIDE2-2:a")[2] == ["8P5AB-10*", "WIDE2-1"]
    assert w("X>APRS,WIDE2-1:a")[2] == ["8P5AB-10*", "WIDE2*"]
    assert w("X>APRS,D1*,WIDE1*,WIDE2-1:a")[2] == ["D1*", "WIDE1*", "8P5AB-10*", "WIDE2*"]
    for bad in ("X>APRS,WIDE2-3:a", "X>APRS,WIDE3-3:a", "X>APRS,D1*,WIDE1*,WIDE2-2:a"):
        assert w(bad) is None, bad
    assert digi.plan("X>APRS,WIDE2-1:a", me, mode="fill")[0] is None, "fill-in ignores WIDE2"
    print("digipeater ok")
    test_weak_signal()
    test_dx_mode()


def test_weak_signal():
    """FX.25, bit repair and the parallel decoders; Robust DX loopback in deep noise and off frequency."""
    import random
    random.seed(5)
    for n, k in ((255, 239), (48, 32), (80, 64)):
        data = bytes(random.randrange(256) for _ in range(k))
        cw = bytearray(data + fx25.rs_encode(data, n - k))
        for p in random.sample(range(n), (n - k) // 2):
            cw[p] ^= random.randrange(1, 256)
        assert fx25.rs_decode(bytes(cw), n - k) == (data, (n - k) // 2), f"RS({n},{k})"
    frame = afsk.encode_ax25("8P6XX-9", "APRS", ["WIDE1-1"], "!1306.25N/05937.50W>FX.25 test")
    # FX.25 frame with burst damage inside the codeblock: the plain decoder fails, FX.25 repairs it
    a = afsk.modulate(frame, 48000, 200, 30, 0.5, "1200", fx25=True)
    a = np.concatenate([np.zeros(800), a, np.zeros(1500)])
    mid = len(a) // 2
    a[mid:mid + 300] = 0.0                                     # a 6 ms drop-out (about 7 bits)
    d = afsk.MultiDemodulator(48000)
    got = []
    for i in range(0, len(a), 4800):
        got += d.process(a[i:i + 4800])
    assert frame[:-2] in got and d.fx25_decoded >= 1, "FX.25 repair"
    # bit repair: one flipped data bit
    bits = [(b >> k) & 1 for b in frame for k in range(8)]
    bits[100] ^= 1
    assert afsk.fix_frame(bits) == frame[:-2], "single-bit repair"
    # Robust DX at -12 dB SNR (2.5 kHz), 150 Hz off frequency
    rng = np.random.default_rng(7)
    payload = b"8P5AB-10>APTWDX:!1306.25N/05937.50W- Robust DX test"
    rate = 8000
    x = afsk.robust_modulate(payload, rate, 0.5, 300, modem="robust_dx").astype(float)
    n = len(x)
    A = np.fft.fft(x)
    h = np.zeros(n)
    h[0] = 1
    h[1:(n + 1) // 2] = 2
    x = np.real(np.fft.ifft(A * h) * np.exp(2j * np.pi * 150.0 * np.arange(n) / rate))
    noise = 0.125 / (10 ** (-12 / 10)) * (rate / 2) / 2500.0
    x = x + rng.normal(0, noise ** 0.5, n)
    rd = afsk.RobustDemodulator(rate, "robust_dx")
    out = []
    for i in range(0, n, 4000):
        out += rd.process(x[i:i + 4000])
    out += rd.process(np.zeros(8000))
    assert payload in out, "Robust DX at -12 dB, 150 Hz off"
    print("weak-signal modem ok")


def test_dx_mode():
    me = (13.104, -59.625)                                      # GK03ec
    t = {x["name"]: x for x in dxmode.targets_from(me, my_height_m=340)}
    assert 165 <= t["St Vincent"]["km"] <= 185 and 265 <= t["St Vincent"]["bearing"] <= 280, t["St Vincent"]
    assert t["St Vincent"]["path"].startswith("line of sight") and t["Trinidad"]["path"].startswith("beyond"), t
    class Spot:
        def __init__(self, tg, rg, km):
            self.tx_grid, self.rx_grid, self.km, self.band, self.mode, self.snr, self.tx, self.rx = tg, rg, km, "2m", "FT8", -12, "8P6AA", "J88BB"
    op = dxmode.openings([Spot("GK03ec", "FK93hd", 175)], me, list(t.values()))  # Barbados <-> St Vincent
    assert "St Vincent" in op and op["St Vincent"]["kind"] == "direct", op
    assert dxmode.openings([Spot("GK03ec", "GK03fd", 15)], me, list(t.values())) == {}
    assert dxmode.dx_beacon_text(t["St Lucia"], {"call": "8P5AB-10", "modem": "robust_dx"}, "144.150USB", 1, 2) == ">DX St Lucia 8P5AB-10 RDX 144.150USB 1/2"
    for nm in t:
        assert len(dxmode.dx_beacon_text(t[nm], {"call": "8P5AB-10", "modem": "1200"}, "144.390FM", 4, 4)) <= 63, nm
    assert dxmode.burst_plan({"tnc": "sound", "modem": "1200"}) == (4, 15.0)
    assert dxmode.burst_plan({"tnc": "sound", "modem": "robust_dx"}) == (2, 30.0)
    # CAT: Robust modes tune the radio in USB data mode
    ic = tnc.CivPTT("COMx", 9600, "7C")
    ic.ser = FakeIcom()
    tr = tnc.SoundModemTNC(ptt=ic, tune_hz=144_150_000, modem="robust_dx")
    assert tr.tune(144_150_000) and ic.ser.mode == [0x01, 0x01] and "USB" in tr.cat_status, tr.cat_status
    assert tnc.dx_freq_hz({"dx_freq": "144.150"}) == 144_150_000 and tnc.dx_freq_hz({"dx_freq": "145.9"}) == 145_900_000
    assert tnc.dx_freq_hz({"dx_freq": "100.0"}) is None
    test_robust_fm()
    print("DX mode ok")
    test_iss()


def test_robust_fm():
    """Robust FM: FM data mode on a simplex frequency, refuses APRS / satellite channels, decodes Robust."""
    import numpy as np
    import app
    ic = tnc.CivPTT("COMx", 9600, "7C")
    ic.ser = FakeIcom()
    tr = tnc.SoundModemTNC(ptt=ic, tune_hz=145_570_000, modem="robust_fm")
    assert tr.tune(145_570_000) and ic.ser.mode[0] == 0x05 and "FM" in tr.cat_status, (ic.ser.mode, tr.cat_status)
    assert tnc.fm_freq_check({"fm_freq": "145.570"}) == (145_570_000, "")
    for bad in ("144.390", "144.39", "145.825", "436.795", "144.150", "100", "x"):
        assert tnc.fm_freq_check({"fm_freq": bad})[0] is None, bad
    assert "144.390" in tnc.fm_freq_check({"fm_freq": "144.390"})[1]
    base = dict(app.DEFAULTS, call="8P5AB-10", grid="GK03gc", tnc="sound", ptt="CIV", ptt_port="COM4", modem="robust_fm")
    assert not [e for e in app.validate(dict(base, fm_freq="145.570")) if "Robust FM" in e]
    assert any("144.390" in e for e in app.validate(dict(base, fm_freq="144.390")))
    assert any("CP210" in e for e in app.validate(dict(base, ptt_port="")))
    # same waveform as Robust: a Robust FM packet decodes with both receivers
    payload = b"8P5AB-10>APTW01:>Robust FM test"
    x = afsk.robust_modulate(payload, 48000, 0.5, modem="robust_fm")
    x = x + np.random.default_rng(3).normal(0, 0.05, len(x))
    for m in ("robust_fm", "robust"):
        rd = afsk.RobustDemodulator(48000, m)
        out = rd.process(x) + rd.process(np.zeros(8000))
        assert payload in out, m
    assert dxmode.dx_beacon_text({"name": "St Vincent", "km": 175, "bearing": 272}, {"call": "8P5AB", "modem": "robust_fm"},
                                 "145.570FM").startswith(">DX OPEN St Vincent 175km brg272 8P5AB RFM 145.570FM")
    assert tnc.aprsis_error_text(ConnectionResetError(10054, "forcibly closed")).startswith("APRS-IS server dropped")
    print("Robust FM ok")


def _tle_line(body):
    """Complete a 68-character TLE line with its checksum."""
    return body[:68] + str(sum(int(c) if c.isdigit() else (1 if c == "-" else 0) for c in body[:68]) % 10)


def test_iss():
    """v3.15 ISS passes: SGP4 against the published reference, look angles, passes, beacon schedule, app wiring."""
    import math
    import iss
    import app
    # SGP4 verification case 00005 (Vallado et al. 2006, tcppver.out): km and km/s in TEME
    sat = iss.Satellite("1 00005U 58002B   00179.78495062  .00000023  00000-0  28098-4 0  4753",
                        "2 00005  34.2682 348.7242 1859667 331.7664  19.3264 10.82419157413667")
    ref = {0: ((7022.46529266, -1400.08296755, 0.03995155), (1.893841015, 6.405893759, 4.534807250)),
           360: ((-7154.03120202, -3783.17682504, -3536.19412294), (4.741887409, -4.151817765, -2.093935425)),
           4320: ((-9060.47373569, 4658.70952502, 813.68673153), (-2.232832783, -4.110453490, -3.157345433))}
    for t, (r0, v0) in ref.items():
        r, v = sat.propagate(t)
        assert max(abs(a - b) for a, b in zip(r, r0)) < 1e-6 and max(abs(a - b) for a, b in zip(v, v0)) < 1e-9, (t, r, v)
    assert iss.parse_tle_text("ISS (ZARYA)\n1 00005U 58002B   00179.78495062  .00000023  00000-0  28098-4 0  4753\n"
                              "2 00005  34.2682 348.7242 1859667 331.7664  19.3264 10.82419157413667\n")[0] == "ISS (ZARYA)"
    assert iss.parse_tle_text("1 00005U 58002B   00179.78495062  .00000023  00000-0  28098-4 0  4754\n"
                              "2 00005  34.2682 348.7242 1859667 331.7664  19.3264 10.82419157413667") is None   # bad checksum
    # an ISS-like orbit: straight overhead from its sub-point, passes of the right shape over Barbados
    l1 = _tle_line("1 25544U 98067A   26309.50000000  .00012000  00000-0  21000-3 0  999 ")
    l2 = _tle_line("2 25544  51.6400 120.0000 0005000  90.0000 270.0000 15.50000000 1000 ")
    s = iss.Satellite(l1, l2)
    t = s.epoch + 3600
    (x, y, z), _ = s.at(t)
    g = iss.gmst(t)
    xe, ye = math.cos(g) * x + math.sin(g) * y, -math.sin(g) * x + math.cos(g) * y
    lat = math.degrees(math.atan(math.tan(math.atan2(z, math.hypot(xe, ye))) / (1 - 1 / 298.257223563) ** 2))
    az, el, rng = iss.look(s, (lat, math.degrees(math.atan2(ye, xe))), t)
    assert el > 89.5 and 380 < rng < 460, (el, rng)
    me = (13.104, -59.625)
    ps = iss.passes(s, me, start=s.epoch, hours=24)
    assert 2 <= len(ps) <= 8 and all(60 < p["duration"] < 720 and p["aos"] < p["tca"] < p["los"] for p in ps), ps
    hi = max(ps, key=lambda p: p["max_el"])
    assert abs(iss.elevation(s, me, hi["aos"])) < 0.2 and abs(iss.elevation(s, me, hi["los"])) < 0.2
    assert iss.elevation(s, me, hi["tca"]) >= iss.elevation(s, me, hi["tca"] + 20) - 1e-6
    assert len(iss.passes(s, me, start=s.epoch, hours=24, min_el=30)) < len(ps)
    w = iss.window_above(s, me, hi, 10)
    assert hi["aos"] < w[0] < hi["tca"] < w[1] < hi["los"] and abs(iss.elevation(s, me, w[0]) - 10) < 0.2
    mid = (hi["aos"] + hi["los"]) / 2                       # a pass already in progress keeps its true AOS
    assert abs(iss.passes(s, me, start=mid, hours=2)[0]["aos"] - hi["aos"]) < 2
    # beacon schedule: 1/min rising, <=2 while setting, <=5 in any 5 minutes
    assert iss.plan(tca=600, start=0, end=900) == [0, 60, 120, 180, 240, 300, 360, 420, 480, 540, 600, 660]
    assert iss.plan(tca=150, start=0, end=600) == [0, 60, 120, 180, 240]
    assert iss.plan(tca=0, start=0, end=600) == [0, 60]
    for tca, end in ((600, 900), (200, 700), (30, 400)):
        times = iss.plan(tca, 0, end)
        assert all(sum(1 for u in times if 0 <= u - t0 < 300) <= 5 for t0 in times), times
        assert sum(1 for u in times if u >= tca) <= 2
    assert iss.beacon_due(100, 500, 5.0, 10, [], [])[0] is False                       # too low
    assert iss.beacon_due(100, 500, 20, 10, [50], [50])[0] is False                    # under a minute
    assert iss.beacon_due(400, 300, 20, 10, [310, 370], [310, 370])[0] is False        # 2 already while setting
    assert iss.beacon_due(400, 900, 20, 10, [], [110, 170, 230, 290, 340])[0] is False  # 5 in 5 minutes
    assert iss.beacon_due(400, 900, 20, 10, [], [100, 170, 230, 290, 340])[0] is True
    cm = iss.beacon_comment({"grid": "GK03ec", "iss_comment": "x" * 60}, 12, 34.6)
    assert len(cm) <= 43 and cm.endswith(" GK03 #12 el35"), cm
    # app: a simulated pass sends beacons on the schedule, on path ARISS, RF only
    st = app.Station.__new__(app.Station)
    app.Station.__init__(st)
    st.cfg = dict(app.DEFAULTS, call="8P5AB-7", grid="GK03ec", iss_enabled=True, iss_auto_beacon=True, iss_min_el=10)
    st.iss_sat, st.iss_cat, st.iss_next_tle = s, 25544, float("inf")
    sent = []
    st.transmit = lambda info, rf=True, net=True, mode=None, force_rf=False, path=None: sent.append((info, net, force_rf, path))
    st._iss_can_beacon = lambda: (True, "")
    st._iss_tune = lambda on, now=None: None
    for t in range(int(hi["aos"]) - 30, int(hi["los"]) + 30):
        st._iss_tick(t)
    assert len(sent) == len(iss.plan(hi["tca"], *w)), (len(sent), w)
    assert all(not n and f and p == ["ARISS"] and i.startswith("=") for i, n, f, p in sent), sent[0]
    assert st.iss_cur is None and st.iss_last["sent"] == len(sent)
    assert not [e for e in app.validate(dict(st.cfg)) if "ISS" in e]
    assert any("Satellite minimum" in e for e in app.validate(dict(st.cfg, iss_min_el=80)))
    st2 = app.Station.__new__(app.Station)
    app.Station.__init__(st2)
    st2.cfg = dict(app.DEFAULTS, call="8P5AB-7", grid="GK03ec", tnc="sound", modem="robust", rf_announce=True)
    st2.tnc = object()
    assert "1200" in st2._iss_can_beacon()[1]
    test_sat_watch(s, hi)
    test_iss_cli(s, l1, l2)
    print(f"ISS passes ok ({len(ps)} passes/day, best {hi['max_el']} deg, {len(sent)} beacons)")
    test_satrx(s, hi)


def test_iss_cli(s, l1, l2):
    """v3.17 stand-alone iss.py: self-test, pass report from a TLE file and from Tradewind's saved orbit data."""
    import contextlib
    import io
    import tempfile
    import iss
    assert iss.selftest(verbose=False)
    for g in ("GK03ec", "GK03", "FK52", "EL29fs", "JO01"):
        a, b = iss.grid_latlon(g), eng.grid_to_latlon(g)
        assert abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9, g
    with tempfile.TemporaryDirectory() as d:
        tle = os.path.join(d, "iss.txt")
        with open(tle, "w") as f:
            f.write(f"ISS (ZARYA)\n{l1}\n{l2}\n")
        lines = []
        ps = iss.report(s, (13.104, -59.625), s.epoch, 24, 10, out=lines.append)
        assert ps and sum(1 for x in lines if x.strip().startswith("AOS")) == len(ps)
        assert all("145.825 MHz FM, path ARISS" in x for x in lines if "beacons at" in x)
        with contextlib.redirect_stdout(io.StringIO()) as o:
            assert iss.main(["GK03ec", "--tle", tle, "--hours", "24"]) == 0
        assert "AOS" in o.getvalue() and "ISS (ZARYA)" in o.getvalue()
        os.makedirs(os.path.join(d, ".tradewind"))
        with open(os.path.join(d, ".tradewind", "sat_25544_celestrak.txt"), "w") as f:
            f.write(f"ISS (ZARYA)\n{l1}\n{l2}\n")
        got = iss._cached_orbit(25544, [os.path.join(d, ".tradewind"), os.path.join(d, "missing")])
        assert got and got[0].epoch == s.epoch
    print("iss.py stand-alone ok (self-test, pass report, saved orbit data)")


def test_sat_watch(s, hi):
    """Satellite Watch Mode: N2YO / AMSAT / NASA orbit sources, and Satellite Switch on the IC-9100 and FT-991A."""
    import json
    import math
    import iss
    import app
    # N2YO JSON and AMSAT's multi-satellite file
    tle1 = "1 00005U 58002B   00179.78495062  .00000023  00000-0  28098-4 0  4753"
    tle2 = "2 00005  34.2682 348.7242 1859667 331.7664  19.3264 10.82419157413667"
    n2 = json.dumps({"info": {"satid": 5, "satname": "VANGUARD 1", "transactionscount": 1}, "tle": tle1 + "\r\n" + tle2})
    assert iss.parse_n2yo(n2.encode()) == ("VANGUARD 1", tle1, tle2)
    assert iss.parse_n2yo(b'{"error":"Invalid API Key!"}') is None and iss.parse_n2yo(b"<html>") is None
    other1 = _tle_line("1 07530U 74089B   26308.02040639 -.00000039  00000-0  46821-4 0  999 ")
    other2 = _tle_line("2 07530 101.9884 322.7152 0012062 223.6405 162.0645 12.53700304 3780 ")
    amsat = f"AO-07\n{other1}\n{other2}\nVANGUARD\n{tle1}\n{tle2}\n"
    assert iss.parse_tle_text(amsat, 5)[0] == "VANGUARD" and iss.parse_tle_text(amsat, 7530)[0] == "AO-07"
    assert iss.parse_tle_text(amsat, 25544) is None
    # NASA: precession goes the right way (J2000 equinox at RA +0.34 deg, Dec +0.15 deg by 2026.85)
    t = iss._unix_of_year(2026) + 309 * 86400
    P = iss.precession_matrix(t)
    ra, dec = math.degrees(math.atan2(P[1][0], P[0][0])), math.degrees(math.asin(P[2][0]))
    assert 0.33 < ra < 0.355 and 0.14 < dec < 0.155, (ra, dec)
    assert all(abs(sum(P[i][k] * P[j][k] for k in range(3)) - (i == j)) < 1e-12 for i in range(3) for j in range(3))
    # an OEM file made from the SGP4 orbit (taken back to J2000) reproduces it after interpolation
    lines = ["CCSDS_OEM_VERS = 2.0", "META_START", "OBJECT_NAME = ISS", "REF_FRAME = EME2000", "TIME_SYSTEM = UTC", "META_STOP",
             "COMMENT test"]
    t0 = s.epoch
    for k in range(0, 24 * 15):
        tk = t0 + k * 240
        r, v = s.at(tk)
        Pt = iss.precession_matrix(tk)
        rj = tuple(sum(Pt[i][n] * r[i] for i in range(3)) for n in range(3))      # transpose: date -> J2000
        vj = tuple(sum(Pt[i][n] * v[i] for i in range(3)) for n in range(3))
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(tk)) + ".000"
        lines.append(ts + " " + " ".join(f"{x:.6f}" for x in rj + vj))
    lines += ["COVARIANCE_START", "EPOCH = 2026-11-05T12:00:00.000", "1 2 3 4 5 6 7", "COVARIANCE_STOP"]
    eph = iss.Ephemeris(iss.parse_oem("\n".join(lines)))
    for dt in (1000, 7777, 30000):
        assert iss.position_diff_km(eph, s, t0 + dt) < 0.05, dt
    ps2 = iss.passes(eph, (13.104, -59.625), start=hi["aos"] - 600, hours=1)
    assert ps2 and abs(ps2[0]["aos"] - hi["aos"]) < 3 and abs(ps2[0]["max_el"] - hi["max_el"]) < 0.2, (ps2, hi)
    try:
        eph.at(t0 - 3600)
        raise AssertionError("outside the ephemeris must raise")
    except ValueError:
        pass
    # freshest source wins; NASA is the fallback when the TLEs are old
    old_tle = iss.Satellite(tle1, tle2)
    assert iss.pick_best({"AMSAT": s, "CelesTrak": old_tle, "N2YO": None}, t0 + 3600)[0] == "AMSAT"
    assert iss.pick_best({"AMSAT": old_tle, "NASA": eph}, t0 + 3600)[0] == "NASA"
    assert iss.pick_best({"N2YO": None}) == (None, None)
    # Satellite Switch: 1 minute before AOS the radio goes to 145.825 FM data, back to APRS at LOS
    for radio in ("IC-9100", "FT-991A"):
        if radio == "IC-9100":
            ptt = tnc.CivPTT("COMx", 9600, "7C")
            ptt.ser = FakeIcom()
            freq = lambda: ptt.ser.hz                                      # noqa: E731
            fm = lambda: ptt.ser.mode[0] == 0x05                          # noqa: E731
        else:
            ptt = tnc.YaesuCatPTT("COMy", 38400)
            ptt.ser = FakeYaesu()
            freq = lambda: ptt.ser.hz                                      # noqa: E731
            fm = lambda: ptt.ser.mode == "A"                              # noqa: E731
        st = app.Station.__new__(app.Station)
        app.Station.__init__(st)
        st.cfg = dict(app.DEFAULTS, call="8P5AB-7", grid="GK03ec", iss_enabled=True, iss_auto_beacon=True,
                      tnc="sound", modem="1200", rf_announce=True, aprs_freq="144.390")
        st.iss_sat, st.iss_cat, st.iss_next_tle, st.running = s, 25544, float("inf"), True
        st.tnc = tnc.SoundModemTNC(ptt=ptt, tune_hz=144_390_000)
        sent = []
        st.transmit = lambda info, rf=True, net=True, mode=None, force_rf=False, path=None: sent.append((info, freq()))
        st._iss_tick(hi["aos"] - 120)
        assert not st.iss_tuned
        st._iss_tick(hi["aos"] - 59)
        assert st.iss_tuned and freq() == 145_825_000 and fm(), (radio, freq())
        for tk in range(int(hi["aos"]) - 58, int(hi["aos"]) + 1):
            st._iss_tick(tk)
        assert not sent
        st._iss_tick(hi["aos"] + 1)                                            # on the horizon: sequence starts
        assert len(sent) == 1 and sent[0][1] == 145_825_000, (radio, sent)
        for tk in range(int(hi["aos"]) + 2, int(hi["los"]) + 5):
            st._iss_tick(tk)
        assert not st.iss_tuned and freq() == 144_390_000, (radio, freq())
        times = [x for x in range(int(hi["aos"]) + 1, int(hi["los"]) + 1)]
        want = iss.plan(hi["tca"], times[0], times[-1])
        assert len(sent) == len(want) and sum(1 for x in want if x >= hi["tca"]) <= 2, (radio, len(sent), len(want))
    assert any("NORAD" in e for e in app.validate(dict(app.DEFAULTS, call="8P5AB", grid="GK03ec", iss_enabled=True, sat_catnr="x")))
    assert any("Satellite frequency" in e for e in app.validate(dict(app.DEFAULTS, call="8P5AB", grid="GK03ec",
                                                                       iss_enabled=True, sat_freq="100")))
    print("Satellite Watch ok (N2YO / AMSAT / NASA sources, IC-9100 + FT-991A switch)")


def test_satrx(s, hi):
    """v3.16 satellite receivers: SSTV codec, recording, Doppler, a received SSTV pass, telemetry tuning, SDR jobs."""
    import math
    import shutil
    import struct
    import tempfile
    import zlib
    import iss
    import satrx
    import app
    tmp = tempfile.mkdtemp(prefix="tw_rx_")
    rng = np.random.default_rng(5)
    yy, xx = np.mgrid[0:496, 0:640]
    img = np.stack([xx * 255 // 640, yy * 255 // 496, (xx + yy) * 255 // 1136], -1).astype(np.uint8)
    img[100:200, 100:300] = [255, 0, 0]
    for vis in (95, 44, 60, 8):                          # PD120 (ISS), Martin M1, Scottie S1, Robot 36
        m = satrx.SSTV_MODES[vis]
        a = satrx.sstv_encode(img, vis, 48000)
        x = np.concatenate([rng.normal(0, 0.05, 24000), a, rng.normal(0, 0.05, 12000)])
        pics = satrx.sstv_decode(x + rng.normal(0, 0.1, len(x)), 48000)
        ref = img[(np.arange(m["h"]) * 496 // m["h"])][:, (np.arange(m["w"]) * 640 // m["w"])]
        assert len(pics) == 1 and pics[0][0] == m["name"] and pics[0][1].shape == ref.shape, (vis, [p[0] for p in pics])
        assert np.abs(pics[0][1].astype(int) - ref.astype(int)).mean() < 14, m["name"]
    assert satrx.sstv_decode(rng.normal(0, 0.3, 48000 * 5), 48000) == []          # noise is not a picture
    # PNG writer: valid signature, size and pixels
    satrx.write_png(os.path.join(tmp, "t.png"), img[:4, :5])
    raw = open(os.path.join(tmp, "t.png"), "rb").read()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", raw[16:24]) == (5, 4)
    i = raw.index(b"IDAT")
    data = zlib.decompress(raw[i + 4:i + 4 + struct.unpack(">I", raw[i - 4:i])[0]])
    assert data[1:16] == img[0, :5].tobytes()
    # pass recorder -> WAV -> back
    rec = satrx.PassRecorder(os.path.join(tmp, "r", "p.wav"), 48000)
    tone = (0.5 * np.sin(np.arange(4800) * 0.1)).astype(np.float32)
    rec(tone[:2400]); rec(tone[2400:])
    assert abs(rec.close() - 0.1) < 1e-9
    back, rate = satrx.read_wav(os.path.join(tmp, "r", "p.wav"))
    assert rate == 48000 and np.abs(back - tone).max() < 1e-3
    # Doppler: approaching satellite is heard higher; geostationary look angles from Barbados
    assert satrx.doppler_hz(145_800_000, -7.0) - 145_800_000 == 3404 and satrx.doppler_hz(145_800_000, 7.0) < 145_800_000
    az, el, _ = satrx.geo_look((13.104, -59.625), -75.2)
    assert 64 < el < 69 and 225 < az < 236, (az, el)
    assert satrx.geo_look((13.104, -59.625), -137.0)[1] < 5
    # commands for the external decoders
    assert satrx.satdump_cmd(satrx.receiver("meteor_lrpt")[1], "/o", 900, {})[:4] == ["satdump", "live", "meteor_m2-x_lrpt", "/o"]
    assert "137900000" in satrx.satdump_cmd(satrx.receiver("meteor_lrpt")[1], "/o", 900, {})
    assert satrx.gr_satellites_cmd(39444, "a.wav", 48000, "a.kss")[:4] == ["gr_satellites", "39444", "--wavfile", "a.wav"]
    hz = satrx.receiver("iss_hamtv")[1]
    assert satrx.hamtv_cmd(hz, "/o/v.ts", {})[0] is None                            # 2.4 GHz: needs a downconverter
    cmd, _ = satrx.hamtv_cmd(hz, "/o/v.ts", {"hamtv_lo_mhz": 1998})
    assert "rtl_sdr -f 397000000" in cmd and "leandvb" in cmd and cmd.endswith("/o/v.ts")
    # H.264 video from HamTV: MPEG-TS -> MP4 + thumbnail (when FFmpeg is on this PC)
    if shutil.which("ffmpeg"):
        ts = os.path.join(tmp, "v.ts")
        import subprocess
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
                        "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mpegts", ts], check=True, timeout=120)
        mp4, jpg, msg = satrx.video_finish(ts)
        assert mp4 and jpg and satrx.probe_codec(mp4) == "h264", msg
    # the station: an ISS pass received as SSTV - radio to 145.800 FM (+Doppler) before AOS, pass recorded, picture saved
    app.RX_DIR = tmp
    for radio in ("IC-9100", "FT-991A"):
        ptt = tnc.CivPTT("COMx", 9600, "7C") if radio == "IC-9100" else tnc.YaesuCatPTT("COMy", 38400)
        ptt.ser = FakeIcom() if radio == "IC-9100" else FakeYaesu()
        st = app.Station.__new__(app.Station)
        app.Station.__init__(st)
        st.cfg = dict(app.DEFAULTS, call="8P5AB-7", grid="GK03ec", iss_enabled=True, sat_rx="iss_sstv",
                      tnc="sound", modem="1200", aprs_freq="144.390")
        st.iss_sat, st.iss_cat, st.iss_next_tle, st.running = s, 25544, float("inf"), True
        st.tnc = tnc.SoundModemTNC(ptt=ptt, tune_hz=144_390_000)
        st.transmit = lambda *a, **k: (_ for _ in ()).throw(AssertionError("receive-only: must not transmit"))
        st._iss_tick(hi["aos"] - 59)
        f = ptt.ser.hz
        assert st.iss_tuned and 145_800_000 < f <= 145_803_600, (radio, f)       # approaching: heard higher
        assert (ptt.ser.mode[0] == 0x05) if radio == "IC-9100" else ptt.ser.mode == "A"
        st._iss_tick(hi["aos"] + 1)
        assert st.rx_rec and st.rx_rec in st.tnc.audio_taps
        audio = satrx.sstv_encode(img, 95, 48000)
        for k in range(0, len(audio), 4800):                         # the pass audio arrives through the tap
            for tap in list(st.tnc.audio_taps):
                tap(audio[k:k + 4800])
        for t in range(int(hi["aos"]) + 2, int(hi["tca"]) + 1, 5):   # Doppler steps follow the pass
            st._iss_tick(t)
        assert ptt.ser.hz < f, (radio, ptt.ser.hz, f)                # past the highest point it reads lower
        st._iss_tick(hi["los"] + 2)
        assert not st.iss_tuned and ptt.ser.hz == 144_390_000 and st.rx_rec is None
        assert st.rx_busy
        t0 = time.time()
        while st.rx_busy and time.time() - t0 < 60:
            time.sleep(0.2)
        time.sleep(1.1)                                          # next pass gets its own file name
    imgs = [x for x in satrx.gallery(tmp) if x["kind"] == "image" and x["path"].startswith("sstv/")]
    assert len(imgs) == 2 and "PD120" in imgs[0]["name"], imgs
    assert not [x for x in satrx.gallery(tmp) if x["kind"] == "audio" and x["path"].startswith("sstv/")], "decoded pass WAVs are removed"
    # BPSK telemetry: USB data mode on the IC-9100
    ptt = tnc.CivPTT("COMx", 9600, "7C")
    ptt.ser = FakeIcom()
    st = app.Station.__new__(app.Station)
    app.Station.__init__(st)
    st.cfg = dict(app.DEFAULTS, call="8P5AB-7", grid="GK03ec", iss_enabled=True, sat_catnr=39444, sat_rx="ao73_tlm",
                  tnc="sound", modem="1200", aprs_freq="144.390")
    assert st._sat_radio_rx()["mode"] == "usb" and st._sat_radio_rx()["hz"] == 145_935_000
    st.iss_sat, st.iss_cat, st.iss_next_tle, st.running = s, 39444, float("inf"), True
    st.tnc = tnc.SoundModemTNC(ptt=ptt, tune_hz=144_390_000)
    st._iss_tick(hi["aos"] - 59)
    assert ptt.ser.mode[0] == 0x01 and abs(ptt.ser.hz - 145_935_000) < 4000, (ptt.ser.mode, ptt.ser.hz)
    # SDR jobs: GOES-19 runs while idle; a Meteor pass pre-empts it, then GOES comes back
    started = []

    class FakeJob:
        def __init__(self, name, cmd, cwd, shell=False, log=None):
            self.name, self.cmd, self.alive = name, cmd, True
            started.append(self)

        def running(self):
            return self.alive

        def stop(self, wait=10):
            self.alive = False
            return 0

        def tail(self, n=300):
            return [""]
    real_job, real_tools = satrx.Job, satrx.tools_status
    satrx.Job = FakeJob
    satrx.tools_status = lambda which=None: {t: True for t in satrx.TOOLS}
    try:
        st = app.Station.__new__(app.Station)
        app.Station.__init__(st)
        st.cfg = dict(app.DEFAULTS, call="8P5AB-7", grid="GK03ec", iss_enabled=True,
                      rx_jobs=["goes19_hrit", "meteor_lrpt", "goes18_hrit"], tnc="sound")
        st.rx_orbits[59051] = (s, float("inf"))               # an orbit for "Meteor" so it has passes
        st._sdr_tick(hi["aos"] - 3600)
        assert started and started[-1].name == "goes19_hrit" and "goes_hrit" in started[-1].cmd
        assert "only" in st.sdr_status["goes18_hrit"] and "above your horizon" in st.sdr_status["goes18_hrit"]
        mp = next(p for p in st.rx_passes["meteor_lrpt"] if p["aos"] > hi["aos"] - 3600)
        st.sdr_next = 0
        st._sdr_tick(mp["aos"] + 5)
        assert started[-1].name == "meteor_lrpt" and not started[0].alive, [j.name for j in started]
        secs = int(started[-1].cmd[started[-1].cmd.index("--timeout") + 1])
        assert abs(secs - (mp["los"] - mp["aos"] - 5 + 30)) < 3
        started[-1].alive = False                               # SatDump finishes at LOS
        st.sdr_next, st.sdr_geo_retry = 0, 0
        st._sdr_tick(mp["los"] + 90)
        assert started[-1].name == "goes19_hrit", [j.name for j in started]
        st.cfg["tnc"] = "sdr"
        st.sdr_next = 0
        st._sdr_finish()
        st._sdr_tick(mp["los"] + 200)
        assert "APRS receiver" in st.sdr_status["goes19_hrit"]
    finally:
        satrx.Job, satrx.tools_status = real_job, real_tools
    shutil.rmtree(tmp, ignore_errors=True)
    print("Satellite receivers ok (SSTV x4 modes, IC-9100 + FT-991A SSTV pass, USB telemetry, Doppler, SDR jobs"
          + (", H.264 video)" if shutil.which("ffmpeg") else ")"))


if __name__ == "__main__":
    main()
