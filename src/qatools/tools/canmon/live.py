"""Live measurement without Qt: frame sources, running statistics, CSV logging and the reader thread."""
from __future__ import annotations

import csv
import math
import select
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import cantools
import numpy as np

from . import sockcan
from .core import FD, Group, design_period, frame_us, key_of, label, resolve_dbc

HIST_LO, HIST_PER_DECADE, HIST_BINS = -2.0, 8, 48        # gap histogram: 0.01 ms to 10 s, log spaced
HIST_EDGES = [10 ** (HIST_LO + k / HIST_PER_DECADE) for k in range(HIST_BINS + 1)]


@dataclass
class Frame:
    t_hw: float | None
    t_sw: float
    iface: str
    can_id: int
    ext: int
    fd: int
    brs: int
    esi: int
    err: int
    length: int
    data: bytes
    dir: str = ""                       # "tx" for frames this PC sent

    def row(self):
        return ["" if self.t_hw is None else f"{self.t_hw:.9f}", f"{self.t_sw:.9f}", self.iface, f"0x{self.can_id:X}",
                self.ext, self.fd, self.brs, self.esi, self.err, self.length, self.data.hex(), self.dir]


# ---------------------------------------------------------------- sources
class SocketSource:
    """Frames from SocketCAN interfaces.

    Writes happen only through send(), which the message-transmit screen calls after the user armed it. The frame is
    sent on the same socket that receives, so its echo comes back flagged and is logged as Tx with the bus timestamp.
    """

    def __init__(self, ifaces):
        self.ifaces = list(ifaces)
        self.socks = {sockcan.open_socket(i, blocking=False, own=True): i for i in self.ifaces}
        self.by_iface = {i: s for s, i in self.socks.items()}
        self.dropped = {i: 0 for i in self.ifaces}

    def send(self, frame):
        """Write one tx.TxFrame. Raises BlockingIOError when the transmit queue is full."""
        s = self.by_iface.get(frame.iface)
        if s is None:
            raise OSError(f"{frame.iface}는 측정 중인 채널이 아닙니다.")
        s.send(sockcan.pack_frame(frame.can_id, frame.ext, frame.fd, frame.brs, frame.data))

    def read(self, timeout):
        ready, _, _ = select.select(list(self.socks), [], [], timeout)
        out = []
        for s in ready:
            iface = self.socks[s]
            for _ in range(2000):                     # drain, but let the other sockets have a turn
                try:
                    raw, anc, flags, _ = s.recvmsg(sockcan.FRAME.size, sockcan.ANC_SPACE)
                except BlockingIOError:
                    break
                hw, sw, dropped = sockcan.parse_anc(anc)
                if dropped is not None:
                    self.dropped[iface] = dropped
                f = sockcan.parse_frame(raw)
                own = "tx" if flags & (sockcan.MSG_CONFIRM | sockcan.MSG_DONTROUTE) else ""
                out.append(Frame(hw, sw, iface, f["can_id"], f["ext"], f["fd"], f["brs"], f["esi"], f["err"], f["len"],
                                 bytes.fromhex(f["data"]), own))
        out.sort(key=lambda f: f.t_hw if f.t_hw is not None else f.t_sw)
        return out

    def close(self):
        for s in self.socks:
            s.close()


class ReplaySource:
    """Plays a capture CSV at its recorded pace. For trying the live screens and for tests, not a measurement.

    send() does not reach any bus: the frame is looped back into the stream as Tx, as a real channel would echo it.
    """

    def __init__(self, path, speed=1.0):
        self.speed = speed
        self.frames = []
        with open(path, newline="") as f:
            r = csv.DictReader(f)
            for row in r:
                self.frames.append(Frame(float(row["t_hw"]) if row["t_hw"] else None, float(row["t_sw"]), row["iface"],
                                         int(row["can_id"], 16), int(row["ext"]), int(row["fd"]), int(row["brs"]),
                                         int(row["esi"]), int(row["err"]), int(row["len"]), bytes.fromhex(row["data"]),
                                         row.get("dir") or ""))
        self.ifaces = sorted({f.iface for f in self.frames})
        self.dropped = {i: 0 for i in self.ifaces}
        self.pos = 0
        self.start = None
        self.t0 = self._t(self.frames[0]) if self.frames else 0.0
        self.hw = any(f.t_hw is not None for f in self.frames)
        self.looped = []
        self.loop_lock = threading.Lock()
        self.sent = 0

    def _now(self):
        return self.t0 + (time.monotonic() - self.start) * self.speed if self.start is not None else self.t0

    def send(self, frame):
        t = self._now()
        f = Frame(t if self.hw else None, t, frame.iface, frame.can_id, int(frame.ext), int(frame.fd), int(frame.brs), 0, 0,
                  len(frame.data), bytes(frame.data), "tx")
        with self.loop_lock:
            self.looped.append(f)
            self.sent += 1

    def _take_looped(self):
        with self.loop_lock:
            out, self.looped = self.looped, []
        return out

    @staticmethod
    def _t(f):
        return f.t_hw if f.t_hw is not None else f.t_sw

    @property
    def finished(self):
        return self.pos >= len(self.frames)

    def read(self, timeout):
        if self.start is None:
            self.start = time.monotonic()
        looped = self._take_looped()
        if self.finished:
            if not looped:
                time.sleep(min(timeout, 0.002))
            return looped
        due = self._now()
        end = self.pos
        while end < len(self.frames) and self._t(self.frames[end]) <= due:
            end += 1
        if end == self.pos and not looped:
            time.sleep(min(timeout, 0.002))
            return []
        out, self.pos = self.frames[self.pos:end], end
        if looped:
            out = sorted(out + looped, key=self._t)
        return out

    def close(self):
        pass


