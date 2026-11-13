# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""Tradewind -> Pat bridge (v3.26).

Pat (getpat.io) is a Winlink client. When it runs on the same computer with its web interface on
(`pat http`, or `pat --listen varafm,telnet http` to also take incoming P2P calls), Tradewind can:

  * put a message - with an ICS form and attachments - in Pat's outbox   POST /api/mailbox/out
  * tell Pat to connect and exchange mail                                GET  /api/connect?url=...
      varafm:///CALL      VARA FM peer-to-peer
      varahf:///CALL      VARA HF peer-to-peer
      telnet://MYCALL@host:8774/   Telnet P2P (over a LAN, mesh or the internet)
      telnet              Pat's alias for the Winlink CMS over the internet (cms.winlink.org:8772)
  * read Pat's status and mailboxes                                      GET  /api/status, /api/mailbox/<box>

Pat does the Winlink B2F protocol, compression and VARA; Tradewind only composes and asks.
"""
import json
import re
import time
import urllib.parse
import urllib.request
import uuid

DEFAULT_URL = "http://127.0.0.1:8080"
TRANSPORTS = {"varafm": "VARA FM (P2P)", "varahf": "VARA HF (P2P)", "telnet_p2p": "Telnet P2P", "cms": "Winlink CMS (internet)",
              "ardop": "ARDOP (P2P)", "ax25": "AX.25 packet (P2P)"}
MAX_ATTACH = 120 * 1024          # Winlink radio mail: keep it small


def connect_url(transport, peer="", mycall="", host=""):
    """The connect string Pat understands for a transport and peer."""
    peer = re.sub(r"[^A-Z0-9/-]", "", str(peer or "").upper())
    if transport == "cms":
        return "telnet"                  # Pat's built-in alias: telnet://{mycall}:CMSTelnet@cms.winlink.org:8772/wl2k
    if not peer:
        raise ValueError("Enter the callsign of the station to connect to.")
    if transport in ("varafm", "varahf", "ardop", "ax25"):
        return f"{transport}:///{peer}"
    if transport == "telnet_p2p":
        h = str(host or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9.\-]+(:\d{1,5})?", h):
            raise ValueError("Enter the other station's address for Telnet P2P (for example 10.0.0.5 or myhost:8774).")
        if ":" not in h:
            h += ":8774"
        me = re.sub(r"[^A-Z0-9-]", "", str(mycall or "").upper())
        return f"telnet://{me}@{h}/"
    raise ValueError("Unknown transport")


def multipart(fields, files):
    """(body, content-type) for a multipart/form-data POST. files: [(field, filename, mime, bytes)]."""
    b = "----TradewindPat" + uuid.uuid4().hex
    out = []
    for k, v in fields:
        out.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, name, mime, data in files:
        safe = re.sub(r'["\r\n\\]', "_", name)
        out.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{safe}"\r\n'
                   f'Content-Type: {mime}\r\n\r\n'.encode() + data + b"\r\n")
    out.append(f"--{b}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={b}"


def _friendly(e):
    """Plain words for the usual failures."""
    text = str(getattr(e, "reason", "") or e)
    if "10061" in text or "refused" in text.lower():
        return "Pat isn't running on this computer - start it with: pat http"
    read = getattr(e, "read", None)
    if read:
        try:
            body = read().decode("utf-8", "replace").strip()
        except Exception:
            body = ""
        if body:
            return body[:160] + " - see the Pat window for the reason"
    return text[:160]


class Pat:
    def __init__(self, url=DEFAULT_URL, opener=None):
        self.url = url.rstrip("/")
        self.open = opener or urllib.request.urlopen
        self.busy = False              # a connect is in progress
        self.last = {"t": 0, "text": "", "ok": None}

    def set_url(self, url):
        url = str(url or DEFAULT_URL).strip().rstrip("/")
        p = urllib.parse.urlparse(url)
        if p.scheme != "http" or p.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("Pat must run on this computer: use http://127.0.0.1:8080 (or another port).")
        self.url = url

    def _req(self, path, data=None, ctype=None, timeout=8):
        req = urllib.request.Request(self.url + path, data=data, method="POST" if data is not None else "GET")
        if ctype:
            req.add_header("Content-Type", ctype)
        try:
            with self.open(req, timeout=timeout) as r:
                raw = r.read()
        except OSError as e:                       # URLError / HTTPError / refused: say it plainly
            raise IOError(_friendly(e)) from e
        try:
            return json.loads(raw or b"null")
        except ValueError:
            return raw.decode("utf-8", "replace")

    def status(self):
        """Pat's status, or {'running': False, 'error': ...} when Pat's web interface isn't reachable."""
        try:
            st = self._req("/api/status", timeout=3) or {}
            st = st if isinstance(st, dict) else {}
            st["running"] = True
        except Exception as e:
            st = {"running": False, "error": _friendly(e)}
        st["busy"], st["last"] = self.busy, self.last
        return st

    def post(self, to, subject, body, cc="", files=(), p2p_only=True):
        """Put a message in Pat's outbox. files: [(filename, mime, bytes)]."""
        to = " ".join(re.sub(r"[^A-Za-z0-9@._\-]", "", t) for t in re.split(r"[,; ]+", str(to)) if t).strip()
        if not to:
            raise ValueError("Who is it to? Enter a callsign (or an email address for the CMS).")
        if sum(len(f[2]) for f in files) > MAX_ATTACH:
            raise ValueError(f"Attachments come to more than {MAX_ATTACH // 1024} kB - too much for radio mail.")
        fields = [("to", to), ("cc", cc or ""), ("subject", (subject or "(no subject)")[:120]), ("body", body or ""),
                  ("date", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))]
        if p2p_only:
            fields.append(("p2ponly", "true"))
        body_b, ctype = multipart(fields, [("files", n, m, d) for n, m, d in files])
        return self._req("/api/mailbox/out", body_b, ctype, timeout=15)

    def connect(self, url):
        """Blocking: Pat connects, exchanges mail and hangs up. Run it in a thread."""
        if self.busy:
            raise IOError("Pat is already connecting")
        self.busy = True
        self.last = {"t": time.time(), "text": f"Connecting: {url}", "ok": None}
        try:
            r = self._req("/api/connect?url=" + urllib.parse.quote(url, safe=""), timeout=600)
            n = (r or {}).get("NumReceived", 0) if isinstance(r, dict) else 0
            self.last = {"t": time.time(), "text": f"Session finished - {n} message(s) received", "ok": True}
        except Exception as e:
            detail = _friendly(e)
            self.last = {"t": time.time(), "text": f"Connect failed: {detail}", "ok": False}
        finally:
            self.busy = False
        return self.last

    def abort(self):
        try:
            self._req("/api/disconnect?dirty=true", b"", timeout=5)
        except Exception:
            pass

    def mailbox(self, box="in"):
        if box not in ("in", "out", "sent", "archive"):
            raise ValueError("box")
        r = self._req(f"/api/mailbox/{box}")
        return [{"mid": m.get("MID"), "date": m.get("Date"), "from": (m.get("From") or {}).get("Addr", ""),
                 "to": [t.get("Addr", "") for t in (m.get("To") or [])], "subject": m.get("Subject", ""),
                 "files": [f.get("Name") for f in (m.get("Files") or [])], "unread": m.get("Unread", False),
                 "p2p": m.get("P2POnly", False)} for m in (r or []) if isinstance(m, dict)][:100]

    def message(self, box, mid):
        if box not in ("in", "out", "sent", "archive") or not re.fullmatch(r"[A-Z0-9]{12}", str(mid)):
            raise ValueError("message")
        m = self._req(f"/api/mailbox/{box}/{mid}") or {}
        return {"mid": mid, "from": (m.get("From") or {}).get("Addr", ""), "subject": m.get("Subject", ""),
                "date": m.get("Date"), "body": m.get("Body", ""), "files": [f.get("Name") for f in (m.get("Files") or [])]}
