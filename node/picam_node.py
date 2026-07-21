#!/usr/bin/env python3
"""picam_node — UDP-driven capture daemon for one Pi in the 32PiScanner rig.

Runs as a systemd service on every Pi. Listens on UDP/9999 for broadcast
commands defined in docs/protocol.md and replies unicast to the sender.

Design notes:
- Single-threaded asyncio event loop; capture runs in a background thread so
  the UDP socket stays responsive (we still need to ACK the trigger fast).
- The camera is opened once at startup and kept warm with a continuous preview
  stream — this avoids the 200–500 ms cold-start latency that would blow our
  trigger budget.
- Scheduled trigger uses clock_nanosleep(CLOCK_REALTIME, TIMER_ABSTIME) so the
  shutter fires at the absolute timestamp regardless of when the UDP message
  arrived. chrony keeps CLOCK_REALTIME aligned across the rig.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import functools
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

try:
    from picamera2 import Picamera2  # type: ignore
    from libcamera import controls  # type: ignore
except ImportError:  # pragma: no cover — allows tooling on non-Pi dev boxes
    Picamera2 = None
    controls = None

# ─── constants ──────────────────────────────────────────────────────────────
PROTOCOL_VERSION = 1
LISTEN_PORT = 9999
MIN_LEADTIME_S = 0.200
MAX_LEADTIME_S = 60.0
MAX_CLOCK_OFFSET_MS = 50.0
DEDUPE_CACHE_SIZE = 256
DEDUPE_TTL_S = 60
CAPTURE_DIR = Path("/var/lib/picam_node/captures")
CONFIG_PATH = Path("/etc/picam_node/node.yaml")
CREDS_DIR = Path("/etc/picam_node/credentials")
CHRONY_CONF = Path("/etc/chrony/chrony.conf")
SMB_CONF = Path("/etc/picam_node/smb.yaml")
SMB_PROBE_INTERVAL_S = 30
SMB_PROBE_TIMEOUT_S = 1.0
SMB_PORT = 445

log = logging.getLogger("picam_node")

# ─── clock_nanosleep via libc ───────────────────────────────────────────────
# Python's time.sleep doesn't take an absolute time; we want exactly that to
# avoid drift between "message received" and "shutter fires".
CLOCK_REALTIME = 0
TIMER_ABSTIME = 1
_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


class _Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


def sleep_until(abs_unix_seconds: float) -> None:
    """Block until CLOCK_REALTIME crosses abs_unix_seconds."""
    ts = _Timespec(
        tv_sec=int(abs_unix_seconds),
        tv_nsec=int((abs_unix_seconds - int(abs_unix_seconds)) * 1_000_000_000),
    )
    rc = _libc.clock_nanosleep(CLOCK_REALTIME, TIMER_ABSTIME, ctypes.byref(ts), None)
    if rc != 0:
        raise OSError(rc, f"clock_nanosleep failed: {os.strerror(rc)}")


# ─── dedupe cache ───────────────────────────────────────────────────────────
class DedupeCache:
    """LRU cache of recently-seen message IDs; drops duplicates within TTL."""

    def __init__(self, size: int, ttl_s: float):
        self.size = size
        self.ttl_s = ttl_s
        self._seen: OrderedDict[str, float] = OrderedDict()

    def check_and_add(self, msg_id: str) -> bool:
        """Return True if new, False if duplicate."""
        now = time.monotonic()
        # purge expired
        while self._seen and next(iter(self._seen.values())) + self.ttl_s < now:
            self._seen.popitem(last=False)
        if msg_id in self._seen:
            return False
        self._seen[msg_id] = now
        if len(self._seen) > self.size:
            self._seen.popitem(last=False)
        return True


# ─── camera wrapper ─────────────────────────────────────────────────────────
@dataclass
class CameraSettings:
    exposure_us: int = 2000
    analogue_gain: float = 4.0
    awb_gains: tuple[float, float] = (1.8, 1.6)
    resolution: tuple[int, int] = (3280, 2464)
    jpeg_quality: int = 95


class Camera:
    """Wraps picamera2 with manual exposure + a warm preview loop."""

    def __init__(self):
        self.settings = CameraSettings()
        self.configured = False
        self._lock = threading.Lock()
        self._cam: Optional[Picamera2] = None
        if Picamera2 is not None:
            self._cam = Picamera2()
            self._open()

    def _open(self):
        assert self._cam is not None
        cfg = self._cam.create_still_configuration(
            main={"size": self.settings.resolution, "format": "RGB888"},
            buffer_count=2,
        )
        self._cam.configure(cfg)
        self._cam.start()
        self._apply_controls()

    def _apply_controls(self):
        if self._cam is None:
            return
        s = self.settings
        self._cam.set_controls({
            "AeEnable": False,
            "AwbEnable": False,
            "ExposureTime": s.exposure_us,
            "AnalogueGain": s.analogue_gain,
            "ColourGains": s.awb_gains,
        })

    def configure(self, s: CameraSettings) -> None:
        with self._lock:
            need_reopen = s.resolution != self.settings.resolution
            self.settings = s
            if self._cam is None:
                self.configured = True
                return
            if need_reopen:
                self._cam.stop()
                self._open()
            else:
                self._apply_controls()
            self.configured = True

    def available(self) -> bool:
        return self._cam is not None

    def capture_jpeg(self, dest: Path) -> int:
        """Synchronously capture a JPEG to dest. Returns bytes written."""
        with self._lock:
            if self._cam is None:
                # On dev boxes without a camera, write a placeholder so the
                # rest of the pipeline can be exercised.
                dest.write_bytes(b"\xff\xd8\xff\xe0PLACEHOLDER\xff\xd9")
                return dest.stat().st_size
            self._cam.capture_file(
                str(dest),
                format="jpeg",
                # picamera2 quality is set via the encoder config; for the
                # capture_file shortcut we accept the default and rely on the
                # high analogue gain + short exposure to be the bottleneck.
            )
            return dest.stat().st_size

    def meter(self, settle_s: float = 2.0) -> dict:
        """Briefly enable AE/AWB, let them converge on the scene, read the values
        the camera settled on, then restore the fixed manual state (re-disabling
        auto, keeping the sensor warm). Returns exposure_us / analogue_gain / awb_gains.
        The client averages these across the rig and sends one CONFIGURE."""
        with self._lock:
            if self._cam is None:
                # dev box without a camera — echo current settings as a stand-in.
                s = self.settings
                return {
                    "exposure_us": s.exposure_us,
                    "analogue_gain": s.analogue_gain,
                    "awb_gains": list(s.awb_gains),
                }
            self._cam.set_controls({"AeEnable": True, "AwbEnable": True})
            time.sleep(settle_s)                 # let AE/AWB converge
            md = self._cam.capture_metadata()
            cg = md.get("ColourGains", self.settings.awb_gains)
            result = {
                "exposure_us": int(md.get("ExposureTime", self.settings.exposure_us)),
                "analogue_gain": float(md.get("AnalogueGain", self.settings.analogue_gain)),
                "awb_gains": [float(cg[0]), float(cg[1])],
            }
            # Restore the fixed manual settings (turns auto back off, stays warm).
            self._apply_controls()
            return result


# ─── identity ───────────────────────────────────────────────────────────────
@functools.lru_cache(maxsize=1)
def pi_id() -> str:
    """Stable, collision-free identifier for this node — derived from the eth0 MAC
    (last 6 hex chars, e.g. 'pi-a1b2c3'), matching the first-boot hostname scheme.

    Unlike the hostname, this is unique BY CONSTRUCTION: cloned SD cards that share a
    hostname (e.g. imaged after first-boot.sh already ran) still report distinct `pi`
    values and write non-colliding `<pi>.jpg` files. The MAC never changes at runtime,
    so the result is cached. Falls back to the hostname on dev boxes with no eth0."""
    try:
        mac = Path("/sys/class/net/eth0/address").read_text().strip().replace(":", "")
        if len(mac) >= 6:
            return f"pi-{mac[-6:]}"
    except OSError:
        pass
    return socket.gethostname()


def is_raspberry_pi() -> bool:
    """True only on real Pi hardware — guards REBOOT/HALT from powering off a dev box."""
    try:
        return "raspberry pi" in Path("/proc/device-tree/model").read_text().lower()
    except OSError:
        return False


def chrony_offset_ms() -> Optional[float]:
    """Returns current clock offset to NTP source in ms, or None on failure."""
    try:
        out = subprocess.check_output(["chronyc", "tracking"], text=True, timeout=2)
        for line in out.splitlines():
            if line.startswith("Last offset"):
                # "Last offset     : +0.000123 seconds"
                parts = line.split(":")[1].strip().split()
                return float(parts[0]) * 1000.0
    except (subprocess.SubprocessError, FileNotFoundError, ValueError, OSError):
        return None
    return None


def disk_free_mb(path: Path = CAPTURE_DIR) -> int:
    try:
        stat = os.statvfs(path if path.exists() else path.parent)
        return int(stat.f_bavail * stat.f_frsize / (1024 * 1024))
    except OSError:
        return 0


# ─── atomic file write ──────────────────────────────────────────────────────
def atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    """Write content to path via temp+rename so interrupted writes don't corrupt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


