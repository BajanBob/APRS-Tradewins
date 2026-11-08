# Tradewind / Tradewind Commander roadmap - towards 2040

**Goal:** the Swiss Army knife of radio for hams and licensed commercial operators: **reception, analysis,
prediction, redundancy and transmission**. What a station may transmit is set by its licence and location
(grid square).

Written November 2026 from published research (sources at the end). Each item says what we would build, why,
and how we will know it works. Honest ground rules:

- **Physics first.** No feature claims more than the channel allows. Every new receiver is measured against
  the current one in the same simulated and recorded signals, and only replaces it where it is better.
- **Amateur traffic stays clear text and documented.** Gale and every Tradewind mode are published so anyone
  can decode them. Encryption lives only in the separate Commander package, for licensed non-amateur services.
- **Nothing designed to hide transmissions.** Spread spectrum and weak-signal modes are for robustness and
  sharing a channel. They always identify with a callsign.

---

## 1. Prediction - "which channel, which mode, right now and in 3 hours"

| Build | Research it draws on | Done when |
|---|---|---|
| **Propagation forecaster**: predict MUF / best band per path from the station's own link-quality history (ALE soundings, Gale beacons), PSKReporter / WSPR spots and solar indices | China: deep-learning MUF and foF2 forecasts that beat the IRI model (time-series decomposition; BiLSTM-attention: about half the IRI error); statistical model using Lyman-alpha, month and UT for M(3000)F2. DARPA **Ouija**: near-real-time HF prediction from in-situ data. UK: HF propagation from WSPR amateur data (greyline clustering) | Forecast error on a held-out month of our own LQA data is smaller than the plain IRI-based estimate |
| **Live ionosphere inputs**: optional download of ionosonde / space-weather data | Canada: **SWINCan** (formerly CHAIN), near-real-time ionosondes and GNSS monitors, expanding to 21 ionosondes and 131 GNSS monitors | Data fetched, cached and shown; forecaster uses it when online, works without it |
| **Path diagnosis from soundings**: turn our ALE / Gale soundings into an estimate of what the path supports | Russia: oblique and backscatter chirp sounding (ISTP SB RAS, Irkutsk) for real-time MUF forecasts and channel diagnosis | LQA view shows a per-band "open / marginal / closed" estimate that matches on-air results |

## 2. Reception - better decoding in fading and noise

