# Provisioning a Pi for 32PiScanner

> **⚠️ SUPERSEDED (2026-06-04).** This describes the original topology where a
> **Windows RC box served NTP**. The current architecture uses a **Linux laptop
> field brain** (DHCP + NTP + SMB) with the Windows desktop for **reconstruction
> only**. For the current end-to-end setup, follow
> **[`setup-guide-ubuntu.md`](setup-guide-ubuntu.md)** (Debian/Kali) instead. The **Pi-side steps
> here** (base image, `install.sh`, SD cloning, camera checks) are still accurate —
> only the NTP/SMB *server* moved from Windows to the Linux laptop.

End-to-end bring-up for one Pi. Repeat 32×, or flash one and `dd` the SD.

## 1. Base image

1. Flash **Raspberry Pi OS Lite (64-bit, Bookworm or newer)** with Raspberry
   Pi Imager.
2. In Imager's advanced settings (Ctrl-Shift-X):
   - Set username/password.
   - **Do not** set a custom hostname — leave it `raspberrypi` so the
     first-boot script can derive a unique one from the MAC.
   - Configure your wired LAN (or temporary Wi-Fi for initial setup).
   - Enable SSH.
3. Boot the Pi, SSH in. Confirm `libcamera-hello --list-cameras` shows the
   sensor (`imx219` for v2, `ov5647` for v1).

## 2. Pick an NTP source — once, for the whole rig

You need exactly one always-on time source on the LAN. Two paths:

### Path A (recommended) — Windows RC box with Meinberg NTP

1. On the RC box, download and install
   [Meinberg NTP for Windows](https://www.meinbergglobal.com/english/sw/ntp.htm).
2. Accept defaults. The installer opens Windows Firewall for UDP/123.
3. Note the RC box's static LAN IP — this is your `NTP_SERVER`.

### Path B — designate one Pi

On the chosen "server" Pi, instead of running `install.sh` straight through:
1. Copy `provision/chrony-server-pi.conf` to `/etc/chrony/chrony.conf` (edit
   the `allow` line for your subnet).
2. `systemctl restart chrony`.
3. Note this Pi's LAN IP — it's your `NTP_SERVER`.

## 3. Per-Pi install

On each capture Pi:

```bash
git clone <this repo> ~/32PiScanner   # or scp the repo over
cd ~/32PiScanner/provision
sudo NTP_SERVER=192.168.1.10 ./install.sh   # ← use your real NTP server IP
sudo reboot                                   # only needed first time, for hostname
```

After reboot:
- `hostname` should now be `pi-XXXXXX` (last 6 of the eth0 MAC).
- `systemctl status picam_node` should show *active (running)*.
- `chronyc tracking` should report `Last offset` under ~5 ms within a minute.

## 4. Smoke test from your laptop

On any machine on the same LAN:

```bash
cd 32PiScanner/tools
python3 cli.py ping
```

You should see one `✓ pi-XXXXXX  PONG ...` line per live Pi.

```bash
python3 cli.py configure --exposure-us 2000 --gain 4.0
python3 cli.py capture --session smoke01 --leadtime 2.0
```

`capture` prints a "trigger spread across rig" number — the delta between the
earliest and latest `actual_at_unix`. **Healthy values on wired Gigabit are
under 5 ms.** Anything above 20 ms means chrony hasn't converged yet (give it
a minute) or one Pi is on a slower link.

## 5. RC box setup

1. Create a shared folder: `C:\scans` → right-click → Properties → Sharing →
   Advanced Sharing → name `scans`, grant Change permission to a dedicated
   `scanner` Windows account.
2. Edit `/etc/picam_node/credentials/default` on each Pi with that account's
   username + password. (Or replace with NFS — see protocol doc.)
3. From your laptop:
   ```bash
   python3 cli.py upload --session smoke01 --dest smb://<rc-box-ip>/scans/
   ```
4. On the RC box, `C:\scans\smoke01\` should now contain 32 JPEGs named
   `pi-XXXXXX.jpg`.
5. Point RealityCapture at that folder.

## 6. Cloning the SD (the lazy 32× option)

Once one Pi works:

1. Shut it down, pull the SD card.
2. `sudo dd if=/dev/sdX of=master.img bs=4M status=progress` (Linux/Mac).
3. Flash `master.img` to 31 more SD cards.
4. Boot each — the first-boot service re-derives the hostname from each card's
   host Pi's MAC, so you never get hostname collisions.

The credentials file and chrony config are identical across all Pis on purpose.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `clock_unsynced` errors from `CAPTURE` | chrony hasn't converged or NTP server unreachable | `chronyc tracking` — if `Stratum` is 0 or offset > 50 ms, check the NTP server is up and firewall allows UDP/123. |
| `trigger spread > 20 ms` consistently | one Pi on slower link, or NTP server flaky | Test the outlier Pi individually; consider Path B (Pi-as-NTP) for lower jitter. |
| `camera_unavailable` in PONG | sensor not detected | `libcamera-hello --list-cameras` — if empty, check ribbon cable orientation, `dtparam=camera` in `/boot/firmware/config.txt`. |
| Some Pis missing from PING replies | UDP broadcast not reaching them | Confirm same subnet, no `STP/portfast`/multicast filtering on the switch. |
| `upload_failed: NT_STATUS_LOGON_FAILURE` | wrong SMB credentials | Update `/etc/picam_node/credentials/default` on all Pis, restart `picam_node`. |
