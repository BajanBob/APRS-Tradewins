# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""Package the two editions for live testing.

    python tools/build_editions.py                  Tradewind zips (and Commander zips if commander/ is here)
    python tools/build_editions.py --ham-only       never include Commander
    python tools/build_editions.py --exe dist/Tradewind.exe --apk Tradewind.apk
                                                    also wrap CI-built binaries into the Windows / Android zips

Output in release/:
    Tradewind-<v>-linux.zip         source + install_linux.sh       (Debian / Ubuntu / Mint)
    Tradewind-<v>-raspberrypi.zip   source + install_linux.sh       (Raspberry Pi OS, headless service option)
    Tradewind-<v>-windows.zip       Tradewind.exe                   (needs --exe)
    Tradewind-<v>-android.zip       Tradewind.apk                   (needs --apk)
    TradewindCommander-<v>-linux.zip / -raspberrypi.zip            ready to install
    TradewindCommander-<v>-windows-kit.zip                          source + commander\\build_commander.bat
    TradewindCommander-<v>-android-kit.zip                          source + commander/android/build_apk.sh

The Tradewind (amateur) packages never contain the commander/ folder or any encryption code:
the script checks every file it packs.
"""
import argparse
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = ["app.py", "afsk.py", "digi.py", "dxmode.py", "fx25.py", "hfcall.py", "iss.py", "satrx.py", "tnc.py",
        "vhf_prop_aprs.py", "wx.py", "faceplate.html", "icon.png", "icon.ico", "requirements.txt",
        "test_modem.py", "install_linux.sh", "README.md", "LICENSE"]
ANDROID = ["android_main.py", "buildozer.spec"]
CRYPTO_MARKS = (b"AESGCM", b"from cryptography", b"import cryptography")


def version():
    with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as f:
        return re.search(r'^APP_VERSION = "([^"]+)"', f.read(), re.M).group(1)


FIRST = {
    "linux": """{name} {v} for Linux (Debian, Ubuntu, Mint)

1. Unzip, open a terminal in this folder and run:   bash install_linux.sh
   (add --sdr for RTL-SDR tools)
2. Log out and back in once (radio / USB permissions), then run:   {cmd}
3. Your browser opens the face plate at http://127.0.0.1:8073
Check Satellite Watch any time from a terminal:   .venv/bin/python iss.py GK03ec
""",
    "raspberrypi": """{name} {v} for Raspberry Pi (Raspberry Pi OS Bookworm, 64-bit recommended)

1. Copy this folder to the Pi, open a terminal in it and run:
       bash install_linux.sh --service        start at power-on, reachable from phones on your network
   or  bash install_linux.sh                  run it yourself with:  {cmd}
   (add --sdr for RTL-SDR tools)
2. Open http://<pi-address>:8073 from a phone or PC. Set a station PIN first (MENU > STATION).
3. A USB sound card / Digirig plus a CAT cable gives the full station (sound modem, Satellite Switch).
Check Satellite Watch from SSH:   .venv/bin/python iss.py GK03ec --min-el 10
""",
    "windows": """{name} {v} for Windows 10 / 11

Double-click {exe}. A console window opens and your browser shows the face plate.
If SmartScreen warns about an unknown publisher: More info > Run anyway (the .exe is not code-signed).
Closing the console window stops it.
""",
    "android": """{name} {v} for Android 7 and later

Copy {apk} to the phone and tap it; allow "Install unknown apps" when asked.
Android works over APRS-IS or Wi-Fi to a KISS TCP modem (Direwolf / UZ7HO); USB and sound-card radios need the PC version.
""",
    "windows-kit": """{name} {v} - Windows build kit

Tradewind Commander is not built on GitHub (its code is kept private), so build the .exe on your own PC:
1. Install Python 3.10+ from python.org (tick "Add python.exe to PATH").
2. Double-click  commander\\build_commander.bat   - it installs the packages, runs the self-test and builds
   dist\\TradewindCommander.exe
