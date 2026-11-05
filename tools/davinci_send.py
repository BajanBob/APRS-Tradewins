#!/usr/bin/env python3
# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""Export code Da Vinci wrote (saved on this computer) to the repository as a pull request.

    set GITHUB_TOKEN=github_pat_...            (fine-grained token, this repository only - docs/davinci/README.md)
    python tools/davinci_send.py --title "Add PD90 SSTV" satrx.py test_modem.py
    python tools/davinci_send.py --title "Rename" --delete old.py new.py
    python tools/davinci_send.py --dry-run ...    show the payload, send nothing
    python tools/davinci_send.py --test-only ...  GitHub applies and self-tests, pushes nothing

Paths are sent relative to --base (default: the current folder), exactly as they should appear in the repo.
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

REPO = "BajanBob/APRS-Tradewins"
API = "https://api.github.com"


def build_payload(paths, title, base=".", branch="", message="", body="", delete=(), dry_run=False):
    files = []
    for p in paths:
        rel = os.path.relpath(os.path.join(base, p), base).replace(os.sep, "/") if not os.path.isabs(p) \
            else os.path.relpath(p, base).replace(os.sep, "/")
        with open(os.path.join(base, rel), "rb") as f:
            data = f.read()
        try:
            files.append({"path": rel, "content": data.decode("utf-8")})
        except UnicodeDecodeError:
            files.append({"path": rel, "content_b64": base64.b64encode(data).decode()})
    files += [{"path": d.replace(os.sep, "/"), "delete": True} for d in delete]
    payload = {"title": title, "files": files}
    for k, v in (("branch", branch), ("message", message), ("body", body)):
        if v:
            payload[k] = v
    if dry_run:
        payload["dry_run"] = True
    return payload


def send(payload, token, repo=REPO):
    req = urllib.request.Request(
        f"{API}/repos/{repo}/dispatches", method="POST",
        data=json.dumps({"event_type": "davinci-code", "client_payload": payload}).encode(),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json",
                 "User-Agent": "Tradewind-davinci-send"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Send Da Vinci's code to GitHub as a pull request")
    ap.add_argument("files", nargs="*")
    ap.add_argument("--title", required=True)
    ap.add_argument("--branch", default="")
    ap.add_argument("--message", default="")
    ap.add_argument("--body", default="")
    ap.add_argument("--delete", nargs="*", default=[])
    ap.add_argument("--base", default=".")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--dry-run", action="store_true", help="print the payload and send nothing")
    ap.add_argument("--test-only", action="store_true", help="GitHub applies and tests, pushes nothing")
    a = ap.parse_args(argv)
    if not a.files and not a.delete:
        ap.error("give at least one file (or --delete)")
    payload = build_payload(a.files, a.title, a.base, a.branch, a.message, a.body, a.delete, a.test_only)
    if a.dry_run:
        print(json.dumps(payload, indent=2)[:4000])
        return 0
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("DAVINCI_TOKEN")
    if not token:
        print("Set GITHUB_TOKEN to a fine-grained token for this repository (docs/davinci/README.md).", file=sys.stderr)
        return 2
    code, err = send(payload, token, a.repo)
    if code == 204:
        print(f"Sent. GitHub is building the pull request: https://github.com/{a.repo}/actions/workflows/davinci-intake.yml")
        return 0
    print(f"GitHub refused the send ({code}): {err[:300]}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
