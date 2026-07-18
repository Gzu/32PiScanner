# 32PiScanner — wire protocol

This is the contract between any **client** (Android app, Python CLI, anything
else) and the **32 Pi nodes**. Lock changes here behind a version bump.

## Transport

- **UDP** on **port 9999** (both directions).
- **Client → nodes**: send to broadcast address `255.255.255.255:9999`.
  All Pis on the same subnet receive it.
- **Nodes → client**: unicast reply to the sender's address and source port.
- One JSON object per datagram, UTF-8 encoded, no trailing newline required.
- Max payload **1400 bytes** to stay under typical MTU. We never need more.
- **Protocol version**: `"v": 1` on every message. Reject unknown versions.

## Reliability without TCP

UDP can drop packets. Two rules handle it:

1. **Idempotent messages with a UUID.** Every message carries `"id": "<uuid4>"`.
   Each Pi keeps a small LRU cache of recently-seen IDs (size 256, TTL 60 s)
   and silently drops duplicates.
2. **Triple-send on the wire.** Clients send each command 3× spaced ~10 ms
   apart. Combined with the dedupe cache, this gives effectively zero loss on
   a quiet wired LAN, with no impact on Pi-side behavior.

For replies, the client just waits for N=32 unique `pi` values within a
timeout (typically 5 s) and treats anything missing as a failure to retry.

## Message types

All messages share the envelope `{ "v": 1, "id": "<uuid4>", "msg": "<TYPE>", ... }`.

### 1. `PING` → `PONG`

Discovery + health check. Send before a session to know who's alive.

**Request**
```json
{ "v": 1, "id": "<uuid>", "msg": "PING" }
```

**Reply** (one per Pi)
```json
{
  "v": 1,
  "id": "<uuid>",            // echoes request id
  "msg": "PONG",
  "pi": "pi-07",             // stable per-Pi identifier
  "free_mb": 12400,          // SD card free space
  "camera_ok": true,
  "uptime_s": 18420,
  "version": "0.1.0",        // node software version
  "ntp": {
    "server": "192.168.1.10",     // currently configured NTP source
    "synced": true,                // chronyc reports a usable source
    "offset_ms": 1.4,              // |Last offset| from chronyc tracking
    "stratum": 3                   // 0 = unsynced/unknown
  },
  "smb": {
    "server": "rc-box",            // configured default; empty if unset
    "share": "scans",
    "credentials_ref": "default",
    "reachable": true,             // last reachability probe result
    "last_check_age_s": 12,        // seconds since last probe
    "last_error": ""               // populated when reachable=false
  }
}
```

`clock_offset_ms` is preserved as a top-level field for back-compat with v1
clients written before the `ntp` object existed; it mirrors `ntp.offset_ms`.

### 2. `CONFIGURE` → `CONFIGURED`

Lock exposure / gain / white balance / resolution. Send once per session before
`CAPTURE`. Pis keep the camera warm with these settings after this message.

**Request**
```json
{
  "v": 1,
  "id": "<uuid>",
  "msg": "CONFIGURE",
  "exposure_us": 2000,           // microseconds; ≤2000 strongly recommended for motion
  "analogue_gain": 4.0,          // 1.0–16.0 typical
  "awb_gains": [1.8, 1.6],       // [red, blue]
  "resolution": [3280, 2464],    // v2 full-res; [2592, 1944] for v1
  "jpeg_quality": 95
}
```

**Reply**
```json
{ "v": 1, "id": "<uuid>", "msg": "CONFIGURED", "pi": "pi-07" }
```

### 3. `CAPTURE` → `CAPTURED`

The time-triggered shot. `trigger_at_unix` is an **absolute** Unix timestamp
(seconds, float). Every Pi schedules `clock_nanosleep(ABSTIME, trigger_at)` and
fires when its chrony-disciplined clock crosses that instant.

**Request**
```json
{
  "v": 1,
  "id": "<uuid>",
  "msg": "CAPTURE",
  "session_id": "2026-05-26_rex_take03",   // [a-z0-9_-]+, used as folder name
  "trigger_at_unix": 1716724925.000        // float seconds since epoch
}
```

**Reply** (sent ~50 ms after trigger, once JPEG is on SD)
```json
{
  "v": 1,
  "id": "<uuid>",
  "msg": "CAPTURED",
  "pi": "pi-07",
  "session_id": "2026-05-26_rex_take03",
  "actual_at_unix": 1716724925.0012,    // when shutter actually completed
  "file": "pi-07.jpg",
  "size_bytes": 2841200
}
```

**Constraints**
- `trigger_at_unix` must be **at least 200 ms in the future** when received.
  Pis reply `ERROR { reason: "trigger_too_soon" }` otherwise.
- `trigger_at_unix` must be **at most 60 s in the future**. Avoids accidental
  far-future schedules from clock-drifted clients.
- A second `CAPTURE` for the same `session_id` overwrites the file. Clients
  are responsible for unique session IDs (timestamp prefix is the convention).

### 4. `UPLOAD` → `UPLOADED`

Push captured files to a network location. Called after `CAPTURE` for the
session. Pis don't block on this — they reply when the push completes.

**Request**
```json
{
  "v": 1,
  "id": "<uuid>",
  "msg": "UPLOAD",
  "session_id": "2026-05-26_rex_take03",
  "dest": "smb://rc-box/scans/",         // optional; if absent, use stored default
  "credentials_ref": "default"           // optional; if absent, use stored default
}
```

`dest` and `credentials_ref` are both optional in v1.1+. If omitted, the node
falls back to the values stored by the most recent `SET_SMB` (persisted at
`/etc/picam_node/smb.yaml`). If neither the message nor stored config has a
destination, the node replies `ERROR { reason: "no_smb_dest" }`.

