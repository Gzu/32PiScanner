#!/usr/bin/env bash
# diagnose-pis.sh — find Pis that are ON the network but NOT answering cli.py ping.
#
# 1. discovers live Pis (DHCP leases, else ARP sweep of POOL)
# 2. asks each over SSH for its pi_id (eth0 MAC last-6 — what the daemon reports)
# 3. runs cli.py ping and sees which pi_ids actually answered over UDP/9999
# 4. reports MISSING Pis (SSH-reachable but silent) and any DUPLICATE pi_ids
# 5. SSHes into each missing Pi and dumps: service state, :9999 listener, daemon
#    version (MAC- vs hostname-based), and the last few journal lines
#
# RUN FROM THE FIELD-BRAIN LAPTOP. Needs SSH key access to the Pis (setup-pi-keys.sh).
#
# Usage:   SSH_USER=pi ./diagnose-pis.sh [ip ...]
# Env:     SSH_USER (required)  POOL (default 192.168.50.100-200)  LEASES  PING_TIMEOUT (10)
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CLI="$REPO/tools/cli.py"
POOL="${POOL:-192.168.50.100-200}"
PING_TIMEOUT="${PING_TIMEOUT:-10}"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 -o BatchMode=yes)

log(){ printf '\033[1;32m[diagnose]\033[0m %s\n' "$*"; }
err(){ printf '\033[1;31m[diagnose]\033[0m %s\n' "$*" >&2; }

: "${SSH_USER:?set SSH_USER=<pi-login-user>}"
[[ -f "$CLI" ]] || { err "cli.py not found at $CLI"; exit 1; }

# ── discovery (leases, else unprivileged ARP sweep) ──────────────────────────
_read_leases() {
  if   [[ -n "${LEASES:-}" && -r "$LEASES" ]];                then cat "$LEASES"
  elif [[ -r /var/lib/misc/dnsmasq.leases ]];                 then cat /var/lib/misc/dnsmasq.leases
  elif sudo test -r /var/lib/misc/dnsmasq.leases 2>/dev/null; then sudo cat /var/lib/misc/dnsmasq.leases
  elif command -v podman >/dev/null 2>&1;                     then sudo podman exec rig-dhcp cat /var/lib/misc/dnsmasq.leases 2>/dev/null
  fi
}
_scan_pool() {
  local base="${POOL%.*}" last="${POOL##*.}" s e i
  s="${last%-*}"; e="${last#*-}"
  [[ "$s" =~ ^[0-9]+$ && "$e" =~ ^[0-9]+$ ]] || { err "bad POOL '$POOL'"; return 1; }
  for ((i=s; i<=e; i++)); do ping -c1 -W1 "${base}.${i}" >/dev/null 2>&1 & done; wait
  ip -4 neigh show 2>/dev/null | awk -v b="${base}." 'index($1,b)==1 && $NF !~ /FAILED|INCOMPLETE/ {print $1}'
}

ips=("$@")
if [[ ${#ips[@]} -eq 0 ]]; then
  raw=$(_read_leases)
  [[ -n "$raw" ]] && ips=($(echo "$raw" | awk '{print $3}' | grep -E '^[0-9.]+$' | sort -u))
  if [[ ${#ips[@]} -eq 0 ]]; then
    log "no leases — ARP-scanning $POOL"
    ips=($(_scan_pool | grep -E '^[0-9.]+$' | sort -u))
    self=$(ip -4 -o addr show 2>/dev/null | awk '{sub(/\/.*/,"",$4); print $4}')
    tmp=(); for x in "${ips[@]}"; do grep -qxF "$x" <<<"$self" || tmp+=("$x"); done; ips=("${tmp[@]}")
  fi
fi
[[ ${#ips[@]} -gt 0 ]] || { err "no Pis found. Pass IPs, or set LEASES=/POOL=."; exit 1; }
log "live on network: ${#ips[@]}"

# ── map each IP → pi_id (over SSH) ───────────────────────────────────────────
declare -A ID_OF IPS_OF
sshfail=()
for ip in "${ips[@]}"; do
  mac=$(ssh "${SSH_OPTS[@]}" "${SSH_USER}@${ip}" 'cat /sys/class/net/eth0/address' 2>/dev/null | tr -d ':\r\n ')
  if [[ ${#mac} -ge 6 ]]; then
    id="pi-${mac: -6}"
  else
    id="(ssh-failed)"; sshfail+=("$ip")
  fi
  ID_OF[$ip]="$id"
  IPS_OF[$id]="${IPS_OF[$id]:-}${IPS_OF[$id]:+ }$ip"
done

# ── who actually answered ping over UDP ──────────────────────────────────────
log "pinging (timeout ${PING_TIMEOUT}s)…"
responded=$(python3 "$CLI" ping --timeout "$PING_TIMEOUT" 2>/dev/null | grep -oE 'pi-[0-9a-f]{6}' | sort -u)
resp_count=$(grep -c . <<<"$responded" || true)

# ── report ───────────────────────────────────────────────────────────────────
echo
log "SSH-reachable: $(( ${#ips[@]} - ${#sshfail[@]} ))   answered ping: ${resp_count}"

# duplicate pi_ids (collapse to one ping entry — a hidden way to lose Pis)
for id in "${!IPS_OF[@]}"; do
  [[ "$id" == "(ssh-failed)" ]] && continue
  n=$(wc -w <<<"${IPS_OF[$id]}")
  [[ "$n" -gt 1 ]] && err "DUPLICATE pi_id $id on $n IPs: ${IPS_OF[$id]}  (they collapse to 1 ping reply)"
done

[[ ${#sshfail[@]} -gt 0 ]] && err "SSH failed (couldn't fingerprint): ${sshfail[*]}"

# missing = SSH-reachable pi_id that did NOT answer ping
missing=()
for ip in "${ips[@]}"; do
  id="${ID_OF[$ip]}"
  [[ "$id" == "(ssh-failed)" ]] && continue
  grep -qx "$id" <<<"$responded" || missing+=("$ip")
done

if [[ ${#missing[@]} -eq 0 ]]; then
  log "✓ every SSH-reachable Pi answered ping"
  exit 0
fi

err "${#missing[@]} Pi(s) reachable via SSH but SILENT on ping — inspecting:"
REMOTE='
  echo -n "  service:        "; systemctl is-active picam_node 2>/dev/null || true
  echo -n "  udp/9999:        "; ss -uln 2>/dev/null | grep -q ":9999" && echo LISTENING || echo "NOT LISTENING"
  echo -n "  daemon version:  "; grep -q "class/net/eth0" /opt/picam_node/picam_node.py 2>/dev/null \
      && echo "MAC-based (new)" || echo "OLD hostname-based -> redeploy with update-pis.sh"
  echo    "  recent logs:"
  { sudo -n journalctl -u picam_node -n 5 --no-pager 2>/dev/null \
      || journalctl -u picam_node -n 5 --no-pager 2>/dev/null \
      || echo "(no journal access)"; } | sed "s/^/    /"
'
for ip in "${missing[@]}"; do
  echo "───── $ip  (${ID_OF[$ip]}) ─────"
  ssh "${SSH_OPTS[@]}" "${SSH_USER}@${ip}" "$REMOTE" 2>&1 || echo "  (ssh failed during inspection)"
done

echo
log "hints: NOT LISTENING -> 'sudo systemctl restart picam_node' on that Pi;"
log "       OLD daemon -> re-run update-pis.sh;  DUPLICATE pi_id -> colliding MAC tails."
