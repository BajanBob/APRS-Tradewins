# HF Calling - selcall and ALE in Tradewind (v3.18)

Tradewind can now hail and be hailed on HF the way commercial HF radios do (Icom, Barrett, Codan, Harris),
using only the radio's audio and an ordinary SSB rig in USB. Open **HF CALLING - SELCALL & ALE** on the face plate.

## What is in it

| Feature | What it does | Works with |
|---|---|---|
| **Selcall** (CCIR-493 style, 100 baud FSK 1615/1785 Hz) | Call a 4- or 6-digit ID; the called station rings | Tradewind to Tradewind |
| **Beacon call + revertive** | Test the path: the far station answers with 4 tones and a signal report, never logged as a call | Tradewind to Tradewind |
| **Page call** | Short text (up to 60 characters) | Tradewind to Tradewind |
| **GPS call** | Sends your position from your grid square | Tradewind to Tradewind |
| **Emergency call** | Priority call with your position, loud alert at the far end | Tradewind to Tradewind |
| **Telcall** | Sends a phone number for a phone-patch operator. Signal only - Tradewind does not dial | Tradewind to Tradewind |
| **2G ALE** (MIL-STD-188-141, 8-FSK 125 baud, Golay FEC) | Scanning call, 3-way handshake, LINKED; soundings; SNR exchange | Other amateur ALE stations (HFN), PC-ALE style - try it on air first |
| **ARINC SELCAL** | Shows aircraft SELCAL codes heard on the channel | Receive only |
| **Up to 10 self IDs** | 10 selcall IDs and 10 ALE addresses | |
| **Scanning** | Steps through your channels, stops when a call or ALE signal appears | CAT radios |
| **Link quality (LQA)** | Records SNR both ways per station and channel, and says which data mode to use | |
| **USE THIS LINK** | Moves APRS chat onto the linked channel with the Robust modem | |

All calls are clear text and carry your callsign. Tradewind listens anywhere on HF, but transmits only inside the amateur
HF bands; your licence and band plan decide the rest. ALL-CALL IDs (9999 / 999999) are never auto-answered.

## How the modem uses it

The weak point of HF is not the data modem, it is *finding a channel that works right now*. HF Calling fixes that first:

1. **Beacon call or ALE sounding** measures the path on each channel (SNR here, SNR there).
2. Tradewind recommends the data mode from the worse of the two numbers:
   - above +10 dB: 300 baud packet;
   - -7 to +10 dB: Robust (44 bit/s, works to about -10 dB);
   - -13 to -7 dB: Robust DX (22 bit/s, about -14 dB);
   - below that: wait for a better channel.
3. **USE THIS LINK** retunes the APRS modem to that channel and mode, so a message goes out where it will be heard.

Measured in the self-test (2.5 kHz noise bandwidth): ALE decodes to about -6 dB SNR, selcall reliably to about -3 dB,
and the SNR estimates are within about 1 dB - good enough to choose a channel and mode.

## Honest limits

- Selcall is the CCIR-493 *style*, not a copy of any maker's private format. Codan, Barrett and other OEM selcall,
  Barrett Secure Call and any encrypted call are **not** included and will not be: Tradewind is clear text only.
- ALE follows the published 2G standard. Real radios differ in small details (bit interleaving); Tradewind tries the
  variants and saves the one that works the first time it hears a real sounding. Please test with an HFN station.
- SELCAL is receive only. Telcall carries a number; it does not connect a phone.
- Scanning needs CAT control. Without it, set the dial by hand and pick the channel in the panel.
- While HF Calling is scanning (or the main modem is a VHF modem), APRS beacons on the main radio pause. APRS-IS and a
  second radio link keep working.

## Command line

```
python hfcall.py --selftest        # selcall, ALE, SELCAL, two-station handshake, all in simulated noise
```

## Looking ahead to 2040

What the next generation of HF calling can be, roughly in the order it becomes practical:

1. **Scanless ALE with an SDR.** One wideband SDR receiver decodes every channel at once, so nobody has to scan and
   calls are heard instantly. The decoders in `hfcall.py` already run on plain audio and can be run per channel.
2. **Wideband / 4G-style deep links.** Once linked, move from 44 bit/s to kilobits per second with adaptive OFDM
   when the channel is good, and back to Robust when it fades - chosen automatically from live LQA.
3. **An ionospheric "digital twin".** Combine your own LQA history, PSKReporter / WSPR spots, solar indices and
   ionosonde data to *predict* the best channel for a station hours ahead, instead of measuring it every time.
4. **Neural receivers.** Learned demodulators that handle HF fading and interference better than matched filters,
   trained on recorded soundings - checked against the classic decoder so they can never do worse.
5. **HF mesh with store-and-forward.** Stations that can hear each other relay messages for stations that cannot,
   using LQA to pick the route, all in clear text with callsigns.
6. **Stronger error correction.** LDPC or polar codes for the payload give a few dB more than today's codes at the
   same speed.
