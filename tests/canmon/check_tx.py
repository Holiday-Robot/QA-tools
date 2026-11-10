#!/usr/bin/env python3
"""Checks for message transmission without hardware: encoding, frame building, scheduler timing, Tx logging."""
import errno
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
from qatools.tools.canmon import sample as sample_tool  # noqa: E402
from qatools.tools.canmon import sockcan  # noqa: E402
from qatools.tools.canmon.core import Capture, resolve_dbc  # noqa: E402
from qatools.tools.canmon.live import LiveMeta, LiveSession, ReplaySource  # noqa: E402
from qatools.tools.canmon.tx import (KIND_LOAD, KIND_MSG, KIND_RAW, LOAD_ID, Transmitter, TxEntry, TxFrame, active_signals,  # noqa: E402
                       added_load, build, encode, frame_time_us, period_s)

import cantools  # noqa: E402

fails = []
db = cantools.database.load_file(resolve_dbc("HETC-L-TACT_ch1"))

# ---- DBC encoding
quiet = db.get_message_by_name("QUIET_HETC_L_TACT_to_ALL")
if encode(quiet, {"Divider": 0, "HoldMs": 3000}).hex() != "0000b80b0000":
    fails.append("QUIET encoding")
ev = db.get_message_by_name("EVENT_HETC_L_TACT_to_ALL")
p_name, p_num = encode(ev, {"EvtCode": "ESTOP"}), encode(ev, {"EvtCode": 1})
if p_name != p_num or p_num[1] != 1:
    fails.append(f"EVENT choice by name {p_name.hex()} vs number {p_num.hex()}")
try:
    encode(quiet, {"Divider": 70000})
    fails.append("out-of-range value should be refused")
except ValueError:
    pass
ctrl = db.get_message_by_name("CTRL_HETC_L_TACT_to_ALL")
sig9 = {s.name for s in active_signals(ctrl, {"SubCode": 9})}
if "Rtc_Year2000" not in sig9 or "SubCode" not in sig9:
    fails.append(f"multiplexed signals for SubCode 9: {sorted(sig9)[:6]}")
st = db.get_message_by_name("STATUS_HTAC_51_to_HETC_L_TACT")
sub_default = encode(st, {})
if len(sub_default) > 64 or len(sub_default) < 1:
    fails.append(f"STATUS default encoding length {len(sub_default)}")

# ---- building frames
f = build(TxEntry("can1", KIND_MSG, "QUIET_HETC_L_TACT_to_ALL", {"HoldMs": 3000}), db)
if (f.can_id, f.ext, f.fd, f.data.hex()) != (0xF282FF, True, True, "0000b80b0000"):
    fails.append(f"QUIET frame {f}")
raw = build(TxEntry("can0", KIND_RAW, can_id=0x123, ext=False, fd=False, brs=True, data=bytes([1, 2, 3])))
if raw.fd or raw.brs or raw.data != b"\x01\x02\x03":
    fails.append(f"classic raw frame {raw}")
padded = build(TxEntry("can0", KIND_RAW, can_id=0x100, data=bytes(10)))
if len(padded.data) != 12 or not padded.fd:
    fails.append(f"FD padding to a valid size: {len(padded.data)}")
for bad in (TxEntry("can0", KIND_RAW, can_id=0x800, ext=False, fd=False, data=b"\0"),
            TxEntry("can0", KIND_RAW, can_id=1, data=bytes(65)),
            TxEntry("can1", KIND_MSG, "NO_SUCH_MESSAGE")):
    try:
        build(bad, db)
        fails.append(f"should refuse {bad}")
    except ValueError:
        pass
load = TxEntry("can1", KIND_LOAD, can_id=LOAD_ID, length=64, load_pct=25)
lf = build(load)
rates = (1.0, 5.0)
if abs(added_load(load, lf, rates) - 25) > 0.01 or abs(period_s(load, lf, rates) - frame_time_us(lf, rates) * 4e-6) > 1e-12:
    fails.append(f"load entry period {period_s(load, lf, rates)} load {added_load(load, lf, rates)}")
if period_s(TxEntry(period_ms=0), raw, rates) != 0:
    fails.append("period 0 must mean once")
entry_back = TxEntry.from_dict(load.to_dict())
if entry_back != load:
    fails.append("TxEntry round trip")

# ---- socket frame packing
pk = sockcan.pack_frame(0xF282FF, True, True, True, bytes.fromhex("0000b80b0000"))
cid, ln, flags, _, _, data = sockcan.FRAME.unpack(pk)
if len(pk) != 72 or cid != 0xF282FF | sockcan.CAN_EFF_FLAG or ln != 6 or flags != sockcan.CANFD_BRS:
    fails.append("canfd_frame packing")
pc = sockcan.pack_frame(0x123, False, False, False, b"\x01\x02")
if len(pc) != 16 or struct.unpack_from("=IB", pc) != (0x123, 2):
    fails.append("can_frame packing")
back = sockcan.parse_frame(pk)
if back["can_id"] != 0xF282FF or back["len"] != 6 or back["brs"] != 1 or back["data"] != "0000b80b0000":
    fails.append(f"pack/parse round trip {back}")


# ---- scheduler with a fake sink
class Sink:
    def __init__(self, full_every=0, fail_after=0):
        self.times, self.frames = [], []
        self.full_every, self.fail_after, self.calls = full_every, fail_after, 0

    def send(self, frame):
        self.calls += 1
        if self.fail_after and self.calls > self.fail_after:
            raise OSError(errno.ENETDOWN, "Network is down")
        if self.full_every and self.calls % self.full_every == 0:
            raise OSError(errno.ENOBUFS, "No buffer space available")
        self.times.append(time.monotonic())
        self.frames.append(frame)


