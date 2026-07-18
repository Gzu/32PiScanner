#!/usr/bin/env bash
# Runs once on first boot to derive a stable hostname from the eth0 MAC,
# then disables itself. Triggered by /etc/systemd/system/first-boot.service
# installed by install.sh.
set -euo pipefail

# eth0 MAC last 6 hex chars (without colons) — e.g. "a1b2c3"
MAC=$(cat /sys/class/net/eth0/address | tr -d ':' | tail -c 7 | head -c 6)
NEW_HOSTNAME="pi-${MAC}"

echo "[first-boot] setting hostname to ${NEW_HOSTNAME}"
hostnamectl set-hostname "${NEW_HOSTNAME}"

# Update /etc/hosts so sudo doesn't complain
sed -i "s/127\.0\.1\.1.*/127.0.1.1\t${NEW_HOSTNAME}/" /etc/hosts || \
    echo "127.0.1.1	${NEW_HOSTNAME}" >> /etc/hosts

# Disable ourselves so we don't run again
systemctl disable first-boot.service
rm -f /etc/systemd/system/first-boot.service

echo "[first-boot] done — rebooting"
reboot
