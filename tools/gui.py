#!/usr/bin/env python3
"""32PiScanner GUI backend — local web control panel for the rig.

Serves the faceplate UI (tools/gui_web/) plus a JSON + SSE API that drives the
fleet over the v1 UDP protocol (docs/protocol.md). Protocol client behavior is
copied from tools/cli.py: broadcasts sent 3× spaced 10 ms, unicast replies
collected by message id, last reply kept per Pi. `--sim N` swaps the transport
for an in-process fake fleet so the whole UI can be exercised with no Pis.

    python3 tools/gui.py                          # real rig on :8321
    python3 tools/gui.py --sim 32 --sim-faults dead:1,ntp:1,smb:1,stale:1
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import mimetypes
import os
import queue
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from PIL import Image  # optional — thumbs fall back to original/SVG
except ImportError:  # pragma: no cover
    Image = None

# ─── constants ──────────────────────────────────────────────────────────────
PROTOCOL_VERSION = 1
UDP_PORT = 9999
BROADCAST = "255.255.255.255"
TRIPLE_SEND_GAP_S = 0.010

STATE_DIR = Path.home() / ".picam_gui"
THUMBS_DIR = STATE_DIR / "thumbs"
CONFIG_PATH = STATE_DIR / "config.json"
WEB_ROOT = Path(__file__).resolve().parent / "gui_web"
PROVISION_DIR = Path(__file__).resolve().parent.parent / "provision"
UPDATE_SCRIPT = PROVISION_DIR / "update-pis.sh"
DIAG_PIS_SCRIPT = PROVISION_DIR / "diagnose-pis.sh"
DIAG_SMB_SCRIPT = PROVISION_DIR / "diagnose-smb.sh"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")   # scripts colorize; the ticker/overlay don't

NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SESSION_PARSE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_([a-z0-9-]+)_take(\d+)$")

PING_TIMEOUT_S = 2.5
CMD_TIMEOUT_S = 5.0
UPLOAD_TIMEOUT_S = 35.0   # > the daemon's 30 s smbclient budget, so slow-but-successful
                          # pushes are never misreported as failed
UPDATE_DEADLINE_S = 600.0  # hard kill for update-pis.sh — it holds the op lock
DIAG_DEADLINE_S = 300.0    # hard kill for diagnose-*.sh — same reason
POWER_TIMEOUT_S = 3.0      # REBOOTING/HALTING acks arrive ~instantly (the daemon
                           # delays the systemctl action 1 s, not the reply)
TICKER_SIZE = 200
SSE_HEARTBEAT_S = 15.0

CONFIG_DEFAULTS: Dict[str, Any] = {
    "expected_pis": 32,
    "scans_root": None,           # resolved at startup
    "ssh_user": "pi",
    "go_max_offset_ms": 2.5,
    "spread_budget_ms": 5.0,
    "motion_cap_us": 2000,
    "leadtime_s": 2.0,
    "verify_min_bytes": 1024,
    "ping_interval_s": 5.0,
    "subjects": [],
    "presets": {},
    "last_configure": None,
    "last_configure_at": None,
    "seen_pis": [],
}


# ─── errors ─────────────────────────────────────────────────────────────────
class ApiError(Exception):
    def __init__(self, status: int, body: dict):
        super().__init__(str(body.get("error", "")))
        self.status = status
        self.body = body


class BusyError(ApiError):
    def __init__(self, op: str):
        super().__init__(409, {"error": "busy", "op": op})


# ─── transport abstraction ──────────────────────────────────────────────────
@dataclass
class Reply:
    pi: str
    msg_type: str
    payload: dict


class RigClient:
    """broadcast(payload, timeout_s, expected) -> replies, last-per-pi.

    `expected` early-exits on raw reply COUNT — right for PING-like verbs where
    any reply counts. For UPLOAD, every alive Pi answers (unknown_session ERRORs
    included), so a count threshold can be filled by fast wrong repliers before
    slow smbclient pushes finish; pass `expected_ids` instead to early-exit only
    once that specific set of pis has replied."""

    def broadcast(self, payload: dict, timeout_s: float,
                  expected: Optional[int] = None,
                  expected_ids: Optional[set] = None) -> List[Reply]:
        raise NotImplementedError


class UdpRig(RigClient):
    """Real transport — exact cli.py semantics (3× send / 10 ms / collect by id)."""

    def broadcast(self, payload: dict, timeout_s: float,
                  expected: Optional[int] = None,
                  expected_ids: Optional[set] = None) -> List[Reply]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", 0))
        try:
            payload = dict(payload)
            payload.setdefault("v", PROTOCOL_VERSION)
            payload.setdefault("id", str(uuid.uuid4()))
            msg_id = payload["id"]
            data = json.dumps(payload).encode("utf-8")
            for i in range(3):
                sock.sendto(data, (BROADCAST, UDP_PORT))
                if i < 2:
                    time.sleep(TRIPLE_SEND_GAP_S)

            replies: Dict[str, Reply] = {}
            deadline = time.monotonic() + timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    raw, _ = sock.recvfrom(4096)
                except socket.timeout:
                    break
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if msg.get("v") != PROTOCOL_VERSION or msg.get("id") != msg_id:
                    continue
                pi = msg.get("pi", "?")
                replies[pi] = Reply(pi=pi, msg_type=msg.get("msg", "?"), payload=msg)
                if expected_ids:
                    if expected_ids.issubset(replies):
                        break
                elif expected is not None and len(replies) >= expected:
                    break
            return sorted(replies.values(), key=lambda r: r.pi)
        finally:
            sock.close()


# ─── sim rig ────────────────────────────────────────────────────────────────
@dataclass
class SimNode:
    pi: str
    index: int
    dead: bool = False
    ntp_fault: bool = False
    smb_fault: bool = False
    stale: bool = False
    configured: bool = True     # sim boots pre-configured for smooth demos
    settings: dict = field(default_factory=dict)
    ntp_server: str = "192.168.50.1"
    smb_server: str = "rig-box"
    smb_share: str = "scans"
    started_at: float = field(default_factory=time.monotonic)
    offline_until: float = 0.0  # sim REBOOT: silent until this monotonic time


class SimRig(RigClient):
    """In-process fake fleet: N stable ids, latency 50–200 ms, fault injection."""

    def __init__(self, n: int, faults: Dict[str, int], scans_root: Path):
        self.scans_root = scans_root
        self.rand = random.Random(0x32)
        kinds: List[str] = []
        for kind in ("dead", "ntp", "smb", "stale"):
            kinds += [kind] * int(faults.get(kind, 0))
        self.nodes: List[SimNode] = []
        for i in range(n):
            pid = "pi-" + hashlib.sha1(("simnode-%d" % i).encode()).hexdigest()[:6]
            kind = kinds[i] if i < len(kinds) else ""
            self.nodes.append(SimNode(
                pi=pid, index=i,
                dead=kind == "dead", ntp_fault=kind == "ntp",
                smb_fault=kind == "smb", stale=kind == "stale",
            ))
        # Pi-side capture store: {session: {pi: jpeg_bytes}} — UPLOAD copies to
        # scans_root, CLEAR drops entries and reports real counts.
        self.store: Dict[str, Dict[str, bytes]] = {}
        self._lock = threading.Lock()

    def broadcast(self, payload: dict, timeout_s: float,
                  expected: Optional[int] = None,
                  expected_ids: Optional[set] = None) -> List[Reply]:
        msg = payload.get("msg", "")
        now = time.time()
        slate: List[Tuple[float, Reply]] = []
        for node in self.nodes:
            if node.dead or time.monotonic() < node.offline_until:
                continue
            made = self._respond(node, msg, payload, now)
            if made is None:
                continue
            delay, mtype, body = made
            full = {"v": PROTOCOL_VERSION, "id": payload.get("id", ""),
                    "msg": mtype, "pi": node.pi}
            full.update(body)
            slate.append((delay, Reply(pi=node.pi, msg_type=mtype, payload=full)))

        arrived = [(d, r) for d, r in slate if d <= timeout_s]
        got_ids = {r.pi for _, r in arrived}
        if expected_ids:
            if expected_ids.issubset(got_ids):
                # early exit once the wanted set is in — at the slowest wanted reply
                time.sleep(min(max(d for d, r in arrived if r.pi in expected_ids),
                               timeout_s))
            else:
                time.sleep(timeout_s)
        elif expected is not None and len(arrived) < expected:
            time.sleep(timeout_s)          # short fleet → client waits full timeout
        elif arrived:
            time.sleep(min(max(d for d, _ in arrived), timeout_s))
        return sorted((r for _, r in arrived), key=lambda r: r.pi)

    # ── per-node reply logic ──
    def _respond(self, node: SimNode, msg: str, payload: dict,
                 now: float) -> Optional[Tuple[float, str, dict]]:
        lat = self.rand.uniform(0.05, 0.20)
        if msg == "PING":
            return lat, "PONG", self._pong(node)
        if msg == "CONFIGURE":
            node.settings = {k: payload.get(k) for k in
                             ("exposure_us", "analogue_gain", "awb_gains",
                              "resolution", "jpeg_quality")}
            node.configured = True
            return lat, "CONFIGURED", {}
        if msg == "METER":
            settle = max(0.2, min(float(payload.get("settle_ms", 2000)) / 1000.0, 10.0))
            n = max(len(self.nodes) - 1, 1)
            exp = int(1400 + 1200 * node.index / n + self.rand.uniform(-60, 60))
            return lat + settle, "METERED", {
                "exposure_us": exp,
                "analogue_gain": round(self.rand.uniform(3.0, 5.0), 3),
                "awb_gains": [round(self.rand.uniform(1.70, 1.90), 3),
                              round(self.rand.uniform(1.50, 1.70), 3)],
            }
        if msg == "CAPTURE":
            return self._capture(node, payload, now, lat)
        if msg == "UPLOAD":
            return self._upload(node, payload, lat)
        if msg == "SET_NTP":
            node.ntp_server = str(payload.get("server", ""))
            return lat + 0.3, "NTP_SET", {"server": node.ntp_server}
        if msg == "SET_SMB":
            node.smb_server = str(payload.get("server", ""))
            node.smb_share = str(payload.get("share", ""))
            reachable = not node.smb_fault
            return lat + 0.2, "SMB_SET", {
                "server": node.smb_server, "share": node.smb_share,
                "reachable": reachable,
                "probe_ms": self.rand.randint(4, 40) if reachable else 1001,
            }
        if msg == "CLEAR":
            return lat, "CLEARED", self._clear(node, payload)
        if msg == "REBOOT":
            # ack now, "go down" ~1 s later for ~8 s, come back with fresh uptime
            node.offline_until = time.monotonic() + 9.0
            node.started_at = node.offline_until
            return lat, "REBOOTING", {"action": "reboot"}
        if msg == "HALT":
            # ack, then stay down — like the real fleet, only a power-cycle
            # (here: a server restart) brings it back
            node.dead = True
            return lat, "HALTING", {"action": "halt"}
        return lat, "ERROR", {"in_reply_to": msg, "reason": "unknown_msg", "detail": msg}

    def _pong(self, node: SimNode) -> dict:
        off = 6.5 if node.ntp_fault else round(self.rand.uniform(-1.5, 1.5), 2)
        return {
            "clock_offset_ms": off,
            "free_mb": 1200 + (node.index * 37) % 200,
            "camera_ok": True,
            "uptime_s": int(time.monotonic() - node.started_at),
            "version": "0.0.9" if node.stale else "0.1.0",
            "ntp": {"server": node.ntp_server, "synced": True,
                    "offset_ms": off, "stratum": 3},
            "smb": {"server": node.smb_server, "share": node.smb_share,
                    "credentials_ref": "default",
                    "reachable": not node.smb_fault,
                    "last_check_age_s": self.rand.randint(1, 29),
                    "last_error": "NT_STATUS_LOGON_FAILURE" if node.smb_fault else ""},
        }

    def _capture(self, node: SimNode, payload: dict, now: float,
                 lat: float) -> Tuple[float, str, dict]:
        if not node.configured:
            return lat, "ERROR", {"in_reply_to": "CAPTURE",
                                  "reason": "not_configured", "detail": ""}
        sid = str(payload.get("session_id", ""))
        try:
            trig = float(payload["trigger_at_unix"])
        except (KeyError, TypeError, ValueError):
            return lat, "ERROR", {"in_reply_to": "CAPTURE", "reason": "bad_capture",
                                  "detail": "trigger_at_unix missing/invalid"}
        lead = trig - now
        if lead < 0.2:
            return lat, "ERROR", {"in_reply_to": "CAPTURE", "reason":
                                  "trigger_too_soon", "detail": "leadtime=%.3fs" % lead}
        if lead > 60.0:
            return lat, "ERROR", {"in_reply_to": "CAPTURE", "reason":
                                  "trigger_too_far", "detail": "leadtime=%.3fs" % lead}
        jitter = self.rand.uniform(0, 0.0025)
        rnd = random.Random("%s:%s" % (sid, node.pi))
        data = (b"\xff\xd8\xff\xe0" + node.pi.encode()
                + rnd.randbytes(rnd.randint(2048, 4000)) + b"\xff\xd9")
        with self._lock:
            self.store.setdefault(sid, {})[node.pi] = data
        return lead + jitter + 0.05, "CAPTURED", {
            "session_id": sid, "actual_at_unix": trig + jitter,
            "file": "%s.jpg" % node.pi, "size_bytes": len(data),
        }

    def _upload(self, node: SimNode, payload: dict,
                lat: float) -> Tuple[float, str, dict]:
        sid = str(payload.get("session_id", ""))
        if node.smb_fault:
            return lat + 0.6, "ERROR", {
                "in_reply_to": "UPLOAD", "reason": "upload_failed",
                "detail": "smbclient failed: NT_STATUS_LOGON_FAILURE"}
        with self._lock:
            data = self.store.get(sid, {}).get(node.pi)
        if data is None:
            return lat, "ERROR", {"in_reply_to": "UPLOAD", "reason": "unknown_session",
                                  "detail": "/var/lib/picam_node/captures/%s/%s.jpg"
                                            % (sid, node.pi)}
        dest = self.scans_root / sid
        dest.mkdir(parents=True, exist_ok=True)
        (dest / ("%s.jpg" % node.pi)).write_bytes(data)
        dur = self.rand.randint(200, 900)
        return lat + dur / 1000.0, "UPLOADED", {
            "session_id": sid,
            "remote_path": "smb://%s/%s/%s/%s.jpg"
                           % (node.smb_server, node.smb_share, sid, node.pi),
            "duration_ms": dur,
        }

    def _clear(self, node: SimNode, payload: dict) -> dict:
        sid = str(payload.get("session_id", "") or "")
        removed = files = freed = 0
        with self._lock:
            targets = [sid] if sid else list(self.store.keys())
            for s in targets:
                data = self.store.get(s, {}).pop(node.pi, None)
                if data is not None:
                    removed += 1
                    files += 1
                    freed += len(data)
                if s in self.store and not self.store[s]:
                    del self.store[s]
        return {"session_id": sid, "sessions_removed": removed,
                "files_removed": files, "freed_mb": round(freed / (1024 * 1024), 1)}


# ─── SSE hub + ticker helpers ───────────────────────────────────────────────
class Hub:
    """Fan-out of (event, data) tuples to per-client queues."""

    def __init__(self):
        self._clients: List["queue.Queue"] = []
        self._lock = threading.Lock()

    def register(self) -> "queue.Queue":
        q: "queue.Queue" = queue.Queue(maxsize=500)
        with self._lock:
            self._clients.append(q)
        return q

    def unregister(self, q: "queue.Queue") -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def publish(self, event: str, data: Any) -> None:
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait((event, data))
            except queue.Full:
                # Stalled client: it would silently miss this event forever.
                # Unregister it and poison its queue so the SSE handler closes
                # the connection — EventSource reconnects and re-seeds from a
                # fresh snapshot.
                self.unregister(q)
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(("__stale__", None))
                except queue.Full:
                    pass


def reply_summary(replies: List[Reply]) -> List[dict]:
    out = []
    for r in replies:
        item = {"pi": r.pi, "msg": r.msg_type}
        item.update({k: v for k, v in r.payload.items()
                     if k not in ("v", "id", "msg", "pi")})
        out.append(item)
    return out


def sanitize_subject(s: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")
    return s or "subject"


def placeholder_svg(pi: str, size: int) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="360" height="270" '
        'viewBox="0 0 360 270"><rect width="360" height="270" fill="#202326"/>'
        '<rect x="0.5" y="0.5" width="359" height="269" fill="none" '
        'stroke="#34393C"/><text x="180" y="128" fill="#D7DBDD" '
        'font-family="ui-monospace,Menlo,monospace" font-size="22" '
        'text-anchor="middle">%s</text>'
        '<text x="180" y="158" fill="#878E93" '
        'font-family="ui-monospace,Menlo,monospace" font-size="13" '
        'text-anchor="middle">%d B</text></svg>' % (pi, size)
    )


# ─── application core ───────────────────────────────────────────────────────
class App:
    def __init__(self, port: int, scans_root: Optional[str],
                 sim_n: int, sim_faults: Dict[str, int]):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        THUMBS_DIR.mkdir(parents=True, exist_ok=True)

        self._cfg_lock = threading.RLock()
        self.config = self._load_config()

        # An explicit --scans-root applies to THIS run only — persisting it
        # would let a one-off debug run silently repoint where every future
        # session verifies and reviews.
        explicit_root = scans_root is not None
        root = scans_root or self.config.get("scans_root")
        if not root:
            root = "/srv/scans" if Path("/srv/scans").is_dir() \
                else str(STATE_DIR / "sim-scans")
        self.scans_root = Path(root).expanduser()
        if not self.scans_root.exists() and str(self.scans_root) != "/srv/scans":
            self.scans_root.mkdir(parents=True, exist_ok=True)
        if not explicit_root:
            self.config["scans_root"] = str(self.scans_root)

        self.sim = sim_n > 0
        if self.sim:
            self.config["expected_pis"] = sim_n
            self.rig: RigClient = SimRig(sim_n, sim_faults, self.scans_root)
        else:
            self.rig = UdpRig()
        self._save_config()

        self.hub = Hub()
        self.ticker: "deque" = deque(maxlen=TICKER_SIZE)
        self.op_lock = threading.Lock()
        self.current_op: Optional[str] = None
        # Serializes sweep broadcasts + fleet/verdict commits: the background
        # loop, the SWEEP button and a take's gating sweep may otherwise
        # interleave and let a stale slow sweep overwrite a fresher one.
        self._sweep_lock = threading.Lock()

        self.fleet: dict = {"checked_at": None,
                            "expected": int(self.config["expected_pis"]), "pis": []}
        self.verdict: dict = {"state": "NO-GO", "reasons": ["no sweep yet"],
                              "counts": {}, "armed": False}
        self._last_tick_sig: Optional[tuple] = None

        threading.Thread(target=self._ping_loop, name="ping-loop",
                         daemon=True).start()

    # ── config persistence ──
    def _load_config(self) -> dict:
        try:
            data = json.loads(CONFIG_PATH.read_text())
        except (OSError, ValueError):
            data = {}
        cfg = dict(CONFIG_DEFAULTS)
        cfg["subjects"] = []
        cfg["presets"] = {}
        cfg["seen_pis"] = []
        cfg.update({k: v for k, v in data.items() if k in CONFIG_DEFAULTS})
        return cfg

    def _save_config(self) -> None:
        with self._cfg_lock:
            tmp = CONFIG_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.config, indent=2))
            os.replace(tmp, CONFIG_PATH)

    # ── ticker ──
    def tick(self, level: str, text: str) -> None:
        entry = {"ts": time.time(), "level": level, "text": text}
        self.ticker.append(entry)
        self.hub.publish("ticker", entry)

    # ── operation lock ──
    @contextlib.contextmanager
    def operation(self, name: str):
        if not self.op_lock.acquire(blocking=False):
            raise BusyError(self.current_op or "?")
        self.current_op = name
        self.hub.publish("op", {"op": name, "status": "start"})
        extra: dict = {}
        try:
            yield extra
        except ApiError as e:
            self.hub.publish("op", {"op": name, "status": "error",
                                    "detail": str(e.body.get("error", e))})
            raise
        except Exception as e:
            self.hub.publish("op", {"op": name, "status": "error", "detail": str(e)})
            raise
        else:
            done = {"op": name, "status": "done"}
            done.update(extra)
            self.hub.publish("op", done)
        finally:
            self.current_op = None
            self.op_lock.release()

    # ── sweeps + verdict ──
    def _ping_loop(self) -> None:
        time.sleep(0.2)
        while True:
            if self.current_op is None:       # skip while an op holds the lock
                try:
                    self.sweep()
                except Exception:
                    pass
            time.sleep(float(self.config.get("ping_interval_s", 5.0)))

    def sweep(self, force_tick: bool = False) -> Tuple[dict, dict]:
        with self._sweep_lock:
            return self._sweep_locked(force_tick)

    def _sweep_locked(self, force_tick: bool) -> Tuple[dict, dict]:
        expected = int(self.config["expected_pis"])
        replies = self.rig.broadcast({"msg": "PING"}, PING_TIMEOUT_S,
                                     expected=expected)
        pis = []
        for r in replies:
            if r.msg_type != "PONG":
                continue
            p = {k: v for k, v in r.payload.items() if k not in ("v", "id", "msg")}
            pis.append(p)

        versions = Counter(str(p.get("version", "?")) for p in pis)
        mode_version = versions.most_common(1)[0][0] if versions else None
        for p in pis:
            p["stale"] = (mode_version is not None
                          and str(p.get("version", "?")) != mode_version)

        with self._cfg_lock:
            seen = set(self.config["seen_pis"]) | {p["pi"] for p in pis}
            if sorted(seen) != self.config["seen_pis"]:
                self.config["seen_pis"] = sorted(seen)
                self._save_config()

        fleet = {"checked_at": time.time(), "expected": expected, "pis": pis}
        verdict = self._compute_verdict(pis, expected)
        self.fleet, self.verdict = fleet, verdict

        sig = (verdict["state"], len(pis))
        if force_tick or sig != self._last_tick_sig:
            self._last_tick_sig = sig
            level = {"GO": "ok", "DEGRADED": "warn"}.get(verdict["state"], "fail")
            text = "PING %d/%d · %s" % (len(pis), expected, verdict["state"])
            if verdict["reasons"]:
                text += " · " + verdict["reasons"][0]
            self.tick(level, text)

        self.hub.publish("fleet", {"fleet": fleet, "verdict": verdict})
        return fleet, verdict

    def _laptop_reasons(self) -> List[str]:
        out = []
        root = self.scans_root
        if not root.is_dir():
            out.append("SCANS ROOT MISSING %s" % root)
        elif not os.access(str(root), os.W_OK):
            out.append("SCANS ROOT NOT WRITABLE %s" % root)
        else:
            try:
                st = os.statvfs(str(root))
                free = st.f_bavail * st.f_frsize
                if free < 1024 ** 3:
                    out.append("LAPTOP FREE %d MB < 1 GB" % (free // (1024 * 1024)))
            except OSError as e:
                out.append("LAPTOP DISK CHECK FAILED %s" % e)
        return out

    def _compute_verdict(self, pis: List[dict], expected: int) -> dict:
        cfg = self.config
        go_max = float(cfg["go_max_offset_ms"])
        reasons: List[str] = []

        got = {p["pi"] for p in pis}
        silent = sorted(set(cfg["seen_pis"]) - got)
        if len(pis) < expected:
            if silent:
                shown = " ".join(silent[:4]) + (" …" if len(silent) > 4 else "")
                reasons.append("SILENT " + shown)
            else:
                reasons.append("%d PI(S) SILENT" % (expected - len(pis)))
        elif silent:
            silent = []  # full head-count — stale seen_pis entries don't matter

        def _off(p: dict) -> float:
            ntp = p.get("ntp") or {}
            return float(ntp.get("offset_ms", p.get("clock_offset_ms", 0.0)))

        cam_bad = [p["pi"] for p in pis if not p.get("camera_ok")]
        unsynced = [p["pi"] for p in pis if not (p.get("ntp") or {}).get("synced")]
        off_over = [p["pi"] for p in pis if abs(_off(p)) > 5.0]
        off_warn = [p["pi"] for p in pis if go_max < abs(_off(p)) <= 5.0]
        smb_bad = [p["pi"] for p in pis if not (p.get("smb") or {}).get("reachable")]
        disk_low = [p["pi"] for p in pis if int(p.get("free_mb", 0)) < 100]
        versions = Counter(str(p.get("version", "?")) for p in pis)

        if cam_bad:
            reasons.append("CAM FAIL " + " ".join(cam_bad))
        if unsynced:
            reasons.append("UNSYNCED " + " ".join(unsynced))
        if off_over:
            reasons.append("OFFSET >5ms " + " ".join(off_over))
        if smb_bad:
            errs = {p["pi"]: (p.get("smb") or {}).get("last_error", "") for p in pis}
            first = smb_bad[0]
            detail = (" (%s)" % errs[first][:30]) if errs.get(first) else ""
            reasons.append("SMB UNREACHABLE " + " ".join(smb_bad) + detail)
        if disk_low:
            reasons.append("DISK <100MB " + " ".join(disk_low))
        reasons += self._laptop_reasons()

        if reasons:
            state = "NO-GO"
        elif len(versions) > 1 or off_warn:
            state = "DEGRADED"
            if len(versions) > 1:
                reasons.append("MIXED VERSIONS " + "/".join(sorted(versions)))
            if off_warn:
                reasons.append("OFFSET WARN " + " ".join(off_warn))
        else:
            state = "GO"

        counts = {
            "expected": expected, "replied": len(pis), "silent": silent,
            "camera_bad": cam_bad, "unsynced": unsynced,
            "offset_over": off_over, "offset_warn": off_warn,
            "smb_bad": smb_bad, "disk_low": disk_low,
            "versions": dict(versions),
        }
        return {"state": state, "reasons": reasons, "counts": counts,
                "armed": state != "NO-GO"}

    # ── sessions on disk ──
    def read_manifest(self, session: str) -> Optional[dict]:
        try:
            return json.loads((self.scans_root / session / "session.json").read_text())
        except (OSError, ValueError):
            return None

    def write_manifest(self, session: str, data: dict) -> None:
        d = self.scans_root / session
        d.mkdir(parents=True, exist_ok=True)
        # temp+rename: list_sessions/read_manifest run concurrently from other
        # request threads and must never see a torn session.json.
        tmp = d / "session.json.tmp"
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, d / "session.json")

    def list_sessions(self) -> List[dict]:
        out = []
        if not self.scans_root.is_dir():
            return out
        dirs = [d for d in self.scans_root.iterdir()
                if d.is_dir() and NAME_RE.match(d.name)]
        dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        for d in dirs:
            files = len(list(d.glob("*.jpg")))
            m = self.read_manifest(d.name)
            if m:
                out.append({
                    "session": d.name,
                    "subject": m.get("subject"),
                    "take": m.get("take"),
                    "test": bool(m.get("test")),
                    "created_at": m.get("created_at"),
                    "expected": m.get("expected"),
                    "files": files,
                    "verified": bool(m.get("verified")),
                    "spread_ms": m.get("spread_ms"),
                    "cleared_on_pis": bool(m.get("cleared_on_pis")),
                    "triage": m.get("triage"),
                    "has_manifest": True,
                })
            else:
                pm = SESSION_PARSE_RE.match(d.name)
                out.append({
                    "session": d.name,
                    "subject": pm.group(2) if pm else None,
                    "take": int(pm.group(3)) if pm else None,
                    "test": False,
                    "created_at": d.stat().st_mtime,
                    "expected": None,
                    "files": files,
                    "verified": False,
                    "spread_ms": None,
                    "cleared_on_pis": False,
                    "triage": None,
                    "has_manifest": False,
                })
        return out

    def next_session_id(self, subject: str) -> str:
        date = time.strftime("%Y-%m-%d")
        pat = re.compile(re.escape("%s_%s_take" % (date, subject)) + r"(\d+)$")
        mx = 0
        if self.scans_root.is_dir():
            for d in self.scans_root.iterdir():
                m = pat.match(d.name)
                if m:
                    mx = max(mx, int(m.group(1)))
        return "%s_%s_take%02d" % (date, subject, mx + 1)

    def _verify(self, session: str, captured_pis: List[str],
                expected: int) -> Tuple[dict, bool]:
        # Validity = JPEG SOI head + EOI tail. The tail is the real truncation
        # detector (a cut-short smb push loses it); a size floor would wrongly
        # reject the daemon's tiny dev-mode placeholder while proving nothing
        # about completeness.
        ok = 0
        missing: List[str] = []
        bad: List[str] = []
        for pi in sorted(captured_pis):
            p = self.scans_root / session / ("%s.jpg" % pi)
            try:
                size = p.stat().st_size
                with p.open("rb") as f:
                    head = f.read(2)
                    if size >= 4:
                        f.seek(-2, os.SEEK_END)
                        tail = f.read(2)
                    else:
                        tail = b""
            except OSError:
                missing.append(pi)
                continue
            if head != b"\xff\xd8" or tail != b"\xff\xd9":
                bad.append(pi)
            else:
                ok += 1
        verified = (not missing and not bad
                    and len(captured_pis) == int(expected))
        return {"ok": ok, "missing": missing, "bad": bad}, verified

    # ── state snapshot ──
    def state(self) -> dict:
        return {
            "fleet": self.fleet,
            "verdict": self.verdict,
            "config": self.config,
            "current_op": self.current_op,
            "sessions": self.list_sessions(),
            "ticker": list(self.ticker),
            "sim": isinstance(self.rig, SimRig),
        }

    # ── shared op internals (called with the lock already held) ──
    def _do_configure(self, cfg: dict) -> Tuple[int, List[Reply]]:
        payload = {"msg": "CONFIGURE"}
        payload.update(cfg)
        expected = int(self.config["expected_pis"])
        replies = self.rig.broadcast(payload, CMD_TIMEOUT_S, expected=expected)
        acks = sum(1 for r in replies if r.msg_type == "CONFIGURED")
        with self._cfg_lock:
            self.config["last_configure"] = cfg
            self.config["last_configure_at"] = time.time()
            self._save_config()
        return acks, replies

    def _do_autoconfigure(self, settle_s: float, motion_safe: bool) -> dict:
        expected = int(self.config["expected_pis"])
        replies = self.rig.broadcast(
            {"msg": "METER", "settle_ms": int(settle_s * 1000)},
            settle_s + 4.0, expected=expected)
        metered = [r for r in replies if r.msg_type == "METERED"]
        if not metered:
            raise ApiError(502, {"error": "no METERED replies"})

        n = len(metered)
        exps = [int(r.payload["exposure_us"]) for r in metered]
        gains = [float(r.payload["analogue_gain"]) for r in metered]
        rs = [float(r.payload["awb_gains"][0]) for r in metered]
        bs = [float(r.payload["awb_gains"][1]) for r in metered]
        avg_exp = sum(exps) / n
        avg_gain, avg_r, avg_b = sum(gains) / n, sum(rs) / n, sum(bs) / n

        cap = int(self.config["motion_cap_us"])
        clamped = bool(motion_safe and avg_exp > cap)
        final_exp = cap if clamped else int(round(avg_exp))

        base = self.config.get("last_configure") or {}
        applied = {
            "exposure_us": int(final_exp),
            "analogue_gain": round(avg_gain, 2),
            "awb_gains": [round(avg_r, 2), round(avg_b, 2)],
            "resolution": list(base.get("resolution") or [3280, 2464]),
            "jpeg_quality": int(base.get("jpeg_quality") or 95),
        }
        acks, _ = self._do_configure(applied)
        self.tick("warn" if clamped else "ok",
                  "AUTOCONFIGURE %dµs g%.2f%s · %d/%d ack"
                  % (applied["exposure_us"], applied["analogue_gain"],
                     " (clamped)" if clamped else "", acks, expected))
        return {
            "metered": {
                "n": n,
                "exposure": {"avg": round(avg_exp, 1),
                             "min": min(exps), "max": max(exps)},
                "gain": {"avg": round(avg_gain, 2),
                         "min": round(min(gains), 2), "max": round(max(gains), 2)},
                "awb": [round(avg_r, 2), round(avg_b, 2)],
            },
            "clamped": clamped,
            "applied": applied,
            "acks": acks,
        }

    # ─── API handlers ───────────────────────────────────────────────────────
    def api_ping(self, body: dict) -> dict:
        fleet, verdict = self.sweep(force_tick=True)
        return {"fleet": fleet, "verdict": verdict}

    def api_configure(self, body: dict) -> dict:
        try:
            cfg = {
                "exposure_us": int(body["exposure_us"]),
                "analogue_gain": float(body["analogue_gain"]),
                "awb_gains": [float(body["awb_r"]), float(body["awb_b"])],
                "resolution": [int(body["width"]), int(body["height"])],
                "jpeg_quality": int(body["jpeg_quality"]),
            }
        except (KeyError, TypeError, ValueError) as e:
            raise ApiError(400, {"error": "bad configure body: %s" % e})
        expected = int(self.config["expected_pis"])
        with self.operation("configure"):
            acks, replies = self._do_configure(cfg)
            self.tick("ok" if acks == expected else "warn",
                      "CONFIGURE %dµs g%.2f · %d/%d ack"
                      % (cfg["exposure_us"], cfg["analogue_gain"], acks, expected))
        return {"applied": cfg, "at": self.config["last_configure_at"],
                "acks": acks, "expected": expected,
                "results": reply_summary(replies)}

    def api_preset_save(self, body: dict) -> dict:
        name = str(body.get("name", "")).strip()
        if not name:
            raise ApiError(400, {"error": "name required"})
        last = self.config.get("last_configure")
        if not last:
            raise ApiError(400, {"error": "no last_configure to save"})
        with self._cfg_lock:
            self.config["presets"][name] = dict(last)
            self._save_config()
        return {"presets": self.config["presets"]}

    def api_preset_apply(self, body: dict) -> dict:
        name = str(body.get("name", "")).strip()
        cfg = (self.config.get("presets") or {}).get(name)
        if not cfg:
            raise ApiError(404, {"error": "unknown preset: %s" % name})
        expected = int(self.config["expected_pis"])
        with self.operation("configure"):
            acks, replies = self._do_configure(dict(cfg))
            self.tick("ok" if acks == expected else "warn",
                      "PRESET %s applied · %d/%d ack" % (name, acks, expected))
        return {"applied": cfg, "at": self.config["last_configure_at"],
                "acks": acks, "expected": expected,
                "results": reply_summary(replies)}

    def api_preset_delete(self, body: dict) -> dict:
        name = str(body.get("name", "")).strip()
        with self._cfg_lock:
            if name not in self.config["presets"]:
                raise ApiError(404, {"error": "unknown preset: %s" % name})
            del self.config["presets"][name]
            self._save_config()
        return {"presets": self.config["presets"]}

    def api_autoconfigure(self, body: dict) -> dict:
        # Same clamp as the daemon applies to settle_ms — an unbounded value
        # would hold the op lock (and the rig) for that long.
        settle = max(0.2, min(float(body.get("settle_s", 2.0)), 10.0))
        motion_safe = bool(body.get("motion_safe", True))
        with self.operation("autoconfigure"):
            return self._do_autoconfigure(settle, motion_safe)

    def api_take(self, body: dict) -> dict:
        test = bool(body.get("test"))
        override = bool(body.get("override"))
        leadtime = float(body.get("leadtime_s") or self.config["leadtime_s"])
        leadtime = max(0.5, min(leadtime, 30.0))
        subject = "test" if test else sanitize_subject(str(body.get("subject") or ""))
        with self.operation("take") as opctx:
            fleet, verdict = self.sweep()
            if verdict["state"] == "NO-GO" and not override:
                raise ApiError(409, {"error": "no-go",
                                     "reasons": verdict["reasons"],
                                     "verdict": verdict})
            report = self._run_take(subject, test, leadtime, verdict, fleet)
            opctx["report"] = report
        self.hub.publish("sessions", self.list_sessions())
        return report

    def _run_take(self, subject: str, test: bool, leadtime: float,
                  verdict: dict, fleet: dict) -> dict:
        cfg = self.config
        expected = int(cfg["expected_pis"])
        budget = float(cfg["spread_budget_ms"])
        session = self.next_session_id(subject)
        pm = SESSION_PARSE_RE.match(session)
        take_n = int(pm.group(3)) if pm else 0

        # ── capture ──
        # trigger_at is computed AFTER the gating sweep, so publish it in the
        # step event — the browser countdown re-bases on it; a click-time local
        # countdown would beep T0 up to sweep-timeout early (subject relaxes
        # before the real shutter).
        trigger_at = time.time() + leadtime
        self.hub.publish("op", {"op": "take", "step": "capture", "status": "run",
                                "session": session,
                                "trigger_at_unix": trigger_at,
                                "lead_remaining_s": round(leadtime, 3)})
        replies = self.rig.broadcast(
            {"msg": "CAPTURE", "session_id": session,
             "trigger_at_unix": trigger_at},
            leadtime + 2.5, expected=expected)
        captured = [r for r in replies if r.msg_type == "CAPTURED"]
        cap_errors = [{"pi": r.pi, "reason": r.payload.get("reason", "?")}
                      for r in replies if r.msg_type == "ERROR"]
        seen = {r.pi for r in replies}
        known = set(cfg["seen_pis"]) | {p["pi"] for p in fleet["pis"]}
        # A Pi that never PONGed is in neither `known` nor `seen` — it can only
        # show up as an anonymous shortfall, which must still force a retake.
        missing = sorted(known - seen) if len(seen) < expected else []
        shortfall = max(0, expected - len(seen))

        actuals = [float(r.payload["actual_at_unix"]) for r in captured
                   if r.payload.get("actual_at_unix")]
        spread_ms = round((max(actuals) - min(actuals)) * 1000, 2) \
            if len(actuals) >= 2 else None
        # A complete single-camera take has no spread to judge — that's a pass,
        # not a warning.
        spread_ok = (spread_ms <= budget) if spread_ms is not None \
            else (expected == 1 and len(captured) == 1)

        n_cap = len(captured)
        cap_text = "CAPTURED %d/%d" % (n_cap, expected)
        if spread_ms is not None:
            cap_text += " · spread %.1f ms (budget %.1f)" % (spread_ms, budget)
        self.tick("fail" if n_cap < expected else ("ok" if spread_ok else "warn"),
                  cap_text)
        self.hub.publish("op", {"op": "take", "step": "capture", "status": "done",
                                "session": session, "captured": n_cap,
                                "expected": expected, "errors": len(cap_errors),
                                "missing": missing, "spread_ms": spread_ms})

        # ── upload ──
        captured_set = {r.pi for r in captured}
        uploaded: List[Reply] = []
        up_errors: List[dict] = []
        if captured:
            self.hub.publish("op", {"op": "take", "step": "upload",
                                    "status": "run", "session": session})
            # Early-exit only when every pi that actually CAPTURED has answered
            # — a raw count fills up with instant unknown_session ERRORs from
            # bystander Pis while the slowest real upload is still in flight.
            u_replies = self.rig.broadcast(
                {"msg": "UPLOAD", "session_id": session},
                UPLOAD_TIMEOUT_S, expected_ids=set(captured_set))
            uploaded = [r for r in u_replies if r.msg_type == "UPLOADED"]
            up_errors = [{"pi": r.pi, "reason": r.payload.get("reason", "?")}
                         for r in u_replies
                         if r.msg_type == "ERROR" and r.pi in captured_set]
            self.tick("ok" if len(uploaded) == n_cap else "warn",
                      "UPLOADED %d/%d" % (len(uploaded), n_cap))
        self.hub.publish("op", {"op": "take", "step": "upload", "status": "done",
                                "session": session, "uploaded": len(uploaded),
                                "captured": n_cap, "errors": len(up_errors)})

        # ── verify ──
        self.hub.publish("op", {"op": "take", "step": "verify", "status": "run",
                                "session": session})
        verify, verified = self._verify(session, sorted(captured_set), expected)
        if verified:
            self.tick("ok", "VERIFIED %s" % session)
        else:
            self.tick("fail", "VERIFY FAILED %s · %d ok · %d missing · %d bad"
                      % (session, verify["ok"], len(verify["missing"]),
                         len(verify["bad"])))
        self.hub.publish("op", {"op": "take", "step": "verify", "status": "done",
                                "session": session, "verified": verified,
                                "ok": verify["ok"], "missing": verify["missing"],
                                "bad": verify["bad"]})

        if missing or cap_errors or shortfall:
            triage = "retake"
        elif not verified:
            triage = "retry_upload"
        else:
            triage = "ok"

        manifest = {
            "session": session, "subject": subject, "take": take_n, "test": test,
            "created_at": time.time(), "expected": expected,
            "settings": cfg.get("last_configure"),
            "trigger_at_unix": trigger_at,
            "captured": sorted(captured_set), "missing": missing,
            "shortfall": shortfall,
            "capture_errors": cap_errors,
            "spread_ms": spread_ms, "spread_ok": spread_ok,
            "uploaded": sorted(r.pi for r in uploaded),
            "upload_errors": up_errors,
            "verify": verify, "verified": verified, "triage": triage,
            "cleared_on_pis": False,
        }

        # ── test frames: verified → wipe the Pi-side copies immediately ──
        if test and verified:
            c_replies = self.rig.broadcast(
                {"msg": "CLEAR", "session_id": session},
                CMD_TIMEOUT_S, expected=expected)
            acks = sum(1 for r in c_replies if r.msg_type == "CLEARED")
            manifest["cleared_on_pis"] = acks > 0
            self.tick("info", "CLEARED %s on Pis · %d/%d ack (test frame)"
                      % (session, acks, expected))

        self.write_manifest(session, manifest)

        return {
            "session": session,
            "verdict_at_fire": verdict,
            "captured": n_cap,
            "expected": expected,
            "missing": missing,
            "shortfall": shortfall,
            "capture_errors": cap_errors,
            "spread_ms": spread_ms,
            "spread_ok": spread_ok,
            "uploaded": len(uploaded),
            "upload_errors": up_errors,
            "verify": verify,
            "verified": verified,
            "triage": triage,
        }

    def api_upload_retry(self, body: dict) -> dict:
        session = str(body.get("session", ""))
        if not NAME_RE.match(session):
            raise ApiError(400, {"error": "bad session name"})
        manifest = self.read_manifest(session)
        if not manifest:
            raise ApiError(404, {"error": "no manifest for %s" % session})
        expected = int(manifest.get("expected") or self.config["expected_pis"])
        captured = list(manifest.get("captured") or [])
        with self.operation("upload-retry"):
            u_replies = self.rig.broadcast(
                {"msg": "UPLOAD", "session_id": session},
                UPLOAD_TIMEOUT_S, expected_ids=set(captured) or None)
            uploaded = [r for r in u_replies if r.msg_type == "UPLOADED"]
            up_errors = [{"pi": r.pi, "reason": r.payload.get("reason", "?")}
                         for r in u_replies
                         if r.msg_type == "ERROR" and r.pi in set(captured)]
            verify, verified = self._verify(session, captured, expected)
            self.tick("ok" if verified else "warn",
                      "RETRY UPLOAD %s · %d/%d up · %s"
                      % (session, len(uploaded), len(captured),
                         "VERIFIED" if verified else "INCOMPLETE"))
            missing = list(manifest.get("missing") or [])
            cap_errors = list(manifest.get("capture_errors") or [])
            if missing or cap_errors or int(manifest.get("shortfall") or 0):
                triage = "retake"
            elif not verified:
                triage = "retry_upload"
            else:
                triage = "ok"
            manifest["uploaded"] = sorted(set(manifest.get("uploaded") or [])
                                          | {r.pi for r in uploaded})
            manifest["upload_errors"] = up_errors
            manifest["verify"] = verify
            manifest["verified"] = verified
            manifest["triage"] = triage
            self.write_manifest(session, manifest)
        self.hub.publish("sessions", self.list_sessions())
        return {
            "session": session,
            "captured": len(captured),
            "missing": missing,
            "capture_errors": cap_errors,
            "spread_ms": manifest.get("spread_ms"),
            "spread_ok": bool(manifest.get("spread_ok")),
            "uploaded": len(uploaded),
            "upload_errors": up_errors,
            "verify": verify,
            "verified": verified,
            "triage": triage,
        }

    def api_clear(self, body: dict) -> dict:
        session = str(body.get("session", ""))
        if not NAME_RE.match(session):
            raise ApiError(400, {"error": "bad session name"})
        manifest = self.read_manifest(session)
        if not manifest or not manifest.get("verified"):
            raise ApiError(409, {"error": "session not verified — clear refused",
                                 "session": session})
        # The manifest flag is history, not ground truth: files may have been
        # pruned/moved/corrupted on the share since it was written, and CLEAR
        # destroys the only other copies. Re-verify against the disk right now.
        verify, still_ok = self._verify(session,
                                        list(manifest.get("captured") or []),
                                        int(manifest.get("expected")
                                            or self.config["expected_pis"]))
        if not still_ok:
            manifest["verify"] = verify
            manifest["verified"] = False
            self.write_manifest(session, manifest)
            self.hub.publish("sessions", self.list_sessions())
            self.tick("fail", "CLEAR REFUSED %s · share re-verify failed "
                      "(%d ok · %d missing · %d bad)"
                      % (session, verify["ok"], len(verify["missing"]),
                         len(verify["bad"])))
            raise ApiError(409, {
                "error": "share re-verify failed — files changed since "
                         "verification; clear refused",
                "session": session, "verify": verify})
        expected = int(self.config["expected_pis"])
        with self.operation("clear"):
            replies = self.rig.broadcast(
                {"msg": "CLEAR", "session_id": session},
                CMD_TIMEOUT_S, expected=expected)
            acks = sum(1 for r in replies if r.msg_type == "CLEARED")
            freed = sum(float(r.payload.get("freed_mb", 0)) for r in replies
                        if r.msg_type == "CLEARED")
            manifest["cleared_on_pis"] = acks > 0
            self.write_manifest(session, manifest)
            self.tick("ok", "CLEAR %s · %d/%d ack · freed %.1f MB"
                      % (session, acks, expected, freed))
        self.hub.publish("sessions", self.list_sessions())
        return {"session": session, "acks": acks, "expected": expected,
                "freed_mb": round(freed, 1), "results": reply_summary(replies)}

    def api_clear_all(self, body: dict) -> dict:
        if body.get("confirm") != "CLEAR ALL":
            raise ApiError(400, {"error": 'confirm must be "CLEAR ALL"'})
        expected = int(self.config["expected_pis"])
        with self.operation("clear"):
            replies = self.rig.broadcast({"msg": "CLEAR"}, 8.0, expected=expected)
            acks = sum(1 for r in replies if r.msg_type == "CLEARED")
            freed = sum(float(r.payload.get("freed_mb", 0)) for r in replies
                        if r.msg_type == "CLEARED")
            files = sum(int(r.payload.get("files_removed", 0)) for r in replies
                        if r.msg_type == "CLEARED")
            self.tick("warn", "CLEAR ALL · %d/%d ack · %d files · freed %.1f MB"
                      % (acks, expected, files, freed))
        return {"acks": acks, "expected": expected, "files_removed": files,
                "freed_mb": round(freed, 1), "results": reply_summary(replies)}

    def session_detail(self, session: str) -> dict:
        """Summary row + full manifest for one session on the share."""
        if not NAME_RE.match(session):
            raise ApiError(400, {"error": "bad session name"})
        d = self.scans_root / session
        if not d.is_dir():
            raise ApiError(404, {"error": "no such session"})
        summary = next((s for s in self.list_sessions()
                        if s["session"] == session), None)
        return {"summary": summary, "manifest": self.read_manifest(session)}

    def api_session_delete(self, body: dict) -> dict:
        """Delete a session directory from the scans share (laptop side only —
        the Pis are untouched). When the Pi-side copies were already CLEARed,
        the share holds the ONLY copy, so the confirm word escalates from
        "DELETE" to the full session name."""
        session = str(body.get("session", ""))
        if not NAME_RE.match(session):
            raise ApiError(400, {"error": "bad session name"})
        root = self.scans_root.resolve()
        target = (root / session).resolve()
        if target == root or not target.is_relative_to(root):
            raise ApiError(400, {"error": "bad path"})
        if not target.is_dir():
            raise ApiError(404, {"error": "no such session"})
        manifest = self.read_manifest(session)
        # Only a manifest that says the Pis still hold their copies proves this
        # is NOT the last copy; no manifest = unknown provenance = assume last.
        only_copy = manifest is None or bool(manifest.get("cleared_on_pis"))
        required = session if only_copy else "DELETE"
        if body.get("confirm") != required:
            raise ApiError(400, {
                "error": 'confirm must be "%s"' % required,
                "only_copy": only_copy})
        with self.operation("session-delete"):
            files = sum(1 for f in target.rglob("*") if f.is_file())
            shutil.rmtree(target)
            shutil.rmtree(THUMBS_DIR / session, ignore_errors=True)
            self.tick("warn", "DELETED %s from share · %d files%s"
                      % (session, files,
                         " · was the only copy" if only_copy else ""))
        self.hub.publish("sessions", self.list_sessions())
        return {"session": session, "files_removed": files,
                "only_copy": only_copy}

    def api_set_ntp(self, body: dict) -> dict:
        server = str(body.get("server", "")).strip()
        if not server or any(c.isspace() for c in server):
            raise ApiError(400, {"error": "server required (no whitespace)"})
        expected = int(self.config["expected_pis"])
        with self.operation("set-ntp"):
            replies = self.rig.broadcast({"msg": "SET_NTP", "server": server},
                                         8.0, expected=expected)
            acks = sum(1 for r in replies if r.msg_type == "NTP_SET")
            self.tick("ok" if acks == expected else "warn",
                      "SET_NTP %s · %d/%d ack" % (server, acks, expected))
        return {"server": server, "acks": acks, "expected": expected,
                "results": reply_summary(replies)}

    def api_set_smb(self, body: dict) -> dict:
        server = str(body.get("server", "")).strip()
        share = str(body.get("share", "")).strip()
        username = str(body.get("username", ""))
        password = str(body.get("password", ""))
        domain = str(body.get("domain") or "WORKGROUP")
        if not server or not share or not username:
            raise ApiError(400, {"error": "server/share/username required"})
        expected = int(self.config["expected_pis"])
        with self.operation("set-smb"):
            # password goes on the wire only — never persisted, never returned.
            replies = self.rig.broadcast({
                "msg": "SET_SMB", "server": server, "share": share,
                "username": username, "password": password, "domain": domain,
                "credentials_ref": "default",
            }, 8.0, expected=expected)
            acks = sum(1 for r in replies if r.msg_type == "SMB_SET")
            reachable = sum(1 for r in replies if r.msg_type == "SMB_SET"
                            and r.payload.get("reachable"))
            self.tick("ok" if reachable == expected else "warn",
                      "SET_SMB //%s/%s · %d/%d reachable"
                      % (server, share, reachable, expected))
        return {"server": server, "share": share, "acks": acks,
                "reachable": reachable, "expected": expected,
                "results": reply_summary(replies)}

    def _stream_script(self, cmd: List[str], env: dict, opname: str,
                       deadline_s: float) -> Tuple[int, int]:
        """Run a provisioning script, streaming each stdout line (ANSI stripped)
        as an op event. stdin=DEVNULL: an ssh/sudo password prompt must fail
        fast, not block on our tty holding the op lock forever. The deadline
        backstops a hung remote (a Pi dying mid-transfer keeps its TCP session
        in retransmission for many minutes). Returns (lines, exit_code)."""
        # errors="replace": scripts relay remote bytes verbatim (journalctl via
        # ssh in diagnose-pis.sh) which are not reliably UTF-8 — a strict
        # decoder would crash the stream mid-run and orphan the child.
        proc = subprocess.Popen(
            cmd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
        assert proc.stdout is not None
        lines = 0
        exit_code = 0
        deadline = time.monotonic() + deadline_s
        killer = threading.Timer(deadline_s, proc.kill)
        killer.daemon = True
        killer.start()
        try:
            for line in proc.stdout:
                self.hub.publish("op", {"op": opname,
                                        "line": ANSI_RE.sub("", line.rstrip())})
                lines += 1
            exit_code = proc.wait()
        except BaseException:
            proc.kill()      # never leave the child running with no deadline
            proc.wait()
            raise
        finally:
            killer.cancel()
        if time.monotonic() >= deadline:
            self.hub.publish("op", {
                "op": opname,
                "line": "ABORTED — exceeded %ds deadline" % int(deadline_s)})
            exit_code = exit_code or 124
        return lines, exit_code

    def api_update_fleet(self, body: dict) -> dict:
        with self.operation("update"):
            lines = 0
            exit_code = 0
            if self.sim:
                for node in getattr(self.rig, "nodes", []):
                    self.hub.publish("op", {"op": "update",
                                            "line": "  %-15s updated" % node.pi})
                    lines += 1
                    time.sleep(0.03)
            else:
                if not UPDATE_SCRIPT.is_file():
                    raise ApiError(500, {"error": "missing %s" % UPDATE_SCRIPT})
                env = dict(os.environ)
                env["SSH_USER"] = str(self.config.get("ssh_user") or "pi")
                lines, exit_code = self._stream_script(
                    ["bash", str(UPDATE_SCRIPT)], env, "update",
                    UPDATE_DEADLINE_S)
            fleet, _ = self.sweep()
            census = dict(Counter(str(p.get("version", "?"))
                                  for p in fleet["pis"]))
            self.hub.publish("op", {"op": "update", "status": "census",
                                    "census": census})
            self.tick("ok" if exit_code == 0 else "fail",
                      "UPDATE FLEET · exit %d · versions %s"
                      % (exit_code, " ".join("%s×%d" % (v, c)
                                             for v, c in sorted(census.items()))))
        return {"ok": exit_code == 0, "exit_code": exit_code,
                "lines": lines, "census": census}

    def api_power(self, body: dict) -> dict:
        """REBOOT/HALT the whole fleet. HALT is close to irreversible in the
        field — a Pi 3B has no soft power-on — so both require their confirm
        word, mirroring the CLI's y/N prompt."""
        action = str(body.get("action", ""))
        if action not in ("reboot", "halt"):
            raise ApiError(400, {"error": 'action must be "reboot" or "halt"'})
        if body.get("confirm") != action.upper():
            raise ApiError(400, {"error": 'confirm must be "%s"' % action.upper()})
        verb = action.upper()                      # REBOOT / HALT
        ack_type = "REBOOTING" if action == "reboot" else "HALTING"
        expected = int(self.config["expected_pis"])
        with self.operation("power"):
            replies = self.rig.broadcast({"msg": verb}, POWER_TIMEOUT_S,
                                         expected=expected)
            acks = sum(1 for r in replies if r.msg_type == ack_type)
            note = ("fleet restarting — back in ~60 s" if action == "reboot"
                    else "fleet powering off — PHYSICAL power-cycle to return")
            self.tick("warn", "%s · %d/%d ack · %s" % (verb, acks, expected, note))
        return {"action": action, "acks": acks, "expected": expected,
                "note": note, "results": reply_summary(replies)}

    def api_diagnose(self, body: dict) -> dict:
        """Stream provision/diagnose-pis.sh or diagnose-smb.sh into the GUI.
        diagnose-smb.sh must run as root, so it goes through `sudo -n`; without
        passwordless sudo it fails fast and the streamed output says so."""
        target = str(body.get("target", ""))
        if target not in ("pis", "smb"):
            raise ApiError(400, {"error": 'target must be "pis" or "smb"'})
        script = DIAG_PIS_SCRIPT if target == "pis" else DIAG_SMB_SCRIPT
        with self.operation("diagnose"):
            if self.sim:
                lines, exit_code = self._sim_diagnose(target)
            else:
                if not script.is_file():
                    raise ApiError(500, {"error": "missing %s" % script})
                env = dict(os.environ)
                if target == "pis":
                    env["SSH_USER"] = str(self.config.get("ssh_user") or "pi")
                    cmd = ["bash", str(script)]
                else:
                    cmd = ["sudo", "-n", "bash", str(script)]
                lines, exit_code = self._stream_script(cmd, env, "diagnose",
                                                       DIAG_DEADLINE_S)
                if target == "smb" and exit_code != 0 and lines <= 2:
                    self.hub.publish("op", {
                        "op": "diagnose",
                        "line": "hint: diagnose-smb.sh needs root — allow "
                                "passwordless sudo for it, or run manually: "
                                "sudo ./provision/diagnose-smb.sh"})
            self.tick("ok" if exit_code == 0 else "warn",
                      "DIAGNOSE %s · exit %d" % (target.upper(), exit_code))
        return {"target": target, "ok": exit_code == 0,
                "exit_code": exit_code, "lines": lines}

    def _sim_diagnose(self, target: str) -> Tuple[int, int]:
        """Plausible diagnose output for the fake fleet, tied to real sim faults."""
        nodes = getattr(self.rig, "nodes", [])
        out: List[str] = []
        code = 0
        if target == "pis":
            dead = [n for n in nodes if n.dead]
            out.append("[diagnose] live on network: %d" % len(nodes))
            out.append("[diagnose] pinging (timeout 10s)…")
            out.append("[diagnose] SSH-reachable: %d   answered ping: %d"
                       % (len(nodes), len(nodes) - len(dead)))
            if dead:
                code = 1
                out.append("[diagnose] %d Pi(s) reachable via SSH but SILENT "
                           "on ping — inspecting:" % len(dead))
                for n in dead:
                    out += ["───── 192.168.50.%d  (%s) ─────" % (100 + n.index, n.pi),
                            "  service:        inactive",
                            "  udp/9999:        NOT LISTENING",
                            "  daemon version:  MAC-based (new)",
                            "  recent logs:",
                            "    (picam_node exited 1 — see journalctl on the Pi)"]
                out.append("[diagnose] hints: NOT LISTENING -> 'sudo systemctl "
                           "restart picam_node' on that Pi")
            else:
                out.append("[diagnose] ✓ every SSH-reachable Pi answered ping")
        else:
            for sec, res in [("Samba service", ["PASS  smbd active",
                                                "PASS  listening on :445"]),
                             ("Users", ["PASS  system user 'scanner' exists",
                                        "PASS  Samba user 'scanner' exists"]),
                             ("Share config", ["PASS  [scans] path = /srv/scans",
                                               "PASS  writable = yes"]),
                             ("Filesystem", ["PASS  owned scanner:scanner",
                                             "PASS  write-as-scanner ok"])]:
                out.append("== %s ==" % sec)
                out += ["  " + r for r in res]
            out.append("0 FAIL · 0 WARN — SMB layer looks healthy")
        for line in out:
            self.hub.publish("op", {"op": "diagnose", "line": line})
            time.sleep(0.02)
        return len(out), code

    def api_preflight(self, body: dict) -> dict:
        motion_safe = bool(body.get("motion_safe", True))
        cfg = self.config
        expected = int(cfg["expected_pis"])
        go_max = float(cfg["go_max_offset_ms"])
        steps: List[dict] = []

        def emit(step: str, status: str, detail: str = "") -> None:
            self.hub.publish("op", {"op": "preflight", "step": step,
                                    "status": status, "detail": detail})
            if status != "run":
                steps.append({"step": step, "status": status, "detail": detail})

        with self.operation("preflight"):
            # 1. ping
            emit("ping", "run")
            fleet, verdict = self.sweep()
            pis = fleet["pis"]
            if len(pis) == expected:
                emit("ping", "pass", "%d/%d replied" % (len(pis), expected))
            else:
                silent = verdict["counts"].get("silent") or []
                emit("ping", "fail", "%d/%d replied · silent: %s"
                     % (len(pis), expected, " ".join(silent) or "?"))

            # 2. ntp
            emit("ntp", "run")
            offs = [abs(float((p.get("ntp") or {}).get(
                "offset_ms", p.get("clock_offset_ms", 0)))) for p in pis]
            unsynced = [p["pi"] for p in pis
                        if not (p.get("ntp") or {}).get("synced")]
            worst = max(offs) if offs else 0.0
            if unsynced or worst > 5.0:
                emit("ntp", "fail", "unsynced: %s · max |offset| %.1f ms"
                     % (" ".join(unsynced) or "—", worst))
            elif worst > go_max:
                emit("ntp", "warn", "max |offset| %.1f ms (go limit %.1f)"
                     % (worst, go_max))
            else:
                emit("ntp", "pass", "max |offset| %.1f ms" % worst)

            # 3. smb
            emit("smb", "run")
            smb_bad = [p["pi"] for p in pis
                       if not (p.get("smb") or {}).get("reachable")]
            if smb_bad:
                emit("smb", "fail", "unreachable: " + " ".join(smb_bad))
            else:
                emit("smb", "pass", "%d/%d reachable" % (len(pis), len(pis)))

            # 4. disk (Pis + laptop share)
            emit("disk", "run")
            frees = [int(p.get("free_mb", 0)) for p in pis]
            laptop = self._laptop_reasons()
            low = [p["pi"] for p in pis if int(p.get("free_mb", 0)) < 100]
            if low or laptop:
                emit("disk", "fail", " · ".join(
                    (["low: " + " ".join(low)] if low else []) + laptop))
            elif frees and min(frees) < 1024:
                emit("disk", "warn", "min Pi free %d MB" % min(frees))
            else:
                emit("disk", "pass", "min Pi free %d MB · laptop ok"
                     % (min(frees) if frees else 0))

            # 5. autoconfigure
            emit("autoconfigure", "run")
            try:
                auto = self._do_autoconfigure(2.0, motion_safe)
                detail = "exp %dµs gain %.2f · %d/%d ack" % (
                    auto["applied"]["exposure_us"],
                    auto["applied"]["analogue_gain"], auto["acks"], expected)
                emit("autoconfigure", "warn" if auto["clamped"] else "pass",
                     detail + (" · clamped" if auto["clamped"] else ""))
            except ApiError as e:
                emit("autoconfigure", "fail", str(e.body.get("error", e)))

            ok = all(s["status"] != "fail" for s in steps)
            self.tick("ok" if ok else "fail",
                      "PREFLIGHT %s" % ("PASS" if ok else "FAIL"))
        return {"steps": steps, "ok": ok}

    def api_config(self, body: dict) -> dict:
        """Persist safe config keys (subjects list, thresholds, ssh_user…)."""
        casts = {
            "expected_pis": int, "ssh_user": str, "go_max_offset_ms": float,
            "spread_budget_ms": float, "motion_cap_us": int, "leadtime_s": float,
            "verify_min_bytes": int, "ping_interval_s": float,
        }
        # Validate everything first, then apply — a bad value must not leave
        # earlier keys half-mutated.
        staged: Dict[str, Any] = {}
        for k, cast in casts.items():
            if k in body:
                try:
                    staged[k] = cast(body[k])
                except (TypeError, ValueError):
                    raise ApiError(400, {"error": "bad value for %s" % k})
        if "subjects" in body:
            subj = body["subjects"]
            if not isinstance(subj, list):
                raise ApiError(400, {"error": "subjects must be a list"})
            staged["subjects"] = [str(s) for s in subj]
        with self._cfg_lock:
            self.config.update(staged)
            self._save_config()
        return {"config": self.config}