**Reply**
```json
{
  "v": 1,
  "id": "<uuid>",
  "msg": "UPLOADED",
  "pi": "pi-07",
  "session_id": "2026-05-26_rex_take03",
  "remote_path": "smb://rc-box/scans/2026-05-26_rex_take03/pi-07.jpg",
  "duration_ms": 850
}
```

**Supported `dest` schemes (v1)**
- `smb://host/share/` — uses `smbclient`, credentials from local file
- `nfs://host/path/` — requires NFS export mounted at boot (config in node.yaml)
- `file:///mnt/...` — already-mounted path (use for testing)

### 5. `SET_NTP` → `NTP_SET`

Repoint chrony at a new NTP server at runtime. Persists by rewriting
`/etc/chrony/chrony.conf` (atomic temp+rename) and restarting `chronyd`.
chrony typically reconverges within 10–30 s on a wired LAN.

**Request**
```json
{
  "v": 1,
  "id": "<uuid>",
  "msg": "SET_NTP",
  "server": "192.168.1.10"     // IP or hostname; the only `server` line written
}
```

**Reply** (sent immediately after restart kicks off, *before* convergence)
```json
{
  "v": 1,
  "id": "<uuid>",
  "msg": "NTP_SET",
  "pi": "pi-07",
  "server": "192.168.1.10"
}
```

Verify convergence with a follow-up `PING` after ~30 s and inspect
`ntp.synced` and `ntp.offset_ms` in the reply.

**Errors**
- `bad_ntp_config` — server field missing/malformed
- `chrony_restart_failed` — chronyd refused to restart (detail = systemctl stderr)

### 6. `SET_SMB` → `SMB_SET`

Set the default SMB destination + credentials. Writes
`/etc/picam_node/smb.yaml` (server/share/credentials_ref) and
`/etc/picam_node/credentials/<ref>` (username/password/domain) atomically.
After this message, `UPLOAD` requests may omit `dest` and `credentials_ref`.

**Request**
```json
{
  "v": 1,
  "id": "<uuid>",
  "msg": "SET_SMB",
  "server": "rc-box",                // IP or hostname (no smb:// prefix)
  "share": "scans",
  "username": "scanner",
  "password": "hunter2",
  "domain": "WORKGROUP",             // optional, defaults to WORKGROUP
  "credentials_ref": "default"       // optional, defaults to "default"
}
```

**Reply**
```json
{
  "v": 1,
  "id": "<uuid>",
  "msg": "SMB_SET",
  "pi": "pi-07",
  "server": "rc-box",
  "share": "scans",
  "reachable": true,                 // result of immediate port-445 probe
  "probe_ms": 12                     // probe duration; useful for slow paths
}
```

Unlike `SET_NTP`, this handler runs a synchronous reachability probe (TCP
connect to `server:445`, 1 s timeout) before replying — credentials aren't
validated end-to-end, but a green `reachable` means the server is at least
visible from this Pi. Background probes also continue every 30 s and feed
`smb.reachable` in PONG replies.

**Errors**
- `bad_smb_config` — required field missing/malformed
- `smb_write_failed` — couldn't write credentials/config (disk full, permissions)

### 7. `ERROR` (reply only)

Any node-side failure replies with:
```json
{
  "v": 1,
  "id": "<uuid>",
  "msg": "ERROR",
  "pi": "pi-07",
  "in_reply_to": "CAPTURE",
  "reason": "trigger_too_soon",
  "detail": "trigger_at_unix is 50 ms in the future, minimum is 200 ms"
}
```

**Defined `reason` codes**
- `trigger_too_soon` — leadtime < 200 ms
- `trigger_too_far` — leadtime > 60 s
- `clock_unsynced` — chronyc reports offset > 50 ms or no NTP source
- `camera_unavailable` — libcamera reports no camera
- `disk_full` — < 100 MB free
- `not_configured` — `CAPTURE` received before `CONFIGURE`
- `upload_failed` — SMB/NFS push error (detail contains stderr)
- `unknown_session` — `UPLOAD` for a session_id with no captured file
- `no_smb_dest` — `UPLOAD` with no `dest` and no stored default
- `bad_ntp_config` — `SET_NTP` payload invalid
- `chrony_restart_failed` — chronyd refused to restart after `SET_NTP`
- `bad_smb_config` — `SET_SMB` payload missing required fields
- `smb_write_failed` — persistence of `SET_SMB` to disk failed

## Per-Pi identity

Each Pi has a stable hostname `pi-NN` set at first boot from its eth0 MAC
address (see `provision/first-boot.sh`). The hostname is what appears in `pi`
fields. Files are named `<pi>.jpg`, never colliding across the rig.

## Session ID convention

Recommended format: `YYYY-MM-DD_<subject>_<take>` (e.g. `2026-05-26_rex_take03`).
Must match `[a-z0-9_-]+`. Anything else: nodes reject with
`ERROR { reason: "bad_session_id" }`.

## Timing budget (typical)

```
T-2000 ms   client sends CAPTURE (×3 spaced 10ms)
T-1990 ms   all 32 Pis received & scheduled
T-50  ms    Pis wake from clock_nanosleep, kick capture
T+0   ms    shutter completes (±2 ms across rig on wired LAN)
T+20  ms    JPEG encoded
T+50  ms    JPEG flushed to SD
T+50  ms    CAPTURED reply sent
T+60  ms    client has all 32 replies
```

`UPLOAD` adds ~1 s per Pi over wired Gigabit (~3 MB JPEG). Run uploads in
parallel — Pis don't coordinate; the SMB server handles concurrency.
