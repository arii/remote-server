#!/usr/bin/env bash
# First-boot hardening for the S5 home server. Idempotent — safe to re-run.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "run as root (sudo)" >&2; exit 1
fi

LAN_SUBNET="${LAN_SUBNET:-192.168.1.0/24}"  # override: LAN_SUBNET=10.0.0.0/24 sudo -E ./bootstrap.sh
TERMINAL_PORT="${TERMINAL_PORT:-8443}"  # public web-terminal port; must match .env

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get full-upgrade -y
apt-get install -y unattended-upgrades ufw fail2ban curl git ca-certificates avahi-daemon

# --- automatic security updates, reboot at 03:30 if needed ---
cat > /etc/apt/apt.conf.d/51-unattended-upgrades-reboot <<'EOF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "03:30";
EOF
dpkg-reconfigure -f noninteractive unattended-upgrades

# --- firewall ---
# Default deny inbound / allow outbound. Two allow rules:
#  1. TERMINAL_PORT/tcp from anywhere — the public web terminal. The GitHub
#     OAuth single-user gate (oauth2-proxy) is the entire auth boundary here;
#     see docs/remote-access.md "threat model" before opening this.
#  2. 22/tcp from the LAN only — SSH for initial bring-up / emergencies.
# Everything else (Tailscale, certbot renewals) is outbound-initiated.
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow "$TERMINAL_PORT"/tcp comment 'public web terminal (GitHub OAuth gated)'
ufw allow from "$LAN_SUBNET" to any port 22 proto tcp comment 'LAN SSH only'
ufw --force enable

# --- fail2ban: sshd jail ---
cat > /etc/fail2ban/jail.local <<'EOF'
[sshd]
enabled = true
maxretry = 5
bantime = 1h
EOF
systemctl enable --now fail2ban

# --- sshd: key-only auth ---
sshd_cfg=/etc/ssh/sshd_config.d/99-hardening.conf
grep -q PasswordAuthentication "$sshd_cfg" 2>/dev/null || cat > "$sshd_cfg" <<'EOF'
PasswordAuthentication no
ChallengeResponseAuthentication no
PermitRootLogin no
EOF
systemctl reload sshd

# --- basics ---
timedatectl set-timezone America/Los_Angeles || true
hostnamectl set-hostname s5 || true
systemctl enable --now avahi-daemon || true

echo "bootstrap done. ufw status:"
ufw status verbose