# ---------------------------------------------------------------- DBC lookup for live groups
class LiveMeta:
    """DBC of each interface, with the lookups the live screens and the statistics need."""

    def __init__(self, mapping, dbc_dir=None):
        self.paths, self.dbs = {}, {}
        self.errors = []
        for iface, spec in (mapping or {}).items():
            if not spec:
                continue
            path = resolve_dbc(spec, dbc_dir)
            try:
                self.dbs[iface] = cantools.database.load_file(path)
                self.paths[iface] = path
            except Exception as e:  # noqa: BLE001 - measure without a DBC rather than refuse to start
                self.errors.append(f"{iface}: {path}\n{e}")

    def group(self, iface, can_id, sub):
        """A core.Group carrying name, part and design period for one live key."""
        g = Group(iface, can_id, sub, np.zeros(0, np.int64))
        db = self.dbs.get(iface)
        if db is not None:
            try:
                g.msg = db.get_message_by_frame_id(can_id)
            except KeyError:
                pass
        g.part = label(g.msg, sub, can_id >> 16)
        g.design = design_period(g.msg, sub)
        return g

    def design(self, iface, can_id, sub):
        return self.group(iface, can_id, sub).design

    def rates(self, iface):
        db = self.dbs.get(iface)
        out = []
        for name, default in (("Baudrate", 1.0), ("BaudRateCANFD", 5.0)):
            attr = db.dbc.attributes.get(name) if db is not None and db.dbc else None
            out.append(float(attr.value) / 1e6 if attr is not None and attr.value else default)
        return tuple(out)

    def decode(self, iface, can_id, payload):
        db = self.dbs.get(iface)
        if db is None:
            return []
        try:
            msg = db.get_message_by_frame_id(can_id)
            values = msg.decode(payload, decode_choices=True, allow_truncated=True, allow_excess=True)
        except Exception:  # noqa: BLE001
            return []
        units = {s.name: s.unit for s in msg.signals}
        out = []
        for k, v in values.items():
            text = f"{v:.4f}" if isinstance(v, float) else str(v)
            out.append((k, f"{text} {units[k]}" if units.get(k) else text))
        return out

    def channel_name(self, iface):
        p = self.paths.get(iface)
        return p.stem if p else None


# ---------------------------------------------------------------- running statistics
@dataclass
class LiveGroup:
    iface: str
    can_id: int
    sub: tuple | None
    design: float | None = None
    count: int = 0
    last_t: float = 0.0
    last_gap: float | None = None       # ms
    last_len: int = 0
    last_data: bytes = b""
    gmin: float = math.inf
    gmax: float = 0.0
    gsum: float = 0.0
    t_max: float | None = None
    over: int = 0
    hist: list = field(default_factory=lambda: [0] * HIST_BINS)
    lengths: dict = field(default_factory=dict)
    tx: int = 0                         # frames of this message sent by this PC

    @property
    def key(self):
        return (self.iface, self.can_id, self.sub)

    @property
    def mean(self):
        return self.gsum / (self.count - 1) if self.count > 1 else None

    def p99(self):
        """Upper edge of the histogram bin that holds the 99th percentile (an upper estimate)."""
        total = self.count - 1
        if total < 1:
            return None
        need, acc = 0.99 * total, 0
        for k, c in enumerate(self.hist):
            acc += c
            if acc >= need:
                return min(HIST_EDGES[k + 1], self.gmax)
        return self.gmax

    def copy(self):
        g = LiveGroup(**{k: getattr(self, k) for k in ("iface", "can_id", "sub", "design", "count", "last_t", "last_gap",
                                                       "last_len", "last_data", "gmin", "gmax", "gsum", "t_max", "over", "tx")})
        g.hist, g.lengths = list(self.hist), dict(self.lengths)
        return g