| Build | Research it draws on | Done when |
|---|---|---|
| **Neural helper receiver** for Robust and ALE: a small convolutional network gives soft bits to the existing decoders, running beside the classic receiver | Nokia Bell Labs **DeepRx**; Korea **ETRI** neural receiver (MWC 2026); Lingjia Liu, Lizhong Zheng et al. (2026) universal neural receiver set from domain knowledge with little training | At least 1 dB better at the same error rate on a standard HF fading channel model and on our recordings; never worse (automatic fallback) |
| **Neural-assisted error correction** for Gale and Robust payloads | Israel, Tel Aviv University (Nachmani, Be'ery, Burshtein): weighted / neural belief-propagation decoders, about 0.9-1.1 dB gains on short codes | Measured gain on our code lengths; same frame format on air, so older Tradewind versions still interoperate |
| **SDR "hear everything" mode**: one SDR decodes every ALE / selcall / APRS channel at once - no scanning | Standard ALE practice plus SDR front ends | Calls on any listed channel decoded within one frame time |
| **Interference-tough front-end settings** guidance for hardware partners | MIT low-power harmonic-rejection receiver (about 30x more harmonic interference rejected, under 1 mW) | Hardware note for a future Commander radio interface |

## 3. Transmission - faster where the band allows, robust where it does not

| Build | Research / rule | Done when |
|---|---|---|
| **Gale-2 / Gale-4 data modes**: adaptive multi-carrier data within 2.8 kHz, choosing speed from live LQA | US rule change (Jan 2024): HF symbol-rate cap replaced by 2.8 kHz bandwidth | Documented, published, interoperable between Tradewind stations; speed steps chosen automatically |
| **Learned waveforms (DeepTx style)** - research only | Nokia Bell Labs / DOCOMO / NTT learned air interface (up to 30 % throughput) | Only if the trained waveform can be fully published as a fixed specification |
| **Spread spectrum, 70 cm and up only (US: 222 MHz and up, 10 W)**: an openly documented SS data mode for shared channels | FCC Part 97.305 / 97.311; must not obscure meaning | Licence profile allows it for the operator's location; callsign ID in clear |
| **Semantic / meaning-first messages** for emergencies (forms, positions, status codes) | Korea (ETRI, KT with Bell Labs) semantic communication | Standard emergency forms sent as compact fields instead of free text |

## 4. Redundancy - always a way through

| Build | Research it draws on | Done when |
|---|---|---|
| **Gateway between radio systems**: APRS, Gale, CCIR selcall, ALE, APRS-IS and email, with automatic failover | DARPA **DyNAMO** (linking incompatible tactical networks) | A message entered once is delivered by whichever path works, with the route logged |
| **HF mesh with store-and-forward** using LQA to choose relays | ALE network practice | Messages reach stations with no direct path through a relaying Tradewind station |
| **Multi-link bonding**: second radio, KISS TNCs, internet | existing Tradewind link controller | Automatic choice per message, shown to the operator |

## 5. Analysis - see the spectrum and the path

- Waterfall + decoded overlay for every supported mode; selcall / ALE / SELCAL recordings (started in v3.21).
- Station "path cards": best hours and bands to each regular contact, learned from history.
- Exportable reports for EmComm exercises (who heard whom, when, on what).

## 6. Licence and location profiles ("based on grid square")

Tradewind already chooses APRS frequencies from the grid square. Extend that into a **licence profile**:

- Country / ITU region from the grid square and callsign prefix; the operator confirms it.
- The profile sets **where Tradewind may transmit**: amateur bands and modes for that country (e.g. 60 m
  channels, spread spectrum bands, selcall), and - in Commander only - commercial channels the user is licensed for
  (entered with licence number and expiry).
- Everything outside the profile is **receive-only**, as aviation SELCAL is today.
- The rule tables are open files, so national societies can correct them.

## 7. Commander as a product (EmComm and commercial)

- **One console for mixed fleets**: talks to Barrett / Codan / Icom radios with standard CCIR selcall (Barrett INT)
  and ALE, plus Tradewind's Gale between Tradewind stations.
- **Prediction + redundancy** are the differentiators for NGOs, disaster agencies and island nations.
- Licensed commercial use on type-approved radios and licensed frequencies; encryption only there.
- Keep the amateur Tradewind free and open (MIT); Commander adds the licensed-service features.

## Suggested order

1. Propagation forecaster from our own LQA + PSKReporter (no new hardware, immediate value).
2. Neural helper receiver for Robust, measured in simulation and on recordings.
3. Licence / location profiles (needed before any new transmit mode).
4. Gateway / failover between APRS, Gale, selcall, ALE and internet.
5. Gale-2 / Gale-4 data, then spread spectrum on 70 cm where licensed.

## Sources

- Nokia Bell Labs, DeepRx: https://arxiv.org/abs/2005.01494
- Nokia / DOCOMO / NTT AI-native air interface: https://www.rcrwireless.com/?p=386284
- AI-native 6G at MWC 2026 (Ericsson, Intel, Qualcomm, ZTE): https://techblog.comsoc.org/2026/03/02/ericsson-and-intel-collaborate-to-accelerate-ai-native-6g-related-ai-native-6g-advancements-at-mwc-2026
- Universal neural receiver (Liu, Zheng, Yi, Calderbank, 2026): https://arxiv.org/pdf/2602.15458
- MIT low-power interference-resilient receiver: https://www.sciencedaily.com/releases/2025/06/250620064909.htm
- DARPA Ouija (HF propagation prediction): https://www.executivegov.com/?p=233105
- DARPA DyNAMO: https://www.defencetalk.com/darpa-successfully-demonstrates-transitions-advanced-rf-networking-program-76611
- ETRI neural receiver: https://www.eurekalert.org/news-releases/1111380
- KT / Bell Labs semantic communication: https://www.etoday.co.kr/news/view/2483188
- China, short-term MUF prediction (time-series decomposition): https://bohrium.dp.tech/paper/arxiv/65792be9939a5f4082fa37de
- China, foF2 deep-learning forecast: https://cjss.ac.cn/en/article/doi/10.11728/cjss2025-0073
- China, M(3000)F2 statistical ML: https://cjss.ac.cn/en/article/doi/10.11728/cjss2026-0011
- Russia, chirp sounding real-time MUF forecast: https://zh-szf.ru/en/nauka/article/23013/view
- Russia, backscatter chirp channel diagnosis: https://naukaru.ru/en/nauka/article/22243/view
- UK, WSPR amateur data for HF propagation research: https://core.ac.uk/download/519702535.pdf
- Canada, SWINCan / CHAIN: https://www.ursi.org/proceedings/procAP25/papers/0975.pdf
- Israel, neural decoding of linear codes: https://arxiv.org/pdf/1706.07043
- FCC HF 2.8 kHz bandwidth rule (Jan 2024): https://www.worldwidedx.com/threads/bandwidth-limits-replace-symbol-rates-on-the-hf-bands-other-bands-open-for-comment.267503/
- FCC spread spectrum rules: https://hamstudy.org/browse/E4_2024/E1F
