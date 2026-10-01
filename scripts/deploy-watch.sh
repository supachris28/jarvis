#!/usr/bin/env bash
# Lets Claude (Cowork) deploy for you. Leave this running in a WSL terminal:
#
#   bash scripts/deploy-watch.sh
#
# Claude can only write files in this folder, it can't run commands in WSL or reach the server. When it drops an
# empty file releases/DEPLOY_REQUEST, this runs scripts/deploy.sh (the same thing you'd run yourself) and writes
# the outcome to releases/DEPLOY_RESULT plus the full output to releases/deploy-<time>.log, which Claude reads back.
# The request file's contents are ignored — it's only a signal. Ctrl+C stops it.
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p releases
echo "Watching releases/DEPLOY_REQUEST (Ctrl+C to stop)…"
while true; do
    if [ -e releases/DEPLOY_REQUEST ]; then
        rm -f releases/DEPLOY_REQUEST
        stamp="$(date +%Y%m%d-%H%M%S)"
        log="releases/deploy-$stamp.log"
        version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/jarvis/__init__.py)"
        echo "$(date +%H:%M:%S) deploy requested ($version) — logging to $log"
        printf 'running %s %s %s\n' "$version" "$stamp" "$log" > releases/DEPLOY_RESULT
        if bash scripts/deploy.sh > "$log" 2>&1; then status=ok; else status=failed; fi
        printf '%s %s %s %s\n' "$status" "$version" "$stamp" "$log" > releases/DEPLOY_RESULT
        echo "$(date +%H:%M:%S) $status"
        ls -1t releases/deploy-*.log 2>/dev/null | tail -n +21 | xargs -r rm -f   # keep the last 20 logs
    fi
    sleep 3
done
