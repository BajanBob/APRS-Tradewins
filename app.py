#!/usr/bin/env python3
# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""
Tradewind - Caribbean VHF/UHF propagation beacon, APRS client and soundcard TNC.

Run:   python3 app.py            then open http://127.0.0.1:8073
       python3 app.py --host 0.0.0.0   (reach the face plate from a phone on your LAN)

The face plate lets you enter your callsign and grid square, choose a radio
or interface (built-in sound modem, KISS TCP, KISS serial), and watch
2m / 70cm openings between the Caribbean and North America, Europe and
Latin America as they're announced on APRS.
"""

import argparse
import collections
import hashlib
import hmac
import json
import os
import sys
import re
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import afsk
import tnc as tncmod
import vhf_prop_aprs as eng
import wx

APP_NAME = "Tradewind"
APP_VERSION = "3.6"
# When packaged as an .exe (PyInstaller) bundled files live in sys._MEIPASS
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
def _conf_dir():
    # Android (python-for-android) has no normal home folder: use the app's private storage
    for var in ("TRADEWIND_HOME", "ANDROID_PRIVATE", "ANDROID_APP_PATH"):
        if os.environ.get(var):
            return os.path.join(os.environ[var], ".tradewind")
    home = os.path.expanduser("~")
    if home in ("~", "/", "") or not os.access(home, os.W_OK):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tradewind")
    return os.path.join(home, ".tradewind")


CONF_DIR = _conf_dir()
CONF_PATH = os.path.join(CONF_DIR, "config.json")
STATE_PATH = os.path.join(CONF_DIR, "announce_state.json")
HISTORY_PATH = os.path.join(CONF_DIR, "opening_history.json")
CHAT_DIR = os.path.join(CONF_DIR, "chat")         # saved chats + comms log (per callsign / unit)
CACHE_DIR = os.path.join(CONF_DIR, "cache")       # map library + map tiles, for use with no internet
VENDOR = {"leaflet.js": ("https://unpkg.com/leaflet@1.9.4/dist/leaflet.js", "application/javascript"),
          "leaflet.css": ("https://unpkg.com/leaflet@1.9.4/dist/leaflet.css", "text/css")}
TILE_SRC = {"esri": "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
            "osm": "https://tile.openstreetmap.org/{z}/{x}/{y}.png"}
TILE_MAX_AGE = 30 * 86400


LOCAL_MAIL_HOSTS = ("127.0.0.1", "localhost", "::1")


def mail_tls_context(host):
    """TLS for the gateway's mail account. Remote servers: certificate and host name are always checked.
    Only a mail bridge on this same computer (Proton Mail Bridge, self-signed) is accepted without a check -
    that traffic never leaves the computer."""
    import ssl
    if str(host or "").strip().lower() in LOCAL_MAIL_HOSTS:
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        return ctx
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _web_get(url, timeout=15):
    """Fetch a URL (map files) with a proper User-Agent; certifi CA bundle when available."""
    import ssl
    import urllib.request
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION} (amateur radio app; github.com/BajanBob/APRS-Tradewins)"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def cached_get(path, url, max_age=None):
    """Serve from the disk cache; refresh from the internet when missing or old. None if neither."""
    fresh = os.path.exists(path) and (max_age is None or time.time() - os.path.getmtime(path) < max_age)
    if not fresh:
        try:
            data = _web_get(url)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            return data
        except Exception:
            pass                                        # offline: fall back to any cached copy
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None
HISTORY_KEEP_S = 48 * 3600

IS_ANDROID = "ANDROID_ARGUMENT" in os.environ or hasattr(sys, "getandroidapilevel")

CALL_RE = re.compile(r"^(?=.*\d)[A-Z0-9]{3,7}(-([0-9]|1[0-5]))?$")
GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}([A-X]{2})?$", re.I)

DEFAULTS = {
    "call": "", "grid": "", "passcode": "",
    "symbol": "/-", "beacon": True, "beacon_min": 30,
    "beacon_comment": "Tradewind VHF/UHF prop alerts",
    "preset": "generic_vox",
    "tnc": "sound", "audio_in": "", "audio_out": "", "sample_rate": 48000,
    "ptt": "VOX", "ptt_port": "", "ptt_invert": False, "cm108_gpio": 3,
    "cat_baud": 9600, "civ_addr": "7C",
    # CAT tuning: put the radio on the APRS frequency in FM data mode at power-on
    "cat_tune": False, "aprs_freq": "auto", "aprs_freq_custom": "", "cat_restore": True,
    "txdelay": 300, "txtail": 50, "tx_level": 0.5, "modem": "1200",
    # Phase 2: a second radio path (Direwolf / UZ7HO / hardware TNC over KISS) and automatic link selection
    "link1_name": "", "link1_speed": 1200,
    "link2_enabled": False, "link2_name": "70cm 9600 G3RUH", "link2_speed": 9600,
    "link2_tnc": "kiss_tcp", "link2_addr": "127.0.0.1:8002", "link2_serial": "", "link2_baud": 9600,
    # Phase 4: off-grid / disaster features
    "store_forward": True, "relay_enabled": False,
    "block_list": "",
    # ---- email (v3.5): "EMAIL-2" service, or a Tradewind gateway station's callsign
    "mail_via": "EMAIL-2",
    "mail_gw_enabled": False,                 # this station is a Tradewind email gateway
    "smtp_host": "", "smtp_port": 587, "smtp_tls": True, "smtp_user": "", "smtp_pass": "", "smtp_from": "",
    "imap_host": "", "imap_port": 993, "imap_user": "", "imap_pass": "", "mail_poll_min": 5,
    "mail_allowed_calls": "",                 # empty = any amateur callsign
    "mail_per_day": 10, "mail_session_min": 10,
    "wolf_on": False, "wolf_min_km": 500, "wolf_per_hour": 5, "wolf_qth": "", "wolf_stop": "",
    "disaster_on": False, "disaster_until": 0, "disaster_reason": "",
    "zone": "auto", "qsy_mode": "auto",     # v3.6: band-plan zone from the grid square; chat channels auto or manual
    "qsy_freqs": "144.510,144.530,144.550,144.570,144.590,145.030,145.050,145.070,145.090",   # chat channels, 20 kHz apart            # callsigns whose hails and messages are ignored
    "pin_hash": "", "pin_salt": "", "lock_settings": False,   # station PIN (v3.2)
    "kiss_addr": "127.0.0.1:8001", "kiss_serial": "", "kiss_baud": 9600,
    "rf_announce": True, "rf_path": "WIDE2-1", "rf_when": "always", "rf_modes": ["TEP"],
    "aprsis": True, "server": "rotate.aprs2.net", "igate": False,
    "igate_tx": False,           # full iGate: also pass internet messages to local RF stations
    "kiss_server": False, "kiss_server_port": 8001, "kiss_server_lan": False,   # be a TNC for other apps
    "sdr_gain": "", "sdr_ppm": 0, "sdr_index": 0,                             # RTL-SDR receive
    "window": 60, "interval": 10, "min_km": 800, "min_paths": 2,
    "strong_km": 1500, "repeat": 30, "announce_close": False, "group": "VHF",
    "alert_sound": True, "alert_voice": True, "alert_modes": ["TEP", "TROPO", "ES"],
    "watch_modes": ["TEP", "TROPO", "ES"],
    "bands": ["2m", "70cm"], "regions": ["NA", "EU", "LA"], "local_km": 300,
    # scheduled weather bulletins (times are local, Barbados = UTC-4, no DST)
    "wx_enabled": False, "wx_rf": True, "wx_utc_offset": -4, "wx_place": "Barbados",
    "wx_general": True, "wx_general_times": "08:00",
    "wx_marine": True, "wx_marine_times": "05:30, 12:00",
    "wx_aviation": True, "wx_aviation_times": "06:30, 18:00",
    "wx_lat": 13.10, "wx_lon": -59.60, "wx_marine_lat": 13.10, "wx_marine_lon": -59.30,
    "wx_icao": "TBPB", "wx_fir": "TTZP",
    # tropical cyclone alerts from the National Hurricane Center
    "tc_enabled": True, "tc_radius_km": 1500, "tc_rf": True,
    # Commercial / EmComm mode: encrypted chat on a licensed 148-174 MHz channel
    "op_mode": "amateur", "com_callsign": "", "com_licensee": "", "com_unit": "",
    "com_freq": "", "com_net": "", "com_key": "", "com_fp": "", "com_id_min": 30,
    "cfg_ver": 4,
}
SECRET_KEYS = ("com_key", "pin_hash", "pin_salt", "smtp_pass", "imap_pass")   # never sent to the face plate
GUARDED_KEYS = SECRET_KEYS + ("lock_settings", "disaster_on", "disaster_until", "disaster_reason")      # only changed through their own endpoints

# Radio / interface presets. Fields override DEFAULTS when chosen in the UI.
PRESETS = [
    {"id": "generic_vox", "label": "Generic soundcard + VOX (SignaLink USB, Easy Digi)",
     "tnc": "sound", "ptt": "VOX",
     "note": "Turn the interface's VOX/delay knob low. Set TX audio so the radio deviates about 3 kHz."},
    {"id": "signalink", "label": "Tigertronics SignaLink USB",
     "tnc": "sound", "ptt": "VOX", "txdelay": 300,
     "note": "Audio device: 'USB Audio CODEC'. The SignaLink keys the radio itself (VOX) - set its DLY knob to about 9 o'clock, TX knob low, and adjust until the radio deviates about 3 kHz. Use the radio jumper / cable for your model on the data or mic jack."},
    {"id": "rtlsdr_audio", "label": "RTL-SDR dongle via SDR++ / SDR# (receive only)",
     "tnc": "sound", "ptt": "VOX", "rf_announce": False,
     "note": "Receive-only. Tune SDR++ or SDR# to the APRS frequency (144.390 MHz in North America - check which frequency your island uses) in narrow FM, 12.5 kHz bandwidth. Send its audio to a virtual audio cable (e.g. VB-Audio Cable) and pick 'CABLE Output' as Tradewind's audio input. Heard packets are shown and can be gated with RX iGate; nothing is transmitted on RF."},
    {"id": "rtlsdr_direwolf", "label": "RTL-SDR dongle via rtl_fm + Direwolf (receive only)",
     "tnc": "kiss_tcp", "kiss_addr": "127.0.0.1:8001", "rf_announce": False,
     "note": "Receive-only. Run: rtl_fm -f 144.39M -s 24000 - | direwolf -r 24000 -D 1 -  (adjust the frequency for your area). Direwolf decodes and Tradewind connects to its KISS port 8001. Turn on RX iGate under MENU > APRS-IS to gate what the dongle hears."},
    {"id": "digirig", "label": "Digirig Mobile",
     "tnc": "sound", "ptt": "RTS",
     "note": "Audio device is 'USB PnP Sound Device'. PTT port is the Digirig's serial port (COMx / /dev/ttyUSBx)."},
    {"id": "aioc", "label": "AIOC all-in-one cable (Baofeng/Kenwood-pin HTs)",
     "tnc": "sound", "ptt": "DTR",
     "note": "AIOC keys on DTR with RTS low. Audio device shows as 'All-In-One-Cable'."},
    {"id": "cm108", "label": "CM108/CM119 USB fob with GPIO PTT",
     "tnc": "sound", "ptt": "CM108", "cm108_gpio": 3,
     "note": "Linux only: PTT port is /dev/hidrawN (give your user access via a udev rule)."},
    {"id": "ic705", "label": "Icom IC-705 (built-in USB sound card)",
     "tnc": "sound", "ptt": "RTS",
     "note": "Audio: 'USB Audio CODEC'. PTT port: the IC-705 USB serial port. On the radio set USB SEND to RTS, DATA MOD to USB, and use FM-D (data) mode. For automatic tuning, set PTT to 'Icom CI-V' with address A4 and tick 'Tune the radio'."},
    {"id": "ic9700", "label": "Icom IC-9700 (built-in USB sound card)",
     "tnc": "sound", "ptt": "RTS",
     "note": "Audio: 'USB Audio CODEC'. PTT port: first IC-9700 USB serial port. Set USB SEND to RTS, DATA MOD to USB, use FM-D. For automatic tuning, set PTT to 'Icom CI-V' with address A2 and tick 'Tune the radio'."},
    {"id": "ic7100", "label": "Icom IC-7100 (built-in USB sound card)",
     "tnc": "sound", "ptt": "RTS",
     "note": "Audio: 'USB Audio CODEC'. Set USB SEND to RTS and DATA MOD to USB; use FM-D. For automatic tuning, set PTT to 'Icom CI-V' with address 88 and tick 'Tune the radio'."},
    {"id": "ic9100", "label": "Icom IC-9100 (built-in USB sound card, CI-V PTT)",
     "tnc": "sound", "ptt": "CIV", "civ_addr": "7C", "cat_baud": 9600, "cat_tune": True,
     "note": "One USB cable carries audio and CI-V. Audio: 'USB Audio CODEC'. PTT port: the IC-9100's USB serial port. On the radio set DATA MOD to USB, USB MOD Level about 25-50%, CI-V address 7C and CI-V baud 9600. With 'Tune the radio' on, Tradewind sets the APRS frequency and FM data mode for you at power-on (2m on the main band) and puts the radio back when you switch off. Close other CAT software (logger, WSJT-X) first - only one program can use the port."},
    {"id": "ft991a", "label": "Yaesu FT-991A (built-in USB sound card)",
     "tnc": "sound", "ptt": "RTS",
     "note": "Audio: 'USB Audio CODEC'. PTT port: the 'Enhanced' COM port. Set the radio's PC keying / data PTT option to RTS and use DATA-FM. For automatic tuning to the APRS frequency, choose PTT 'Yaesu CAT' on the 'Standard' COM port at the radio's CAT baud rate (menu 031) and tick 'Tune the radio'."},
    {"id": "ft2980", "label": "Yaesu FT-2980R (mic + speaker jacks, via interface)",
     "tnc": "sound", "ptt": "VOX", "txdelay": 350, "tx_level": 0.3,
     "note": "The FT-2980R has no data jack or CAT port. Use a soundcard interface with a cable for Yaesu's 8-pin modular mic jack and the 3.5 mm external speaker jack: SignaLink USB (VOX), or a Digirig with Yaesu mic cable (set PTT to Serial RTS). Check the cable's mic-jack pinout against the radio manual. Set squelch just closed, keep TX audio low (mic audio is pre-emphasised), and use Low or Mid power for long sessions."},
    {"id": "yaesu_datajack", "label": "Yaesu FT-7900R / FT-8800R / FT-8900R (DATA jack)",
     "tnc": "sound", "ptt": "VOX",
     "note": "Use the 6-pin mini-DIN DATA jack with a SignaLink, Digirig or Easy Digi data cable. Set the radio's packet speed to 1200. PTT: VOX on a SignaLink, or Serial RTS on a Digirig."},
    {"id": "yaesu_ftm", "label": "Yaesu FTM-400 / FTM-300 / FTM-200 (DATA jack)",
     "tnc": "sound", "ptt": "VOX",
     "note": "These radios' built-in APRS has no KISS mode, so connect a soundcard interface to the rear DATA jack (cable for your model). Set the data jack speed to 1200 bps and turn the radio's own APRS off on that band. PTT: VOX or Serial RTS (Digirig)."},
    {"id": "rigctld", "label": "Any radio via Hamlib rigctld (CAT PTT)",
     "tnc": "sound", "ptt": "RIGCTLD", "ptt_port": "127.0.0.1:4532",
     "note": "Start rigctld for your radio first. Pick the radio's USB audio device for in/out. Tick 'Tune the radio' to have Tradewind set the APRS frequency and FM data mode through Hamlib."},
    {"id": "thd75", "label": "Kenwood TH-D75 (built-in TNC, KISS)",
     "tnc": "kiss_serial", "kiss_baud": 9600,
     "note": "Enable KISS mode on the radio, then choose its USB or Bluetooth serial port."},
    {"id": "thd74", "label": "Kenwood TH-D74 (built-in TNC, KISS)",
     "tnc": "kiss_serial", "kiss_baud": 9600,
     "note": "Enable KISS mode on the radio, then choose its USB or Bluetooth serial port."},
    {"id": "tmd710", "label": "Kenwood TM-D710G / TM-D710GA (built-in TNC)",
     "tnc": "kiss_serial", "kiss_baud": 9600,
     "note": "Put the built-in TNC in KISS mode (PACKET mode, then KISS ON). Use the COM port on the control head."},
    # ---- HF and more VHF/UHF radios (v2.0). HF presets use the 300-baud HF modem on USB.
    {"id": "ic7300", "label": "Icom IC-7300 (HF/6m, built-in USB sound card)",
     "tnc": "sound", "ptt": "CIV", "civ_addr": "94", "cat_baud": 19200, "modem": "300",
     "note": "One USB cable. Audio: 'USB Audio CODEC'. PTT: the radio's USB COM port, Icom CI-V address 94. On the radio set CI-V USB Baud Rate to 19200 (or Auto), DATA MOD to USB, and use USB-D mode. HF packet uses the 300-baud modem (e.g. 10.1476 MHz USB for HF APRS)."},
    {"id": "ic7610", "label": "Icom IC-7610 (HF/6m, built-in USB sound card)",
     "tnc": "sound", "ptt": "CIV", "civ_addr": "98", "cat_baud": 19200, "modem": "300",
     "note": "Audio: 'USB Audio CODEC'. PTT: Icom CI-V address 98 on the USB COM port. Set DATA MOD to USB and use USB-D. 300-baud HF modem."},
    {"id": "ic2730", "label": "Icom IC-2730 / IC-5100 / other FM mobiles (via interface)",
     "tnc": "sound", "ptt": "VOX", "txdelay": 350,
     "note": "These radios have no built-in USB sound card. Use a SignaLink (VOX) or Digirig (Serial RTS) with the cable for your radio's data or mic jack, and check the pinout in the radio's manual."},
    {"id": "ts590", "label": "Kenwood TS-590S / TS-590SG / TS-890S (HF, USB audio)",
     "tnc": "sound", "ptt": "RIGCTLD", "ptt_port": "127.0.0.1:4532", "modem": "300",
     "note": "One USB cable carries audio ('USB Audio CODEC') and CAT. Run Hamlib rigctld for your model for PTT and tuning, and set the radio's data input to USB audio. 300-baud HF modem."},
    {"id": "tmv71", "label": "Kenwood TM-V71A / TM-V71E (data jack)",
     "tnc": "sound", "ptt": "VOX",
     "note": "Use the 6-pin mini-DIN DATA jack with a SignaLink (VOX) or Digirig (Serial RTS) data cable. Set the data band and 1200 bps in the radio menu."},
    {"id": "ft710", "label": "Yaesu FT-710 / FTDX10 / FTDX101 (HF, USB audio)",
     "tnc": "sound", "ptt": "YAESU_CAT", "cat_baud": 38400, "modem": "300",
     "note": "Audio: the radio's USB audio device. PTT: Yaesu CAT on the 'Standard' COM port at the radio's CAT rate (or Serial RTS on the 'Enhanced' port). Select USB as the data/rear audio input and use DATA-U. 300-baud HF modem."},
    {"id": "yaesu_ht", "label": "Yaesu FT-5DR / FT-3DR / FT-65 handhelds (audio cable)",
     "tnc": "sound", "ptt": "RTS", "txdelay": 400, "tx_level": 0.3,
     "note": "Use a Digirig (Serial RTS) with the Yaesu handheld audio cable for your model; check the plug type in the manual. Turn the radio's VOX off, keep TX audio low, and use low power for long sessions."},
    {"id": "x6100", "label": "Xiegu X6100 (HF/6m, built-in USB audio)",
     "tnc": "sound", "ptt": "RIGCTLD", "ptt_port": "127.0.0.1:4532", "modem": "300",
     "note": "Audio and CAT over USB (depends on firmware). Run Hamlib rigctld with the X6100 model for PTT and tuning. 300-baud HF modem."},
    {"id": "xiegu_g90", "label": "Xiegu G90 / G106 (HF, via DE-19 or Digirig)",
     "tnc": "sound", "ptt": "VOX", "modem": "300",
     "note": "These radios have no built-in sound card. Use the DE-19 expansion or a Digirig for audio; PTT by VOX, Serial RTS on a Digirig, or Hamlib rigctld through the CI-V port. 300-baud HF modem."},
    {"id": "aioc_ht", "label": "Radtel RT-890 / RT-4D, Quansheng UV-K5, Baofeng UV-5R (AIOC cable)",
     "tnc": "sound", "ptt": "DTR", "txdelay": 400, "tx_level": 0.3,
     "note": "Use an AIOC all-in-one cable (Kenwood-style 2-pin plug). Audio device 'All-In-One-Cable'; PTT is DTR. Turn the radio's VOX off, keep TX audio low and squelch just closed. These handhelds are not made for long transmissions, so use low power."},
    {"id": "mobilinkd", "label": "Mobilinkd TNC3 / TNC4 (Bluetooth or USB)",
     "tnc": "kiss_serial", "kiss_baud": 115200,
     "note": "Pair the TNC, then choose its serial port (rfcomm / Bluetooth COM)."},
    {"id": "direwolf", "label": "Direwolf (external sound modem)",
     "tnc": "kiss_tcp", "kiss_addr": "127.0.0.1:8001",
     "note": "Direwolf's default KISS TCP port is 8001. Direwolf handles audio and PTT."},
    {"id": "uz7ho", "label": "UZ7HO Soundmodem (external sound modem)",
     "tnc": "kiss_tcp", "kiss_addr": "127.0.0.1:8100",
     "note": "Enable the KISS server in Soundmodem (default port 8100)."},
    {"id": "aprsis_only", "label": "No radio - APRS-IS only",
     "tnc": "none", "rf_announce": False,
     "note": "Announcements go to APRS-IS only."},
]


def latlon_to_aprs(lat, lon):
    la, lo = abs(lat), abs(lon)
    lat_s = f"{int(la):02d}{(la - int(la)) * 60:05.2f}{'N' if lat >= 0 else 'S'}"
    lon_s = f"{int(lo):03d}{(lo - int(lo)) * 60:05.2f}{'E' if lon >= 0 else 'W'}"
    return lat_s, lon_s


ALL_MODES = list(eng.MODES)
_REGION_TO_MODES = {"LA": ["TEP"], "NA": ["TROPO", "ES"], "EU": ["TROPO"]}


def _modes_from_regions(regions):
    out = []
    for r in regions or []:
        for m in _REGION_TO_MODES.get(r, []):
            if m not in out:
                out.append(m)
    return out


def _migrate(cfg):
    """Convert settings saved by earlier versions (path-based -> mode-based)."""
    if cfg.get("rf_when") == "tep":
        cfg["rf_when"], cfg["rf_modes"] = "selected", ["TEP"]
    elif cfg.get("rf_when") == "openings":
        cfg["rf_when"], cfg["rf_modes"] = "selected", list(ALL_MODES)
    if "rf_regions" in cfg:
        cfg["rf_modes"] = _modes_from_regions(cfg.pop("rf_regions")) or ["TEP"]
    if cfg.pop("alert_tep_only", False):
        cfg["alert_modes"] = ["TEP"]
    if "alert_regions" in cfg:
        regs = cfg.pop("alert_regions")
        cfg["alert_modes"] = list(ALL_MODES) if set(regs) >= {"NA", "EU", "LA"} else _modes_from_regions(regs)
    return cfg


# ---- on-air and network etiquette limits (v3.2)
PSK_MIN_GAP = 600        # at least 10 minutes between PSKReporter queries
PSK_MAX_PER_HOUR = 6     # and never more than 6 in any hour
IGATE_TX_PER_HOUR = 30   # internet -> RF messages gated per hour (full iGate)
LOCAL_HEARD_S = 30 * 60  # "local" = heard on RF in the last 30 minutes
SAME_TX_PER_HOUR = 2     # identical beacon / bulletin text: at most twice an hour
BEACON_MIN_MIN = 30      # so the position beacon is never more than twice an hour


TW_USERS_PATH = os.path.join(CONF_DIR, "tradewind_users.json")


def load_tw_users():
    try:
        with open(TW_USERS_PATH) as f:
            d = json.load(f)
        return {k: v for k, v in d.items() if isinstance(v, dict) and "last" in v}
    except (OSError, ValueError):
        return {}


def save_tw_users(users):
    try:
        os.makedirs(CONF_DIR, exist_ok=True)
        keep = dict(sorted(users.items(), key=lambda kv: -kv[1]["last"])[:500])
        with open(TW_USERS_PATH + ".tmp", "w") as f:
            json.dump(keep, f)
        os.replace(TW_USERS_PATH + ".tmp", TW_USERS_PATH)
    except OSError:
        pass


def block_set(cfg):
    return {tncmod.norm_call(x) for x in re.split(r"[,\s]+", str(cfg.get("block_list") or "")) if x.strip()}


def _pin_hash(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), ("tradewind-pin:" + salt).encode(), 100_000).hex()


def load_config():
    cfg, saved = dict(DEFAULTS), {}
    if IS_ANDROID:
        cfg.update(tnc="none", preset="aprsis_only", rf_announce=False)
    try:
        with open(CONF_PATH) as f:
            saved = json.load(f)
        cfg.update(saved)
    except (OSError, ValueError):
        pass
    cfg = _migrate(cfg)
    try:
        cfg["interval"] = max(PSK_MIN_GAP // 60, int(cfg.get("interval") or 10))
        cfg["beacon_min"] = max(BEACON_MIN_MIN, int(cfg.get("beacon_min") or 30))
    except (TypeError, ValueError):
        cfg["interval"], cfg["beacon_min"] = 10, 30
    if cfg.get("qsy_freqs") == "145.030,145.050,145.070,145.090":
        cfg["qsy_freqs"] = DEFAULTS["qsy_freqs"]      # v3.4: 144.5 MHz channels added first
    if saved and int(saved.get("cfg_ver") or 0) < 4 and saved.get("aprs_freq") in (None, "144.390"):
        cfg["aprs_freq"] = "auto"                   # v3.6: the old default follows the zone (same 144.390 in the Caribbean)
    if saved and "qsy_mode" not in saved:          # v3.6: a hand-edited channel list stays as the manual list
        cfg["qsy_mode"] = "auto" if cfg.get("qsy_freqs") == DEFAULTS["qsy_freqs"] else "manual"
    cfg["cfg_ver"] = 4
    return cfg


def save_config(cfg):
    os.makedirs(CONF_DIR, exist_ok=True)
    tmp = CONF_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=1)
    os.replace(tmp, CONF_PATH)


def validate_commercial(cfg):
    errs = []
    missing = tncmod.crypto_available()
    if missing:
        errs.append("Commercial mode: " + missing + ".")
    if cfg.get("tnc") not in ("sound", "kiss_tcp", "kiss_serial"):
        errs.append("Commercial mode needs a radio: choose a TNC under MENU > RADIO / TNC.")
    if not tncmod.com_freq_hz(cfg):
        errs.append("Commercial channel must be 148.000 - 174.000 MHz (your licensed frequency).")
    if not re.match(r"^[A-Z0-9]{1,6}(-([0-9]|1[0-5]))?$", str(cfg.get("com_unit") or "").upper()):
        errs.append("Enter this unit's ID: 1-6 letters/numbers, e.g. RC01 or BDF7.")
    if not str(cfg.get("com_callsign") or "").strip() or not str(cfg.get("com_licensee") or "").strip():
        errs.append("Enter the licence holder and the station call sign / licence number from your licence.")
    if not cfg.get("com_key"):
        errs.append("Set the network name and passphrase (MENU > COMMERCIAL).")
    return errs


PATH_WIDE_RE = re.compile(r"^WIDE([1-7])-([1-7])$")


def check_path(path):
    """Digipeater path rule: at most 2 hops in all (e.g. WIDE1-1,WIDE2-1 or WIDE2-2).
    WIDE3 and above, RELAY and TRACE flood the network and are refused. Returns an error or ''."""
    parts = [x.strip().upper() for x in str(path or "").split(",") if x.strip()]
    if len(parts) > 2:
        return "RF path: use at most two entries, e.g. WIDE1-1,WIDE2-1."
    hops = 0
    for x in parts:
        if x.startswith(("RELAY", "TRACE")) or x.startswith("WIDE") and not PATH_WIDE_RE.match(x):
            return f"RF path: {x} is outdated or floods the network - use WIDE1-1,WIDE2-1."
        m = PATH_WIDE_RE.match(x)
        if m:
            n, hop = int(m.group(1)), int(m.group(2))
            if n > 2 or hop > n:
                return f"RF path: {x} is too wide - the limit is 2 hops (WIDE1-1,WIDE2-1 or WIDE2-2)."
            hops += hop
        elif not re.match(r"^[A-Z0-9]{1,6}(-([0-9]|1[0-5]))?$", x):
            return f"RF path: {x} is not a valid digipeater call or WIDE alias."
        else:
            hops += 1
    if hops > 2:
        return "RF path: the limit is 2 hops in all (e.g. WIDE1-1,WIDE2-1)."
    return ""


def validate(cfg):
    if cfg.get("op_mode") == "commercial":
        return validate_commercial(cfg)
    errs = []
    if IS_ANDROID and cfg.get("tnc") in ("sound", "kiss_serial"):
        errs.append("On Android use 'APRS-IS only' or 'KISS over TCP' - the sound modem and USB/serial radios need the Windows/PC version.")
    call = cfg.get("call", "").upper().strip()
    if not CALL_RE.match(call) or call.split("-")[0] in ("N0CALL", "NOCALL", "MYCALL", "CALL", "TEST", "AB1CD"):
        errs.append("Enter your own callsign (with optional -SSID, e.g. 8P6XX-10).")
    if cfg.get("mail_gw_enabled") and cfg.get("smtp_host") and not cfg.get("smtp_tls", True) \
            and int(cfg.get("smtp_port") or 587) != 465 and str(cfg["smtp_host"]).strip().lower() not in LOCAL_MAIL_HOSTS:
        errs.append("Tick 'Use STARTTLS' (or use port 465) so the mail password is never sent unencrypted.")
    zone = tncmod.zone_of(cfg)
    if str(cfg.get("zone") or "auto") not in ("auto", *tncmod.ZONES):
        errs.append("Choose a band-plan zone, or Automatic.")
    if cfg.get("qsy_mode") == "manual":
        bad = [f.strip() for f in str(cfg.get("qsy_freqs") or "").split(",")
               if f.strip() and not tncmod.qsy_freq_ok(f.strip(), zone)]
        if bad:
            errs.append(f"These chat frequencies clash with APRS, protected or satellite segments in "
                        f"{tncmod.ZONES[zone]['name']}: {', '.join(bad)}")
    perr = check_path(cfg.get("rf_path"))
    if perr:
        errs.append(perr)
    if not GRID_RE.match(cfg.get("grid", "").strip()):
        errs.append("Enter a 4 or 6 character Maidenhead grid square (e.g. GK03 or GK03fd).")
    if cfg.get("tnc") == "sound" and cfg.get("ptt") in ("RTS", "DTR", "CM108", "CIV", "YAESU_CAT") and not cfg.get("ptt_port"):
        errs.append("Choose the serial / HID port used for PTT.")
    if cfg.get("ptt") == "CIV" and cfg.get("tnc") == "sound":
        try:
            int(str(cfg.get("civ_addr") or "7C"), 16)
        except ValueError:
            errs.append("CI-V address must be hex, e.g. 7C for the IC-9100.")
    if cfg.get("cat_tune") and cfg.get("tnc") == "sound" and not tncmod.aprs_freq_hz(cfg):
        errs.append("APRS frequency must be in MHz within the 2m or 70cm band, e.g. 144.390.")
    if cfg.get("wx_enabled"):
        for k in ("wx_general_times", "wx_marine_times", "wx_aviation_times"):
            try:
                wx.parse_times(cfg.get(k))
            except ValueError as e:
                errs.append(f"Weather times must look like 05:30, 12:00 (problem: {e}).")
    if cfg.get("tnc") == "sdr" and IS_ANDROID:
        errs.append("The RTL-SDR receiver needs the Windows, Linux or Raspberry Pi version.")
    if cfg.get("tnc") == "sdr" and not tncmod.aprs_freq_hz(cfg):
        errs.append("APRS frequency must be in MHz within the 2m or 70cm band, e.g. 144.390.")
    if cfg.get("igate_tx") and not cfg.get("igate"):
        errs.append("The full iGate needs 'RX iGate' switched on too (MENU > APRS-IS).")
    if cfg.get("tnc") == "kiss_serial" and not cfg.get("kiss_serial"):
        errs.append("Choose the radio / TNC serial port.")
    return errs


class Station:
    """Everything that runs while the station is ON."""

    def __init__(self):
        self.cfg = load_config()
        self.lock = threading.RLock()
        self.running = False
        self.tnc = None
        self.isc = None
        self.log = collections.deque(maxlen=400)
        self.log_id = 0
        self.openings = {}
        self.last_check = 0
        self.psk_times = collections.deque()   # when each PSKReporter query went out
        self.tx_seen = {}                       # beacon/bulletin text -> recent send times
        self.next_check = 0
        self.next_beacon = 0
        self.spot_count = 0
        self.car_count = 0
        self.local = []
        self.map_paths = []           # Caribbean paths for the live map
        self.check_error = ""
        self.ann_state = eng.load_state(STATE_PATH)
        self.demo_xml = None
        self.last_mode = {}
        self.history = self._load_history()   # list of opening events
        self.wx_done = set()          # "kind@HH:MM@YYYY-MM-DD" already sent
        self.wx_last = {}             # kind -> (time, lines or error)
        self.tc_sent = set()          # "stormid#advisory" already announced
        self.tc_next = 0
        self.tc_alerts = []           # latest alerts for the face plate
        self.tc_seq = 0
        self.chat = None              # two-way APRS chat (created when the station starts)
        self.tnc2 = None              # second radio link (Phase 2), KISS
        self.linkctl = None           # automatic link controller
        self.commercial = False       # True while running in Commercial / EmComm mode
        self.sec = None               # SecureLink (AES-256-GCM) in commercial mode
        self.last_id = 0
        self.prop_off = False
        self.heard = {}               # callsign -> stations heard on RF (works with no internet)
        self.kiss_srv = None          # KISS TCP server for other APRS apps
        self.qsy_hz = None            # set while a chat has moved off the APRS frequency
        self.mail_count = {}          # gateway: (callsign, date) -> emails sent today
        self.mail_next_poll = 0
        self.mail_error = ""
        self.wolf = {}                # Wolf Mode: base callsign -> DX station seen on PSKReporter
        self.wolf_pings = collections.deque()
        self.igate_tx_times = collections.deque()
        self.igate_seen = {}          # (src, info) -> last gated, to skip duplicates
        self.tw_users = load_tw_users()   # Tradewind stations heard: call -> {first, last, via}
        self.commlog = []             # ICS-309-style log of chat traffic
        self._chat_saved = -1
        self._chat_save_t = 0
        self._stop = threading.Event()
        self._wake = threading.Event()

    # ---- logging
    def add_log(self, kind, text):
        with self.lock:
            self.log_id += 1
            self.log.append({"id": self.log_id, "t": time.time(), "kind": kind, "text": text})

    # ---- lifecycle
    def start(self):
        with self.lock:
            if self.running:
                return []
            errs = validate(self.cfg)
            if errs:
                return errs
            c = self.cfg
            c["call"] = c["call"].upper().strip()
            self._stop.clear()
            self.tnc = tncmod.make_tnc(c)
            try:
                self.tnc.start(self.on_rf_packet)
            except Exception as e:
                self.tnc = None
                return [f"Could not start TNC: {e}"]
            self.tnc2, self.linkctl = None, None
            if c.get("link2_enabled"):
                try:
                    if c.get("link2_tnc") == "kiss_serial":
                        self.tnc2 = tncmod.KissSerialTNC(c.get("link2_serial", ""), c.get("link2_baud", 9600))
                    else:
                        host, _, port = (c.get("link2_addr") or "127.0.0.1:8002").partition(":")
                        self.tnc2 = tncmod.KissTcpTNC(host, port or 8002)
                    self.tnc2.start(lambda line: self.on_rf_packet(line, "B"))
                except Exception as e:
                    self.tnc2 = None
                    self.add_log("ERR", f"Second radio link not started: {e}")
            if self.tnc2 is not None and c.get("tnc") not in ("none", "sdr"):
                s1 = 44 if c.get("tnc") == "sound" and c.get("modem") == "robust" else \
                    300 if c.get("tnc") == "sound" and str(c.get("modem")) == "300" else \
                    (1200 if c.get("tnc") == "sound" else int(c.get("link1_speed") or 1200))
                self.linkctl = tncmod.LinkController({
                    "A": {"name": c.get("link1_name") or f"{s1} bps (main radio)", "speed": s1},
                    "B": {"name": c.get("link2_name") or "second link", "speed": int(c.get("link2_speed") or 9600)}})
            self.commercial = c.get("op_mode") == "commercial"
            self.sec, self.last_id = None, 0
            self.kiss_srv = None
            if c.get("kiss_server") and not self.commercial and c.get("tnc") in ("sound", "sdr", "kiss_serial"):
                try:
                    self.kiss_srv = tncmod.KissServer("0.0.0.0" if c.get("kiss_server_lan") else "127.0.0.1",
                                                     int(c.get("kiss_server_port") or 8001), self._kiss_client_tx)
                    self.kiss_srv.start()
                    self.tnc.on_frame = self.kiss_srv.broadcast
                    self.add_log("SYS", f"KISS TNC server on port {self.kiss_srv.port} - other APRS apps can connect")
                except Exception as e:
                    self.kiss_srv = None
                    self.add_log("ERR", f"KISS server not started: {e}")
            if self.commercial:
                self.sec = tncmod.SecureLink(bytes.fromhex(c["com_key"]))
                unit = c["com_unit"].upper()
                self.chat = tncmod.ChatManager(unit, self._com_send)
                self._attach_chat()
            elif c.get("aprsis"):
                pc = c.get("passcode") or eng.aprs_passcode(c["call"])
                self.isc = tncmod.AprsIsClient(c["call"], int(pc), c.get("server") or "rotate.aprs2.net",
                                               software=APP_NAME, version=APP_VERSION)
                self.isc.start(self.on_is_line)
            if self.chat:
                self.chat.links = self.linkctl
                self.chat.store_forward = bool(c.get("store_forward", True))
                self.chat.relay_enabled = bool(c.get("relay_enabled"))
                self.chat.retry = (90, 150, 240) if (c.get("tnc") == "sound" and c.get("modem") == "robust") else tncmod.RETRY_S
            if not self.commercial and (not self.chat or self.chat.mycall != c["call"]
                                        or getattr(self.chat, "send_fn", None) != self._chat_send):
                self.chat = tncmod.ChatManager(c["call"], self._chat_send)
                self._attach_chat()
            self.running = True
            self.openings = {}
            self._rf_was_live = self.rf_live()
            self.next_check = time.time() + 3
            self.next_beacon = time.time() + 10 if c.get("beacon") and not self.commercial else 0
            # commercial units without a grid square skip the propagation watch
            self.prop_off = self.commercial and not GRID_RE.match(c.get("grid", "").strip())
            threading.Thread(target=self._loop, daemon=True).start()
            if self.commercial:
                self.add_log("SEC", f"COMMERCIAL MODE - unit {c['com_unit'].upper()} on {c['com_freq']} MHz, "
                                    f"network {c['com_net']} (key {c['com_fp']}), AES-256-GCM. APRS bulletins and APRS-IS are off.")
            else:
                self.add_log("SYS", f"Station ON as {c['call']} @ {c['grid']} - TNC: {self.tnc.kind}")
            cat = getattr(self.tnc, "cat_status", "")
            if cat:
                self.add_log("ERR" if "failed" in cat else "SYS", f"Radio: {cat}")
            return []

    def stop(self):
        with self.lock:
            if not self.running:
                return
            self._stop.set()
            self._wake.set()
            for x in (self.tnc, self.isc, self.tnc2, self.kiss_srv):
                try:
                    x and x.stop()
                except Exception:
                    pass
            self._save_chat()
            self.tnc = self.isc = self.tnc2 = self.kiss_srv = None
            self.linkctl = None
            self.running = False
            self.add_log("SYS", "Station OFF")

    def update_config(self, new):
        was = self.running
        if was:
            trial = dict(self.cfg)
            trial.update({k: v for k, v in new.items() if k in DEFAULTS and k not in GUARDED_KEYS})
            trial["call"] = str(trial.get("call", "")).upper().strip()
            errs = validate(trial)
            if errs:  # keep the running station on its last good settings
                return ["Not applied: " + errs[0]]
            self.stop()
        with self.lock:
            for k, v in new.items():
                if k in DEFAULTS and k not in GUARDED_KEYS:
                    self.cfg[k] = v
            self.cfg["rf_path"] = ",".join(x.strip().upper() for x in str(self.cfg.get("rf_path") or "").split(",") if x.strip())
            _migrate(self.cfg)
            self.cfg["call"] = self.cfg.get("call", "").upper().strip()
            self.cfg["com_unit"] = str(self.cfg.get("com_unit") or "").upper().strip()
            self.cfg["group"] = (self.cfg.get("group") or "VHF").upper()[:5]
            save_config(self.cfg)
        errs = validate(self.cfg)
        if was and not errs:
            errs = self.start()
        return errs

    # ---- main loop
    def _loop(self):
        while not self._stop.is_set():
            now = time.time()
            if now >= self.next_check and not self.prop_off:
                wait = self.psk_wait()
                if wait > 0:
                    self.next_check = now + wait
                else:
                    self.run_check()
                    self.next_check = time.time() + max(PSK_MIN_GAP, self.cfg["interval"] * 60)
            if self.next_beacon and now >= self.next_beacon:
                self.send_beacon()
                self.next_beacon = time.time() + max(BEACON_MIN_MIN, self.cfg["beacon_min"]) * 60
            if self.cfg.get("wx_enabled"):
                self._wx_schedule(now)
            c0 = self.cfg
            if c0.get("mail_gw_enabled") and c0.get("imap_host") and now >= self.mail_next_poll and not self.commercial:
                self.mail_next_poll = now + max(2, int(c0.get("mail_poll_min") or 5)) * 60
                threading.Thread(target=self._imap_poll, daemon=True).start()
            if c0.get("disaster_on") and now > float(c0.get("disaster_until") or 0):
                self.set_disaster(False, reason="session expired")
            if self.cfg.get("tc_enabled") and now >= self.tc_next:
                self.tc_next = now + 20 * 60
                threading.Thread(target=self.check_tropical, daemon=True).start()
            if self.chat:
                try:
                    self.chat.tick()
                    if self.chat.seq != self._chat_saved and time.time() - self._chat_save_t > 5:
                        self._save_chat()
                except Exception as e:
                    self.add_log("ERR", f"chat: {e}")
            self._wake.wait(1.0)
            self._wake.clear()

    # ---- opening history (for "while you were away")
    def _load_history(self):
        try:
            with open(HISTORY_PATH) as f:
                h = json.load(f)
            cutoff = time.time() - HISTORY_KEEP_S
            return [e for e in h if (e.get("end") or e.get("last") or 0) >= cutoff]
        except (OSError, ValueError):
            return []

    def _save_history(self):
        try:
            os.makedirs(CONF_DIR, exist_ok=True)
            tmp = HISTORY_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.history, f)
            os.replace(tmp, HISTORY_PATH)
        except OSError:
            pass

    def _record_history(self, ops):
        now = time.time()
        active = {(e["band"], e["region"]): e for e in self.history if not e.get("end")}
        for (band, region), op in ops.items():
            b = op.best
            e = active.get((band, region))
            if e is None:
                e = {"id": f"{band}-{region}-{int(now)}", "band": band, "region": region,
                     "start": now, "end": None, "mode": op.mode, "paths": 0, "km": 0}
                self.history.append(e)
            e["last"] = now
            e["mode"] = op.mode or e.get("mode")
            if op.score >= e.get("paths", 0):
                e["paths"] = op.score
            if b and round(b.km) >= e.get("km", 0):
                e.update(km=round(b.km), tx=b.tx, rx=b.rx, dmode=b.mode)
        for key, e in active.items():
            if key not in ops:
                e["end"] = e.get("last", now)
        cutoff = now - HISTORY_KEEP_S
        self.history = [e for e in self.history if (e.get("end") or now) >= cutoff]
        self._save_history()

    def recent_history(self, hours=6):
        cutoff = time.time() - hours * 3600
        return [e for e in self.history if (e.get("end") or time.time()) >= cutoff]

    # ---- scheduled weather bulletins
    def _local(self, now):
        return time.gmtime(now + float(self.cfg.get("wx_utc_offset", -4)) * 3600)

    def wx_next(self, now=None):
        """{kind: 'HH:MM'} of the next scheduled bulletin of each enabled kind."""
        now = now or time.time()
        lt = self._local(now)
        cur = f"{lt.tm_hour:02d}:{lt.tm_min:02d}"
        out = {}
        for kind in ("general", "marine", "aviation"):
            if not self.cfg.get(f"wx_{kind}"):
                continue
            try:
                ts = sorted(wx.parse_times(self.cfg.get(f"wx_{kind}_times")))
            except ValueError:
                continue
            if ts:
                out[kind] = next((t for t in ts if t > cur), ts[0])
        return out

    def _wx_schedule(self, now):
        lt = self._local(now)
        today = time.strftime("%Y-%m-%d", lt)
        mins_now = lt.tm_hour * 60 + lt.tm_min
        for kind in ("general", "marine", "aviation"):
            if not self.cfg.get(f"wx_{kind}"):
                continue
            try:
                times = wx.parse_times(self.cfg.get(f"wx_{kind}_times"))
            except ValueError:
                continue
            for t in times:
                key = f"{kind}@{t}@{today}"
                h, m = map(int, t.split(":"))
                due = h * 60 + m
                # send at the scheduled minute; allow 10 min grace (e.g. PC woke up),
                # never send a backlog of old bulletins
                if key not in self.wx_done and 0 <= mins_now - due <= 10:
                    self.wx_done.add(key)
                    threading.Thread(target=self.send_wx, args=(kind,), daemon=True).start()
        if len(self.wx_done) > 200:
            self.wx_done = {k for k in self.wx_done if k.endswith(today)}

    def send_wx(self, kind):
        c = self.cfg
        try:
            lines = wx.build(kind, c)
        except Exception as e:
            self.wx_last[kind] = (time.time(), f"error: {e}")
            self.add_log("ERR", f"Weather ({kind}) not sent - could not get data: {e}")
            return
        self.wx_last[kind] = (time.time(), lines)
        group = wx.GROUP[kind]
        self.add_log("WX", f"{kind.capitalize()} weather bulletin ({len(lines)} line(s))")
        for i, line in enumerate(lines[:wx.MAX_LINES], start=1):
            addr = f"BLN{i}{group}"[:9].ljust(9)
            self.transmit(f":{addr}:{line}", rf=bool(c.get("wx_rf")), force_rf=bool(c.get("wx_rf")))
            time.sleep(2)

    def check_tropical(self, force=False):
        c = self.cfg
        try:
            alerts = wx.tropical_alerts(c)
        except Exception as e:
            self.add_log("ERR", f"Tropical check failed: {e}")
            return
        if not alerts:
            if force:
                self.add_log("WX", f"No active Atlantic tropical systems within {c.get('tc_radius_km')} km of {c['wx_place']}")
            self.tc_alerts = []
            return
        self.tc_alerts = [lines for _k, lines in alerts]
        for key, lines in alerts:
            if key in self.tc_sent and not force:
                continue
            self.tc_sent.add(key)
            self.tc_seq += 1
            self.add_log("WX", "TROPICAL ALERT: " + " / ".join(lines[:2]))
            for i, line in enumerate(lines, start=1):
                self.transmit(f":{f'BLN{i}TC'.ljust(9)}:{line}", rf=bool(c.get("tc_rf")), force_rf=bool(c.get("tc_rf")))
                time.sleep(2)

    def psk_wait(self, now=None):
        """Seconds until another PSKReporter query is allowed (0 = go now).
        Rules: 10 minutes between queries and at most 6 in any hour."""
        now = time.time() if now is None else now
        while self.psk_times and now - self.psk_times[0] >= 3600:
            self.psk_times.popleft()
        wait = 0.0
        if self.psk_times:
            wait = self.psk_times[-1] + PSK_MIN_GAP - now
        if len(self.psk_times) >= PSK_MAX_PER_HOUR:
            wait = max(wait, self.psk_times[0] + 3600 - now)
        return max(0.0, wait)

    def run_check(self):
        c = self.cfg
        if not self.demo_xml:
            if self.psk_wait() > 0:
                return
            self.psk_times.append(time.time())
        try:
            if self.demo_xml:
                with open(self.demo_xml, "rb") as f:
                    raw = f.read()
            else:
                raw = eng.fetch_pskreporter(c["window"] * 60, c["call"])
            spots = eng.parse_spots(raw)
            self.check_error = ""
        except Exception as e:
            self.check_error = f"PSKReporter: {e}"
            self.add_log("ERR", self.check_error)
            return
        self.last_check = time.time()
        spots = [s for s in spots if s.band in c["bands"]]
        self.spot_count = len(spots)
        self.car_count = sum(1 for s in spots if "CAR" in (s.tx_region, s.rx_region))
        me = eng.grid_to_latlon(c["grid"])
        if me:
            self.local = []
            for s in spots:
                for call, g in ((s.tx, s.tx_grid), (s.rx, s.rx_grid)):
                    p = eng.grid_to_latlon(g)
                    if p and eng.haversine_km(me, p) <= c["local_km"] and s.km >= 200:
                        self.local.append({"band": s.band, "tx": s.tx, "rx": s.rx,
                                           "km": round(s.km), "mode": s.mode, "snr": s.snr})
                        break
            self.local = sorted(self.local, key=lambda x: -x["km"])[:8]
        self.map_paths = self._map_paths(spots)
        if c.get("wolf_on"):
            try:
                self._wolf_update(spots)
            except Exception as e:
                self.add_log("ERR", f"Wolf Mode: {e}")
        ops = eng.detect(spots, c["min_km"], c["min_paths"], c["strong_km"],
                         modes=set(c.get("watch_modes") or ALL_MODES))
        ops = {k: v for k, v in ops.items() if k[1] in c["regions"]}
        self.openings = ops
        self._update_rf_state()
        self._record_history(ops)
        self.add_log("PROP", f"{len(spots)} spots, {self.car_count} Caribbean, "
                             f"{len(ops)} opening(s)" + (": " + ", ".join(f"{b} CAR-{r} {eng.MODE_NAMES.get(o.mode, '')}" for (b, r), o in sorted(ops.items())) if ops else ""))
        for k, o in ops.items():
            self.last_mode[k] = o.mode
        msgs = eng.plan_announcements(ops, self.ann_state, int(time.time()),
                                      c["repeat"] * 60, c["announce_close"], c["group"])
        slot_key = {slot: key for key, slot in eng.BLN_SLOT.items()}
        for addr, text in msgs:
            mode = self.last_mode.get(slot_key.get(addr[3:4]))
            self.transmit(eng.info_field(addr, text), mode=mode)
        try:
            eng.save_state(STATE_PATH, self.ann_state)
        except OSError:
            pass

    # ---- live map
    def _map_paths(self, spots, min_km=300, limit=400):
        """Caribbean paths (one line per station pair and band) for the map."""
        best = {}
        for s in spots:
            if "CAR" not in (s.tx_region, s.rx_region) or s.km < min_km:
                continue
            a, b = eng.grid_to_latlon(s.tx_grid), eng.grid_to_latlon(s.rx_grid)
            if not (a and b):
                continue
            key = (s.tx, s.rx, s.band)
            if key in best and best[key]["ts"] >= (s.ts or 0):
                continue
            best[key] = {"band": s.band, "pm": s.prop_mode or eng.classify_mode(s),
                         "tx": s.tx, "rx": s.rx, "tg": s.tx_grid, "rg": s.rx_grid,
                         "a": [round(a[0], 3), round(a[1], 3)], "b": [round(b[0], 3), round(b[1], 3)],
                         "km": round(s.km), "mode": s.mode, "snr": s.snr, "ts": s.ts or 0,
                         "far": s.rx_region if s.tx_region == "CAR" else s.tx_region}
        return sorted(best.values(), key=lambda p: -p["km"])[:limit]

    def map_data(self):
        c = self.cfg
        me = eng.grid_to_latlon(c.get("grid", ""))
        return {"now": time.time(), "last_check": self.last_check,
                "next_check": self.next_check if self.running and not self.prop_off else 0, "running": self.running,
                "me": {"call": c.get("call", ""), "grid": c.get("grid", ""),
                       "pos": [round(me[0], 3), round(me[1], 3)] if me else None},
                "paths": self.map_paths,
                "open": [{"band": b, "region": r, "mode": o.mode} for (b, r), o in sorted(self.openings.items())],
                "names": eng.MODE_NAMES}

    # ---- RF mode: always, or only while selected propagation modes are open
    def rf_possible(self):
        c = self.cfg
        return bool(c.get("rf_announce") and self.tnc and c.get("tnc") != "none")

    def rf_live(self, mode=None):
        """Is RF transmission switched on right now (for this message's mode)?"""
        if not self.rf_possible() or self.qsy_hz:
            return False            # moved to a chat channel: beacons and alerts wait (APRS-IS still works)
        if self.cfg.get("rf_when", "always") == "always":
            return True
        chosen = set(self.cfg.get("rf_modes") or [])
        if mode is not None:                   # an alert about one opening
            return mode in chosen
        return bool(chosen & {o.mode for o in self.openings.values()})   # beacons etc.

    def _update_rf_state(self):
        live = self.rf_live()
        prev = getattr(self, "_rf_was_live", None)
        if prev is not None and live != prev and self.cfg.get("rf_when", "always") != "always":
            chosen = set(self.cfg.get("rf_modes") or [])
            names = ", ".join(f"{b} {eng.MODE_NAMES.get(o.mode, '')} CAR-{r}" for (b, r), o in sorted(self.openings.items()) if o.mode in chosen)
            self.add_log("SYS", f"RF now ON - {names}" if live else "Opening faded - RF back to standby (APRS-IS only)")
        self._rf_was_live = live

    # ---- TX paths
    def same_tx_ok(self, info, now=None):
        """Identical beacon/bulletin text may go out at most SAME_TX_PER_HOUR times an hour."""
        now = time.time() if now is None else now
        for k in [k for k, v in self.tx_seen.items() if not v or now - v[-1] >= 3600]:
            del self.tx_seen[k]
        times = [t for t in self.tx_seen.get(info, []) if now - t < 3600]
        if len(times) >= SAME_TX_PER_HOUR:
            self.tx_seen[info] = times
            return False
        self.tx_seen[info] = times + [now]
        return True

    def transmit(self, info, rf=True, net=True, mode=None, force_rf=False):
        if self.commercial:
            return      # APRS bulletins / beacons never go out on a commercial channel
        if not self.same_tx_ok(info):
            self.add_log("SYS", f"Held back - the same text was already sent {SAME_TX_PER_HOUR} times "
                                f"this hour: {info[:60]}")
            return
        c = self.cfg
        call = c["call"]
        sent = []
        if rf and (self.rf_live(mode) or (force_rf and self.rf_possible())):
            path = [p.strip() for p in (c.get("rf_path") or "").split(",") if p.strip()]
            try:
                self._tx_tnc().send(call, eng.TOCALL, path, info)
                sent.append("RF")
            except Exception as e:
                self.add_log("ERR", f"RF send: {e}")
        if net and self.isc:
            try:
                self.isc.send(f"{call}>{eng.TOCALL},TCPIP*:{info}")
                sent.append("IS")
            except Exception as e:
                self.add_log("ERR", f"APRS-IS send: {e}")
        self.add_log("TX", f"[{'+'.join(sent) or 'nothing sent'}] {call}>{eng.TOCALL}:{info}")

    def send_beacon(self):
        if self.commercial:
            return
        c = self.cfg
        p = eng.grid_to_latlon(c["grid"])
        if not p:
            return
        lat, lon = latlon_to_aprs(*p)
        sym = (c.get("symbol") or "/-").ljust(2)[:2]
        self.transmit(f"={lat}{sym[0]}{lon}{sym[1]}{c.get('beacon_comment', '')[:43]}")

    def send_test(self):
        self.transmit(f":{eng.bln_addressee('9', self.cfg['group'])}:{APP_NAME} test - no propagation alert",
                      force_rf=True)

    # ---- two-way chat
    # ---- saved chats + ICS-309 comms log (survive restarts and power cuts)
    def _chat_file(self):
        name = re.sub(r"[^A-Z0-9-]", "_", self.chat.mycall)
        return os.path.join(CHAT_DIR, ("com_" if self.commercial else "ham_") + name + ".json")

    def _attach_chat(self):
        self.chat.on_log = self._commlog_add
        self.chat.store_forward = bool(self.cfg.get("store_forward", True))
        self.chat.relay_enabled = bool(self.cfg.get("relay_enabled"))
        self.chat.blocked = set(block_set(self.cfg))
        self.chat.relay_candidates = self.relay_candidates
        self.chat.is_ok = lambda: bool(self.isc and self.isc.verified)
        self.chat.on_tw = lambda call: self._tw_seen(call, "chat")
        self.chat.links = self.linkctl
        robust = self.cfg.get("tnc") == "sound" and self.cfg.get("modem") == "robust"
        self.chat.on_qsy = None if self.commercial else self._on_qsy   # a licensed commercial channel never moves
        tncmod.set_zone(tncmod.zone_of(self.cfg))
        self.chat.qsy_channels = tncmod.zone_qsy(self.cfg)     # empty: chats stay on the APRS frequency
        home = tncmod.aprs_freq_hz(self.cfg)
        self.chat.rf_block = ("Hails and email are never sent on a satellite / ISS frequency (145.800-146.000, 435-438). "
                              "Change the APRS frequency, or message by internet."
                              if home and tncmod.on_satellite(home / 1e6) else None)
        self.chat.qsy_delay = 60 if robust else 6
        gw = bool(self.cfg.get("mail_gw_enabled")) and not self.commercial and bool(self.cfg.get("smtp_host"))
        self.chat.on_mail_out = self._gateway_send if gw else None
        self.chat.on_stop = self._on_stop
        self.chat.mail_session_s = max(1, int(self.cfg.get("mail_session_min") or 10)) * 60
        self.qsy_hz = None
        self.chat.retry = (90, 150, 240) if robust else tncmod.RETRY_S
        self.commlog = []
        try:
            with open(self._chat_file(), "rb") as f:
                raw = f.read()
            if self.commercial:                      # stored encrypted with the network key
                raw = tncmod.seal_open(bytes.fromhex(self.cfg["com_key"]), raw)
            d = json.loads(raw)
            self.chat.load(d.get("chat"))
            self.commlog = d.get("log", [])[-5000:]
        except (OSError, ValueError, TypeError):
            pass
        self._chat_saved = self.chat.seq

    def _save_chat(self):
        if not self.chat:
            return
        try:
            os.makedirs(CHAT_DIR, exist_ok=True)
            raw = json.dumps({"chat": self.chat.to_dict(), "log": self.commlog[-5000:]}).encode()
            if self.commercial:
                raw = tncmod.seal_close(bytes.fromhex(self.cfg["com_key"]), raw)
            tmp = self._chat_file() + ".tmp"
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, self._chat_file())
            self._chat_saved, self._chat_save_t = self.chat.seq, time.time()
        except (OSError, ValueError) as e:
            self.add_log("ERR", f"could not save chat: {e}")

    def _commlog_add(self, direction, peer, text, via):
        me = self.chat.mycall if self.chat else ""
        self.commlog.append({"t": time.time(), "from": peer if direction == "in" else me,
                             "to": me if direction == "in" else peer, "via": via or "", "text": text})

    def commlog_csv(self):
        import csv
        import io
        c = self.cfg
        off = float(c.get("wx_utc_offset") or 0)
        buf = io.StringIO()
        w = csv.writer(buf)
        station = (c.get("com_callsign") or c.get("com_unit")) if self.commercial else c.get("call")
        w.writerow(["ICS-309 style communications log", f"Station: {station}",
                    f"Exported {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC", f"{APP_NAME} {APP_VERSION}"])
        w.writerow(["Date/time (local)", "Date/time (UTC)", "From", "To", "Via", "Message"])
        for e in self.commlog:
            w.writerow([time.strftime("%Y-%m-%d %H:%M", time.gmtime(e["t"] + off * 3600)),
                        time.strftime("%Y-%m-%d %H:%M", time.gmtime(e["t"])),
                        e["from"], e["to"], e["via"], e["text"]])
        return buf.getvalue()

    # ---- stations heard on RF (a local picture with no internet)
    def _heard_add(self, line, src=None):
        head = line.partition(":")[0]
        if ">" not in head:
            return
        call = (src or head.split(">", 1)[0]).rstrip("*").upper()
        path = head.split(">", 1)[1].split(",")[1:]
        digi = [x.rstrip("*") for x in path if x.endswith("*") and not x.startswith(("WIDE", "TCPIP", "q"))]
        h = self.heard.setdefault(call, {"call": call, "count": 0, "first": time.time()})
        h.update(last=time.time(), via=("via " + digi[-1]) if digi else "direct", direct=not digi)
        h["count"] += 1
        dst = head.split(">", 1)[1].split(",")[0].upper()
        if dst == eng.TOCALL or call in self.tw_users:
            h["tw"] = True
            self._tw_seen(call, "RF")
        if len(self.heard) > 300:
            oldest = min(self.heard.values(), key=lambda x: x["last"])
            self.heard.pop(oldest["call"], None)

    # ---- Wolf Mode: DX stations on PSKReporter that are also on APRS, ready to ping
    SW = {"APZPRP": "Tradewind", "APDR": "APRSdroid", "APK": "Kenwood", "APY": "Yaesu", "APDW": "Direwolf",
          "APX": "Xastir", "APWW": "APRSIS32", "APIC": "Icom", "APLR": "LoRa", "APMI": "Mic-E", "APOT": "OpenTracker"}

    def _wolf_update(self, spots):
        c, now = self.cfg, time.time()
        me = c["call"].split("-")[0].upper()
        min_km = float(c.get("wolf_min_km") or 500)
        for sp in spots:
            if sp.km < min_km:
                continue
            for call, grid in ((sp.tx, sp.tx_grid), (sp.rx, sp.rx_grid)):
                base = call.split("/")[0].split("-")[0].upper()
                if not base or base == me or not CALL_RE.match(base):
                    continue
                w = self.wolf.setdefault(base, {"call": base, "km": 0, "aprs_call": "", "aprs_t": 0, "sw": "", "pinged": 0})
                w.update(t=now, grid=grid, band=sp.band, mode=sp.mode, snr=sp.snr, prop=sp.prop_mode or "")
                w["km"] = max(w["km"], round(sp.km))
        for b in [b for b, w in self.wolf.items() if now - w["t"] > 7200]:
            del self.wolf[b]
        keep = sorted(self.wolf.values(), key=lambda w: -w["km"])[:40]
        self.wolf = {w["call"]: w for w in keep}
        if self.isc and self.wolf:
            self.isc.set_filter("b/" + "/".join(f"{b}*" for b in self.wolf))

    def _wolf_heard(self, line):
        head = line.partition(":")[0]
        if ">" not in head:
            return
        src, dst = head.split(">", 1)[0].upper(), head.split(">", 1)[1].split(",")[0].upper()
        w = self.wolf.get(src.split("-")[0])
        if w:
            w.update(aprs_call=src, aprs_t=time.time(),
                     sw=next((n for k, n in self.SW.items() if dst.startswith(k)), "APRS"))

    def wolf_stopped(self):
        return {x.strip().upper() for x in str(self.cfg.get("wolf_stop") or "").split(",") if x.strip()}

    def wolf_ping(self, base, text=""):
        c, now = self.cfg, time.time()
        w = self.wolf.get(str(base).upper())
        if not w or not w.get("aprs_call") or now - w["aprs_t"] > 7200:
            raise ValueError("That station has not been seen on APRS recently.")
        if w["call"] in self.wolf_stopped():
            raise ValueError(f"{w['call']} asked not to be pinged.")
        if now - w.get("pinged", 0) < 86400:
            raise ValueError(f"{w['call']} was already pinged today.")
        while self.wolf_pings and now - self.wolf_pings[0] > 3600:
            self.wolf_pings.popleft()
        if len(self.wolf_pings) >= int(c.get("wolf_per_hour") or 5):
            raise ValueError("Ping limit reached for this hour - try later.")
        if not (self.isc and self.isc.verified and self.chat):
            raise ValueError("Wolf pings go by internet: APRS-IS must be connected.")
        qth = (c.get("wolf_qth") or c.get("grid", "")[:4]).strip()
        text = (text or f"{c['call'].split('-')[0]} {qth}: {w['band']} open to you now, {w['km']} km. QSO? 73").strip()
        self.chat.open_direct(w["aprs_call"], "is")
        self.chat.send(w["aprs_call"], text[:tncmod.MAX_TEXT])
        w["pinged"] = now
        self.wolf_pings.append(now)
        self.add_log("SYS", f"Wolf ping to {w['aprs_call']} ({w['km']} km)")

    def _on_stop(self, call):
        base = tncmod.norm_call(call).split("-")[0]
        if base in self.wolf or base in self.wolf_stopped():
            with self.lock:
                self.cfg["wolf_stop"] = ",".join(sorted(self.wolf_stopped() | {base}))
                save_config(self.cfg)
            self.add_log("SYS", f"{base} asked for no more Wolf pings - added to the do-not-ping list")

    def zone_info(self, c=None):
        c = c or self.cfg
        z = tncmod.zone_of(c)
        hz = tncmod.aprs_freq_hz(c)
        return {"key": z, "name": tncmod.ZONES[z]["name"], "auto": str(c.get("zone") or "auto") == "auto",
                "from_grid": tncmod.zone_for_grid(c.get("grid", "")), "aprs": f"{hz / 1e6:.3f}" if hz else "",
                "zone_aprs": tncmod.ZONES[z]["aprs"], "qsy": tncmod.zone_qsy(c),
                "qsy_mode": c.get("qsy_mode") or "auto", "note": tncmod.ZONES[z]["note"],
                "satellite": bool(hz and tncmod.on_satellite(hz / 1e6))}

    def wolf_state(self):
        now = time.time()
        while self.wolf_pings and now - self.wolf_pings[0] > 3600:
            self.wolf_pings.popleft()
        stop = self.wolf_stopped()
        rows = []
        for w in sorted(self.wolf.values(), key=lambda w: (-(now - w["aprs_t"] < 7200), -w["km"])):
            on_aprs = bool(w.get("aprs_call")) and now - w["aprs_t"] < 7200
            rows.append({k: w.get(k) for k in ("call", "km", "band", "mode", "snr", "prop", "grid", "aprs_call", "sw")} |
                        {"aprs_ago": int(now - w["aprs_t"]) if on_aprs else None, "stopped": w["call"] in stop,
                         "pinged": bool(now - w.get("pinged", 0) < 86400)})
        return {"on": bool(self.cfg.get("wolf_on")), "rows": rows[:40],
                "on_aprs": sum(1 for r in rows if r["aprs_ago"] is not None),
                "pings_left": max(0, int(self.cfg.get("wolf_per_hour") or 5) - len(self.wolf_pings))}

    # ---- disaster session: third-party (non-ham) email allowed only while it is on
    def disaster_active(self):
        c = self.cfg
        return bool(c.get("disaster_on")) and time.time() < float(c.get("disaster_until") or 0)

    def set_disaster(self, on, hours=24, reason=""):
        with self.lock:
            if on:
                hours = max(1, min(168, int(hours or 24)))
                self.cfg.update(disaster_on=True, disaster_until=time.time() + hours * 3600,
                                disaster_reason=str(reason or "")[:120])
            else:
                self.cfg.update(disaster_on=False, disaster_until=0)
            save_config(self.cfg)
        msg = (f"DISASTER SESSION ON for {hours} h - third-party email allowed. Reason: {reason or '-'}" if on
               else f"Disaster session OFF ({reason or 'by operator'}) - email to hams only")
        self.add_log("SYS", msg)
        self._commlog_add("out", "LOG", msg, "station")

    # ---- Tradewind email gateway (this station relays radio email to the internet and back)
    def _gateway_send(self, src, to, subject, body):
        c = self.cfg
        src = tncmod.norm_call(src)
        allowed = {tncmod.norm_call(x).split("-")[0] for x in re.split(r"[,\s]+", str(c.get("mail_allowed_calls") or "")) if x.strip()}
        if not CALL_RE.match(src) or (allowed and src.split("-")[0] not in allowed):
            return False, "not an approved callsign"
        if not tncmod.EMAIL_RE.match(to or ""):
            return False, "bad address"
        if not (tncmod.ham_address(to) or self.disaster_active()):
            return False, "non-ham address needs a disaster session"
        if len(body) > tncmod.MAIL_MAX_CHARS:
            return False, "too long"
        day = (src.split("-")[0], time.strftime("%Y-%m-%d"))
        if self.mail_count.get(day, 0) >= int(c.get("mail_per_day") or 10):
            return False, "daily limit reached"
        import smtplib
        from email.message import EmailMessage
        m = EmailMessage()
        m["From"] = c.get("smtp_from") or c.get("smtp_user")
        m["To"] = to
        m["Subject"] = f"{src}: {subject}" if subject else f"{src}: message by amateur radio"
        m.set_content(f"{body}\n\n-- \nSent by {src} over amateur radio through the Tradewind gateway {c['call']}.\n"
                      f"To reply, keep the subject starting with '{src}:' and keep it short (under "
                      f"{tncmod.MAIL_MAX_CHARS} characters, plain text). Replies reach {src} by radio.")
        try:
            host, port = c["smtp_host"].strip(), int(c.get("smtp_port") or 587)
            ctx = mail_tls_context(host)
            if port == 465:                           # implicit TLS
                srv = smtplib.SMTP_SSL(host, port, timeout=20, context=ctx)
            else:
                srv = smtplib.SMTP(host, port, timeout=20)
            with srv:
                if port != 465:
                    if c.get("smtp_tls", True):
                        srv.starttls(context=ctx)     # raises if the certificate is wrong: no password is sent
                    elif host.lower() not in LOCAL_MAIL_HOSTS:
                        raise OSError("refusing to send the mail password without encryption - tick Use STARTTLS")
                if c.get("smtp_user"):
                    srv.login(c["smtp_user"], c.get("smtp_pass") or "")
                srv.send_message(m)
        except Exception as e:
            self.mail_error = f"SMTP: {e}"
            self.add_log("ERR", f"Gateway email from {src} failed: {e}")
            return False, "mail server error"
        self.mail_count[day] = self.mail_count.get(day, 0) + 1
        self.add_log("SYS", f"Gateway: email from {src} to {to} sent")
        self._commlog_add("in", src, f"EMAIL to {to}: {subject} | {body}", "gateway")
        return True, ""

    def _imap_poll(self):
        """Collect replies: subject starting 'CALL:' goes to that station by radio (ham senders, or any in a disaster)."""
        import email
        import imaplib
        from email.header import decode_header, make_header
        c = self.cfg
        try:
            host, port = c["imap_host"].strip(), int(c.get("imap_port") or 993)
            ctx = mail_tls_context(host)
            if port == 993:
                box = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=30)
            else:                                     # e.g. Proton Mail Bridge 1143: STARTTLS
                box = imaplib.IMAP4(host, port, timeout=30)
                box.starttls(ssl_context=ctx)
            box.login(c.get("imap_user") or c.get("smtp_user"), c.get("imap_pass") or c.get("smtp_pass") or "")
            box.select("INBOX")
            _, data = box.search(None, "UNSEEN")
            for num in (data[0].split() if data and data[0] else [])[:20]:
                _, msg_data = box.fetch(num, "(RFC822)")
                box.store(num, "+FLAGS", "\\Seen")
                msg = email.message_from_bytes(msg_data[0][1])
                subj = str(make_header(decode_header(msg.get("Subject", "")))).strip()
                sender = email.utils.parseaddr(msg.get("From", ""))[1]
                mm = re.match(r"^(?:re:\s*)*([A-Za-z0-9]{3,7}(?:-\d{1,2})?)\s*:\s*(.*)$", subj, re.I)
                if not mm or not CALL_RE.match(mm.group(1).upper()):
                    continue
                call, rest = mm.group(1).upper(), mm.group(2)
                if not (tncmod.ham_address(sender) or self.disaster_active()):
                    self.add_log("SYS", f"Gateway: email from {sender} for {call} held back - non-ham sender outside a disaster session")
                    continue
                text = ""
                for part in (msg.walk() if msg.is_multipart() else [msg]):
                    if part.get_content_type() == "text/plain":
                        text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                        break
                lines = []
                for line in text.splitlines():
                    if line.startswith(">") or re.match(r"^On .* wrote:$", line.strip()) or line.strip() == "--":
                        break
                    lines.append(line.strip())
                body = " ".join(x for x in lines if x)[:tncmod.MAIL_MAX_CHARS]
                if self.chat and body:
                    self.chat.mail_deliver(call, sender, rest, body)
                    self._commlog_add("out", call, f"EMAIL from {sender}: {rest} | {body}", "gateway")
            box.logout()
            self.mail_error = ""
        except Exception as e:
            self.mail_error = f"IMAP: {e}"
            self.add_log("ERR", f"Gateway mailbox check failed: {e}")

    # ---- Tradewind users heard (the "Tradewind" badge)
    def _tw_seen(self, call, via):
        call = tncmod.norm_call(call)
        if not call or call == self.cfg.get("call", "").upper():
            return
        now = time.time()
        u = self.tw_users.get(call)
        changed = not u or now - u["last"] > 600
        if not u:
            u = self.tw_users[call] = {"first": now, "last": now, "via": via}
            self.add_log("SYS", f"New Tradewind station heard: {call} ({via})")
        u.update(last=now, via=via)
        if call in self.heard:
            self.heard[call]["tw"] = True
        if changed:
            save_tw_users(self.tw_users)

    def tw_summary(self):
        now = time.time()
        u = self.tw_users
        return {"today": sum(1 for x in u.values() if now - x["last"] < 86400),
                "week": sum(1 for x in u.values() if now - x["last"] < 7 * 86400),
                "new_week": sum(1 for x in u.values() if now - x["first"] < 7 * 86400),
                "total": len(u),
                "list": sorted(({"call": c, **v} for c, v in u.items()), key=lambda x: -x["last"])[:60]}

    def relay_candidates(self, dest):
        """RF-first routing: Tradewind stations heard directly on RF in the last 30 minutes, newest first."""
        now = time.time()
        hs = [h for h in self.heard.values() if h.get("tw") and h.get("direct") and now - h["last"] < LOCAL_HEARD_S
              and h["call"] != tncmod.norm_call(dest)]
        return [h["call"] for h in sorted(hs, key=lambda h: -h["last"])]

    def _pick_link(self, call, route):
        """Which radio link a message goes out on: 'A' (main radio), 'B' (second link) or None."""
        if route == "smart":
            route = "rf"
        if route == "auto":
            return self.linkctl.choose(call) if self.linkctl else "A"
        if str(route).startswith("link:"):
            return route[5:]
        return "A" if route in ("rf", "rf_is") else None

    def _on_qsy(self, mhz):
        """Retune for a chat QSY (mhz) or back to the APRS frequency (None). Beacons and RF alerts pause meanwhile."""
        self.qsy_hz = int(round(float(mhz) * 1e6)) if mhz else None
        target = self.qsy_hz or tncmod.aprs_freq_hz(self.cfg)
        tnc = self.tnc
        if tnc and hasattr(tnc, "tune") and getattr(getattr(tnc, "ptt", None), "can_tune", False) and target:
            tnc.tune(target)
            self.add_log("SYS", f"Radio tuned to {target / 1e6:.3f} MHz for " + ("chat (QSY)" if mhz else "APRS"))
        else:
            self.add_log("SYS", f"Set the radio to {target / 1e6:.3f} MHz" if target else "Set the radio back to the APRS frequency")

    def _tx_tnc(self):
        """The radio that transmits: the main TNC, or link B when the main one is a receive-only SDR."""
        if isinstance(self.tnc, tncmod.SdrTNC):
            if not self.tnc2:
                raise IOError("the SDR only receives - add a radio as the second link to transmit")
            return self.tnc2
        return self.tnc

    def _kiss_client_tx(self, frame):
        """A frame from another app connected to the KISS server: transmit it if it is from this station."""
        line = afsk.decode_ax25(frame) or ""
        src = line.split(">", 1)[0].split("-")[0].upper()
        if not line or src != self.cfg["call"].split("-")[0].upper():
            self.add_log("ERR", f"KISS client frame refused (not from {self.cfg['call'].split('-')[0]}): {line[:60]}")
            return
        tx = self._tx_tnc()
        if not hasattr(tx, "send_frame"):
            raise IOError("this radio link cannot send frames from other apps")
        tx.send_frame(frame)
        self.add_log("TX", f"[KISS client] {line}")

    def _link_send(self, link, src, dst, path, info):
        if link == "B":
            if not self.tnc2:
                raise IOError("the second radio link is not running (MENU > RADIO / TNC > Second radio link)")
            self.tnc2.send(src, dst, path, info)
        else:
            if not self.rf_possible() and not self.commercial:
                raise IOError("RF needs a radio: set a TNC under MENU > RADIO / TNC, or choose APRS-IS")
            self._tx_tnc().send(src, dst, path, info)

    def _chat_send(self, call, info, route):
        """Send one APRS message for the chat (RF ignores the propagation-mode standby).
        Returns the radio link used ('A' / 'B'), 'IS', or raises."""
        c, sent = self.cfg, []
        link = self._pick_link(call, route)
        if link:
            h = self.heard.get(tncmod.norm_call(call))
            direct = self.qsy_hz or (h and h.get("direct") and time.time() - h["last"] < LOCAL_HEARD_S)
            # stations that hear each other (or a QSY'd simplex chat) need no digipeater: half the airtime
            path = [] if direct else [p.strip() for p in (c.get("rf_path") or "").split(",") if p.strip()]
            peer = self.chat.peers.get(tncmod.norm_call(call)) if self.chat else None
            if peer and peer.get("nogate"):
                path = path + ["NOGATE"]          # ask iGates not to copy this chat to the internet
            self._link_send(link, c["call"], eng.TOCALL, path, info)
            sent.append("RF" if link == "A" else f"RF:{link}")
        if route in ("rf_is", "is") and self.isc and self.isc.verified:
            self.isc.send(f"{c['call']}>{eng.TOCALL},TCPIP*:{info}")
            sent.append("IS")
        if not sent:
            raise IOError("nothing to send with - no radio and APRS-IS not connected")
        self.add_log("MSG", f"[{'+'.join(sent)}] {c['call']}>{call}: {info[11:]}")
        return link or "IS"

    def _com_id(self, force=False):
        """Clear-text station identification required by most commercial licences."""
        c, now = self.cfg, time.time()
        if force or now - self.last_id >= max(5, int(c.get("com_id_min") or 30)) * 60:
            text = f">ID {c['com_callsign'].strip()} {c['com_licensee'].strip()}"[:80]
            self.tnc.send(c["com_unit"].upper(), "ID", [], text)
            if self.tnc2:
                self.tnc2.send(c["com_unit"].upper(), "ID", [], text)
            self.last_id = now
            self.add_log("SEC", f"station ID sent in clear: {text[1:]}")

    def _com_send(self, call, info, route):
        if not (self.tnc and self.sec):
            raise IOError("station is off")
        if self.tnc.kind == "none":
            raise IOError("commercial mode needs a radio (TNC)")
        self._com_id()
        unit = self.cfg["com_unit"].upper()
        frame = self.sec.seal(unit, info)
        link = self._pick_link(call, route if route in ("auto",) or str(route).startswith("link:") else "rf") or "A"
        self._link_send(link, unit, tncmod.SEC_DEST, [], frame)
        self.add_log("SEC", f"encrypted message to {call} on link {link} ({len(frame)} bytes on air)")
        return link

    def _com_rx(self, line, link="A"):
        head, _, info = line.partition(":")
        if ">" not in head:
            return
        src = head.split(">", 1)[0].rstrip("*").upper()
        dst = head.split(">", 1)[1].split(",")[0].rstrip("*").upper()
        if dst != tncmod.SEC_DEST or not info.startswith(tncmod.SEC_TAG):
            return
        plain = self.sec.open(src, info)
        if plain is None:
            self.add_log("SEC", f"frame from {src} refused (wrong key, altered or replayed)")
            return
        if self.chat:
            self.chat.on_packet(f"{src}>{tncmod.SEC_DEST}:{plain}", "RF" if link == "A" else f"RF:{link}")

    def set_block(self, call, block):
        call = tncmod.norm_call(call)
        if not tncmod.valid_call(call):
            raise ValueError("Enter a callsign to block or unblock.")
        with self.lock:
            calls = block_set(self.cfg)
            (calls.add if block else calls.discard)(call)
            self.cfg["block_list"] = ",".join(sorted(calls))
            save_config(self.cfg)
            if self.chat:
                self.chat.blocked = set(calls)
                self.chat.muted.pop(call, None)
                self.chat.seq += 1
        self.add_log("SYS", f"{call} {'blocked' if block else 'unblocked'}")

    # ---- station PIN: protects the face plate from other devices and (optionally) the settings
    def pin_ok(self, pin):
        c = self.cfg
        if not c.get("pin_hash"):
            return False
        return hmac.compare_digest(_pin_hash(str(pin or ""), c.get("pin_salt", "")), c["pin_hash"])

    def set_pin(self, action, old="", new="", lock=False):
        """action: 'set' (new or changed PIN), 'remove', or 'lock' (lock / unlock the settings)."""
        c = self.cfg
        if c.get("pin_hash") and not self.pin_ok(old):
            raise ValueError("The current PIN is wrong.")
        with self.lock:
            if action == "set":
                new = str(new or "").strip()
                if not re.fullmatch(r"\d{4,8}", new):
                    raise ValueError("The PIN must be 4 to 8 digits.")
                salt = os.urandom(8).hex()
                c.update(pin_salt=salt, pin_hash=_pin_hash(new, salt))
            elif action == "remove":
                c.update(pin_salt="", pin_hash="", lock_settings=False)
            elif action == "lock":
                if not c.get("pin_hash"):
                    raise ValueError("Set a PIN first.")
                c["lock_settings"] = bool(lock)
            else:
                raise ValueError("Unknown PIN action.")
            save_config(c)
        self.add_log("SYS", "station PIN " + ("set" if c.get("pin_hash") else "removed") +
                     (" - settings locked" if c.get("lock_settings") else ""))

    def session_token(self):
        return hmac.new(bytes.fromhex(self.cfg["pin_hash"]), b"tradewind-session", hashlib.sha256).hexdigest()

    def set_com_key(self, net, passphrase):
        key = tncmod.derive_key(net, passphrase)
        fp = tncmod.key_fingerprint(key)
        with self.lock:
            self.cfg.update(com_net=net.strip().upper(), com_key=key.hex(), com_fp=fp)
            save_config(self.cfg)
            if self.running and self.commercial:
                self.sec = tncmod.SecureLink(key)
        self.add_log("SEC", f"network key set for {net.strip().upper()} - fingerprint {fp}")
        return fp

    def on_is_line(self, line):
        if self.wolf:
            try:
                self._wolf_heard(line)
            except Exception:
                pass
        try:
            head = line.partition(":")[0]
            if ">" in head and head.split(">", 1)[1].split(",")[0].upper() == eng.TOCALL:
                self._tw_seen(head.split(">", 1)[0], "APRS-IS")
            self._igate_to_rf(line)
        except Exception as e:
            self.add_log("ERR", f"iGate: {e}")
        if self.chat:
            try:
                self.chat.on_heard(line.split(">", 1)[0])
                if self.chat.on_packet(line, "IS"):
                    self.add_log("MSG", f"[IS] {line}")
            except Exception as e:
                self.add_log("ERR", f"chat: {e}")

    def _igate_to_rf(self, line):
        """Full iGate: an internet message for a station heard locally on RF is passed to RF
        (as a third-party packet), the standard iGate rules."""
        c = self.cfg
        if not (c.get("igate_tx") and c.get("igate") and not self.commercial and self.tnc):
            return
        m = tncmod.parse_message(line)
        if not m:
            return
        head, _, info = line.partition(":")
        src, path = m["src"], head.split(">", 1)[1].split(",")
        mycall = c["call"].upper()
        if m["to"] in (mycall, "") or src == mycall or m["to"].startswith(("BLN", "NWS")):
            return
        if any(x.rstrip("*") in ("TCPXX", "NOGATE", "RFONLY") for x in path):
            return
        now = time.time()
        h, hs = self.heard.get(m["to"]), self.heard.get(src)
        if not h or now - h["last"] > LOCAL_HEARD_S:
            return                              # addressee not local
        if hs and now - hs["last"] < LOCAL_HEARD_S:
            return                              # sender is local too: they hear each other on RF
        key = (src, info)
        if now - self.igate_seen.get(key, 0) < 30:
            return
        while self.igate_tx_times and now - self.igate_tx_times[0] > 3600:
            self.igate_tx_times.popleft()
        if len(self.igate_tx_times) >= IGATE_TX_PER_HOUR:
            return
        self.igate_seen = {k: t for k, t in self.igate_seen.items() if now - t < 600}
        self.igate_seen[key] = now
        self.igate_tx_times.append(now)
        dst = head.split(">", 1)[1].split(",")[0]
        rf_path = [x.strip() for x in (c.get("rf_path") or "").split(",") if x.strip()]
        self._tx_tnc().send(mycall, eng.TOCALL, rf_path, f"}}{src}>{dst},TCPIP,{mycall}*:{info}")
        self.add_log("IGT", f"internet -> RF: {src} -> {m['to']}")

    # ---- RX
    def on_rf_packet(self, line, link="A"):
        self.add_log("RX", line if link == "A" else f"[link {link}] {line}")
        try:
            self._heard_add(line)
            if self.chat:
                self.chat.on_heard(line.split(">", 1)[0].rstrip("*"))
        except Exception:
            pass
        if self.commercial:
            try:
                self._com_rx(line, link)
            except Exception as e:
                self.add_log("ERR", f"secure rx: {e}")
            return          # no iGate in commercial mode
        inner = line.partition(":")[2]
        if inner.startswith("}") and ">" in inner.split(":", 1)[0]:
            line_for_chat = inner[1:]                  # gated from the internet by an iGate: unwrap
            try:
                h0 = line_for_chat.partition(":")[0]
                if h0.split(">", 1)[1].split(",")[0].upper() == eng.TOCALL:
                    self._tw_seen(h0.split(">", 1)[0], "RF (gated)")
            except Exception:
                pass
        else:
            line_for_chat = line
        if self.chat:
            try:
                self.chat.on_packet(line_for_chat, "RF" if link == "A" else f"RF:{link}")
            except Exception as e:
                self.add_log("ERR", f"chat: {e}")
        c = self.cfg
        if not (c.get("igate") and self.isc and self.isc.verified):
            return
        head, _, info = line.partition(":")
        path = head.split(">", 1)[1].split(",") if ">" in head else []
        if any(p.rstrip("*") in ("TCPIP", "TCPXX", "NOGATE", "RFONLY") for p in path):
            return
        if info.startswith(("?", "}")) or head.split(">")[0].rstrip("*") == c["call"]:
            return
        try:
            self.isc.send(f"{head},qAR,{c['call']}:{info}")
            self.add_log("IGT", f"gated {head.split('>')[0]}")
        except Exception as e:
            self.add_log("ERR", f"iGate: {e}")

    # ---- status for the face plate
    def state(self, since=0):
        c = self.cfg
        matrix = []
        for band in ("2m", "70cm"):
            for region in ("NA", "EU", "LA"):
                op = self.openings.get((band, region))
                cell = {"band": band, "region": region,
                        "enabled": band in c["bands"] and region in c["regions"], "open": bool(op)}
                if op:
                    b = op.best
                    cell.update(paths=op.score, km=round(b.km), tx=b.tx, rx=b.rx, prop_mode=op.mode, mode_name=eng.MODE_NAMES.get(op.mode, ""),
                                tx_grid=b.tx_grid, rx_grid=b.rx_grid, mode=b.mode, snr=b.snr,
                                modes=sorted(op.modes))
                matrix.append(cell)
        tst = self.tnc.status if self.tnc else None
        return {
            "app": APP_NAME, "version": APP_VERSION, "running": self.running,
            "platform": "android" if IS_ANDROID else sys.platform,
            "config": {k: v for k, v in c.items() if k not in SECRET_KEYS}, "matrix": matrix, "utc": time.time(),
            "pin_set": bool(c.get("pin_hash")),
            "op_mode": "commercial" if self.commercial else c.get("op_mode", "amateur"),
            "heard": sorted(self.heard.values(), key=lambda h: -h["last"])[:40],
            "tw": self.tw_summary(),
            "qsy_hz": self.qsy_hz,
            "wolf": self.wolf_state(),
            "disaster": {"on": self.disaster_active(), "until": c.get("disaster_until") or 0,
                         "reason": c.get("disaster_reason") or ""},
            "mail_error": self.mail_error,
            "mail_gw": bool(c.get("mail_gw_enabled")),
            "mail_pass_set": {"smtp": bool(c.get("smtp_pass")), "imap": bool(c.get("imap_pass"))},
            "qsy_freqs": tncmod.zone_qsy(c),
            "zone": self.zone_info(c),
            "kiss_clients": len(self.kiss_srv.clients) if self.kiss_srv else None,
            "links": ([{"link": l, **v} for l, v in sorted(self.linkctl.links.items())] if self.linkctl else []),
            "link2": None if not self.tnc2 else {"kind": self.tnc2.kind, "error": self.tnc2.error,
                                                  "rx": self.tnc2.rx_count, "tx": self.tnc2.tx_count},
            "commlog_count": len(self.commlog),
            "com": {"net": c.get("com_net", ""), "fp": c.get("com_fp", ""), "freq": c.get("com_freq", ""),
                    "unit": c.get("com_unit", ""), "has_key": bool(c.get("com_key")),
                    "crypto": tncmod.crypto_available(), "active": bool(self.running and self.commercial)},
            "last_check": self.last_check, "next_check": self.next_check if self.running and not self.prop_off else 0,
            "next_beacon": self.next_beacon if self.running else 0,
            "spots": self.spot_count, "car_spots": self.car_count, "local": self.local,
            "check_error": self.check_error, "tnc": tst,
            "tc": {"enabled": bool(c.get("tc_enabled")), "seq": self.tc_seq, "alerts": self.tc_alerts},
            "wx": {"enabled": bool(c.get("wx_enabled")), "next": self.wx_next() if c.get("wx_enabled") else {},
                   "last": {k: {"t": v[0], "lines": v[1]} for k, v in self.wx_last.items()}},
            "rf": ("OFF" if not (self.running and self.rf_possible()) else
                   "ON" if self.rf_live() else "STBY"),
            "aprsis": None if not self.isc else {"connected": self.isc.connected,
                                                  "verified": self.isc.verified, "error": self.isc.error},
            "log": [e for e in list(self.log) if e["id"] > since],
            "errors": validate(c),
        }


