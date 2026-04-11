# AWS EC2 Deployment Design — ema-gap-trader

**Date:** 2026-04-11
**Status:** Approved (pending user review of this document)
**Author:** Brainstormed via Claude Code

## Goal

Deploy the `ema-gap-trader` project — live Angel One trading bot, monitoring
dashboard, and Streamlit-based backtest UI — to AWS as a personal-use,
single-tenant, always-on service. Optimise for low cost, low operational
overhead, and a workflow that lets one developer push changes from a laptop
without ceremony.

## Non-Goals

- High availability / multi-AZ redundancy
- Multi-user / production-grade access control
- CI/CD pipelines, blue-green deploys, or staging environments
- Automated daily backups (explicitly declined by user; rebuild-from-API
  accepted as the recovery path)
- Containerisation (no Docker) — single-tenant box, systemd is sufficient
- Latency optimisation beyond region selection

## Architecture Overview

A single AWS EC2 instance in `ap-south-1` (Mumbai) runs four long-lived
processes managed by systemd, fronted by nginx with Let's Encrypt TLS. All
state (SQLite, the ~1.5 GB Dhan option cache, logs) lives on the instance's
EBS root volume. No RDS, no S3, no load balancer, no container runtime.

```
                    Internet
                       │
                       ▼
        ┌──────────────────────────────┐
        │  EC2 t4g.small (ap-south-1)  │
        │  Ubuntu 22.04, IST timezone  │
        │                              │
        │  nginx :443  ──┬──► :8000  app.py (Flask dashboard)
        │  (LE TLS,      │
        │   basic auth)  └──► :8501  Streamlit (backtest UI)
        │                              │
        │  systemd: ema-trader.service │  ◄── Angel One SmartAPI
        │                              │       (live + paper trading)
        │  EBS gp3 30 GB:              │
        │   /opt/ema-gap-trader/       │
        │   /etc/ema-trader/           │
        └──────────────────────────────┘
                       │
                       ▼
              CloudWatch Logs +
              StatusCheck alarm → SNS email
```

## Section 1 — Infrastructure

**Instance:** EC2 `t4g.small` (ARM Graviton, 2 vCPU, 2 GB RAM)
- Region: `ap-south-1` (Mumbai). **Load-bearing choice** — keeps round-trip
  latency to Angel One sub-50 ms. Other regions add 100–200 ms per order,
  which materially affects a live strategy.
- AMI: Ubuntu 22.04 LTS for ARM64
- Storage: 30 GB gp3 EBS root volume (~$2.40/mo)
- Networking: Elastic IP attached (free while attached)
- Timezone: `Asia/Kolkata` so logs and any cron jobs match market hours

**Why `t4g.small` and not free-tier `t3.micro`:** the free-tier `t3.micro` has
only 1 GB RAM, which is too tight for the trader process + Flask dashboard +
Streamlit + occasional ad-hoc backtests on the same box. Paying ~$12/mo for
2 GB RAM and an extra vCPU is the right tradeoff. If AWS credits cover the
bill, this is effectively free for the duration of the credits.

**Region note for non-trading users:** if at any point you want to access the
dashboards from outside India and find them slow, that's expected — the box
sits in Mumbai for trading latency, not for human UI latency.

## Section 2 — Components

Four long-lived processes, each a systemd unit with `Restart=always` and
`After=network-online.target`. All run as the unprivileged `ubuntu` user.

### 2.1 `ema-trader.service`
- Runs `python trader.py` from `/opt/ema-gap-trader` using the project venv
- The live (or paper) trading bot — places orders via Angel One SmartAPI
- Logs to journald → CloudWatch agent forwards to a log group (see §5.1)
- Restart on crash; restart on boot

### 2.2 `ema-dashboard.service`
- Runs `app.py` (existing Flask/FastAPI monitoring UI) bound to
  `127.0.0.1:8000`
- Not directly exposed; reached only through the nginx reverse proxy

### 2.3 `ema-backtest.service`
- Runs `streamlit run <backtest_app>.py` bound to `127.0.0.1:8501`
- Hosts the on-demand backtest UI and the Dhan-token update widget (see §3)
- Backtests are CPU bursts on the same box. The 2 vCPU `t4g.small` can
  handle them; if a sweep starts hurting the live trader's tick processing,
  the user can either (a) temporarily resize the instance or (b) run the
  sweep on a laptop using the same code

### 2.4 `nginx`
- Reverse proxy on port 443, terminates Let's Encrypt TLS
- HTTP basic auth (single user) protects everything
- Routes:
  - `https://<host>/` → `127.0.0.1:8000` (dashboard)
  - `https://<host>/backtest/` → `127.0.0.1:8501` (Streamlit; needs WebSocket
    upgrade headers `Upgrade` and `Connection`)
- Port 80 serves only the ACME challenge and 301-redirects everything else
  to 443

**No Docker.** Direct Python `.venv` + systemd. A single-tenant box does not
benefit from a container layer; it just adds moving parts.

**Open question (resolved during implementation, not blocking):** the exact
framework and entry-point for `app.py` (Flask vs FastAPI vs something else)
and the exact module name of the Streamlit backtest app. The systemd unit
files need to match — the implementation plan will start by reading these
two files.

