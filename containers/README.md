# Containerized field-brain (DHCP + NTP)

Run the rig's **DHCP** and **NTP** services as throwaway Podman containers instead
of installing dnsmasq/chrony onto the laptop. Optimized for **clean teardown**:
nothing lands in the host package set or `/etc`, and removal leaves the host as it
was. Host-distro-independent — the same files work on **Kali / Debian / Fedora /
Ubuntu** because the service internals are the Debian base image, not your host.

> Scope: DHCP + NTP only (what you asked about). **SMB stays on the host** — it
> writes captured JPEGs to `/srv/scans` on the laptop's disk, so a host Samba (or
> a container with a `/srv/scans` bind-mount) is the natural home. See
> `../docs/setup-guide-ubuntu.md` §1.6 for the host Samba step (Kali = Debian).

## Why containers need special networking here

| Service | Constraint | Consequence |
|---|---|---|
| **DHCP** | `DISCOVER` is an L2 broadcast to `255.255.255.255:67` | A NAT bridge never receives it → **must use host networking** (or macvlan). Compose uses `network_mode: host`. |
| **NTP** | There is **one** kernel clock shared by host + containers | We run chrony **serve-only** (`local stratum 10`, no `SYS_TIME`): it reads the host clock and answers queries but never steps/slews it. Zero host-clock impact. |

So the isolation you get is **packaging + config + lifecycle** (the clean-teardown
win), not full network sandboxing — DHCP is inherently a host-network citizen. If
you want true netns isolation, switch `network_mode: host` for a macvlan network and
give each container its own IP (then bake that IP into the Pi configs).

## Prerequisites (Kali/Debian)

```bash
sudo apt update && sudo apt install -y podman podman-compose
```

## One host-side step (not containerizable)

The rig NIC's static IP is host network config. Set it once (teardown is a one-liner):

```bash
# find your wired NIC:  ip -o link show    (Kali wired is usually eth0)
sudo nmcli con add type ethernet ifname eth0 con-name rig \
     ipv4.method manual ipv4.addresses 192.168.50.1/24 \
     ipv4.gateway "" ipv4.dns "" ipv6.method disabled autoconnect yes
sudo nmcli con up rig
```

Also edit **`dnsmasq.conf`** and set `interface=` to that same NIC.

## Bring up

```bash
cd containers
sudo podman-compose up -d --build
```

Verify:

```bash
sudo podman ps                         # rig-dhcp + rig-ntp both Up
sudo ss -ulnp | grep -E ':(67|123)\b'  # dnsmasq on 67, chronyd on 123
sudo podman logs rig-dhcp              # watch DHCPACKs as Pis boot
chronyc -h 127.0.0.1 tracking          # (if chrony-cli on host) or: podman exec rig-ntp chronyc tracking
```

Then from the laptop, `python3 ../tools/cli.py ping` should list the Pis with NTP ✓.

## Teardown (the whole point)

```bash
sudo podman-compose down               # stop + remove both containers
sudo podman rmi rig-dhcp rig-ntp       # remove the images too
sudo nmcli con delete rig              # drop the static-IP connection
```

After that the host has no dnsmasq/chrony packages, no `/etc/dnsmasq.conf`, no
`/etc/chrony` changes — nothing. Clean.

## Two things that will bite you

1. **Nothing else may bind 67 or 123 on the host.** `network_mode: host` means the
   container shares the host's ports. Check `sudo ss -ulnp | grep -E ':(67|123)\b'`
   is empty before `up`. Kali's default `systemd-timesyncd` is an NTP *client* (no
   :123 server bind) so it won't conflict — but a host dnsmasq/isc-dhcp-server or a
   host chrony/ntpd *will*. Stop/disable those first.
2. **Offline = the laptop clock free-runs.** With NTP containerized serve-only and
   no host NTP client, the laptop's absolute time drifts when offline. That's fine
   for the rig (all Pis drift together, staying mutually synced). If you want the
   laptop clock roughly correct when online, leave host `systemd-timesyncd` enabled
   — it won't fight the serve-only container.

## Two deployment paths

| | **v1 — compose** (`compose.yaml`) | **v2 — quadlets** (`quadlet/`) |
|---|---|---|
| Use for | **Testing** (start/stop by hand) | Boot-persistent "plug in → rig live" |
| Managed by | `podman-compose` | **systemd** (`systemctl`, `journalctl`) |
| Survives reboot | No | Yes |
| Start | `sudo podman-compose up -d --build` | see below |

Everything above this section uses **v1 (compose)** — that's the testing path.

## v2: boot-persistent quadlets

`quadlet/rig-dhcp.container` + `quadlet/rig-ntp.container` run the same two
containers as native systemd services (Podman 4.4+). One-time setup:

```bash
cd containers

# 1. Build the two images locally
sudo podman build -t rig-dhcp -f Containerfile.dnsmasq .
sudo podman build -t rig-ntp  -f Containerfile.chrony  .

# 2. Put the configs in a stable host location (edit interface= in dnsmasq.conf first)
sudo install -D dnsmasq.conf /etc/32piscanner/dnsmasq.conf
sudo install -D chrony.conf  /etc/32piscanner/chrony.conf

# 3. Install the quadlet units + activate
sudo cp quadlet/rig-dhcp.container quadlet/rig-ntp.container /etc/containers/systemd/
sudo systemctl daemon-reload
sudo systemctl start rig-dhcp rig-ntp
```

Manage like any service: `systemctl status rig-dhcp`, `journalctl -u rig-ntp -f`.
They now auto-start on boot (the `[Install] WantedBy=` line).

Teardown (still leaves no host packages):

```bash
sudo systemctl stop rig-dhcp rig-ntp
sudo rm /etc/containers/systemd/rig-{dhcp,ntp}.container
sudo systemctl daemon-reload
sudo rm -rf /etc/32piscanner
sudo podman rmi rig-dhcp rig-ntp
sudo nmcli con delete rig            # drop the static-IP connection
```
