#!/usr/bin/env python3
"""Offline checks for the frame parser and for qa canmon analyze, without a CAN device."""
import csv
import os
import random
import socket
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
from qatools.tools.canmon import sockcan as cc  # noqa: E402

ANALYZE = [sys.executable, "-m", "qatools", "canmon", "analyze"]
ENV = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src")] + sys.path))

fails = []

# 1. Frame parser: CAN FD extended frame with BRS, and a classic 16-byte frame
raw = struct.pack("=IBBBB64s", 0x040178 | cc.CAN_EFF_FLAG, 20, cc.CANFD_BRS, 0, 0, bytes(range(20)))
fr = cc.parse_frame(raw)
if (fr["can_id"], fr["ext"], fr["fd"], fr["brs"], fr["len"], fr["data"]) != (0x040178, 1, 1, 1, 20, bytes(range(20)).hex()):
    fails.append(f"fd parse {fr}")
classic = struct.pack("=IBBBB8s", 0x123, 3, 0, 0, 0, b"\x01\x02\x03")
fr = cc.parse_frame(classic)
if (fr["can_id"], fr["ext"], fr["fd"], fr["len"], fr["data"]) != (0x123, 0, 0, 3, "010203"):
    fails.append(f"classic parse {fr}")
fr = cc.parse_frame(struct.pack("=IBBBB8s", cc.CAN_ERR_FLAG | 0x4, 8, 0, 0, 0, bytes(8)))
if not fr["err"]:
    fails.append("error flag not detected")

# 2. Timestamp parser: hardware present, and software only
anc = [(socket.SOL_SOCKET, cc.SCM_TIMESTAMPING, cc.TS.pack(100, 500, 0, 0, 7, 250))]
hw, sw = cc.parse_ts(anc)
if abs(hw - 7.00000025) > 1e-12 or abs(sw - 100.0000005) > 1e-12:
    fails.append(f"ts parse {hw} {sw}")
hw, sw = cc.parse_ts([(socket.SOL_SOCKET, cc.SCM_TIMESTAMPING, cc.TS.pack(100, 0, 0, 0, 0, 0))])
if hw is not None or sw != 100:
    fails.append("software-only timestamp")

# 3. Analyzer on a synthetic L-TACT ch1 capture: HTAC 51 encoder 2 ms with one 12 ms dropout,
#    HMDC TPDO 10 ms, an unknown ID, and one error frame.
rnd = random.Random(1)
rows = []
t = 0.0
for i in range(500):
    t += 0.012 if i == 250 else 0.002 + rnd.uniform(-5e-5, 5e-5)
    enc = struct.pack("<BB", 0x1B, 2) + struct.pack("<BHf", 52, 3, 0.1 * i) + struct.pack("<BHf", 53, 3, 0.2)
    rows.append([f"{t:.9f}", f"{t + 0.0003:.9f}", "can1", "0xF13382", 1, 1, 1, 0, 0, len(enc), enc.hex()])
for i in range(100):
    tp = struct.pack("<BIBBfffb", 0, 0, 5, 2, 0.01 * i, 0, 0, 30)
    rows.append([f"{0.01 * i:.9f}", f"{0.01 * i:.9f}", "can1", "0x040382", 1, 1, 1, 0, 0, 20, tp.hex()])
rows.append(["0.5", "0.5", "can1", "0x123456", 1, 1, 1, 0, 0, 1, "00"])
rows.append(["0.6", "0.6", "can1", "0x4", 0, 0, 0, 0, 1, 8, "00" * 8])

with tempfile.TemporaryDirectory() as d:
    cap = Path(d) / "cap.csv"
    with cap.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_hw", "t_sw", "iface", "can_id", "ext", "fd", "brs", "esi", "err", "len", "data"])
        w.writerows(rows)
    rep = subprocess.run(ANALYZE + [str(cap), "--map", "can1=HETC-L-TACT_ch1"], capture_output=True, text=True, env=ENV)
    out = rep.stdout + rep.stderr
    for want in ("timestamps: hardware", "error frames 1", "ANGLE_MULTI", "**gap over limit**", "not in DBC", "| 10 |"):
        if want not in out:
            fails.append(f"report missing {want!r}")
    enc_line = next((l for l in out.splitlines() if "ANGLE_MULTI" in l), "")
    if "12.0" not in enc_line or "| 16:500 |" not in enc_line:
        fails.append(f"ANGLE_MULTI stats wrong: {enc_line}")
    tpdo_line = next((l for l in out.splitlines() if "0x040382" in l), "")
    if "**gap over limit**" in tpdo_line:
        fails.append(f"TPDO flagged wrongly: {tpdo_line}")
    dec = subprocess.run(ANALYZE + [str(cap), "--map", "can1=HETC-L-TACT_ch1", "--decode", "0xF13382"],
                         capture_output=True, text=True, env=ENV).stdout.splitlines()
    head = dec[0].split(",") if dec else []
    if "Enc0_AngleRad" not in head or len(dec) != 501:
        fails.append(f"decode export wrong: {dec[:2]}")
    else:
        last = dict(zip(head, dec[-1].split(",")))
        if abs(float(last["Enc0_AngleRad"]) - 49.9) > 1e-3 or last["SubCode"] != "27":
            fails.append(f"decode values wrong: {last}")
    print(out if fails else "\n".join(out.splitlines()[:14]))

print("FAILED:\n" + "\n".join(fails) if fails else "ALL CHECKS PASSED")
sys.exit(1 if fails else 0)
