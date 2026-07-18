# 32PiScanner — Full Setup Guide · **Ubuntu** field brain + Windows RC desktop

End-to-end bring-up for the rig with the **split architecture**:

- **Linux laptop (Ubuntu/Debian)** = portable field brain — serves **DHCP + NTP + SMB** to the rig.
- **Windows desktop** = reconstruction only — runs RealityCapture offline, after the shoot.
- **32× Raspberry Pi 3B** + **Camera Module v2.1 (IMX219, 8 MP)**.

> Running Fedora on the laptop instead? Use **`setup-guide-fedora.md`** (different
> package manager, chrony path, firewall, and a required SELinux step).
> This guide supersedes `provisioning.md` for this topology. No `node/` or `tools/`
> code changes are required — SMB is SMB, and the Pi's chrony client just points
> at the laptop's IP instead of a Windows box.

---

## 0. Architecture & network plan

```
FIELD (portable, no internet needed after provisioning)
┌────────────────────────────────────────────────────────────┐
│  Ubuntu laptop  192.168.50.1                                 │
│    dnsmasq  → DHCP (pool .100–.200)                          │
│    chrony   → NTP server (LAN time source)                   │
│    samba    → //192.168.50.1/scans  (images land here)       │
│        │ GbE NIC (ingest bottleneck — use a real Gigabit port)│
│        ▼                                                      │
│   ┌──────────── unmanaged Gigabit switch ───────────────┐    │
│   │   Pi-a1b2c3   Pi-d4e5f6   …   ×32  (each 100M NIC)   │    │
│   └──────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────┘

POST (at the desk)
   Windows desktop + NVIDIA GPU
     mounts  \\192.168.50.1\scans   (or copy via USB SSD)
     RealityCapture reconstructs offline
```

**IP plan** (pick a subnet unlikely to collide with a home router — `192.168.50.0/24`):

| Host | IP | Role |
|---|---|---|
| Ubuntu laptop (rig NIC) | `192.168.50.1` static | DHCP + NTP + SMB |
| DHCP pool for Pis | `192.168.50.100 – .200` | dynamic, no reservations needed |
| Windows desktop | DHCP or `192.168.50.10` | only present at the desk |

Per-Pi static IPs are **not** needed: hostnames derive from MAC, RealityCapture
aligns by image content, and discovery is UDP broadcast. Pure dynamic DHCP is correct.

**Ingest speed reminder:** each Pi 3B has a **100 Mbps** NIC, so per-Pi upload tops
out ~94 Mbps. With a Gigabit laptop NIC as the funnel, a full 32× capture
(~5–6 MB each on the v2.1) drains in **~2–3 s**. If the laptop only has a 100 Mbps
port/dongle, that becomes **~14–17 s** — use a Gigabit port.

---

## 1. Ubuntu laptop — the field brain

Assumes **Ubuntu 22.04/24.04 Desktop** (or Debian 12) — NetworkManager, systemd-resolved,
and ufw. **The laptop needs internet for `apt` in this step**; it does not need
internet in the field afterward.

### 1.0 Automated setup (recommended)

The whole of §1 is scripted. From the repo:

```bash
cd 32PiScanner/provision
sudo RIG_NIC=<your-wired-iface> SMB_PASS=<choose-a-password> ./setup-fieldbrain-ubuntu.sh
```

Find `<your-wired-iface>` with `ip -o link show` (e.g. `enp0s31f6`, or `enxXXXX`
for a USB dongle). The script is idempotent and backs up anything it overwrites.
Then skip to **§2**. The rest of this section is the manual/reference version —
read it to understand what the script does, or if you want to do it by hand.

> The script assumes **NetworkManager** (Ubuntu Desktop). On a netplan /
> systemd-networkd server install, set the static IP by hand and skip §1.3.

### 1.1 Packages

```bash
sudo apt update
sudo apt install -y chrony dnsmasq samba smbclient
```

(On Debian/Ubuntu, `smbclient` is a separate package from the `samba` server — the
`smbclient -L` verify step below needs it.)

### 1.2 Don't let the laptop sleep

The old "laptop sleeps → NTP dies" risk is killed with one command:

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

