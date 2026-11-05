#!/usr/bin/env python3
"""Self-test for the Da Vinci intake (tools/davinci_apply.py, tools/davinci_send.py). Run: python tools/test_davinci_apply.py"""
import base64
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import davinci_apply as da  # noqa: E402
import davinci_send as ds  # noqa: E402


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
    work = tempfile.mkdtemp(prefix="davinci_")
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
    assert pl["branch"] == "davinci/add-pd90-sstv-mode" and pl["message"] == "Add PD90 SSTV mode!"
    done = da.apply(pl, root)
    assert [d[0] for d in done] == ["added", "added", "deleted"], done
    assert open(os.path.join(root, "satrx.py")).read() == "print('PD90')\n"
    assert open(os.path.join(root, "docs/img/a.png"), "rb").read() == png and not os.path.exists(os.path.join(root, "old.py"))
    # branch is always under davinci/ and cleaned
    assert run({"title": "t", "branch": "main", "files": [{"path": "a", "content": ""}]}, root)["branch"] == "davinci/main"
    assert run({"title": "t", "branch": "davinci/Fix SSTV/../x", "files": [{"path": "a", "content": ""}]},
               root)["branch"] == "davinci/fix-sstv/x"
    assert run({"title": "t", "branch": "refs/heads/main;rm -rf /", "files": [{"path": "a", "content": ""}]},
               root)["branch"].startswith("davinci/refs/heads/main-rm-rf")
    # refused: escapes, protected folders, bad input, limits
    for bad, why in (("../evil.py", "not allowed"), ("/etc/passwd", "not allowed"), (".github/workflows/x.yml", "can only be changed by a person"),
                     (".git/config", "can only be changed by a person"), ("a/../../b", "not allowed"), ("a b.py", "not allowed"),
                     ("a\\..\\..\\b", "not allowed"), ("", "needs a 'path'")):
        refused({"title": "t", "files": [{"path": bad, "content": "x"}]}, root, why)
    refused({"title": "", "files": [{"path": "a", "content": ""}]}, root, "'title' is required")
    refused({"title": "t", "files": []}, root, "non-empty")
    refused({"title": "t", "files": [{"path": "a"}]}, root, "either 'content'")
    refused({"title": "t", "files": [{"path": "a", "content": "x", "content_b64": "eA=="}]}, root, "either 'content'")
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
    assert da.load(p)["branch"] == "davinci/export"
    print("Da Vinci intake ok")


if __name__ == "__main__":
    main()