STATION = Station()


LOCAL_ADDRS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
LOGIN_FAILS = collections.deque()      # wrong PIN attempts (5 in 5 minutes locks logins out)
LOCKED_PATHS = ("/api/config", "/api/comkey", "/api/mailpass", "/api/disaster")   # need the PIN when the settings are locked

LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Tradewind - PIN</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0f14;color:#e5e7eb;font:16px system-ui,sans-serif}
form{background:#111827;border:1px solid #374151;border-radius:12px;padding:24px;width:min(320px,90vw)}
h1{font-size:18px;margin:0 0 12px}input,button{width:100%;box-sizing:border-box;font-size:18px;padding:10px;border-radius:8px;margin-top:10px}
input{background:#030712;color:#fff;border:1px solid #4b5563;letter-spacing:4px;text-align:center}
button{background:#3b82f6;color:#fff;border:0;font-weight:600}p{color:#9ca3af;font-size:14px;margin:12px 0 0}#e{color:#f87171}</style></head>
<body><form id="f"><h1>Tradewind station PIN</h1><input id="p" type="password" inputmode="numeric" autocomplete="current-password" placeholder="PIN" autofocus>
<button>Unlock</button><p id="e"></p><p>This device is not the station computer. Enter the station PIN to use the face plate.</p></form>
<script>f.onsubmit=async e=>{e.preventDefault();const r=await(await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin:p.value})})).json();if(r.ok)location.reload();else document.getElementById('e').textContent=r.errors[0];};</script></body></html>"""

NO_PIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tradewind</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0f14;color:#e5e7eb;font:16px system-ui,sans-serif}
div{max-width:420px;padding:24px;border:1px solid #374151;border-radius:12px;background:#111827}</style></head><body><div>
<h1 style="font-size:18px">Station PIN needed</h1><p>Tradewind can key a transmitter, so other devices on the network are only let in once a station PIN is set.</p>
<p>On the station computer, open MENU &gt; STATION and set a PIN, then reload this page.</p></div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{APP_VERSION}"

    def _local(self):
        return self.client_address[0] in LOCAL_ADDRS

    def _authed(self):
        if self._local():
            return True
        if not STATION.cfg.get("pin_hash"):
            return False
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "tw_auth" and hmac.compare_digest(v, STATION.session_token()):
                return True
        return False

    def _html(self, text, code=200):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data, ctype, cache="no-store"):
        if data is None:
            return self._json({"error": "not available offline yet"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return {}

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/manifest.json":          # iPhone / Android "Add to Home Screen"
            return self._bytes(json.dumps({
                "name": APP_NAME, "short_name": APP_NAME, "start_url": "/", "display": "standalone",
                "background_color": "#0b0f14", "theme_color": "#0b0f14",
                "icons": [{"src": "/icon.png", "sizes": "512x512", "type": "image/png"}]}).encode(),
                "application/manifest+json", "public, max-age=86400")
        if path == "/icon.png":
            try:
                with open(os.path.join(HERE, "icon.png"), "rb") as f:
                    return self._bytes(f.read(), "image/png", "public, max-age=86400")
            except OSError:
                return self._json({"error": "no icon"}, 404)
        if not self._authed():
            if path in ("/", "/index.html"):
                return self._html(LOGIN_PAGE if STATION.cfg.get("pin_hash") else NO_PIN_PAGE, 401)
            return self._json({"error": "PIN required"}, 401)
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "faceplate.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            q = self.path.partition("since=")[2]
            self._json(STATION.state(int(q) if q.isdigit() else 0))
        elif path == "/api/devices":
            audio, aerr = tncmod.list_audio_devices()
            ports, perr = tncmod.list_serial_ports()
            self._json({"audio": audio, "audio_error": aerr, "serial": ports, "serial_error": perr})
        elif path == "/api/history":
            q = self.path.partition("hours=")[2]
            hours = float(q) if q.replace(".", "", 1).isdigit() else 6
            self._json({"now": time.time(), "events": STATION.recent_history(min(hours, 48))})
        elif path == "/api/presets":
            self._json(PRESETS)
        elif path == "/api/map":
            self._json(STATION.map_data())
        elif path.startswith("/vendor/") and path[8:] in VENDOR:
            url, ctype = VENDOR[path[8:]]
            data = cached_get(os.path.join(CACHE_DIR, "vendor", path[8:]), url)
            self._bytes(data, ctype, "public, max-age=86400")
        elif path.startswith("/tile/"):
            m = re.fullmatch(r"/tile/(esri|osm)/(\d{1,2})/(\d{1,6})/(\d{1,6})", path)
            if not m or int(m.group(2)) > 12:
                return self._json({"error": "bad tile"}, 404)
            src, z, a, b = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
            x, y = (b, a) if src == "esri" else (a, b)          # esri URLs are z/y/x
            data = cached_get(os.path.join(CACHE_DIR, "tiles", src, str(z), str(x), f"{y}.png"),
                              TILE_SRC[src].format(z=z, x=x, y=y), TILE_MAX_AGE)
            self._bytes(data, "image/png", "public, max-age=604800")
        elif path == "/api/commlog.csv":
            body = STATION.commlog_csv().encode("utf-8-sig")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="tradewind-ics309-{time.strftime("%Y%m%d-%H%M")}.csv"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/chat":
            self._json(STATION.chat.state() if STATION.chat else {"seq": 0, "peers": [], "events": [], "off": True})
        elif path == "/api/freqs":
            self._json([{"value": v, "label": l} for v, l in tncmod.APRS_FREQS])
        elif path == "/api/zones":
            self._json([{"key": k, "name": z["name"], "aprs": z["aprs"], "qsy": z["qsy"], "note": z["note"]}
                        for k, z in tncmod.ZONES.items()])
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        # Basic CSRF guard: browsers send Origin on cross-site POSTs
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and origin.split("://", 1)[-1] != host:
            return self._json({"error": "cross-origin request refused"}, 403)
        path = self.path.split("?")[0]
        if path == "/api/login":
            now = time.time()
            while LOGIN_FAILS and now - LOGIN_FAILS[0] > 300:
                LOGIN_FAILS.popleft()
            if len(LOGIN_FAILS) >= 5:
                return self._json({"ok": False, "errors": ["Too many wrong PINs - wait 5 minutes."]}, 429)
            if not STATION.pin_ok(self._body().get("pin")):
                LOGIN_FAILS.append(now)
                return self._json({"ok": False, "errors": ["Wrong PIN."]}, 403)
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", f"tw_auth={STATION.session_token()}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if not self._authed():
            return self._json({"ok": False, "errors": ["PIN required - reload the page."]}, 401)
        if path in LOCKED_PATHS and STATION.cfg.get("lock_settings") and \
                not STATION.pin_ok(self.headers.get("X-TW-PIN")):
            if self.headers.get("X-TW-PIN"):
                LOGIN_FAILS.append(time.time())
            return self._json({"ok": False, "pin_required": True,
                               "errors": ["Settings are locked - enter the station PIN."]}, 403)
        if path == "/api/pin":
            b = self._body()
            now = time.time()
            while LOGIN_FAILS and now - LOGIN_FAILS[0] > 300:
                LOGIN_FAILS.popleft()
            if len(LOGIN_FAILS) >= 5:
                return self._json({"ok": False, "errors": ["Too many wrong PINs - wait 5 minutes."]})
            try:
                STATION.set_pin(str(b.get("action") or ""), b.get("old"), b.get("new"), b.get("lock"))
            except ValueError as e:
                if "wrong" in str(e):
                    LOGIN_FAILS.append(now)
                return self._json({"ok": False, "errors": [str(e)]})
            return self._json({"ok": True, "pin_set": bool(STATION.cfg.get("pin_hash")),
                               "lock": bool(STATION.cfg.get("lock_settings"))})
        if path == "/api/config":
            errs = STATION.update_config(self._body())
            self._json({"ok": True, "errors": errs})
        elif path == "/api/start":
            errs = STATION.start()
            self._json({"ok": not errs, "errors": errs})
        elif path == "/api/stop":
            STATION.stop()
            self._json({"ok": True})
        elif path == "/api/wx":
            if not STATION.running:
                return self._json({"ok": False, "errors": ["Turn the station ON first."]})
            kind = self._body().get("kind")
            if kind == "tropical":
                threading.Thread(target=STATION.check_tropical, kwargs={"force": True}, daemon=True).start()
                return self._json({"ok": True})
            if kind not in ("general", "marine", "aviation"):
                return self._json({"ok": False, "errors": ["Unknown bulletin type."]})
            threading.Thread(target=STATION.send_wx, args=(kind,), daemon=True).start()
            self._json({"ok": True})
        elif path == "/api/mail":
            if not (STATION.running and STATION.chat):
                return self._json({"ok": False, "errors": ["Turn the station ON first."]})
            b = self._body()
            try:
                mid = STATION.chat.mail_send(str(b.get("to") or ""), str(b.get("subject") or ""), str(b.get("body") or ""),
                                             str(b.get("via") or STATION.cfg.get("mail_via") or "EMAIL-2"),
                                             third_party_ok=STATION.disaster_active())
            except ValueError as e:
                return self._json({"ok": False, "errors": [str(e)]})
            return self._json({"ok": True, "id": mid})
        elif path == "/api/wolf":
            b = self._body()
            try:
                if b.get("action") == "ping":
                    STATION.wolf_ping(str(b.get("call") or ""), str(b.get("text") or ""))
                elif b.get("action") == "toggle":
                    with STATION.lock:
                        STATION.cfg["wolf_on"] = bool(b.get("on"))
                        save_config(STATION.cfg)
                    if not STATION.cfg["wolf_on"]:
                        STATION.wolf = {}
                        if STATION.isc:
                            STATION.isc.set_filter("")
                    else:
                        STATION.next_check = 0
                        STATION._wake.set()
                elif b.get("action") == "unstop":
                    with STATION.lock:
                        STATION.cfg["wolf_stop"] = ",".join(sorted(STATION.wolf_stopped() - {str(b.get("call") or "").upper()}))
                        save_config(STATION.cfg)
            except ValueError as e:
                return self._json({"ok": False, "errors": [str(e)]})
            return self._json({"ok": True})
        elif path == "/api/disaster":
            b = self._body()
            STATION.set_disaster(bool(b.get("on")), b.get("hours") or 24, str(b.get("reason") or ""))
            return self._json({"ok": True, "on": STATION.disaster_active()})
        elif path == "/api/mailpass":
            b = self._body()
            with STATION.lock:
                for k in ("smtp_pass", "imap_pass"):
                    if k in b:
                        STATION.cfg[k] = str(b.get(k) or "")
                save_config(STATION.cfg)
            return self._json({"ok": True})
        elif path == "/api/comkey":
            b = self._body()
            try:
                fp = STATION.set_com_key(str(b.get("net") or ""), str(b.get("passphrase") or ""))
            except ValueError as e:
                return self._json({"ok": False, "errors": [str(e)]})
            self._json({"ok": True, "fp": fp})
        elif path == "/api/chat":
            if not (STATION.running and STATION.chat):
                return self._json({"ok": False, "errors": ["Turn the station ON first."]})
            b = self._body()
            act, call = b.get("action"), str(b.get("call") or "")
            try:
                if act == "hail":
                    tries = b.get("tries")
                    STATION.chat.hail(call, b.get("route") or "rf",
                                      tries=int(tries) if str(tries).isdigit() else None,
                                      interval=int(b["interval"]) if str(b.get("interval", "")).isdigit() else None,
                                      forever=tries == "forever")
                elif act == "direct":
                    STATION.chat.open_direct(call, str(b.get("route") or "smart"))
                elif act == "route":
                    STATION.chat.set_route(call, str(b.get("route") or "smart"))
                elif act == "nogate":
                    STATION.chat.set_nogate(call, bool(b.get("on")))
                elif act == "qsy":
                    STATION.chat.qsy(call, str(b.get("freq") or ""))
                elif act in ("qsy_yes", "qsy_no"):
                    STATION.chat.qsy_answer(call, act == "qsy_yes")
                elif act == "qsy_back":
                    STATION.chat.qsy_back(call)
                elif act in ("block", "unblock"):
                    STATION.set_block(call, act == "block")
                elif act == "probe":
                    STATION.chat.probe(call)
                elif act == "relay":
                    STATION.chat.relay(b.get("relay") or "", b.get("dest") or "", str(b.get("text") or ""),
                                       b.get("route") or None)
                elif act in ("accept", "decline", "end", "read"):
                    getattr(STATION.chat, "mark_read" if act == "read" else act)(call)
                elif act == "send":
                    STATION.chat.send(call, str(b.get("text") or ""))
                else:
                    return self._json({"ok": False, "errors": ["Unknown chat action."]})
            except ValueError as e:
                return self._json({"ok": False, "errors": [str(e)]})
            self._json({"ok": True})
        elif path in ("/api/beacon", "/api/test", "/api/check"):
            if not STATION.running:
                return self._json({"ok": False, "errors": ["Turn the station ON first."]})
            if path == "/api/beacon":
                STATION.send_beacon()
            elif path == "/api/test":
                STATION.send_test()
            else:
                wait = STATION.psk_wait()
                if wait > 0:
                    return self._json({"ok": False, "errors": [
                        "Tradewind checks PSKReporter at most 6 times an hour, 10 minutes apart - "
                        f"try again in {int(wait) // 60} min {int(wait) % 60} s."]})
                STATION.next_check = 0
                STATION._wake.set()
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)


def open_browser(url):
    """Open the face plate in the user's browser (more reliable on Windows .exe builds)."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(url)  # uses the default browser
            return
    except OSError:
        pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description=f"{APP_NAME} face plate")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to allow other devices on your LAN")
    ap.add_argument("--port", type=int, default=8073)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--autostart", action="store_true", help="turn the station on at launch")
    ap.add_argument("--demo", metavar="XML", help="replay a saved PSKReporter XML file instead of querying live")
    a = ap.parse_args()
    STATION.demo_xml = a.demo
    try:
        srv = ThreadingHTTPServer((a.host, a.port), Handler)
    except OSError:
        # Another program (usually an older Tradewind) already uses this port: the browser
        # would silently show THAT copy, so say so clearly instead of closing at once.
        print(f"{APP_NAME} {APP_VERSION} could not start: port {a.port} is already in use.")
        print()
        print("  Another copy of Tradewind is probably still running (maybe an older version).")
        print("  Close its black window - or end 'Tradewind' in Task Manager (Ctrl+Shift+Esc) -")
        print("  then start Tradewind again.")
        if not a.no_browser:
            try:
                input("\nPress Enter to close this window...")
            except EOFError:
                pass
        sys.exit(1)
    url = f"http://{'127.0.0.1' if a.host in ('0.0.0.0', '') else a.host}:{a.port}/"
    print(f"{APP_NAME} {APP_VERSION} - by Brandon St Rose (8P5DG) & Kammie Holder (8P5AB) - MIT License")
    print()
    print("  Tradewind is running. KEEP THIS WINDOW OPEN - closing it stops Tradewind.")
    print()
    print(f"  If the face plate doesn't open by itself, open Chrome or Edge")
    print(f"  and go to:   {url}")
    print()
    if a.host not in ("127.0.0.1", "localhost"):
        print("Other devices on your network need the station PIN (set it under MENU > STATION).")
    if a.autostart:
        errs = STATION.start()
        if errs:
            print("Autostart failed:", "; ".join(errs))
    if not a.no_browser:
        threading.Timer(1.0, open_browser, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STATION.stop()


if __name__ == "__main__":
    main()
