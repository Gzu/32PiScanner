# 32PiScanner — command cheat sheet

Operational quick-reference. Placeholders: `<ip>` = a Pi's address, `<pi-user>` =
the Pi login user, `<session>` = a session id (`[a-z0-9_-]` only).

## Key values

| Thing | Value |
|---|---|
| Field-brain laptop IP | `192.168.50.1` |
| Subnet / DHCP pool | `192.168.50.0/24` / `.100–.200` |
| Rig NIC (laptop / Pi) | `eth0` |
| SMB user / share | `scanner` / `scans` → `smb://192.168.50.1/scans/` |
| Camera | Pi Cam v2.1 (IMX219, 8 MP, `3280×2464`) |
| Daemon on Pi | `/opt/picam_node/picam_node.py` (service `picam_node`) |
| Captures on Pi | `/var/lib/picam_node/captures/<session>/<pi>.jpg` |
| Images on laptop | `/srv/scans/<session>/` |

---

## Field brain — bring up / down (container path)

```bash
cd ~/32PiScanner/containers
sudo RIG_NIC=eth0 ./setup.sh          # static IP + broadcast route + DHCP+NTP + no-sleep
sudo ./setup.sh --down                # stop containers, restore sleep, drop static IP

sudo podman ps                        # rig-dhcp + rig-ntp should be 'Up' (needs sudo — rootful)
sudo podman logs -f rig-dhcp          # watch DHCP leases as Pis boot
sudo podman exec rig-ntp chronyc tracking   # NTP server status (chrony lives in the container)
```

Rebuild after a code/config change:
```bash
cd ~/32PiScanner/containers && sudo podman-compose up -d --build
```

---

## Network & power (laptop)

```bash
ip -4 addr show eth0                   # confirm 192.168.50.1/24
sudo ss -ulnp | grep -E ':(67|123)'    # who serves DHCP(67) / NTP(123)

# broadcast route (needed for cli.py on the gateway-less rig):
sudo nmcli con mod rig +ipv4.routes "255.255.255.255/32" && sudo nmcli con up rig

# keep the laptop awake (mask sleep + lid):
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
systemctl status sleep.target          # should say 'masked'
```

---

## Find a Pi's IP

```bash
# DHCP leases (IP + MAC + hostname) — container path:
sudo podman exec rig-dhcp cat /var/lib/misc/dnsmasq.leases
# bare-metal path:  cat /var/lib/misc/dnsmasq.leases

ip neigh show dev eth0 | grep 192.168.50     # ARP table (after a ping)
sudo nmap -sn 192.168.50.100-200             # scan the pool (Pi 3B MAC prefix b8:27:eb)
```

---

## Capture workflow (from `~/32PiScanner/tools`)

```bash
python3 cli.py ping --expected 32              # discover; check NTP ✓ / SMB ✓ per Pi

python3 cli.py set-smb --server 192.168.50.1 --share scans \
        --username scanner --password 'PW'     # writes smb.yaml + creds on ALL Pis (do once)
python3 cli.py set-ntp --server 192.168.50.1   # repoint chrony on ALL Pis (do if NTP ✗)

python3 cli.py autoconfigure                   # meter on auto, average, apply (fixed+identical)
python3 cli.py autoconfigure --max-exposure-us 2000   # cap exposure for motion
python3 cli.py configure --exposure-us 2000 --gain 4.0 # manual settings instead

python3 cli.py capture --session <session> --leadtime 2.0   # timed shot; prints trigger spread
python3 cli.py upload  --session <session>                  # push to SMB (stored default)
python3 cli.py session --session <session> --dest smb://192.168.50.1/scans/  # cfg+cap+upload

python3 cli.py clear --session <session>       # delete that session on all Pis
python3 cli.py clear --all                     # delete ALL captures (prompts; --yes to skip)
```

---

## GUI — web control panel (alternative to `cli.py`)

`tools/gui.py` serves the Faceplate web UI + API on port **8321** (binds `0.0.0.0`,
so a tablet or other device on the rig LAN can drive it).

```bash
cd ~/32PiScanner
python3 tools/gui.py                    # real rig  → http://localhost:8321
python3 tools/gui.py --port 8080        # custom port
```

Open in a browser:
- on the laptop:  `http://localhost:8321`
- from a tablet / other device on the rig LAN:  `http://192.168.50.1:8321`

Demo / test with **no hardware** (in-process fake fleet):
```bash
python3 tools/gui.py --sim 32                                          # 32 fake Pis
python3 tools/gui.py --sim 32 --sim-faults dead:1,ntp:1,smb:1,stale:1  # inject faults
```

