#!/usr/bin/env bash
# setup.sh — bring the containerized 32PiScanner field brain up (or down) for testing.
#
# Automates the v1 (compose) path from README.md:
#   • static IP on the rig NIC (the one host-side step)
#   • sets dnsmasq's interface= to that NIC
#   • builds + starts the DHCP (dnsmasq) and NTP (chrony) containers, host-networked
#
# DHCP + NTP only — SMB stays on the host (see ../docs/setup-guide-ubuntu.md §1.6).
# Target: Kali/Debian/Ubuntu with Podman. Run as root (DHCP needs privileged ports).
#
# Usage:
#   sudo RIG_NIC=eth0 ./setup.sh          # up  (build + start)
#   sudo ./setup.sh --down                # down (stop + remove containers, drop static IP)
#
# Env overrides (defaults):  RIG_IP=192.168.50.1  RIG_CIDR=24  CON_NAME=rig
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
RIG_IP="${RIG_IP:-192.168.50.1}"
RIG_CIDR="${RIG_CIDR:-24}"
CON_NAME="${CON_NAME:-rig}"
ACTION="up"; [[ "${1:-}" == "--down" ]] && ACTION="down"

log(){ printf '\033[1;32m[rig]\033[0m %s\n' "$*"; }
err(){ printf '\033[1;31m[rig] ERROR:\033[0m %s\n' "$*" >&2; }

[[ $EUID -eq 0 ]] || { err "run as root (sudo)"; exit 1; }

# Pick a compose front-end: standalone podman-compose, else the `podman compose` plugin.
compose(){
  if command -v podman-compose >/dev/null 2>&1; then podman-compose "$@"
  elif podman compose version >/dev/null 2>&1; then podman compose "$@"
  else err "no compose tool. Install: sudo apt install -y podman podman-compose"; exit 1
  fi
}

# ── teardown ────────────────────────────────────────────────────────────────
if [[ "$ACTION" == "down" ]]; then
  log "stopping + removing containers"
  ( cd "$DIR" && compose down ) || true
  nmcli con delete "$CON_NAME" >/dev/null 2>&1 && log "removed static-IP connection '$CON_NAME'" || true
  log "down. Images kept for fast restart — remove with: sudo podman rmi rig-dhcp rig-ntp"
  exit 0
fi

# ── bring up ────────────────────────────────────────────────────────────────
if [[ -z "${RIG_NIC:-}" ]]; then
  err "RIG_NIC not set. Wired interfaces on this machine:"
  ip -o link show | awk -F': ' '$2 !~ /lo|wl|docker|veth|podman|cni|virbr/ {print "    " $2}' >&2
  err "Re-run:  sudo RIG_NIC=<iface> $0   (Kali wired is usually eth0)"
  exit 1
fi
ip link show "$RIG_NIC" >/dev/null 2>&1 || { err "interface '$RIG_NIC' not found"; exit 1; }
command -v podman >/dev/null 2>&1 || { err "podman not installed. sudo apt install -y podman podman-compose"; exit 1; }

# Host-net containers can't bind :67/:123 if a host daemon already holds them.
if ss -ulnp 2>/dev/null | grep -qE ':(67|123)\b'; then
  err "a host process already binds udp/67 or udp/123 — the host-net containers would fail to start:"
  ss -ulnp | grep -E ':(67|123)\b' >&2
  err "stop the host daemon (dnsmasq / isc-dhcp-server / chrony / ntpd) and retry."
  exit 1
fi

log "static IP $RIG_IP/$RIG_CIDR on $RIG_NIC (NetworkManager con '$CON_NAME')"
nmcli con delete "$CON_NAME" >/dev/null 2>&1 || true
nmcli con add type ethernet ifname "$RIG_NIC" con-name "$CON_NAME" \
    ipv4.method manual ipv4.addresses "$RIG_IP/$RIG_CIDR" \
    ipv4.gateway "" ipv4.dns "" ipv6.method disabled autoconnect yes >/dev/null
nmcli con up "$CON_NAME" >/dev/null

log "pinning dnsmasq interface=$RIG_NIC"
sed -i -E "s/^interface=.*/interface=${RIG_NIC}/" "$DIR/dnsmasq.conf"

log "building + starting containers"
( cd "$DIR" && compose up -d --build )

sleep 1
log "containers:"
podman ps --filter name=rig- --format '  {{.Names}}  {{.Status}}' || true
log "listening (expect dnsmasq :67, chronyd :123):"
ss -ulnp 2>/dev/null | grep -E ':(67|123)\b' | sed 's/^/  /' \
  || err "  no :67/:123 yet — check: sudo podman logs rig-dhcp"

cat <<EOF

$(log "field brain up.")
  python3 ../tools/cli.py ping     # from the laptop — Pis should appear, NTP OK after ~30s
  sudo podman logs -f rig-dhcp     # watch DHCP leases as the Pis boot
  sudo $0 --down                   # tear it all down
EOF
