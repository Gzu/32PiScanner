#!/usr/bin/env python3
"""32PiScanner CLI — drive the rig from any laptop on the same subnet.

Speaks the protocol in docs/protocol.md. Use this to bring up + smoke-test the
rig before the Android app exists, and as a reference for the protocol.

Examples:
    ./cli.py ping
    ./cli.py set-ntp --server 192.168.1.10
    ./cli.py set-smb --server rc-box --share scans \\
                     --username scanner --password hunter2
    ./cli.py configure --exposure-us 2000 --gain 4.0
    ./cli.py capture --session 2026-05-26_test01 --leadtime 2.0
    ./cli.py upload  --session 2026-05-26_test01      # uses stored SET_SMB
    ./cli.py session --session 2026-05-26_test01 --dest smb://rc-box/scans/
                                    # configure + capture + upload in one go
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Optional

PROTOCOL_VERSION = 1
PORT = 9999
BROADCAST = "255.255.255.255"
TRIPLE_SEND_GAP_S = 0.010


@dataclass
class Reply:
    pi: str
    msg_type: str
    payload: dict


def _open_socket() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", 0))
    return s


def _send_triple(sock: socket.socket, payload: dict) -> str:
    """Send the payload 3× spaced TRIPLE_SEND_GAP_S apart. Returns the message id."""
    payload.setdefault("v", PROTOCOL_VERSION)
    payload.setdefault("id", str(uuid.uuid4()))
    data = json.dumps(payload).encode("utf-8")
    for i in range(3):
        sock.sendto(data, (BROADCAST, PORT))
        if i < 2:
            time.sleep(TRIPLE_SEND_GAP_S)
    return payload["id"]


def _collect_replies(sock: socket.socket, msg_id: str, timeout_s: float,
                     expected: Optional[int] = None) -> list[Reply]:
    """Listen for replies with matching id; stop when expected reached or timeout."""
    replies: dict[str, Reply] = {}
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock.settimeout(remaining)
        try:
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            break
        try:
            msg = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if msg.get("v") != PROTOCOL_VERSION or msg.get("id") != msg_id:
            continue
        pi = msg.get("pi", "?")
        # Keep the last reply per pi (UPLOADED supersedes CAPTURED for same id
        # only if pis reuse the id, which they don't — so first-wins effectively).
        replies[pi] = Reply(pi=pi, msg_type=msg.get("msg", "?"), payload=msg)
        if expected is not None and len(replies) >= expected:
            break
    return sorted(replies.values(), key=lambda r: r.pi)


def _format_pong(p: dict) -> str:
    """Render a PONG payload as a single readable line with NTP + SMB status."""
    ntp = p.get("ntp", {})
    smb = p.get("smb", {})

    ntp_mark = "✓" if ntp.get("synced") else "✗"
    ntp_str = (
        f"NTP {ntp_mark} {ntp.get('server', '?'):16} "
        f"off={ntp.get('offset_ms', -1):+.1f}ms str={ntp.get('stratum', 0)}"
    )

    smb_mark = "✓" if smb.get("reachable") else "✗"
    smb_server = smb.get("server") or "—"
    smb_str = f"SMB {smb_mark} //{smb_server}/{smb.get('share', '')}"
    if not smb.get("reachable") and smb.get("last_error"):
        smb_str += f" ({smb['last_error'][:40]})"

    cam_str = "cam✓" if p.get("camera_ok") else "cam✗"
    return (
        f"{cam_str}  free={p.get('free_mb', 0)}MB  "
        f"up={p.get('uptime_s', 0)}s  v{p.get('version', '?')}\n"
        f"             {ntp_str}\n"
        f"             {smb_str}"
    )


def _print_replies(replies: list[Reply], expected: Optional[int] = None) -> None:
    for r in replies:
        if r.msg_type == "ERROR":
            print(f"  ✗ {r.pi:10}  ERROR {r.payload.get('reason')}: {r.payload.get('detail', '')}")
        elif r.msg_type == "PONG":
            print(f"  ✓ {r.pi:10}  PONG  {_format_pong(r.payload)}")
        else:
            extras = {k: v for k, v in r.payload.items()
                      if k not in ("v", "id", "msg", "pi")}
            extra_str = " ".join(f"{k}={v}" for k, v in extras.items())
            print(f"  ✓ {r.pi:10}  {r.msg_type:10}  {extra_str}")
    print(f"\n{len(replies)} reply/replies"
          + (f" / {expected} expected" if expected else ""))


# ─── commands ───────────────────────────────────────────────────────────────
def cmd_ping(args) -> int:
    sock = _open_socket()
    msg_id = _send_triple(sock, {"msg": "PING"})
    replies = _collect_replies(sock, msg_id, timeout_s=args.timeout, expected=args.expected)
    _print_replies(replies, expected=args.expected)
    return 0 if (args.expected is None or len(replies) == args.expected) else 1


def cmd_configure(args) -> int:
    sock = _open_socket()
    msg_id = _send_triple(sock, {
        "msg": "CONFIGURE",
        "exposure_us": args.exposure_us,
        "analogue_gain": args.gain,
        "awb_gains": [args.awb_r, args.awb_b],
        "resolution": list(args.resolution),
        "jpeg_quality": args.quality,
    })
    replies = _collect_replies(sock, msg_id, timeout_s=args.timeout, expected=args.expected)
    _print_replies(replies, expected=args.expected)
    return 0


def cmd_capture(args) -> int:
    sock = _open_socket()
    trigger_at = time.time() + args.leadtime
    print(f"trigger_at_unix = {trigger_at:.3f}  ({time.strftime('%H:%M:%S', time.localtime(trigger_at))}.{int((trigger_at%1)*1000):03d})")
    msg_id = _send_triple(sock, {
        "msg": "CAPTURE",
        "session_id": args.session,
        "trigger_at_unix": trigger_at,
    })
    # Replies come ~50ms after trigger, so wait at least leadtime + 1s
    timeout = args.leadtime + 2.0
    replies = _collect_replies(sock, msg_id, timeout_s=timeout, expected=args.expected)
    _print_replies(replies, expected=args.expected)
    if replies:
        actuals = [r.payload.get("actual_at_unix") for r in replies
                   if r.msg_type == "CAPTURED" and r.payload.get("actual_at_unix")]
        if actuals:
            spread_ms = (max(actuals) - min(actuals)) * 1000
            print(f"\ntrigger spread across rig: {spread_ms:.2f} ms")
    return 0


def cmd_upload(args) -> int:
    sock = _open_socket()
    payload: dict = {"msg": "UPLOAD", "session_id": args.session}
    # dest and creds are now optional — daemon falls back to stored SET_SMB.
    if args.dest:
        payload["dest"] = args.dest
    if args.creds:
        payload["credentials_ref"] = args.creds
    msg_id = _send_triple(sock, payload)
    replies = _collect_replies(sock, msg_id, timeout_s=args.timeout, expected=args.expected)
    _print_replies(replies, expected=args.expected)
    return 0


def cmd_set_ntp(args) -> int:
    sock = _open_socket()
    msg_id = _send_triple(sock, {"msg": "SET_NTP", "server": args.server})
    replies = _collect_replies(sock, msg_id, timeout_s=args.timeout, expected=args.expected)
    _print_replies(replies, expected=args.expected)
    print("\nVerify with `cli.py ping` after ~30s; ntp.synced should be true.")
    return 0 if all(r.msg_type == "NTP_SET" for r in replies) else 1


def cmd_set_smb(args) -> int:
    sock = _open_socket()
    msg_id = _send_triple(sock, {
        "msg": "SET_SMB",
        "server": args.server,
        "share": args.share,
        "username": args.username,
        "password": args.password,
        "domain": args.domain,
        "credentials_ref": args.creds_ref,
    })
    replies = _collect_replies(sock, msg_id, timeout_s=args.timeout, expected=args.expected)
    _print_replies(replies, expected=args.expected)
    # Summary: how many Pis can actually reach the share?
    reachable = sum(1 for r in replies if r.msg_type == "SMB_SET" and r.payload.get("reachable"))
    total = len([r for r in replies if r.msg_type == "SMB_SET"])
    if total:
        print(f"\nReachability: {reachable}/{total} Pis can reach //{args.server}/{args.share} on port 445")
    return 0 if reachable == total and total > 0 else 1


def cmd_autoconfigure(args) -> int:
    """Meter every camera on auto, average the results, then CONFIGURE the whole
    rig to that average — scene-appropriate but fixed + identical across all Pis."""
    sock = _open_socket()
    print(f"metering all cameras on auto ({args.settle:.1f}s settle)…")
    meter_id = _send_triple(sock, {"msg": "METER", "settle_ms": int(args.settle * 1000)})
    replies = _collect_replies(sock, meter_id, timeout_s=args.settle + 4.0, expected=args.expected)

    metered = [r for r in replies if r.msg_type == "METERED"]
    for r in (r for r in replies if r.msg_type == "ERROR"):
        print(f"  ✗ {r.pi:10} ERROR {r.payload.get('reason')}: {r.payload.get('detail', '')}")
    if not metered:
        print("no METERED replies — aborting")
        return 1

    n = len(metered)
    exps = [r.payload["exposure_us"] for r in metered]
    gains = [r.payload["analogue_gain"] for r in metered]
    rs = [r.payload["awb_gains"][0] for r in metered]
    bs = [r.payload["awb_gains"][1] for r in metered]
    avg_exp = sum(exps) / n
    avg_gain, avg_r, avg_b = sum(gains) / n, sum(rs) / n, sum(bs) / n

    print(f"\nmetered {n} camera(s):")
    print(f"  exposure_us  avg={avg_exp:7.0f}  range={min(exps)}–{max(exps)}")
    print(f"  gain         avg={avg_gain:7.2f}  range={min(gains):.2f}–{max(gains):.2f}")
    print(f"  awb [r,b]    avg=[{avg_r:.2f}, {avg_b:.2f}]")

    if avg_exp > 2000:
        print(f"\n  ⚠ averaged exposure {avg_exp:.0f}µs > 2000µs — long for moving subjects "
              f"(rolling-shutter blur risk).\n    Add light, or pass --max-exposure-us 2000 "
              f"to cap it (raise gain / brightness to compensate).")
    if args.max_exposure_us and avg_exp > args.max_exposure_us:
        print(f"  clamping exposure {avg_exp:.0f} → {args.max_exposure_us}µs")
        avg_exp = args.max_exposure_us

    print("\napplying averaged settings to the rig…")
    cfg_id = _send_triple(sock, {
        "msg": "CONFIGURE",
        "exposure_us": int(round(avg_exp)),
        "analogue_gain": round(avg_gain, 2),
        "awb_gains": [round(avg_r, 2), round(avg_b, 2)],
        "resolution": list(args.resolution),
        "jpeg_quality": args.quality,
    })
    cfg = _collect_replies(sock, cfg_id, timeout_s=args.timeout, expected=args.expected)
    _print_replies(cfg, expected=args.expected)
    return 0


def cmd_clear(args) -> int:
    """Delete captured images on the Pis (one session, or all with --all)."""
    if args.all and not args.yes:
        try:
            resp = input("Delete ALL captured sessions on every Pi? [y/N] ")
        except EOFError:
            resp = ""
        if resp.strip().lower() != "y":
            print("aborted")
            return 0
    sock = _open_socket()
    payload: dict = {"msg": "CLEAR"}
    if args.session:
        payload["session_id"] = args.session
    # --all → no session_id, the daemon clears every session.
    msg_id = _send_triple(sock, payload)
    replies = _collect_replies(sock, msg_id, timeout_s=args.timeout, expected=args.expected)
    _print_replies(replies, expected=args.expected)
    return 0


def cmd_session(args) -> int:
    """configure + capture + upload, end-to-end."""
    print("─── 1/3 CONFIGURE ───")
    rc = cmd_configure(args)
    if rc != 0:
        return rc
    print("\n─── 2/3 CAPTURE ───")
    rc = cmd_capture(args)
    if rc != 0:
        return rc
    print("\n─── 3/3 UPLOAD ───")
    return cmd_upload(args)


# ─── argparse ───────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="32PiScanner CLI")
    p.add_argument("--expected", type=int, default=None,
                   help="expected number of Pis (errors if mismatch)")
    p.add_argument("--timeout", type=float, default=5.0,
                   help="seconds to wait for replies")

    sub = p.add_subparsers(dest="cmd", required=True)

    sp_ping = sub.add_parser("ping", help="discover live Pis")
    sp_ping.set_defaults(func=cmd_ping)

    sp_cfg = sub.add_parser("configure", help="lock exposure/gain/wb")
    sp_cfg.add_argument("--exposure-us", type=int, default=2000)
    sp_cfg.add_argument("--gain", type=float, default=4.0)
    sp_cfg.add_argument("--awb-r", type=float, default=1.8)
    sp_cfg.add_argument("--awb-b", type=float, default=1.6)
    sp_cfg.add_argument("--resolution", type=int, nargs=2, default=[3280, 2464],
                        metavar=("W", "H"))
    sp_cfg.add_argument("--quality", type=int, default=95)
    sp_cfg.set_defaults(func=cmd_configure)

    sp_cap = sub.add_parser("capture", help="time-triggered shot")
    sp_cap.add_argument("--session", required=True, help="session id (folder name)")
    sp_cap.add_argument("--leadtime", type=float, default=2.0,
                        help="seconds in the future to trigger")
    sp_cap.set_defaults(func=cmd_capture)

    sp_up = sub.add_parser("upload", help="push captured files to dest")
    sp_up.add_argument("--session", required=True)
    sp_up.add_argument("--dest", default=None,
                       help="smb://host/share/ ; omit to use stored SET_SMB default")
    sp_up.add_argument("--creds", default=None,
                       help="credentials_ref name; omit to use stored default")
    sp_up.set_defaults(func=cmd_upload)

    sp_sn = sub.add_parser("set-ntp", help="repoint chrony at a new NTP server")
    sp_sn.add_argument("--server", required=True, help="IP or hostname of NTP server")
    sp_sn.set_defaults(func=cmd_set_ntp)

    sp_ss = sub.add_parser("set-smb", help="set default SMB destination + credentials")
    sp_ss.add_argument("--server", required=True, help="SMB server (IP or hostname, no scheme)")
    sp_ss.add_argument("--share", required=True, help="share name, e.g. 'scans'")
    sp_ss.add_argument("--username", required=True)
    sp_ss.add_argument("--password", required=True)
    sp_ss.add_argument("--domain", default="WORKGROUP")
    sp_ss.add_argument("--creds-ref", default="default",
                       help="credentials file name under /etc/picam_node/credentials/")
    sp_ss.set_defaults(func=cmd_set_smb)

    sp_auto = sub.add_parser("autoconfigure",
                             help="meter all cameras on auto, average, then CONFIGURE the rig")
    sp_auto.add_argument("--settle", type=float, default=2.0,
                         help="seconds to let each camera's AE/AWB converge")
    sp_auto.add_argument("--max-exposure-us", type=int, default=None,
                         help="cap the averaged exposure (e.g. 2000 to keep motion frozen)")
    sp_auto.add_argument("--resolution", type=int, nargs=2, default=[3280, 2464],
                         metavar=("W", "H"))
    sp_auto.add_argument("--quality", type=int, default=95)
    sp_auto.set_defaults(func=cmd_autoconfigure)

    sp_clr = sub.add_parser("clear", help="delete captured images on the Pis")
    g_clr = sp_clr.add_mutually_exclusive_group(required=True)
    g_clr.add_argument("--session", help="session id to delete")
    g_clr.add_argument("--all", action="store_true",
                       help="delete ALL sessions on every Pi")
    sp_clr.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt for --all")
    sp_clr.set_defaults(func=cmd_clear)

    sp_ses = sub.add_parser("session", help="configure + capture + upload")
    sp_ses.add_argument("--session", required=True)
    sp_ses.add_argument("--dest", required=True)
    sp_ses.add_argument("--creds", default="default")
    sp_ses.add_argument("--exposure-us", type=int, default=2000)
    sp_ses.add_argument("--gain", type=float, default=4.0)
    sp_ses.add_argument("--awb-r", type=float, default=1.8)
    sp_ses.add_argument("--awb-b", type=float, default=1.6)
    sp_ses.add_argument("--resolution", type=int, nargs=2, default=[3280, 2464])
    sp_ses.add_argument("--quality", type=int, default=95)
    sp_ses.add_argument("--leadtime", type=float, default=2.0)
    sp_ses.set_defaults(func=cmd_session)

    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