fr = TxFrame("can1", 0x100, True, True, True, bytes(8))
sink = Sink()
tx = Transmitter(sink, auto_stop_s=0)
tx.start("p", fr, 0.002)                      # 2 ms periodic
time.sleep(1.0)
tx.stop("p")
n = len(sink.times)
gaps = [b - a for a, b in zip(sink.times, sink.times[1:])]
mean = sum(gaps) / len(gaps) if gaps else 0
if not (450 <= n <= 520) or abs(mean - 0.002) > 0.0002:
    fails.append(f"2 ms periodic: {n} frames in 1 s, mean gap {mean * 1e3:.3f} ms")
st = tx.status()["p"]
if not st["done"] or st["reason"] != "stopped":
    fails.append(f"stop status {st}")
time.sleep(0.05)
if len(sink.times) != n:
    fails.append("frames sent after stop")

sink2 = Sink()
tx2 = Transmitter(sink2, auto_stop_s=0.3)
tx2.start("count", fr, 0.001, count=50)
tx2.once(fr)
tx2.start("auto", fr, 0.01)
time.sleep(0.6)
s2 = tx2.status()
if s2["count"]["sent"] != 50 or s2["count"]["reason"] != "count":
    fails.append(f"count limit {s2['count']}")
if s2["auto"]["reason"] != "auto" or not (25 <= s2["auto"]["sent"] <= 32):
    fails.append(f"auto stop {s2['auto']}")
if sum(1 for k in s2 if k[0] == "once") != 1 or not tx2.status()[next(k for k in s2 if k[0] == "once")]["sent"] == 1:
    fails.append("once")
tx2.close()
if tx2.alive:
    fails.append("transmitter thread did not end")

sink3 = Sink(full_every=5)
tx3 = Transmitter(sink3, auto_stop_s=0)
tx3.start("f", fr, 0.001, count=40)
time.sleep(0.3)
s3 = tx3.status()["f"]
if s3["sent"] != 40 or s3["full"] < 5:
    fails.append(f"queue full backoff {s3}")
tx3.close()

sink4 = Sink(fail_after=3)
tx4 = Transmitter(sink4, auto_stop_s=0)
tx4.start("e", fr, 0.001)
time.sleep(0.1)
s4 = tx4.status()["e"]
if not s4["done"] or "송신 오류" not in s4["reason"] or s4["sent"] != 3:
    fails.append(f"send error stops the entry {s4}")
tx4.close()

sink5 = Sink()
tx5 = Transmitter(sink5, auto_stop_s=0)
tx5.start("u", fr, 0.01)
time.sleep(0.1)
fr2 = TxFrame("can1", 0x200, True, True, True, bytes(8))
tx5.update("u", fr2, 0.002)
time.sleep(0.2)
tx5.close()
after = [f for f in sink5.frames if f.can_id == 0x200]
if not (80 <= len(after) <= 110):
    fails.append(f"update to a new frame and period: {len(after)} frames of the new frame")

hi = Sink()
tx6 = Transmitter(hi, auto_stop_s=0)
tx6.start("h", fr, 1 / 6000)                  # what a 100 % load entry asks for at 1 M / 5 M with 64 bytes
time.sleep(1.0)
tx6.close()
if not 5000 <= len(hi.times) <= 6100:
    fails.append(f"6000 frames/s entry sent {len(hi.times)} in 1 s")

# ---- loopback through the preview source: sent frames are logged as Tx and reopen as Tx
MAP = {"can0": "HETC-L-TACT_ch0", "can1": "HETC-L-TACT_ch1"}
with tempfile.TemporaryDirectory() as d:
    sample = Path(d) / "s.csv"
    sample_tool.main([str(sample), "--seconds", "3"])
    src = ReplaySource(sample, speed=1.0)
    log = Path(d) / "live.csv"
    ses = LiveSession(src, LiveMeta(MAP), log)
    ses.start()
    txl = Transmitter(src, auto_stop_s=0)
    qf = build(TxEntry("can1", KIND_MSG, "QUIET_HETC_L_TACT_to_ALL", {"HoldMs": 3000}), db)
    time.sleep(0.3)
    txl.start("q", qf, 0.01, count=100)
    time.sleep(1.5)
    txl.close()
    ses.stop()
    cap = Capture(log)
    groups = {(g.iface, g.can_id): g for g in cap.groups}
    gq = groups.get(("can1", 0xF282FF))
    if gq is None or gq.tx != 100 or len(gq.idx) != 100 or int(cap.tx.sum()) != 100:
        fails.append(f"Tx frames in the log: {None if gq is None else (gq.tx, len(gq.idx))}, total tx {int(cap.tx.sum())}")
    elif abs(float((cap.t[gq.idx[-1]] - cap.t[gq.idx[0]])) - 0.99) > 0.05:
        fails.append("Tx frame times are not on the log's clock")
    if not all(g.tx == 0 for g in cap.groups if g.can_id != 0xF282FF):
        fails.append("received frames marked as Tx")
    live_g = {g.key: g for g in ses.stats.snapshot()[0]}
    if live_g.get(("can1", 0xF282FF, None)) is None or live_g[("can1", 0xF282FF, None)].tx != 100:
        fails.append("live statistics Tx count")
    old = Capture(sample)                     # logs without a dir column still open
    if old.tx.any():
        fails.append("old capture shows Tx frames")

print("FAILED:\n" + "\n".join(fails) if fails else "ALL CHECKS PASSED")
sys.exit(1 if fails else 0)
