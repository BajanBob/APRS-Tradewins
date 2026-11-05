#!/usr/bin/env python3
"""Self-test for the Venice.ai pipeline (tools/venice_apply.py, venice_send.py, venice_bridge.py). Run: python tools/test_venice.py"""
import base64
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import venice_apply as da  # noqa: E402
import venice_send as ds  # noqa: E402
import venice_bridge as vb  # noqa: E402


def run(payload, root):
    p = os.path.join(root, "..", "payload.json")
    with open(p, "w") as f:
        json.dump(payload, f)
    return da.load(p)


def refused(payload, root, why):
    try:
        run(payload, root)
    except da.PayloadError as e:
        assert why in str(e), (why, str(e))
        return
    raise AssertionError(f"should have been refused: {why}")


def main():
    work = tempfile.mkdtemp(prefix="venice_")
    root = os.path.join(work, "repo")
    os.makedirs(os.path.join(root, ".github", "workflows"))
    with open(os.path.join(root, "old.py"), "w") as f:
        f.write("x = 1\n")
    # a normal send: text, binary, a new folder, a delete
    png = b"\x89PNG\r\n\x1a\nfake"
    pl = run({"title": "Add PD90 SSTV mode!", "files": [
        {"path": "satrx.py", "content": "print('PD90')\n"},
        {"path": "docs/img/a.png", "content_b64": base64.b64encode(png).decode()},
        {"path": "old.py", "delete": True}]}, root)
    assert pl["branch"] == "venice/add-pd90-sstv-mode" and pl["message"] == "Add PD90 SSTV mode!"
    done = da.apply(pl, root)
    assert [d[0] for d in done] == ["added", "added", "deleted"], done
    assert open(os.path.join(root, "satrx.py")).read() == "print('PD90')\n"
    assert open(os.path.join(root, "docs/img/a.png"), "rb").read() == png and not os.path.exists(os.path.join(root, "old.py"))
    # branch is always under venice/ and cleaned
    assert run({"title": "t", "branch": "main", "files": [{"path": "a", "content": ""}]}, root)["branch"] == "venice/main"
    assert run({"title": "t", "branch": "venice/Fix SSTV/../x", "files": [{"path": "a", "content": ""}]},
               root)["branch"] == "venice/fix-sstv/x"
    assert run({"title": "t", "branch": "refs/heads/main;rm -rf /", "files": [{"path": "a", "content": ""}]},
               root)["branch"].startswith("venice/refs/heads/main-rm-rf")
    # refused: escapes, protected folders, bad input, limits
    for bad, why in (("../evil.py", "not allowed"), ("/etc/passwd", "not allowed"), (".github/workflows/x.yml", "can only be changed by a person"),
                     (".git/config", "can only be changed by a person"), ("a/../../b", "not allowed"), ("a b.py", "not allowed"),
                     ("a\\..\\..\\b", "not allowed"), ("", "needs a 'path'")):
        refused({"title": "t", "files": [{"path": bad, "content": "x"}]}, root, why)
    refused({"title": "", "files": [{"path": "a", "content": ""}]}, root, "'title' is required")
    refused({"title": "t", "files": []}, root, "non-empty")
    refused({"title": "t", "files": [{"path": "a"}]}, root, "either 'content'")
    refused({"title": "t", "files": [{"path": "a", "content": "x", "content_b64": "eA=="}]}, root, "either 'content'")
    refused({"title": "t", "files": [{"path": "a", "content": "x", "edits": [{"find": "a", "replace": "b"}]}]}, root, "not both")
    refused({"title": "t", "files": [{"path": "a", "edits": [{"find": "", "replace": "b"}]}]}, root, "non-empty 'find'")
    refused({"title": "t", "files": [{"path": "a", "edits": []}]}, root, "list of 1-200")
    refused({"title": "t", "files": [{"path": "a", "content_b64": "%%%"}]}, root, "not valid base64")
    refused({"title": "t", "files": [{"path": "a", "content": "x" * (da.MAX_BYTES + 1)}]}, root, "at most")
    refused({"title": "t", "files": [{"path": f"f{i}", "content": ""} for i in range(51)]}, root, "at most 50")
    refused({"title": "t", "files": [{"path": "a", "content": ""}, {"path": "a", "content": ""}]}, root, "twice")
    # a symlink inside the repo pointing outside is not followed
    os.symlink(work, os.path.join(root, "link"))
    try:
        da.apply(run({"title": "t", "files": [{"path": "link/escape.txt", "content": "x"}]}, root), root)
        raise AssertionError("symlink escape should be refused")
    except da.PayloadError as e:
        assert "leaves the repository" in str(e)
    assert not os.path.exists(os.path.join(work, "escape.txt"))
    # command line: plan prints JSON, apply refuses with exit 2
    p = os.path.join(work, "p.json")
    json.dump({"title": "Plan me", "dry_run": True, "files": [{"path": "z.py", "content": "z"}]}, open(p, "w"))
    assert da.main(["plan", p]) == 0
    json.dump({"title": "x", "files": [{"path": ".github/workflows/a.yml", "content": "z"}]}, open(p, "w"))
    assert da.main(["apply", p, "--root", root]) == 2
    # the sender builds the same payload format (text and binary)
    pl2 = ds.build_payload(["satrx.py", "docs/img/a.png"], "Export", base=root, delete=["gone.py"], dry_run=True)
    assert pl2["files"][0] == {"path": "satrx.py", "content": "print('PD90')\n"}
    assert base64.b64decode(pl2["files"][1]["content_b64"]) == png and pl2["files"][2] == {"path": "gone.py", "delete": True}
    assert pl2["dry_run"] is True
    with open(p, "w") as f:
        json.dump(pl2, f)
    assert da.load(p)["branch"] == "venice/export"
    test_edits(root)
    test_bridge(work)
    print("Venice pipeline ok")


