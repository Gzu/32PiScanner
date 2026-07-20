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
#   POOL     DHCP pool to ARP-scan if leases are empty (default: 192.168.50.100-200)
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DAEMON="${DAEMON:-$REPO/node/picam_node.py}"
DEST="${DEST:-/opt/picam_node/picam_node.py}"

log(){ printf '\033[1;32m[update-pis]\033[0m %s\n' "$*"; }
err(){ printf '\033[1;31m[update-pis] ERROR:\033[0m %s\n' "$*" >&2; }

: "${SSH_USER:?set SSH_USER=<pi-login-user>  (e.g. SSH_USER=pi ./update-pis.sh)}"
[[ -f "$DAEMON" ]] || { err "daemon file not found: $DAEMON"; exit 1; }

# ── collect target IPs (same discovery as fix-pi-hostnames.sh) ───────────────
POOL="${POOL:-192.168.50.100-200}"     # DHCP pool to scan when leases are unavailable

# Raw lease file from env / host / container (whichever is readable).
_read_leases() {
  if   [[ -n "${LEASES:-}" && -r "$LEASES" ]];                then cat "$LEASES"
  elif [[ -r /var/lib/misc/dnsmasq.leases ]];                 then cat /var/lib/misc/dnsmasq.leases
  elif sudo test -r /var/lib/misc/dnsmasq.leases 2>/dev/null; then sudo cat /var/lib/misc/dnsmasq.leases
  elif command -v podman >/dev/null 2>&1;                     then sudo podman exec rig-dhcp cat /var/lib/misc/dnsmasq.leases 2>/dev/null
  fi
}

# Live hosts in POOL via an UNPRIVILEGED ARP sweep — the fallback when the lease
# file is empty (e.g. rig-dhcp was rebuilt after the Pis leased). ARP resolves at
# L2, so this finds Pis even if they filter ICMP.
_scan_pool() {
  local base="${POOL%.*}" last="${POOL##*.}" s e i
  s="${last%-*}"; e="${last#*-}"
  [[ "$s" =~ ^[0-9]+$ && "$e" =~ ^[0-9]+$ ]] || { err "bad POOL '$POOL' (want A.B.C.START-END)"; return 1; }
  for ((i=s; i<=e; i++)); do ping -c1 -W1 "${base}.${i}" >/dev/null 2>&1 & done
  wait
  ip -4 neigh show 2>/dev/null | awk -v b="${base}." 'index($1,b)==1 && $NF !~ /FAILED|INCOMPLETE/ {print $1}'
}

ips=("$@")
if [[ ${#ips[@]} -eq 0 ]]; then
  log "no IPs given — reading DHCP leases"
  raw=$(_read_leases)
  [[ -n "$raw" ]] && ips=($(echo "$raw" | awk '{print $3}' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | sort -u))
  if [[ ${#ips[@]} -eq 0 ]]; then
    log "no leases — ARP-scanning pool $POOL"
    ips=($(_scan_pool | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | sort -u))
    self=$(ip -4 -o addr show 2>/dev/null | awk '{sub(/\/.*/,"",$4); print $4}')   # never target ourselves
    tmp=(); for x in "${ips[@]}"; do grep -qxF "$x" <<<"$self" || tmp+=("$x"); done; ips=("${tmp[@]}")
  fi
  [[ ${#ips[@]} -gt 0 ]] || { err "no Pis found (no leases + empty scan of $POOL). Pass IPs, or set LEASES=/POOL=."; exit 1; }
fi
[[ ${#ips[@]} -gt 0 ]] || { err "no target IPs found"; exit 1; }

log "pushing $(basename "$DAEMON") ($(wc -c <"$DAEMON" | tr -d ' ') bytes) -> $DEST on ${#ips[@]} Pi(s)"

# BatchMode: fail fast instead of prompting for a password (this script also
# runs headless under tools/gui.py). ServerAlive*: a Pi dying mid-transfer
# would otherwise hang the established TCP session for many minutes.
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=5
          -o BatchMode=yes -o ServerAliveInterval=5 -o ServerAliveCountMax=3)
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
