# Architecture rationale

Why the system looks the way it does. If you're going to change something
load-bearing, read the relevant section first.

## Why all-peer UDP broadcast (no controller, no broker)

The alternative architectures considered:

| Design | Why rejected |
|---|---|
| One controller Pi runs orchestrator + MQTT broker | Adds a SPOF and a "special" Pi. Tablet/laptop becomes redundant. Doesn't help the actual goal (portable, field-driven). |
| Tablet runs MQTT broker | Tablet sleep / Android Doze mode kills the broker. Trigger jitter spikes. |
| Cloud MQTT broker | Adds internet dependency + ~50 ms broker hop, defeats sub-ms trigger. |
| HTTP API on each Pi | No native fan-out — 32 sequential requests is slow and out-of-sync. |

UDP broadcast is the only design where:
- The client is replaceable (Android, laptop, anything).
- There is no broker.
- Fan-out is single-packet at the IP layer.
- Per-Pi failure is local — 1/32 down still yields 31 usable photos.

Cost paid: UDP is lossy. Mitigated by 3× send + UUID dedupe (see protocol).

## Why absolute Unix timestamps for `CAPTURE`

The naive design is "shoot now, in 2 seconds" — relative leadtime. That's
broken: the 3× resend means three different "shoot now"s, and network jitter
makes "now" different on every Pi.

Absolute Unix timestamp + chrony means all 32 Pis converge to the same
`clock_nanosleep` deadline regardless of when the message arrived. The trigger
spread becomes a function of *clock sync quality*, not network behavior.

## Why chrony, not ntpd, and never SNTP

- ntpd: converges in ~15 min; weak on noisy networks. chrony: under 60 s,
  designed for LAN clients.
- SNTP / Android's `SystemClock`: single-shot poll, no drift compensation,
  unsuitable for sub-10 ms accuracy.

## Why `clock_nanosleep(TIMER_ABSTIME)` and not `time.sleep`

`time.sleep` is *relative* — it sleeps for a duration measured from when the
call started. Python's GIL + scheduler add ~1 ms jitter at the boundary.
`clock_nanosleep` with `TIMER_ABSTIME` wakes when CLOCK_REALTIME crosses a
specific value. Combined with `Nice=-10` in the systemd unit, we land within
~100 µs of the target on a quiet Pi.

## Why keep the camera "warm" with a preview stream

First-frame latency after `Picamera2.start()` is 200–500 ms (auto-exposure
ramp, sensor settle, libcamera helper threads warming up). We can't afford
that — our entire trigger leadtime budget is 2 seconds.

By starting the camera at boot and keeping it running, `capture_file()`
returns within ~30 ms of the call. The fixed exposure/gain/wb (set by
CONFIGURE) means there's no AE/AWB convergence step in the hot path either.

## Why per-Pi SMB push, not central pull

Two options for moving JPEGs to the RC box:
1. **Central pull**: orchestrator runs `rsync user@pi-XX:/...` for each Pi.
2. **Per-Pi push**: each Pi runs its own `smbclient ... put`.

We use push because:
- It needs no orchestrator service. The tablet sends one UPLOAD broadcast
  and the rig disperses files in parallel.
- The RC box already runs Windows file sharing — natural destination.
- Failures are localized — one Pi's failed upload doesn't block the others.

Cost: every Pi needs SMB credentials. Stored in `/etc/picam_node/credentials/`
with mode 0600 + 0700 directory. Acceptable for a closed LAN.

## Why hostname-from-MAC

Two failure modes a static IP scheme would have:
1. Cloning SD cards causes hostname collisions — DHCP gets confused, SMB
   pushes overwrite each other.
2. Replacing a Pi means reassigning numbers.

MAC-derived hostnames (`pi-a1b2c3`) are stable across SD reflashes (the
MAC is the *host* hardware's, not the SD's), unique by construction, and
require no coordination. The PWA/CLI display them as-is — humans don't
need to know which physical position `pi-a1b2c3` occupies, because
RealityCapture aligns by image content, not by name.

If you want positional labels for debugging, write them on the physical
Pi case once at rig assembly.

## Rolling shutter — what we can't fix in software

OV5647 (v1) and IMX219 (v2) read out top-to-bottom over ~20–33 ms at full
resolution. For a still subject this is invisible. For motion:
- A subject moving 10 cm/s causes ~2–3 mm of skew across the frame.
- RealityCapture's bundle adjustment rejects affected views as outliers.

Software mitigations applied:
- Short exposure (≤2 ms) — reduces *motion blur within rows* (not skew).
- High analogue gain — compensates for the short exposure's low light.
- Bright continuous lighting — required to make the above viable.
- Pre-warmed sensor — all 32 are mid-readout at trigger, not just-started.
- Fixed AE/AWB — no per-row exposure changes.

Hardware-only mitigations (not yet applied):
- Global shutter sensors — $50/cam × 32 = $1600 if needed.
- Strobe-synchronized lighting — possible but rolling-shutter banding is its
  own problem (requires strobe pulse > readout time).

## What I'd build next, in order

1. **Validation**: run `cli.py session` against 1 real Pi end-to-end.
   Measure trigger spread, capture latency, upload speed.
2. **Calibration tool** (`tools/calibrate.py`): capture a ChArUco board from
   all cams, solve intrinsics/extrinsics, export RC's camera registration
   XML so it doesn't bundle-adjust from scratch every session.
3. **Android app** (`android/`): Kotlin + Compose, four screens — Devices
   (PING grid), Settings (CONFIGURE), Capture (big shutter button, leadtime
   slider), Sessions (history + UPLOAD retry).
4. **Lighting + rig geometry doc**: where to place cameras for human + animal
   subjects, lens choices, bright continuous lighting selection.
5. **Watchdog**: tiny service that posts heartbeats to a known address;
   if a Pi misses 3 heartbeats, it reboots itself. Defensive.
