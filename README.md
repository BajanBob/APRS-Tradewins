# Tradewind — Caribbean 2m/70cm propagation beacon & soundcard TNC

Tradewind watches PSKReporter for 2m and 70cm digital-mode spots (FT8, FT4,
MSK144, Q65…) and announces openings between the **Caribbean** and
**North America, Europe and Latin America** as APRS bulletins, on RF and/or
APRS-IS. It has its own AFSK 1200 sound modem, so a PC soundcard or a radio's
built-in USB sound card works as the TNC. You don't need Direwolf or
hardware, though both are supported.

![Tradewind face plate](screenshot.png)

## Download

Go to the **Releases** page (right-hand side of this repository) and download:

| Device | File | What works |
|---|---|---|
| Windows PC | `Tradewind.exe` | Everything: sound modem, USB radios (IC-9100, IC-705, FT-991A…), interfaces, KISS, APRS-IS, iGate |
| Android phone | `Tradewind.apk` | Propagation alerts over APRS-IS, or over Wi-Fi to a networked TNC (Direwolf / UZ7HO via KISS TCP) |

An `.exe` only runs on Windows, and Android uses `.apk` files. Both are built automatically from this code.

**Windows**
1. Double-click `Tradewind.exe`. A console window opens and your browser shows the face plate.
2. If SmartScreen warns about an unknown publisher, click **More info → Run anyway**. The .exe isn't code-signed.
3. Closing the console window stops Tradewind.

**Android**
1. Download `Tradewind.apk` on the phone and tap it.
2. Allow **Install unknown apps** for your browser or file manager when asked.
3. Play Protect may warn about an unknown developer. Tap **Install anyway**.
4. Keep the app open while you want alerts sent. Android may pause apps in the background,
   so you may need to exempt Tradewind from battery optimisation.

Radios that connect by USB cable or audio (soundcard, CI-V, CAT, Digirig) need the Windows/PC version.
Android's audio and USB systems aren't reachable from this app.

### Phone as a remote control

You can also run `Tradewind.exe --host 0.0.0.0` on the station PC and open
`http://<PC's IP address>:8073` in any phone browser on the same Wi-Fi. That gives you full control of the
PC's radio from the phone. Only do this on a trusted network.

You don't need Tradewind to *receive* alerts. APRSdroid, a TH-D74/D75 or FTM-500, or aprs.fi all show the bulletins.

## Run from source (Windows, macOS, Linux)

```
pip install -r requirements.txt
python3 app.py
```
Your browser opens the face plate at http://127.0.0.1:8073.

- **Windows/macOS:** `pip install sounddevice` includes PortAudio.
- **Linux:** also install PortAudio: `sudo apt install libportaudio2`.
- **APRS-IS only or KISS TNCs:** numpy and sounddevice are only needed for the built-in sound modem.

## Building the .exe

**Automatic.** Every push to `main` builds `Tradewind.exe` and `Tradewind.apk` on GitHub. Download them under
**Actions → latest run → Artifacts**. The first Android build takes 20–40 minutes; later ones are cached.
To publish a version for everyone:

1. Go to **Releases → Draft a new release**.
2. Create a new tag such as `v1.0` and publish.
3. Wait for both builds. They attach `Tradewind.exe` and `Tradewind.apk` to the release.

**On your own PC.** Install Python from python.org, then double-click `build_exe.bat`.
The result is `dist\Tradewind.exe`.

## First run

1. Type your **callsign-SSID** (e.g. `8P6XX-10`) and **grid square** (e.g. `GK03fd`).
2. Pick your **radio / interface**. A note under it shows the radio menu settings to make.
3. Press **MENU**, then **RADIO / TNC**, and choose the audio in/out devices and PTT port.
4. Press **SAVE**, then the **power button**.

To reach the face plate from a phone on the same Wi-Fi, run `python3 app.py --host 0.0.0.0`.
Only do this on a trusted network: anyone who can open the page can key your transmitter.

## TNC options

| Mode | Use for |
|---|---|
| Built-in sound modem | IC-9100, IC-705, IC-9700, IC-7100, FT-991A (built-in USB audio); FT-2980R, FT-7900R/8800R/8900R, FTM-400/300/200 and other radios via Digirig, SignaLink, AIOC or CM108 interfaces |
| KISS TCP | Direwolf (port 8001), UZ7HO Soundmodem (port 8100) |
| KISS serial | Kenwood TH-D74 / TH-D75 / TM-D710 built-in TNC, Mobilinkd |
| None | APRS-IS only |

