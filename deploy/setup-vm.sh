#!/usr/bin/env bash
# One-time setup of a fresh Oracle Cloud VM (Ubuntu 22.04/24.04, Arm or x86) for FIRELENS.
# Run on the VM:  bash setup-vm.sh
# Installs Docker, opens ports 80/443 in the VM's own firewall, clones the repo to ~/FireLens,
# and schedules the daily database backup. Idempotent: safe to run again.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/murafacu12/FireLens.git}"
BRANCH="${BRANCH:-main}"   # e.g. BRANCH=analysis-engine bash setup-vm.sh
DIR="$HOME/FireLens"

echo "== Docker"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  echo "   (log out and back in once so 'docker' works without sudo)"
fi

echo "== Firewall: Oracle's Ubuntu images block everything but SSH in iptables"
for port in 80 443; do
  if ! sudo iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
    sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport "$port" -j ACCEPT
  fi
done
if command -v netfilter-persistent >/dev/null; then
  sudo netfilter-persistent save
else
  sudo apt-get update -y && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y iptables-persistent
  sudo netfilter-persistent save
fi

echo "== Swap (helps during image builds on small VMs)"
if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "== Code"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch origin "$BRANCH" && git -C "$DIR" checkout "$BRANCH" && git -C "$DIR" pull --ff-only origin "$BRANCH"
else
  git clone --branch "$BRANCH" "$REPO_URL" "$DIR"
fi
mkdir -p "$DIR/deploy/data" "$DIR/deploy/backups"
sudo chown -R 10001 "$DIR/deploy/data"   # the API container runs as uid 10001

echo "== Daily backup at 04:30 (server time)"
CRON="30 4 * * * bash $DIR/deploy/backup.sh >> $DIR/deploy/backups/backup.log 2>&1"
( crontab -l 2>/dev/null | grep -v 'deploy/backup.sh' ; echo "$CRON" ) | crontab -

cat <<EOF

Done. Next:
  1. cp $DIR/deploy/.env.production.example $DIR/deploy/.env   and fill it in
  2. upload the data from your PC (deploy/upload-data.sh), or start empty
  3. cd $DIR/deploy && docker compose up -d --build
EOF
