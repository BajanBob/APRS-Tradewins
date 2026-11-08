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
import shutil
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import afsk
import digi as digimod
import dxmode
import hfcall as hfmod
import iss as issmod
import satrx
import tnc as tncmod
import vhf_prop_aprs as eng
import wx

try:                                    # present only in the Tradewind Commander edition
    import commander
    from commander import seclink as secmod
    from commander import station as comstation
    from commander import ui as comui
except ImportError:
    commander = secmod = comstation = comui = None
EDITION = commander.EDITION if commander else "amateur"

APP_NAME = "Tradewind"
APP_VERSION = "3.21"
APP_TITLE = commander.TITLE if commander else APP_NAME
# When packaged as an .exe (PyInstaller) bundled files live in sys._MEIPASS
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
_CONF_NAME = commander.CONF_NAME if commander else ".tradewind"


def _conf_dir():
    # Android (python-for-android) has no normal home folder: use the app's private storage
    for var in ("TRADEWIND_HOME", "ANDROID_PRIVATE", "ANDROID_APP_PATH"):
        if os.environ.get(var):
            return os.path.join(os.environ[var], _CONF_NAME)
    home = os.path.expanduser("~")
    if home in ("~", "/", "") or not os.access(home, os.W_OK):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), _CONF_NAME)
    return os.path.join(home, _CONF_NAME)


CONF_DIR = _conf_dir()
CONF_PATH = os.path.join(CONF_DIR, "config.json")
STATE_PATH = os.path.join(CONF_DIR, "announce_state.json")
HISTORY_PATH = os.path.join(CONF_DIR, "opening_history.json")
CHAT_DIR = os.path.join(CONF_DIR, "chat")         # saved chats + comms log (per callsign / unit)
RX_DIR = os.path.join(CONF_DIR, "received")       # v3.16 satellite pictures, video and telemetry
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


def aprs_position(info):
    """(lat, lon) from an uncompressed or compressed APRS position report, or None."""
    if not info:
        return None
    t = info[0]
    body = info[1:] if t in "!=" else (info[8:] if t in "/@" else None)
    if body is None:
        return None
    m = re.match(r"(\d{2})(\d{2}\.\d{2})([NS]).(\d{3})(\d{2}\.\d{2})([EW])", body)
    if m:
        lat = int(m.group(1)) + float(m.group(2)) / 60
        lon = int(m.group(4)) + float(m.group(5)) / 60
        lat, lon = (-lat if m.group(3) == "S" else lat), (-lon if m.group(6) == "W" else lon)
        return (lat, lon) if abs(lat) <= 90 and abs(lon) <= 180 else None
    if len(body) >= 9 and body[0] in "/\\" or (len(body) >= 9 and body[0].isalpha()):
        try:
            y = sum((ord(ch) - 33) * 91 ** (3 - i) for i, ch in enumerate(body[1:5]))
            x = sum((ord(ch) - 33) * 91 ** (3 - i) for i, ch in enumerate(body[5:9]))
            lat, lon = 90 - y / 380926, -180 + x / 190463
            return (lat, lon) if abs(lat) <= 90 and abs(lon) <= 180 else None
        except (TypeError, ValueError):
            return None
    return None


