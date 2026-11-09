# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""List every English string a translation needs: the face plate (HTML, attributes, JavaScript
text) and the server messages shown on screen (errors and notes from app.py / tnc.py).

    python tools/i18n_extract.py            writes i18n/strings.json
    python tools/i18n_extract.py --missing es   lists strings i18n/es.json does not translate yet

Strings with changing parts become patterns: "{0} stations heard". On-air text (beacons,
bulletins, APRS messages) is never translated: only what is drawn on the screen.
"""
import argparse
import ast
import html.parser
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ATTRS = ("placeholder", "title", "aria-label")
PH = "\x00"


def human(s):
    """Text a person reads (not code, ids, selectors or units alone)."""
    t = re.sub(r"\{\d+\}", "", s).strip()
    if len(t) < 2 or not re.search(r"[A-Za-z]{2}", t):
        return False
    if re.search(r"[;{}]|=>|\(\)|^[#.\[]|^https?:|\.(py|js|json|html|png|csv)\b|^[a-z0-9_\-]+$|^[a-z]+[A-Z]\w*$", t):
        return False
    if re.fullmatch(r"[A-Z0-9_\-/]+", t) and len(t) < 4:       # short codes: TX, RX, 2M, CW
        return False
    if re.search(r"/|\bpx\b|\d+px|^[A-Za-z]+/[a-z]|^[A-Z0-9]{1,3}\d[A-Z]{1,4}(-\d+)?$|^BLN|^\w+\.\w+$", t):
        return False                                               # paths, CSS, MIME, callsigns, bulletin ids
    if re.fullmatch(r"[A-Za-z]+[A-Z][a-z]+\w*", t) and " " not in t:   # camelCase identifiers
        return False
    return True


class _Collect(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "code", "pre"):
            self.skip += 1
        for k, v in attrs:
            if k in ATTRS and v:
                self.out.append(v)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "code", "pre") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            for part in re.split(r"\s*\n\s*", data):
                if part.strip():
                    self.out.append(part.strip())


def _norm(s):
    """Placeholders back to {n}, numbered in order; collapse spaces."""
    n = [0]

    def num(_):
        n[0] += 1
        return "{%d}" % (n[0] - 1)
    return re.sub(r"\s+", " ", re.sub(PH + r"\d+" + PH, num, s)).strip()


def _subst_templates(lit):
    """Replace ${...} (balanced) by placeholders."""
    out, i, k = [], 0, 0
    while i < len(lit):
        if lit.startswith("${", i):
            depth, j = 1, i + 2
            while j < len(lit) and depth:
                depth += {"{": 1, "}": -1}.get(lit[j], 0)
                j += 1
            out.append(f"{PH}{k}{PH}")
            k += 1
            i = j
        else:
            out.append(lit[i])
            i += 1
    return "".join(out)


def html_strings(src):
    p = _Collect()
    p.feed(src)
    return p.out


def js_literals(js):
    lits = []
    i, n = 0, len(js)
    while i < n:
        c = js[i]
        if c == "/" and js.startswith("//", i):
            i = js.find("\n", i)
            i = n if i < 0 else i
            continue
        if c == "/" and js.startswith("/*", i):
            i = js.find("*/", i)
            i = n if i < 0 else i + 2
            continue
        if c in "'\"`":
            j, buf, depth = i + 1, [], 0
            while j < n:
                d = js[j]
                if d == "\\" and j + 1 < n:
                    buf.append(js[j:j + 2])
                    j += 2
                    continue
                if c == "`" and js.startswith("${", j):
                    depth += 1
                elif c == "`" and d == "}" and depth:
                    depth -= 1
                elif d == c and not depth:
                    break
                elif d == "\n" and c != "`":
                    break
                buf.append(d)
                j += 1
            lits.append(("".join(buf), c))
            i = j + 1
            continue
        i += 1
    return lits


def js_strings(js):
    out = []
    for lit, q in js_literals(js):
        lit = lit.replace("\\'", "'").replace('\\"', '"').replace("\\n", " ")
        s = _subst_templates(lit) if q == "`" else lit
        if "<" in s and re.search(r"<[a-z/!]", s):
            out += html_strings(s)
        else:
            out.append(s)
    return [_norm(x) for x in out]


def py_strings(path):
    """Messages the server sends to the screen: errors, notes, refusals (not logs, not on-air text)."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    out = []

    def text(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            parts, k = [], 0
            for v in node.values:
                if isinstance(v, ast.Constant):
                    parts.append(str(v.value))
                else:
                    parts.append(f"{PH}{k}{PH}")
                    k += 1
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            a, b = text(node.left), text(node.right)
            if a is not None and b is not None:
                return a + b
        return None

    def grab(node):
        for sub in ast.walk(node):
            t = text(sub)
            if t is not None:
                out.append(t)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            owner = getattr(getattr(f, "value", None), "id", "")
            if (name == "append" and owner in ("errs", "errors", "warn", "warnings", "notes")) or \
                    name in ("ValueError", "IOError", "RuntimeError", "OSError"):
                for a in node.args:
                    grab(a)
        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value in ("errors", "error", "note", "msg", "message", "warning", "hint"):
                    grab(v)
    return [_norm(x) for x in out]


def extract():
    face = open(os.path.join(ROOT, "faceplate.html"), encoding="utf-8").read()
    head, _, rest = face.partition("<script>")
    js, _, tail = rest.partition("</script>")
    found = html_strings(head) + js_strings(js) + html_strings(tail)
    for f in ("app.py", "tnc.py"):
        found += py_strings(os.path.join(ROOT, f))
    seen, out = set(), []
    for s in (_norm(x) for x in found):
        s = s.strip(" :")
        if s and s not in seen and human(s):
            seen.add(s)
            out.append(s)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--missing", metavar="LANG")
    a = ap.parse_args(argv)
    strings = extract()
    if a.missing:
        have = json.load(open(os.path.join(ROOT, "i18n", a.missing + ".json"), encoding="utf-8"))
        for s in strings:
            if s not in have:
                print(s)
        return 0
    os.makedirs(os.path.join(ROOT, "i18n"), exist_ok=True)
    with open(os.path.join(ROOT, "i18n", "strings.json"), "w", encoding="utf-8") as f:
        json.dump(strings, f, ensure_ascii=False, indent=0)
    print(f"{len(strings)} strings, {sum(len(s.split()) for s in strings)} words -> i18n/strings.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
