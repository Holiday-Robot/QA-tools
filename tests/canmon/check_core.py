#!/usr/bin/env python3
"""Checks for the canmon core on the synthetic sample: grouping, period statistics, signal extraction."""
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
from qatools.tools.canmon import sample as sample_tool  # noqa: E402
from qatools.tools.canmon.core import Capture, Session  # noqa: E402

fails = []
with tempfile.TemporaryDirectory() as d:
    path = Path(d) / "s.csv"
    sample_tool.main([str(path), "--seconds", "12"])
    cap = Capture(path)
    ses = Session(cap)
    ses.set_channel("can0", "HETC-L-TACT_ch0")
    ses.set_channel("can1", "HETC-L-TACT_ch1")

    def group(iface, can_id, part):
        return next(g for g in cap.groups if g.iface == iface and g.can_id == can_id and g.part == part)

    if cap.ts_kind != "hardware" or cap.ifaces != ["can0", "can1"] or abs(cap.duration - 12) > 0.01:
        fails.append(f"capture meta {cap.ts_kind} {cap.ifaces} {cap.duration}")

    # HTAC_61 encoder: 2 ms design with one ~12 ms dropout at 8.2 s
    g = group("can0", 0xF13D82, "ANGLE_MULTI")
    st = ses.period_stats(g)
    if not (g.design == 2 and st.flagged and st.over == 1 and 11.5 < st.max < 13 and abs(st.t_max - 8.2103) < 0.002
            and abs(st.mean - 2.0) < 0.01 and st.lengths == {44: st.count}):
        fails.append(f"HTAC_61 stats {st.count} {st.mean} {st.max} {st.t_max} {st.over} {st.lengths}")
    st2 = ses.period_stats(g, 0.0, 5.0)
    if st2.flagged or not (2400 < st2.count < 2510) or st2.max > 2.2:
        fails.append(f"range stats {st2.count} {st2.max} {st2.flagged}")
    # other parts of the same CAN ID stay separate
    for part, design in (("TACTILE_G2 point 0", 10), ("TACTILE_G2 status", 50)):
        gp = group("can0", 0xF13D82, part)
        sp = ses.period_stats(gp)
        if gp.design != design or abs(sp.mean - design) > 0.05 or sp.flagged:
            fails.append(f"{part}: {gp.design} {sp.mean}")
    # HMDC TPDO 10 ms, unknown stream frame has no DBC entry
    gt = group("can1", 0x040382, "")
    if gt.design != 10 or abs(ses.period_stats(gt).mean - 10) > 0.05:
        fails.append("HMDC TPDO period")
    unk = [g for g in cap.groups if g.msg is None]
    if [(g.iface, g.can_id) for g in unk] != [("can1", 0xFF8203)] or "BYPASS" not in unk[0].name:
        fails.append(f"unknown groups {[(g.iface, hex(g.can_id)) for g in unk]}")

    # Vectorised signal extraction must equal cantools frame decoding
    for iface, can_id, name in (("can1", 0xF13382, "Enc0_AngleRad"), ("can1", 0xF13382, "Enc1_Id"),
                                ("can1", 0xF13382, "G2p_Z3"), ("can1", 0xF13382, "G2s_TactileOk"),
                                ("can1", 0x040382, "PresentPosition"), ("can1", 0x040382, "Temperature"),
                                ("can1", 0x040382, "PresentState"), ("can1", 0xF10B01, "Ang_Valid"),
                                ("can0", 0xF13D82, "Enc5_AngleRad"), ("can1", 0x038203, "DesiredPosition")):
        s = ses.series(iface, can_id, name)
        if len(s.t) < 10:
            fails.append(f"{name}: only {len(s.t)} samples")
            continue
        msg = ses.db(iface).get_message_by_frame_id(can_id)
        for j in np.linspace(0, len(s.t) - 1, 25).astype(int):
            ref = msg.decode(cap.payload(s.fidx[j]), decode_choices=False, allow_truncated=True)[name]
            if abs(float(ref) - s.y[j]) > 1e-6 * max(1.0, abs(float(ref))):
                fails.append(f"{name}[{j}] {s.y[j]} != {ref}")
                break
    s = ses.series("can1", 0xF13382, "Enc0_AngleRad")
    if not (abs(len(s.t) - 6000) < 5 and abs(s.y[:100].mean() - 0.42) < 1e-4 and s.y.max() > 0.7):
        fails.append(f"Enc0 series {len(s.t)} {s.y[:3]} {s.y.max()}")
    if len(ses.series("can1", 0x040382, "PresentState").t) != len(gt.idx):
        fails.append("non-mux series length")

    # Frame decode for the Trace view, and bus load
    dec = dict(ses.decode(int(group("can1", 0xF13382, "ANGLE_MULTI").idx[0])))
    if dec.get("SubCode") != "ANGLE_MULTI" or dec.get("Enc0_Id") != "52" or not dec.get("Enc0_AngleRad", "").endswith("rad"):
        fails.append(f"decode {list(dec.items())[:6]}")
    load1, fps1, err1 = ses.bus_load("can1")
    load0, fps0, _ = ses.bus_load("can0")
    if not (55 < load1 < 70 and 7400 < fps1 < 7650 and 20 < load0 < 30 and err1 == 0):
        fails.append(f"bus load {load1:.1f} {fps1:.0f} {load0:.1f} {fps0:.0f}")
    print(f"{cap.n} frames, {len(cap.groups)} groups, can0 {load0:.1f} % {fps0:.0f} fps, can1 {load1:.1f} % {fps1:.0f} fps")

print("FAILED:\n" + "\n".join(fails) if fails else "ALL CHECKS PASSED")
sys.exit(1 if fails else 0)
