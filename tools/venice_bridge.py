#!/usr/bin/env python3
# Tradewind - Copyright (c) 2026 Brandon St Rose (8P5DG) and Kammie Holder (8P5AB)
# Released under the MIT License - see LICENSE
"""Venice.ai -> GitHub: give Venice a coding task; its change comes back as a pull request.

Venice's chat app cannot reach GitHub, so this bridge does it through the Venice API (OpenAI-compatible,
with function calling):

  1. Venice gets the task, the list of files in the repository and the README.
  2. It reads the files it needs with the read_file tool (answered from this checkout).
  3. It hands in its change with submit_change: whole new files, or exact find/replace edits for big files.
  4. The change is tried on a scratch copy of the repository; if an edit does not match, Venice is told what
     failed and tries again (up to 3 times).
  5. The checked change is written as a payload (--out), shown (--dry-run), or sent to the venice-intake
     workflow (--send), which tests it and opens the pull request.

    set VENICE_API_KEY=...        (venice.ai -> Settings -> API)
    set GITHUB_TOKEN=github_pat_...   (only for --send; see docs/venice/README.md)
    python tools/venice_bridge.py "Add the PD90 SSTV mode to satrx.py with a test" --send
    python tools/venice_bridge.py "..." --dry-run          (show what Venice wrote, send nothing)

The GitHub workflow runs the same bridge when you start it with a task (Actions -> Venice code intake).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import venice_apply as va  # noqa: E402

BASE_URL = os.environ.get("VENICE_BASE_URL", "https://api.venice.ai/api/v1")
DEFAULT_MODEL = "qwen3-coder-480b-a35b-instruct-turbo"      # Venice coding model with function calling
MAX_TURNS = 16
MAX_READ = 120_000                                           # characters per read_file call
SKIP_DIRS = {".git", "__pycache__", "build", "dist", "release", ".venv", "node_modules"}

SYSTEM = """You are a careful senior Python developer working on Tradewind, an amateur-radio APRS station
(github.com/BajanBob/APRS-Tradewins): soundcard modem, propagation alerts, Satellite Watch Mode and satellite
receivers. A person will review your change as a pull request; the self-test `python test_modem.py` runs on it.

Rules:
- Read every file you change with read_file before changing it. Never guess at code you have not read.
- Make the smallest change that does the task well. Keep the existing style and comments.
- For existing files larger than about 300 lines use "edits": each "find" must be copied EXACTLY from the
  file (including indentation) and appear in it exactly once - include a few surrounding lines to make it
  unique. For new or small files send the whole "content".