def latlon_to_grid(lat, lon):
    """6-character Maidenhead square."""
    lon, lat = lon + 180, lat + 90
    return (chr(65 + int(lon // 20)) + chr(65 + int(lat // 10)) + str(int(lon % 20 // 2)) + str(int(lat % 10))
            + chr(97 + int(lon % 2 * 12)) + chr(97 + int(lat % 1 * 24)))


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


def _web_json(url, obj=None, timeout=12):
    """GET (obj=None) or POST JSON and parse the JSON reply. Returns a dict, or None on any failure."""
    import ssl
    import urllib.request
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    data = json.dumps(obj).encode() if obj is not None else None
    hdr = {"User-Agent": f"{APP_NAME}/{APP_VERSION} (amateur radio app)"}
    if data is not None:
        hdr["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdr, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


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
    # v3.8 Wolf auto-call: when PSKReporter shows the path open NOW and the DX station is on APRS, call it on 2m RF
    "wolf_auto": False, "wolf_auto_fallback": True, "wolf_open_min": 15,
    "wolf_msg": "{me} {grid}: 2m DX via APRS on Tradewind! {km}km {prop}. QSL? 73",
    "disaster_on": False, "disaster_until": 0, "disaster_reason": "",
    "share_whatsapp": False, "share_facebook": False, "share_email": False, "share_email_to": "",   # opt-in alert sharing
    "uhf_range": "430-440", "allowed_hosts": "",     # v3.7: 70 cm range on your licence; extra face-plate host names
    "zone": "auto", "qsy_mode": "auto",     # v3.6: band-plan zone from the grid square; chat channels auto or manual
    "qsy_freqs": "144.510,144.530,144.550,144.570,144.590,145.030,145.050,145.070,145.090",   # chat channels, 20 kHz apart            # callsigns whose hails and messages are ignored
    "pin_hash": "", "pin_salt": "", "lock_settings": False,   # station PIN (v3.2)
    "kiss_addr": "127.0.0.1:8001", "kiss_serial": "", "kiss_baud": 9600,
    "rf_announce": True, "rf_path": "WIDE2-1", "rf_when": "always", "rf_modes": ["TEP"],
    "aprsis": True, "server": "rotate.aprs2.net", "igate": False,
    "igate_tx": False,           # full iGate: also pass internet messages to local RF stations
    "kiss_server": False, "kiss_server_port": 8001, "kiss_server_lan": False,   # be a TNC for other apps
    "digi": False, "digi_alias": "", "digi_max_per_min": 10,                    # digipeater (WIDE1-1 / WIDE2-n)
    "digi_mode": "fill",                                                        # fill = WIDE1-1 only, wide = high site
    "multi_decoder": True, "fx25_tx": False,                                    # modem: parallel decoders, FX.25 FEC
    "dx_enabled": False, "dx_auto_beacon": False, "dx_height_m": 30,            # APRS DX mode (inter-island)
    "dx_freq": "144.150", "fm_freq": "145.570", "dx_path": "", "dx_beacon_min": 60,
    "dx_burst_count": 4,                                                        # DX beacons in the minute after an opening
    # v3.15 ISS passes: predicted from the grid square; beacons through the ISS digipeater during a pass
    # Satellite Watch Mode: passes predicted from the grid square (N2YO / AMSAT / NASA / CelesTrak orbit data);
    # Satellite Switch puts a CAT radio (IC-9100, FT-991A...) on the satellite frequency for each pass
    "iss_enabled": False, "iss_auto_beacon": False, "iss_min_el": 0, "iss_path": "ARISS",
    "iss_comment": "Tradewind via ISS", "iss_tune": True,
    "sat_catnr": 25544, "sat_freq": "145.825", "n2yo_key": "",
    # v3.16 satellite receivers: what the radio does on a pass, and the SDR receivers run on their own
    "sat_rx": "iss_aprs", "sat_doppler": True, "rx_keep_wav": False,
    "rx_jobs": [], "sdr_source": "rtlsdr", "sdr_gain_db": 40, "geo_min_el": 5,
    "hamtv_lo_mhz": 0, "hamtv_cmd": "",
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
    # v3.9 Global Operators map: show other Tradewind stations worldwide, with hailing preference
    "map_show": True,                 # appear on the global operator map (off = browse only, invisible)
    "hail_status": "available",       # available | dnd | reject  (how others may hail you)
    "locate_by": "grid",              # grid | ip  (how your dot is placed)
    "directory_url": "https://tradewind-directory.YOURNAME.workers.dev",
    # v3.18 HF Calling: CCIR-493 style selcall (beacon / revertive LQA, page, GPS, emergency), 2G ALE, ARINC SELCAL rx
    "hf_enabled": False, "hf_ids": "", "hf_ale_addrs": "", "hf_channels": "", "hf_scan": False,
    "hf_dwell": 1.0, "hf_far_scans": False, "hf_revertive": True, "hf_ale_respond": True,
    "hf_selcal": False, "hf_selcall_tones": "lm", "hf_call_format": "ccir", "hf_ccir_dots_s": 6.0, "hf_cw_id": True, "hf_far_channels": 0, "hf_capture": True, "hf_sound_min": 0, "hf_ale_variant": "", "hf_preamble_s": 0,
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
     "note": "One USB cable carries audio and CI-V. Audio: 'USB Audio CODEC'. PTT port: the IC-9100's USB serial port. On the radio set DATA MOD to USB, USB MOD Level about 25-50%, CI-V address 7C and CI-V baud 9600. With 'Tune the radio' on, Tradewind sets the frequency for you at power-on (2m on the main band): FM data for 1200 baud and Robust FM, USB data for Robust and Robust DX, and puts the radio back when you switch off. Close other CAT software (logger, WSJT-X) first - only one program can use the port."},
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


def serial_port_name(p, windows=sys.platform.startswith("win")):
    """Windows names serial ports COM1, COM2...: a bare number or "com 12" becomes "COM12".
    Anything else (/dev/ttyUSB0, 127.0.0.1:4532 for rigctld) is left alone."""
    p = str(p or "").strip()
    if windows and re.fullmatch(r"(?i)(com)?\s*0*([1-9]\d{0,2})", p):
        return "COM" + re.fullmatch(r"(?i)(com)?\s*0*([1-9]\d{0,2})", p).group(2)
    return p


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
    for k in ("ptt_port", "kiss_serial", "link2_serial"):     # v3.17.1: "12" -> "COM12" on Windows
        cfg[k] = serial_port_name(cfg.get(k))
    return cfg


# ---- on-air and network etiquette limits (v3.2)
PSK_MIN_GAP = 600        # at least 10 minutes between PSKReporter queries
PSK_MAX_PER_HOUR = 6     # and never more than 6 in any hour
IGATE_TX_PER_HOUR = 30   # internet -> RF messages gated per hour (full iGate)
LOCAL_HEARD_S = 30 * 60  # "local" = heard on RF in the last 30 minutes
SAT_SWITCH_LEAD_S = 60   # Satellite Switch: radio moves to the satellite frequency this long before AOS
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
    if not commander:
        cfg["op_mode"] = "amateur"      # Commercial / EmComm mode is only in Tradewind Commander
    return cfg


def save_config(cfg):
    os.makedirs(CONF_DIR, exist_ok=True)
    tmp = CONF_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=1)
    os.replace(tmp, CONF_PATH)


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
        if not comstation:
            return ["Commercial / EmComm mode is part of Tradewind Commander. This is the amateur edition: clear text only."]
        return comstation.validate_commercial(cfg)
    errs = []
    if IS_ANDROID and cfg.get("tnc") in ("sound", "kiss_serial"):
        errs.append("On Android use 'APRS-IS only' or 'KISS over TCP' - the sound modem and USB/serial radios need the Windows/PC version.")
    call = cfg.get("call", "").upper().strip()
    if not CALL_RE.match(call) or call.split("-")[0] in ("N0CALL", "NOCALL", "MYCALL", "CALL", "TEST", "AB1CD"):
        errs.append("Enter your own callsign (with optional -SSID, e.g. 8P6XX-10).")
    if cfg.get("mail_gw_enabled") and cfg.get("smtp_host") and not cfg.get("smtp_tls", True) \
            and int(cfg.get("smtp_port") or 587) != 465 and str(cfg["smtp_host"]).strip().lower() not in LOCAL_MAIL_HOSTS:
        errs.append("Tick 'Use STARTTLS' (or use port 465) so the mail password is never sent unencrypted.")
    if not tncmod.set_uhf(cfg.get("uhf_range") or "430-440"):
        errs.append("70 cm licence range must look like 430-440 (MHz, inside 420-450).")
    tncmod.set_zone(tncmod.zone_of(cfg))
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
        errs.append("Choose the CI-V / CAT serial port (the radio's COM port - for the IC-9100 it is "
                    "'Silicon Labs CP210x' in Device Manager > Ports)." if cfg.get("ptt") in ("CIV", "YAESU_CAT") else
                    "Choose the serial / HID port used for PTT.")
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
    if cfg.get("digi"):
        if cfg.get("tnc") in ("none",) or (cfg.get("tnc") == "sdr" and not cfg.get("link2_enabled")):
            errs.append("The fill-in digipeater needs a radio that transmits (MENU > RADIO / TNC).")
        if cfg.get("tnc") == "sound" and cfg.get("modem") in ("robust", "robust_dx", "robust_fm", "300"):
            errs.append("The digipeater works on 1200-baud VHF/UHF APRS, not on the Robust or HF modems.")
        if cfg.get("digi_alias") and not digimod.valid_call(cfg.get("digi_alias")):
            errs.append("Digipeater alias: at most 6 letters/digits plus an optional -SSID, e.g. BRBDOS or 8P5AB-1 (or leave it blank).")
        try:
            if not 1 <= int(cfg.get("digi_max_per_min") or 10) <= 60:
                raise ValueError
        except (TypeError, ValueError):
            errs.append("Digipeater limit must be 1-60 packets a minute.")
    if cfg.get("modem") in ("robust_dx", "robust_fm") and cfg.get("tnc") != "sound":
        errs.append("Robust DX and Robust FM need the built-in sound modem (MENU > RADIO / TNC > TNC type: sound card).")
    if cfg.get("modem") == "robust_fm" and cfg.get("tnc") == "sound":
        hz, why = tncmod.fm_freq_check(cfg)
        if not hz:
            errs.append(why + ".")
    if cfg.get("modem") in ("robust", "robust_dx") and cfg.get("tnc") == "sound" and cfg.get("cat_tune") \
            and not tncmod.dx_freq_hz(cfg):
        errs.append("Robust / DX frequency must be in an amateur band (160 m to 70 cm), e.g. 144.150 or 10.142 (MHz, USB dial).")
    if cfg.get("dx_enabled"):
        try:
            if not 0 <= float(cfg.get("dx_height_m") or 0) <= 3000:
                raise ValueError
        except (TypeError, ValueError):
            errs.append("Antenna height above sea level must be 0-3000 m.")
        if cfg.get("dx_path") and check_path(cfg.get("dx_path")):
            errs.append("DX path: " + check_path(cfg.get("dx_path")).removeprefix("RF path: "))
    if cfg.get("iss_enabled"):
        try:
            if not 0 <= float(cfg.get("iss_min_el") or 0) <= 60:
                raise ValueError
        except (TypeError, ValueError):
            errs.append("Satellite minimum elevation must be 0-60 degrees.")
        try:
            if not 1 <= int(cfg.get("sat_catnr") or 0) <= 999999:
                raise ValueError
        except (TypeError, ValueError):
            errs.append("Satellite: enter its NORAD catalogue number (ISS = 25544).")
        try:
            f = float(str(cfg.get("sat_freq") or "").replace(",", "."))
            if not (144.0 <= f <= 148.0 or 430.0 <= f <= 440.0):
                raise ValueError
        except ValueError:
            errs.append("Satellite frequency must be a 2 m or 70 cm frequency in MHz, e.g. 145.825.")
        if cfg.get("iss_path") and check_path(cfg.get("iss_path")):
            errs.append("ISS path: " + check_path(cfg.get("iss_path")).removeprefix("RF path: "))
    if cfg.get("hf_enabled"):
        errs += hf_validate(cfg)
    if cfg.get("tnc") == "kiss_serial" and not cfg.get("kiss_serial"):
        errs.append("Choose the radio / TNC serial port.")
    return errs


def hf_validate(cfg):
    """v3.18 HF Calling settings."""
    errs = []
    if cfg.get("tnc") != "sound":
        errs.append("HF Calling needs the built-in sound modem (MENU > RADIO / TNC > TNC type: sound card).")
    ids = [s.strip() for s in str(cfg.get("hf_ids") or "").replace(" ", ",").split(",") if s.strip()]
    if len(ids) > 10 or any(not (s.isdigit() and len(s) in (4, 6)) for s in ids):
        errs.append("HF selcall IDs: up to 10, each 4 or 6 digits (e.g. 1234 or 123456).")
    addrs = [s.strip() for s in str(cfg.get("hf_ale_addrs") or "").replace(" ", ",").split(",") if s.strip()]
    if len(addrs) > 10 or any(not re.fullmatch(r"[A-Za-z0-9@]{1,15}", a) for a in addrs):
        errs.append("ALE addresses: up to 10, letters and digits only, 15 characters at most (e.g. 8P6AB).")
    try:
        chans = hfmod.parse_channels(cfg.get("hf_channels") or "")
    except ValueError as e:
        chans, _ = [], errs.append(f"HF channels: {e}")
    try:
        if not 0.5 <= float(cfg.get("hf_dwell") or 1.0) <= 10:
            raise ValueError
    except (TypeError, ValueError):
        errs.append("HF scan dwell must be 0.5-10 seconds per channel.")
    try:
        if not 0 <= int(cfg.get("hf_sound_min") or 0) <= 240:
            raise ValueError
        if int(cfg.get("hf_sound_min") or 0) and int(cfg.get("hf_sound_min")) < 30:
            errs.append("ALE sounding: every 30 minutes or more (0 = off) - keep the bands quiet.")
    except (TypeError, ValueError):
        errs.append("ALE sounding interval must be 0-240 minutes.")
    if str(cfg.get("hf_selcall_tones") or "lm") not in hfmod.SC_TONES:
        errs.append("Selcall tones: choose 1700/1870 Hz (amateur / land mobile) or 1615/1785 Hz (marine).")
    if str(cfg.get("hf_call_format") or "ccir") not in ("ccir", "tradewind"):
        errs.append("Selcall format: standard CCIR 493-4 or Tradewind.")
    try:
        if not 0.5 <= float(cfg.get("hf_ccir_dots_s") or 6) <= 20:
            raise ValueError
    except (TypeError, ValueError):
        errs.append("Selcall preamble (dot pattern) must be 0.5-20 seconds; 6 s suits scanning Barrett / Codan radios.")
    try:
        if not 0 <= int(cfg.get("hf_far_channels") or 0) <= 38:
            raise ValueError
    except (TypeError, ValueError):
        errs.append("Called radio's scan list: 0-38 channels (Barrett preamble is 0.5 s a channel + 1 s, up to 20 s).")
    if cfg.get("hf_scan") and len(chans) < 2:
        errs.append("HF scanning needs at least two channels.")
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
        self.dx_open = {}             # APRS DX mode: target island -> {"since", "evidence"}
        self.dx_beaconed = {}         # target island -> time of the last automatic DX beacon
        self.dx_burst = []            # pending DX beacons: (send at, target, n, of)
        self.iss_sat = None           # v3.15 ISS: SGP4 propagator from the latest TLE
        self.iss_err = ""
        self.iss_loading = False
        self.iss_next_tle = 0         # next TLE refresh (and retry after a failure)
        self.iss_passes = []          # upcoming passes over this station
        self.iss_calc_t = 0           # when the passes were last worked out
        self.iss_calc_key = None      # (grid, min_el, TLE epoch) they were worked out for
        self.iss_cur = None           # the pass in progress
        self.iss_pass_sent = []       # beacon times on the pass in progress
        self.iss_recent = collections.deque(maxlen=20)   # all ISS beacon times (5-in-5-minutes cap)
        self.iss_tuned = False        # radio moved to 145.825 for the pass
        self.iss_warned = False       # told the operator once this pass why beacons cannot go out
        self.iss_last = None          # last pass: summary for the face plate
        self.iss_label = ""           # which source the orbit came from (N2YO / AMSAT / CelesTrak / NASA)
        self.iss_sources = {}         # each source: status, age, distance from the chosen one
        self.iss_cat = None           # satellite (NORAD number) the data above is for
        self.iss_switch_aos = None    # pass the radio was switched for
        self.rx_rec = None            # v3.16 PassRecorder while a pass is being recorded
        self.rx_rec_info = None       # (receiver, sat name, wav path)
        self.rx_tuned_hz = 0          # where the radio is now (Doppler steps)
        self.rx_next_dop = 0
        self.rx_busy = ""             # post-processing in progress (decoding a pass)
        self.rx_orbits = {}           # NORAD -> (sat or None, next refresh) for the SDR receivers
        self.rx_passes = {}           # receiver id -> upcoming passes
        self.sdr_job = None           # satrx.Job running on the SDR
        self.sdr_info = None          # {"rid", "kind": "pass" | "geo", "los", "out"}
        self.sdr_status = {}          # receiver id -> status text for the face plate
        self.sdr_next = 0
        self.sdr_geo_retry = 0
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
        self.wolf_log = {}            # v3.8 DX stations we called (kept for the "I made it" evidence)
        self.my_dx = {}               # v3.7 "I made it": PSKReporter spots of my own signal (evidence for DX cards)
        self.msg_stats = []           # v3.7 field test: delivery time of every chat message
        self.kiss_srv = None          # KISS TCP server for other APRS apps
        self.digi = None              # fill-in digipeater (digi.FillInDigi) while it is switched on
        self.qsy_hz = None            # set while a chat has moved off the APRS frequency
        self.hf = None                # v3.18 HFCenter (HF selcall / ALE) while HF Calling is on
        self.mail_count = {}          # gateway: (callsign, date) -> emails sent today
        self.mail_next_poll = 0
        self.mail_error = ""
        self.wolf = {}                # Wolf Mode: base callsign -> DX station seen on PSKReporter
        self.wolf_pings = collections.deque()
        self.igate_tx_times = collections.deque()
        self.igate_seen = {}          # (src, info) -> last gated, to skip duplicates
        self.tw_users = load_tw_users()   # Tradewind stations heard: call -> {first, last, via}
        self.dir_ops = []                 # global operator directory: list of {call, grid, pos, status, last, online}
        self.dir_online = 0               # how many are online now (from the directory)
        self.dir_next = 0                 # next directory announce/fetch time
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
                s1 = 22 if c.get("tnc") == "sound" and c.get("modem") == "robust_dx" else \
                    44 if c.get("tnc") == "sound" and c.get("modem") in ("robust", "robust_fm") else \
                    300 if c.get("tnc") == "sound" and str(c.get("modem")) == "300" else \
                    (1200 if c.get("tnc") == "sound" else int(c.get("link1_speed") or 1200))
                self.linkctl = tncmod.LinkController({
                    "A": {"name": c.get("link1_name") or f"{s1} bps (main radio)", "speed": s1},
                    "B": {"name": c.get("link2_name") or "second link", "speed": int(c.get("link2_speed") or 9600)}})
            self.commercial = c.get("op_mode") == "commercial"
            self.sec, self.last_id = None, 0
            self.digi = None
            if c.get("digi") and not self.commercial:
                self.digi = digimod.FillInDigi(c["call"], c.get("digi_alias") or "", int(c.get("digi_max_per_min") or 10),
                                               mode=c.get("digi_mode") or "fill")
                kind = "Wide-area digipeater ON (WIDE1-1, WIDE2-1, WIDE2-2" if self.digi.mode == "wide" else \
                    "Fill-in digipeater ON (WIDE1-1"
                self.add_log("SYS", f"{kind}{', ' + self.digi.alias if self.digi.alias else ''}; "
                                    f"max {self.digi.max_per_min}/min)")
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
                self.sec = comstation.open_link(c)
                unit = c["com_unit"].upper()
                self.chat = tncmod.ChatManager(unit, self._com_send)
                self.chat.id_word, self.chat.id_example = "unit ID", ("PX2" if unit != "PX2" else "PX1")
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
                self.chat.retry = (150, 300, 480) if (c.get("tnc") == "sound" and c.get("modem") == "robust_dx") else \
                    (90, 150, 240) if (c.get("tnc") == "sound" and c.get("modem") in ("robust", "robust_fm")) else tncmod.RETRY_S
            if not self.commercial and (not self.chat or self.chat.mycall != c["call"]
                                        or getattr(self.chat, "send_fn", None) != self._chat_send):
                self.chat = tncmod.ChatManager(c["call"], self._chat_send)
                self._attach_chat()
            self._hf_start(c)
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
            self._hf_stop()
            for x in (self.tnc, self.isc, self.tnc2, self.kiss_srv):
                try:
                    x and x.stop()
                except Exception:
                    pass
            self._save_chat()
            self.tnc = self.isc = self.tnc2 = self.kiss_srv = None
            self.digi = None
            self.linkctl = None
            self.iss_tuned = False
            try:
                self._rx_stop()
                self._sdr_finish()
            except Exception:
                pass
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
            if self.dx_burst:
                self.dx_burst_due(now)
            if self.cfg.get("iss_enabled"):
                try:
                    self._iss_tick(now)
                except Exception as e:
                    self.add_log("ERR", f"ISS: {e}")
                try:
                    self._sdr_tick(now)
                except Exception as e:
                    self.add_log("ERR", f"SDR receiver: {e}")
            if self.next_beacon and now >= self.next_beacon and self.iss_tuned:
                self.next_beacon = now + 60          # radio is on 145.825 for an ISS pass: position beacon waits
            if self.next_beacon and now >= self.next_beacon:
                self.send_beacon()
                self.next_beacon = time.time() + max(BEACON_MIN_MIN, self.cfg["beacon_min"]) * 60
            if self.cfg.get("wx_enabled"):
                self._wx_schedule(now)
            c0 = self.cfg
            if c0.get("mail_gw_enabled") and c0.get("imap_host") and now >= self.mail_next_poll and not self.commercial:
                self.mail_next_poll = now + max(2, int(c0.get("mail_poll_min") or 5)) * 60
                threading.Thread(target=self._imap_poll, daemon=True).start()
            if c0.get("wolf_auto") and now >= getattr(self, "wolf_auto_next", 0):
                self.wolf_auto_next = now + 30
                try:
                    self._wolf_auto()
                except Exception as e:
                    self.add_log("ERR", f"Wolf auto-call: {e}")
            if c0.get("disaster_on") and now > float(c0.get("disaster_until") or 0):
                self.set_disaster(False, reason="session expired")
            if self.cfg.get("tc_enabled") and now >= self.tc_next:
                self.tc_next = now + 20 * 60
                threading.Thread(target=self.check_tropical, daemon=True).start()
            if self._dir_base() and now >= self.dir_next:
                self.dir_next = now + 120
                threading.Thread(target=self._directory_sync, daemon=True).start()
            if self.chat:
                try:
                    self.chat.tick()
                    if self.chat.seq != self._chat_saved and time.time() - self._chat_save_t > 5:
                        self._save_chat()
                except Exception as e:
                    self.add_log("ERR", f"chat: {e}")
            self._hf_learned()
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
            try:
                self._dx_update(spots, me)
            except Exception as e:
                self.add_log("ERR", f"DX mode: {e}")
        self.map_paths = self._map_paths(spots)
        self._my_dx_update(spots)
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
                "ops": self._map_ops(),
                "online": self.dir_online,
                "names": eng.MODE_NAMES}

    def _map_ops(self):
        """Tradewind operators to plot as dots: the global directory, minus our own callsign (drawn as 'me')."""
        mycall = (self.cfg.get("call") or "").strip().upper()
        out = []
        for o in self.dir_ops:
            if not isinstance(o, dict) or not o.get("pos"):
                continue
            if (o.get("call") or "").upper() == mycall:
                continue
            out.append({"call": o.get("call", ""), "grid": o.get("grid", ""),
                        "pos": o["pos"], "status": o.get("status", "available"),
                        "online": bool(o.get("online"))})
        return out

    # ---- RF mode: always, or only while selected propagation modes are open
    def rf_possible(self):
        c = self.cfg
        return bool(c.get("rf_announce") and self.tnc and c.get("tnc") != "none")

    def rf_live(self, mode=None):
        """Is RF transmission switched on right now (for this message's mode)?"""
        if not self.rf_possible() or self.qsy_hz or self._hf_holds_radio():
            return False            # moved to a chat channel / HF calling has the radio: beacons and alerts wait (APRS-IS still works)
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

    def transmit(self, info, rf=True, net=True, mode=None, force_rf=False, path=None):
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
            if path is None:
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

    # ---- APRS DX mode
    def dx_targets(self):
        me = eng.grid_to_latlon(self.cfg.get("grid", ""))
        if not me:
            return []
        return dxmode.targets_from(me, float(self.cfg.get("dx_height_m") or 30))

    def _dx_update(self, spots, me):
        c = self.cfg
        if not c.get("dx_enabled"):
            self.dx_open = {}
            return
        targets = dxmode.targets_from(me, float(c.get("dx_height_m") or 30))
        now = time.time()
        found = dxmode.openings(spots, me, targets)
        for name, ev in found.items():
            if name not in self.dx_open:
                t = next(x for x in targets if x["name"] == name)
                self.add_log("DX", f"DX OPEN to {name}: {t['km']} km, beam {t['bearing']} deg ({t['compass']}) - "
                                   f"{ev['tx']} > {ev['rx']} {ev['km']} km {ev['mode']} {ev['snr']} dB ({ev['kind']})")
                if c.get("dx_auto_beacon") and now - self.dx_beaconed.get(name, 0) >= int(c.get("dx_beacon_min") or 60) * 60:
                    self.dx_beaconed[name] = now
                    count, gap = dxmode.burst_plan(c, c.get("dx_burst_count") or 4)
                    self.dx_burst += [(now + i * gap, t, i + 1, count) for i in range(count)]
                    self.add_log("DX", f"{count} DX beacon{'s' if count > 1 else ''} to {name} over the next minute"
                                       + (" (Robust DX: about 22 s each, so 2 fit)" if count == 2 and c.get("modem") == "robust_dx" else ""))
                    self.dx_burst_due(now)
            self.dx_open[name] = {"since": self.dx_open.get(name, {}).get("since", now), "evidence": ev}
        for name in [n for n in self.dx_open if n not in found]:
            self.add_log("DX", f"DX closed to {name}")
            del self.dx_open[name]

    def dx_burst_due(self, now=None):
        """Send the DX beacons whose time has come (called from the station loop every second)."""
        now = time.time() if now is None else now
        due = [b for b in self.dx_burst if b[0] <= now]
        self.dx_burst = [b for b in self.dx_burst if b[0] > now]
        for _, target, n, of in due:
            self.send_dx_beacon(target, n, of)

    def send_dx_beacon(self, target, seq=1, of=1):
        """Status beacon announcing the opening, on the DX path (default: direct, no digipeaters)."""
        c = self.cfg
        modem = c.get("modem") if c.get("tnc") == "sound" else "1200"
        freq = (tncmod.fm_freq_hz(c) if modem == "robust_fm" else
                tncmod.dx_freq_hz(c) if modem in ("robust", "robust_dx") else tncmod.aprs_freq_hz(c))
        label = f"{freq / 1e6:.3f}{'USB' if modem in ('robust', 'robust_dx') else 'FM'}" if freq else ""
        path = [p.strip() for p in (c.get("dx_path") or "").split(",") if p.strip()]
        self.transmit(dxmode.dx_beacon_text(target, c, label, seq, of), path=path)

    # ---- v3.15 Satellite Watch Mode: passes of the ISS (or another APRS satellite) and beacons through it
    SAT_MAX_AGE = 12 * 3600              # refresh orbit data twice a day; cached copies work offline

    def _sat_catnr(self):
        try:
            return int(self.cfg.get("sat_catnr") or issmod.ISS_CATNR)
        except (TypeError, ValueError):
            return issmod.ISS_CATNR

    def _sat_get(self, fname, url):
        """(bytes or None, 'downloaded' | 'cached' | 'unavailable') through the disk cache."""
        path = os.path.join(CACHE_DIR, fname)
        before = os.path.getmtime(path) if os.path.exists(path) else None
        data = cached_get(path, url, self.SAT_MAX_AGE)
        after = os.path.getmtime(path) if os.path.exists(path) else None
        return data, ("unavailable" if data is None else "downloaded" if after != before else "cached")

    def _fetch_orbit(self, cat):
        """Orbit of one satellite from N2YO, AMSAT, CelesTrak and (ISS only) NASA.
        Returns (label, sat, sources, worst disagreement km, error)."""
        key = str(self.cfg.get("n2yo_key") or "").strip()
        got, status = {}, {}
        if key:
            data, how = self._sat_get(f"sat_{cat}_n2yo.json", issmod.n2yo_url(cat, key))
            t = issmod.parse_n2yo(data) if data else None
            got["N2YO"], status["N2YO"] = t, how if t else ("bad reply - check the API key" if data else how)
        else:
            got["N2YO"], status["N2YO"] = None, "add your free N2YO API key to use it"
        data, how = self._sat_get("amsat_nasabare.txt", issmod.AMSAT_URL)
        t = issmod.parse_tle_text(data.decode("ascii", "replace"), cat) if data else None
        got["AMSAT"], status["AMSAT"] = t, how if t else ("not in AMSAT's list" if data else how)
        data, how = self._sat_get(f"sat_{cat}_celestrak.txt", issmod.celestrak_url(cat))
        t = issmod.parse_tle_text(data.decode("ascii", "replace"), cat) if data else None
        got["CelesTrak"], status["CelesTrak"] = t, how if t else ("no data" if data else how)
        sats = {}
        for lab, t in got.items():
            if t:
                try:
                    sats[lab] = issmod.Satellite(t[1], t[2], t[0])
                except ValueError as e:
                    status[lab] = str(e)
        if cat == issmod.ISS_CATNR:
            data, how = self._sat_get("iss_nasa_oem.txt", issmod.NASA_OEM_URL)
            try:
                sats["NASA"] = issmod.Ephemeris(issmod.parse_oem(data.decode("ascii", "replace"))) if data else None
                status["NASA"] = how
            except ValueError as e:
                sats["NASA"], status["NASA"] = None, str(e)
        now = time.time()
        label, sat = issmod.pick_best(sats, now)
        if not sat:
            return None, None, {k: {"status": v} for k, v in status.items()}, 0, \
                "no orbit data yet - Satellite Watch needs the internet once (N2YO, AMSAT, NASA or CelesTrak)"
        src = {}
        for lab, s in sats.items():
            row = {"status": status.get(lab, "")}
            if s is not None:
                row["age_days"] = None if isinstance(s, issmod.Ephemeris) else round((now - s.epoch) / 86400, 1)
                try:
                    row["diff_km"] = 0.0 if lab == label else round(issmod.position_diff_km(s, sat, now), 1)
                except ValueError:
                    row["diff_km"] = None
            src[lab] = row
        for lab in status:
            src.setdefault(lab, {"status": status[lab]})
        worst = max([r["diff_km"] for r in src.values() if r.get("diff_km") is not None] or [0])
        return label, sat, src, worst, ""

    def _iss_load_tle(self):
        """Load the watched satellite's orbit (freshest source) and check how far apart the sources put it.
        Runs in its own thread."""
        try:
            label, sat, src, worst, err = self._fetch_orbit(self._sat_catnr())
            if not sat:
                self.iss_err, self.iss_sources = err, src
                self.iss_next_tle = time.time() + 600
                return
            if not self.iss_sat or getattr(self.iss_sat, "epoch", None) != sat.epoch or self.iss_label != label:
                self.add_log("SYS", f"Satellite Watch: orbit of {issmod.sat_name(sat)} from {label}"
                                    + (f"; sources agree within {worst:.0f} km" if len(src) > 1 and worst else ""))
            self.iss_sat, self.iss_label, self.iss_sources = sat, label, src
            age = 0 if isinstance(sat, issmod.Ephemeris) else (time.time() - sat.epoch) / 86400
            self.iss_err = (f"orbit data is {age:.0f} days old - pass times may be off by minutes" if age > 7 else
                            f"orbit sources disagree by {worst:.0f} km - pass times may be off by up to "
                            f"{worst / 7.7 / 60:.0f} min" if worst > 100 else "")
            self.iss_next_tle = time.time() + (self.SAT_MAX_AGE if age <= 7 else 1800)
        except Exception as e:
            self.iss_err = f"orbit data: {e}"
            self.iss_next_tle = time.time() + 600
        finally:
            self.iss_loading = False

    def _iss_obs(self):
        return eng.grid_to_latlon(self.cfg.get("grid", ""))

    def _iss_refresh(self, now=None):
        """Keep the orbit fresh and the pass list worked out (cheap: called each second while ON,
        and from the face plate when OFF)."""
        now = time.time() if now is None else now
        cat = self._sat_catnr()
        if self.iss_cat != cat:                       # another satellite chosen: start over
            self.iss_cat, self.iss_sat, self.iss_passes, self.iss_next_tle = cat, None, [], 0
        if now >= self.iss_next_tle and not self.iss_loading:
            self.iss_loading = True
            self.iss_next_tle = now + 60
            threading.Thread(target=self._iss_load_tle, daemon=True).start()
        sat, obs = self.iss_sat, self._iss_obs()
        if not sat or not obs:
            self.iss_passes = []
            return
        min_el = float(self.cfg.get("iss_min_el") or 0)
        key = (self.cfg.get("grid", ""), min_el, sat.epoch, cat)
        ahead = [p for p in self.iss_passes if p["los"] > now]
        if key != self.iss_calc_key or now - self.iss_calc_t > 1800 or len(ahead) < 2:
            try:
                ps = issmod.passes(sat, obs, start=now, hours=36, min_el=min_el)
                for p in ps:
                    w = issmod.window_above(sat, obs, p, min_el)
                    p["win"] = w
                    p["planned"] = len(issmod.plan(p["tca"], *w)) if w else 0
                self.iss_passes, self.iss_calc_t, self.iss_calc_key = ps, now, key
            except ValueError as e:
                self.iss_err = f"orbit: {e}"
                self.iss_passes = []
        else:
            self.iss_passes = ahead

    def _sat_freq_hz(self):
        try:
            mhz = float(str(self.cfg.get("sat_freq") or issmod.ISS_FREQ_MHZ).replace(",", "."))
        except ValueError:
            return None
        return int(round(mhz * 1e6)) if 144.0 <= mhz <= 148.0 or 430.0 <= mhz <= 440.0 else None

    def _iss_can_beacon(self):
        """(ok, why not) - can this station send a satellite beacon on RF right now?"""
        c = self.cfg
        if self.commercial:
            return False, "commercial mode"
        if not self.rf_possible():
            return False, "RF transmit is off (MENU > RADIO / TNC)"
        if isinstance(self.tnc, tncmod.SdrTNC) and not self.tnc2:
            return False, "the SDR only receives"
        if c.get("tnc") == "sound" and c.get("modem", "1200") != "1200":
            return False, "the satellite digipeater only decodes 1200 baud AFSK - set Modem to 1200"
        if self.qsy_hz:
            return False, "a chat has moved the radio off frequency"
        return True, ""

    def _sat_radio_rx(self):
        """What the radio does on a pass of the watched satellite: {id, type, label, hz, mode}.
        aprs = beacon through the digipeater; sstv / telemetry = receive and record (no transmitting)."""
        cat = self._sat_catnr()
        rx = {r["id"]: r for r in satrx.radio_receivers(cat)}
        r = rx.get(self.cfg.get("sat_rx") or "") or next((x for x in rx.values() if x["type"] == "aprs"), None)
        if not r or r["type"] == "aprs":
            return {"id": r["id"] if r else "aprs", "type": "aprs", "label": r["label"] if r else "APRS digipeater",
                    "hz": self._sat_freq_hz(), "mode": "fm"}
        return {"id": r["id"], "type": r["type"], "label": r["label"], "hz": int(round(r["mhz"] * 1e6)),
                "mode": r.get("mode", "fm")}

    # ---- v3.18 HF Calling (selcall / ALE)
    def _hf_start(self, c):
        self.hf = None
        if not c.get("hf_enabled") or self.commercial:
            return
        taps = getattr(self.tnc, "audio_taps", None)
        if taps is None or not hasattr(self.tnc, "send_audio"):
            self.add_log("ERR", "HF Calling needs the built-in sound modem - not started")
            return
        try:
            hf = hfmod.HFCenter(c, transmit=self.tnc.send_audio,
                                tune=self._hf_tune if self._can_cat() else None,
                                log=lambda kind, text: self.add_log("HF", text),
                                tx_active=lambda: bool(getattr(self.tnc, "tx_active", False)),
                                rate=getattr(self.tnc, "rate", 48000),
                                pos=self._hf_pos,
                                busy=lambda: bool(self.iss_tuned or self.qsy_hz),
                                tx_ok=getattr(commander, "hf_tx_ok", None) if commander else None,
                                capture_dir=self._rx_dir("hf"))
        except Exception as e:
            self.add_log("ERR", f"HF Calling not started: {e}")
            return
        taps.append(hf.feed)
        hf.start()
        self.hf = hf
        self.add_log("HF", "HF Calling ON - " + hf.summary())

    def _hf_stop(self):
        hf, self.hf = self.hf, None
        if not hf:
            return
        hf.stop()
        try:
            self.tnc.audio_taps.remove(hf.feed)
        except (AttributeError, ValueError):
            pass

    def _hf_tune(self, khz):
        tnc = self.tnc
        return bool(tnc and tnc.tune(int(round(float(khz) * 1000)), "usb"))

    def _hf_pos(self):
        try:
            lat, lon = eng.grid_to_latlon(self.cfg.get("grid", ""))
            return lat, lon
        except Exception:
            return None

    def _hf_holds_radio(self):
        """HF calling owns the radio while it scans, or when the main modem is not an HF data modem:
        APRS beacons / RF alerts wait (APRS-IS keeps working)."""
        hf = self.hf
        return bool(hf) and (hf.scan_on or self.cfg.get("modem") not in ("robust", "robust_dx", "300"))

    def _hf_learned(self):
        """Keep the ALE interleave the decoder calibrated on a real signal, so the next start uses it."""
        hf = self.hf
        v = hf and hf.variant_learned
        if v and v != self.cfg.get("hf_ale_variant"):
            self.cfg["hf_ale_variant"] = v
            try:
                save_config(self.cfg)
            except Exception:
                pass
            self.add_log("HF", f"ALE receive format calibrated ({v}) and saved")

    def hf_state(self):
        hf = self.hf
        base = {"hfn": hfmod.HFN_CHANNELS, "presets": hfmod.presets(), "enabled": bool(self.cfg.get("hf_enabled")),
                "running": getattr(self, "running", False), "holds_radio": self._hf_holds_radio()}
        if not hf:
            base["on"] = False
            return base
        st = hf.state()
        st.update(base)
        st["on"] = True
        return st

    def hf_action(self, b):
        hf = self.hf
        if not hf:
            raise ValueError("Turn on HF Calling (tick it, SAVE) and switch the station ON first.")
        act = str(b.get("action") or "")
        khz = b.get("khz")
        khz = float(khz) if khz not in (None, "") else None
        if act == "call":
            kind = str(b.get("kind") or "selcall")
            to, text = str(b.get("to") or "").strip(), str(b.get("text") or "")
            if kind == "ale":
                hf.ale_call(to, text, khz=khz)
                return f"ALE call to {to.upper()} sent"
            if kind == "sound":
                hf.sound_now(khz)
                return "ALE sounding sent"
            fmt = str(b.get("fmt") or "") or None
            if fmt not in (None, "ccir", "tradewind"):
                raise ValueError("unknown selcall format")
            hf.selcall(kind, to, text=text, number=str(b.get("number") or ""), khz=khz, fmt=fmt)
            return f"{kind.capitalize()} call to {to} sent"
        if act == "scan":
            on = hf.set_scan(bool(b.get("on")))
            if bool(b.get("on")) and not on:
                raise ValueError("Scanning needs a CAT radio link and at least two channels.")
            return "Scanning " + ("ON" if on else "OFF")
        if act == "tune":
            if khz is None:
                raise ValueError("Pick a channel.")
            if not hf.tune_to(khz):
                raise IOError(getattr(self.tnc, "cat_status", "") or "the radio did not take the frequency")
            return f"On {khz:g} kHz USB" + ("" if hf.tune_fn else " (set the radio dial to this by hand)")
        if act == "capture":
            return "Saved " + hf.capture(int(b.get("seconds") or 30), "recorded by the operator")
        if act == "use_link":
            return self.hf_use_link(khz)
        raise ValueError("unknown HF action")

    def hf_use_link(self, khz=None):
        """Linked: move APRS messaging onto this HF channel with the robust modem, scanning off."""
        hf = self.hf
        khz = khz or (hf and (hf.linked or {}).get("khz")) or (hf and hf.khz)
        if not khz:
            raise ValueError("No HF channel yet - make an ALE call or pick a channel first.")
        modem = self.cfg.get("modem")
        note = ""
        if modem not in ("robust", "robust_dx"):
            note = " (Robust modem chosen - 300 baud packet will not survive a weak HF path)" if str(modem) == "300" else ""
            modem = "robust"
        errs = self.update_config({"modem": modem, "dx_freq": f"{float(khz) / 1000:.4f}",
                                   "hf_scan": False, "cat_tune": True})
        if errs:
            raise ValueError("; ".join(errs))
        return f"APRS chat now on {float(khz):g} kHz USB, {modem} modem{note}"

    def _can_cat(self):
        tnc = self.tnc
        return bool(tnc and hasattr(tnc, "tune") and getattr(getattr(tnc, "ptt", None), "can_tune", False))

    def _rx_doppler_hz(self, base_hz, now):
        """Downlink frequency corrected for Doppler (received signal), from the range rate."""
        sat, obs = self.iss_sat, self._iss_obs()
        if not (sat and obs and base_hz):
            return base_hz
        try:
            r1, r2 = issmod.look(sat, obs, now - 1)[2], issmod.look(sat, obs, now + 1)[2]
        except ValueError:
            return base_hz
        return satrx.doppler_hz(base_hz, (r2 - r1) / 2.0)

    def _iss_tune(self, on, now=None):
        """Satellite Switch: move the radio (IC-9100, FT-991A or another CAT radio) to the satellite frequency
        - FM data for APRS / SSTV, USB data for BPSK telemetry, Doppler-corrected when receiving - or back to APRS."""
        tnc, rx = self.tnc, self._sat_radio_rx()
        now = time.time() if now is None else now
        base = rx["hz"]
        hz = self._rx_doppler_hz(base, now) if on and rx["type"] != "aprs" and self.cfg.get("sat_doppler", True) else base
        mode = rx["mode"].upper()
        if not self._can_cat():
            if on and base:
                self.add_log("ISS", f"Set the radio to {base / 1e6:.3f} MHz {mode} now for {rx['label']} "
                                    "(no CAT link for automatic switching)")
            return
        target = hz if on else (self.qsy_hz or tncmod.aprs_freq_hz(self.cfg))
        radio = getattr(tnc.ptt, "name", "radio")
        if target and tnc.tune(target, rx["mode"] if on else None):
            self.iss_tuned = on
            self.rx_tuned_hz = target if on else 0
            self.add_log("SYS", f"Satellite Switch: {radio} tuned to {target / 1e6:.4f} MHz "
                                + (f"{mode} for {rx['label']}" if on else "for APRS"))
        elif on:
            self.add_log("ISS", f"Could not switch the radio ({getattr(tnc, 'cat_status', '')}) - "
                                f"set it to {(base or 0) / 1e6:.3f} MHz {mode}")

    def _rx_doppler_step(self, now):
        """Follow the Doppler shift while receiving: retune when it has moved by more than a step."""
        rx = self._sat_radio_rx()
        if rx["type"] == "aprs" or not self.cfg.get("sat_doppler", True) or not self._can_cat() \
                or now < self.rx_next_dop or getattr(self.tnc, "tx_active", False):
            return
        self.rx_next_dop = now + 10
        hz = self._rx_doppler_hz(rx["hz"], now)
        step = 100 if rx["mode"] == "usb" else 500
        if abs(hz - self.rx_tuned_hz) >= step and self.tnc.tune(hz, rx["mode"]):
            self.rx_tuned_hz = hz

    def _rx_dir(self, *parts):
        d = os.path.join(RX_DIR, *parts)
        os.makedirs(d, exist_ok=True)
        return d

    def _rx_start(self, sat_name):
        """Start recording the radio's audio for this pass (SSTV / telemetry)."""
        rx = self._sat_radio_rx()
        if rx["type"] == "aprs" or self.rx_rec:
            return
        taps = getattr(self.tnc, "audio_taps", None)
        if taps is None:
            self.add_log("ISS", f"{rx['label']}: needs the built-in sound modem (MENU > RADIO / TNC) to record the pass")
            return
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        safe = re.sub(r"[^A-Za-z0-9]+", "-", sat_name).strip("-")
        wav = os.path.join(self._rx_dir(rx["type"]), f"{stamp}_{safe}.wav")
        self.rx_rec = satrx.PassRecorder(wav, getattr(self.tnc, "rate", 48000))
        self.rx_rec_info = (rx, sat_name, wav)
        taps.append(self.rx_rec)
        self.add_log("ISS", f"Recording {rx['label']} ({rx['hz'] / 1e6:.3f} MHz {rx['mode'].upper()})")

    def _rx_stop(self):
        """Pass over: stop recording and decode it in the background."""
        rec, info = self.rx_rec, self.rx_rec_info
        if not rec:
            return
        try:
            self.tnc.audio_taps.remove(rec)
        except (AttributeError, ValueError):
            pass
        secs = rec.close()
        self.rx_rec = self.rx_rec_info = None
        if secs < 5:
            return
        self.rx_busy = f"decoding {info[0]['label']}"
        threading.Thread(target=self._rx_decode, args=(info, secs), daemon=True).start()

    def _rx_decode(self, info, secs):
        rx, sat_name, wav = info
        self.rx_busy = f"decoding {rx['label']}"
        keep = bool(self.cfg.get("rx_keep_wav"))
        try:
            if rx["type"] == "sstv":
                audio, rate = satrx.read_wav(wav)
                pics = satrx.sstv_decode(audio, rate)
                for i, (mode, rgb) in enumerate(pics, 1):
                    png = wav[:-4] + f"_{i}_{mode.replace(' ', '')}.png"
                    satrx.write_png(png, rgb)
                    self.add_log("ISS", f"SSTV picture received from {sat_name}: {mode}, {rgb.shape[1]}x{rgb.shape[0]}")
                if not pics:
                    self.add_log("ISS", f"No SSTV picture in this {sat_name} pass ({secs / 60:.0f} min recorded)")
                    keep = True
            else:                                               # BPSK telemetry
                cat = next((s["catnr"] for s in satrx.CATALOG if rx in s["receivers"]), self._sat_catnr())
                if not shutil.which("gr_satellites"):
                    self.add_log("ISS", f"{sat_name} pass recorded ({secs / 60:.0f} min). Install gr-satellites to "
                                        "decode telemetry; the recording is kept in the received folder")
                    keep = True
                else:
                    kss, txt = wav[:-4] + ".kss", wav[:-4] + ".txt"
                    with open(txt, "wb") as out:
                        r = subprocess.run(satrx.gr_satellites_cmd(cat, wav, satrx.read_wav(wav)[1], kss),
                                           stdout=out, stderr=subprocess.STDOUT, timeout=1800)
                    n = 0
                    if os.path.exists(kss):
                        with open(kss, "rb") as f:
                            n = f.read().count(b"\xc0") // 2
                    self.add_log("ISS", f"{sat_name} telemetry: {n} frame{'s' if n != 1 else ''} decoded"
                                 + ("" if r.returncode == 0 else f" (gr-satellites exit {r.returncode})"))
                    keep = keep or n == 0
        except Exception as e:
            self.add_log("ERR", f"decoding {rx['label']}: {e}")
            keep = True
        finally:
            self.rx_busy = ""
            if not keep:
                try:
                    os.remove(wav)
                except OSError:
                    pass

    def _iss_tick(self, now):
        """Every second while the station is ON: switch the radio before AOS, follow the pass, and - for APRS -
        send beacons on schedule from the horizon, or - for SSTV / telemetry - record and decode the pass."""
        self._iss_refresh(now)
        sat, obs, c = self.iss_sat, self._iss_obs(), self.cfg
        rx = self._sat_radio_rx()
        if c.get("iss_tune", True) and not self.iss_tuned and self.running and self.tnc:
            nxt = next((x for x in self.iss_passes if x["aos"] - SAT_SWITCH_LEAD_S <= now < x["los"]), None)
            if nxt and nxt["aos"] != self.iss_switch_aos:
                self.iss_switch_aos = nxt["aos"]
                ok, why = self._iss_can_beacon() if c.get("iss_auto_beacon") and rx["type"] == "aprs" else (True, "")
                if ok or "1200" not in why:
                    self._iss_tune(True, now)
        p = next((x for x in self.iss_passes if x["aos"] <= now < x["los"]), None)
        if self.iss_cur and (not p or p["aos"] != self.iss_cur["aos"]):        # the pass has ended
            n = len(self.iss_pass_sent)
            self.iss_last = {"los": self.iss_cur["los"], "max_el": self.iss_cur["max_el"], "sent": n}
            self.add_log("ISS", f"{issmod.sat_name(sat)} pass over: LOS az {self.iss_cur['los_az']} deg"
                                + (f", {n} beacon{'s' if n != 1 else ''} sent" if rx["type"] == "aprs" else ""))
            self._rx_stop()
            if self.iss_tuned:
                self._iss_tune(False)
            self.iss_cur, self.iss_pass_sent, self.iss_warned = None, [], False
        if not p or not sat or not obs:
            return
        if not self.iss_cur:
            self.iss_cur, self.iss_pass_sent, self.iss_warned = p, [], False
            what = (" - beacon sequence starting" if c.get("iss_auto_beacon") else "") if rx["type"] == "aprs" \
                else f" - receiving {rx['label']}"
            self.add_log("ISS", f"{issmod.sat_name(sat)} on the horizon: AOS az {p['aos_az']} deg "
                                f"({issmod.compass8(p['aos_az'])}), max {p['max_el']:.0f} deg at az {p['tca_az']}, "
                                f"LOS az {p['los_az']} deg ({issmod.compass8(p['los_az'])}) in "
                                f"{round((p['los'] - now) / 60)} min" + what)
            if rx["type"] != "aprs":
                self._rx_start(issmod.sat_name(sat))
        if rx["type"] != "aprs":
            if self.iss_tuned:
                self._rx_doppler_step(now)
            return
        if not c.get("iss_auto_beacon"):
            return
        az, el, _ = issmod.look(sat, obs, now)
        due, _ = issmod.beacon_due(now, p["tca"], el, float(c.get("iss_min_el") or 0),
                                   self.iss_pass_sent, list(self.iss_recent))
        if not due:
            return
        ok, why = self._iss_can_beacon()
        if not ok:
            if not self.iss_warned:
                self.iss_warned = True
                self.add_log("ISS", f"No satellite beacons this pass: {why}")
            return
        if c.get("iss_tune", True) and not self.iss_tuned:
            self._iss_tune(True, now)
        self.iss_pass_sent.append(now)
        self.iss_recent.append(now)
        self.send_iss_beacon(len(self.iss_pass_sent), el, rising=now < p["tca"])

    # ---- v3.16 SDR receivers: Meteor LRPT and HamTV on their passes, GOES HRIT/EMWIN continuously
    def _rx_orbit(self, cat, now):
        """Orbit for an SDR receiver's satellite (loaded in the background, refreshed twice a day)."""
        sat, nxt = self.rx_orbits.get(cat, (None, 0))
        if cat == self._sat_catnr() and self.iss_sat:
            return self.iss_sat
        if now >= nxt:
            self.rx_orbits[cat] = (sat, now + 600)

            def load():
                label, s, _, _, err = self._fetch_orbit(cat)
                self.rx_orbits[cat] = (s or sat, time.time() + (self.SAT_MAX_AGE if s else 600))
            threading.Thread(target=load, daemon=True).start()
        return sat

    def _sdr_tick(self, now):
        """Every few seconds while ON: run one SDR job at a time - a LEO pass beats the geostationary feed."""
        if now < self.sdr_next:
            return
        self.sdr_next = now + 5
        c = self.cfg
        jobs = [j for j in (c.get("rx_jobs") or []) if satrx.receiver(j)[1] and satrx.receiver(j)[1]["hw"] != "radio"]
        obs = self._iss_obs()
        st = {}
        job, info = self.sdr_job, self.sdr_info
        if job and (not job.running() or (info["kind"] == "pass" and now > info["los"] + 60)
                    or info["rid"] not in jobs):
            self._sdr_finish()
            job, info = None, None
        if not jobs or not obs:
            self.sdr_status = {j: ("set your grid square" if not obs else "") for j in jobs}
            return
        if c.get("tnc") == "sdr":
            self.sdr_status = {j: "the RTL-SDR is in use as the APRS receiver (MENU > RADIO / TNC)" for j in jobs}
            return
        tools = satrx.tools_status()
        want = None                                            # (rid, kind, los, pass)
        for rid in jobs:
            s, r = satrx.receiver(rid)
            need = "satdump" if r["type"] in ("lrpt", "hrit") else "ffmpeg"
            if not tools.get(need) or (r["type"] == "hamtv" and not (c.get("hamtv_cmd") or tools.get("leandvb"))):
                st[rid] = f"install {need if tools.get('ffmpeg') or need != 'ffmpeg' else 'ffmpeg'}" \
                          + (" and leandvb" if r["type"] == "hamtv" and not tools.get("leandvb") else "")
                continue
            if s["kind"] == "geo":
                az, el, _ = satrx.geo_look(obs, s["geo_lon"])
                if el < float(c.get("geo_min_el") or 5):
                    st[rid] = f"{s['name']} is only {el:.1f} deg above your horizon"
                    continue
                st[rid] = f"ready - dish az {az:.0f} deg, el {el:.0f} deg"
                if want is None:
                    want = (rid, "geo", 0, None)
                continue
            sat = self._rx_orbit(s["catnr"], now)
            if not sat:
                st[rid] = "loading orbit"
                continue
            ps = self.rx_passes.get(rid)
            if not ps or ps[0]["los"] < now or now - ps[0].get("_t", 0) > 1800:
                try:
                    ps = [dict(p, _t=now) for p in issmod.passes(sat, obs, start=now, hours=24, min_el=10)]
                except ValueError as e:
                    st[rid] = f"orbit: {e}"
                    continue
                self.rx_passes[rid] = ps
            cur = next((p for p in ps if p["aos"] <= now < p["los"]), None)
            nxt = next((p for p in ps if p["aos"] > now), None)
            st[rid] = (f"receiving - LOS in {round((cur['los'] - now) / 60)} min" if cur else
                       f"next pass {time.strftime('%H:%M', time.localtime(nxt['aos']))}, max {nxt['max_el']:.0f} deg"
                       if nxt else "no pass in 24 h")
            if cur and (want is None or want[1] == "geo"):
                want = (rid, "pass", cur["los"], cur)
        if job and want and (info["rid"] != want[0]) and want[1] == "pass" and info["kind"] == "geo":
            self._sdr_finish()                                 # a pass pre-empts the geostationary feed
            job = None
        if not job and want and not (want[1] == "geo" and now < self.sdr_geo_retry):
            self._sdr_start(*want[:3], now=now)
        if self.sdr_info:
            st[self.sdr_info["rid"]] = ("RUNNING - " + st.get(self.sdr_info["rid"], "")).rstrip(" -")
            tail = self.sdr_job.tail()[0] if self.sdr_job else ""
            if tail:
                st[self.sdr_info["rid"]] += f" | {tail[-80:]}"
        self.sdr_status = st

    def _sdr_start(self, rid, kind, los, now=None):
        now = time.time() if now is None else now
        s, r = satrx.receiver(rid)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
        out = self._rx_dir(r["type"], f"{stamp}_{re.sub(r'[^A-Za-z0-9]+', '-', s['name']).strip('-')}")
        secs = int(los - now + 30) if kind == "pass" else 3600
        try:
            if r["type"] == "hamtv":
                cmd, why = satrx.hamtv_cmd(r, os.path.join(out, "hamtv.ts"), self.cfg)
                if not cmd:
                    self.sdr_status[rid] = why
                    self.sdr_next = now + 60
                    return
                self.sdr_job = satrx.Job(rid, cmd, out, shell=True)
            else:
                self.sdr_job = satrx.Job(rid, satrx.satdump_cmd(r, out, secs, self.cfg), out)
        except OSError as e:
            self.add_log("ERR", f"{r['label']}: {e}")
            self.sdr_geo_retry = now + 300
            return
        self.sdr_info = {"rid": rid, "kind": kind, "los": los, "out": out, "start": now}
        self.add_log("ISS", f"SDR: {r['label']} started ({r['mhz']:.1f} MHz)"
                            + (f" until LOS in {round((los - now) / 60)} min" if kind == "pass" else ""))

    def _sdr_finish(self):
        job, info = self.sdr_job, self.sdr_info
        self.sdr_job = self.sdr_info = None
        if not job:
            return
        code = job.stop()
        s, r = satrx.receiver(info["rid"])
        if info["kind"] == "geo" and time.time() - info["start"] < 120:
            self.sdr_geo_retry = time.time() + 300             # exits straight away: don't spin
        if r["type"] == "hamtv":
            def finish():
                mp4, jpg, msg = satrx.video_finish(os.path.join(info["out"], "hamtv.ts"))
                self.add_log("ISS", f"HamTV video saved ({satrx.probe_codec(mp4) or 'video'})" if mp4
                             else f"HamTV: {msg}")
            threading.Thread(target=finish, daemon=True).start()
        else:
            n = sum(1 for x in satrx.gallery(info["out"], 10000) if x["kind"] == "image")
            self.add_log("ISS", f"SDR: {r['label']} stopped - {n} picture{'s' if n != 1 else ''}"
                                + (f" (SatDump exit {code})" if code not in (0, None, -15) else ""))

    def rx_state(self):
        rx = self._sat_radio_rx()
        return {"radio": {**rx, "choices": [{"id": r["id"], "label": r["label"], "mhz": r["mhz"],
                                              "mode": r.get("mode", "fm"), "type": r["type"]}
                                             for r in satrx.radio_receivers(self._sat_catnr())],
                          "recording": bool(self.rx_rec), "tuned_hz": self.rx_tuned_hz if self.iss_tuned else 0,
                          "busy": self.rx_busy},
                "sdr": [{"id": r["id"], "label": r["label"], "sat": s["name"], "mhz": r["mhz"], "hw": r["hw"],
                         "on": r["id"] in (self.cfg.get("rx_jobs") or []),
                         "status": self.sdr_status.get(r["id"], "")} for s, r in satrx.sdr_receivers()],
                "tools": satrx.tools_status(), "not_supported": satrx.NOT_SUPPORTED}

    def send_iss_beacon(self, seq, el, rising=True):
        """Position beacon through the satellite digipeater (path ARISS), RF only - stations that hear it gate it."""
        c = self.cfg
        p = self._iss_obs()
        if not p:
            return
        lat, lon = latlon_to_aprs(*p)
        sym = (c.get("symbol") or "/-").ljust(2)[:2]
        path = [x.strip().upper() for x in (c.get("iss_path") or issmod.ISS_PATH).split(",") if x.strip()]
        self.add_log("ISS", f"Satellite beacon {seq} ({'rising' if rising else 'setting'}, el {el:.0f} deg)")
        self.transmit(f"={lat}{sym[0]}{lon}{sym[1]}{issmod.beacon_comment(c, seq, el)}",
                      net=False, force_rf=True, path=path)

    def iss_state(self):
        """Satellite Watch block for the face plate."""
        now = time.time()
        self._iss_refresh(now)
        sat, obs = self.iss_sat, self._iss_obs()
        hz = self._sat_freq_hz()
        out = {"freq": f"{hz / 1e6:.3f}" if hz else "", "name": issmod.sat_name(sat) if sat else "",
               "catnr": self._sat_catnr(), "source": self.iss_label if sat else "", "sources": self.iss_sources,
               "error": self.iss_err if sat or not self.iss_loading else "",
               "loading": self.iss_loading and not sat, "tuned": self.iss_tuned, "last": self.iss_last,
               "tle_age_days": (round((now - sat.epoch) / 86400, 1) if sat and not isinstance(sat, issmod.Ephemeris)
                                else None),
               "now": None, "cur": None,
               "passes": [{k: p[k] for k in ("aos", "tca", "los", "max_el", "aos_az", "tca_az", "los_az",
                                             "duration", "planned")} for p in self.iss_passes[:6]]}
        if sat and obs:
            try:
                az, el, rng = issmod.look(sat, obs, now)
                out["now"] = {"az": round(az), "el": round(el, 1), "km": round(rng), "up": el >= 0}
            except ValueError:
                pass
        if self.iss_cur:
            out["cur"] = {"aos": self.iss_cur["aos"], "los": self.iss_cur["los"], "tca": self.iss_cur["tca"],
                          "sent": len(self.iss_pass_sent), "rising": now < self.iss_cur["tca"]}
        if self.running and self.cfg.get("iss_auto_beacon"):
            ok, why = self._iss_can_beacon()
            out["blocked"] = "" if ok else why
        return out

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
        robust = self.cfg.get("tnc") == "sound" and self.cfg.get("modem") in ("robust", "robust_dx", "robust_fm")
        self.chat.on_qsy = None if self.commercial else self._on_qsy   # a licensed commercial channel never moves
        tncmod.set_zone(tncmod.zone_of(self.cfg))
        tncmod.set_uhf(self.cfg.get("uhf_range") or "430-440")
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
                raw = secmod.seal_open(bytes.fromhex(self.cfg["com_key"]), raw)
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
                raw = secmod.seal_close(bytes.fromhex(self.cfg["com_key"]), raw)
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
        pos = aprs_position(line.partition(":")[2])
        me = eng.grid_to_latlon(self.cfg.get("grid", ""))
        if pos:
            h["grid"] = latlon_to_grid(*pos)
            if me:
                h["km"] = round(eng.haversine_km(me, pos))
            if me and not digi and h["km"] >= h.get("best_km", 0):   # best DIRECT contact = DX evidence
                h.update(best_km=h["km"], best_t=time.time(), best_pkt=line[:200])
        dst = head.split(">", 1)[1].split(",")[0].upper()
        if dst == eng.TOCALL or call in self.tw_users:
            h["tw"] = True
            self._tw_seen(call, "RF")
        if len(self.heard) > 300:
            oldest = min(self.heard.values(), key=lambda x: x["last"])
            self.heard.pop(oldest["call"], None)

    def _my_dx_update(self, spots):
        """Spots where my own call was heard or heard someone: independent evidence for an "I made it" card."""
        me = self.cfg.get("call", "").split("-")[0].upper()
        now = time.time()
        for sp in spots:
            for mine, other, ogrid, mgrid, heard_by in ((sp.tx, sp.rx, sp.rx_grid, sp.tx_grid, True),
                                                       (sp.rx, sp.tx, sp.tx_grid, sp.rx_grid, False)):
                if mine.split("/")[0].split("-")[0].upper() != me or sp.km < 100:
                    continue
                key = (other.upper(), sp.band, sp.mode)
                old = self.my_dx.get(key)
                if not old or sp.km > old["km"]:
                    self.my_dx[key] = {"call": other.upper(), "grid": ogrid, "my_grid": mgrid, "km": round(sp.km),
                                       "band": sp.band, "mode": sp.mode, "snr": sp.snr, "ts": sp.ts,
                                       "prop": sp.prop_mode or "", "heard_me": heard_by, "freq": sp.freq}
        self.my_dx = {k: v for k, v in self.my_dx.items() if now - v["ts"] < 7 * 86400}

    def field_report(self):
        """A summary friends can send back after a test session: how well messages got through, and how fast."""
        c = self.cfg
        st = list(self.chat.stats) if self.chat else []
        def summ(rows):
            ok = [r for r in rows if r["result"] == "delivered"]
            secs = sorted(r["secs"] for r in ok)
            return {"sent": len(rows), "delivered": len(ok), "pct": round(100 * len(ok) / len(rows)) if rows else 0,
                    "median_s": secs[len(secs) // 2] if secs else None, "worst_s": secs[-1] if secs else None,
                    "avg_tries": round(sum(r["tries"] for r in rows) / len(rows), 1) if rows else 0}
        by_route = {}
        for r in st:
            key = "RF" if r["via"].upper().startswith("RF") or r["route"] in ("rf", "rf_only") else \
                  ("Internet" if r["via"].upper().startswith("IS") or r["route"].startswith("is") else (r["route"] or "auto"))
            by_route.setdefault(key, []).append(r)
        return {"app": APP_NAME, "version": APP_VERSION, "call": c.get("call", ""), "grid": c.get("grid", "")[:4],
                "tnc": getattr(self.tnc, "kind", "") if self.tnc else c.get("tnc", ""),
                "zone": tncmod.zone_of(c), "aprs_mhz": (tncmod.aprs_freq_hz(c) or 0) / 1e6,
                "since": min([r["t"] for r in st]) if st else None, "generated": time.time(),
                "all": summ(st), "by_route": {k: summ(v) for k, v in by_route.items()},
                "by_station": {k: summ([r for r in st if r["call"] == k]) for k in sorted({r["call"] for r in st})},
                "heard_rf": len(self.heard), "heard_direct": sum(1 for h in self.heard.values() if h.get("direct")),
                "tradewind_heard": sum(1 for h in self.heard.values() if h.get("tw")),
                "best_direct_km": max([h.get("best_km", 0) for h in self.heard.values()] or [0]),
                "errors": [l for l in list(self.log)[-200:] if l.get("kind") == "ERR"][-15:] if hasattr(self, "log") else []}

    def dx_evidence(self):
        """Candidates for an "I made it" card: PSKReporter spots of my signal, and stations I heard DIRECT on RF."""
        c = self.cfg
        def sw_of(pkt):
            dst = pkt.partition(":")[0].split(">", 1)[-1].split(",")[0].upper()
            return "Tradewind" if dst == eng.TOCALL else next((n for k, n in self.SW.items() if dst.startswith(k)), "APRS")
        rf = [{"call": h["call"], "grid": h.get("grid", ""), "km": h.get("best_km", 0), "t": h.get("best_t", 0),
               "packet": h.get("best_pkt", ""), "sw": sw_of(h.get("best_pkt", "")), "tw": bool(h.get("tw"))}
              for h in self.heard.values() if h.get("best_km", 0) >= 50]
        aprs_dx = []                      # Wolf calls answered over RF: an APRS message and its ack both crossed the path
        for r in (self.chat.stats if self.chat else []):
            w = self.wolf.get(r["call"].split("-")[0]) or self.wolf_log.get(r["call"].split("-")[0])
            if w and r["result"] == "delivered" and str(r["via"]).upper().startswith("RF"):
                aprs_dx.append({"call": r["call"], "grid": w.get("grid", ""), "km": w.get("km", 0), "band": w.get("band", "2m"),
                                "prop": w.get("prop", ""), "t": r["t"], "secs": r["secs"], "snr": w.get("snr", ""),
                                "mode": w.get("mode", "")})
        chats = {}
        if self.chat:
            for p in self.chat.peers.values():
                ins = [m for m in p["messages"] if m["dir"] == "in" and str(m.get("via", "RF")).upper().startswith("RF")]
                outs = [m for m in p["messages"] if m["dir"] == "out" and m.get("state") == "delivered"]
                if ins or outs:
                    chats[p["call"].split("-")[0]] = {"rx": len(ins), "tx_ok": len(outs),
                                                     "last": max([m["t"] for m in ins + outs])}
        for r in rf:
            r["chat"] = chats.get(r["call"].split("-")[0])
        return {"me": {"call": c.get("call", ""), "grid": c.get("grid", "")},
                "spots": sorted(self.my_dx.values(), key=lambda x: -x["km"])[:30],
                "rf": sorted(rf, key=lambda x: -x["km"])[:30],
                "aprs_dx": sorted(aprs_dx, key=lambda x: -x["km"])[:20]}

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

    WOLF_SPOT_FRESH_S = 15 * 60

    def _wolf_text(self, w, text=""):
        c = self.cfg
        qth = (c.get("wolf_qth") or c.get("grid", "")[:4]).strip()
        tpl = text or c.get("wolf_msg") or DEFAULTS["wolf_msg"]
        try:
            out = tpl.format(me=c["call"].split("-")[0], grid=qth, km=w["km"], band=w.get("band", "2m"),
                             prop=(w.get("prop") or w.get("mode") or "path"), mode=w.get("mode", ""), call=w["call"])
        except (KeyError, IndexError, ValueError):
            out = tpl
        return " ".join(out.split())[:tncmod.MAX_TEXT]

    def wolf_rf_check(self, w):
        """Can this DX station be called on RF right now? Returns (ok, reason)."""
        c, now = self.cfg, time.time()
        if self.commercial or c.get("tnc") not in ("sound", "kiss_tcp", "kiss_serial") or not self.tnc:
            return False, "no transmitting radio"
        if self.chat and self.chat.rf_block:
            return False, "radio is on a satellite frequency"
        if self.qsy_hz:
            return False, "radio is on a chat channel"
        if now - w.get("t", 0) > int(c.get("wolf_open_min") or 15) * 60:
            return False, "no fresh PSKReporter spot - the path may be closed"
        my_hz = tncmod.aprs_freq_hz(c)
        band = "70cm" if my_hz and my_hz > 400e6 else "2m"
        if w.get("band") != band:
            return False, f"path is open on {w.get('band')}, your APRS radio is on {band}"
        theirs = tncmod.ZONES[tncmod.zone_for_grid(w.get("grid", ""))]["aprs"]
        if my_hz and abs(float(theirs) * 1e6 - my_hz) > 5000:
            return False, f"they use {theirs} MHz for APRS, you use {my_hz / 1e6:.3f}"
        return True, ""

    def wolf_ping(self, base, text="", auto=False):
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
        if not self.chat:
            raise ValueError("Turn the station ON first.")
        rf_ok, why = self.wolf_rf_check(w)
        is_ok = bool(self.isc and self.isc.verified)
        if rf_ok:            # 2m RF first: a reply heard on RF is real DX proof; internet only as a fallback
            route, how = ("smart" if c.get("wolf_auto_fallback", True) and is_ok else "rf"), "2m RF"
        elif is_ok and (not auto or c.get("wolf_auto_fallback", True)):
            route, how = "is", "internet"
        else:
            raise ValueError(f"Can't reach {w['call']}: {why or 'no radio path'}" + ("" if is_ok else ", and APRS-IS is not connected"))
        text = self._wolf_text(w, text)
        self.chat.open_direct(w["aprs_call"], route)
        self.chat.send(w["aprs_call"], text)
        w.update(pinged=now, ping_route=how, ping_why=why if not rf_ok else "")
        self.wolf_log[w["call"]] = dict(w)
        self.wolf_pings.append(now)
        self.add_log("SYS", f"Wolf {'auto-call' if auto else 'ping'} to {w['aprs_call']} by {how} ({w['km']} km): {text}")
        return how

    def _wolf_auto(self):
        """Wolf auto-call: call a DX station that PSKReporter shows open to us NOW and that is on APRS."""
        c, now = self.cfg, time.time()
        if not (c.get("wolf_on") and c.get("wolf_auto") and self.chat and self.wolf):
            return
        stop = self.wolf_stopped()
        for w in sorted(self.wolf.values(), key=lambda w: -w["km"]):
            if not w.get("aprs_call") or now - w.get("aprs_t", 0) > 7200 or w["call"] in stop:
                continue
            if now - w.get("pinged", 0) < 86400 or now - w.get("t", 0) > int(c.get("wolf_open_min") or 15) * 60:
                continue
            if not self.wolf_rf_check(w)[0] and not c.get("wolf_auto_fallback", True):
                continue
            try:
                self.wolf_ping(w["call"], auto=True)
            except ValueError as e:
                w["pinged"] = now          # don't retry every loop; it can be pinged by hand
                w["ping_why"] = str(e)
            return                         # at most one auto-call per loop pass

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
                         "pinged": bool(now - w.get("pinged", 0) < 86400), "ping_route": w.get("ping_route", ""),
                         "ping_why": w.get("ping_why", ""), "spot_ago": int(now - w.get("t", now)),
                         "rf_ok": self.wolf_rf_check(w)[0] if on_aprs else False})
        return {"on": bool(self.cfg.get("wolf_on")), "auto": bool(self.cfg.get("wolf_auto")), "rows": rows[:40],
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
                "online": self.dir_online,
                "list": sorted(({"call": c, **v} for c, v in u.items()), key=lambda x: -x["last"])[:60]}

    # ---- Global Operators directory (v3.9): presence on the world map
    def _dir_base(self):
        """The directory service URL, or '' if it isn't set up."""
        url = (self.cfg.get("directory_url") or "").strip().rstrip("/")
        if not url or "YOURNAME" in url or not url.startswith("http"):
            return ""
        return url

    def _directory_sync(self):
        """Announce this station (if opted in) and fetch the global operator roster."""
        base = self._dir_base()
        if not base:
            return
        c = self.cfg
        call = (c.get("call") or "").strip().upper()
        if c.get("map_show") and call and not self.commercial:
            locate_by = c.get("locate_by", "grid")
            pos = None
            if locate_by != "ip":
                ll = eng.grid_to_latlon(c.get("grid", ""))
                if ll:
                    pos = [round(ll[0], 3), round(ll[1], 3)]
            body = {"call": call, "grid": c.get("grid", ""), "pos": pos,
                    "status": c.get("hail_status", "available"), "locate_by": locate_by}
            _web_json(base + "/announce", body)     # best-effort; ignore the reply
        r = _web_json(base + "/ops")
        if isinstance(r, dict) and isinstance(r.get("ops"), list):
            self.dir_ops = r["ops"]
            self.dir_online = int(r.get("online") or 0)

    def relay_candidates(self, dest):
        """RF-first routing: Tradewind stations heard directly on RF in the last 30 minutes, newest first."""
        now = time.time()
        hs = [h for h in self.heard.values() if h.get("tw") and h.get("direct") and now - h["last"] < LOCAL_HEARD_S
              and h["call"] != tncmod.norm_call(dest)]
        return [h["call"] for h in sorted(hs, key=lambda h: -h["last"])]

    def _pick_link(self, call, route):
        """Which radio link a message goes out on: 'A' (main radio), 'B' (second link) or None."""
        link = self._pick_link_raw(call, route)
        if link == "A" and self._hf_holds_radio():   # v3.18: HF calling has the main radio
            if self.tnc2:
                return "B"
            if route not in ("rf_is", "is") or not (self.isc and self.isc.verified):
                raise IOError("the main radio is busy with HF Calling - press USE THIS LINK in HF CALLING, "
                              "or stop scanning, to send APRS messages on it")
            return None
        return link

    def _pick_link_raw(self, call, route):
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
        self._digipeat(line, link)
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

    def _digipeat(self, line, link="A"):
        """Fill-in digipeater: repeat a heard packet once if its first hop is WIDE1-1 (or our call / alias)."""
        if not self.digi:
            return
        try:
            r = self.digi.consider(line)
            if not r:
                return
            tx = self.tnc2 if link == "B" else self._tx_tnc()
            if not tx or getattr(tx, "modem", "1200") in ("robust", "robust_dx", "robust_fm", "300"):
                return
            src, dst, path, info = r
            tx.send(src, dst, path, info)
            self.add_log("DIG", f"{src}>{','.join([dst] + path)}:{info}"[:200])
        except Exception as e:
            self.add_log("ERR", f"digipeater: {e}")

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
            "app": APP_TITLE, "edition": EDITION, "licence": commander.LICENCE_LABEL if commander else "MIT License", "version": APP_VERSION, "running": self.running,
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
            "digi": self.digi.status if self.digi else None,
            "dx": {"enabled": bool(c.get("dx_enabled")), "targets": [
                {**t, "open": t["name"] in self.dx_open, "evidence": self.dx_open.get(t["name"], {}).get("evidence")}
                for t in self.dx_targets()]} if c.get("dx_enabled") else None,
            "iss": self.iss_state() if c.get("iss_enabled") else None,
            "rx": self.rx_state() if c.get("iss_enabled") else None,
            "links": ([{"link": l, **v} for l, v in sorted(self.linkctl.links.items())] if self.linkctl else []),
            "link2": None if not self.tnc2 else {"kind": self.tnc2.kind, "error": self.tnc2.error,
                                                  "rx": self.tnc2.rx_count, "tx": self.tnc2.tx_count},
            "commlog_count": len(self.commlog),
            "com": {"net": c.get("com_net", ""), "fp": c.get("com_fp", ""), "freq": c.get("com_freq", ""),
                    "unit": c.get("com_unit", ""), "has_key": bool(c.get("com_key")),
                    "crypto": secmod.crypto_available() if secmod else "not in this edition", "active": bool(self.running and self.commercial)},
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