Also, in GNOME: Settings → Power → **Automatic Suspend: Off**, and set
"When the lid is closed" to do nothing (or run lid-open). The laptop is awake during
a shoot anyway (it's receiving images), but this removes the footgun.

### 1.3 Static IP on the rig NIC

Find the wired interface name first:

```bash
ip -o link show | awk -F': ' '{print $2}'   # e.g. enp0s31f6, or enxXXXX for a USB dongle
```

Set a static address on it (replace `<RIG_NIC>`):

```bash
sudo nmcli con add type ethernet ifname <RIG_NIC> con-name rig \
     ipv4.method manual ipv4.addresses 192.168.50.1/24 \
     ipv4.gateway "" ipv4.dns "" ipv6.method disabled autoconnect yes
sudo nmcli con up rig
```

Verify: `ip addr show <RIG_NIC>` shows `192.168.50.1/24`.

### 1.4 NTP server (chrony)

Replace **`/etc/chrony/chrony.conf`** (Debian/Ubuntu path) with a server config
(mirrors your repo's `provision/chrony-server-pi.conf`, adapted for the laptop):

```ini
# Upstream — only used when the laptop has internet; harmless when offline.
pool pool.ntp.org iburst maxsources 4

# Serve time to the rig.
allow 192.168.50.0/24

# When offline, keep serving our own clock so the Pis still converge on us.
# Relative agreement across the rig is all that matters for trigger sync.
local stratum 10

makestep 1.0 3
rtcsync
driftfile /var/lib/chrony/chrony.drift
```

```bash
sudo systemctl enable --now chrony     # Debian/Ubuntu service is 'chrony'
sudo systemctl restart chrony
chronyc tracking                       # confirm it's running
```

> **Why `local stratum 10`:** offline, the laptop disciplines nothing, but it still
> serves its own crystal as a stratum-10 source. All 32 Pis follow that single source,
> so they stay mutually aligned to <5 ms regardless of whether the clock equals true UTC.
> Absolute UTC is irrelevant to photogrammetry.

### 1.5 DHCP (dnsmasq)

We use dnsmasq for **DHCP only** — `port=0` disables its DNS server so it never
fights `systemd-resolved` over port 53.

`/etc/dnsmasq.conf`:

```ini
port=0                                  # DHCP only, no DNS (avoids resolved conflict)
interface=<RIG_NIC>
bind-interfaces

dhcp-range=192.168.50.100,192.168.50.200,12h
dhcp-option=42,192.168.50.1             # option 42 = NTP server (informational)

# Offline field rig: hand out NO gateway and NO DNS.
dhcp-option=3                           # empty = no router
dhcp-option=6                           # empty = no DNS server
```

```bash
sudo systemctl enable --now dnsmasq
sudo systemctl restart dnsmasq
```

> If you later want the Pis to reach the internet through the laptop during
> provisioning, that's a NAT setup — see §3.6. For field use, leave it offline.

### 1.6 SMB target (Samba)

Create the landing directory and a dedicated `scanner` account (matches the Pi
credentials file your `install.sh` writes):

```bash
sudo useradd -M -s /usr/sbin/nologin scanner
sudo smbpasswd -a scanner            # set a password — you'll put this on the Pis
sudo mkdir -p /srv/scans
sudo chown scanner:scanner /srv/scans
```

Append a share to `/etc/samba/smb.conf`:

```ini
[scans]
   path = /srv/scans
   read only = no
   guest ok = no
   valid users = scanner
   create mask = 0664
   directory mask = 0775
```

```bash
sudo systemctl enable --now smbd       # Debian/Ubuntu service is 'smbd'
sudo systemctl restart smbd
```

Quick local check:

```bash
smbclient -L localhost -U scanner      # should list the 'scans' share
```

> Ubuntu ships an AppArmor profile for Samba, but it does not restrict arbitrary
> share paths by default, so `/srv/scans` works with no extra step. (This is the one
> place Ubuntu is *simpler* than Fedora, which needs an SELinux label here.)

### 1.7 Firewall

Ubuntu uses **ufw** (often inactive out of the box). The rig LAN is a trusted,
isolated subnet (this matches the project's trust model: UDP broadcast on a closed
LAN), so the simplest correct rule is to trust the whole rig subnet — this covers
DHCP, NTP, SMB, the UDP/9999 protocol, and the ephemeral-port replies `cli.py` needs:

```bash
sudo ufw allow from 192.168.50.0/24
```

If ufw is inactive the rule is simply stored and applies whenever you enable it. If
you prefer least-privilege instead of trusting the subnet:

```bash
sudo ufw allow from 192.168.50.0/24 to any port 123 proto udp   # NTP
sudo ufw allow from 192.168.50.0/24 to any port 67  proto udp   # DHCP
sudo ufw allow from 192.168.50.0/24 to any port 445 proto tcp   # SMB
sudo ufw allow from 192.168.50.0/24 to any port 137,138,139 proto udp  # SMB/NetBIOS
sudo ufw allow from 192.168.50.0/24 to any port 9999 proto udp  # picam protocol
```

> Note the ephemeral-reply gotcha: `cli.py` sends UDP broadcast and receives unicast
> replies on a random source port. Opening only 9999 won't let those back in — trust
> the subnet (first form) if pings go out but no PONGs return.

---

## 2. Raspberry Pi — capture nodes (Camera v2.1)

Do §2.1–2.5 for **one** Pi, verify it end to end, then clone the SD card 31× (§2.6).

### 2.1 Flash the base image

1. Flash **Raspberry Pi OS Lite (64-bit, Bookworm or newer)** with Raspberry Pi Imager.
2. In Imager's advanced settings (Ctrl-Shift-X):
   - Set username + password.
   - **Do NOT set a custom hostname** — leave it `raspberrypi` so the first-boot
     script can derive a unique `pi-XXXXXX` from the eth0 MAC.
   - Enable **SSH**.
   - Wired LAN needs no config (DHCP is default). Optionally set temporary Wi-Fi
     just for the provisioning step if that's how you'll reach the internet.

### 2.2 Connect and verify the v2.1 camera

Power off before touching the ribbon. On the **camera module**: contacts face away
from the blue tab. On the **Pi's CSI port** (between HDMI and audio jack): lift the
black clip, insert with the **blue stripe facing the Ethernet/USB side**, press the
clip down.

Boot, SSH in, and confirm the sensor is seen:

```bash
rpicam-hello --list-cameras     # older images: libcamera-hello --list-cameras
```

You should see an **`imx219`** entry with mode `3280x2464`. If empty:

- Bookworm auto-detects the v2.1; ensure `/boot/firmware/config.txt` has
  `camera_auto_detect=1` (default). Only if auto-detect fails, add `dtoverlay=imx219`.
- Re-seat the ribbon (a lifted clip is the #1 cause).

### 2.3 Get this Pi online for the install

`install.sh` needs internet **once** to `apt` its dependencies. Easiest: provision
this first Pi on your normal home/office network (internet + DHCP), then move it to
the rig LAN afterward. The NTP server IP you bake in (`192.168.50.1`) simply won't
resolve until it's on the rig — that's expected; you'll verify sync in §4.

### 2.4 Run the installer

```bash
git clone <this repo> ~/32PiScanner        # or scp the repo across
cd ~/32PiScanner/provision
sudo NTP_SERVER=192.168.50.1 ./install.sh  # ← the Ubuntu laptop's rig IP
```

This installs `picam_node`, the systemd unit, the chrony **client** config pointed
at the laptop, and a placeholder SMB credentials file.

### 2.5 Set the real SMB password + first-boot hostname

Put the `scanner` password from §1.6 into the credentials file:

```bash
sudo nano /etc/picam_node/credentials/default
#   username=scanner
#   password=<the smbpasswd you set on the laptop>
#   domain=WORKGROUP
sudo systemctl restart picam_node

sudo reboot        # first reboot only — sets hostname to pi-XXXXXX from the MAC
```

After reboot, `hostname` should read `pi-XXXXXX` and
`systemctl status picam_node` should be **active (running)**.

### 2.6 Clone to the other 31 cards

```bash
# On a Linux/Mac box, with the finished card in a reader:
sudo dd if=/dev/sdX of=master.img bs=4M status=progress
# Flash master.img to 31 more cards; boot each.
```

The first-boot service re-derives the hostname from **each card's host Pi's MAC**, so
you never get collisions even though the image is identical. The chrony config and
`scanner` credentials are deliberately identical across all 32.

---

## 3. Bring the rig together

1. Cable: Ubuntu laptop + all 32 Pis into the **Gigabit switch**. No internet needed.
2. Power the laptop first (so DHCP + NTP are up before the Pis boot), then the Pis.
3. Give chrony **30–60 s** to converge before the first capture.

### 3.6 (Optional) Internet through the laptop during provisioning

If you'd rather provision Pis on the rig LAN instead of your home network, share the
laptop's Wi-Fi internet to the rig NIC (NAT), and hand out the laptop as gateway:

```bash
sudo sysctl -w net.ipv4.ip_forward=1
sudo iptables -t nat -A POSTROUTING -o <WIFI_NIC> -j MASQUERADE
sudo iptables -A FORWARD -i <RIG_NIC> -o <WIFI_NIC> -j ACCEPT
sudo iptables -A FORWARD -i <WIFI_NIC> -o <RIG_NIC> -m state \
     --state RELATED,ESTABLISHED -j ACCEPT
```

Then temporarily point DHCP at the laptop as gateway/DNS in `/etc/dnsmasq.conf`:
`dhcp-option=3,192.168.50.1` and `dhcp-option=6,192.168.50.1` (and add a DNS
forwarder — remove `port=0` and add `server=1.1.1.1`). Revert to the offline
settings for field use.

---

## 4. Smoke test (from the laptop)

```bash
cd ~/32PiScanner/tools

python3 cli.py ping --expected 32
#   → one ✓ pi-XXXXXX PONG line per Pi; check NTP ✓ and offset within a few ms.
#     If NTP shows ✗, wait ~30s for chrony to converge and re-ping.

python3 cli.py configure --exposure-us 2000 --gain 4.0
python3 cli.py capture --session smoke01 --leadtime 2.0
#   → prints per-Pi size_bytes AND "trigger spread across rig".
#     Healthy on wired Gigabit: < 5 ms. > 20 ms means chrony hasn't converged.
```

Note the `size_bytes` from `capture` — that's your **exact** v2.1 JPEG size; plug it
into `(32 × size_MB × 8.389) / goodput_Mbps` for real transfer times.

Then push to the laptop's Samba share and confirm the files land:

```bash
python3 cli.py upload --session smoke01 --dest smb://192.168.50.1/scans/
ls /srv/scans/smoke01/          # should show 32 files named pi-XXXXXX.jpg
```

(Or set the default once so `upload` needs no `--dest`:)

```bash
python3 cli.py set-smb --server 192.168.50.1 --share scans \
        --username scanner --password <pw>
```

---

## 5. Field operation (per scan session)

```bash
# One session, configure + capture + upload in one go:
python3 cli.py session --session 2026-06-04_rex_take01 \
        --dest smb://192.168.50.1/scans/ \
        --exposure-us 2000 --gain 4.0 --leadtime 2.0
```

Images accumulate under `/srv/scans/<session>/` on the laptop.

---

## 6. Reconstruction — Windows desktop

The Windows box never touches the rig LAN during a shoot. At the desk:

1. Connect the laptop and the Windows desktop to the same network (home LAN, or a
   direct Ethernet cable between them).
2. From Windows, mount the share — **File Explorer → `\\192.168.50.1\scans`**, or:
   ```
   net use Z: \\192.168.50.1\scans /user:scanner *
   ```
   (Windows 10/11 Home mounts SMB shares fine — no Pro/gpedit needed for a client.)
3. In **RealityCapture**, add the session folder (`Z:\<session>\`) as the image
   input. Align → mesh → texture as usual.
4. Alternatively, skip the network: copy `/srv/scans/` to a USB SSD and carry it over.

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `clock_unsynced` from CAPTURE | chrony not converged, or Pi can't reach `192.168.50.1:123` | `chronyc tracking` on a Pi; check laptop's `chrony` is running + ufw allows the subnet (§1.7). Wait 30–60 s. |
| Trigger spread > 20 ms | one Pi on a bad link, or NTP flaky | test the outlier Pi alone; confirm all on the Gigabit switch, not a 100 M hop. |
| Some Pis missing from `ping` | didn't get a DHCP lease, or broadcast filtered | `ip addr` on the Pi (should be `192.168.50.1xx`); confirm dnsmasq is up; use an **unmanaged** switch (no IGMP/STP filtering). |
| No Pis get an IP | dnsmasq bound to wrong NIC, or another DHCP server present | check `interface=` in dnsmasq.conf; ensure nothing else on the LAN serves DHCP (dueling DHCP). |
| `camera_unavailable` in PONG | v2.1 ribbon lifted / reversed | `rpicam-hello --list-cameras`; re-seat ribbon, blue stripe toward Ethernet side. |
| `upload_failed: NT_STATUS_LOGON_FAILURE` | wrong SMB password on Pi | fix `/etc/picam_node/credentials/default`, `systemctl restart picam_node`. Confirm `scanner` password via `smbclient -L localhost -U scanner` on the laptop. |
| `upload_failed: NT_STATUS_ACCESS_DENIED` | `/srv/scans` not writable by `scanner` | `sudo chown scanner:scanner /srv/scans` (Ubuntu needs no SELinux step). |
| Windows can't open `\\192.168.50.1\scans` | not on same network as laptop | put both on one LAN or a direct cable; re-check the laptop IP. |

---

## Appendix — what stays identical across all 32 Pis

`chrony-client.conf` (pointed at `192.168.50.1`), the `scanner` credentials file, and
the `picam_node` install are byte-identical on every card **by design** — only the
hostname differs, and that's derived at first boot. This is what makes the `dd`-clone
workflow safe.