## Section 3 — Persistence and Secrets

### 3.1 Disk layout

```
/opt/ema-gap-trader/        ← git checkout, owned by ubuntu:ubuntu
  .venv/                    ← Python virtualenv
  *.py                      ← project source
  data/                     ← parquet / CSV cached datasets
  dhan_option_cache.db      ← 1.5 GB Dhan SQLite cache
  logs/                     ← rotated daily by logrotate, 7-day retention

/etc/ema-trader/            ← root:ubuntu, 0750
  app.env                   ← permanent secrets, mode 0640
  dhan.token                ← rotating Dhan JWT, mode 0640, writable by ubuntu
```

### 3.2 Permanent secrets (`app.env`)

Loaded **once at boot** by each systemd unit via `EnvironmentFile=`. These do
not rotate, so a service restart on update is acceptable.

```
# Angel One — used by trader + dashboard (live + paper)
API_KEY=...
SECRET_KEY=...
CLIENT_ID=...
PASSWORD=...
TOTP_SECRET=...

# Dhan — only the permanent client ID lives here
DHAN_CLIENT_ID=...

# Notifications
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

(Dashboard basic-auth credentials live in `/etc/nginx/.htpasswd`, not in
`app.env` — they're enforced at the nginx layer, not by the app.)

The Angel One credentials are all permanent. `broker.py` already does
TOTP-based login via `pyotp` with a 5-hour session refresh
(`SESSION_MAX_AGE_HOURS = 5`). There is **no daily token rotation** for the
live trader — it logs in fresh whenever the cached session goes stale.

### 3.3 Rotating Dhan token (the only hot-reload case)

`DHAN_ACCESS_TOKEN` is a daily-expiring JWT used **only** by `dhan_data.py`
for backtest historical fetching. The live trader does not touch Dhan at
all. So the only consumer of the rotating token is the Streamlit backtest
service.

**Mechanism:** the token lives in `/etc/ema-trader/dhan.token`.
`dhan_data.py` reads it just-in-time on every Dhan API call (no cache /
TTL = 0 — backtests are not latency-sensitive and a file read is microseconds).

**Code change required in `dhan_data.py`:**

```python
_DHAN_TOKEN_PATH = os.environ.get("DHAN_TOKEN_PATH", "/etc/ema-trader/dhan.token")

def _get_dhan_token() -> str:
    """Read the Dhan access token from disk on every call.

    The token is rotated daily by the user via the Streamlit admin widget;
    reading per-call ensures we always use the latest value without needing
    to restart the service.
    """
    try:
        return Path(_DHAN_TOKEN_PATH).read_text().strip()
    except FileNotFoundError:
        # Local-dev fallback — env var still works for CLI / pytest
        return os.environ.get("DHAN_ACCESS_TOKEN", "")
```

Replace the single `os.environ.get("DHAN_ACCESS_TOKEN", "")` lookup in
`init_dhan()` (and any other call sites) with `_get_dhan_token()`.

**Streamlit admin widget** (added to the backtest app sidebar):

```python
with st.sidebar.expander("Dhan token"):
    new_token = st.text_input("Paste new Dhan access token", type="password")
    if st.button("Save"):
        Path("/etc/ema-trader/dhan.token").write_text(new_token.strip())
        st.success("Saved. Next backtest will use it.")
