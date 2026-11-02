#!/usr/bin/env bash
# Prepare a FRESH Ubuntu 22.04/24.04 server to run the Kalshi bot backend.
#
#   sudo bash deploy/bootstrap-vps.sh            # run once, as root, on the new server
#
# What it does (all idempotent; safe to re-run):
#   1. patches the OS and enables automatic security updates
#   2. installs Docker + the compose plugin, and chrony (accurate clock: Kalshi request signatures are
#      timestamped, and the bot refuses to start live if the clock is more than 30 s off)
#   3. sets the timezone to UTC
#   4. firewall: deny everything inbound except SSH. The backend publishes NO ports (the dashboard is a
#      separate deployment), so nothing else needs to be open.
#   5. creates /opt/kalshi with a locked-down secrets/ directory owned by uid 10001 (the container user)
#
# What it deliberately does NOT do: it never touches, creates or prints secrets (no Kalshi key, no bucket
# keys); it does not clone the repo or start the bot. Those are manual steps in docs/deployment.md.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash $0" >&2
  exit 1
fi

if ! grep -qiE 'ubuntu' /etc/os-release; then
  echo "This script targets Ubuntu. Aborting rather than guessing on another OS." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
APP_DIR="${APP_DIR:-/opt/kalshi}"
APP_UID=10001                 # must match the uid in the Dockerfile

echo "==> 1/5 system packages and automatic security updates"
apt-get update -y
# --force-confold: keep existing config files instead of stopping at an interactive prompt
apt-get -o Dpkg::Options::=--force-confold upgrade -y
apt-get install -y --no-install-recommends \
  ca-certificates curl git ufw chrony unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades || true

echo "==> 2/5 Docker engine and compose plugin"
if ! command -v docker >/dev/null 2>&1; then
  apt-get install -y --no-install-recommends docker.io docker-compose-v2 \
    || apt-get install -y --no-install-recommends docker.io docker-compose-plugin
fi
systemctl enable --now docker

echo "==> 3/5 clock: UTC + chrony"
timedatectl set-timezone UTC
systemctl enable --now chrony
# Give chrony a moment, then show the offset so the operator can see it is sane.
sleep 3
chronyc tracking | grep -E 'System time|Leap status' || true

echo "==> 4/5 firewall: deny inbound except SSH"
# Allow the port sshd ACTUALLY listens on (a custom port would otherwise lock you out), plus 22.
SSH_PORT="$(sshd -T 2>/dev/null | awk '/^port /{print $2; exit}')"
SSH_PORT="${SSH_PORT:-22}"
if ufw status | grep -q '^Status: active'; then
  # Already configured: never wipe rules someone added later (Tailscale, a custom port, ...).
  echo "ufw is already active; leaving existing rules in place."
  ufw allow "${SSH_PORT}/tcp" >/dev/null
else
  ufw default deny incoming
  ufw default allow outgoing
  ufw allow "${SSH_PORT}/tcp"
  ufw allow OpenSSH >/dev/null 2>&1 || true
  echo "Enabling the firewall with SSH on port ${SSH_PORT}. Keep this session open until you have"
  echo "confirmed in a SECOND terminal that you can still log in."
  ufw --force enable
fi
ufw status verbose | head -12

echo "==> 5/5 application directory"
mkdir -p "$APP_DIR/secrets"
# Only the secrets directory itself: never recurse. A re-run must not re-own the cloned repo or the
# private key file inside secrets/ (the container user, uid $APP_UID, has to keep reading it).
chown "$APP_UID:$APP_UID" "$APP_DIR/secrets"
chmod 700 "$APP_DIR/secrets"

cat <<MSG

Server is ready. Next (manual; see docs/deployment.md):
  1. git clone <your repo> $APP_DIR/app   && cd $APP_DIR/app
  2. put the Kalshi private key at $APP_DIR/secrets/kalshi_private_key.pem
       chown $APP_UID:$APP_UID <file> && chmod 600 <file>
  3. cp .env.example .env   and fill it in (BOT_MODE stays 'dry' until you have verified everything)
  4. docker compose up -d --build
  5. docker compose logs -f bot replicator
MSG