Or run from source:  pip install -r requirements.txt -r commander\\requirements.txt  then  python app.py
""",
    "android-kit": """{name} {v} - Android build kit

Build TradewindCommander.apk on a Linux PC (or WSL2) with Docker:
    bash commander/android/build_apk.sh
It uses the official Kivy Buildozer container, like the Tradewind build; the first run takes 20-40 minutes.
The Commander APK adds python-for-android's cryptography recipe for encrypted chat. Test it on a spare
phone first: this recipe has not been built for Tradewind before.
""",
}


def _add(z, src, arc, ham):
    with open(src, "rb") as f:
        data = f.read()
    if ham and not arc.endswith("test_modem.py") and any(m in data for m in CRYPTO_MARKS):
        raise SystemExit(f"refusing to pack {arc}: encryption code in the amateur Tradewind package")
    info = zipfile.ZipInfo(arc, date_time=(2026, 11, 6, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (0o755 if arc.endswith((".sh", ".bat")) else 0o644) << 16
    z.writestr(info, data)


def _commander_files():
    base = os.path.join(ROOT, "commander")
    for d, dirs, files in os.walk(base):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        for f in files:
            if not f.endswith(".pyc"):
                p = os.path.join(d, f)
                yield p, os.path.relpath(p, ROOT).replace(os.sep, "/")


def package(edition, plat, out, v, exe=None, apk=None):
    ham = edition == "ham"
    name = "Tradewind" if ham else "Tradewind Commander"
    stem = ("Tradewind" if ham else "TradewindCommander") + f"-{v}-{plat}"
    top = stem + "/"
    path = os.path.join(out, stem + ".zip")
    cmd = "tradewind" if ham else "tradewind-commander"
    with zipfile.ZipFile(path, "w") as z:
        text = FIRST[plat].format(name=name, v=v, cmd=cmd, exe=os.path.basename(exe or ""), apk=os.path.basename(apk or ""))
        z.writestr(top + "READ-ME-FIRST.txt", text.replace("\n", "\r\n") if plat.startswith("windows") else text)
        if plat == "windows":
            _add(z, exe, top + os.path.basename(exe), ham)
            _add(z, os.path.join(ROOT, "LICENSE"), top + "LICENSE.txt", ham)
            _add(z, os.path.join(ROOT, "README.md"), top + "README.md", ham)
        elif plat == "android":
            _add(z, apk, top + os.path.basename(apk), ham)
            _add(z, os.path.join(ROOT, "LICENSE"), top + "LICENSE.txt", ham)
        else:
            for f in CORE + (ANDROID if plat == "android-kit" else []) + (["build_exe.bat"] if ham else []):
                _add(z, os.path.join(ROOT, f), top + f, ham)
            if not ham:
                for p, arc in _commander_files():
                    _add(z, p, top + arc, ham)
    if ham:
        with zipfile.ZipFile(path) as z:
            assert not [n for n in z.namelist() if "/commander/" in n], "commander/ inside a Tradewind package"
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(ROOT, "release"))
    ap.add_argument("--exe", help="CI-built Tradewind.exe to wrap")
    ap.add_argument("--apk", help="CI-built Tradewind.apk to wrap")
    ap.add_argument("--ham-only", action="store_true")
    a = ap.parse_args(argv)
    v = version()
    os.makedirs(a.out, exist_ok=True)
    made = [package("ham", p, a.out, v) for p in ("linux", "raspberrypi")]
    if a.exe:
        made.append(package("ham", "windows", a.out, v, exe=a.exe))
    if a.apk:
        made.append(package("ham", "android", a.out, v, apk=a.apk))
    if not a.ham_only and os.path.isfile(os.path.join(ROOT, "commander", "__init__.py")):
        made += [package("commander", p, a.out, v) for p in ("linux", "raspberrypi", "windows-kit", "android-kit")]
    for m in made:
        print(f"{os.path.getsize(m) / 1e6:6.2f} MB  {os.path.relpath(m, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
