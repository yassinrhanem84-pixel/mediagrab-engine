#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 server for the MediaGrab engine.
# Run as root:  bash setup.sh <allowed-site-origin>
set -euo pipefail
SITE_ORIGIN="${1:-https://mediagrab-alpha.vercel.app}"
APP=/opt/mediagrab

export DEBIAN_FRONTEND=noninteractive
echo "== system updates"
apt-get update -q
apt-get -yq -o Dpkg::Options::=--force-confold upgrade
apt-get install -yq ca-certificates curl ufw fail2ban unattended-upgrades rsync

echo "== automatic security updates"
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "== docker"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -yq docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
systemctl enable --now docker

echo "== firewall: only SSH, HTTP, HTTPS"
# Oracle Cloud Ubuntu images ship their own iptables rules that block 80/443; replace them with ufw
if dpkg -l | grep -q netfilter-persistent; then
  apt-get remove -yq iptables-persistent netfilter-persistent || true
  rm -f /etc/iptables/rules.v4 /etc/iptables/rules.v6
  iptables -P INPUT ACCEPT; iptables -F INPUT
fi
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw limit OpenSSH          # also rate-limits SSH connection attempts
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "== SSH: keys only, no passwords"
cat > /etc/ssh/sshd_config.d/99-mediagrab.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
MaxAuthTries 3
X11Forwarding no
EOF
systemctl reload ssh || systemctl reload sshd || true

echo "== fail2ban (bans IPs that keep failing SSH logins)"
cat > /etc/fail2ban/jail.d/sshd.local <<'EOF'
[sshd]
enabled = true
maxretry = 4
findtime = 10m
bantime = 1h
EOF
systemctl enable --now fail2ban
systemctl restart fail2ban

echo "== swap (helps ffmpeg on big 4K merges)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== start MediaGrab"
IP=$(curl -fsS https://api.ipify.org)
DOMAIN="${IP//./-}.sslip.io"          # free hostname that points at this server, so HTTPS works without buying a domain
mkdir -p "$APP"
cd "$APP/grab/deploy"
touch cookies.txt
cat > .env <<EOF
DOMAIN=$DOMAIN
ALLOWED_ORIGINS=$SITE_ORIGIN,http://localhost:3000
EOF
docker compose up -d --build
echo "== waiting for HTTPS certificate"
for i in $(seq 1 60); do curl -fsS "https://$DOMAIN/health" >/dev/null 2>&1 && break; sleep 5; done
curl -fsS "https://$DOMAIN/health" && echo
echo "ENGINE_URL=https://$DOMAIN"
