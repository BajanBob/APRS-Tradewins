# Da Vinci → GitHub pipeline

Code written by the **Da Vinci** AI assistant goes straight into this repository, as a pull request you review.

```
Da Vinci  ──sendCode──▶  GitHub API (repository_dispatch "davinci-code")
                              │
                              ▼
          .github/workflows/davinci-intake.yml
            1. checks every path (no .github/, nothing outside the repo, ≤50 files, ≤1 MB each)
            2. writes the files to branch davinci/<name>   (never main)
            3. runs python test_modem.py
            4. opens a pull request (a draft marked FAILED if the self-test fails),
               or adds a comment if that branch already has one open
                              │
                              ▼
               you review and merge  ──▶  main builds Windows / Linux / Android as usual
```

## One-time set-up (about 5 minutes)

### 1. Let Actions open pull requests
Repository **Settings → Actions → General → Workflow permissions**: choose *Read and write permissions* and
tick **Allow GitHub Actions to create and approve pull requests**. Save.

### 2. Make a token for Da Vinci (this repository only)
GitHub → your picture → **Settings → Developer settings → Personal access tokens → Fine-grained tokens →
Generate new token**:

- Name: `Da Vinci - Tradewind`; Expiration: 90 days (renew when it runs out)
- Repository access: **Only select repositories → APRS-Tradewins**
- Repository permissions:
  - **Contents: Read and write** (needed to send — GitHub requires it for repository dispatch)
  - **Actions: Read** (to see the intake runs)
  - **Pull requests: Read** (to find the pull request)
  - Metadata: Read (set automatically)

Copy the token (`github_pat_...`). It is like a password: keep it only in Da Vinci's settings, never in code
or chat.

> The token can push to branches, but the workflow and your review are the gate: only pull requests you merge
> reach `main`. For extra safety add a branch protection rule on `main` (Settings → Branches) requiring a pull
> request before merging.

### 3. Connect Da Vinci

**If Da Vinci is a custom GPT in ChatGPT:** edit the GPT → **Configure → Actions → Create new action**
- **Authentication:** API Key → Auth type **Bearer** → paste the token.
- **Schema:** paste the contents of [`openapi.yaml`](openapi.yaml).
- **Instructions:** add the text of [`instructions.md`](instructions.md) to the GPT's instructions.
- Save, then ask it: *"List the files in the Tradewind repository."* to check the connection.

**Any other assistant or tool** that can make HTTPS requests uses the same call:

```
POST https://api.github.com/repos/BajanBob/APRS-Tradewins/dispatches
Authorization: Bearer <token>
Accept: application/vnd.github+json
{"event_type": "davinci-code",
 "client_payload": {"title": "Add PD90 SSTV mode",
                    "files": [{"path": "satrx.py", "content": "...complete file..."}]}}
```

**Exporting files Da Vinci gave you** (saved on your computer):

```
set GITHUB_TOKEN=github_pat_...          (Windows;  export GITHUB_TOKEN=... on Linux / Mac)
python tools/davinci_send.py --title "Add PD90 SSTV mode" satrx.py test_modem.py
python tools/davinci_send.py --title "Try it" --test-only satrx.py     (test only, no pull request)
python tools/davinci_send.py --title "Look" --dry-run satrx.py         (show what would be sent)
```

**By hand, no token:** Actions → **Da Vinci code intake** → *Run workflow* → paste the payload JSON.

## The payload

| field | | |
|---|---|---|
| `title` | required | pull request title |
| `files` | required | 1–50 of `{"path", "content"}` (text), `{"path", "content_b64"}` (binary) or `{"path", "delete": true}` |
| `branch` | optional | `davinci/<name>`; reuse it to add to an open pull request (anything else is put under `davinci/`) |
| `message` | optional | commit message |
| `body` | optional | pull request description |
| `dry_run` | optional | `true` = apply and self-test, push nothing |

Refused: paths starting with `/`, containing `..`, or in `.git/` or `.github/`; more than 50 files; files over
1 MB. Workflows can only be changed by a person.
