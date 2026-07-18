# 32PiScanner

A 32-camera photogrammetry rig built from 32× Raspberry Pi 3B + camera modules,
driven by a tablet over the LAN, feeding RealityCapture on a Windows box.

## Design in one paragraph

Every Pi runs the same software image and listens on **UDP port 9999** for
broadcast commands. A client (Android tablet, or any UDP-capable device) sends
four message types — `PING`, `CONFIGURE`, `CAPTURE`, `UPLOAD` — to
`255.255.255.255:9999`. Each Pi schedules its own shutter using its
chrony-disciplined clock, captures locally, and pushes the JPEG to an SMB share
on the RC box. No broker, no orchestrator service, no controller Pi — all peers,
identical software.

```
Android tablet (Kotlin/Compose) ─┐
Laptop CLI (Python)              ├─► UDP broadcast 255.255.255.255:9999
Any other UDP client             ─┘
                                       │
                       ┌───────────────┼───────────────┐  × 32
                       ▼               ▼               ▼
                  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐
                  │   Pi 3B     │ │   Pi 3B     │ │   Pi 3B     │
                  │ picam_node  │ │ picam_node  │ │ picam_node  │
                  └──────┬──────┘ └──────┬──────┘ └──────┬──────┘
                         │ SMB push       │               │
                         └────────────────┼───────────────┘
                                          ▼
                              \\rc-box\scans\<session>\pi-NN.jpg
                              (Windows; RealityCapture watches)
```

## Hardware

- 32× Raspberry Pi 3B + camera module (v1 OV5647 or v2 IMX219)
- Wired Gigabit network (unmanaged 48-port switch + 32 Cat5e patches)
- Windows box running RealityCapture + Meinberg NTP (also serves SMB share)
- Android tablet for field control (optional — Python CLI works too)
- Bright continuous lighting (essential for short exposures to reduce
  rolling-shutter motion artifacts)

## Repo layout

```
docs/         protocol spec, architecture notes, provisioning guide
node/         picam_node daemon (runs on every Pi)
tools/        Python CLI for testing the rig from a laptop
provision/    chrony configs, systemd units, Pi install script
android/      Kotlin + Jetpack Compose app (scaffolded in next iteration)
```

## Status

🚧 **Early scaffolding.** Protocol and Pi node are first cuts; nothing has been
run on real hardware yet. See [docs/protocol.md](docs/protocol.md) for the wire
format and [docs/provisioning.md](docs/provisioning.md) for bring-up.

## Honest constraints

- **Rolling shutter sensors** (OV5647 / IMX219) will produce skew artifacts on
  any subject that moves during the ~20–33 ms readout. Mitigations: bright
  light → short exposure (≤2 ms), pre-warmed sensors, calm subjects, retries.
  This is the dominant quality risk for animal scans.
- **Pi 3B Wi-Fi** is 2.4 GHz single-stream and shared across 32 clients —
  unusable for a 32-cam rig. **Wired Ethernet is mandatory.**
- **Time sync** requires one always-on NTP source on the LAN (RC box with
  Meinberg NTP, or one Pi running chronyd). The tablet cannot be that source.
