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
