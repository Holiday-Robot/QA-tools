#!/usr/bin/env python3
"""Checks for live statistics and logging, using a capture file played back as the frame source."""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
from qatools.tools.canmon import sample as sample_tool  # noqa: E402
from qatools.tools.canmon.core import Capture, Session  # noqa: E402
from qatools.tools.canmon.live import LiveMeta, LiveSession, LiveStats, ReplaySource  # noqa: E402

fails = []
MAP = {"can0": "HETC-L-TACT_ch0", "can1": "HETC-L-TACT_ch1"}
with tempfile.TemporaryDirectory() as d:
    sample = Path(d) / "s.csv"
    sample_tool.main([str(sample), "--seconds", "12"])
    cap = Capture(sample)
    ses = Session(cap)
    for k, v in MAP.items():
        ses.set_channel(k, v)
    meta = LiveMeta(MAP)
    src = ReplaySource(sample)

    # ---- running statistics must equal the offline statistics of the same frames
    stats = LiveStats(meta)
    t = time.perf_counter()
    for i in range(0, len(src.frames), 500):
        stats.update(src.frames[i:i + 500])
    rate = len(src.frames) / (time.perf_counter() - t)
    groups, ifaces, elapsed, _ = stats.snapshot()
    live = {g.key: g for g in groups}
    if len(live) != len(cap.groups) or abs(elapsed - cap.duration) > 1e-6:
        fails.append(f"groups {len(live)} vs {len(cap.groups)}, elapsed {elapsed} vs {cap.duration}")
    for g in cap.groups:
        off = ses.period_stats(g)
        lg = live.get((g.iface, g.can_id, g.sub))
        if lg is None:
            fails.append(f"missing live group {g.title}")
            continue
        if lg.count != off.count or lg.design != g.design or lg.lengths != off.lengths:
            fails.append(f"{g.title}: count/design/lengths {lg.count} {lg.design} vs {off.count} {g.design}")
        if off.mean is not None:
            if (abs(lg.mean - off.mean) > 1e-6 or abs(lg.gmin - off.min) > 1e-6 or abs(lg.gmax - off.max) > 1e-6
                    or abs(lg.t_max - off.t_max) > 1e-6 or lg.over != off.over):
                fails.append(f"{g.title}: {lg.mean} {lg.gmin} {lg.gmax} {lg.over} vs {off.mean} {off.min} {off.max} {off.over}")
            p = lg.p99()
            if not (off.p99 <= p * 1.0001 and p <= max(off.p99 * 10 ** (1 / 8) * 1.0001, off.p99 + 1e-9)):
                fails.append(f"{g.title}: p99 estimate {p} vs {off.p99}")
    for iface in cap.ifaces:
        load, fps, err = ses.bus_load(iface)
        ic = ifaces[iface]
        if abs(ic.busy_us / 1e6 / elapsed * 100 - load) > 0.01 or abs(ic.frames / elapsed - fps) > 0.5:
            fails.append(f"{iface} load {ic.busy_us / 1e4 / elapsed:.2f} vs {load:.2f}")
    g61 = live[("can0", 0xF13D82, (0x1B,))]
    if g61.over != 1 or not (11.5 < g61.gmax < 12.5) or abs(g61.t_max - 8.2103) > 0.002:
        fails.append(f"HTAC_61 dropout {g61.over} {g61.gmax} {g61.t_max}")
    dec = dict(meta.decode("can1", 0xF13382, live[("can1", 0xF13382, (0x1B,))].last_data))
    if dec.get("Enc0_Id") != "52" or meta.group("can1", 0xF13382, (0x1B,)).short != "HTAC_51 · ANGLE_MULTI":
        fails.append(f"live decode/meta {list(dec.items())[:3]}")

    # ---- reset and limit change
    stats.reset()
    if stats.snapshot()[0] or stats.snapshot()[3] != 0:
        fails.append("reset should clear the statistics")
    stats.set_limit(1.01)
    stats.update(src.frames[:3000])
    if not any(g.over for g in stats.snapshot()[0]):
        fails.append("a tight limit should flag ordinary jitter")

    # ---- reader thread: log file equals the source, statistics complete
    log = Path(d) / "log" / "live.csv"
    fast = ReplaySource(sample, speed=40)
    live_ses = LiveSession(fast, meta, log)
    live_ses.start()
    deadline = time.time() + 30
    while not fast.finished and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    live_ses.stop()
    if live_ses.error or live_ses.running:
        fails.append(f"session error {live_ses.error} running {live_ses.running}")
    if live_ses.stats.total != cap.n:
        fails.append(f"session frames {live_ses.stats.total} != {cap.n}")
    import csv
    with open(log, newline="") as fa, open(sample, newline="") as fb:
        la, lb = list(csv.reader(fa)), list(csv.reader(fb))
    if la[0][-1] != "dir" or [r[:11] for r in la] != lb or any(r[11] for r in la[1:]):
        fails.append("log file differs from the source capture (apart from the empty dir column)")
    back = Capture(log)
    if back.n != cap.n or len(back.groups) != len(cap.groups) or back.ts_kind != "hardware":
        fails.append("log cannot be reopened as a capture")

    # ---- frames without hardware timestamps, error frames, missing DBC
    from qatools.tools.canmon.live import Frame
    st2 = LiveStats(LiveMeta({"can0": "no_such_file"}))
    st2.update([Frame(None, 10.0, "can0", 0x040178, 1, 1, 1, 0, 0, 20, bytes(20)),
                Frame(None, 10.002, "can0", 0x040178, 1, 1, 1, 0, 0, 20, bytes(20)),
                Frame(None, 10.003, "can0", 0x4, 0, 0, 0, 0, 1, 8, bytes(8))])
    gs, ifs, _, _ = st2.snapshot()
    if st2.all_hw or len(gs) != 1 or gs[0].count != 2 or abs(gs[0].gmax - 2.0) > 1e-6 or ifs["can0"].errors != 1 or gs[0].design is not None:
        fails.append(f"software timestamps / error frames {gs} {ifs}")
    if not st2.meta.errors:
        fails.append("a missing DBC should be reported, not raise")
    print(f"{cap.n} frames, statistics update {rate:,.0f} frames/s")

print("FAILED:\n" + "\n".join(fails) if fails else "ALL CHECKS PASSED")
sys.exit(1 if fails else 0)
