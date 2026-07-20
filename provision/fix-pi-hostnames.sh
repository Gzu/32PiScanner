#!/usr/bin/env bash
# fix-pi-hostnames.sh — re-derive each Pi's hostname from its own eth0 MAC.
#
# Fixes the cloned-SD DUPLICATE-HOSTNAME problem: if master.img was imaged AFTER the
# master ran first-boot.sh, that script had already set a fixed hostname and deleted
# itself — so every clone boots with the MASTER's hostname. They then all report the
# same `pi` field and `cli.py ping` collapses them to a single reply.
#
# This SSHes into each Pi (over the rig LAN, addressed by IP — which is unique even
# when hostnames aren't), sets pi-<MAC6>, fixes /etc/hosts, and restarts picam_node.
# Idempotent: a Pi already named correctly is left as-is.
#
# RUN FROM THE FIELD-BRAIN LAPTOP (not a Pi). Requirements:
#   • SSH access to the Pis — key auth strongly recommended (ssh-copy-id <user>@<ip>),
#     otherwise you'll type each Pi's password.
#   • passwordless sudo on the Pis (Raspberry Pi OS default for the Imager-created user).
#
# Usage:
#   SSH_USER=pi ./fix-pi-hostnames.sh                         # discover IPs from DHCP leases
#   SSH_USER=pi ./fix-pi-hostnames.sh 192.168.50.101 .102 …   # or pass IPs explicitly
#
# Env:
#   SSH_USER   (required) the Pi login user
#   LEASES     (optional) path to a dnsmasq leases file; auto-detected otherwise
#   POOL       (optional) DHCP pool to ARP-scan if leases are empty (default 192.168.50.100-200)
set -uo pipefail

log(){ printf '\033[1;32m[fix-hostnames]\033[0m %s\n' "$*"; }
err(){ printf '\033[1;31m[fix-hostnames] ERROR:\033[0m %s\n' "$*" >&2; }

: "${SSH_USER:?set SSH_USER=<pi-login-user>  (e.g. SSH_USER=pi ./fix-pi-hostnames.sh)}"

# ── collect target IPs ───────────────────────────────────────────────────────
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
  # dnsmasq lease line: <expiry> <MAC> <IP> <hostname> <clientid>; grab field 3.
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
log "targets (${#ips[@]}): ${ips[*]}"

# ── remote: derive pi-<MAC6>, set hostname, fix /etc/hosts, restart the daemon ─
# Single-quoted so ${NEW} etc. expand on the PI, not the laptop.
REMOTE='
  set -e
  MAC=$(cat /sys/class/net/eth0/address | tr -d ":" | tail -c 7 | head -c 6)
  NEW="pi-${MAC}"
  CUR=$(hostname)
  if [ "$CUR" != "$NEW" ]; then
    sudo hostnamectl set-hostname "$NEW"
    sudo sed -i "s/127\.0\.1\.1.*/127.0.1.1\t${NEW}/" /etc/hosts
    sudo systemctl restart picam_node 2>/dev/null || true
    echo "changed: ${CUR} -> ${NEW}"
  else
    echo "ok: ${NEW} (already correct)"
  fi
'

ok=0; fail=0
for ip in "${ips[@]}"; do
  printf '  %-15s ' "$ip"
  if out=$(ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 \
              "${SSH_USER}@${ip}" "$REMOTE" 2>&1); then
    echo "$out"; ok=$((ok+1))
  else
    echo "FAILED: $out"; fail=$((fail+1))
  fi
done

log "done: ${ok} ok, ${fail} failed"
log "verify:  python3 tools/cli.py ping --expected ${#ips[@]}"
[[ $fail -eq 0 ]]
