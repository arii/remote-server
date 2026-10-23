# USB install + home network bring-up

From your Linux machine to a booting S5 on Google Nest Wifi, with No-IP
DDNS and certbot. Complements `docs/setup-plan.md` (phases 0–3) — this doc
is the exact commands and clicks.

## 1. Bootable USB (from your Linux machine)

**Download** — Ubuntu Server 24.04 LTS, latest point release:
`https://releases.ubuntu.com/24.04/` → grab
`ubuntu-24.04.5-live-server-amd64.iso` (3.8 GB; point release moves on —
take whatever `-live-server-amd64.iso` is current) **and** `SHA256SUMS`.

**Verify:**
```bash
cd ~/Downloads
sha256sum -c SHA256SUMS 2>/dev/null | grep -i live-server || \
  sha256sum ubuntu-24.04.*-live-server-amd64.iso
# compare against the hash in SHA256SUMS — must match exactly
```

**Flash** (any one):
```bash
# A) dd — identify the stick FIRST, then write (triple-check /dev/sdX)
lsblk -d -o NAME,SIZE,MODEL
# unmount any mounted partitions on it, then:
sudo dd if=ubuntu-24.04.5-live-server-amd64.iso of=/dev/sdX bs=4M status=progress conv=fsync
sync
```
- B) GNOME Disks (ships with Ubuntu desktop): select the USB → ⋮ menu →
  "Restore Disk Image…" → pick the ISO. Safer than `dd`, no command line.
- C) Balena Etcher (`https://etcher.balena.io`) — graphical, verifies the
  write, won't let you pick your system disk.

## 2. Boot the S5, install Ubuntu Server

1. Plug USB into the S5, power on, hammer **F7** for the boot menu
   (per the TRIGKEY S5 manual; **Del** gets you BIOS/UEFI setup instead).
2. Pick the USB stick → "Install Ubuntu Server".
3. Installer essentials:
   - Language/keyboard: your defaults.
   - Network: use Ethernet if you can run a cable for the install (less to
     debug). Wi-Fi works too — pick your Nest SSID here, or configure after
     with the netplan snippet below.
   - Storage: custom layout per `docs/setup-plan.md` (ext4 on LVM:
     80G `/`, 60G `/var`, 30G `/home`, rest unallocated). Guided LVM is
     fine if you'd rather not hand-partition.
   - Profile: your user + strong password.
   - **Install OpenSSH server: yes** (needed for initial setup from your
     Linux box; LAN-only later per the hardening plan).
   - Snaps: skip them all. Reboot, remove USB.

**Wi-Fi via netplan** (if you installed over Ethernet and want to cut the
cable, or need to fix Wi-Fi later) — `/etc/netplan/99-s5-wifi.yaml`:
```yaml
network:
  version: 2
  wifis:
    wlp2s0:                       # check yours: `ip link`
      dhcp4: true
      access-points:
        "YourNestSSID":
          password: "YourWifiPassword"
```
`sudo netplan apply`. Confirm with `ip a` and `ping -c3 1.1.1.1`.
Wired is still the better server choice if the S5 lives near a Nest point
with a LAN port — one less thing to flap at 3 AM.

## 3. Nest Wifi: reserve the IP, forward the port

All in the **Google Home app** (Nest Wifi has no web UI):

1. **DHCP reservation** (so the forward doesn't rot when the lease renews):
   Wi-Fi → Devices → tap the S5 → Settings (gear) → **IP reservation** →
   pick an address (e.g. `192.168.86.50`), save. Note the address.
2. **Port forward** (Google's own path):
   Wi-Fi → Settings (gear) → **Advanced networking** →
   **Port management** → **+** → IPv4 tab → select the S5 →
   external port = internal port = your `TERMINAL_PORT` (e.g. `8443`) →
   protocol **TCP** → Save.
3. **Test from outside your LAN** (phone on cellular, Wi-Fi off):
   `curl -vk https://<your-noip-host>:<port>/ 2>&1 | head -5`
   Expect a TLS handshake (then the OAuth redirect). If it hangs, the
   forward or the S5 firewall is wrong — check `sudo ufw status` on the S5.

Gotchas:
- **Hairpin/NAT loopback**: some Nest setups won't let a LAN device reach
  its own public IP. If the URL works on cellular but not on home Wi-Fi,
  that's why — not a bug in your config. Use the Tailscale IP or LAN IP
  at home.
- **Double NAT**: if your ISP's modem is also a router (not bridged),
  forward the port there too, or put the Nest in bridge mode. Symptom:
  everything looks right and nothing arrives. Check the Nest's WAN IP in
  the Home app — if it's `10.x/192.168.x/172.16.x`, you're double-NATed.
- Nest also has UPnP (Advanced networking → UPnP) — leave it off; the
  manual forward is deterministic and auditable.

## 4. No-IP DUC on the S5

Official client (v3, `.deb` inside the tarball):
```bash
cd /tmp
wget --content-disposition https://www.noip.com/download/linux/latest
tar xf noip-duc_*.tar.gz
cd noip-duc_*/binaries
sudo apt install ./noip-duc_*_amd64.deb
```
Then configure (interactive — email, password, hostname, 30-min interval):
```bash
sudo noip-duc -C        # first-time config; or: noip-duc --help
sudo systemctl enable --now noip-duc
systemctl status noip-duc
```
Verify: `dig +short <your-host>.ddns.net` (or `nslookup`) returns your
public IP — compare with `curl -s ifconfig.me`. Give DNS a few minutes.

(Alternative: `sudo apt install noip2` from Ubuntu's repos is the older
v2 client — fine, but the v3 `.deb` above is No-IP's current path.)

## 5. Certbot on the S5

**If your certs already live on this box** (your existing setup):
```bash
sudo snap install --classic certbot
sudo ln -s /snap/bin/certbot /usr/bin/certbot
certbot certificates     # confirm a valid cert for TERMINAL_DOMAIN
```
That's it — the compose stack mounts `/etc/letsencrypt` read-only and the
deploy-hook (`scripts/install-certbot-deploy-hook.sh`) handles renewals.

**If the certs live on a different machine**, either move them:
```bash
# on the old box — preserves symlinks, perms, and renewal configs:
sudo rsync -aAX /etc/letsencrypt/ s5:/etc/letsencrypt/
```
or issue fresh on the S5 (needs port 80 reachable for the HTTP-01
challenge — add a temporary Nest forward external 80 → internal 80 TCP,
then remove it):
```bash
sudo certbot certonly --standalone -d <your-noip-host> \
  --deploy-hook /path/to/remote-server/scripts/certbot-deploy-hook.sh
```

Renewal dry-run (do this once): `sudo certbot renew --dry-run`.

---
Next: `docs/setup-plan.md` phases 4+ (`scripts/bootstrap.sh`,
`scripts/install-docker.sh`, `.env`, `docker compose up -d --build`).
