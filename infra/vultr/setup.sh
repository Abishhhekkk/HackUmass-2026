#!/usr/bin/env bash
# Vantage cloud on a fresh Vultr Ubuntu 24.04 VM. Run as root; run again to update.
#   1. Copy your .env to the VM first:   scp .env root@VM-IP:/root/vantage.env
#   2. On the VM:   curl -fsSL https://raw.githubusercontent.com/Abishhhekkk/HackUmass-2026/claude/infra/vultr/setup.sh | bash
# Result: https://<VM-IP-with-dashes>.sslip.io  (free HTTPS, no domain needed)
set -euo pipefail
DIR=/opt/vantage
BRANCH="${BRANCH:-claude}"

echo "== Packages (Docker, git)"
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  apt-get update -q
  apt-get install -y -q docker.io docker-compose-v2 git curl
fi

echo "== Swap (a 1 GB VM needs it to build the website)"
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== Code ($BRANCH)"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q origin "$BRANCH" && git -C "$DIR" checkout -q "$BRANCH" && git -C "$DIR" pull -q --ff-only
else
  git clone -q --branch "$BRANCH" https://github.com/Abishhhekkk/HackUmass-2026.git "$DIR"
fi

echo "== Settings (.env)"
if [ -f /root/vantage.env ]; then install -m 600 /root/vantage.env "$DIR/.env"; fi
if [ ! -f "$DIR/.env" ]; then
  echo "No .env. From your laptop: scp .env root@<this VM's IP>:/root/vantage.env  then run this again."; exit 1
fi
for k in DATABASE_URL INGEST_API_KEY; do
  grep -q "^$k=" "$DIR/.env" || { echo "$k is missing from .env"; exit 1; }
done

IP=$(hostname -I | awk '{print $1}')
DOMAIN="${DOMAIN:-${IP//./-}.sslip.io}"
echo "== Firewall: allow web traffic"
if command -v ufw >/dev/null && ufw status | grep -q active; then ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; fi

echo "== Build and start (first time ~5 min)"
cd "$DIR/infra/vultr"
DOMAIN="$DOMAIN" docker compose up -d --build
docker image prune -f >/dev/null

echo "== Waiting for https://$DOMAIN"
for i in $(seq 1 60); do
  if out=$(curl -fsS "https://$DOMAIN/api/v1/health" 2>/dev/null); then
    echo "   $out"
    echo
    echo "Vantage cloud is up:   https://$DOMAIN/#/dashboard"
    echo "On the Jetson, .env:   CLOUD_URL=https://$DOMAIN"
    exit 0
  fi
  sleep 5
done
echo "Not answering yet. Logs: cd $DIR/infra/vultr && docker compose logs --tail 50"
exit 1
