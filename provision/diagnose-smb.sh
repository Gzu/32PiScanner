#!/usr/bin/env bash
# diagnose-smb.sh — diagnose SMB upload failures on the field-brain laptop (Samba host).
#
# Walks every layer the Pi's UPLOAD depends on and reports PASS/WARN/FAIL with fixes:
#   service · users · share config · directory ownership (recursive) · filesystem
#   write-as-scanner · AppArmor · smbd log · a real end-to-end SMB write as scanner.
#
# RUN ON THE LAPTOP WITH SUDO (needs to read logs, test as the SMB user, check AppArmor):
#   sudo ./diagnose-smb.sh
#   sudo SMB_PASS='...' ./diagnose-smb.sh      # also runs the end-to-end SMB write test
#
# Env (defaults): SMB_USER=scanner  SHARE=scans  SHARE_DIR=/srv/scans  SERVER=localhost  SMB_PASS=
set -uo pipefail

SMB_USER="${SMB_USER:-scanner}"
SHARE="${SHARE:-scans}"
SHARE_DIR="${SHARE_DIR:-/srv/scans}"
SERVER="${SERVER:-localhost}"

fails=0; warns=0
sec(){ printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
pass(){ printf '  \033[1;32mPASS\033[0m  %s\n' "$*"; }
warn(){ printf '  \033[1;33mWARN\033[0m  %s\n' "$*"; warns=$((warns+1)); }
fail(){ printf '  \033[1;31mFAIL\033[0m  %s\n' "$*"; fails=$((fails+1)); }
info(){ printf '        %s\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "run with sudo (needs logs, sudo -u $SMB_USER, AppArmor)"; exit 1; }
tp(){ testparm -s --section-name="$SHARE" --parameter-name="$1" 2>/dev/null; }

# ── 1. service ───────────────────────────────────────────────────────────────
sec "Samba service"
systemctl is-active --quiet smbd && pass "smbd active" || fail "smbd not active — sudo systemctl start smbd"
ss -ltn 2>/dev/null | grep -q ':445 ' && pass "listening on :445" || fail "nothing listening on :445"

# ── 2. users ─────────────────────────────────────────────────────────────────
sec "Users"
id "$SMB_USER" >/dev/null 2>&1 && pass "system user '$SMB_USER' exists" \
  || fail "no system user '$SMB_USER' — sudo useradd -M -s /usr/sbin/nologin $SMB_USER"
pdbedit -L 2>/dev/null | grep -q "^${SMB_USER}:" && pass "Samba user '$SMB_USER' exists" \
  || fail "no Samba user '$SMB_USER' — sudo smbpasswd -a $SMB_USER"

# ── 3. share config ──────────────────────────────────────────────────────────
sec "Share [$SHARE] config"
if testparm -s 2>/dev/null | grep -q "^\[$SHARE\]"; then
  pass "share [$SHARE] defined"
  p=$(tp path);  info "path        = ${p:-<unset>}"
  [[ "$p" == "$SHARE_DIR" ]] || warn "share path ($p) != expected ($SHARE_DIR)"
  ro=$(tp "read only"); info "read only   = ${ro:-?}"
  [[ "$ro" == "No" ]] || warn "share is read-only — set 'read only = no'"
  fu=$(tp "force user"); [[ -n "$fu" ]] && warn "force user = $fu → smbd writes as $fu, not $SMB_USER (classic error-13 cause)" || info "force user  = (none)"
  vu=$(tp "valid users"); info "valid users = ${vu:-<any>}"
else
  fail "no [$SHARE] share — add it to /etc/samba/smb.conf (guide §1.6)"
fi

# ── 4. share directory + recursive ownership ─────────────────────────────────
sec "Share directory $SHARE_DIR"
if [[ -d "$SHARE_DIR" ]]; then
  pass "exists"; info "$(ls -ld "$SHARE_DIR")"
  [[ "$(stat -c '%U' "$SHARE_DIR")" == "$SMB_USER" ]] && pass "top dir owned by $SMB_USER" \
    || warn "top dir not owned by $SMB_USER"
  bad=$(find "$SHARE_DIR" ! -user "$SMB_USER" 2>/dev/null)
  if [[ -n "$bad" ]]; then
    fail "entries NOT owned by $SMB_USER (scanner can't write into these → error 13):"
    echo "$bad" | head -10 | sed 's/^/          /'
    info "fix: sudo chown -R $SMB_USER:$SMB_USER $SHARE_DIR"
  else
    pass "all entries owned by $SMB_USER"
  fi
else
  fail "$SHARE_DIR missing — sudo mkdir -p $SHARE_DIR && sudo chown $SMB_USER:$SMB_USER $SHARE_DIR"
fi

# ── 5. filesystem write as the SMB user (reproduces the daemon's mkdir+put) ───
sec "Filesystem write as $SMB_USER"
t="_diag_fs_$$"
if sudo -u "$SMB_USER" bash -c "mkdir -p '$SHARE_DIR/$t' && touch '$SHARE_DIR/$t/f'" 2>/dev/null; then
  pass "$SMB_USER can create a subdir + file under $SHARE_DIR"
else
  fail "$SMB_USER CANNOT write a subdir/file under $SHARE_DIR — fix ownership/perms (see §4)"
fi
rm -rf "${SHARE_DIR:?}/$t" 2>/dev/null || true

# ── 6. AppArmor ──────────────────────────────────────────────────────────────
sec "AppArmor"
if command -v aa-status >/dev/null 2>&1 && aa-status 2>/dev/null | grep -qi smbd; then
  warn "smbd is AppArmor-confined — may block $SHARE_DIR (non-default path)"
  info "quick fix: sudo aa-complain /usr/sbin/smbd && sudo systemctl restart smbd"
else
  pass "smbd not AppArmor-confined"
fi
den=$(dmesg 2>/dev/null | grep -iE 'apparmor=.DENIED.*smbd' | tail -3)
[[ -n "$den" ]] && { warn "AppArmor denials for smbd in dmesg:"; echo "$den" | sed 's/^/          /'; }

# ── 7. smbd log ──────────────────────────────────────────────────────────────
sec "Recent smbd errors"
logf=""; for f in /var/log/samba/log.smbd /var/log/samba/log.smbd.old; do [[ -f "$f" ]] && { logf="$f"; break; }; done
if [[ -n "$logf" ]]; then
  errs=$(grep -iE 'denied|NT_STATUS|error 13' "$logf" 2>/dev/null | tail -8)
  [[ -n "$errs" ]] && { warn "recent errors in $logf:"; echo "$errs" | sed 's/^/          /'; } || pass "no recent errors in $logf"
else
  info "no /var/log/samba/log.smbd — checking journald:"
  journalctl -u smbd -n 8 --no-pager 2>/dev/null | grep -iE 'denied|error' | sed 's/^/          /' || info "(nothing)"
fi

# ── 8. end-to-end SMB write as scanner (the real test) ───────────────────────
sec "SMB write as $SMB_USER (end-to-end — reproduces the Pi's UPLOAD)"
if [[ -z "${SMB_PASS:-}" && -t 0 ]]; then
  read -r -s -p "  $SMB_USER SMB password (blank to skip): " SMB_PASS; echo
fi
if [[ -n "${SMB_PASS:-}" ]]; then
  out=$(smbclient "//$SERVER/$SHARE" -U "$SMB_USER%$SMB_PASS" \
        -c 'mkdir _diag_smb; cd _diag_smb; put /etc/hostname h.txt; ls' 2>&1)
  rm -rf "${SHARE_DIR:?}/_diag_smb" 2>/dev/null || true
  if grep -qiE 'NT_STATUS|denied|fail' <<<"$out"; then
    fail "SMB write FAILED — this is the actual upload error:"
    echo "$out" | grep -iE 'NT_STATUS|denied|fail' | sed 's/^/          /'
  else
    pass "SMB write as $SMB_USER succeeded → the server-side upload path is healthy"
  fi
else
  info "skipped — pass SMB_PASS='...' to run the definitive end-to-end write test"
fi

# ── summary ──────────────────────────────────────────────────────────────────
echo
if   [[ $fails -gt 0 ]]; then printf '\033[1;31m%d FAIL, %d WARN\033[0m — fix the FAILs above.\n' "$fails" "$warns"; exit 1
elif [[ $warns -gt 0 ]]; then printf '\033[1;33m0 FAIL, %d WARN\033[0m — likely the warning(s) above.\n' "$warns"
else printf '\033[1;32mall checks passed\033[0m — server side looks healthy; check the Pi/creds next.\n'; fi
