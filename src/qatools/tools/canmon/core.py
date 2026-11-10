"""Capture loading, message grouping, period statistics and signal extraction. No Qt in this module."""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from pathlib import Path

import cantools
import numpy as np

# DBC files stay with their generator in workspace/notes/can-db/dbc; CANMON_DBC_DIR overrides it.
DBC_DIR = Path(os.environ.get("CANMON_DBC_DIR") or Path(__file__).resolve().parents[4] / "data" / "dbc")
FD = [0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64]
TYPE_NAME = {0x00: "NORMAL", 0x02: "EVENT", 0x03: "RPDO", 0x04: "TPDO", 0x05: "DEV_BYPASS", 0xF0: "CTRL",
             0xF1: "STATUS", 0xF2: "QUIET", 0xFC: "REMOTE", 0xFD: "MSG", 0xFE: "CLI", 0xFF: "BYPASS"}
COLUMNS = ["t_hw", "t_sw", "iface", "can_id", "ext", "fd", "brs", "esi", "err", "len", "data"]


# ---------------------------------------------------------------- shared helpers (also used by can_analyze.py)
def frame_us(length, arb, data, brs):
    """Nominal frame time in microseconds for a 29-bit CAN FD frame, without dynamic stuff bits."""
    dlc = next(b for b in FD if b >= length)
    if not brs:
        data = arb
    crc = 17 if dlc <= 16 else 21
    return 49 / arb + (5 + 8 * dlc + 4 + crc + 6) / data


def key_of(can_id, payload):
    """Group STATUS / CTRL by SubCode, tactile frames by G2 type and point index."""
    t = can_id >> 16
    if t not in (0xF0, 0xF1) or not payload:
        return (can_id, None)
    sub = payload[0]
    if t == 0xF1 and sub == 0x09 and len(payload) >= 3:
        return (can_id, (sub, payload[1], payload[2] if payload[1] == 0 else None))
    return (can_id, (sub,))


def sub_key(sub):
    """Key used in the DBC SubCodeCycles / SubCodeLengths attributes."""
    return f"{sub[0]}.{sub[1]}" if sub[0] == 0x09 and len(sub) > 1 else str(sub[0])


def sub_attr(msg, name):
    """Parse a "key:value;..." message attribute from the DBC into a dict."""
    attr = msg.dbc.attributes.get(name) if msg is not None and msg.dbc else None
    if attr is None or not attr.value:
        return {}
    return {k: float(v) for k, v in (item.split(":") for item in attr.value.split(";"))}


def label(msg, sub, t):
    """Readable name of the SubCode part of a message."""
    if sub is None:
        return ""
    name = f"0x{sub[0]:02X}"
    if msg is not None:
        choices = next((s.choices for s in msg.signals if s.name == "SubCode"), None) or {}
        name = str(choices.get(sub[0], name))
    if t == 0xF1 and sub[0] == 0x09 and len(sub) > 1:
        if sub[1] == 0:
            return f"{name} point {sub[2]}"
        return f"{name} status" if sub[1] == 1 else f"{name} type {sub[1]}"
    return name


def design_period(msg, sub):
    """Design period in ms from the DBC, or None for event-driven messages."""
    if msg is None:
        return None
    if sub is None:
        return msg.cycle_time
    return sub_attr(msg, "SubCodeCycles").get(sub_key(sub))


def pct(values, q):
    v = sorted(values)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def dbc_files(dbc_dir=None):
    """DBC files in a folder (default: the folder shipped with the tool), sorted by name."""
    return sorted(Path(dbc_dir or DBC_DIR).glob("*.dbc"))


def dbc_channels(dbc_dir=None):
    return [p.stem for p in dbc_files(dbc_dir)]


def resolve_dbc(spec, dbc_dir=None):
    """Turn a channel name such as HETC-L-ARM_ch0, or a path to any .dbc file, into a path."""
    p = Path(spec)
    if p.suffix.lower() == ".dbc" or len(p.parts) > 1:
        return p
    return Path(dbc_dir or DBC_DIR) / f"{spec}.dbc"