```

Both Streamlit and `dhan_data.py` run as the `ubuntu` user, so the file
permissions just work.

**Daily flow for the user:**
1. Open the Streamlit dashboard in a browser
2. Generate a new token from dhanhq.co
3. Paste into the sidebar widget, click Save
4. Run the backtest. No SSH, no service restart.

### 3.4 No backups

User explicitly declined snapshot backups. Accepted recovery path: rebuild
the Dhan cache from the API (slow but possible) and re-clone the repo.
Anything irreplaceable that ends up on disk later (e.g., live trade history
database) should be revisited at that time.

### 3.5 Logs

`logrotate` rotates any file-based logs daily, keeps 7 days locally. The
CloudWatch agent forwards journald output for both `ema-trader` and
`ema-backtest` to CloudWatch log groups with 7-day retention. (Free tier is
5 GB/month — well above expected volume.)

## Section 4 — Network and Access

### 4.1 Security group

| Port | Protocol | Source        | Purpose                          |
|------|----------|---------------|----------------------------------|
| 22   | TCP      | `0.0.0.0/0`   | SSH (key-only, fail2ban)         |
| 80   | TCP      | `0.0.0.0/0`   | ACME challenge + redirect to 443 |
| 443  | TCP      | `0.0.0.0/0`   | HTTPS dashboard + Streamlit      |

Outbound: allow all (default).

**SSH hardening:**
- Password auth disabled in `/etc/ssh/sshd_config` (`PasswordAuthentication no`)
- Key-only auth (RSA or ed25519)
- `fail2ban` installed; bans IPs after 3 failed attempts for 10 minutes
- `root` login disabled

This is reversible — the user can switch to AWS Systems Manager Session
Manager later (no port 22 exposed at all) if they want a more locked-down
posture.

### 4.2 Domain and TLS

- **DuckDNS** free subdomain, e.g. `ema-trader-<user>.duckdns.org`
- An A record on the DuckDNS subdomain points at the EC2 Elastic IP
- Let's Encrypt cert via `certbot` with the nginx plugin
- Auto-renewal via the systemd timer that certbot installs by default
- Total cost: $0

User can later swap to a paid custom domain by updating one DNS record and
re-running `certbot --nginx -d <new-domain>` — no other config changes
needed.

### 4.3 nginx config sketch

```nginx
server {
    listen 80;
    server_name ema-trader-<user>.duckdns.org;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl http2;
    server_name ema-trader-<user>.duckdns.org;

    ssl_certificate     /etc/letsencrypt/live/.../fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/.../privkey.pem;

    auth_basic           "ema-trader";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
    }

    location /backtest/ {
        proxy_pass http://127.0.0.1:8501/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }
}
```

## Section 5 — Monitoring and Deploy

### 5.1 Three-layer monitoring

**Layer 1 — systemd auto-restart.** `Restart=always` brings any crashed
service back within seconds. Most "the bot died" cases are silently solved
without producing an alert at all.

**Layer 2 — Telegram alerts via existing `notifications.py`.** Wire into the
critical paths:

- Trader startup: `Trader started, logged into Angel One as <client_id>`
- Login failure: `Angel One login failed: <error>`
- Order placement failure: `Order failed for <symbol>: <error>`
- Daily summary at market close: `Today: N trades, P&L Rs.X`

Free, push-to-phone, no AWS spend.

**Layer 3 — CloudWatch instance alarm.** The one case Telegram alerts can't
cover: the whole instance is down. For that:

- CloudWatch agent installed, forwards journald logs from `ema-trader` and
  `ema-backtest` to log groups
- One alarm: `StatusCheckFailed_Instance > 0 for 5 consecutive minutes` →
  SNS topic → email
- Free tier covers 10 alarms + 1,000 SNS emails/month

### 5.2 Deploy flow

A single shell script in the repo, run from the developer's laptop:

```bash
# deploy.sh
#!/usr/bin/env bash
set -euo pipefail

# Do not deploy between 09:15 and 15:30 IST unless it's an emergency —
# restarting the trader mid-session creates a brief tick gap.

EC2_HOST="${EC2_HOST:?set EC2_HOST=ubuntu@<elastic-ip>}"

ssh "$EC2_HOST" '
    set -euo pipefail
    cd /opt/ema-gap-trader
    git pull
    .venv/bin/pip install -r requirements.txt
    sudo systemctl restart ema-trader ema-dashboard ema-backtest
    sudo systemctl status ema-trader --no-pager | head -20
'
```

Rollback: `git checkout <prev-sha> && ./deploy.sh`. No automation around it
— if a deploy goes wrong, the developer fixes it manually.

**Explicitly not building:**
- GitHub Actions / CI pipeline (overkill for one developer, one box)
- Blue/green deploys (the bot is stateful; two copies cannot run concurrently)
- Automated rollback (the manual flow above is sufficient)
- Staging environment (backtests are the staging environment)
- Market-hours deploy lock (a comment in `deploy.sh` is enough for one user)

## Cost Summary

| Item                                   | Monthly       |
|----------------------------------------|---------------|
| EC2 t4g.small (Mumbai, on-demand)      | ~$12          |
| 30 GB gp3 EBS root                     | ~$2.40        |
| Elastic IP (attached)                  | $0            |
| Data transfer (~5 GB/mo realistic)     | $0 (free tier)|
| CloudWatch logs + 1 alarm + SNS        | $0 (free tier)|
| DuckDNS + Let's Encrypt                | $0            |
| Telegram bot                           | $0            |
| **Total**                              | **~$14–15/mo** |

Effectively $0 while AWS credits last.

## Required Code Changes

These are application-level changes the deployment depends on. The
implementation plan will sequence them.

1. **`dhan_data.py`** — replace direct `os.environ.get("DHAN_ACCESS_TOKEN")`
   reads with a `_get_dhan_token()` helper that reads from
   `/etc/ema-trader/dhan.token` per call, with env-var fallback for local
   dev / pytest.
2. **Streamlit backtest app** — add the sidebar "Dhan token" widget that
   writes `/etc/ema-trader/dhan.token`.
3. **`notifications.py` integrations** — add the four alert sites listed in
   §5.1 if they don't already exist.
4. **`deploy.sh`** — new file at repo root.
5. **systemd unit files** — `ema-trader.service`, `ema-dashboard.service`,
   `ema-backtest.service` checked into `deploy/systemd/` in the repo.
6. **nginx config + logrotate config** — checked into `deploy/nginx/` and
   `deploy/logrotate/`.

## Open Questions Deferred to Implementation

- Exact framework / entry-point of `app.py` (Flask vs FastAPI vs other) —
  determined by reading the file at plan time.
- Exact module name and entry point of the Streamlit backtest app — same.
- Whether `notifications.py` already wraps the four alert sites or needs
  new call sites added.
- Whether `requirements.txt` already pins `streamlit` (it must, for the
  backtest service).
