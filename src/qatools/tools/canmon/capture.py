"""Capture CAN / CAN FD frames from SocketCAN interfaces to CSV, with hardware timestamps when available.

Usage:
  qa canmon capture can0 can1 -o capture.csv [--seconds 60]

Columns: t_hw, t_sw, iface, can_id, ext, fd, brs, esi, err, len, data
  t_hw  hardware (adapter) timestamp in seconds, empty if the driver did not provide one
  t_sw  kernel receive time in seconds (CLOCK_REALTIME)
Stop with Ctrl+C. Needs no extra Python packages.
"""
import argparse
import csv
import select
import socket
import sys
import time

from .sockcan import FRAME, TS, open_socket, parse_frame, parse_ts


def main(argv=None):
    ap = argparse.ArgumentParser(prog="qa canmon capture", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ifaces", nargs="+")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = until Ctrl+C)")
    a = ap.parse_args(argv)

    socks = {open_socket(i): i for i in a.ifaces}
    cols = ["t_hw", "t_sw", "iface", "can_id", "ext", "fd", "brs", "esi", "err", "len", "data"]
    counts = {i: [0, 0] for i in a.ifaces}   # frames, frames with hardware timestamp
    start = time.monotonic()
    with open(a.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        try:
            while not a.seconds or time.monotonic() - start < a.seconds:
                ready, _, _ = select.select(list(socks), [], [], 0.5)
                for s in ready:
                    raw, anc, _, _ = s.recvmsg(FRAME.size, socket.CMSG_SPACE(TS.size))
                    hw, sw = parse_ts(anc)
                    fr = parse_frame(raw)
                    name = socks[s]
                    counts[name][0] += 1
                    counts[name][1] += hw is not None
                    w.writerow([f"{hw:.9f}" if hw is not None else "", f"{sw:.9f}", name,
                                f"0x{fr['can_id']:X}", fr["ext"], fr["fd"], fr["brs"], fr["esi"], fr["err"],
                                fr["len"], fr["data"]])
        except KeyboardInterrupt:
            pass
    for name, (n, hw) in counts.items():
        note = "hardware timestamps" if n and hw == n else ("software timestamps only" if not hw else f"{hw}/{n} hardware")
        print(f"{name}: {n} frames, {note}", file=sys.stderr)