def test_edits(root):
    """Find/replace edits: exactly one match each, applied in order; nothing written if one fails."""
    big = os.path.join(root, "big.py")
    with open(big, "w") as f:
        f.write("def a():\n    return 1\n\ndef b():\n    return 1\n")
    pl = run({"title": "t", "files": [{"path": "big.py", "edits": [
        {"find": "def b():\n    return 1", "replace": "def b():\n    return 2"},
        {"find": "def a():", "replace": "def a_new():"}]}]}, root)
    assert da.apply(pl, root)[0][0] == "edited (2 changes)"
    assert open(big).read() == "def a_new():\n    return 1\n\ndef b():\n    return 2\n"
    for find, n in (("    return", 2), ("missing", 0), ("return 1", 1)):
        pl = run({"title": "t", "files": [{"path": "big.py", "edits": [{"find": find, "replace": "x"}]}]}, root)
        if n == 1:
            da.apply(pl, root)
            continue
        before = open(big).read()
        try:
            da.apply(pl, root)
            raise AssertionError("should be refused")
        except da.PayloadError as e:
            assert f"appears {n} times" in str(e), str(e)
        assert open(big).read() == before
    try:
        da.apply(run({"title": "t", "files": [{"path": "nofile.py", "edits": [{"find": "a", "replace": "b"}]}]}, root), root)
        raise AssertionError("edits on a missing file should be refused")
    except da.PayloadError as e:
        assert "existing file" in str(e)


def test_bridge(work):
    """The bridge against a fake Venice API: read a file, a wrong edit is refused and explained, the fixed one passes."""
    import http.server
    import threading
    repo = os.path.join(work, "bridge_repo")
    os.makedirs(repo)
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("# Tradewind\n")
    with open(os.path.join(repo, "satrx.py"), "w") as f:
        f.write("MODES = {95: 'PD120'}\n\n\ndef names():\n    return list(MODES.values())\n")
    seen = []

    def call(name, args, cid):
        return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    script = [
        {"role": "assistant", "content": None, "tool_calls": [call("read_file", {"path": "satrx.py"}, "c1")]},
        {"role": "assistant", "content": None, "tool_calls": [call("submit_change", {
            "title": "Add PD90", "branch": "venice/pd90",
            "files": [{"path": "satrx.py", "edits": [{"find": "MODES = {95: 'PD12'}", "replace": "x"}]}]}, "c2")]},
        {"role": "assistant", "content": None, "tool_calls": [call("submit_change", {
            "title": "Add PD90", "branch": "venice/pd90", "body": "Adds PD90.",
            "files": [{"path": "satrx.py", "edits": [{"find": "MODES = {95: 'PD120'}",
                                                       "replace": "MODES = {95: 'PD120', 99: 'PD90'}"}]}]}, "c3")]},
    ]

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path.endswith("/chat/completions") and self.headers["Authorization"] == "Bearer k"
            assert body["model"] == "m" and {t["function"]["name"] for t in body["tools"]} == {"read_file", "submit_change"}
            seen.append(body["messages"])
            out = json.dumps({"choices": [{"message": script[len(seen) - 1]}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    real = vb.BASE_URL
    vb.BASE_URL = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        change, done = vb.run("Add the PD90 mode", repo, "m", "k", log=lambda *a: None)
    finally:
        vb.BASE_URL = real
        srv.shutdown()
    assert len(seen) == 3 and "Add the PD90 mode" in seen[0][1]["content"] and "satrx.py" in seen[0][1]["content"]
    tool_msgs = [m for m in seen[1] if m.get("role") == "tool"]
    assert tool_msgs and "MODES = {95: 'PD120'}" in tool_msgs[0]["content"]          # the file was read
    refusal = [m for m in seen[2] if m.get("role") == "tool"][-1]["content"]
    assert refusal.startswith("REFUSED") and "appears 0 times" in refusal             # told what was wrong
    assert change["branch"] == "venice/pd90" and done[0][0] == "edited (1 change)"
    assert "99: 'PD90'" not in open(os.path.join(repo, "satrx.py")).read()            # checked on a copy only
    # the bridge's own checks
    assert vb.read_file(repo, "../etc/passwd").startswith("ERROR") and vb.read_file(repo, "nope.py").startswith("ERROR")
    assert vb.read_file(repo, ".git/config").startswith("ERROR")
    try:
        vb.check_change(repo, {"title": "t", "files": [{"path": "bad.py", "content": "def (:\n"}]})
        raise AssertionError("code that does not compile must be refused")
    except da.PayloadError as e:
        assert "does not compile" in str(e)
    assert vb._json_in_text('Here you go: {"title": "x", "files": []} done')["title"] == "x"
    assert vb._json_in_text("no json") is None


if __name__ == "__main__":
    main()