These PTT methods are supported:
- VOX
- Serial RTS or DTR (Icom, Yaesu, Digirig, AIOC)
- CM108 GPIO (Linux `/dev/hidrawN`)
- Icom CI-V command (IC-9100 and other Icoms, no extra software needed)
- Yaesu CAT command `TX1;` (FT-991A and newer Yaesu radios)
- Hamlib `rigctld` for CAT PTT on any radio Hamlib supports

### Radio notes

- **Icom IC-9100.** Uses one USB cable for audio and control. Pick "USB Audio CODEC" for audio, set PTT to Icom CI-V, address `7C`, 9600 baud. On the radio, set DATA MOD to USB. Tradewind tunes the radio to the APRS frequency in FM data mode for you. Close loggers and WSJT-X first, because only one program can use the CI-V port.
- **Yaesu FT-2980R.** Has no data jack and no CAT port. Connect through the mic jack and the external speaker jack with a SignaLink (VOX) or a Digirig with a Yaesu mic cable (Serial RTS). Check the cable's mic-jack pinout against the radio manual. Keep TX audio low, and use Low or Mid power for long sessions.
- **Yaesu FT-7900R / FT-8800R / FT-8900R / FTM-400 / FTM-300 / FTM-200.** Use the rear DATA jack at 1200 bps with a soundcard interface cable for that model.

### Automatic tuning (CAT)

Tick **Tune the radio to the APRS frequency** (MENU → RADIO / TNC) and pick a frequency:
144.390, 144.800, 144.930, 145.570, 145.825 (ISS digipeater) or your own.
When the station turns on, Tradewind sets the radio to that frequency in FM data mode (FM-D / DATA-FM).
When it turns off, it puts the radio back on the frequency and mode it was using before.

Tuning works when the PTT method is a CAT link:
- **Icom CI-V**: IC-9100 (address 7C), IC-705 (A4), IC-9700 (A2), IC-7100 (88)
- **Yaesu CAT**: FT-991A and newer Yaesu radios
- **Hamlib rigctld**: any radio Hamlib supports

Radios without a CAT port, such as the FT-2980R, have to be set by hand.
If you choose 145.825 for the ISS, set the RF path to `ARISS`, and only transmit while the ISS is overhead.

## Live propagation map

