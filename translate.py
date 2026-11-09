# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""Chat translation for Tradewind (v3.23).

English stays the on-air language for global communication:
  - outgoing: a chat typed in another language is translated to English before it is sent;
  - incoming: a chat not in the operator's language is translated before it is shown.
The original text is always kept and shown beside the translation: machine translation makes
mistakes, and in an emergency the operator must be able to see exactly what was sent and received.

Engines (MENU > STATION > Language & translation):
  off            no translation
  mymemory       MyMemory public API: internet, no account (about 5,000 characters a day;
                 50,000 with an email address)
  libretranslate LibreTranslate: open source; your own server on the LAN keeps working when the
                 internet is down (e.g. on a Raspberry Pi at the EOC), or a hosted one with an API key
  deepl          DeepL API (free or pro key): best quality, internet
  argos          Argos Translate: fully offline on this computer (pip install argostranslate,
                 then install the language packages)

Callsigns, Q-codes, grid squares, numbers and frequencies are protected so no engine changes them.
This module has no encryption and sends nothing on the air itself.
"""
import json
import re
import threading
import urllib.parse
import urllib.request

ENGINES = ("off", "mymemory", "libretranslate", "deepl", "argos")
LANGS = {"en": "English", "es": "Español", "pt": "Português", "fr": "Français", "de": "Deutsch",
         "ru": "Русский", "pl": "Polski"}
TIMEOUT = 8

# ---- language detection (offline: common short words) ---------------------------------------
_WORDS = {
    "en": "the and you are is to of in for on with it this that have will not be at we my your all "
          "from can what there please thanks thank good here going road water need help".split(),
    "es": "el la los las de que y en un una es por para con no se su al lo como más pero sus le ya "
          "o este sí porque esta entre cuando muy sin sobre también me hasta hay donde quien desde "
          "nos usted gracias bien estoy necesito ayuda agua camino está están".split(),
    "pt": "o a os as de que e do da em um uma para com não se na no por mais dos das como mas ao "
          "ele ela à seu sua ou quando muito nos já está eu também só pelo pela até isso você "
          "obrigado obrigada preciso ajuda água estrada estou tem são".split(),
    "fr": "le la les de des et un une est pour pas que qui dans en sur au avec il elle nous vous "
          "je merci bien besoin aide eau route sont ce".split(),
    "de": "der die das und ist nicht ein eine zu den von mit sich des auf für im dem auch es an "
          "ich wir sie danke bitte hilfe wasser straße".split(),
}
_SETS = {k: set(v) for k, v in _WORDS.items()}
_HINT = {"es": "ñ¿¡", "pt": "ãõç", "de": "äöüß", "fr": "èêëàâîôûç"}


def detect(text):
    """Best guess of the language: 'en', 'es', 'pt', 'fr', 'de', or '' when unsure."""
    words = re.findall(r"[a-záéíóúñãõâêôàçèëîïûüäöß']+", text.lower())
    if len(words) < 2:
        return ""
    score = {k: sum(w in s for w in words) for k, s in _SETS.items()}
    for k, chars in _HINT.items():
        if any(c in text.lower() for c in chars):
            score[k] += 2
    best = max(score, key=score.get)
    ranked = sorted(score.values(), reverse=True)
    if ranked[0] < 2 or ranked[0] == ranked[1]:
        return ""
    return best


# ---- protect what must not change ---------------------------------------------------------------
_PROTECT = re.compile(
    r"\b[A-Z0-9]{1,3}[0-9][A-Z]{1,4}(?:-[0-9]{1,2})?\b"       # callsigns 8P6XX-9, PY2ABC, LU1XX
    r"|\bQ[A-Z]{2}\??"                                        # Q-codes QSL QTH QRZ?
    r"|\b[A-R]{2}[0-9]{2}(?:[a-x]{2})?\b"                     # grid squares GK03fd
    r"|\b\d+(?:[.,:]\d+)*\s?(?:MHz|kHz|km|mi|dB|W|m|UTC|Z)?\b"  # numbers, frequencies, times
    r"|\b(?:73|88|SITREP|ICS-?\d+|APRS|RF|NOGATE)\b")


def protect(text):
    keep = []

    def sub(m):
        keep.append(m.group(0))
        return f"[{len(keep) - 1}]"
    return _PROTECT.sub(sub, text), keep


def restore(text, keep):
    def sub(m):
        i = int(m.group(1))
        return keep[i] if i < len(keep) else m.group(0)
    return re.sub(r"\[\s*(\d+)\s*\]", sub, text)


# ---- engines ---------------------------------------------------------------------------------
def _http(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=dict({"User-Agent": "Tradewind (amateur radio)"}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _mymemory(text, src, dst, cfg):
    q = {"q": text, "langpair": f"{src}|{dst}"}
    if cfg.get("tr_email"):
        q["de"] = cfg["tr_email"]
    d = _http("https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(q))
    if int(d.get("responseStatus") or 0) != 200:
        raise IOError(str(d.get("responseDetails") or "MyMemory refused the request"))
    return d["responseData"]["translatedText"]


def _libre(text, src, dst, cfg):
    url = str(cfg.get("tr_url") or "").rstrip("/")
    if not url:
        raise IOError("set the LibreTranslate server address (e.g. http://192.168.1.20:5000)")
    body = {"q": text, "source": src, "target": dst, "format": "text"}
    if cfg.get("tr_key"):
        body["api_key"] = cfg["tr_key"]
    d = _http(url + "/translate", json.dumps(body).encode(), {"Content-Type": "application/json"})
    if "translatedText" not in d:
        raise IOError(str(d.get("error") or "LibreTranslate gave no translation"))
    return d["translatedText"]


_DEEPL_TARGET = {"en": "EN-GB", "pt": "PT-BR"}


def _deepl(text, src, dst, cfg):
    key = str(cfg.get("tr_key") or "")
    if not key:
        raise IOError("enter your DeepL API key")
    host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
    body = urllib.parse.urlencode({"text": text, "source_lang": src.upper(),
                                   "target_lang": _DEEPL_TARGET.get(dst, dst.upper())}).encode()
    d = _http(f"https://{host}/v2/translate", body, {"Authorization": "DeepL-Auth-Key " + key})
    return d["translations"][0]["text"]


def _argos(text, src, dst, cfg):
    try:
        from argostranslate import translate as at          # optional, offline
    except ImportError:
        raise IOError("Argos Translate is not installed (pip install argostranslate, then add language packages)")
    return at.translate(text, src, dst)


_ENGINE = {"mymemory": _mymemory, "libretranslate": _libre, "deepl": _deepl, "argos": _argos}
_cache, _lock = {}, threading.Lock()


def translate(text, src, dst, cfg):
    """Translate text from src to dst with the engine in cfg. Returns the translation; raises IOError."""
    engine = cfg.get("tr_engine") or "off"
    if engine == "off" or src == dst or not text.strip():
        return text
    fn = _ENGINE.get(engine)
    if not fn:
        raise IOError(f"unknown translation engine {engine}")
    key = (engine, src, dst, text)
    with _lock:
        if key in _cache:
            return _cache[key]
    body, keep = protect(text)
    out = restore(fn(body, src, dst, cfg), keep).strip()
    out = re.sub(r"\s+", " ", out)
    with _lock:
        if len(_cache) > 500:
            _cache.clear()
        _cache[key] = out
    return out


def user_lang(cfg):
    lang = str(cfg.get("lang") or "auto")
    return lang if lang in LANGS else "en"


def outgoing(text, cfg):
    """(text to send, note). Non-English text becomes English; the note says what happened."""
    if (cfg.get("tr_engine") or "off") == "off" or not cfg.get("tr_out", True):
        return text, None
    src = detect(text)
    if not src:                     # too short to tell: trust the operator's own language
        src = user_lang(cfg) if len(text.split()) >= 3 else ""
    if not src or src == "en":
        return text, None
    try:
        return translate(text, src, "en", cfg), {"from": src, "to": "en", "orig": text}
    except Exception as e:
        return text, {"error": f"not translated ({e}) - sent as typed"}


def incoming(text, cfg):
    """(translation or None, source language). Only when the text is not in the operator's language."""
    if (cfg.get("tr_engine") or "off") == "off" or not cfg.get("tr_in", True):
        return None, ""
    want = user_lang(cfg)
    src = detect(text)
    if not src or src == want:
        return None, src
    return translate(text, src, want, cfg), src