@dataclass
class IfaceCounters:
    frames: int = 0
    errors: int = 0
    busy_us: float = 0.0
    tx: int = 0


class LiveStats:
    """Per-message receive statistics, updated frame by frame. Thread safe through one lock."""

    def __init__(self, meta, limit=5.0):
        self.meta, self.limit = meta, limit
        self.lock = threading.Lock()
        self.groups = {}
        self.ifaces = {}
        self.t0 = None
        self.t_last = 0.0
        self.t_reset = 0.0
        self.total = 0
        self.all_hw = True
        self._cost = {}

    def _frame_cost(self, iface, length, brs):
        key = (iface, length, brs)
        c = self._cost.get(key)
        if c is None:
            arb, data = self.meta.rates(iface)
            c = self._cost[key] = frame_us(min(length, 64), arb, data, brs)
        return c

    def update(self, frames):
        with self.lock:
            for f in frames:
                t_abs = f.t_hw if f.t_hw is not None else f.t_sw
                if f.t_hw is None and not f.err:
                    self.all_hw = False
                if self.t0 is None:
                    self.t0 = t_abs
                t = t_abs - self.t0
                self.t_last = t
                self.total += 1
                ic = self.ifaces.get(f.iface)
                if ic is None:
                    ic = self.ifaces[f.iface] = IfaceCounters()
                if f.err:
                    ic.errors += 1
                    continue
                ic.frames += 1
                ic.busy_us += self._frame_cost(f.iface, f.length, f.brs)
                can_id, sub = key_of(f.can_id, f.data)
                key = (f.iface, can_id, sub)
                g = self.groups.get(key)
                if g is None:
                    g = self.groups[key] = LiveGroup(f.iface, can_id, sub, self.meta.design(f.iface, can_id, sub))
                elif g.count:
                    gap = (t - g.last_t) * 1e3
                    g.last_gap = gap
                    g.gsum += gap
                    if gap < g.gmin:
                        g.gmin = gap
                    if gap > g.gmax:
                        g.gmax, g.t_max = gap, t
                    if g.design and gap > self.limit * g.design:
                        g.over += 1
                    k = int((math.log10(gap) - HIST_LO) * HIST_PER_DECADE) if gap > 0 else 0
                    g.hist[min(max(k, 0), HIST_BINS - 1)] += 1
                g.count += 1
                if f.dir:
                    g.tx += 1
                    ic.tx += 1
                g.last_t, g.last_len, g.last_data = t, f.length, f.data
                g.lengths[f.length] = g.lengths.get(f.length, 0) + 1

    def reset(self):
        """Start the statistics again from now. Frames keep being logged."""
        with self.lock:
            self.groups = {}
            self.t_reset = self.t_last
            for ic in self.ifaces.values():
                ic.frames, ic.errors, ic.busy_us, ic.tx = 0, 0, 0.0, 0

    def set_limit(self, limit):
        """Change the over-limit rule. It applies to frames received from now on."""
        with self.lock:
            self.limit = limit

    def snapshot(self):
        """Copies for the UI: ([LiveGroup], {iface: IfaceCounters}, elapsed seconds, seconds since reset)."""
        with self.lock:
            groups = [g.copy() for g in self.groups.values()]
            ifaces = {k: IfaceCounters(v.frames, v.errors, v.busy_us, v.tx) for k, v in self.ifaces.items()}
            return groups, ifaces, self.t_last, self.t_last - self.t_reset


# ---------------------------------------------------------------- session
class LiveSession:
    """Reader thread: source -> log file and statistics."""

    def __init__(self, source, meta, log_path=None, limit=5.0):
        self.source, self.meta = source, meta
        self.stats = LiveStats(meta, limit)
        self.log_path = Path(log_path) if log_path else None
        self.error = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="canmon-reader", daemon=True)
        self._file = None

    def start(self):
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self.log_path, "w", newline="")
            self._writer = csv.writer(self._file)
            self._writer.writerow(sockcan.LOG_COLUMNS)
        self._thread.start()

    def _run(self):
        last_flush = time.monotonic()
        try:
            while not self._stop.is_set():
                frames = self.source.read(0.1)
                if frames:
                    if self._file is not None:
                        self._writer.writerows(f.row() for f in frames)
                    self.stats.update(frames)
                if self._file is not None and time.monotonic() - last_flush > 1.0:
                    self._file.flush()
                    last_flush = time.monotonic()
        except Exception as e:  # noqa: BLE001 - report to the UI instead of dying silently
            self.error = f"{type(e).__name__}: {e}"

    @property
    def running(self):
        return self._thread.is_alive()

    def dropped(self):
        return dict(getattr(self.source, "dropped", {}))

    def stop(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)
        self.source.close()
        if self._file is not None:
            self._file.close()
            self._file = None
