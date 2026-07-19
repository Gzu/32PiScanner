# 32PiScanner — AI handoff context

Audience: Claude or similar LLM resuming work on this project in a fresh session.
This file is dense and skips human-friendly framing. Read top to bottom before acting.

## Project identity

- Name: **32PiScanner**
- Goal: full-body / full-subject photogrammetry rig for **humans and animals**
- Reconstruction: **RealityCapture** (Windows, user's licensed tool — not Meshroom/COLMAP)
- Repo root: `/Users/gzu/dev/32PiScanner`
- Status: scaffolding complete; nothing run on real hardware yet

## Immutable hardware constraints

- 32× **Raspberry Pi 3B** (not 3B+) — user already owns; not being replaced
- Camera modules: original Pi cam **v1 OV5647** or **v2 IMX219** — both are **rolling shutter**, no hardware sync pin exposed
- Power: each Pi has its own PSU (per user)
- Network: **wired Gigabit** (user accepted recommendation; Pi 3B 2.4GHz-only Wi-Fi was rejected as unworkable for 32 clients)

## Load-bearing architectural decisions (with rationale; do NOT re-litigate)

1. **All-peer UDP broadcast, no controller, no broker.** User explicitly pushed back against the initial Controller-Pi + MQTT design. The all-peer design is correct for their goal of a *portable, tablet-driven, field rig*. Every Pi runs identical software. Clients (any UDP-capable device) broadcast to `255.255.255.255:9999`. Pis reply unicast.

2. **Tablet is replaceable thin client, not the brain.** MVP control is `tools/cli.py` from macOS. Target is **Android tablet, Kotlin + Jetpack Compose** (chosen over Flutter and Termux). The protocol is identical for both.

3. **NTP source = Windows RC box running Meinberg NTP** (not stock w32time which is ~1s precision). Fallback: one Pi runs chronyd in server mode (`provision/chrony-server-pi.conf`). Android tablet was explicitly rejected as NTP source (Doze mode kills it). — **UPDATED 2026-06-04: NTP is now served by the Linux field-brain laptop via chrony, not the Windows box. See decision #10.**

4. **Absolute Unix timestamps for CAPTURE**, never relative leadtime. Pis use `clock_nanosleep(CLOCK_REALTIME, TIMER_ABSTIME)`. chrony keeps the wall clocks aligned (<5 ms across rig on wired LAN expected).

5. **3× redundant UDP send + UUID dedupe** for reliability. Don't reach for TCP — TCP has no broadcast.

6. **Per-Pi SMB push**, not central pull; each Pi runs `smbclient ... put`. — **UPDATED 2026-06-04: SMB target is now the Linux field-brain laptop's Samba `scans` share, not the Windows box. See decision #10.**

7. **Hostname derived from eth0 MAC at first boot** (`pi-XXXXXX`). Stable across SD reflashes, unique by construction. RealityCapture aligns by image content, so positional labels aren't needed.

8. **Camera kept warm with continuous preview** since cold start is 200–500 ms (eats trigger budget). Fixed AE/AWB set via CONFIGURE before captures.

9. **Runtime config via SET_NTP / SET_SMB** added in iteration 2 — no need to edit 32 SD cards to repoint NTP or SMB target.

10. **Split field-brain architecture (decided 2026-06-04).** A **Linux laptop** (Ubuntu *or* Fedora — user's laptop is Fedora) is the portable field brain: it serves **DHCP (dnsmasq) + NTP (chrony) + SMB (Samba)** to the rig on `192.168.50.0/24`. The **Windows desktop is reconstruction-only** — it never joins the rig LAN during a shoot; images are pulled afterward from the laptop's `//192.168.50.1/scans` share into RealityCapture. This **supersedes the NTP-on-Windows / SMB-to-Windows parts of decisions #3 and #6** (no code change needed — SMB is SMB, the Pi chrony client just points at the laptop IP). Internet is optional and only needed for one-time `apt` provisioning; offline the laptop serves `local stratum 10`. Full guides: `docs/setup-guide-{ubuntu,fedora}.md`; one-shot provisioners: `provision/setup-fieldbrain-{ubuntu,fedora}.sh`.

## Dominant quality risk (cannot fully solve in software)

**Rolling shutter on humans/animals.** Sensors read top-to-bottom over 20–33 ms; any subject motion during readout causes skew. Software mitigations applied (short exposure ≤2 ms, high gain, fixed AE/AWB, pre-warmed sensor, bright continuous lighting required). Hardware fix would be **Global Shutter Camera modules** ($50 × 32 ≈ $1600) — not done in v1. Animals are harder than humans because they can't be coached still.

## Trust model

UDP broadcast on closed LAN. Any device on the LAN can send any message including `SET_NTP` (rogue NTP source would silently destroy trigger sync). The protocol is structured to accept a future `auth: <hmac>` field additively. Not implemented in v1. Mention to user before exposing the rig LAN to untrusted devices.

## Protocol summary (full spec: `docs/protocol.md`)

- Transport: UDP/9999, JSON payloads, `"v": 1`, every message carries `"id": <uuid>`
- Client→nodes: broadcast `255.255.255.255:9999`, sent 3× spaced 10 ms
- Nodes→client: unicast reply to sender; first-wins per-Pi dedupe on client
- Pi dedupe cache: 256 entries, 60 s TTL

| Request | Reply | Purpose |
|---|---|---|
| `PING` | `PONG` (one per Pi) | discovery + health; returns `ntp{server,synced,offset_ms,stratum}` and `smb{server,share,credentials_ref,reachable,last_check_age_s,last_error}` |
| `CONFIGURE` | `CONFIGURED` | lock exposure/gain/wb/resolution/quality |
| `CAPTURE` | `CAPTURED` | absolute-time trigger; `trigger_at_unix` must be 0.2s–60s in future |
| `UPLOAD` | `UPLOADED` | SMB push; `dest`/`credentials_ref` optional (fall back to stored SET_SMB) |
| `SET_NTP` | `NTP_SET` | rewrites `/etc/chrony/chrony.conf` atomically + restarts chronyd (~30s reconvergence) |
| `SET_SMB` | `SMB_SET` | writes `/etc/picam_node/smb.yaml` + credentials file; immediate port-445 probe |
| — | `ERROR` | reasons enumerated in protocol doc |

**Error reasons:** `trigger_too_soon`, `trigger_too_far`, `clock_unsynced`, `camera_unavailable`, `disk_full`, `not_configured`, `upload_failed`, `unknown_session`, `no_smb_dest`, `bad_ntp_config`, `chrony_restart_failed`, `bad_smb_config`, `smb_write_failed`, `internal_error`, `unknown_msg`, `bad_session_id`.

## Repo layout

```
README.md                        project overview
AI_CONTEXT.md                    this file
docs/
  protocol.md                    UDP wire spec (the contract)
  architecture.md                design rationale (why each decision)
  provisioning.md                SUPERSEDED (Windows-NTP topology) — see setup-guide-*.md
  setup-guide-ubuntu.md          full bring-up: Ubuntu field brain + Windows RC desktop
  setup-guide-fedora.md          full bring-up: Fedora field brain + Windows RC desktop
node/
  picam_node.py                  ~600 lines, single-file daemon, runs as root
  requirements.txt               PyYAML only (picamera2 from apt)
provision/
  picam_node.service             systemd unit, Nice=-10
  chrony-client.conf             32 Pis use this; pointed at NTP_SERVER var
  chrony-server-pi.conf          OPTIONAL: if one Pi serves NTP instead of RC box
  first-boot.sh                  sets hostname from MAC, then self-disables
  install.sh                     idempotent, sudo NTP_SERVER=x.x.x.x ./install.sh
  setup-fieldbrain-ubuntu.sh     one-shot: DHCP+NTP+SMB on an Ubuntu/Debian laptop
  setup-fieldbrain-fedora.sh     one-shot: DHCP+NTP+SMB on a Fedora laptop (SELinux-aware)
tools/
  cli.py                         subcommands: ping configure capture upload set-ntp set-smb session
android/                         EMPTY — Kotlin/Compose app not scaffolded yet
```

## Key implementation details to know before editing

### `node/picam_node.py`
- Single file, runs as root (needed for chrony config writes + privileged systemd actions)
- Listens UDP/9999, dispatches by `msg` type
- `Camera` class: opens picamera2 once at startup, `capture_jpeg` is synchronous under a lock
- Falls back to placeholder JPEG if picamera2 not importable (allows dev on Mac/Linux)
- `clock_nanosleep` called via ctypes to libc (Python's `time.sleep` is relative, not absolute)
- `CAPTURE` and `UPLOAD` handlers spin background threads so the dispatch loop stays responsive; the thread sends the reply itself when work completes
- `atomic_write()` for chrony.conf + credentials (temp+fsync+rename)
- `write_ntp_server()` strips all existing `server`/`pool` lines, writes one canonical `server <ip> iburst minpoll 4 maxpoll 6`
- `SmbProbe` is a background daemon thread; jittered start (hash(hostname) % 1000 ms) to avoid 32-Pi lockstep; probes every 30 s; PING reads cached state
- `SmbConfig.load()`/`save()` use `/etc/picam_node/smb.yaml`
- Credentials file format is smbclient `-A` format: `username=...\npassword=...\ndomain=...`

### `tools/cli.py`
- Stdlib only (socket, json, uuid, argparse, dataclasses)
- `_send_triple()` does 3× send with 10 ms gap
- `_collect_replies()` returns when expected count reached OR timeout
- `_format_pong()` renders the nested ntp/smb status as 3 lines per Pi with ✓/✗ marks
- `cmd_set_smb` reports aggregate reachability count across the rig

### Bring-up command sequence (no SD edits needed for repoint)

```bash
# Per Pi, once:
sudo NTP_SERVER=192.168.1.10 ./install.sh
sudo reboot   # only first time, for hostname-from-MAC

# From laptop, once per rig:
python3 cli.py set-ntp --server 192.168.1.10
python3 cli.py ping                     # wait ~30s, check ntp.synced
python3 cli.py set-smb --server rc-box --share scans --username scanner --password ...

# Per scan session:
python3 cli.py configure --exposure-us 2000 --gain 4.0
python3 cli.py capture --session 2026-05-26_rex_take01 --leadtime 2.0
python3 cli.py upload --session 2026-05-26_rex_take01
# or all three:
python3 cli.py session --session 2026-05-26_rex_take01 --dest smb://rc-box/scans/
```

## Outstanding work (priority order)

1. **Validate on one real Pi** — run `session` end-to-end, measure trigger spread (target <5 ms wired). User has not yet done this.
2. **Scaffold Android app** — `android/` directory exists but empty. Kotlin + Jetpack Compose. Four screens: Devices (PING grid), Settings (CONFIGURE + SET_NTP/SET_SMB), Capture (shutter + leadtime slider), Sessions (history + UPLOAD retry). Reuse protocol semantics from `tools/cli.py` and `docs/protocol.md` verbatim.
3. **Calibration tool** `tools/calibrate.py` — capture ChArUco board from all cams, solve intrinsics+extrinsics with OpenCV, export RealityCapture camera registration XML.
4. **Rig geometry / lighting doc** — camera placement for humans+animals, lens choices, continuous-light selection. Currently undecided whether one rig serves both subjects or separate.
5. **Watchdog** — heartbeat-or-reboot daemon on each Pi. Defensive.

## Open user questions never resolved

- ~~Is the RC box a desktop or laptop?~~ **RESOLVED 2026-06-04: Windows desktop (always-on), and it's now reconstruction-only — NTP moved off it entirely onto the Linux field-brain laptop (decision #10), so the Pi-as-NTP fallback is not needed.**
- One rig for humans and animals, or separate? Affects geometry/lens decisions.
- Hardware upgrade to Global Shutter Camera modules ($1600) — only relevant if rolling-shutter artifacts on animals prove unacceptable in real captures.

## User preferences observed

- Wants honest engineering trade-offs called out, not glossed over
- Pushes back on over-engineering — proposed the all-peer simplification themselves
- Values portability and replaceable clients
- French timezone / French native speaker (asked questions in French-influenced English; respond in clear English)
- Prefers concise, structured answers with tables/diagrams over prose

## Saved memory files (project-level, indexed by `MEMORY.md`)

- `~/.claude/projects/-Users-gzu-dev-32PiScanner/memory/hardware-and-subject.md`
- `~/.claude/projects/-Users-gzu-dev-32PiScanner/memory/architecture-all-peer-udp.md`
- `~/.claude/projects/-Users-gzu-dev-32PiScanner/memory/ntp-rc-box-meinberg.md`
- `~/.claude/projects/-Users-gzu-dev-32PiScanner/memory/reality-capture-pipeline.md`

If importing into a fresh Claude account, recreate equivalents or inline the facts above.

## Things NOT to do

- Don't propose MQTT broker / controller Pi / cloud broker — explicitly rejected
- Don't propose Wi-Fi for the rig — Pi 3B Wi-Fi can't handle 32 clients
- Don't propose Meshroom/COLMAP — user is on RealityCapture
- Don't propose Flutter/PWA for the tablet app — Kotlin+Compose was chosen
- Don't suggest the tablet as NTP source — Doze mode kills it
- Don't break the protocol's back-compat — `"v": 1` clients (the CLI and the future Android app) must keep working through any extension
- Don't `cd` in compound bash commands (RTK constraint); use absolute paths
- End any git commits with: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

## Environment notes

- Working directory: `/Users/gzu/dev/32PiScanner`
- User's machine: macOS (orchestrator MVP)
- Today's date when this was written: 2026-06-02
- RTK (Rust Token Killer) proxy is active in the user's shell — most commands transparently rewritten; don't worry about it
