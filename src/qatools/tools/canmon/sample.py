"""Write a synthetic capture of HETC-L-TACT ch0 (can0) and ch1 (can1) for trying the viewer without a robot.

Usage: qa canmon sample sample_ltact.csv [--seconds 20]

The traffic follows the design periods in the DBC. It also contains three things to look for:
  - HTAC_61 encoder frames (can0, 0xF13D82) stop for about 12 ms at t = 8.2 s
  - the fingers move between t = 5 s and t = 15 s
  - a few frames with an ID that is not in the DBC
This is generated data, not a recording from a robot.
"""
import argparse
import csv
import math
import random
import struct

HOST = 130


def cid(t, s, d):
    return (t << 16) | (s << 8) | d


def angle(t, amp, lag=0.0, base=0.35):
    if t < 5 or t > 15:
        return base
    return base + amp * 0.5 * (1 - math.cos(2 * math.pi * 0.4 * (t - 5 - lag)))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="qa canmon sample", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--seconds", type=float, default=20.0)
    a = ap.parse_args(argv)
    rnd = random.Random(26)
    rows = []

    def emit(t, iface, can_id, payload):
        t += rnd.uniform(-4e-5, 4e-5)
        rows.append((t, iface, can_id, payload))

    steps = int(a.seconds / 0.002)
    for k in range(steps):
        t = k * 0.002
        # can1: motor commands and states, wrist angle sensors, palm HTAC
        for m in range(1, 9):
            pos = angle(t, 0.35, 0, 0.0)
            emit(t + 0.00005 * m, "can1", cid(0x03, HOST, m), struct.pack("<BBBfff", 0, 5, 2, pos, 0.0, 0.0))
        for m in (1, 2):
            pos = angle(t, 0.2, 0.01, 0.1 * m)
            emit(t + 0.0006 + 0.0001 * m, "can1", cid(0x04, m, HOST), struct.pack("<BIBBfffb", 0, 0, 5, 2, pos, 0.0, 0.3, 34))
            emit(t + 0.0009 + 0.0001 * m, "can1", cid(0xF1, 10 + m, m), struct.pack("<BHf", 0x11, 3, pos + 0.004))
        if k % 5 == 0:
            for m in range(3, 9):
                pos = angle(t, 0.35, 0.02, 0.0)
                state = 4 if t < 0.7 else 5
                emit(t + 0.0012 + 0.00008 * m, "can1", cid(0x04, m, HOST),
                     struct.pack("<BIBBfffb", 0, 0, state, 2, pos, 0.0, 0.12 * m, 31))
        enc = struct.pack("<BB", 0x1B, 2) + b"".join(
            struct.pack("<BHf", 52 + i, 3, angle(t, 0.3 - 0.07 * i, 0.06 + 0.03 * i, 0.42 - 0.12 * i)) for i in range(2))
        emit(t + 0.0015, "can1", cid(0xF1, 51, HOST), enc)

        # can0: two finger HTAC boards, six encoders each
        for b, board in enumerate((61, 71)):
            if board == 61 and 8.2 <= t < 8.2095:
                continue
            enc = struct.pack("<BB", 0x1B, 6) + b"".join(
                struct.pack("<BHf", board + 1 + i, 3, angle(t, 0.25 + 0.03 * i, 0.05 + 0.01 * i, 0.2 + 0.05 * i))
                for i in range(6))
            emit(t + 0.0003 + 0.0004 * b, "can0", cid(0xF1, board, HOST), enc)

        if k % 5 == 0:   # tactile points every 10 ms
            for iface, board, total, off in (("can1", 51, 40, 0.0017), ("can0", 61, 32, 0.0010), ("can0", 71, 32, 0.0013)):
                for first in range(0, total, 10):
                    cnt = min(10, total - first)
                    pts = b"".join(struct.pack("<hhh", 20 + p, -15 + p, 1200 + int(40 * math.sin(t + p)))
                                   for p in range(first, first + cnt))
                    emit(t + off + 0.00012 * (first // 10), iface, cid(0xF1, board, HOST),
                         struct.pack("<BBBB", 0x09, 0, first, cnt) + pts)
        if k % 25 == 0:  # board status every 50 ms
            for iface, board, tn, en in (("can1", 51, 40, 2), ("can0", 61, 32, 6), ("can0", 71, 32, 6)):
                st = struct.pack("<BBBBBBQQII", 0x09, 1, tn, en, 0, 0, (1 << tn) - 1, 0, (1 << en) - 1, 0)
                emit(t + 0.0019, iface, cid(0xF1, board, HOST), st)
        if k % 2500 == 1200:  # a stream frame the DBC does not describe
            emit(t + 0.0018, "can1", cid(0xFF, HOST, 3), bytes([2, 0, 1, 0, 0x21, 0, 0, 0]))

    rows.sort(key=lambda r: r[0])
    t0 = 1793900000.0
    with open(a.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_hw", "t_sw", "iface", "can_id", "ext", "fd", "brs", "esi", "err", "len", "data"])
        for t, iface, can_id, payload in rows:
            w.writerow([f"{t + 100:.9f}", f"{t0 + t + 0.0003:.9f}", iface, f"0x{can_id:X}", 1, 1, 1, 0, 0, len(payload), payload.hex()])
    print(f"{len(rows)} frames, {a.seconds:g} s -> {a.out}")