# ─── NTP server config ──────────────────────────────────────────────────────
def read_ntp_server() -> Optional[str]:
    """Returns the current `server` line from chrony.conf, if any."""
    try:
        for line in CHRONY_CONF.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("server ") and not stripped.startswith("#"):
                return stripped.split()[1]
    except OSError:
        pass
    return None


def write_ntp_server(new_server: str) -> None:
    """Rewrite chrony.conf with a single `server <new_server>` line, preserving
    all non-`server`/`pool` directives. Use atomic_write to avoid corruption."""
    lines = []
    server_written = False
    try:
        existing = CHRONY_CONF.read_text().splitlines()
    except OSError:
        existing = []

    for line in existing:
        stripped = line.strip()
        if stripped.startswith("server ") or stripped.startswith("pool "):
            if not server_written:
                lines.append(f"server {new_server} iburst minpoll 4 maxpoll 6")
                server_written = True
            # drop any further server/pool lines — we want exactly one source
            continue
        lines.append(line)

    if not server_written:
        # No existing server line — append one.
        lines.append(f"server {new_server} iburst minpoll 4 maxpoll 6")

    atomic_write(CHRONY_CONF, "\n".join(lines) + "\n", mode=0o644)


def restart_chrony() -> tuple[bool, str]:
    """Returns (success, stderr-on-failure)."""
    result = subprocess.run(
        ["systemctl", "restart", "chrony"],
        capture_output=True, text=True, timeout=10,
    )
    return (result.returncode == 0, result.stderr.strip())