# ---------------------------------------------------------------- capture
@dataclass
class Group:
    """Frames of one message part on one interface (CAN ID, plus SubCode / tactile part)."""
    iface: str
    can_id: int
    sub: tuple | None
    idx: np.ndarray                      # frame indices, time ordered
    msg: object = None                   # cantools message, when a DBC is mapped and knows the ID
    part: str = ""
    design: float | None = None
    tx: int = 0                          # frames of this group sent by this PC (logs with a dir column)

    @property
    def type(self):
        return self.can_id >> 16

    @property
    def name(self):
        if self.msg is not None:
            return self.msg.name
        return f"{TYPE_NAME.get(self.type, f'0x{self.type:02X}')} {(self.can_id >> 8) & 0xFF}→{self.can_id & 0xFF}"

    @property
    def title(self):
        return f"0x{self.can_id:06X} {self.name}" + (f" · {self.part}" if self.part else "")

    @property
    def short(self):
        """Compact name for narrow lists: sender and part, or type and peer."""
        if self.msg is None:
            return self.name
        kind, _, rest = self.msg.name.partition("_")
        src, _, dst = rest.partition("_to_")
        if self.part:
            return f"{src} · {self.part}"
        return f"{kind} → {dst}" if kind == "RPDO" else f"{kind} {src}"


@dataclass
class PeriodStats:
    count: int = 0
    mean: float | None = None
    min: float | None = None
    max: float | None = None
    p99: float | None = None
    t_max: float | None = None           # time of the frame that ended the longest gap
    over: int = 0                        # gaps above limit x design
    design: float | None = None
    ratio: float | None = None
    flagged: bool = False
    lengths: dict = field(default_factory=dict)
    gaps_ms: np.ndarray | None = None
    gap_t: np.ndarray | None = None


@dataclass
class Series:
    t: np.ndarray
    y: np.ndarray
    fidx: np.ndarray                     # frame index of each sample
    sig: object


class Capture:
    """One capture file loaded into arrays. Times are seconds from the first frame."""

    def __init__(self, path):
        self.path = Path(path)
        with open(path, newline="") as f:
            reader = csv.reader(f)
            head = next(reader, None)
            if head is None or any(c not in head for c in COLUMNS):
                raise ValueError(f"캡처 파일 형식이 아닙니다. 필요한 열: {', '.join(COLUMNS)}")
            col = {n: i for i, n in enumerate(head)}
            rows = list(reader)
        if not rows:
            raise ValueError("캡처 파일에 프레임이 없습니다.")
        n = len(rows)
        c_hw, c_sw, c_if, c_id, c_brs, c_err, c_len, c_data = (col[k] for k in
                                                               ("t_hw", "t_sw", "iface", "can_id", "brs", "err", "len", "data"))
        err = np.fromiter((r[c_err] == "1" for r in rows), bool, n)
        self.ts_kind = "hardware" if all(r[c_hw] for r, e in zip(rows, err) if not e) else "software"
        c_t = c_hw if self.ts_kind == "hardware" else c_sw
        t = np.fromiter((float(r[c_t] or r[c_sw]) for r in rows), float, n)
        names = sorted({r[c_if] for r in rows})
        pos = {name: i for i, name in enumerate(names)}
        iface = np.fromiter((pos[r[c_if]] for r in rows), np.int16, n)
        can_id = np.fromiter((int(r[c_id], 16) for r in rows), np.int64, n)
        length = np.fromiter((int(r[c_len]) for r in rows), np.int16, n)
        brs = np.fromiter((r[c_brs] == "1" for r in rows), bool, n)
        c_dir = col.get("dir")           # optional: logs written by the live measurement mark frames this PC sent
        tx = (np.fromiter((len(r) > c_dir and r[c_dir] == "tx" for r in rows), bool, n) if c_dir is not None
              else np.zeros(n, bool))
        buf = bytearray(n * 64)
        for i, r in enumerate(rows):
            b = bytes.fromhex(r[c_data])[:64]
            buf[i * 64:i * 64 + len(b)] = b
        data = np.frombuffer(buf, np.uint8).reshape(n, 64)

        order = np.argsort(t, kind="stable")
        self.t0 = float(t[order[0]])
        self.t = t[order] - self.t0
        self.iface, self.can_id, self.length = iface[order], can_id[order], length[order]
        self.brs, self.err, self.data, self.tx = brs[order], err[order], data[order], tx[order]
        self.ifaces = names
        self.n = n
        self.duration = float(self.t[-1])
        self.groups = self._group()

    def _group(self):
        typ = self.can_id >> 16
        d0, d1, d2 = (self.data[:, k].astype(np.int64) for k in (0, 1, 2))
        has_sub = ((typ == 0xF0) | (typ == 0xF1)) & (self.length > 0)
        sub = np.where(has_sub, d0, -1)
        g2 = np.where((typ == 0xF1) & (sub == 0x09) & (self.length >= 3), d1, -1)
        point = np.where(g2 == 0, d2, -1)
        key = ((self.iface.astype(np.int64) << 29 | self.can_id) << 27) | ((sub + 1) << 18) | ((g2 + 1) << 9) | (point + 1)
        ok = np.flatnonzero(~self.err)
        uniq, inv = np.unique(key[ok], return_inverse=True)
        order = np.argsort(inv, kind="stable")
        bounds = np.concatenate(([0], np.cumsum(np.bincount(inv, minlength=len(uniq)))))
        groups = []
        for k in range(len(uniq)):
            idx = ok[order[bounds[k]:bounds[k + 1]]]
            i = idx[0]
            s = None
            if sub[i] >= 0:
                s = (int(sub[i]),) if g2[i] < 0 else (int(sub[i]), int(g2[i]), int(point[i]) if point[i] >= 0 else None)
            g = Group(self.ifaces[self.iface[i]], int(self.can_id[i]), s, idx)
            g.tx = int(self.tx[idx].sum())
            groups.append(g)
        groups.sort(key=lambda g: (g.iface, g.can_id, str(g.sub)))
        return groups

    def payload(self, i):
        return bytes(self.data[i, :self.length[i]])


