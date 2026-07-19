#!/usr/bin/env bash
# teardown-fieldbrain-ubuntu.sh — reverse everything setup-fieldbrain-ubuntu.sh did.
#
# Reverts, in order: services (stop+disable) → sleep mask → static-IP connection →
# host packages (apt purge) → optionally the SMB user + captured-image dir.
#
# SAFE BY DEFAULT:
#   • Captured images in the share dir are KEPT (that's your data). Use --purge-data
#     to also delete /srv/scans and the 'scanner' user.
#   • Config backups (*.orig the setup made) are left on disk for manual recovery;
#     their locations are printed at the end.
#
# Flags:
#   --keep-smb      Leave Samba (package/service/share/user) intact — use this when
#                   switching to the container path, where SMB stays on the host.
#                   Only DHCP+NTP (dnsmasq/chrony) and the static IP are removed.
#   --purge-data    Also delete SHARE_DIR and the SMB user (destroys captured images).
#   --dry-run       Print what would happen, change nothing.
#   --yes           Skip the confirmation prompt.
#
# Env overrides (match the setup script): CON_NAME=rig  SMB_USER=scanner  SHARE_DIR=/srv/scans
#
# Usage:
#   sudo ./teardown-fieldbrain-ubuntu.sh                 # full revert, keeps images
#   sudo ./teardown-fieldbrain-ubuntu.sh --keep-smb      # prep for the container path
#   sudo ./teardown-fieldbrain-ubuntu.sh --purge-data --yes
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

CON_NAME="${CON_NAME:-rig}"
SMB_USER="${SMB_USER:-scanner}"
SHARE_DIR="${SHARE_DIR:-/srv/scans}"
SLEEP_TARGETS=(sleep.target suspend.target hibernate.target hybrid-sleep.target)

KEEP_SMB=0; PURGE_DATA=0; DRY=0; YES=0
for a in "$@"; do case "$a" in
  --keep-smb)   KEEP_SMB=1 ;;
  --purge-data) PURGE_DATA=1 ;;
  --dry-run)    DRY=1 ;;
  --yes|-y)     YES=1 ;;
  *) echo "unknown flag: $a" >&2; exit 2 ;;
esac; done

log(){ printf '\033[1;32m[teardown]\033[0m %s\n' "$*"; }
err(){ printf '\033[1;31m[teardown] ERROR:\033[0m %s\n' "$*" >&2; }
run(){ if [[ $DRY -eq 1 ]]; then printf '  would: %s\n' "$*"; else "$@"; fi; }

[[ $EUID -eq 0 ]] || { err "run as root (sudo)"; exit 1; }

# Build the package + service lists (Samba included unless --keep-smb).
PKGS=(chrony dnsmasq)
SVCS=(dnsmasq chrony)
if [[ $KEEP_SMB -eq 0 ]]; then PKGS+=(samba smbclient); SVCS+=(smbd); fi

# ── plan ────────────────────────────────────────────────────────────────────
log "plan:"
echo "  • stop + disable services:      ${SVCS[*]}"
echo "  • unmask sleep targets:         ${SLEEP_TARGETS[*]}"
echo "  • delete static-IP connection:  ${CON_NAME}"
echo "  • apt purge packages:           ${PKGS[*]}  (+ autoremove)"
if [[ $KEEP_SMB -eq 1 ]]; then
  echo "  • Samba: KEPT (--keep-smb) — service, share, user '${SMB_USER}', ${SHARE_DIR} untouched"
elif [[ $PURGE_DATA -eq 1 ]]; then
  echo "  • DELETE captured data:         ${SHARE_DIR} + SMB user '${SMB_USER}'   ⚠"
else
  echo "  • captured images:              KEPT in ${SHARE_DIR} (use --purge-data to remove)"
fi
[[ $DRY -eq 1 ]] && { log "dry-run — nothing changed."; exit 0; }

if [[ $YES -eq 0 ]]; then
  read -r -p "Proceed? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || { log "aborted."; exit 0; }
fi

# ── execute ─────────────────────────────────────────────────────────────────
log "stopping + disabling services"
for s in "${SVCS[@]}"; do
  run systemctl disable --now "$s" 2>/dev/null || true
done

log "unmasking sleep targets"
run systemctl unmask "${SLEEP_TARGETS[@]}" 2>/dev/null || true

log "removing static-IP connection '${CON_NAME}'"
run nmcli con delete "$CON_NAME" 2>/dev/null || true

if [[ $PURGE_DATA -eq 1 && $KEEP_SMB -eq 0 ]]; then
  log "deleting SMB user + captured data"
  if id "$SMB_USER" >/dev/null 2>&1; then
    run smbpasswd -x "$SMB_USER" 2>/dev/null || true
    run userdel "$SMB_USER" 2>/dev/null || true
  fi
  run rm -rf "$SHARE_DIR"
fi

log "purging packages: ${PKGS[*]}"
run apt-get purge -y "${PKGS[@]}" || true
run apt-get autoremove -y --purge || true

# ── report ──────────────────────────────────────────────────────────────────
log "✓ teardown complete."
if [[ $KEEP_SMB -eq 1 ]]; then
  log "Samba left running for the container path. Next: cd ../containers && sudo RIG_NIC=<nic> ./setup.sh"
fi
LEFT=$(ls -1 /etc/chrony/chrony.conf.orig /etc/dnsmasq.conf.orig /etc/samba/smb.conf.orig 2>/dev/null || true)
if [[ -n "$LEFT" ]]; then
  log "config backups left on disk (delete manually if you don't need them):"
  echo "$LEFT" | sed 's/^/  /'
fi
if [[ $PURGE_DATA -eq 0 && $KEEP_SMB -eq 0 && -d "$SHARE_DIR" ]]; then
  log "kept your captured images in ${SHARE_DIR} (re-run with --purge-data to remove)."
fi
