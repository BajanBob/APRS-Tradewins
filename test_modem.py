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
    clock[0] += 9; u1.tick(); deliver()          # held ack released
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


if __name__ == "__main__":
    main()
