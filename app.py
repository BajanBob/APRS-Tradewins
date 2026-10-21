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
import json
import os
import sys
import re
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import tnc as tncmod
import vhf_prop_aprs as eng
import wx

APP_NAME = "Tradewind"
APP_VERSION = "3.0"
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
    "cat_tune": False, "aprs_freq": "144.390", "aprs_freq_custom": "", "cat_restore": True,
    "txdelay": 300, "txtail": 50, "tx_level": 0.5, "modem": "1200",
    # Phase 2: a second radio path (Direwolf / UZ7HO / hardware TNC over KISS) and automatic link selection
    "link1_name": "", "link1_speed": 1200,
    "link2_enabled": False, "link2_name": "70cm 9600 G3RUH", "link2_speed": 9600,
    "link2_tnc": "kiss_tcp", "link2_addr": "127.0.0.1:8002", "link2_serial": "", "link2_baud": 9600,
    # Phase 4: off-grid / disaster features
    "store_forward": True, "relay_enabled": False,
    "kiss_addr": "127.0.0.1:8001", "kiss_serial": "", "kiss_baud": 9600,
    "rf_announce": True, "rf_path": "WIDE2-1", "rf_when": "always", "rf_modes": ["TEP"],
    "aprsis": True, "server": "rotate.aprs2.net", "igate": False,
    "window": 60, "interval": 5, "min_km": 800, "min_paths": 2,
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
    "cfg_ver": 2,
}
SECRET_KEYS = ("com_key",)        # never sent to the face plate

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


def load_config():
    cfg = dict(DEFAULTS)
    if IS_ANDROID:
        cfg.update(tnc="none", preset="aprsis_only", rf_announce=False)
    try:
        with open(CONF_PATH) as f:
            saved = json.load(f)
        if saved.get("cfg_ver", 1) < 2 and saved.get("interval") == 10:
            saved["interval"] = 5     # v1.5: map and checks every 5 minutes by default
        cfg.update(saved)
    except (OSError, ValueError):
        pass
    return _migrate(cfg)


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


