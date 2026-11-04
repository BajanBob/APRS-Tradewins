# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""Fill-in and wide-area digipeater (WIDEn-N paradigm, at most 2 hops).

Wide-area mode (for a high site serving a whole island with no other digipeaters): also answers WIDE2-2 and
WIDE2-1, counting the hop down, e.g.  ...,WIDE2-2:  ->  ...,8P5AB-10*,WIDE2-1:  and  ...,WIDE2-1:  ->  ...,8P5AB-10*,WIDE2*:
Paths asking for more than 2 hops in total (WIDE2-3, WIDE3-3, ...) are never repeated.


A fill-in digipeater helps stations that the wide-area digipeaters can't hear. It repeats a packet once when:
  * the packet's first unused path hop is WIDE1-1 and it is the FIRST hop of the path (nothing used before it), or
  * the next unused hop is this station's own callsign (or its optional alias), i.e. the sender routed via us.
When it repeats, it marks the hop as used and puts its own call in front (trace), for example
    8P6XX>APRS,WIDE1-1,WIDE2-1:...  ->  8P6XX>APRS,8P5AB-10*,WIDE1*,WIDE2-1:...
It never repeats its own packets, duplicates heard in the last 30 s, internet-gated traffic (TCPIP/TCPXX/q-constructs),
or the outdated RELAY/TRACE and wider-than-WIDE2 aliases, and it limits how many packets it repeats per minute.
"""
import collections
import re
import time

MAX_PATH = 8                         # AX.25 allows at most 8 digipeater addresses
DUP_WINDOW_S = 30                    # a packet heard again within this time is a duplicate
DEFAULT_MAX_PER_MIN = 10             # repeats per minute (on top of the TNC's own airtime limit)
_CALL_RE = re.compile(r"^[A-Z0-9]{1,6}(-([0-9]|1[0-5]))?$")
_NO_DIGI = ("TCPIP", "TCPXX")             # internet-gated (third-party) traffic is never repeated on RF
_REFUSED = ("RELAY", "TRACE", "WIDE", "WIDE3", "WIDE4", "WIDE5", "WIDE6", "WIDE7")


def base_call(c):
    return c.rstrip("*").split("-")[0].upper()


def valid_call(c):
    return bool(_CALL_RE.match(str(c or "").strip().upper()))


def parse(line):
    """'SRC>DST,P1,P2:info' -> (src, dst, [path], info) or None."""
    head, sep, info = line.partition(":")
    if not sep or ">" not in head:
        return None
    src, _, rest = head.partition(">")
    parts = rest.split(",")
    if not src or not parts[0]:
        return None
    return src.strip(), parts[0].strip(), [p.strip() for p in parts[1:] if p.strip()], info


_WIDE_RE = re.compile(r"^WIDE([1-7])-([1-7])$")


def plan(line, mycall, alias="", mode="fill"):
    """Work out the repeat for one heard packet. Returns (src, dst, new_path, info, reason) or (None, why_not)."""
    p = parse(line)
    if not p:
        return None, "not a packet"
    src, dst, path, info = p
    me = mycall.upper()
    if base_call(src) == base_call(me):
        return None, "own packet"
    if info.startswith("}") or any(x.rstrip("*").upper().startswith(_NO_DIGI) or x.upper().startswith("QA") for x in path):
        return None, "internet-gated traffic"
    # first hop not yet used
    used = [i for i, x in enumerate(path) if x.endswith("*")]
    nxt = (used[-1] + 1) if used else 0
    if nxt >= len(path):
        return None, "path complete"
    hop = path[nxt].upper()
    if hop in (me, (alias or "").upper()) and hop:
        new = path[:nxt] + [me + "*"] + path[nxt + 1:]
        return (src, dst, new, info, f"addressed to {hop}"), ""
    if hop.rstrip("*") in _REFUSED or hop.startswith(("RELAY", "TRACE")):
        return None, f"{hop} is outdated or floods the network"
    if hop == "WIDE1-1":
        if nxt != 0:
            return None, "WIDE1-1 is not the first hop (fill-in digipeaters only help the first hop)"
        new = [me + "*", "WIDE1*"] + path[1:]
        if len(new) > MAX_PATH:
            return None, "path too long"
        return (src, dst, new, info, "fill-in WIDE1-1"), ""
    m = _WIDE_RE.match(hop)
    if mode == "wide" and m:
        n, left = int(m.group(1)), int(m.group(2))
        hops_total = sum(int(x.group(1)) for x in (_WIDE_RE.match(p.rstrip("*").upper()) for p in path) if x)
        after_wide1 = any(p.rstrip("*").upper().startswith("WIDE1") for p in path[:nxt])
        if n != 2 or left > 2 or hops_total > 3 or (left == 2 and after_wide1):   # WIDE1-1,WIDE2-2 = 3 hops
            return None, f"{hop}: more than 2 hops - not repeated"
        rest = path[nxt + 1:]
        new = path[:nxt] + [me + "*", f"WIDE2-{left - 1}" if left > 1 else "WIDE2*"] + rest
        if len(new) > MAX_PATH:
            return None, "path too long"
        return (src, dst, new, info, f"wide {hop}"), ""
    return None, f"next hop {hop} is not for a {'wide-area' if mode == 'wide' else 'fill-in'} digipeater"


class FillInDigi:
    """Decides which heard packets to repeat, with duplicate suppression and a per-minute limit."""

    def __init__(self, mycall, alias="", max_per_min=DEFAULT_MAX_PER_MIN, dup_window=DUP_WINDOW_S, now=time.time,
                 mode="fill"):
        self.mycall, self.alias = mycall.upper(), (alias or "").upper()
        self.mode = "wide" if mode == "wide" else "fill"
        self.max_per_min, self.dup_window, self.now = max(1, int(max_per_min)), dup_window, now
        self.seen = {}                          # (src, dst, info) -> last time heard or sent
        self.sent = collections.deque()         # times of repeats in the last minute
        self.count = 0                          # packets repeated since start
        self.skipped = 0                        # duplicates / over the limit
        self.last = None                        # last repeated packet (text)

    def _key(self, src, dst, info):
        return (src.rstrip("*").upper(), dst.upper(), info)

    def consider(self, line):
        """Call for every packet heard on RF. Returns (src, dst, path, info) to transmit, or None."""
        t = self.now()
        for k in [k for k, v in self.seen.items() if t - v > self.dup_window]:
            del self.seen[k]
        p = parse(line)
        if not p:
            return None
        key = self._key(p[0], p[1], p[3])
        dup = key in self.seen
        self.seen[key] = t                     # any copy heard (direct or via another digi) resets the window
        result, _why = plan(line, self.mycall, self.alias, self.mode)
        if not result:
            return None
        if dup:
            self.skipped += 1
            return None
        while self.sent and t - self.sent[0] > 60:
            self.sent.popleft()
        if len(self.sent) >= self.max_per_min:
            self.skipped += 1
            return None
        self.sent.append(t)
        self.count += 1
        src, dst, path, info, _reason = result
        self.last = f"{src}>{','.join([dst] + path)}:{info}"
        return src, dst, path, info

    @property
    def status(self):
        return {"on": True, "mode": self.mode, "count": self.count, "skipped": self.skipped, "last": self.last,
                "per_min": len(self.sent), "max_per_min": self.max_per_min, "alias": self.alias}
