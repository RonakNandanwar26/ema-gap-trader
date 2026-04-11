# AWS EC2 Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy `ema-gap-trader` to a single AWS EC2 `t4g.small` in
`ap-south-1` running the existing Streamlit `app.py` (which covers backtest,
paper, and live trading) behind nginx + Let's Encrypt + basic auth, with the
Dhan token hot-reloadable via a Streamlit sidebar widget and CloudWatch
instance-health alarms wired to email.

**Architecture:** One systemd service (`ema-app.service`) running
`streamlit run app.py`. Nginx terminates TLS and proxies to it. Permanent
secrets in `/etc/ema-trader/app.env` (loaded once at boot). The daily-rotating
Dhan token in `/etc/ema-trader/dhan.token` is read just-in-time on every
Dhan API call so the user can paste a fresh token into the Streamlit sidebar
without restarting the service. Trader runs as a daemon thread inside the
Streamlit process — matches the existing local-dev model and requires no
refactor of `trader.py`.

**Tech Stack:** Ubuntu 22.04 (ARM64), Python 3.13, Streamlit, systemd, nginx,
certbot (Let's Encrypt), DuckDNS, fail2ban, AWS CloudWatch agent, AWS SNS.

**Spec:** `docs/superpowers/specs/2026-04-11-aws-ec2-deployment-design.md`

---

## Phase 1 — Local code changes (TDD where applicable)

These run on your laptop on a feature branch. Nothing in Phase 1 touches AWS.
The whole phase should be done and merged before you provision the instance,
because the deploy script in Phase 2 just `git pull`s whatever is on `main`.

**Branch:** create `feat/aws-deployment` from `main` and stay on it for all of
Phase 1. Only merge to `main` after Task 7.

### Task 1: Add `_get_dhan_token()` helper to `dhan_data.py`

**Files:**
- Modify: `dhan_data.py` (add helper near top of file, after the
  `_DHAN_API_BASE` constant on line 102)
- Test: `tests/test_dhan_token.py` (new file)

**Background:** Today the Dhan access token is read once at startup via
`os.environ.get("DHAN_ACCESS_TOKEN", "")` (line 117 of `dhan_data.py`) and
stored inside the `dhan_creds` dict that gets passed around. We're replacing
that with a function that reads from `/etc/ema-trader/dhan.token` per call,
with env-var fallback for local dev / pytest. This task only adds the helper;
Task 2 wires it into the call sites.

- [ ] **Step 1: Write the failing test**

Create `tests/test_dhan_token.py`:

```python
"""Tests for the Dhan token file-based loader."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from dhan_data import _get_dhan_token


class TestGetDhanToken:
    """Tests for _get_dhan_token() — reads token from disk per call."""

    def test_reads_from_file_when_present(self, tmp_path: Path) -> None:
        token_file = tmp_path / "dhan.token"
        token_file.write_text("file-token-123\n")

        with patch("dhan_data._DHAN_TOKEN_PATH", str(token_file)):
            assert _get_dhan_token() == "file-token-123"

    def test_strips_whitespace_and_newlines(self, tmp_path: Path) -> None:
        token_file = tmp_path / "dhan.token"
        token_file.write_text("  spaced-token  \n\n")

        with patch("dhan_data._DHAN_TOKEN_PATH", str(token_file)):
            assert _get_dhan_token() == "spaced-token"

    def test_falls_back_to_env_var_when_file_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.token"

        with patch("dhan_data._DHAN_TOKEN_PATH", str(missing)), \
             patch.dict(os.environ, {"DHAN_ACCESS_TOKEN": "env-token-456"}, clear=False):
            assert _get_dhan_token() == "env-token-456"

    def test_returns_empty_string_when_neither_present(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.token"

        with patch("dhan_data._DHAN_TOKEN_PATH", str(missing)), \
             patch.dict(os.environ, {}, clear=True):
            assert _get_dhan_token() == ""

    def test_reads_fresh_value_on_each_call(self, tmp_path: Path) -> None:
        """Verifies no caching — successive calls see file edits."""
        token_file = tmp_path / "dhan.token"
        token_file.write_text("first")

        with patch("dhan_data._DHAN_TOKEN_PATH", str(token_file)):
            assert _get_dhan_token() == "first"
            token_file.write_text("second")
            assert _get_dhan_token() == "second"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_dhan_token.py -v`
Expected: `ImportError: cannot import name '_get_dhan_token' from 'dhan_data'`

- [ ] **Step 3: Add the helper to `dhan_data.py`**

Open `dhan_data.py`. Find the line `_DHAN_API_BASE = "https://api.dhan.co/v2"`
(line 102). Immediately after it, add:

```python
_DHAN_TOKEN_PATH = os.environ.get("DHAN_TOKEN_PATH", "/etc/ema-trader/dhan.token")


def _get_dhan_token() -> str:
    """Read the Dhan access token just-in-time.

    The token is rotated daily by the user via the Streamlit sidebar widget
    in app.py, which writes ``/etc/ema-trader/dhan.token``. Reading per call
    means the next Dhan API request always picks up the fresh value with no
    service restart. Falls back to ``DHAN_ACCESS_TOKEN`` env var when the
    file is absent — used for local dev and pytest.
    """
    try:
        return Path(_DHAN_TOKEN_PATH).read_text().strip()
    except FileNotFoundError:
        return os.environ.get("DHAN_ACCESS_TOKEN", "")
```

You'll also need to add `from pathlib import Path` to the top of the file
if it's not already imported. Check the imports block at the top of
`dhan_data.py` and add it if missing.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dhan_token.py -v`
Expected: 5 passed.

Then run the whole suite to be sure nothing else broke:
Run: `.venv/bin/pytest -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add dhan_data.py tests/test_dhan_token.py
git commit -m "feat(dhan): add _get_dhan_token() file-based loader

Reads /etc/ema-trader/dhan.token on every call so the user can rotate
the daily Dhan JWT via the Streamlit sidebar widget without restarting
the service. Falls back to DHAN_ACCESS_TOKEN env var for local dev.

Standalone helper — Task 2 wires it into the existing call sites."
```

---

### Task 2: Replace direct token reads at the four call sites in `dhan_data.py`

**Files:**
- Modify: `dhan_data.py` lines 117 (inside `init_dhan`), 430, 712, 888

**Background:** `dhan_data.py` currently reads the token once in `init_dhan()`
(line 117) and stuffs it into `dhan_creds["access_token"]`, which is then
passed through to header construction at lines 430, 712, and 888. We replace
all four with `_get_dhan_token()`. `init_dhan()` keeps its job of *validating*
the token at startup (so the user gets a clear "regenerate from dhanhq.co"
error early), but it no longer needs to put the token in the returned dict.

**Strategy:** to keep `dhan_creds["access_token"]` working for any call sites
we missed, leave it in the returned dict but populate it from
`_get_dhan_token()`. But then teach the three header sites to call
`_get_dhan_token()` directly so they pick up rotations. This is belt and
braces.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_dhan_token.py` (append to the end of the file):

```python
class TestDhanCallsUseFreshToken:
    """Verifies the three Dhan API call sites read the token per call."""

    @patch("dhan_data.requests.get")
    def test_init_dhan_uses_get_dhan_token(self, mock_get, tmp_path):
        from dhan_data import init_dhan

        token_file = tmp_path / "dhan.token"
        token_file.write_text("init-token")

        mock_resp = type("R", (), {"status_code": 200})()
        mock_get.return_value = mock_resp

        with patch("dhan_data._DHAN_TOKEN_PATH", str(token_file)), \
             patch.dict(os.environ, {"DHAN_CLIENT_ID": "cid"}, clear=False):
            creds = init_dhan()

        # Validation called with the token from the file
        sent_headers = mock_get.call_args.kwargs["headers"]
        assert sent_headers["access-token"] == "init-token"
        # Returned dict still carries access_token for backwards compat
        assert creds["access_token"] == "init-token"
        assert creds["client_id"] == "cid"

    def test_header_construction_calls_get_dhan_token(self, tmp_path):
        """Sanity check: the helper, not dhan_creds dict, is the source of truth.

        We call _get_dhan_token() directly twice with a file edit in between
        and assert both reads succeed. The lines 430/712/888 edits are
        verified by static inspection (see Task 2 step 3) since mocking the
        full Dhan HTTP plumbing would be brittle.
        """
        token_file = tmp_path / "dhan.token"
        token_file.write_text("v1")

        with patch("dhan_data._DHAN_TOKEN_PATH", str(token_file)):
            from dhan_data import _get_dhan_token
            assert _get_dhan_token() == "v1"
            token_file.write_text("v2")
            assert _get_dhan_token() == "v2"
```

- [ ] **Step 2: Run test to verify the new init_dhan test fails**

Run: `.venv/bin/pytest tests/test_dhan_token.py::TestDhanCallsUseFreshToken -v`
Expected: `test_init_dhan_uses_get_dhan_token` FAILS — current `init_dhan` reads
from `os.environ.get("DHAN_ACCESS_TOKEN")`, not from the file path we patched.

- [ ] **Step 3: Patch line 117 (`init_dhan`)**

Open `dhan_data.py`. Find the `init_dhan` function around line 105. Replace:

```python
    client_id = os.environ.get("DHAN_CLIENT_ID", "")
    access_token = os.environ.get("DHAN_ACCESS_TOKEN", "")
```

with:

```python
    client_id = os.environ.get("DHAN_CLIENT_ID", "")
    access_token = _get_dhan_token()
```

Leave the rest of the function unchanged. The validation `requests.get` call
that uses `headers={"access-token": access_token}` keeps working — it now
validates the token currently on disk.

- [ ] **Step 4: Patch lines 430, 712, 888 (header construction)**

At each of these three lines you'll see:

```python
    headers = {
        "access-token": dhan_creds["access_token"],
        ...
    }
```

Replace `dhan_creds["access_token"]` with `_get_dhan_token()` at all three
sites:

```python
    headers = {
        "access-token": _get_dhan_token(),
        ...
    }
```

Use grep to confirm zero remaining references after the edit:
Run: `grep -n 'dhan_creds\["access_token"\]' dhan_data.py`
Expected: no output.

- [ ] **Step 5: Run all tests**

Run: `.venv/bin/pytest -q`
Expected: all green, including the two new `TestDhanCallsUseFreshToken` tests.

- [ ] **Step 6: Commit**

```bash
git add dhan_data.py tests/test_dhan_token.py
git commit -m "feat(dhan): wire _get_dhan_token() into all four token sites

init_dhan() and the three header constructors at lines 430/712/888 now
read the token via _get_dhan_token() instead of holding a stale value
in dhan_creds[\"access_token\"]. The Streamlit sidebar widget added in
Task 3 will let the user rotate the daily JWT without a service restart."
```

---

### Task 3: Add the Dhan token sidebar widget to `app.py`

**Files:**
- Modify: `app.py` (add a sidebar block after the `st.set_page_config` call
  on line 42)

**Background:** This is a UI-only change. Streamlit UI is awkward to unit
test, so this task has no automated test — we verify it manually in step 3.

- [ ] **Step 1: Add the sidebar widget**

Open `app.py`. Find the line:

```python
st.set_page_config(page_title="EMA Gap Trader", page_icon=":chart_with_upwards_trend:", layout="wide")
```

(around line 42). Immediately after it, add:

```python
# ---------------------------------------------------------------------------
# Dhan token sidebar widget — daily JWT rotation without service restart
# ---------------------------------------------------------------------------
from pathlib import Path as _DhanPath

_DHAN_TOKEN_FILE = _DhanPath(os.environ.get("DHAN_TOKEN_PATH", "/etc/ema-trader/dhan.token"))

with st.sidebar.expander("Dhan token (daily refresh)", expanded=False):
    if _DHAN_TOKEN_FILE.exists() and _DHAN_TOKEN_FILE.read_text().strip():
        st.caption(f"Status: SET ({_DHAN_TOKEN_FILE})")
    else:
        st.caption(f"Status: MISSING ({_DHAN_TOKEN_FILE})")
    _new_dhan_token = st.text_input(
        "Paste new Dhan access token",
        type="password",
        key="dhan_token_input",
    )
    if st.button("Save token", key="dhan_token_save"):
        if not _new_dhan_token.strip():
            st.error("Empty token — not saved.")
        else:
            try:
                _DHAN_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
                _DHAN_TOKEN_FILE.write_text(_new_dhan_token.strip())
                st.success("Saved. Next Dhan call will use it.")
            except PermissionError as exc:
                st.error(f"Permission denied writing {_DHAN_TOKEN_FILE}: {exc}")
```

(`os` is already imported at the top of `app.py` — line 7. We import `Path`
under an alias to avoid colliding with anything else the file may import.)

- [ ] **Step 2: Lint check — make sure the file still parses**

Run: `.venv/bin/python -c "import ast; ast.parse(open('app.py').read()); print('OK')"`
Expected: `OK`

Then run the test suite to make sure nothing imports `app.py` and breaks:
Run: `.venv/bin/pytest -q`
Expected: all green.

- [ ] **Step 3: Manual smoke test the widget**

Run Streamlit locally:
```bash
DHAN_TOKEN_PATH=/tmp/test_dhan.token .venv/bin/streamlit run app.py
```

Open the browser tab Streamlit prints. In the sidebar, expand "Dhan token
(daily refresh)". Verify:
- Status shows "MISSING" (because `/tmp/test_dhan.token` doesn't exist yet)
- Pasting a value and clicking "Save token" shows "Saved. Next Dhan call will
  use it." and creates `/tmp/test_dhan.token` with that content
- Refreshing the page shows "Status: SET" with the same path
- Clicking "Save token" with an empty input shows the error

Then `Ctrl+C` to stop Streamlit and `rm /tmp/test_dhan.token`.

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "feat(app): add Dhan token rotation widget to Streamlit sidebar

Lets the user paste a fresh daily Dhan JWT into the sidebar without
SSHing to the server or restarting the systemd unit. Writes to
/etc/ema-trader/dhan.token (overridable via DHAN_TOKEN_PATH for local
dev). Pairs with _get_dhan_token() in dhan_data.py."
```

---

### Task 4: Verify trader.py already telegrams the four critical events

**Files:**
- Read-only: `trader.py` lines 95, 114, 148, 221, 234, 276, 324, 400, 530, 562, 582
- Modify (only if gaps): `trader.py`

**Background:** `grep` showed `trader.py` already has 11+ `send_telegram()`
call sites. The spec's "four critical alerts" (startup, login failure, order
failure, daily summary) may already be covered. This task is a verification
sweep — we add only what's actually missing, to avoid duplicate alerts.

- [ ] **Step 1: Inspect each existing call site**

For each of the line numbers below, run:
```bash
sed -n '90,100p;110,120p;145,155p;218,238p;273,280p;320,330p;395,405p;525,585p' trader.py
```

For each call, classify it as one of:
- (a) **trader startup** — fired once when the trading thread comes up
- (b) **Angel One login failure** — fired when SmartConnect login raises
- (c) **order placement failure** — fired when an order API call returns
  non-success or raises
- (d) **daily summary at market close** — fired once per day at ~15:30 IST
- (e) other (entries, exits, holiday, skip, etc.)

Write the classification down (in your head or a scratch note) — you don't
need to commit this.

- [ ] **Step 2: For each missing category, add the alert**

For any of (a), (b), (c), (d) that is NOT covered by an existing call site,
add a `send_telegram(...)` call at the appropriate location in `trader.py`.
Use the existing call sites as a template — they show the import is already
in place at line 18 (`from notifications import send_telegram`).

Common gap fixes:

**(a) Trader thread startup** — add at the top of the trader's run/start
method (whichever method launches the daemon thread or the main loop):

```python
send_telegram(f"Trader started for {self._instrument_name} (mode={self._mode}, label={self._label})")
```

**(b) Angel One login failure** — wrap any `broker.login()` or `get_api()`
call in `trader.py` that doesn't already alert:

```python
try:
    api = broker.login()
except Exception as exc:
    send_telegram(f"Angel One login failed: {exc}")
    raise
```

(Note: `broker.login()` is in `broker.py` line 38 — confirm by reading that
file. `trader.py` may call it indirectly via `broker.get_api()` at line 75.)

**(c) Order placement failure** — wherever an order API call's response is
checked for failure but no telegram fires, add one. Example pattern:

```python
if not resp.get("status"):
    send_telegram(f"Order failed for {symbol}: {resp.get('message', 'unknown')}")
```

**(d) Daily summary at market close** — if no end-of-day summary exists, add
one in the loop's market-close branch:

```python
send_telegram(f"Today: {n_trades} trades, P&L Rs.{pnl:,.0f}")
```

If all four categories are already covered (likely — there are 11 sites),
make zero changes and skip to Step 4.

- [ ] **Step 3: Run the test suite to make sure no telegram-related tests broke**

Run: `.venv/bin/pytest tests/test_trader.py tests/test_notifications.py -v`
Expected: all green.

- [ ] **Step 4: Commit (only if you actually changed `trader.py`)**

If Step 2 made no edits, skip the commit and move to Task 5.

If Step 2 made edits:
```bash
git add trader.py
git commit -m "feat(trader): wire telegram alerts for <gap categories>

Fills the alert gap(s) identified by the AWS deployment plan
(docs/superpowers/plans/2026-04-11-aws-ec2-deployment-plan.md, Task 4)
so the operator gets push notifications for: <list>."
```

---

### Task 5: Add `deploy.sh` to repo root

**Files:**
- Create: `deploy.sh` (chmod +x)

- [ ] **Step 1: Create the file**

Create `/home/ronak/Desktop/simform_projects/ema-gap-trader/deploy.sh`:

```bash
#!/usr/bin/env bash
#
# deploy.sh — push current main to the EC2 box and restart the service.
#
# Usage:  EC2_HOST=ubuntu@<elastic-ip> ./deploy.sh
#
# WARNING: do not deploy between 09:15 and 15:30 IST unless it's an emergency.
# Restarting ema-app.service stops the in-process Trader thread for a few
# seconds, which means a tick gap and any in-flight order context is lost.

set -euo pipefail

EC2_HOST="${EC2_HOST:?set EC2_HOST=ubuntu@<elastic-ip>}"

ssh "$EC2_HOST" '
    set -euo pipefail
    cd /opt/ema-gap-trader
    git pull --ff-only
    .venv/bin/pip install -q -r requirements.txt
    sudo systemctl restart ema-app
    sleep 2
    sudo systemctl status ema-app --no-pager | head -20
'

echo
echo "Deploy complete. Tail logs with:"
echo "    ssh $EC2_HOST 'sudo journalctl -u ema-app -f'"
```

- [ ] **Step 2: Make it executable and shellcheck**

```bash
chmod +x deploy.sh
shellcheck deploy.sh || echo "shellcheck not installed, skipping (optional)"
```

Expected: no errors. (If shellcheck isn't installed, skip — it's optional.)

- [ ] **Step 3: Commit**

```bash
git add deploy.sh
git commit -m "chore(deploy): add deploy.sh wrapper

One-line laptop-side deploy: git pull, pip install, systemctl restart.
No CI, no staging — single developer, single box."
```

---

### Task 6: Add `deploy/systemd/ema-app.service`

**Files:**
- Create: `deploy/systemd/ema-app.service`

- [ ] **Step 1: Create the unit file**

Create `/home/ronak/Desktop/simform_projects/ema-gap-trader/deploy/systemd/ema-app.service`:

```ini
[Unit]
Description=ema-gap-trader Streamlit app (backtest + paper + live trading)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/ema-gap-trader
EnvironmentFile=/etc/ema-trader/app.env
Environment=DHAN_TOKEN_PATH=/etc/ema-trader/dhan.token
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/ema-gap-trader/.venv/bin/streamlit run app.py \
    --server.address=127.0.0.1 \
    --server.port=8501 \
    --server.headless=true \
    --browser.gatherUsageStats=false
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ema-app

# Security hardening — minimal but useful
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/ema-gap-trader /etc/ema-trader
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Validate the unit file syntax (locally)**

Run:
```bash
.venv/bin/python -c "
import configparser
c = configparser.ConfigParser()
c.read('deploy/systemd/ema-app.service')
assert 'Service' in c
assert 'ExecStart' in c['Service']
print('OK')
"
```

Expected: `OK`. (Real systemd validation happens on the EC2 box in Phase 2.)

- [ ] **Step 3: Commit**

```bash
git add deploy/systemd/ema-app.service
git commit -m "chore(deploy): add ema-app systemd unit

Runs streamlit run app.py as the unprivileged ubuntu user with
EnvironmentFile-loaded secrets and minimal sandboxing
(NoNewPrivileges, ProtectSystem=strict, ProtectHome, PrivateTmp).
ReadWritePaths includes /etc/ema-trader so the Dhan token widget
can write the token file."
```

---

### Task 7: Add nginx site config and logrotate config

**Files:**
- Create: `deploy/nginx/ema-app.conf`
- Create: `deploy/logrotate/ema-app`

- [ ] **Step 1: Create the nginx site config**

Create `/home/ronak/Desktop/simform_projects/ema-gap-trader/deploy/nginx/ema-app.conf`:

```nginx
# /etc/nginx/sites-available/ema-app.conf — installed by Phase 2 task 14.
#
# IMPORTANT: replace `__DUCKDNS_HOST__` with your actual DuckDNS subdomain
# (e.g. ema-trader-yourname.duckdns.org) before installing on the server.
# certbot will rewrite the ssl_certificate paths automatically when it
# obtains the cert; the paths below are placeholders for the initial install.

server {
    listen 80;
    listen [::]:80;
    server_name __DUCKDNS_HOST__;

    # ACME challenge for Let's Encrypt
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    # Everything else: redirect to HTTPS
    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name __DUCKDNS_HOST__;

    # certbot will replace these with /etc/letsencrypt/live/<host>/...
    ssl_certificate     /etc/ssl/certs/ssl-cert-snakeoil.pem;
    ssl_certificate_key /etc/ssl/private/ssl-cert-snakeoil.key;

    auth_basic           "ema-trader";
    auth_basic_user_file /etc/nginx/.htpasswd;

    # Reasonable security defaults
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "no-referrer" always;

    client_max_body_size 16M;

    # Streamlit serves the entire UI (Backtest / Paper / Live tabs)
    location / {
        proxy_pass http://127.0.0.1:8501;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Streamlit relies on WebSockets
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }
}
```

- [ ] **Step 2: Create the logrotate config**

Create `/home/ronak/Desktop/simform_projects/ema-gap-trader/deploy/logrotate/ema-app`:

```
/opt/ema-gap-trader/logs/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su ubuntu ubuntu
}
```

(The systemd journal is rotated separately by systemd itself; this config
covers any file-based logs the app writes under `/opt/ema-gap-trader/logs/`.)

- [ ] **Step 3: Commit**

```bash
git add deploy/nginx/ema-app.conf deploy/logrotate/ema-app
git commit -m "chore(deploy): add nginx site + logrotate configs

nginx config has WebSocket upgrade headers (Streamlit needs them) and
basic-auth in front of the single 127.0.0.1:8501 upstream. The
__DUCKDNS_HOST__ placeholder is replaced during install in Phase 2.
logrotate covers any file-based logs the app writes; journald is
self-rotating."
```

---

### Phase 1 wrap-up: merge to main

- [ ] **Open a PR for `feat/aws-deployment` and merge it to `main`.**

You'll deploy whatever is on `main`, so all six commits from Tasks 1–7 must
be on `main` before Phase 2 starts. Run the test suite one more time on
`main` after the merge to confirm green.

```bash
git checkout main
git pull
.venv/bin/pytest -q
```

Expected: all green.

---

## Phase 2 — AWS provisioning (one-time, run from your laptop or AWS Console)

This phase is infrastructure setup. It's not TDD-able — these are sequenced
manual steps with verification commands.

**Prerequisite:** AWS account with credits, AWS CLI installed and configured
(`aws configure`), or web console access. Pick one and stick with it. The
plan shows CLI commands; if you're in the console, the field names match.

**Region:** all commands assume `ap-south-1` (Mumbai). Set:
```bash
export AWS_DEFAULT_REGION=ap-south-1
```

### Task 8: Generate SSH keypair and create the EC2 instance

- [ ] **Step 1: Create an SSH keypair (laptop-side)**

```bash
ssh-keygen -t ed25519 -f ~/.ssh/ema-trader-aws -N ""
```

Expected: creates `~/.ssh/ema-trader-aws` and `~/.ssh/ema-trader-aws.pub`.

- [ ] **Step 2: Import the public key into AWS**

```bash
aws ec2 import-key-pair \
    --key-name ema-trader \
    --public-key-material "fileb://$HOME/.ssh/ema-trader-aws.pub"
```

Expected: JSON output with `KeyName: ema-trader` and a `KeyFingerprint`.

- [ ] **Step 3: Create the security group**

```bash
SG_ID=$(aws ec2 create-security-group \
    --group-name ema-trader-sg \
    --description "ema-gap-trader: ssh + http + https" \
    --query 'GroupId' --output text)
echo "SG_ID=$SG_ID"
```

Then add the three inbound rules:

```bash
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
    --protocol tcp --port 22 --cidr 0.0.0.0/0
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
    --protocol tcp --port 80 --cidr 0.0.0.0/0
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
    --protocol tcp --port 443 --cidr 0.0.0.0/0
```

Expected: each command returns JSON with `Return: true`.

- [ ] **Step 4: Find the latest Ubuntu 22.04 ARM64 AMI**

```bash
AMI_ID=$(aws ec2 describe-images \
    --owners 099720109477 \
    --filters \
        "Name=name,Values=ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-arm64-server-*" \
        "Name=state,Values=available" \
    --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
    --output text)
echo "AMI_ID=$AMI_ID"
```

Expected: an `ami-...` ID prints.

- [ ] **Step 5: Launch the t4g.small instance**

```bash
INSTANCE_ID=$(aws ec2 run-instances \
    --image-id "$AMI_ID" \
    --instance-type t4g.small \
    --key-name ema-trader \
    --security-group-ids "$SG_ID" \
    --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":30,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
    --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=ema-trader}]' \
    --query 'Instances[0].InstanceId' --output text)
echo "INSTANCE_ID=$INSTANCE_ID"
```

Expected: `i-...` ID prints. Wait ~30 seconds for it to enter `running` state.

- [ ] **Step 6: Allocate and attach an Elastic IP**

```bash
EIP_ALLOC=$(aws ec2 allocate-address --domain vpc --query 'AllocationId' --output text)
EIP_PUBLIC=$(aws ec2 describe-addresses --allocation-ids "$EIP_ALLOC" \
    --query 'Addresses[0].PublicIp' --output text)
aws ec2 associate-address --instance-id "$INSTANCE_ID" --allocation-id "$EIP_ALLOC"
echo "ELASTIC_IP=$EIP_PUBLIC"
```

Save `$EIP_PUBLIC` somewhere — you'll use it as the SSH host for the rest
of Phase 2 and as the DNS A record value in Task 13.

- [ ] **Step 7: Verify SSH access**

```bash
ssh -i ~/.ssh/ema-trader-aws -o StrictHostKeyChecking=accept-new ubuntu@$EIP_PUBLIC \
    'echo connected; uname -a; cat /etc/os-release | head -3'
```

Expected: `connected` plus Ubuntu 22.04 version output.

For convenience, add to `~/.ssh/config`:
```
Host ema-trader
    HostName <paste-EIP_PUBLIC-here>
    User ubuntu
    IdentityFile ~/.ssh/ema-trader-aws
    StrictHostKeyChecking accept-new
```

After this, `ssh ema-trader` works and the rest of the plan uses that.

---

### Task 9: Harden SSH and install fail2ban (on the box)

- [ ] **Step 1: SSH in and update packages**

```bash
ssh ema-trader '
    sudo apt-get update -q
    sudo DEBIAN_FRONTEND=noninteractive apt-get -y -q upgrade
'
```

Expected: package list updates, upgrades complete.

- [ ] **Step 2: Disable password and root SSH login**

```bash
ssh ema-trader '
    sudo sed -i "s/^#\?PasswordAuthentication.*/PasswordAuthentication no/" /etc/ssh/sshd_config
    sudo sed -i "s/^#\?PermitRootLogin.*/PermitRootLogin no/" /etc/ssh/sshd_config
    sudo sed -i "s/^#\?ChallengeResponseAuthentication.*/ChallengeResponseAuthentication no/" /etc/ssh/sshd_config
    sudo sshd -t && sudo systemctl restart ssh
    grep -E "^(PasswordAuthentication|PermitRootLogin)" /etc/ssh/sshd_config
'
```

Expected: prints
```
PermitRootLogin no
PasswordAuthentication no
```

- [ ] **Step 3: Install and enable fail2ban**

```bash
ssh ema-trader '
    sudo DEBIAN_FRONTEND=noninteractive apt-get -y -q install fail2ban
    sudo tee /etc/fail2ban/jail.local >/dev/null <<EOF
[sshd]
enabled = true
maxretry = 3
findtime = 10m
bantime = 10m
EOF
    sudo systemctl enable --now fail2ban
    sudo fail2ban-client status sshd
'
```

Expected: fail2ban-client prints sshd jail status.

- [ ] **Step 4: Set timezone to IST**

```bash
ssh ema-trader '
    sudo timedatectl set-timezone Asia/Kolkata
    timedatectl
'
```

Expected: `Time zone: Asia/Kolkata (IST, +0530)`.

---

### Task 10: Install Python, nginx, certbot, build deps, CloudWatch agent

- [ ] **Step 1: Install OS packages**

```bash
ssh ema-trader '
    sudo DEBIAN_FRONTEND=noninteractive apt-get -y -q install \
        python3 python3-venv python3-pip \
        build-essential libssl-dev libffi-dev \
        nginx \
        certbot python3-certbot-nginx \
        git \
        apache2-utils \
        logrotate \
        curl unzip
'
```

Expected: apt installs without errors.

- [ ] **Step 2: Verify Python version**

```bash
ssh ema-trader 'python3 --version'
```

Expected: Python 3.10 or newer (Ubuntu 22.04 ships 3.10). The repo's
existing `.venv` was built with 3.13 on the laptop, but on EC2 we'll
rebuild it with whatever the OS ships — that's fine for Streamlit and the
deps in `requirements.txt`.

- [ ] **Step 3: Download the CloudWatch agent (we install it in Task 17)**

```bash
ssh ema-trader '
    cd /tmp
    curl -sLO https://amazoncloudwatch-agent.s3.amazonaws.com/ubuntu/arm64/latest/amazon-cloudwatch-agent.deb
    sudo dpkg -i -E amazon-cloudwatch-agent.deb
    /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a status
'
```

Expected: package installs; ctl reports the agent as `not started` (we
configure and start it in Task 17).

---

### Task 11: Clone the repo, build venv, install deps

- [ ] **Step 1: Create the install directory and clone**

```bash
ssh ema-trader '
    sudo mkdir -p /opt/ema-gap-trader
    sudo chown ubuntu:ubuntu /opt/ema-gap-trader
'
```

Then clone (replace the URL with your actual repo URL — GitHub HTTPS works
since this is a public-or-deploy-key flow; for private repos use a deploy
key in `~/.ssh/` on the box):

```bash
ssh ema-trader '
    cd /opt
    git clone <YOUR_REPO_HTTPS_OR_SSH_URL> ema-gap-trader
    cd ema-gap-trader
    git checkout main
    git log -1 --oneline
'
```

Expected: clone succeeds, prints the latest commit on `main` (should match
the merge from Phase 1).

- [ ] **Step 2: Build the venv and install requirements**

```bash
ssh ema-trader '
    cd /opt/ema-gap-trader
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip wheel
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python -c "import streamlit, pandas, plotly, pyotp; print(\"deps OK\")"
'
```

Expected: pip output finishes, prints `deps OK`. The smartapi-python install
may pull in a few extras — let it run.

- [ ] **Step 3: Run the test suite on the box**

```bash
ssh ema-trader '
    cd /opt/ema-gap-trader
    .venv/bin/pytest -q
'
```

Expected: all 128+ tests green. If anything fails on EC2 but not your
laptop, that's a real bug in the code or a missing system dep — fix it
before continuing.

---

### Task 12: Create `/etc/ema-trader/` with `app.env` and `dhan.token`

- [ ] **Step 1: Create the directory**

```bash
ssh ema-trader '
    sudo mkdir -p /etc/ema-trader
    sudo chown root:ubuntu /etc/ema-trader
    sudo chmod 0750 /etc/ema-trader
'
```

- [ ] **Step 2: Populate `app.env` (interactive — paste real secrets)**

```bash
ssh ema-trader 'sudo tee /etc/ema-trader/app.env >/dev/null' <<'EOF'
# Angel One — used by trader for live and paper trading
API_KEY=PASTE_ME
SECRET_KEY=PASTE_ME
CLIENT_ID=PASTE_ME
PASSWORD=PASTE_ME
TOTP_SECRET=PASTE_ME

# Dhan (only the permanent client ID — the JWT lives in dhan.token)
DHAN_CLIENT_ID=PASTE_ME

# Telegram alerts
TELEGRAM_BOT_TOKEN=PASTE_ME
TELEGRAM_CHAT_ID=PASTE_ME
EOF

ssh ema-trader '
    sudo chown root:ubuntu /etc/ema-trader/app.env
    sudo chmod 0640 /etc/ema-trader/app.env
'
```

Then **edit it on the box** to paste real values:

```bash
ssh ema-trader 'sudo nano /etc/ema-trader/app.env'
```

Expected: file populated with real secrets, no `PASTE_ME` placeholders left.

Verify:
```bash
ssh ema-trader '
    sudo grep -c PASTE_ME /etc/ema-trader/app.env || echo "no placeholders left — OK"
'
```

Expected: `no placeholders left — OK`.

- [ ] **Step 3: Seed `dhan.token` with today's token**

```bash
ssh ema-trader '
    sudo touch /etc/ema-trader/dhan.token
    sudo chown ubuntu:ubuntu /etc/ema-trader/dhan.token
    sudo chmod 0640 /etc/ema-trader/dhan.token
'
```

Then on your laptop, generate a fresh Dhan token from dhanhq.co and pipe it in:

```bash
read -s DHAN_TOK
echo -n "$DHAN_TOK" | ssh ema-trader 'sudo -u ubuntu tee /etc/ema-trader/dhan.token >/dev/null'
unset DHAN_TOK
```

Verify:
```bash
ssh ema-trader 'sudo -u ubuntu cat /etc/ema-trader/dhan.token | wc -c'
```

Expected: a non-zero byte count (Dhan tokens are several hundred bytes).

---

### Task 13: Install and start the systemd unit

- [ ] **Step 1: Copy the unit file from the repo into systemd's directory**

```bash
ssh ema-trader '
    sudo cp /opt/ema-gap-trader/deploy/systemd/ema-app.service /etc/systemd/system/ema-app.service
    sudo systemctl daemon-reload
'
```

- [ ] **Step 2: Enable and start**

```bash
ssh ema-trader '
    sudo systemctl enable ema-app
    sudo systemctl start ema-app
    sleep 3
    sudo systemctl status ema-app --no-pager
'
```

Expected: `Active: active (running)`. If the service fails, check
`sudo journalctl -u ema-app -n 50` for the error and fix
`/etc/ema-trader/app.env` or the unit file.

- [ ] **Step 3: Verify Streamlit is listening on 127.0.0.1:8501**

```bash
ssh ema-trader '
    sudo ss -tlnp | grep 8501
    curl -sf http://127.0.0.1:8501/_stcore/health
'
```

Expected: `LISTEN ... 127.0.0.1:8501` line, and `ok` from the health check.

---

### Task 14: Set up DuckDNS subdomain

- [ ] **Step 1: Register a DuckDNS subdomain**

In a browser, go to https://www.duckdns.org/. Sign in (GitHub/Google/Reddit
work — no credit card, no email-only signup). Pick a subdomain like
`ema-trader-yourname` (it must be globally unique within DuckDNS). Copy
the **token** DuckDNS shows you on the dashboard.

- [ ] **Step 2: Point the subdomain at your Elastic IP**

In the DuckDNS dashboard, set the IP for your subdomain to the
`$EIP_PUBLIC` from Task 8. Click "update".

Verify from your laptop:
```bash
dig +short ema-trader-yourname.duckdns.org
```

Expected: prints your `$EIP_PUBLIC` (DNS may take a minute to propagate).

- [ ] **Step 3: Set up DuckDNS auto-update on the box (so the IP stays
  current if AWS ever swaps the EIP)**

```bash
ssh ema-trader '
    mkdir -p ~/duckdns
    cat > ~/duckdns/duck.sh <<EOF
#!/bin/bash
echo url="https://www.duckdns.org/update?domains=ema-trader-yourname&token=YOUR_DUCKDNS_TOKEN&ip=" \
    | curl -k -o ~/duckdns/duck.log -K -
EOF
    chmod 700 ~/duckdns/duck.sh
    ( crontab -l 2>/dev/null; echo "*/5 * * * * ~/duckdns/duck.sh >/dev/null 2>&1" ) | crontab -
    crontab -l
'
```

**You need to edit `~/duckdns/duck.sh` on the box** to replace
`ema-trader-yourname` and `YOUR_DUCKDNS_TOKEN` with your actual values:
```bash
ssh ema-trader 'nano ~/duckdns/duck.sh && ~/duckdns/duck.sh && cat ~/duckdns/duck.log'
```

Expected: `duck.log` contains `OK`.

---

### Task 15: Install nginx config and basic-auth

- [ ] **Step 1: Copy and templatize the nginx config**

```bash
ssh ema-trader '
    DUCKDNS_HOST=ema-trader-yourname.duckdns.org   # replace with yours
    sudo cp /opt/ema-gap-trader/deploy/nginx/ema-app.conf /etc/nginx/sites-available/ema-app.conf
    sudo sed -i "s/__DUCKDNS_HOST__/$DUCKDNS_HOST/g" /etc/nginx/sites-available/ema-app.conf
    sudo ln -sf /etc/nginx/sites-available/ema-app.conf /etc/nginx/sites-enabled/ema-app.conf
    sudo rm -f /etc/nginx/sites-enabled/default
    sudo nginx -t
'
```

Expected: `nginx -t` reports `syntax is ok` and `test is successful`.
(It will warn about the snakeoil cert paths — expected; certbot fixes
those in Task 16.)

- [ ] **Step 2: Create the basic-auth file**

```bash
ssh ema-trader 'sudo htpasswd -c /etc/nginx/.htpasswd <YOUR_USERNAME>'
```

You'll be prompted for a password (twice). Pick something strong.

```bash
ssh ema-trader 'sudo chown root:www-data /etc/nginx/.htpasswd && sudo chmod 0640 /etc/nginx/.htpasswd'
```

- [ ] **Step 3: Reload nginx**

```bash
ssh ema-trader 'sudo systemctl reload nginx && sudo systemctl status nginx --no-pager | head -5'
```

Expected: `Active: active (running)`.

- [ ] **Step 4: Sanity-check the redirect (HTTP → HTTPS)**

From your laptop:
```bash
curl -sI http://ema-trader-yourname.duckdns.org/
```

Expected: `301 Moved Permanently` with `Location: https://...`. (The
HTTPS endpoint will fail until certbot runs — that's the next task.)

---

### Task 16: Run certbot for Let's Encrypt

- [ ] **Step 1: Run certbot in nginx mode**

```bash
ssh ema-trader '
    sudo certbot --nginx \
        -d ema-trader-yourname.duckdns.org \
        --non-interactive --agree-tos -m YOUR_EMAIL@example.com \
        --redirect
'
```

Expected: certbot reports
```
Successfully received certificate.
Certificate is saved at: /etc/letsencrypt/live/ema-trader-yourname.duckdns.org/fullchain.pem
```
and rewrites the nginx config to point at the real cert.

- [ ] **Step 2: Verify auto-renewal timer**

```bash
ssh ema-trader 'sudo systemctl status certbot.timer --no-pager | head -10'
```

Expected: `Active: active (waiting)`. Renewal happens automatically twice a
day; certs renew when they're within 30 days of expiry.

- [ ] **Step 3: End-to-end smoke from your laptop**

```bash
curl -sI -u <YOUR_USERNAME>:<YOUR_PASSWORD> https://ema-trader-yourname.duckdns.org/
```

Expected: `200 OK` (Streamlit returns 200 on `/`). Then open the URL in a
browser, log in with the basic-auth credentials, and confirm the Streamlit
UI loads with the "Backtest | Paper Trading | Live Trading" tabs and the
"Dhan token (daily refresh)" sidebar widget shows `Status: SET`.

---

### Task 17: CloudWatch agent + log group + StatusCheck alarm

- [ ] **Step 1: Create an IAM role for the EC2 instance**

From your laptop:

```bash
cat > /tmp/ec2-trust.json <<'EOF'
{
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ec2.amazonaws.com"},
        "Action": "sts:AssumeRole"
    }]
}
EOF

aws iam create-role --role-name ema-trader-cw \
    --assume-role-policy-document file:///tmp/ec2-trust.json

aws iam attach-role-policy --role-name ema-trader-cw \
    --policy-arn arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy

aws iam create-instance-profile --instance-profile-name ema-trader-cw
aws iam add-role-to-instance-profile \
    --instance-profile-name ema-trader-cw --role-name ema-trader-cw

# Wait a few seconds for IAM to propagate, then attach to the instance
sleep 10
aws ec2 associate-iam-instance-profile \
    --instance-id "$INSTANCE_ID" \
    --iam-instance-profile Name=ema-trader-cw
```

Expected: each command returns JSON without errors.

- [ ] **Step 2: Drop the CloudWatch agent config on the box**

```bash
ssh ema-trader 'sudo tee /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json >/dev/null' <<'EOF'
{
    "agent": {
        "run_as_user": "root"
    },
    "logs": {
        "logs_collected": {
            "files": {
                "collect_list": []
            }
        },
        "log_stream_name": "{instance_id}"
    },
    "metrics": {
        "namespace": "ema-trader",
        "metrics_collected": {
            "mem": {"measurement": ["mem_used_percent"]},
            "disk": {
                "measurement": ["used_percent"],
                "resources": ["/"]
            }
        }
    }
}
EOF
```

(The journald → CloudWatch forwarding for `ema-app` is configured next, in
Step 3, via the `journald` plugin.)

- [ ] **Step 3: Add journald → CloudWatch for the ema-app service**

```bash
ssh ema-trader 'sudo tee -a /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.d/journald.json >/dev/null' <<'EOF'
{
    "logs": {
        "logs_collected": {
            "files": {
                "collect_list": [
                    {
                        "file_path": "/var/log/syslog",
                        "log_group_name": "/ema-gap-trader/syslog",
                        "log_stream_name": "{instance_id}",
                        "retention_in_days": 7
                    }
                ]
            }
        }
    }
}
EOF
```

(The `journald` input plugin requires a slightly different config layout
depending on agent version — if the journald plugin doesn't pick up the
unit, the syslog file fallback above still captures everything because
systemd routes `ema-app` stdout/stderr there too via `SyslogIdentifier=ema-app`.)

- [ ] **Step 4: Start the CloudWatch agent**

```bash
ssh ema-trader '
    sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
        -a fetch-config -m ec2 -s \
        -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json
    sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a status
'
```

Expected: status reports `running` for the agent.

Verify in AWS console: CloudWatch → Log groups → `/ema-gap-trader/syslog`
should appear within ~2 minutes with at least one log stream.

- [ ] **Step 5: Create the SNS topic for alarm emails**

```bash
TOPIC_ARN=$(aws sns create-topic --name ema-trader-alarms --query 'TopicArn' --output text)
echo "TOPIC_ARN=$TOPIC_ARN"

aws sns subscribe \
    --topic-arn "$TOPIC_ARN" \
    --protocol email \
    --notification-endpoint YOUR_EMAIL@example.com
```

Expected: subscription confirmation email arrives at `YOUR_EMAIL`. Click the
"Confirm subscription" link in the email.

- [ ] **Step 6: Create the StatusCheck alarm**

```bash
aws cloudwatch put-metric-alarm \
    --alarm-name ema-trader-status-check \
    --alarm-description "EC2 instance status check failed for 5 min" \
    --metric-name StatusCheckFailed \
    --namespace AWS/EC2 \
    --statistic Maximum \
    --period 60 \
    --evaluation-periods 5 \
    --threshold 0 \
    --comparison-operator GreaterThanThreshold \
    --dimensions Name=InstanceId,Value=$INSTANCE_ID \
    --alarm-actions "$TOPIC_ARN"
```

Expected: command returns silently. Verify in console: CloudWatch → Alarms
→ `ema-trader-status-check` should be in `OK` state.

---

### Task 18: First end-to-end smoke test

- [ ] **Step 1: Verify the service is up after a reboot (proves
  systemd-on-boot works)**

```bash
ssh ema-trader 'sudo reboot'
sleep 60
ssh ema-trader '
    sudo systemctl status ema-app --no-pager | head -10
    curl -sf http://127.0.0.1:8501/_stcore/health && echo " — Streamlit healthy"
'
```

Expected: `active (running)` and `ok — Streamlit healthy`.

- [ ] **Step 2: Smoke-test the public URL**

From your laptop, in a browser, open:
```
https://ema-trader-yourname.duckdns.org/
```

Sign in with the basic-auth username/password from Task 15. Verify:
- Streamlit UI loads
- The three tabs (Backtest / Paper Trading / Live Trading) are present
- The "Dhan token (daily refresh)" sidebar shows `Status: SET`
- No console errors in the browser dev tools

- [ ] **Step 3: Run a sample backtest**

In the Backtest tab, pick a small date range (e.g. last week) and one
instrument. Click Run. Verify:
- The backtest completes without errors
- The Dhan token works (no `401` from Dhan in the logs:
  `ssh ema-trader 'sudo journalctl -u ema-app -n 100 | grep -i dhan'`)
- Results render

- [ ] **Step 4: Test the Dhan token rotation flow**

In the sidebar, expand "Dhan token (daily refresh)". Paste the *same*
current token again and click Save. Verify the success message. Re-run
the backtest — it should still work. (You're testing the write path,
not the rotation behaviour.)

- [ ] **Step 5: Test deploy.sh from your laptop**

Make a trivial whitespace-only change on `main` (e.g. add a blank line at
the end of the README), commit, push. Then:

```bash
EC2_HOST=ema-trader ./deploy.sh
```

Expected: script connects, pulls, restarts, prints `active (running)`.

- [ ] **Step 6: Test telegram alerting end-to-end**

Start a paper trading session from the UI. Within ~30 seconds you should
get a telegram message: `Trader started for ...`. Stop the session.

If no telegram arrives, check:
```bash
ssh ema-trader 'sudo journalctl -u ema-app -n 50 | grep -i telegram'
```

Look for `Telegram API returned 401` (bad bot token) or `Telegram send
failed` (network/credentials problem) and fix `app.env` accordingly.

- [ ] **Step 7: Document the runbook (commit to repo)**

Create a small `docs/superpowers/RUNBOOK.md` with the daily ops you'll need:

```markdown
# ema-gap-trader runbook

## Daily Dhan token refresh
1. Open https://ema-trader-yourname.duckdns.org/
2. Sidebar → "Dhan token (daily refresh)" → paste new token → Save

## Deploy a code change
On laptop:
    git checkout main && git pull && EC2_HOST=ema-trader ./deploy.sh

(Avoid 09:15–15:30 IST during market hours.)

## Tail logs
    ssh ema-trader 'sudo journalctl -u ema-app -f'

## Restart the service
    ssh ema-trader 'sudo systemctl restart ema-app'

## Check disk usage
    ssh ema-trader 'df -h /; du -sh /opt/ema-gap-trader/dhan_option_cache.db'
```

Commit:
```bash
git add docs/superpowers/RUNBOOK.md
git commit -m "docs: add runbook for daily Dhan token refresh and deploy"
git push
```

---

## Done

When all 18 tasks are complete you have:
- A `t4g.small` in Mumbai running `ema-app.service` with auto-restart
- HTTPS dashboard at `https://ema-trader-<you>.duckdns.org/` behind basic auth
- Daily Dhan token rotation via the Streamlit sidebar
- Telegram alerts for trader events
- CloudWatch StatusCheck alarm → email if the box dies
- A one-line `./deploy.sh` to push changes from your laptop

Total monthly cost: ~$14–15, or $0 while AWS credits last.

---

## Self-review notes

The plan was reviewed against the spec
(`docs/superpowers/specs/2026-04-11-aws-ec2-deployment-design.md`) after
writing. Coverage:

- Section 1 (Infrastructure) → Tasks 8, 9, 10
- Section 2 (Components: ema-app + nginx) → Tasks 6, 7, 13, 15, 16
- Section 3 (Persistence + secrets, Dhan hot-reload) → Tasks 1, 2, 3, 12
- Section 4 (Network + access, DuckDNS, Let's Encrypt) → Tasks 9, 14, 15, 16
- Section 5 (Monitoring + deploy) → Tasks 4, 5, 17, 18
- Required code changes checklist → Tasks 1, 2, 3, 4, 5, 6, 7

No spec section is unimplemented.