def chrony_synced_and_stratum() -> tuple[bool, int]:
    """Parse `chronyc tracking` for synced state + stratum."""
    try:
        out = subprocess.check_output(["chronyc", "tracking"], text=True, timeout=2)
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False, 0

    stratum = 0
    leap_ok = False
    has_ref = False
    for line in out.splitlines():
        if line.startswith("Stratum"):
            try:
                stratum = int(line.split(":")[1].strip())
            except (IndexError, ValueError):
                pass
        elif line.startswith("Leap status"):
            # "Normal" means synced; "Not synchronised" / "Unknown" means no.
            leap_ok = "Normal" in line
        elif line.startswith("Reference ID"):
            # "Reference ID    : 00000000 ()" when no source.
            has_ref = "00000000" not in line.split(":")[1]
    return (leap_ok and has_ref and 0 < stratum < 16), stratum


# ─── SMB default config ─────────────────────────────────────────────────────
@dataclass
class SmbConfig:
    server: str = ""
    share: str = ""
    credentials_ref: str = "default"

    @classmethod
    def load(cls) -> "SmbConfig":
        try:
            data = yaml.safe_load(SMB_CONF.read_text()) or {}
            return cls(
                server=str(data.get("server", "")),
                share=str(data.get("share", "")),
                credentials_ref=str(data.get("credentials_ref", "default")),
            )
        except (OSError, yaml.YAMLError):
            return cls()

    def save(self) -> None:
        atomic_write(SMB_CONF, yaml.safe_dump({
            "server": self.server,
            "share": self.share,
            "credentials_ref": self.credentials_ref,
        }), mode=0o640)

    def default_dest(self) -> str:
        if not self.server or not self.share:
            return ""
        return f"smb://{self.server}/{self.share}/"


def write_smb_credentials(ref: str, username: str, password: str, domain: str) -> None:
    """Write the smbclient-format credentials file for `ref`. Mode 0600."""
    if not ref or "/" in ref or ref.startswith("."):
        raise ValueError(f"invalid credentials_ref: {ref!r}")
    body = (
        f"username={username}\n"
        f"password={password}\n"
        f"domain={domain or 'WORKGROUP'}\n"
    )
    atomic_write(CREDS_DIR / ref, body, mode=0o600)


