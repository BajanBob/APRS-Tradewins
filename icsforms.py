# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""ICS forms (v3.26): ICS-213, ICS-213RR, ICS-214 and ICS-205 as structured documents.

A filled form is plain JSON ({"form": "ICS213", "v": 1, "data": {...}}). It travels three ways:
  * as a file by radio between Tradewind stations (twfile, "ICS213_....json") - shown as the form at the other end
  * as text messages in a chat (rendered compactly, split into 200-character parts to Tradewind stations)
  * through Pat as Winlink mail: the readable text in the body, the JSON attached
Field layout follows the FEMA ICS forms (blocks numbered as on the paper form). Clear text only.
"""
import json
import re
import time

T, A, D, H, S, TAB = "text", "area", "date", "time", "select", "table"

FORMS = {
    "ICS213": {"name": "ICS-213 General Message", "fields": [
        ("incident", "1. Incident name (optional)", T),
        ("to", "2. To (name and position)", T),
        ("from", "3. From (name and position)", T),
        ("subject", "4. Subject", T),
        ("date", "5. Date", D), ("time", "6. Time", H),
        ("message", "7. Message", A),
        ("approved", "8. Approved by (name, position / title)", T),
        ("reply", "Reply", A),
        ("replied_by", "Replied by (name, position / title, date / time)", T)]},
    "ICS213RR": {"name": "ICS-213RR Resource Request", "fields": [
        ("incident", "1. Incident name", T),
        ("date", "2. Date / time", T),
        ("rr_no", "3. Resource request number", T),
        ("items", "4. Order", TAB, ["Qty", "Kind", "Type", "Detailed item description", "Requested arrival", "Estimated cost"]),
        ("deliver_to", "5. Requested delivery / reporting location", T),
        ("substitutes", "6. Suitable substitutes and / or suggested sources", A),
        ("requested_by", "7. Requested by (name / position)", T),
        ("priority", "8. Priority", S, ["Urgent", "Routine", "Low"]),
        ("approval", "9. Section chief name for approval", T)]},
    "ICS214": {"name": "ICS-214 Activity Log", "fields": [
        ("incident", "1. Incident name", T),
        ("period", "2. Operational period (date / time from - to)", T),
        ("name", "3. Name", T), ("position", "4. ICS position", T), ("agency", "5. Home agency (and unit)", T),
        ("resources", "6. Resources assigned", TAB, ["Name", "ICS position", "Home agency (and unit)"]),
        ("log", "7. Activity log", TAB, ["Date / time", "Notable activities"]),
        ("prepared_by", "8. Prepared by (name, position / title, date / time)", T)]},
    "ICS205": {"name": "ICS-205 Incident Radio Communications Plan", "fields": [
        ("incident", "1. Incident name", T),
        ("prepared", "2. Date / time prepared", T),
        ("period", "3. Operational period (date / time from - to)", T),
        ("channels", "4. Basic radio channel use", TAB,
         ["Zone / group", "Ch #", "Function", "Channel name / talkgroup", "Assignment", "RX freq", "RX tone / NAC",
          "TX freq", "TX tone / NAC", "Mode (A, D, M)", "Remarks"]),
        ("special", "5. Special instructions", A),
        ("prepared_by", "6. Prepared by (communications unit leader)", T)]},
}
MAX_ROWS = 30
MAX_LEN = {T: 120, D: 10, H: 5, S: 20, A: 4000}


def catalogue():
    """The form definitions for the face plate."""
    return [{"id": k, "name": f["name"],
             "fields": [{"k": x[0], "label": x[1], "type": x[2], **({"cols": x[3]} if x[2] == TAB else {}),
                         **({"options": x[3]} if x[2] == S else {})} for x in f["fields"]]} for k, f in FORMS.items()]


def _clean(v, n):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(v if v is not None else ""))[:n]


def clean(form_id, data):
    """A form with only known fields, sensible lengths and ASCII-safe text. Raises ValueError."""
    f = FORMS.get(str(form_id).upper())
    if not f:
        raise ValueError("Unknown form")
    data = data if isinstance(data, dict) else {}
    out = {}
    for spec in f["fields"]:
        k, typ = spec[0], spec[2]
        v = data.get(k)
        if typ == TAB:
            cols = spec[3]
            rows = []
            for r in (v if isinstance(v, list) else [])[:MAX_ROWS]:
                r = r if isinstance(r, list) else []
                row = [_clean(r[i] if i < len(r) else "", 200) for i in range(len(cols))]
                if any(c.strip() for c in row):
                    rows.append(row)
            out[k] = rows
        elif typ == S:
            out[k] = v if v in spec[3] else ""
        else:
            out[k] = _clean(v, MAX_LEN[typ])
    return {"form": str(form_id).upper(), "v": 1, "data": out}


def render_text(doc):
    """Readable plain text of a filled form (Winlink mail body, printout)."""
    f = FORMS[doc["form"]]
    d = doc["data"]
    lines = [f["name"].upper(), "=" * len(f["name"])]
    for spec in f["fields"]:
        k, label, typ = spec[0], spec[1], spec[2]
        v = d.get(k)
        if typ == TAB:
            if not v:
                continue
            lines.append(f"{label}:")
            for i, row in enumerate(v, 1):
                cells = [f"{c}: {x}" for c, x in zip(spec[3], row) if str(x).strip()]
                lines.append(f"  {i}. " + "; ".join(cells))
        elif typ == A:
            if v:
                lines += [f"{label}:", *["  " + x for x in str(v).splitlines()]]
        elif v:
            lines.append(f"{label}: {v}")
    lines.append(f"-- sent with Tradewind {time.strftime('%Y-%m-%d %H:%M', time.gmtime())}Z")
    return "\n".join(lines)


def render_compact(doc):
    """Short one-line form for APRS text messages: ICS213 TO:..|FR:..|SUBJ:..|MSG:.."""
    f = FORMS[doc["form"]]
    parts = []
    for spec in f["fields"]:
        k, typ, v = spec[0], spec[2], doc["data"].get(spec[0])
        if not v:
            continue
        if typ == TAB:
            v = " / ".join(",".join(str(x) for x in row if str(x).strip()) for row in v)
        parts.append(f"{k.upper()}:{' '.join(str(v).split())}")
    return doc["form"] + " " + "|".join(parts)


def subject(doc):
    d = doc["data"]
    s = d.get("subject") or d.get("incident") or ""
    return f"{doc['form']} {s}".strip()[:100]


def filename(doc):
    s = re.sub(r"[^A-Za-z0-9]+", "_", subject(doc))[:30].strip("_")
    return f"{s or doc['form']}.json"


def to_bytes(doc):
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=True).encode()


def from_bytes(raw):
    """A received form file, cleaned. Raises ValueError."""
    d = json.loads(raw.decode("utf-8", "replace"))
    if not isinstance(d, dict):
        raise ValueError("not a form")
    return clean(d.get("form"), d.get("data"))