def validate(cfg):
    if cfg.get("op_mode") == "commercial":
        return validate_commercial(cfg)
    errs = []
    if IS_ANDROID and cfg.get("tnc") in ("sound", "kiss_serial"):
        errs.append("On Android use 'APRS-IS only' or 'KISS over TCP' - the sound modem and USB/serial radios need the Windows/PC version.")
    call = cfg.get("call", "").upper().strip()
    if not CALL_RE.match(call) or call.startswith("N0CALL"):
        errs.append("Enter your own callsign (with optional -SSID, e.g. 8P6XX-10).")
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
            if self.tnc2 is not None and c.get("tnc") != "none":
                s1 = 44 if c.get("tnc") == "sound" and c.get("modem") == "robust" else \
                    300 if c.get("tnc") == "sound" and str(c.get("modem")) == "300" else \
                    (1200 if c.get("tnc") == "sound" else int(c.get("link1_speed") or 1200))
                self.linkctl = tncmod.LinkController({
                    "A": {"name": c.get("link1_name") or f"{s1} bps (main radio)", "speed": s1},
                    "B": {"name": c.get("link2_name") or "second link", "speed": int(c.get("link2_speed") or 9600)}})
            self.commercial = c.get("op_mode") == "commercial"
            self.sec, self.last_id = None, 0
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
            for x in (self.tnc, self.isc, self.tnc2):
                try:
                    x and x.stop()
                except Exception:
                    pass
            self._save_chat()
            self.tnc = self.isc = self.tnc2 = None
            self.linkctl = None
            self.running = False
            self.add_log("SYS", "Station OFF")

    def update_config(self, new):
        was = self.running
        if was:
            trial = dict(self.cfg)
            trial.update({k: v for k, v in new.items() if k in DEFAULTS})
            trial["call"] = str(trial.get("call", "")).upper().strip()
            errs = validate(trial)
            if errs:  # keep the running station on its last good settings
                return ["Not applied: " + errs[0]]
            self.stop()
        with self.lock:
            for k, v in new.items():
                if k in DEFAULTS:
                    self.cfg[k] = v
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
                self.run_check()
                self.next_check = time.time() + max(eng.MIN_QUERY_INTERVAL, self.cfg["interval"] * 60)
            if self.next_beacon and now >= self.next_beacon:
                self.send_beacon()
                self.next_beacon = time.time() + max(10, self.cfg["beacon_min"]) * 60
            if self.cfg.get("wx_enabled"):
                self._wx_schedule(now)
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

    def run_check(self):
        c = self.cfg
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
        if not self.rf_possible():
            return False
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
    def transmit(self, info, rf=True, net=True, mode=None, force_rf=False):
        if self.commercial:
            return      # APRS bulletins / beacons never go out on a commercial channel
        c = self.cfg
        call = c["call"]
        sent = []
        if rf and (self.rf_live(mode) or (force_rf and self.rf_possible())):
            path = [p.strip() for p in (c.get("rf_path") or "").split(",") if p.strip()]
            try:
                self.tnc.send(call, eng.TOCALL, path, info)
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
        self.transmit(f"!{lat}{sym[0]}{lon}{sym[1]}{c.get('beacon_comment', '')[:43]}")

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
        self.chat.links = self.linkctl
        robust = self.cfg.get("tnc") == "sound" and self.cfg.get("modem") == "robust"
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
        h.update(last=time.time(), via=("via " + digi[-1]) if digi else "direct")
        h["count"] += 1
        if len(self.heard) > 300:
            oldest = min(self.heard.values(), key=lambda x: x["last"])
            self.heard.pop(oldest["call"], None)

    def _pick_link(self, call, route):
        """Which radio link a message goes out on: 'A' (main radio), 'B' (second link) or None."""
        if route == "auto":
            return self.linkctl.choose(call) if self.linkctl else "A"
        if str(route).startswith("link:"):
            return route[5:]
        return "A" if route in ("rf", "rf_is") else None

    def _link_send(self, link, src, dst, path, info):
        if link == "B":
            if not self.tnc2:
                raise IOError("the second radio link is not running (MENU > RADIO / TNC > Second radio link)")
            self.tnc2.send(src, dst, path, info)
        else:
            if not self.rf_possible() and not self.commercial:
                raise IOError("RF needs a radio: set a TNC under MENU > RADIO / TNC, or choose APRS-IS")
            self.tnc.send(src, dst, path, info)

    def _chat_send(self, call, info, route):
        """Send one APRS message for the chat (RF ignores the propagation-mode standby).
        Returns the radio link used ('A' / 'B'), 'IS', or raises."""
        c, sent = self.cfg, []
        link = self._pick_link(call, route)
        if link:
            path = [p.strip() for p in (c.get("rf_path") or "").split(",") if p.strip()]
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
        if self.chat:
            try:
                self.chat.on_heard(line.split(">", 1)[0])
                if self.chat.on_packet(line, "IS"):
                    self.add_log("MSG", f"[IS] {line}")
            except Exception as e:
                self.add_log("ERR", f"chat: {e}")

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
        if self.chat:
            try:
                self.chat.on_packet(line, "RF" if link == "A" else f"RF:{link}")
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
            "op_mode": "commercial" if self.commercial else c.get("op_mode", "amateur"),
            "heard": sorted(self.heard.values(), key=lambda h: -h["last"])[:40],
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


class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{APP_VERSION}"

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
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        # Basic CSRF guard: browsers send Origin on cross-site POSTs
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and origin.split("://", 1)[-1] != host:
            return self._json({"error": "cross-origin request refused"}, 403)
        path = self.path.split("?")[0]
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
                wait = STATION.last_check + eng.MIN_QUERY_INTERVAL - time.time()
                if wait > 0:
                    return self._json({"ok": False, "errors": [
                        f"PSKReporter allows one query every 5 minutes - try again in {int(wait)}s."]})
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
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    url = f"http://{'127.0.0.1' if a.host in ('0.0.0.0', '') else a.host}:{a.port}/"
    print(f"{APP_NAME} {APP_VERSION} - by Brandon St Rose (8P5DG) & Kammie Holder (8P5AB) - MIT License")
    print()
    print("  Tradewind is running. KEEP THIS WINDOW OPEN - closing it stops Tradewind.")
    print()
    print(f"  If the face plate doesn't open by itself, open Chrome or Edge")
    print(f"  and go to:   {url}")
    print()
    if a.host not in ("127.0.0.1", "localhost"):
        print("WARNING: anyone who can reach this port can key your transmitter. Use only on a trusted LAN.")
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