# ─── HTTP server ────────────────────────────────────────────────────────────
POST_ROUTES = {
    "/api/ping": "api_ping",
    "/api/configure": "api_configure",
    "/api/preset-save": "api_preset_save",
    "/api/preset-apply": "api_preset_apply",
    "/api/preset-delete": "api_preset_delete",
    "/api/autoconfigure": "api_autoconfigure",
    "/api/take": "api_take",
    "/api/upload-retry": "api_upload_retry",
    "/api/clear": "api_clear",
    "/api/clear-all": "api_clear_all",
    "/api/set-ntp": "api_set_ntp",
    "/api/set-smb": "api_set_smb",
    "/api/update-fleet": "api_update_fleet",
    "/api/preflight": "api_preflight",
    "/api/config": "api_config",
    "/api/power": "api_power",
    "/api/diagnose": "api_diagnose",
    "/api/session-delete": "api_session_delete",
}

FALLBACK_INDEX = (b"<!doctype html><meta charset=utf-8>"
                  b"<title>32PiScanner</title><body style='background:#1B1D1F;"
                  b"color:#D7DBDD;font-family:monospace;padding:2rem'>"
                  b"gui_web/ not found &mdash; frontend not built yet. "
                  b"API is live at /api/state.")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "picam-gui/0.1"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # quiet — the ticker is the log

    # ── plumbing ──
    def _send_json(self, obj: Any, status: int = 200) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 1024 * 1024:
            raise ApiError(413, {"error": "body too large"})
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, {"error": "invalid JSON body"})
        if not isinstance(body, dict):
            raise ApiError(400, {"error": "JSON object expected"})
        return body

    # ── GET ──
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/state":
                self._send_json(self.app.state())
            elif path == "/api/events":
                self._serve_events()
            elif path == "/api/sessions":
                self._send_json(self.app.list_sessions())
            elif path.startswith("/api/session/"):
                self._send_json(
                    self.app.session_detail(path[len("/api/session/"):]))
            elif path.startswith("/api/thumb/"):
                self._serve_capture(path[len("/api/thumb/"):], thumb=True)
            elif path.startswith("/api/image/"):
                self._serve_capture(path[len("/api/image/"):], thumb=False)
            else:
                self._serve_static(path)
        except ApiError as e:
            self._send_json(e.body, e.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # pragma: no cover
            try:
                self._send_json({"error": str(e)}, 500)
            except OSError:
                pass

    # ── POST ──
    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        method = POST_ROUTES.get(path)
        if method is None:
            self._send_json({"error": "not found"}, 404)
            return
        try:
            body = self._read_body()
            result = getattr(self.app, method)(body)
            self._send_json(result)
        except ApiError as e:
            self._send_json(e.body, e.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # pragma: no cover
            try:
                self._send_json({"error": str(e)}, 500)
            except OSError:
                pass

    # ── SSE ──
    def _serve_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = self.app.hub.register()
        try:
            self._sse("snapshot", self.app.state())
            while True:
                try:
                    event, data = q.get(timeout=SSE_HEARTBEAT_S)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if event == "__stale__":   # hub dropped us — force a reconnect
                    break
                self._sse(event, data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.hub.unregister(q)
            self.close_connection = True

    def _sse(self, event: str, data: Any) -> None:
        chunk = "event: %s\ndata: %s\n\n" % (event, json.dumps(data))
        self.wfile.write(chunk.encode("utf-8"))
        self.wfile.flush()

    # ── capture files (thumb + full image) ──
    def _capture_path(self, rest: str) -> Tuple[Path, str]:
        parts = rest.split("/")
        if len(parts) != 2:
            raise ApiError(404, {"error": "not found"})
        session, fname = parts
        if not fname.endswith(".jpg"):
            raise ApiError(404, {"error": "not found"})
        pi = fname[:-4]
        if not NAME_RE.match(session) or not NAME_RE.match(pi):
            raise ApiError(400, {"error": "bad name"})
        root = self.app.scans_root.resolve()
        path = (root / session / ("%s.jpg" % pi)).resolve()
        if not path.is_relative_to(root):
            raise ApiError(400, {"error": "bad path"})
        if not path.is_file():
            raise ApiError(404, {"error": "not found"})
        return path, pi

    def _serve_capture(self, rest: str, thumb: bool) -> None:
        path, pi = self._capture_path(rest)
        if not thumb:
            self._send_bytes(path.read_bytes(), "image/jpeg")
            return
        session = rest.split("/")[0]
        if Image is not None:
            try:
                mt = int(path.stat().st_mtime * 1000)
                # Nested per-session dir (both names are NAME_RE-validated), so
                # underscore-bearing names can never collide in one flat key.
                cached = THUMBS_DIR / session / ("%s.%d.jpg" % (pi, mt))
                if not cached.exists():
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    im = Image.open(path)
                    im.thumbnail((360, 360))
                    # temp+rename so a concurrent request never reads a
                    # half-written thumbnail.
                    tmp = cached.with_suffix(".tmp-%d" % threading.get_ident())
                    im.convert("RGB").save(tmp, "JPEG", quality=85)
                    os.replace(tmp, cached)
                self._send_bytes(cached.read_bytes(), "image/jpeg")
                return
            except OSError:
                pass  # undecodable (sim placeholder bytes) → fall through
        size = path.stat().st_size
        if size >= 50 * 1024:            # real capture — browser can decode it
            self._send_bytes(path.read_bytes(), "image/jpeg")
        else:                            # sim placeholder — serve an SVG tile
            self._send_bytes(placeholder_svg(pi, size).encode("utf-8"),
                             "image/svg+xml")

    # ── static files ──
    def _serve_static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        root = WEB_ROOT.resolve()
        target = (root / rel).resolve()
        if not (target == root or target.is_relative_to(root)):
            raise ApiError(404, {"error": "not found"})
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            if path == "/":
                self._send_bytes(FALLBACK_INDEX, "text/html; charset=utf-8")
                return
            raise ApiError(404, {"error": "not found"})
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        self._send_bytes(target.read_bytes(), ctype)


# ─── main ───────────────────────────────────────────────────────────────────
def parse_faults(spec: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        kind, _, n = part.partition(":")
        if kind not in ("dead", "ntp", "smb", "stale"):
            raise SystemExit("unknown sim fault: %r" % kind)
        try:
            out[kind] = int(n or 1)
        except ValueError:
            raise SystemExit("bad sim fault count: %r" % part)
    return out


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(description="32PiScanner control GUI")
    p.add_argument("--port", type=int, default=8321)
    p.add_argument("--scans-root", default=None,
                   help="where uploaded sessions land (the SMB share as seen "
                        "from this laptop)")
    p.add_argument("--sim", type=int, default=0, metavar="N",
                   help="replace the UDP transport with N fake in-process Pis")
    p.add_argument("--sim-faults", default="",
                   help="e.g. dead:1,ntp:1,smb:1,stale:1")
    args = p.parse_args(argv)

    app = App(port=args.port, scans_root=args.scans_root,
              sim_n=args.sim, sim_faults=parse_faults(args.sim_faults))

    httpd = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    httpd.app = app  # type: ignore[attr-defined]
    mode = "SIM ×%d" % args.sim if app.sim else "REAL RIG"
    print("32PiScanner GUI · %s · http://0.0.0.0:%d · scans → %s"
          % (mode, args.port, app.scans_root))
    if Image is None:
        print("(Pillow not installed — thumbnails fall back to original/SVG)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