class Session:
    """A capture together with the interface-to-DBC mapping."""

    def __init__(self, capture, dbc_dir=None):
        self.cap = capture
        self.dbc_dir = Path(dbc_dir or DBC_DIR)
        self.chmap = {}                  # iface -> Path of its DBC file
        self._dbs = {}
        self._series = {}
        self._refresh()

    def set_channel(self, iface, spec):
        """Map an interface to a DBC (channel name or file path), or None to clear it.

        The file is loaded first, so a DBC that fails to parse leaves the current mapping untouched.
        """
        if spec:
            path = resolve_dbc(spec, self.dbc_dir)
            if str(path) not in self._dbs:
                self._dbs[str(path)] = cantools.database.load_file(path)
            self.chmap[iface] = path
        else:
            self.chmap.pop(iface, None)
        self._series.clear()
        self._refresh()

    def reload(self):
        """Read every mapped DBC file again, for when a file changed on disk."""
        fresh = {str(p): cantools.database.load_file(p) for p in set(self.chmap.values())}
        self._dbs = fresh
        self._series.clear()
        self._refresh()

    def db(self, iface):
        path = self.chmap.get(iface)
        return self._dbs.get(str(path)) if path else None

    def channel_name(self, iface):
        path = self.chmap.get(iface)
        return path.stem if path else None

    def rates(self, iface):
        """(arbitration, data) bit rates in Mbps from the DBC attributes, or 1 / 5 when they are missing."""
        db = self.db(iface)
        out = []
        for name, default in (("Baudrate", 1.0), ("BaudRateCANFD", 5.0)):
            attr = db.dbc.attributes.get(name) if db is not None and db.dbc else None
            out.append(float(attr.value) / 1e6 if attr is not None and attr.value else default)
        return tuple(out)

    def _refresh(self):
        for g in self.cap.groups:
            db = self.db(g.iface)
            g.msg = None
            if db is not None:
                try:
                    g.msg = db.get_message_by_frame_id(g.can_id)
                except KeyError:
                    pass
            g.part = label(g.msg, g.sub, g.type)
            g.design = design_period(g.msg, g.sub)

    # ------------------------------------------------------------ statistics
    def _slice(self, idx, lo, hi):
        if lo is None:
            return idx
        t = self.cap.t[idx]
        return idx[np.searchsorted(t, lo, "left"):np.searchsorted(t, hi, "right")]

    def period_stats(self, g, lo=None, hi=None, limit=5.0):
        """Receive-interval statistics of one group, optionally within [lo, hi] seconds."""
        idx = self._slice(g.idx, lo, hi)
        st = PeriodStats(count=len(idx), design=g.design)
        if len(idx):
            lens, counts = np.unique(self.cap.length[idx], return_counts=True)
            st.lengths = {int(a): int(b) for a, b in zip(lens, counts)}
        if len(idx) < 2:
            return st
        t = self.cap.t[idx]
        gaps = np.diff(t) * 1e3
        st.gaps_ms, st.gap_t = gaps, t[1:]
        st.mean, st.min, st.max = float(gaps.mean()), float(gaps.min()), float(gaps.max())
        st.p99 = float(np.percentile(gaps, 99))
        st.t_max = float(t[1:][int(gaps.argmax())])
        if g.design:
            st.ratio = st.max / g.design
            st.over = int((gaps > limit * g.design).sum())
            st.flagged = st.over > 0
        return st

    def bus_load(self, iface, lo=None, hi=None):
        """Return (load percent, frames per second, error frames) of one interface."""
        cap = self.cap
        sel = np.flatnonzero(cap.iface == cap.ifaces.index(iface))
        sel = self._slice(sel, lo, hi)
        dur = (hi - lo) if lo is not None else cap.duration
        if not len(sel) or dur <= 0:
            return 0.0, 0.0, 0
        err = int(cap.err[sel].sum())
        sel = sel[~cap.err[sel]]
        arb, data = self.rates(iface)
        dlc = np.asarray(FD)[np.searchsorted(FD, cap.length[sel])]
        crc = np.where(dlc <= 16, 17, 21)
        rate = np.where(cap.brs[sel], data, arb)
        us = 49 / arb + (5 + 8 * dlc + 4 + crc + 6) / rate
        return float(us.sum() / 1e6 / dur * 100), len(sel) / dur, err

    # ------------------------------------------------------------ signals
    @staticmethod
    def sig_matches(sig, sub):
        """True when a multiplexed signal is present in frames of the given SubCode part."""
        mux = sig.multiplexer_signal
        if mux is None:
            return True
        if sub is None:
            return False
        if mux == "SubCode":
            return sub[0] in sig.multiplexer_ids
        if mux == "G2_Type":
            return sub[0] == 0x09 and len(sub) > 1 and sub[1] in sig.multiplexer_ids
        return False

    def signals_for(self, g):
        """Value signals carried by a group (multiplexer selectors are left out)."""
        if g.msg is None:
            return []
        return [s for s in g.msg.signals if not s.is_multiplexer and self.sig_matches(s, g.sub)]

    def series(self, iface, can_id, name):
        """Time series of one signal over the whole capture."""
        key = (iface, can_id, name)
        if key in self._series:
            return self._series[key]
        groups = [g for g in self.cap.groups if g.iface == iface and g.can_id == can_id and g.msg is not None]
        if not groups:
            raise KeyError(f"{iface} 0x{can_id:06X} 메시지가 캡처에 없거나 DBC가 지정되지 않았습니다.")
        sig = groups[0].msg.get_signal_by_name(name)
        parts = [g.idx for g in groups if self.sig_matches(sig, g.sub)]
        idx = np.sort(np.concatenate(parts)) if parts else np.zeros(0, np.int64)
        need = (sig.start + sig.length + 7) // 8
        idx = idx[self.cap.length[idx] >= need]
        y = extract(sig, self.cap.data[idx])
        out = Series(self.cap.t[idx], y, idx, sig)
        self._series[key] = out
        return out

    def decode(self, i):
        """Decode one frame into [(signal name, text)] for display."""
        cap = self.cap
        db = self.db(cap.ifaces[cap.iface[i]])
        if db is None or cap.err[i]:
            return []
        try:
            msg = db.get_message_by_frame_id(int(cap.can_id[i]))
            values = msg.decode(cap.payload(i), decode_choices=True, allow_truncated=True, allow_excess=True)
        except Exception:  # noqa: BLE001 - unknown ID or a frame the DBC cannot describe
            return []
        units = {s.name: s.unit for s in msg.signals}
        return [(k, fmt_value(v, units.get(k))) for k, v in values.items()]


