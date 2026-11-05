# Venice.ai → GitHub pipeline

Give **Venice.ai** a coding task in plain words; the change comes back as a pull request you review.

```
 you: "Add the PD90 SSTV mode to satrx.py with a test"
   │   (Actions → Venice code intake → Run workflow, or tools/venice_bridge.py on a computer)
   ▼
 tools/venice_bridge.py ──── Venice API (api.venice.ai, OpenAI-compatible, function calling)
   │  Venice gets the task + the file list, reads the files it needs (read_file),
   │  and hands in its change (submit_change): whole files, or exact find/replace edits for big files.
   │  The change is tried on a scratch copy; a wrong edit is sent back to Venice to fix (3 tries).
   ▼
 .github/workflows/venice-intake.yml
   1. checks every path (no .github/, nothing outside the repo, ≤50 files, ≤1 MB each)
   2. writes the change to branch venice/<name>   (never main)
   3. runs python test_modem.py
   4. opens a pull request (a draft marked FAILED if the self-test fails),
      or comments on it if that branch already has one open
   ▼
 you review and merge ──▶ main builds Windows / Linux / Android as usual
```

Venice's chat app cannot connect to GitHub by itself, so the bridge talks to Venice through its API for you.

## One-time set-up (about 5 minutes)

### 1. A Venice API key
venice.ai → **Settings → API** → create a key. API use is billed to your Venice account (Pro credits or
pay-as-you-go).

### 2. Let GitHub use it
In this repository: **Settings → Secrets and variables → Actions**
- **Secrets** tab → *New repository secret*: name `VENICE_API_KEY`, value = the key.
- Optional, **Variables** tab → `VENICE_MODEL` to choose the model. Default:
  `qwen3-coder-480b-a35b-instruct-turbo` (a Venice coding model with function calling). Any Venice text model
  that supports function calling works, e.g. `kimi-k2-7-code` or `openai-gpt-53-codex`.

### 3. Let Actions open pull requests
**Settings → Actions → General → Workflow permissions**: *Read and write permissions* and tick
**Allow GitHub Actions to create and approve pull requests**. Save.

That is all for running it from GitHub.

## Using it

**From GitHub (phone or computer):** Actions → **Venice code intake** → *Run workflow* → type the task →
Run. Tick *Test only* to see whether the self-test passes without opening a pull request. The pull request
appears under **Pull requests** when the run finishes (usually 3-10 minutes).

Good tasks are specific: *"In satrx.py add the PD90 SSTV mode (VIS 99, 320x256, 170.240 ms per line pair)
to SSTV_MODES and the encoder/decoder, with a round-trip test in test_modem.py."*

**From a computer** (needs Python):

```
set VENICE_API_KEY=...                         (Windows;  export VENICE_API_KEY=... on Linux / Mac)
python tools/venice_bridge.py "your task" --dry-run      show what Venice wrote, send nothing
set GITHUB_TOKEN=github_pat_...
python tools/venice_bridge.py "your task" --send         open the pull request
```

The computer route needs a GitHub token: GitHub → Settings → Developer settings → **Fine-grained tokens** →
only the **APRS-Tradewins** repository, permission **Contents: Read and write** (GitHub requires it for the
dispatch call). Keep it like a password.

**Code you copied out of Venice's chat** (saved as files):

```
python tools/venice_send.py --title "Add PD90 SSTV mode" satrx.py test_modem.py
```

## The payload (what the intake accepts)

| field | | |
|---|---|---|
| `title` | required | pull request title |
| `files` | required | 1–50 of `{"path", "content"}` (whole text file), `{"path", "content_b64"}` (binary), `{"path", "edits": [{"find", "replace"}]}` (each `find` must appear exactly once) or `{"path", "delete": true}` |
| `branch` | optional | `venice/<name>`; reuse it to add to an open pull request |
| `message` | optional | commit message |
| `body` | optional | pull request description |
| `dry_run` | optional | `true` = apply and self-test, push nothing |

Refused: paths starting with `/`, containing `..`, or in `.git/` or `.github/`; more than 50 files; files over
1 MB; edits whose `find` text is missing or not unique. Workflows can only be changed by a person, and every
change goes through your review.
