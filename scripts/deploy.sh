#!/usr/bin/env bash
# One command from WSL: build + push the image, tell the server to pull it, and check the new version is live.
#
#   bash scripts/deploy.sh
#
# Settings come from .env.deploy in the repo root (not committed, not sent to Docker):
#   DEPLOY_SERVER=chris@192.168.68.203     # ssh target — needs key login (ssh-copy-id), no password prompt
#   DEPLOY_DIR=/opt/jarvis                 # folder with docker-compose.yml on the server
#   DEPLOY_SERVICES=jarvis                 # services to pull/restart (space separated)
#   DEPLOY_URL=https://jarvis.keeeys.uk    # checked afterwards for the new version
#   DEPLOY_DOCKER=docker                   # e.g. "sudo -n docker" if docker needs sudo on the server
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env.deploy ] && . ./.env.deploy
SERVER="${DEPLOY_SERVER:?Set DEPLOY_SERVER=user@server in .env.deploy}"
DIR="${DEPLOY_DIR:-/opt/jarvis}"
SERVICES="${DEPLOY_SERVICES:-jarvis}"
URL="${DEPLOY_URL:-https://jarvis.keeeys.uk}"
DOCKER="${DEPLOY_DOCKER:-docker}"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/jarvis/__init__.py)"
step() { printf '\n=== %s  %s\n' "$(date +%H:%M:%S)" "$*"; }

step "Build and push Jarvis $VERSION"
TAG="${TAG:-$VERSION-$(date +%Y%m%d-%H%M)}" sh ./scripts/build-push.sh

step "Server $SERVER: pull and restart $SERVICES"
ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER" \
    "set -e; cd '$DIR'; $DOCKER compose pull $SERVICES; $DOCKER compose up -d $SERVICES; $DOCKER image prune -f >/dev/null; $DOCKER compose ps $SERVICES"

step "Waiting for $URL to report $VERSION"
for _ in $(seq 1 40); do
    live="$(curl -fsS --max-time 5 "$URL/healthz" 2>/dev/null || true)"
    if [ "$live" = "ok $VERSION" ]; then
        step "Done — Jarvis $VERSION is live"
        exit 0
    fi
    sleep 3
done
echo "Jarvis didn't report $VERSION within 2 minutes (last answer: '${live:-nothing}')." >&2
ssh -o BatchMode=yes "$SERVER" "cd '$DIR'; $DOCKER compose logs --tail 40 jarvis" >&2 || true
exit 1