def probe_smb(server: str, timeout_s: float = SMB_PROBE_TIMEOUT_S) -> tuple[bool, int, str]:
    """TCP-connect to server:445 with a tight timeout. Returns
    (reachable, duration_ms, error_str)."""
    if not server:
        return False, 0, "no server configured"
    t0 = time.monotonic()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        sock.connect((server, SMB_PORT))
        sock.close()
        return True, int((time.monotonic() - t0) * 1000), ""
    except (socket.timeout, socket.gaierror, OSError) as e:
        return False, int((time.monotonic() - t0) * 1000), str(e)


class SmbProbe:
    """Background thread: probes the configured SMB server every 30 s and
    caches the result so PONG replies are fast (no per-PING network I/O)."""

    def __init__(self, smb: SmbConfig):
        self._smb = smb
        self._lock = threading.Lock()
        self._reachable = False
        self._last_check_at: float = 0.0
        self._last_error = ""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def update_target(self, smb: SmbConfig) -> tuple[bool, int, str]:
        """Swap the target server; next probe uses the new one. Also runs an
        immediate probe and returns its (reachable, duration_ms, error)."""
        with self._lock:
            self._smb = smb
        return self.probe_now()

    def probe_now(self) -> tuple[bool, int, str]:
        with self._lock:
            server = self._smb.server
        reachable, dur_ms, err = probe_smb(server)
        with self._lock:
            self._reachable = reachable
            self._last_error = err
            self._last_check_at = time.monotonic()
        return reachable, dur_ms, err

    def snapshot(self) -> dict:
        with self._lock:
            age = int(time.monotonic() - self._last_check_at) if self._last_check_at else -1
            return {
                "server": self._smb.server,
                "share": self._smb.share,
                "credentials_ref": self._smb.credentials_ref,
                "reachable": self._reachable,
                "last_check_age_s": age,
                "last_error": self._last_error,
            }

    def _run(self) -> None:
        # Small jitter so 32 Pis don't all probe in lockstep.
        time.sleep(1 + (hash(pi_id()) % 1000) / 1000.0)
        while not self._stop.is_set():
            self.probe_now()
            self._stop.wait(SMB_PROBE_INTERVAL_S)


# ─── upload backends ────────────────────────────────────────────────────────
def upload_smb(local: Path, dest_url: str, credentials_ref: str) -> tuple[str, int]:
    """Push local file to smb://host/share/<session>/<filename> using smbclient.
    Returns (remote_path, duration_ms)."""
    # dest_url like smb://rc-box/scans/2026-05-26_rex_take03/
    if not dest_url.startswith("smb://"):
        raise ValueError(f"not an smb url: {dest_url}")
    body = dest_url[len("smb://"):]
    host, _, path = body.partition("/")
    share, _, subpath = path.partition("/")
    subpath = subpath.strip("/")
    creds_file = CREDS_DIR / credentials_ref
    remote_dir = f"//{host}/{share}"
    remote_target = f"{subpath}/{local.name}" if subpath else local.name

    t0 = time.monotonic()
    cmd = [
        "smbclient", remote_dir,
        "-A", str(creds_file),
        "-c", f'prompt OFF; recurse OFF; mkdir "{subpath}"; cd "{subpath}"; put "{local}" "{local.name}"',
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"smbclient failed: {result.stderr.strip()}")
    duration_ms = int((time.monotonic() - t0) * 1000)
    return f"smb://{host}/{share}/{remote_target}", duration_ms


# ─── reply helper ───────────────────────────────────────────────────────────
def make_reply(in_msg: dict, msg_type: str, **fields) -> bytes:
    payload = {
        "v": PROTOCOL_VERSION,
        "id": in_msg.get("id"),
        "msg": msg_type,
        "pi": pi_id(),
        **fields,
    }
    return json.dumps(payload).encode("utf-8")


def make_error(in_msg: dict, reason: str, detail: str = "") -> bytes:
    return make_reply(
        in_msg, "ERROR",
        in_reply_to=in_msg.get("msg"),
        reason=reason,
        detail=detail,
    )


