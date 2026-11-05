#!/usr/bin/env python3
# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""Apply code sent by the Da Vinci AI assistant to a working copy of the repository.

Used by .github/workflows/davinci-intake.yml. The payload (JSON) is:

    {
      "title":   "Short title for the pull request",          (required)
      "branch":  "davinci/short-name",                         (optional; made from the title if missing)
      "message": "Commit message",                             (optional; the title if missing)
      "body":    "What changed and why (pull request text)",   (optional)
      "dry_run": false,                                        (optional; true = test only, push nothing)
      "files": [                                               (required, 1-50 entries)
        {"path": "satrx.py", "content": "...full new text..."},
        {"path": "icon.png", "content_b64": "...base64..."},
        {"path": "old_file.py", "delete": true}
      ]
    }

Every path is checked: relative, inside the repository, not in .git/ or .github/ (workflows can only be
changed by a person), no '..', at most 1 MB per file and 50 files per send.

    python tools/davinci_apply.py plan  payload.json        -> prints branch, title (for the workflow)
    python tools/davinci_apply.py apply payload.json [--root DIR] [--summary FILE]
"""
import argparse
import base64
import binascii
import json
import os
import re
import sys

MAX_FILES = 50
MAX_BYTES = 1_000_000
PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-/]*$")
PROTECTED_DIRS = (".git", ".github")
BRANCH_PREFIX = "davinci/"


class PayloadError(ValueError):
    pass


def slug(text, n=40):
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return (s[:n].rstrip("-") or "change")


def clean_branch(payload):
    """davinci/<name> - always under davinci/, never main or anything else."""
    raw = str(payload.get("branch") or "").strip()
    name = raw[len(BRANCH_PREFIX):] if raw.startswith(BRANCH_PREFIX) else raw
    parts = [slug(p, 40) for p in name.split("/") if re.search(r"[A-Za-z0-9]", p)]
    name = "/".join(parts) if parts else slug(payload.get("title"))
    return BRANCH_PREFIX + name[:60].strip("/-")


def check_path(path):
    if not isinstance(path, str) or not path:
        raise PayloadError("each file needs a 'path'")
    p = path.replace("\\", "/")
    if p.split("/")[0] in PROTECTED_DIRS:
        raise PayloadError(f"path not allowed: {path!r} ({p.split('/')[0]}/ can only be changed by a person)")
    if p.startswith("/") or not PATH_RE.match(p) or len(p) > 200:
        raise PayloadError(f"path not allowed: {path!r} (relative paths of letters, digits, _ . - / only)")
    parts = p.split("/")
    if any(x in ("", ".", "..") for x in parts):
        raise PayloadError(f"path not allowed: {path!r} (no empty, '.' or '..' parts)")
    if parts[0] in PROTECTED_DIRS:
        raise PayloadError(f"path not allowed: {path!r} ({parts[0]}/ can only be changed by a person)")
    return p


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError) as e:
        raise PayloadError(f"payload is not valid JSON: {e}")
    if not isinstance(payload, dict):
        raise PayloadError("payload must be a JSON object")
    title = str(payload.get("title") or "").strip()
    if not title:
        raise PayloadError("'title' is required")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise PayloadError("'files' must be a non-empty list")
    if len(files) > MAX_FILES:
        raise PayloadError(f"at most {MAX_FILES} files per send - send the rest to the same branch")
    seen = set()
    for f in files:
        if not isinstance(f, dict):
            raise PayloadError("each entry in 'files' must be an object")
        p = check_path(f.get("path"))
        if p in seen:
            raise PayloadError(f"{p} is listed twice")
        seen.add(p)
        f["path"] = p
        if f.get("delete"):
            continue
        if ("content" in f) == ("content_b64" in f):
            raise PayloadError(f"{p}: give either 'content' (text) or 'content_b64' (base64), not both/neither")
        if "content" in f:
            if not isinstance(f["content"], str):
                raise PayloadError(f"{p}: 'content' must be text")
            data = f["content"].encode("utf-8")
        else:
            try:
                data = base64.b64decode(str(f["content_b64"]), validate=True)
            except (binascii.Error, ValueError):
                raise PayloadError(f"{p}: 'content_b64' is not valid base64")
        if len(data) > MAX_BYTES:
            raise PayloadError(f"{p}: {len(data)} bytes - at most {MAX_BYTES} per file")
        f["_data"] = data
    payload["title"] = title[:120]
    payload["branch"] = clean_branch(payload)
    payload["message"] = str(payload.get("message") or title).strip()[:2000]
    payload["body"] = str(payload.get("body") or "").strip()[:20000]
    payload["dry_run"] = bool(payload.get("dry_run"))
    return payload


def apply(payload, root):
    """Write / delete the files under root. Returns a list of (action, path, bytes)."""
    root = os.path.realpath(root)
    done = []
    for f in payload["files"]:
        full = os.path.realpath(os.path.join(root, f["path"]))
        if not full.startswith(root + os.sep):                       # symlink tricks
            raise PayloadError(f"path not allowed: {f['path']!r} (leaves the repository)")
        if f.get("delete"):
            if os.path.isfile(full):
                os.remove(full)
                done.append(("deleted", f["path"], 0))
            else:
                done.append(("not found (delete skipped)", f["path"], 0))
            continue
        existed = os.path.exists(full)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as out:
            out.write(f["_data"])
        done.append(("updated" if existed else "added", f["path"], len(f["_data"])))
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", choices=("plan", "apply"))
    ap.add_argument("payload")
    ap.add_argument("--root", default=".")
    ap.add_argument("--summary", help="write a Markdown summary of the changes here")
    a = ap.parse_args(argv)
    try:
        payload = load(a.payload)
        if a.mode == "plan":
            print(json.dumps({"branch": payload["branch"], "title": payload["title"],
                              "message": payload["message"], "body": payload["body"],
                              "dry_run": payload["dry_run"], "files": len(payload["files"])}))
            return 0
        done = apply(payload, a.root)
    except PayloadError as e:
        print(f"Da Vinci payload refused: {e}", file=sys.stderr)
        return 2
    lines = [f"- {act}: `{p}`" + (f" ({n:,} bytes)" if n else "") for act, p, n in done]
    if a.summary:
        with open(a.summary, "w", encoding="utf-8") as s:
            s.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
