#!/usr/bin/env bash
# update-pis.sh — push the picam_node daemon to every Pi and restart it.
#
# Deploys node/picam_node.py to /opt/picam_node/picam_node.py on each Pi over the rig
# LAN (scp), then restarts the picam_node service. Use this to roll out daemon changes
# (new protocol verbs, bug fixes) without re-flashing SD cards or SSHing one by one.
# The rig is offline, so this copies the file from the laptop — the Pis never need
# internet or a git remote.
#
# RUN FROM THE FIELD-BRAIN LAPTOP. First: `git pull` here so you push the latest code.
# Requirements:
#   • the repo present locally (daemon read from <repo>/node/picam_node.py)
#   • SSH access to the Pis — key auth recommended (ssh-copy-id <user>@<ip>)
#   • passwordless sudo on the Pis (Raspberry Pi OS default for the Imager user)
#
# Usage:
#   SSH_USER=pi ./update-pis.sh                       # discover IPs from DHCP leases
#   SSH_USER=pi ./update-pis.sh 192.168.50.101 .102   # or pass IPs explicitly
#
# Env overrides:
#   SSH_USER (required)  Pi login user
#   DAEMON   path to picam_node.py    (default: <repo>/node/picam_node.py)
#   DEST     install path on the Pi   (default: /opt/picam_node/picam_node.py)
#   LEASES   dnsmasq leases file       (auto-detected otherwise)
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DAEMON="${DAEMON:-$REPO/node/picam_node.py}"
DEST="${DEST:-/opt/picam_node/picam_node.py}"

log(){ printf '\033[1;32m[update-pis]\033[0m %s\n' "$*"; }
err(){ printf '\033[1;31m[update-pis] ERROR:\033[0m %s\n' "$*" >&2; }

: "${SSH_USER:?set SSH_USER=<pi-login-user>  (e.g. SSH_USER=pi ./update-pis.sh)}"
[[ -f "$DAEMON" ]] || { err "daemon file not found: $DAEMON"; exit 1; }

# ── collect target IPs (same discovery as fix-pi-hostnames.sh) ───────────────
ips=("$@")
if [[ ${#ips[@]} -eq 0 ]]; then
  log "no IPs given — discovering from DHCP leases"
  raw=""
  if [[ -n "${LEASES:-}" && -r "$LEASES" ]]; then
    raw=$(cat "$LEASES")
  elif [[ -r /var/lib/misc/dnsmasq.leases ]]; then           # bare-metal dnsmasq
    raw=$(cat /var/lib/misc/dnsmasq.leases)
  elif sudo test -r /var/lib/misc/dnsmasq.leases 2>/dev/null; then
    raw=$(sudo cat /var/lib/misc/dnsmasq.leases)
  elif command -v podman >/dev/null 2>&1; then               # container path
    raw=$(sudo podman exec rig-dhcp cat /var/lib/misc/dnsmasq.leases 2>/dev/null)
  fi
  [[ -n "$raw" ]] || { err "could not read DHCP leases. Pass IPs explicitly, or set LEASES=<path>."; exit 1; }
  ips=($(echo "$raw" | awk '{print $3}' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | sort -u))
fi
[[ ${#ips[@]} -gt 0 ]] || { err "no target IPs found"; exit 1; }

log "pushing $(basename "$DAEMON") ($(wc -c <"$DAEMON" | tr -d ' ') bytes) -> $DEST on ${#ips[@]} Pi(s)"

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=5)
REMOTE="sudo -n install -m0755 /tmp/picam_node.py '$DEST' \
        && sudo -n systemctl restart picam_node \
        && systemctl is-active --quiet picam_node && echo updated"

ok=0; fail=0
for ip in "${ips[@]}"; do
  printf '  %-15s ' "$ip"
  if ! scp_err=$(scp "${SSH_OPTS[@]}" -q "$DAEMON" "${SSH_USER}@${ip}:/tmp/picam_node.py" 2>&1); then
    echo "FAILED (scp): $scp_err"; fail=$((fail+1)); continue
  fi
  if out=$(ssh "${SSH_OPTS[@]}" "${SSH_USER}@${ip}" "$REMOTE" 2>&1); then
    echo "$out"; ok=$((ok+1))
  else
    echo "FAILED (ssh): $out"; fail=$((fail+1))
  fi
done

log "done: ${ok} updated, ${fail} failed"
log "verify:  python3 tools/cli.py ping --expected ${#ips[@]}"
[[ $fail -eq 0 ]]