# ─── handlers ───────────────────────────────────────────────────────────────
@dataclass
class Node:
    camera: Camera
    smb_config: SmbConfig = field(default_factory=SmbConfig.load)
    smb_probe: Optional[SmbProbe] = None
    dedupe: DedupeCache = field(default_factory=lambda: DedupeCache(DEDUPE_CACHE_SIZE, DEDUPE_TTL_S))
    started_at: float = field(default_factory=time.monotonic)
    version: str = "0.1.0"

    def __post_init__(self):
        if self.smb_probe is None:
            self.smb_probe = SmbProbe(self.smb_config)

    def handle_ping(self, msg: dict) -> bytes:
        offset_ms = chrony_offset_ms()
        synced, stratum = chrony_synced_and_stratum()
        return make_reply(
            msg, "PONG",
            # legacy top-level field, kept for v1 clients
            clock_offset_ms=offset_ms if offset_ms is not None else -1.0,
            free_mb=disk_free_mb(),
            camera_ok=self.camera.available(),
            uptime_s=int(time.monotonic() - self.started_at),
            version=self.version,
            ntp={
                "server": read_ntp_server() or "",
                "synced": synced,
                "offset_ms": offset_ms if offset_ms is not None else -1.0,
                "stratum": stratum,
            },
            smb=self.smb_probe.snapshot(),
        )

    def handle_set_ntp(self, msg: dict) -> bytes:
        server = msg.get("server", "").strip()
        if not server or any(c.isspace() for c in server):
            return make_error(msg, "bad_ntp_config", "server missing or contains whitespace")
        try:
            write_ntp_server(server)
        except OSError as e:
            return make_error(msg, "bad_ntp_config", f"write failed: {e}")
        ok, stderr = restart_chrony()
        if not ok:
            return make_error(msg, "chrony_restart_failed", stderr)
        log.info("NTP server set to %s, chrony restarted", server)
        return make_reply(msg, "NTP_SET", server=server)

    def handle_set_smb(self, msg: dict) -> bytes:
        try:
            server = str(msg["server"]).strip()
            share = str(msg["share"]).strip()
            username = str(msg["username"])
            password = str(msg["password"])
        except KeyError as e:
            return make_error(msg, "bad_smb_config", f"missing field: {e}")
        if not server or not share or not username:
            return make_error(msg, "bad_smb_config", "server/share/username must be non-empty")

        domain = str(msg.get("domain", "WORKGROUP"))
        creds_ref = str(msg.get("credentials_ref", "default"))

        try:
            write_smb_credentials(creds_ref, username, password, domain)
            new_config = SmbConfig(server=server, share=share, credentials_ref=creds_ref)
            new_config.save()
        except (OSError, ValueError) as e:
            return make_error(msg, "smb_write_failed", str(e))

        # Swap in-memory state and run an immediate probe so the reply reflects
        # the new target, not the old one.
        self.smb_config = new_config
        assert self.smb_probe is not None
        reachable, probe_ms, _ = self.smb_probe.update_target(new_config)
        log.info("SMB set to //%s/%s ref=%s reachable=%s", server, share, creds_ref, reachable)
        return make_reply(
            msg, "SMB_SET",
            server=server,
            share=share,
            reachable=reachable,
            probe_ms=probe_ms,
        )

    def handle_configure(self, msg: dict) -> bytes:
        try:
            s = CameraSettings(
                exposure_us=int(msg["exposure_us"]),
                analogue_gain=float(msg["analogue_gain"]),
                awb_gains=tuple(msg["awb_gains"]),
                resolution=tuple(msg["resolution"]),
                jpeg_quality=int(msg.get("jpeg_quality", 95)),
            )
        except (KeyError, ValueError, TypeError) as e:
            return make_error(msg, "bad_configure", str(e))
        self.camera.configure(s)
        return make_reply(msg, "CONFIGURED")

    def handle_capture(self, msg: dict, reply_to: tuple[str, int], sock: socket.socket) -> Optional[bytes]:
        """Schedules capture in a thread; returns immediately with no reply.
        The thread sends CAPTURED itself once the JPEG is on disk."""
        if not self.camera.configured:
            return make_error(msg, "not_configured")

        session_id = msg.get("session_id", "")
        if not session_id or not all(c.isalnum() or c in "_-" for c in session_id):
            return make_error(msg, "bad_session_id", session_id)

        try:
            trigger_at = float(msg["trigger_at_unix"])
        except (KeyError, ValueError, TypeError):
            return make_error(msg, "bad_capture", "trigger_at_unix missing/invalid")

        leadtime = trigger_at - time.time()
        if leadtime < MIN_LEADTIME_S:
            return make_error(msg, "trigger_too_soon", f"leadtime={leadtime:.3f}s")
        if leadtime > MAX_LEADTIME_S:
            return make_error(msg, "trigger_too_far", f"leadtime={leadtime:.3f}s")

        offset = chrony_offset_ms()
        if offset is None or abs(offset) > MAX_CLOCK_OFFSET_MS:
            return make_error(msg, "clock_unsynced", f"offset={offset}")

        if disk_free_mb() < 100:
            return make_error(msg, "disk_full")

        # Spin off the capture thread; the trigger itself is the blocking step.
        threading.Thread(
            target=self._do_capture,
            args=(msg, session_id, trigger_at, reply_to, sock),
            daemon=True,
        ).start()
        return None  # no immediate reply

    def _do_capture(self, msg: dict, session_id: str, trigger_at: float,
                    reply_to: tuple[str, int], sock: socket.socket) -> None:
        session_dir = CAPTURE_DIR / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        dest = session_dir / f"{pi_id()}.jpg"

        try:
            sleep_until(trigger_at)
            t_shutter = time.time()
            size = self.camera.capture_jpeg(dest)
        except Exception as e:
            sock.sendto(make_error(msg, "capture_failed", str(e)), reply_to)
            return

        reply = make_reply(
            msg, "CAPTURED",
            session_id=session_id,
            actual_at_unix=t_shutter,
            file=dest.name,
            size_bytes=size,
        )
        sock.sendto(reply, reply_to)

    def handle_upload(self, msg: dict, reply_to: tuple[str, int], sock: socket.socket) -> Optional[bytes]:
        session_id = msg.get("session_id", "")
        # dest + credentials_ref now optional — fall back to stored SET_SMB config.
        dest_url = msg.get("dest") or self.smb_config.default_dest()
        creds_ref = msg.get("credentials_ref") or self.smb_config.credentials_ref
        local = CAPTURE_DIR / session_id / f"{pi_id()}.jpg"

        if not dest_url:
            return make_error(
                msg, "no_smb_dest",
                "UPLOAD has no `dest` and no stored default (use SET_SMB first)",
            )
        if not local.exists():
            return make_error(msg, "unknown_session", str(local))

        threading.Thread(
            target=self._do_upload,
            args=(msg, session_id, dest_url, creds_ref, local, reply_to, sock),
            daemon=True,
        ).start()
        return None

    def _do_upload(self, msg, session_id, dest_url, creds_ref, local, reply_to, sock):
        # dest_url + session_id/ as subfolder per protocol
        full_dest = dest_url.rstrip("/") + "/" + session_id + "/"
        try:
            if dest_url.startswith("smb://"):
                remote_path, dur_ms = upload_smb(local, full_dest, creds_ref)
            else:
                sock.sendto(make_error(msg, "upload_failed", f"unsupported scheme: {dest_url}"), reply_to)
                return
        except Exception as e:
            sock.sendto(make_error(msg, "upload_failed", str(e)), reply_to)
            return

        reply = make_reply(
            msg, "UPLOADED",
            session_id=session_id,
            remote_path=remote_path,
            duration_ms=dur_ms,
        )
        sock.sendto(reply, reply_to)

    def handle_clear(self, msg: dict) -> bytes:
        """Delete captured images. With `session_id`, clears just that session's
        folder; without it, clears every session under CAPTURE_DIR. Idempotent —
        clearing a missing session succeeds with counts of 0. Synchronous (a quick
        filesystem op), so it replies directly like CONFIGURE."""
        session_id = msg.get("session_id", "")
        if session_id:
            # Same rule as CAPTURE — also prevents path traversal via the join below.
            if not all(c.isalnum() or c in "_-" for c in session_id):
                return make_error(msg, "bad_session_id", session_id)
            targets = [CAPTURE_DIR / session_id]
        else:
            targets = list(CAPTURE_DIR.iterdir()) if CAPTURE_DIR.exists() else []

        base = CAPTURE_DIR.resolve()
        sessions_removed = files_removed = freed_bytes = 0
        for path in targets:
            # Defense in depth: never touch anything outside CAPTURE_DIR (symlinks etc.).
            try:
                path.resolve().relative_to(base)
            except (ValueError, OSError):
                continue
            if not path.is_dir():
                continue
            for f in path.rglob("*"):
                if f.is_file():
                    files_removed += 1
                    try:
                        freed_bytes += f.stat().st_size
                    except OSError:
                        pass
            shutil.rmtree(path, ignore_errors=True)
            sessions_removed += 1

        return make_reply(
            msg, "CLEARED",
            session_id=session_id,
            sessions_removed=sessions_removed,
            files_removed=files_removed,
            freed_mb=round(freed_bytes / (1024 * 1024), 1),
        )

    def handle_meter(self, msg: dict, reply_to: tuple[str, int], sock: socket.socket) -> Optional[bytes]:
        """Auto-meter the scene in a background thread (AE/AWB convergence takes
        ~1–2 s); the thread sends METERED itself. Keeps the dispatch loop responsive."""
        settle_s = float(msg.get("settle_ms", 2000)) / 1000.0
        settle_s = max(0.2, min(settle_s, 10.0))
        threading.Thread(
            target=self._do_meter, args=(msg, settle_s, reply_to, sock), daemon=True,
        ).start()
        return None

    def _do_meter(self, msg: dict, settle_s: float,
                  reply_to: tuple[str, int], sock: socket.socket) -> None:
        try:
            m = self.camera.meter(settle_s)
        except Exception as e:
            sock.sendto(make_error(msg, "meter_failed", str(e)), reply_to)
            return
        reply = make_reply(
            msg, "METERED",
            exposure_us=m["exposure_us"],
            analogue_gain=round(m["analogue_gain"], 3),
            awb_gains=[round(m["awb_gains"][0], 3), round(m["awb_gains"][1], 3)],
        )
        sock.sendto(reply, reply_to)

    def _power(self, msg: dict, systemctl_action: str, reply_type: str, label: str) -> bytes:
        """Reply first, then run `systemctl <action>` ~1 s later so the reply datagram
        leaves before the box goes down. Runs as root; a no-op on non-Pi dev boxes."""
        def _do():
            time.sleep(1.0)
            if not is_raspberry_pi():
                log.warning("%s requested but this is not a Raspberry Pi — ignoring", label)
                return
            try:
                subprocess.run(["systemctl", systemctl_action], timeout=15)
            except Exception as e:  # noqa: BLE001
                log.error("%s (systemctl %s) failed: %s", label, systemctl_action, e)
        threading.Thread(target=_do, daemon=True).start()
        log.info("%s scheduled in ~1s", label)
        return make_reply(msg, reply_type, action=label)

    def handle_reboot(self, msg: dict) -> bytes:
        return self._power(msg, "reboot", "REBOOTING", "reboot")

    def handle_halt(self, msg: dict) -> bytes:
        return self._power(msg, "poweroff", "HALTING", "halt")


