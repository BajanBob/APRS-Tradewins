# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""Tradewind file transfer (v3.26): pictures, voice clips and small files between two Tradewind stations.

Clear text, as amateur rules require: the file is only base64-encoded (a public, documented encoding) so it
fits in AX.25 UI frames. Every frame carries the destination callsign and goes out with NOGATE in the path,
so iGates don't copy it to the internet. APRS programs ignore it: the info field starts with "{{",
APRS's experimental user-defined format.

  {{TFH|TO|XID|TOTAL|SIZE|SHA|MIME|NAME     header: what is coming (sent first, and again if no answer)
  {{TFD|TO|XID|SEQ|BASE64                   one piece of the file (162 bytes)
  {{TFN|TO|XID|SEQ,SEQ,...                  receiver: these pieces are missing, send them again
  {{TFK|TO|XID                              receiver: complete and checked

Files go only where they don't load the shared APRS channel: after QSY to a chat channel (two stations,
up to FILE_AIRTIME_PCT of the time) or on a fast second radio link (9600 baud and up).
"""
import base64
import hashlib
import os
import re
import time

PREFIX = "{{TF"
CHUNK = 162                      # raw bytes per frame -> 216 base64 characters (an AX.25 frame holds 256)
MAX_BYTES = 64 * 1024
GAP_S = 0.4                      # pause between frames when the channel allows it
RX_IDLE_S = 20                   # receiver: ask for missing pieces after this much silence
TX_WAIT_S = 45                   # sender: no answer after the last piece -> send the header again
TRIES = 4
BURST = 8                        # at most this many pieces per tick (the radio's queue limits it further)
FILE_AIRTIME_PCT = 50
MIMES = {"image/png": "png", "image/gif": "gif", "image/jpeg": "jpg", "image/webp": "webp",
         "audio/webm": "webm", "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/wav": "wav", "audio/mp4": "m4a",
         "text/plain": "txt", "application/json": "json", "application/pdf": "pdf", "text/csv": "csv", "application/octet-stream": "bin"}
SHOW_INLINE = ("image/png", "image/gif", "image/jpeg", "image/webp", "audio/webm", "audio/ogg", "audio/mpeg",
               "audio/wav", "audio/mp4")


def safe_name(name, mime):
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(str(name or "")))[:40].strip("._") or "file"
    ext = MIMES.get(mime, "bin")
    return stem if stem.lower().endswith("." + ext) else f"{stem}.{ext}"


def frames_needed(size):
    return max(1, -(-size // CHUNK))


def estimate_s(size, baud=1200, pct=FILE_AIRTIME_PCT):
    """Rough seconds to send a file: airtime of every frame, stretched by the channel-share limit."""
    per = (CHUNK * 4 // 3 + 50 + 20) * 8 * 1.05 / baud + 0.35
    return frames_needed(size) * per * 100 / pct


class Transfers:
    """File transfers of one station. send_frame(to, info) transmits one frame (raises IOError when the
    channel-share limit says wait); on_file(direction, peer, meta) reports progress to the chat."""

    def __init__(self, mycall, store_dir, send_frame, on_file, now=time.time):
        self.mycall, self.dir = mycall.upper(), store_dir
        self.send_frame, self.on_file, self.now = send_frame, on_file, now
        self.tx, self.rx = {}, {}
        self.next_t = 0

    # -- sending --
    def send(self, to, name, mime, data):
        if mime not in MIMES:
            raise ValueError("That kind of file can't be sent. Pictures (PNG, GIF, JPEG, WebP), voice clips, "
                             "text, CSV and PDF files can.")
        if not data:
            raise ValueError("The file is empty.")
        if len(data) > MAX_BYTES:
            raise ValueError(f"That file is {len(data) // 1024} kB. Files up to {MAX_BYTES // 1024} kB can go by radio "
                             "- shrink the picture or record a shorter clip.")
        to = to.upper()
        xid = os.urandom(3).hex()[:5]
        name = safe_name(name, mime)
        chunks = [base64.b64encode(data[i:i + CHUNK]).decode() for i in range(0, len(data), CHUNK)]
        meta = {"id": xid, "name": name, "mime": mime, "size": len(data), "total": len(chunks), "done": 0,
                "state": "sending", "sha": hashlib.sha256(data).hexdigest()[:12]}
        self.tx[xid] = {"to": to, "chunks": chunks, "queue": ["H"] + list(range(len(chunks))), "meta": meta,
                        "last": 0, "tries": 0}
        self._save(xid, self.mycall, name, data)
        self.on_file("out", to, meta)
        return meta

    def _header(self, t):
        m = t["meta"]
        return f"{PREFIX}H|{t['to']}|{m['id']}|{m['total']}|{m['size']}|{m['sha']}|{m['mime']}|{m['name']}"

    def tick(self):
        """Send at most one frame; look after time-outs. Call often (every few hundred ms)."""
        now = self.now()
        for xid, r in list(self.rx.items()):
            if now - r["last"] >= RX_IDLE_S and r.get("total"):
                missing = [i for i in range(r["total"]) if i not in r["parts"]]
                if r["tries"] >= TRIES:
                    r["meta"]["state"] = "failed"
                    self.on_file("in", r["from"], r["meta"])
                    del self.rx[xid]
                    continue
                r["tries"] += 1
                r["last"] = now
                self._out(r["from"], f"{PREFIX}N|{r['from']}|{xid}|{','.join(map(str, missing[:40]))}")
        if now < self.next_t:
            return
        for _ in range(BURST):                        # fast links take several pieces per transmission
            if not self._send_one(now):
                return

    def _send_one(self, now):
        """Send the next piece of some transfer. False when nothing was sent (done, or the radio says wait)."""
        for xid, t in list(self.tx.items()):
            if t["queue"]:
                item = t["queue"][0]
                info = self._header(t) if item == "H" else f"{PREFIX}D|{t['to']}|{xid}|{item}|{t['chunks'][item]}"
                try:
                    self.send_frame(t["to"], info)
                except IOError as e:
                    if "busy" in str(e):
                        return False                   # the radio still has pieces to send: next tick
                    self.next_t = now + (12 if "airtime" in str(e) else 30)
                    t["meta"]["wait"] = str(e)[:80]
                    self.on_file("out", t["to"], t["meta"])
                    return False
                t["queue"].pop(0)
                t["last"], self.next_t = now, now + GAP_S
                t["meta"].pop("wait", None)
                if item != "H":
                    t["meta"]["done"] = min(t["meta"]["total"], t["meta"]["done"] + 1)
                self.on_file("out", t["to"], t["meta"])
                return True
            if now - t["last"] >= TX_WAIT_S:
                if t["tries"] >= TRIES:
                    t["meta"]["state"] = "failed"
                    self.on_file("out", t["to"], t["meta"])
                    del self.tx[xid]
                    continue
                t["tries"] += 1
                t["queue"].append("H")                  # ask the receiver where it stands
        return False

    def _out(self, to, info):
        try:
            self.send_frame(to, info)
        except IOError:
            pass                                       # a lost request is repeated on the next time-out

    # -- receiving --
    def feed(self, src, info):
        """A frame heard on the air. True if it was a file-transfer frame."""
        if not info.startswith(PREFIX):
            return False
        f = info[len(PREFIX):].split("|")
        if len(f) < 3 or f[1].upper() != self.mycall:
            return True                                # someone else's transfer: ignore
        kind, xid, src = f[0], f[2], src.upper()
        now = self.now()
        if kind == "H" and len(f) >= 8:
            done = self._done(xid, src)
            if done:
                self._out(src, f"{PREFIX}K|{src}|{xid}")   # we already have it: our "done" was lost
                return True
            try:
                total, size = int(f[3]), int(f[4])
            except ValueError:
                return True
            if f[6] not in MIMES or size > MAX_BYTES or total != frames_needed(size):
                return True
            r = self.rx.setdefault(xid, {"from": src, "parts": {}, "tries": 0})
            r.update(total=total, size=size, sha=f[5], last=now)
            r["meta"] = {"id": xid, "name": safe_name("|".join(f[7:]), f[6]), "mime": f[6], "size": size,
                         "total": total, "done": len(r["parts"]), "state": "receiving"}
            self.on_file("in", src, r["meta"])
            self._check(xid)
        elif kind == "D" and len(f) >= 5:
            r = self.rx.setdefault(xid, {"from": src, "parts": {}, "tries": 0, "last": now})
            try:
                r["parts"][int(f[3])] = base64.b64decode(f[4], validate=True)
            except (ValueError, TypeError):
                return True
            r["last"] = now
            if r.get("meta"):
                r["meta"]["done"] = len(r["parts"])
                self.on_file("in", src, r["meta"])
            self._check(xid)
        elif kind == "N" and len(f) >= 4:
            t = self.tx.get(xid)
            if t and t["to"] == src:
                want = [int(x) for x in f[3].split(",") if x.isdigit() and int(x) < len(t["chunks"])]
                t["queue"] += [i for i in want if i not in t["queue"]]
                t["meta"]["state"] = "sending"
        elif kind == "K":
            t = self.tx.pop(xid, None)
            if t and t["to"] == src:
                t["meta"].update(state="delivered", done=t["meta"]["total"])
                self.on_file("out", src, t["meta"])
        return True

    def _check(self, xid):
        r = self.rx.get(xid)
        if not r or not r.get("total") or len(r["parts"]) < r["total"]:
            return
        data = b"".join(r["parts"][i] for i in range(r["total"]))
        if len(data) != r["size"] or hashlib.sha256(data).hexdigest()[:12] != r["sha"]:
            r["parts"].clear()                        # damaged: start again
            r["meta"]["done"] = 0
            return
        del self.rx[xid]
        self._save(xid, r["from"], r["meta"]["name"], data)
        r["meta"].update(state="received", done=r["total"])
        self.on_file("in", r["from"], r["meta"])
        self._out(r["from"], f"{PREFIX}K|{r['from']}|{xid}")

    # -- the files on disk --
    def _save(self, xid, who, name, data):
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, f"{xid}_{re.sub(r'[^A-Z0-9-]', '_', who)}_{name}"), "wb") as fh:
            fh.write(data)

    def _done(self, xid, src):
        return self.find(xid, src) is not None

    def find(self, xid, who=None):
        """Path of a stored file, or None."""
        if not re.fullmatch(r"[0-9a-f]{5}", str(xid or "")) or not os.path.isdir(self.dir):
            return None
        for n in os.listdir(self.dir):
            if n.startswith(xid + "_") and (who is None or n.split("_", 2)[1] == re.sub(r"[^A-Z0-9-]", "_", who)):
                return os.path.join(self.dir, n)
        return None