- Add or update a test in test_modem.py for behaviour you change.
- Only Python standard library plus numpy, pyserial, sounddevice, cryptography (it must build for Android).
- New files start with the MIT licence header used in the other files.
- Never touch .github/, and never put keys, tokens or passwords in code.
- When finished, call submit_change exactly once with a clear title, a branch name venice/<short-name>, and a
  body explaining what changed, why, and how it is tested."""

TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file from the repository (main branch). Large files come in parts: use offset.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "e.g. satrx.py"},
            "offset": {"type": "integer", "description": "character offset to start from (default 0)"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "submit_change",
        "description": "Hand in the finished change. It is checked against the repository; you are told if anything fails.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "branch": {"type": "string", "description": "venice/<short-name>"},
            "body": {"type": "string", "description": "what changed, why, how it is tested"},
            "files": {"type": "array", "items": {"type": "object", "properties": {
                "path": {"type": "string"},
                "content": {"type": "string", "description": "complete new file text (new or small files)"},
                "edits": {"type": "array", "items": {"type": "object", "properties": {
                    "find": {"type": "string"}, "replace": {"type": "string"}},
                    "required": ["find", "replace"]}},
                "delete": {"type": "boolean"}},
                "required": ["path"]}}},
            "required": ["title", "files"]}}},
]


def repo_files(root):
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if d not in SKIP_DIRS and not d.startswith(".") or d == ".github")
        for f in sorted(fns):
            p = os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/")
            out.append((p, os.path.getsize(os.path.join(dp, f))))
    return out


def read_file(root, path, offset=0):
    try:
        p = va.check_path(path) if not str(path).startswith(".github/") else str(path)
    except va.PayloadError as e:
        return f"ERROR: {e}"
    full = os.path.realpath(os.path.join(root, p))
    if not full.startswith(os.path.realpath(root) + os.sep) or not os.path.isfile(full):
        return f"ERROR: no such file: {path}"
    try:
        with open(full, encoding="utf-8") as f:
            text = f.read()
    except UnicodeDecodeError:
        return f"ERROR: {path} is a binary file"
    offset = max(0, int(offset or 0))
    part = text[offset:offset + MAX_READ]
    more = len(text) - (offset + len(part))
    head = f"# {path}: {len(text)} characters, {text.count(chr(10)) + 1} lines; showing {offset}-{offset + len(part)}\n"
    return head + part + (f"\n# ... {more} more characters: read_file with offset={offset + len(part)}" if more > 0 else "")


def check_change(root, change):
    """Try the change on a scratch copy of the repository. Returns (payload, summary) or raises PayloadError."""
    tmp = tempfile.mkdtemp(prefix="venice_check_")
    try:
        pj = os.path.join(tmp, "payload.json")
        with open(pj, "w", encoding="utf-8") as f:
            json.dump(change, f)
        payload = va.load(pj)
        work = os.path.join(tmp, "repo")
        shutil.copytree(root, work, ignore=shutil.ignore_patterns(".git", "__pycache__", "build", "dist"), symlinks=True)
        done = va.apply(payload, work)
        for p in [f["path"] for f in payload["files"] if f["path"].endswith(".py") and not f.get("delete")]:
            r = subprocess.run([sys.executable, "-m", "py_compile", os.path.join(work, p)],
                               capture_output=True, text=True)
            if r.returncode:
                raise va.PayloadError(f"{p} does not compile: {r.stderr.strip()[-400:]}")
        return change, done
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def chat(messages, model, key):
    body = {"model": model, "messages": messages, "tools": TOOLS, "temperature": 0.2}
    req = urllib.request.Request(f"{BASE_URL}/chat/completions", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "User-Agent": "Tradewind-venice-bridge"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Venice API {e.code}: {e.read().decode(errors='replace')[:400]}")


def _json_in_text(text):
    """Some models answer with the change as JSON text instead of a tool call."""
    s, e = (text or "").find("{"), (text or "").rfind("}")
    if s < 0 or e <= s:
        return None
    try:
        obj = json.loads(text[s:e + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) and "files" in obj else None


def run(task, root, model, key, log=print, max_turns=MAX_TURNS):
    """The conversation with Venice. Returns (checked change dict, list of applied actions)."""
    files = repo_files(root)
    listing = "\n".join(f"{p}  ({n:,} bytes)" for p, n in files)
    readme = read_file(root, "README.md")[:8000] if os.path.exists(os.path.join(root, "README.md")) else ""
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"Task:\n{task}\n\nFiles in the repository:\n{listing}\n\n"
                                            f"Start of README.md:\n{readme}"}]
    failures = 0
    for turn in range(max_turns):
        reply = chat(messages, model, key)
        msg = reply["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")} | {"role": "assistant"})
        if not calls:
            change = _json_in_text(msg.get("content"))
            if change is None:
                messages.append({"role": "user", "content": "Please finish by calling submit_change with your change."})
                continue
            calls = [{"id": "text", "function": {"name": "submit_change", "arguments": json.dumps(change)}}]
        for c in calls:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                args = None
            if name == "read_file" and isinstance(args, dict):
                log(f"  Venice reads {args.get('path')}" + (f" @{args.get('offset')}" if args.get("offset") else ""))
                result = read_file(root, args.get("path", ""), args.get("offset", 0))
            elif name == "submit_change" and isinstance(args, dict):
                try:
                    return check_change(root, args)
                except va.PayloadError as e:
                    failures += 1
                    log(f"  change refused ({failures}/3): {e}")
                    if failures >= 3:
                        raise
                    result = f"REFUSED: {e}\nFix this (re-read the file if needed) and call submit_change again."
            else:
                result = f"ERROR: unknown tool or bad arguments for {name}"
            if c["id"] != "text":
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
            else:
                messages.append({"role": "user", "content": result})
    raise RuntimeError(f"Venice did not finish within {max_turns} turns")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Give Venice.ai a coding task for this repository")
    ap.add_argument("task", help="what to do, in plain words (or @file.txt to read it from a file)")
    ap.add_argument("--root", default=os.path.dirname(HERE))
    ap.add_argument("--model", default=os.environ.get("VENICE_MODEL") or DEFAULT_MODEL)
    ap.add_argument("--branch", default="", help="venice/<name> (Venice picks one if blank)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--out", help="write the checked payload here (used by the GitHub workflow)")
    g.add_argument("--send", action="store_true", help="send it to GitHub (needs GITHUB_TOKEN)")
    g.add_argument("--dry-run", action="store_true", help="show the change, send nothing")
    ap.add_argument("--test-only", action="store_true", help="with --send: GitHub tests it but opens no pull request")
    a = ap.parse_args(argv)
    task = open(a.task[1:], encoding="utf-8").read() if a.task.startswith("@") else a.task
    key = os.environ.get("VENICE_API_KEY")
    if not key:
        print("Set VENICE_API_KEY (venice.ai -> Settings -> API).", file=sys.stderr)
        return 2
    print(f"Venice ({a.model}) is working on: {task[:200]}")
    try:
        change, done = run(task, a.root, a.model, key)
    except (RuntimeError, va.PayloadError) as e:
        print(f"Venice could not complete the task: {e}", file=sys.stderr)
        return 1
    if a.branch:
        change["branch"] = a.branch
    change.setdefault("body", "")
    change["body"] = (change["body"] + f"\n\n_Written by Venice.ai ({a.model}) from the task:_\n> "
                      + task.strip().replace("\n", "\n> ")[:3000]).strip()
    if a.test_only:
        change["dry_run"] = True
    print(f"Change checked: {change.get('title')}")
    for act, p, n in done:
        print(f"  {act}: {p}" + (f" ({n:,} bytes)" if n else ""))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(change, f)
        return 0
    if a.dry_run:
        print(json.dumps(change, indent=2)[:20000])
        return 0
    if a.send:
        import venice_send as vs
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            print("Set GITHUB_TOKEN to send (docs/venice/README.md).", file=sys.stderr)
            return 2
        code, err = vs.send(change, token)
        if code == 204:
            print(f"Sent - the pull request appears in a few minutes: "
                  f"https://github.com/{vs.REPO}/pulls")
            return 0
        print(f"GitHub refused the send ({code}): {err[:300]}", file=sys.stderr)
        return 1
    print("Nothing sent. Use --send to open the pull request, or --dry-run to see the change.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
