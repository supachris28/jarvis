#!/usr/bin/env sh
# Build the Jarvis image and push it to registry.keeeys.uk (run from the repo root).
#   docker login registry.keeeys.uk      # once
#   ./scripts/build-push.sh              # tags :latest and :<date>
#   PLATFORM=linux/arm64 ./scripts/build-push.sh
set -eu
REGISTRY="${REGISTRY:-registry.keeeys.uk}"
IMAGE="${IMAGE:-jarvis}"
TAG="${TAG:-$(date +%Y%m%d-%H%M)}"
PLATFORM="${PLATFORM:-linux/amd64}"
REF="$REGISTRY/$IMAGE"
echo "Building $REF:$TAG ($PLATFORM)"
docker buildx build --platform "$PLATFORM" -t "$REF:$TAG" -t "$REF:latest" --push .
echo "Pushed $REF:$TAG and $REF:latest"
echo "On the server: cd /opt/jarvis && docker compose pull && docker compose up -d"