# ─── main loop ──────────────────────────────────────────────────────────────
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

    log.info("picam_node starting as %s", pi_id())

    node = Node(camera=Camera())

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", LISTEN_PORT))
    log.info("listening on udp/%d", LISTEN_PORT)

    while True:
        try:
            data, addr = sock.recvfrom(2048)
        except OSError as e:
            log.warning("recvfrom failed: %s", e)
            continue

        try:
            msg = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            log.debug("dropped non-json datagram from %s", addr)
            continue

        if msg.get("v") != PROTOCOL_VERSION:
            continue
        msg_id = msg.get("id")
        if not msg_id or not node.dedupe.check_and_add(msg_id):
            continue

        msg_type = msg.get("msg")
        log.info("recv %s id=%s from=%s", msg_type, msg_id[:8], addr[0])

        reply: Optional[bytes] = None
        try:
            if msg_type == "PING":
                reply = node.handle_ping(msg)
            elif msg_type == "CONFIGURE":
                reply = node.handle_configure(msg)
            elif msg_type == "CAPTURE":
                reply = node.handle_capture(msg, addr, sock)
            elif msg_type == "UPLOAD":
                reply = node.handle_upload(msg, addr, sock)
            elif msg_type == "SET_NTP":
                reply = node.handle_set_ntp(msg)
            elif msg_type == "SET_SMB":
                reply = node.handle_set_smb(msg)
            elif msg_type == "CLEAR":
                reply = node.handle_clear(msg)
            elif msg_type == "METER":
                reply = node.handle_meter(msg, addr, sock)
            elif msg_type == "REBOOT":
                reply = node.handle_reboot(msg)
            elif msg_type == "HALT":
                reply = node.handle_halt(msg)
            else:
                reply = make_error(msg, "unknown_msg", msg_type or "")
        except Exception as e:
            log.exception("handler crashed")
            reply = make_error(msg, "internal_error", str(e))

        if reply is not None:
            sock.sendto(reply, addr)


if __name__ == "__main__":
    main()