NOTICES_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tradewind - licences</title><style>body{margin:0;background:#0b0f14;color:#e5e7eb;font:15px/1.5 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:20px 16px}h1{font-size:20px}h2{font-size:16px;margin:22px 0 4px}a{color:#60a5fa}td{padding:4px 10px 4px 0;vertical-align:top}</style></head>
<body><main><h1>Tradewind licences and credits</h1>
<p>Tradewind &copy; 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB). Released under the MIT License: free to use, copy, change and share,
provided this notice is kept. The software is provided "as is", without warranty of any kind.</p>
<!--TW:COM-NOTICE-P-->
<h2>Software Tradewind is built with</h2><table>
<tr><td>Python</td><td>Python Software Foundation License</td></tr>
<tr><td>NumPy</td><td>BSD 3-Clause</td></tr>
<tr><td>python-sounddevice / PortAudio</td><td>MIT</td></tr>
<tr><td>pySerial</td><td>BSD 3-Clause</td></tr>
<!--TW:COM-NOTICE-->
<tr><td>certifi</td><td>Mozilla Public License 2.0</td></tr>
<tr><td>Leaflet (map)</td><td>BSD 2-Clause</td></tr>
<tr><td>PyInstaller (Windows build)</td><td>GPL 2.0 with bootloader exception</td></tr>
<tr><td>python-for-android / Buildozer (Android build)</td><td>MIT</td></tr>
<tr><td>Barlow Condensed, Share Tech Mono fonts</td><td>SIL Open Font License 1.1</td></tr></table>
<h2>Data and services</h2><table>
<tr><td>Spots</td><td>PSKReporter (Philip Gladstone, N1DQ) - used within its query limits</td></tr>
<tr><td>Map tiles</td><td>&copy; OpenStreetMap contributors (ODbL); Esri base maps</td></tr>
<tr><td>APRS-IS</td><td>The APRS-IS network and its server operators</td></tr>
<tr><td>EMAIL-2</td><td>The APRS EMAIL-2 service (javAPRSSrvr)</td></tr>
<tr><td>APRS</td><td>APRS is a registered trademark of the late Bob Bruninga, WB4APR</td></tr></table>
<p>Full licence texts are available from each project's website. Tradewind's source: <a href="https://github.com/BajanBob/APRS-Tradewins">github.com/BajanBob/APRS-Tradewins</a></p>
</main></body></html>"""

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

    def _host_ok(self):
        """DNS-rebinding guard: a web page on another site that points its name at this computer
        sends its own name as Host - refuse it. Allowed: localhost, IP addresses, *.local, this computer's name."""
        import ipaddress
        import socket
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            name = host[1:host.find("]")] if "]" in host else host
        else:
            name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        if not name:
            return False
        if name in ("localhost", "localhost.") or name.endswith((".local", ".localhost")):
            return True
        try:
            ipaddress.ip_address(name)
            return True
        except ValueError:
            pass
        me = socket.gethostname().lower()
        extra = [h.strip().lower() for h in str(STATION.cfg.get("allowed_hosts") or "").split(",") if h.strip()]
        return name in (me, me.split(".")[0]) or name in extra

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
        if not self._host_ok():
            return self._json({"error": "unknown host name refused - use the IP address, or add the name under MENU > STATION"}, 403)
        path = self.path.split("?")[0]
        if path == "/notices":
            return self._html(NOTICES_PAGE.replace("<!--TW:COM-NOTICE-->", comui.NOTICE_ROW if comui else "")
                              .replace("<!--TW:COM-NOTICE-P-->", comui.NOTICE_P if comui else ""))
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
            if comui:
                body = comui.inject(body.decode("utf-8")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            q = self.path.partition("since=")[2]
            self._json(STATION.state(int(q) if q.isdigit() else 0))
        elif path == "/api/devices":
            audio, aerr = tncmod.list_audio_devices(rescan="rescan=1" in self.path)
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
        elif path == "/api/hf":
            self._json(STATION.hf_state())
        elif path == "/api/freqs":
            self._json([{"value": v, "label": l} for v, l in tncmod.APRS_FREQS])
        elif path == "/api/rx":
            self._json({"items": satrx.gallery(RX_DIR) if os.path.isdir(RX_DIR) else []})
        elif path == "/api/rx/file":
            from urllib.parse import parse_qs, urlsplit
            rel = (parse_qs(urlsplit(self.path).query).get("p") or [""])[0]
            root = os.path.realpath(RX_DIR)
            full = os.path.realpath(os.path.join(root, rel))
            ctype = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "mp4": "video/mp4",
                     "txt": "text/plain; charset=utf-8", "wav": "audio/wav",
                     "kss": "application/octet-stream"}.get(full.rsplit(".", 1)[-1].lower())
            if not rel or not full.startswith(root + os.sep) or not ctype or not os.path.isfile(full):
                return self._json({"error": "not found"}, 404)
            with open(full, "rb") as f:
                return self._bytes(f.read(), ctype, "private, max-age=3600")
        elif path == "/api/dx":
            self._json(STATION.dx_evidence())
        elif path == "/api/fieldtest":
            self._json(STATION.field_report())
        elif path == "/api/zones":
            self._json([{"key": k, "name": z["name"], "aprs": z["aprs"], "qsy": z["qsy"], "note": z["note"]}
                        for k, z in tncmod.ZONES.items()])
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._host_ok():
            return self._json({"error": "unknown host name refused"}, 403)
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
                    how = STATION.wolf_ping(str(b.get("call") or ""), str(b.get("text") or ""))
                    return self._json({"ok": True, "how": how})
                elif b.get("action") == "auto":
                    with STATION.lock:
                        STATION.cfg["wolf_auto"] = bool(b.get("on"))
                        save_config(STATION.cfg)
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
            if not comstation:
                return self._json({"ok": False, "errors": ["Commercial / EmComm mode is part of Tradewind Commander."]})
            b = self._body()
            try:
                fp = STATION.set_com_key(str(b.get("net") or ""), str(b.get("passphrase") or ""))
            except ValueError as e:
                return self._json({"ok": False, "errors": [str(e)]})
            self._json({"ok": True, "fp": fp})
        elif path == "/api/hf":
            b = self._body()
            try:
                msg = STATION.hf_action(b)
            except (ValueError, IOError) as e:
                return self._json({"ok": False, "errors": [str(e)]})
            return self._json({"ok": True, "msg": msg, "hf": STATION.hf_state()})
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


if comstation:                         # Commander: add the commercial station methods
    comstation.install(sys.modules[__name__])


if __name__ == "__main__":
    main()