Below the face plate, a map draws every Caribbean 2m/70cm path that PSKReporter saw during the look-back window.
Lines are coloured by mode (TEP orange, tropo duct teal, Sporadic-E pink); 2m lines are solid and 70cm lines dashed.
Hover over or tap a line to see the stations, distance, digital mode and signal report.
The map refreshes after every check, which is every 5 minutes by default (PSKReporter's limit).
It needs an internet connection for the map tiles.

## Face plates

Use the **FACE PLATE** buttons at the top of the page to switch looks at any time. Each device remembers its own choice.
- **Classic:** the original radio-style panel.
- **Ops Console:** a desktop dashboard with a side menu, status chips, opening cards, the live map, an alert center, chat and the packet log.
- **Field:** a light, high-contrast phone layout with a big power button, an "open now" card and large Beacon / Menu / Check buttons.

All settings stay under **MENU** (or **Settings** in the Ops Console).

## Two-way chat (2m / 70cm)

You can chat station to station using standard APRS messages, with a handshake so both operators agree first:

1. Type the other station's callsign under **TWO-WAY CHAT**, choose how to send, and press **HAIL**.
   **RF only** is a real test of the propagation path.
2. Their Tradewind plays a call-out tone and shows **Accept / Decline**.
3. When they accept, you get **"accepted - press Accept to confirm"**. Press **Accept**.
4. The link opens on both sides and you can chat. **End link** closes it.

Every message is numbered and resent (after 30, 60 and 120 s) until the other station acknowledges it, and each one shows *sending*, *delivered* or *not delivered*.
A completed handshake over RF proves the path works both ways.
Operators using APRSdroid, a TH-D74/D75 or another APRS radio can take part too: they see the request as a message and reply **ACCEPT** or **DECLINE**. Plain APRS messages they send you appear in the chat, and you can reply.

## Setting TX audio

Use **MENU → STATION → BCN** to send a beacon while you adjust the TX audio level. Aim for about 3 kHz deviation, which is clean, unclipped audio on a monitor receiver. Keep RX audio at roughly 30–50% on the face plate meter.

## What gets sent

- **Bulletins.** `BLN1VHF`–`BLN6VHF` carry one line each for 2m and 70cm × NA/EU/LA, e.g. `2m OPEN CAR-NA 2 paths best 8P6XX>K4ABC 2582km FT8`. Each is repeated every 30 minutes while the opening lasts. `BLN9` is the TEST button.
- **Position beacon.** The centre of your grid square, every 30 minutes (optional).
- **Optional RX iGate.** Packets heard on RF are gated to APRS-IS.

## Propagation modes

Tradewind labels every Caribbean opening with its likely mode and lets you choose which modes to watch,
which ones switch RF on, and which ones sound an alert (MENU → PROPAGATION and MENU → RADIO / TNC).

| Mode | Shown as | How it's recognised |
|---|---|---|
| **TEP** (trans-equatorial) | `TEP` | Path to South America that crosses the equator (far station south of 5° S) |
| **Tropospheric ducting** | `DUCT` | 70cm, paths longer than 2,300 km, trans-Atlantic paths (Europe, Canaries, Madeira, Azores, Cape Verde), or outside Es season |
| **Sporadic-E** | `Es` | 2m, 800–2,300 km, May–Aug and Dec–Jan, daytime and evening |

Bulletins include the mode, e.g. `2m TEP OPEN CAR-LA 1 path best 9Y4ZZ>PY7AA 3641km FT8`.
The labels are best guesses from path geometry, band and season; they can't measure the mode directly.

## Weather bulletins (optional)

Tradewind can send scheduled APRS weather bulletins (MENU → WEATHER). Defaults for Barbados:

| Bulletin | Default times (local) | Sent as | Source |
|---|---|---|---|
| General forecast | 08:00 | `BLN1WX`… | Open-Meteo |
| Marine (seas, swell, wind, small-craft caution) | 05:30, 12:00 | `BLN1MAR`… | Open-Meteo Marine |
| Aviation: TBPB METAR + TAF, Piarco FIR (TTZP) SIGMETs | 06:30, 18:00 | `BLN1AVN`… | aviationweather.gov |

**Tropical cyclone alerts** (on by default): every 20 minutes Tradewind checks the National Hurricane
Center. When an Atlantic system is within 1,500 km of Barbados (adjustable), or an NHC advisory puts
Barbados under a hurricane or tropical storm watch/warning, it sends `BLN1TC`… bulletins with each new
advisory, e.g. `HU BERYL adv 010: 12.1N 57.6W 115kt 961mb mov WNW 20mph` /
`Hurricane BERYL is 244km ESE of Barbados` / `HURRICANE WARNING IN EFFECT FOR BARBADOS`.
The face plate chimes and speaks the alert.

These are automated, unofficial summaries. For official forecasts and warnings use the
Barbados Meteorological Services and the Department of Emergency Management.
Grantley Adams (TBPB) lies within the Piarco FIR (TTZP).

## Tuning

**Default rule:** a band counts as open when there are at least 2 distinct Caribbean paths of 800 km or more, or one path of 1500 km or more. Both thresholds are in **MENU → PROPAGATION**.

**Query limit:** PSKReporter asks for no more than one query every 5 minutes, and Tradewind enforces that.

## Testing offline

```
python3 app.py --demo saved_pskreporter.xml
```
Save a PSKReporter response from `https://retrieve.pskreporter.info/query?frange=144000000-450000000&flowStartSeconds=-3600&rronly=1`.

## Files

| File | What it is |
|---|---|
| `app.py` | Station logic and web server |
| `faceplate.html` | The face plate UI |
| `afsk.py` | AFSK 1200 modem and AX.25 framing |
| `tnc.py` | Sound modem, KISS TCP/serial, PTT drivers, APRS-IS client |
| `vhf_prop_aprs.py` | PSKReporter fetch and opening detection; also runs alone from the command line |

Settings are stored in `~/.tradewind/config.json`.

Transmitting requires an amateur licence. Use your own callsign.

## Credits & licence

Tradewind was created by **Brandon St Rose (8P5DG)** and **Kammie Holder (8P5AB)**, Barbados.

It's released under the [MIT License](LICENSE). You're free to use, share and modify it, including
in your own projects, as long as you keep the copyright notice and licence text. Spot data comes from
[PSKReporter](https://pskreporter.info). Transmitting requires an amateur licence; use your own callsign.
