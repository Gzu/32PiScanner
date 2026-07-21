#!/usr/bin/env bash
# setup-pi-keys.sh — install the laptop's SSH public key on every Pi (ssh-copy-id).
#
# One-time setup so the fleet scripts (update-pis.sh, fix-pi-hostnames.sh) run
# passwordless under BatchMode. THIS is the one step where you type each Pi's
# password — after it, key auth works and the deploy scripts never prompt.
#
# Interactive by design (NOT BatchMode): prompts for each Pi's login password once,
# sequentially. Generates an ed25519 key on the laptop if you don't already have one.
# ssh-copy-id is idempotent — a Pi that already has the key is skipped.
#
# RUN FROM THE LAPTOP. Usage:
#   SSH_USER=pi ./setup-pi-keys.sh                       # discover IPs (leases / ARP scan)
#   SSH_USER=pi ./setup-pi-keys.sh 192.168.50.101 .102   # explicit IPs
#
# Env:
#   SSH_USER (required) Pi login user
#   KEY      SSH key path (default ~/.ssh/id_ed25519; generated passphraseless if missing)
#   LEASES   dnsmasq leases file (auto-detected otherwise)
#   POOL     DHCP pool to ARP-scan if leases are empty (default 192.168.50.100-200)
set -uo pipefail

KEY="${KEY:-$HOME/.ssh/id_ed25519}"
POOL="${POOL:-192.168.50.100-200}"

log(){ printf '\033[1;32m[pi-keys]\033[0m %s\n' "$*"; }
err(){ printf '\033[1;31m[pi-keys] ERROR:\033[0m %s\n' "$*" >&2; }

: "${SSH_USER:?set SSH_USER=<pi-login-user>  (e.g. SSH_USER=pi ./setup-pi-keys.sh)}"
command -v ssh-copy-id >/dev/null 2>&1 || { err "ssh-copy-id not found (install openssh-client)"; exit 1; }

# ── ensure we have a key ─────────────────────────────────────────────────────
if [[ ! -f "$KEY.pub" ]]; then
  log "no key at $KEY.pub — generating an ed25519 key (no passphrase)"
  mkdir -p "$(dirname "$KEY")" && chmod 700 "$(dirname "$KEY")" 2>/dev/null || true
  ssh-keygen -t ed25519 -f "$KEY" -N "" -C "32piscanner-fieldbrain" \
    || { err "ssh-keygen failed"; exit 1; }
fi

# ── discovery: leases -> ARP scan -> explicit args ───────────────────────────
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

log "installing $KEY.pub on ${#ips[@]} Pi(s) as '$SSH_USER' — type each Pi's password when asked"

ok=0; fail=0
for ip in "${ips[@]}"; do
  log "→ $ip"
  if ssh-copy-id -i "$KEY.pub" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 \
       "${SSH_USER}@${ip}"; then
    ok=$((ok+1))
  else
    err "  failed on $ip"; fail=$((fail+1))
  fi
done

log "done: ${ok} ok, ${fail} failed"
log "test:  ssh ${SSH_USER}@<ip> true      (should NOT prompt for a password now)"
log "next:  SSH_USER=${SSH_USER} ./provision/update-pis.sh"
[[ $fail -eq 0 ]]
