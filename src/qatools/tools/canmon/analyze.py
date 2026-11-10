"""Analyze a capture CSV against the FM26C DBC files.

Usage:
  qa canmon analyze capture.csv --map can0=HETC-L-TACT_ch0 --map can1=HETC-L-TACT_ch1 [-o report.md]
  qa canmon analyze capture.csv --map can1=HETC-L-TACT_ch1 --decode 0xF13382 -o enc.csv

Report per message (and per SubCode for STATUS / CTRL): count, rate, gap min / mean / p99 / max,
design period, max gap vs design (flag above --limit times design, default 5), observed lengths,
messages missing from the DBC, error frames, and estimated bus load.
--map takes a DBC name from the DBC folder (data/dbc, or CANMON_DBC_DIR).
"""
import argparse
import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import cantools

from .core import DBC_DIR, design_period, frame_us, key_of, label, pct


def main(argv=None):
    ap = argparse.ArgumentParser(prog="qa canmon analyze", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("capture")
    ap.add_argument("--map", action="append", default=[], help="iface=DBC channel name, e.g. can0=HETC-L-ARM_ch0")
    ap.add_argument("--limit", type=float, default=5.0, help="flag when max gap > limit x design period")
    ap.add_argument("--decode", help="CAN ID (hex) to export decoded signals as CSV to -o")
    ap.add_argument("-o", "--out")
    a = ap.parse_args(argv)

    chmap = dict(m.split("=", 1) for m in a.map)
    dbs = {}
    for iface, ch in chmap.items():
        path = DBC_DIR / f"{ch}.dbc"
        if not path.exists():
            sys.exit(f"no DBC for {ch}: {path}")
        dbs[iface] = cantools.database.load_file(path)

    rows = list(csv.DictReader(open(a.capture, newline="")))
    if not rows:
        sys.exit("empty capture")
    use_hw = all(r["t_hw"] for r in rows if not int(r["err"]))
    tcol = "t_hw" if use_hw else "t_sw"

    if a.decode:
        want = int(a.decode, 16)
        out = csv.writer(open(a.out, "w", newline="")) if a.out else csv.writer(sys.stdout)
        header = None
        for r in rows:
            if int(r["err"]) or int(r["can_id"], 16) != want or r["iface"] not in dbs:
                continue
            msg = dbs[r["iface"]].get_message_by_frame_id(want)
            sig = msg.decode(bytes.fromhex(r["data"]).ljust(msg.length, b"\0"), decode_choices=False,
                             allow_truncated=True, allow_excess=True)
            if header is None:
                header = ["t", "iface"] + list(sig)
                out.writerow(header)
            out.writerow([r[tcol], r["iface"]] + [sig.get(k, "") for k in header[2:]])
        return

    frames = defaultdict(list)
    lengths = defaultdict(lambda: defaultdict(int))
    errors = defaultdict(int)
    busy = defaultdict(float)
    span = {}
    for r in rows:
        iface, t = r["iface"], float(r[tcol])
        span.setdefault(iface, [t, t])
        span[iface][0] = min(span[iface][0], t)
        span[iface][1] = max(span[iface][1], t)
        if int(r["err"]):
            errors[iface] += 1
            continue
        cid, payload = int(r["can_id"], 16), bytes.fromhex(r["data"])
        k = (iface,) + key_of(cid, payload)
        frames[k].append(t)
        lengths[k][int(r["len"])] += 1
        dcan = chmap.get(iface) == "D-CAN"
        busy[iface] += frame_us(int(r["len"]), 0.125 if dcan else 1.0, 1.0 if dcan else 5.0, int(r["brs"]))

    lines = [f"# CAN capture report", "", f"- File: `{a.capture}`, timestamps: {'hardware' if use_hw else 'software (kernel receive time)'}",
             f"- Flag rule: max gap > {a.limit:g} x design period", ""]
    if not use_hw:
        lines += ["> Software timestamps include USB and scheduler jitter. Treat sub-millisecond gaps with care.", ""]
    for iface in sorted(span):
        dur = span[iface][1] - span[iface][0] or 1e-9
        db = dbs.get(iface)
        lines += [f"## {iface} ({chmap.get(iface, 'no DBC')})", "",
                  f"- Duration {dur:.2f} s, error frames {errors[iface]}, estimated load {100 * busy[iface] / 1e6 / dur:.1f} %", "",
                  "| CAN ID | Message | Part | Count | Rate (Hz) | Gap min | mean | p99 | max (ms) | Design (ms) | Max / design | Lengths | Note |",
                  "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|"]
        for k in sorted((k for k in frames if k[0] == iface), key=lambda k: (k[1], str(k[2]))):
            _, cid, sub = k
            ts = sorted(frames[k])
            gaps = [(b - a_) * 1e3 for a_, b in zip(ts, ts[1:])]
            msg = None
            if db is not None:
                try:
                    msg = db.get_message_by_frame_id(cid)
                except KeyError:
                    msg = None
            design = design_period(msg, sub)
            note = "not in DBC" if msg is None else ""
            ratio = ""
            if gaps and design:
                ratio = f"{max(gaps) / design:.1f}"
                if max(gaps) > a.limit * design:
                    note = (note + " " if note else "") + "**gap over limit**"
            g = (f"{min(gaps):.3f} | {statistics.mean(gaps):.3f} | {pct(gaps, 0.99):.3f} | {max(gaps):.3f}"
                 if gaps else "- | - | - | -")
            lens = ", ".join(f"{n}:{c}" for n, c in sorted(lengths[k].items()))
            lines.append(f"| 0x{cid:06X} | {msg.name if msg else '-'} | {label(msg, sub, cid >> 16)} | {len(ts)} | "
                         f"{len(ts) / dur:.1f} | {g} | {f'{design:g}' if design else '-'} | {ratio or '-'} | {lens} | {note} |")
        lines.append("")
    text = "\n".join(lines)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    else:
        print(text)