Thumbnails are optional — `sudo apt install -y python3-pil` (Pillow); the UI falls
back gracefully without it. Ctrl-C to stop.

---

## Fleet management (from `~/32PiScanner`, run on the laptop)

```bash
git pull                                                   # get latest code first

SSH_USER=<pi-user> ./provision/setup-pi-keys.sh            # 1. install laptop SSH key on all Pis (type each pw once)
SSH_USER=<pi-user> ./provision/update-pis.sh               # 2. push daemon to all Pis + restart (passwordless via keys)
SSH_USER=<pi-user> ./provision/fix-pi-hostnames.sh         # (optional) re-derive hostnames — cosmetic

# all take explicit IPs too:   ... ./update-pis.sh 192.168.50.101 192.168.50.102
# or control discovery:        POOL=192.168.50.100-200   LEASES=<path>
# NEW clones: bake the laptop pubkey into the master's authorized_keys (guide §2.6) — skips step 1
```

Provision one fresh Pi (needs internet, once):
```bash
cd ~/32PiScanner/provision && sudo NTP_SERVER=192.168.50.1 ./install.sh
```

---

## On a Pi (SSH in)

```bash
ssh <pi-user>@<ip>

rpicam-hello --list-cameras            # camera detected? (expect imx219, 3280x2464)
systemctl status picam_node            # service active?
sudo journalctl -u picam_node -f       # live daemon logs
chronyc tracking                       # Stratum 10-ish, Leap: Normal = synced
chronyc sources -v                     # 192.168.50.1 should be '^*'
hostname                               # pi-XXXXXX

# edit SMB creds by hand (dir is 0700 root — NO tab-complete, use sudo + full path):
sudo nano /etc/picam_node/credentials/default
```

---

## SD card cloning

**Create the master image** (from the master card, on Linux/Mac — verify the device!):
```bash
# macOS:   diskutil list ; sudo dd if=/dev/rdiskN of=master.img bs=4m status=progress
# Linux:   lsblk ;         sudo dd if=/dev/sdX  of=master.img bs=4M status=progress
```

**Flash a card** — ⚠️ triple-check the target device every time (it renumbers):
```bash
# macOS:
diskutil list
diskutil unmountDisk /dev/diskN
sudo dd if=master.img of=/dev/rdiskN bs=4m status=progress    # bs=1m if it times out
diskutil eject /dev/diskN

# Linux:
lsblk
sudo umount /dev/sdX*
sudo dd if=master.img of=/dev/sdX bs=4M conv=fsync status=progress ; sync
```

Notes: smaller image → larger card is fine (extra space unused; `sudo raspi-config nonint
do_expand_rootfs` to reclaim). A flashed card looks **empty on macOS** (ext4 rootfs
unreadable) — that's normal; boot it to verify.

**Card troubleshooting (macOS):**
```bash
diskutil info /dev/diskN | grep -iE 'read-only|writable'     # 'Read-Only: Yes' = card locked itself, dead
diskutil eraseDisk FAT32 SDCARD MBRFormat /dev/diskN         # reformat to clear a stuck controller
```

---

## Reconstruction — Windows desktop

```
net use Z: \\192.168.50.1\scans /user:scanner *
```
Then point RealityCapture at `Z:\<session>\`.

---

## Troubleshooting — symptom → fix

| Symptom | Fix |
|---|---|
| `cli.py` → **network unreachable** | add broadcast route: `sudo nmcli con mod rig +ipv4.routes "255.255.255.255/32" && sudo nmcli con up rig` |
| `ping` → NTP **✗** / source **unusable** | laptop needs `local stratum 10`; container chrony needs `-x`; wait 60 s; or `cli.py set-ntp --server 192.168.50.1` |
| `ping` → SMB **no server configured** | run `cli.py set-smb …` (sets destination + creds together) |
| `ping` returns **only 1 reply** | duplicate hostname on clones → deploy MAC-based `pi_id()` via `update-pis.sh` |
| `rig-ntp` **restart-looping** | needs `chronyd -x` — rebuild: `sudo podman-compose up -d --build` |
| laptop **sleeps / logs out** | mask sleep targets; KDE: System Settings → Screen Locking + Power (lid: Do nothing) |
| `CAPTURE` → `clock_unsynced` | chrony not converged — `chronyc tracking` on the Pi; wait 30–60 s |
| `UPLOAD` → `NT_STATUS_LOGON_FAILURE` | wrong password — re-run `cli.py set-smb … --password <correct>` |
| dd **Operation timed out** / format **error 69825** | bad card or reader — swap card/reader, `bs=1m`, plug direct (no hub) |