def fmt_value(v, unit=None):
    if isinstance(v, float):
        text = f"{v:.4f}"
    else:
        text = str(v)
    return f"{text} {unit}" if unit else text


def extract(sig, data):
    """Vectorised decode of one little-endian signal from an (n, 64) byte array."""
    if sig.byte_order != "little_endian":
        raise NotImplementedError("big-endian signals are not used in the FM26 DBC files")
    b0, shift = divmod(sig.start, 8)
    nbytes = (shift + sig.length + 7) // 8
    if nbytes > 8:
        raise NotImplementedError("signals wider than 64 bits including the bit offset are not supported")
    raw = np.zeros(len(data), np.uint64)
    for k in range(nbytes):
        raw |= data[:, b0 + k].astype(np.uint64) << np.uint64(8 * k)
    raw >>= np.uint64(shift)
    if sig.length < 64:
        raw &= np.uint64((1 << sig.length) - 1)
    if sig.is_float:
        y = raw.astype(np.uint32).view(np.float32).astype(float) if sig.length == 32 else raw.view(np.float64)
    elif sig.is_signed:
        y = raw.astype(np.int64)
        if sig.length < 64:
            y = np.where(y >= (1 << (sig.length - 1)), y - (1 << sig.length), y)
        y = y.astype(float)
    else:
        y = raw.astype(float)
    return y * float(sig.scale) + float(sig.offset)
