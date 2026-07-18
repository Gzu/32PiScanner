#!/usr/bin/env bash
# Install picam_node on a fresh Raspberry Pi OS (Bookworm or later).
# Run as root: sudo ./install.sh
#
# Idempotent — re-running upgrades the installed files in place.
set -euo pipefail

NTP_SERVER="${NTP_SERVER:-192.0.2.10}"  # override: NTP_SERVER=10.0.0.5 ./install.sh
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "must run as root (use sudo)" >&2
    exit 1
fi

echo "[install] apt packages"
apt-get update
apt-get install -y --no-install-recommends \
    python3 python3-pip python3-yaml \
    python3-picamera2 \
    chrony \
    smbclient cifs-utils

echo "[install] picam_node files → /opt/picam_node"
install -d /opt/picam_node
install -m 0755 "${REPO_ROOT}/node/picam_node.py" /opt/picam_node/picam_node.py

echo "[install] runtime dirs"
install -d -m 0755 /var/lib/picam_node/captures
install -d -m 0750 /etc/picam_node
install -d -m 0700 /etc/picam_node/credentials

if [[ ! -f /etc/picam_node/credentials/default ]]; then
    cat > /etc/picam_node/credentials/default <<'EOF'
# Replace with real SMB credentials for the RC box share.
# Format below is what smbclient -A consumes.
username=scanner
password=CHANGE_ME
domain=WORKGROUP
EOF
    chmod 0600 /etc/picam_node/credentials/default
    echo "[install] wrote placeholder /etc/picam_node/credentials/default — EDIT THIS"
fi

echo "[install] chrony client config (server=${NTP_SERVER})"
sed "s|192\.0\.2\.10|${NTP_SERVER}|" "${REPO_ROOT}/provision/chrony-client.conf" \
    > /etc/chrony/chrony.conf
systemctl restart chrony

echo "[install] systemd unit"
install -m 0644 "${REPO_ROOT}/provision/picam_node.service" \
    /etc/systemd/system/picam_node.service
systemctl daemon-reload
systemctl enable picam_node.service

echo "[install] first-boot hostname rule (only on truly fresh image)"
if [[ "$(hostname)" == "raspberrypi" ]]; then
    install -m 0755 "${REPO_ROOT}/provision/first-boot.sh" /usr/local/sbin/first-boot.sh
    cat > /etc/systemd/system/first-boot.service <<'EOF'
[Unit]
Description=Set hostname from MAC on first boot
After=network.target
ConditionHost=raspberrypi

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/first-boot.sh

[Install]
WantedBy=multi-user.target
EOF
    systemctl enable first-boot.service
    echo "[install] hostname will be set on next reboot"
fi

echo "[install] starting picam_node"
systemctl restart picam_node.service
sleep 1
systemctl is-active --quiet picam_node.service && \
    echo "[install] ✓ picam_node is running" || \
    { echo "[install] ✗ picam_node failed — check journalctl -u picam_node"; exit 1; }

echo
echo "Done. Verify:"
echo "  chronyc tracking            # offset should drop below 5ms within 60s"
echo "  systemctl status picam_node"
echo "  hostname                    # should be pi-XXXXXX after reboot"
