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
    if not tnc.crypto_available():
        test_secure()
    else:
        print("note: encryption library missing - commercial-mode tests skipped")
    print("All self-tests passed")


def test_secure():
    key = tnc.derive_key("BDF-NET", "correct horse battery staple")
    assert len(key) == 32, "256-bit key"
    assert key == tnc.derive_key("bdf-net", "correct horse battery staple"), "same name+passphrase = same key"
    assert key != tnc.derive_key("OTHER", "correct horse battery staple"), "network name salts the key"
    try:
        tnc.derive_key("BDF-NET", "short")
        raise AssertionError("short passphrase must be refused")
    except ValueError:
        pass
    clock = [5000.0]
    now = lambda: clock[0]  # noqa: E731
    a, b = tnc.SecureLink(key, now), tnc.SecureLink(key, now)
    sealed = a.seal("RC01", ":RC02     :Shelter at St Philip needs water{4")
    assert "Shelter" not in sealed and sealed.startswith(tnc.SEC_TAG), "content is encrypted"
    assert len(sealed) < 250, "fits one AX.25 frame"
    assert b.open("RC01", sealed) == ":RC02     :Shelter at St Philip needs water{4"
    assert b.open("RC01", sealed) is None, "replayed frame refused"
    assert b.open("XX99", a.seal("RC01", "x")) is None, "sender ID is authenticated"
    evil = tnc.SecureLink(tnc.derive_key("BDF-NET", "a different passphrase!"), now)
    assert evil.open("RC01", a.seal("RC01", "secret")) is None, "wrong key cannot read"
    s2 = a.seal("RC01", "tamper test")
    flipped = s2[:-6] + ("A" if s2[-6] != "A" else "B") + s2[-5:]
    assert b.open("RC01", flipped) is None, "altered frame refused"
    old = a.seal("RC01", "old")
    clock[0] += tnc.SEC_MAX_AGE_S + 5
    assert b.open("RC01", old) is None, "stale frame refused"
    for f in (144_390_000, 147_990_000, 174_100_000):
        assert tnc.com_freq_hz({"com_freq": f / 1e6}) is None, "amateur / out-of-band frequency refused"
    assert tnc.com_freq_hz({"com_freq": "155.325"}) == 155_325_000
    # two units chat end to end through the encrypted link (handshake + messages)
    air = []
    links = {"RC01": tnc.SecureLink(key, now), "RC02": tnc.SecureLink(key, now)}
    u1 = tnc.ChatManager("RC01", lambda to, info, r: air.append(("RC01", links["RC01"].seal("RC01", info))), now)
    u2 = tnc.ChatManager("RC02", lambda to, info, r: air.append(("RC02", links["RC02"].seal("RC02", info))), now)

    def deliver():
        while air:
            src, frame = air.pop(0)
            for unit, mgr in (("RC01", u1), ("RC02", u2)):
                if unit != src:
                    plain = links[unit].open(src, frame)
                    assert plain is not None
                    mgr.on_packet(f"{src}>{tnc.SEC_DEST}:{plain}", "RF")
    u1.hail("RC02")
    deliver()
    u2.accept("RC01")
    deliver()
    u1.accept("RC02")
    deliver()
    assert u1._peer("RC02")["status"] == tnc.OPEN and u2._peer("RC01")["status"] == tnc.OPEN
    u2.send("RC01", "Road to Oistins blocked, use Highway 7")
    deliver()
    assert [m["text"] for m in u1._peer("RC02")["messages"] if m["dir"] == "in"] == ["Road to Oistins blocked, use Highway 7"]
    assert all(m["state"] == "delivered" for m in u2._peer("RC01")["messages"] if m["dir"] == "out")


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
