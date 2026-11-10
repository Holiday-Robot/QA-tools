"""Message transmission without Qt: transmit list entries, DBC encoding, and the scheduler thread.

Nothing here decides whether sending is allowed. The UI arms transmission explicitly and checks that the
channel is configured for transmit; the scheduler only sends what it is given and stops on request.
"""
from __future__ import annotations

import errno
import itertools
import threading
import time
from dataclasses import asdict, dataclass, field

from .core import FD, frame_us, key_of, sub_attr, sub_key

MAX_FPS = 20_000               # per entry; above this Python cannot keep the timing anyway
MAX_BURST = 64                 # frames one entry may send in one scheduler pass when it is behind
RESYNC_S = 0.05                # if an entry falls this far behind, skip ahead instead of bursting
FULL_BACKOFF_S = 0.001         # wait after the kernel transmit queue was full
KIND_MSG, KIND_RAW, KIND_LOAD = "dbc", "raw", "load"
LOAD_ID = 0x1FFFFF00           # default dummy ID: 29-bit, near the lowest priority, not used by the FM26 DBCs


def fd_len(n):
    """Smallest valid CAN FD payload size that holds n bytes."""
    return next(b for b in FD if b >= n)


@dataclass
class TxEntry:
    """One row of the transmit list."""
    iface: str = ""
    kind: str = KIND_MSG                 # dbc: encoded from a DBC message, raw: bytes as typed, load: dummy frames
    message: str = ""                    # DBC message name (kind dbc)
    values: dict = field(default_factory=dict)  # signal values (kind dbc); missing signals use their defaults
    can_id: int = 0
    ext: bool = True
    fd: bool = True
    brs: bool = True
    data: bytes = b""
    length: int = 0                      # payload length for raw and load entries (data is padded with zeros)
    period_ms: float = 0.0               # 0 sends once
    count: int = 0                       # 0 means no limit for periodic entries
    load_pct: float = 10.0               # target added bus load (kind load)
    note: str = ""

    def to_dict(self):
        d = asdict(self)
        d["data"] = self.data.hex()
        return d

    @classmethod
    def from_dict(cls, d):
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        if isinstance(known.get("data"), str):
            known["data"] = bytes.fromhex(known["data"])
        known["values"] = dict(known.get("values") or {})
        return cls(**known)

    def copy(self):
        return TxEntry.from_dict(self.to_dict())


# ---------------------------------------------------------------- DBC encoding
def active_signals(msg, values):
    """Signals that are encoded for the current multiplexer values (all signals of a plain message).

    Follows nested multiplexing: a multiplexer inside a SubCode only counts when its own parent selects it.
    """
    active = {s.name: s for s in msg.signals if s.multiplexer_ids is None}
    changed = True
    while changed:
        changed = False
        for s in msg.signals:
            if s.name in active or s.multiplexer_signal not in active:
                continue
            parent = active[s.multiplexer_signal]
            if _num_value(parent, values.get(parent.name, default_value(parent))) in s.multiplexer_ids:
                active[s.name] = s
                changed = True
    return [s for s in msg.signals if s.name in active]


def _num_value(sig, v):
    """Raw choice number for a value given as a choice name; numbers pass through."""
    if isinstance(v, str) and sig.choices:
        for num, name in sig.choices.items():
            if str(name) == v:
                return num
    try:
        return int(v) if float(v).is_integer() else float(v)
    except (TypeError, ValueError):
        return v


def default_value(sig):
    """Initial value from the DBC, else 0 clamped into the signal's range; multiplexers start at their first id."""
    if sig.is_multiplexer and sig.choices:
        return min(sig.choices)
    if sig.initial is not None:
        return sig.initial
    v = 0
    if sig.minimum is not None and v < sig.minimum:
        v = sig.minimum
    if sig.maximum is not None and v > sig.maximum:
        v = sig.maximum
    return v


def encode(msg, values):
    """Payload for a DBC message. Raises ValueError with a readable reason when a value does not fit."""
    full = {}
    for s in active_signals(msg, values):
        full[s.name] = _num_value(s, values.get(s.name, default_value(s)))
    try:
        payload = msg.encode(full, scaling=True, padding=False, strict=True)
    except Exception as e:  # noqa: BLE001 - cantools raises several types; the message is what matters
        raise ValueError(str(e)) from e
    can_id, sub = key_of(msg.frame_id, payload)
    if sub is not None:
        n = sub_attr(msg, "SubCodeLengths").get(sub_key(sub))
        if n:
            payload = payload[:int(n)]
    return bytes(payload)


# ---------------------------------------------------------------- frames to send
@dataclass
class TxFrame:
    iface: str
    can_id: int
    ext: bool
    fd: bool
    brs: bool
    data: bytes


def build(entry, db=None):
    """TxFrame for one entry. db is the cantools database of the entry's channel (needed for kind dbc)."""
    if entry.kind == KIND_MSG:
        if db is None:
            raise ValueError(f"{entry.iface}에 DBC가 없습니다.")
        try:
            msg = db.get_message_by_name(entry.message)
        except KeyError:
            raise ValueError(f"DBC에 {entry.message} 메시지가 없습니다.") from None
        data = encode(msg, entry.values)
        frame = TxFrame(entry.iface, msg.frame_id, msg.is_extended_frame, msg.is_fd or len(data) > 8, entry.brs and msg.is_fd, data)
    else:
        data = bytes(entry.data)
        if entry.length:
            data = data[:entry.length].ljust(entry.length, b"\0")
        frame = TxFrame(entry.iface, entry.can_id, entry.ext, entry.fd or len(data) > 8, entry.brs and entry.fd, data)
    limit = 0x1FFFFFFF if frame.ext else 0x7FF
    if not 0 <= frame.can_id <= limit:
        raise ValueError(f"CAN ID 0x{frame.can_id:X}가 {'29' if frame.ext else '11'}비트 범위를 넘습니다.")
    if len(frame.data) > (64 if frame.fd else 8):
        raise ValueError(f"데이터가 {len(frame.data)} 바이트로 너무 깁니다.")
    if frame.fd:
        frame.data = frame.data.ljust(fd_len(len(frame.data)), b"\0")
    return frame


def frame_time_us(frame, rates):
    """Nominal bus time of one frame. rates = (arbitration Mbps, data Mbps)."""
    arb, data = rates
    return frame_us(len(frame.data), arb, data if frame.fd else arb, frame.brs)


def period_s(entry, frame, rates):
    """Seconds between frames, or 0 for a single shot. Load entries derive it from the target load."""
    if entry.kind == KIND_LOAD:
        pct = min(max(entry.load_pct, 0.1), 100.0)
        p = frame_time_us(frame, rates) * 1e-6 / (pct / 100)
    else:
        p = max(entry.period_ms, 0.0) / 1000
    return max(p, 1 / MAX_FPS) if p > 0 else 0.0


def added_load(entry, frame, rates):
    """Bus load in % this entry adds while it runs (0 for a single shot)."""
    p = period_s(entry, frame, rates)
    return frame_time_us(frame, rates) * 1e-6 / p * 100 if p else 0.0


# ---------------------------------------------------------------- scheduler
@dataclass
class Slot:
    frame: TxFrame
    period: float
    count: int
    start: float
    due: float
    sent: int = 0
    full: int = 0
    skipped: int = 0
    done: bool = False
    reason: str = ""                     # why it stopped: count, once, auto, stopped, error text

    def status(self):
        return dict(sent=self.sent, full=self.full, skipped=self.skipped, done=self.done, reason=self.reason)


class Transmitter:
    """Sends frames on a schedule from one thread. sink.send(TxFrame) does the actual write.

    auto_stop_s stops every periodic entry that has been running that long (0 disables it).
    """

    def __init__(self, sink, auto_stop_s=60.0, clock=time.monotonic):
        self.sink, self.auto_stop_s, self.clock = sink, auto_stop_s, clock
        self.lock = threading.Lock()
        self.slots = {}
        self.error = None
        self._ids = itertools.count(1)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="canmon-tx", daemon=True)
        self._thread.start()

    def start(self, key, frame, period, count=0):
        """Start (or replace) sending one entry. period 0 sends it once."""
        now = self.clock()
        with self.lock:
            self.slots[key] = Slot(frame, period, count if period else 1, now, now)
        self._wake.set()

    def update(self, key, frame, period, count=0):
        """Change a running entry without restarting its counters or auto-stop timer."""
        with self.lock:
            s = self.slots.get(key)
            if s is None or s.done:
                return False
            s.frame, s.count = frame, count if period else 1
            if period != s.period:
                s.period, s.due = period, self.clock()
        self._wake.set()
        return True

    def once(self, frame):
        key = ("once", next(self._ids))
        self.start(key, frame, 0.0)
        return key

    def stop(self, key, reason="stopped"):
        with self.lock:
            s = self.slots.get(key)
            if s is not None and not s.done:
                s.done, s.reason = True, reason

    def stop_all(self):
        with self.lock:
            for s in self.slots.values():
                if not s.done:
                    s.done, s.reason = True, "stopped"

    def running(self, key):
        with self.lock:
            s = self.slots.get(key)
            return s is not None and not s.done

    def any_running(self):
        with self.lock:
            return any(not s.done for s in self.slots.values())

    def status(self):
        with self.lock:
            return {k: s.status() for k, s in self.slots.items()}

    def close(self):
        """Stop sending and end the thread."""
        self.stop_all()
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=2)

    @property
    def alive(self):
        return self._thread.is_alive()

    def _send_due(self, s, now):
        for _ in range(MAX_BURST):
            if s.due > now:
                return
            try:
                self.sink.send(s.frame)
            except (BlockingIOError, InterruptedError):
                s.full += 1
                s.due = now + FULL_BACKOFF_S
                return
            except OSError as e:
                if e.errno in (errno.ENOBUFS, errno.EAGAIN):
                    s.full += 1
                    s.due = now + FULL_BACKOFF_S
                    return
                s.done, s.reason = True, f"송신 오류: {e}"
                return
            s.sent += 1
            if s.period == 0 or (s.count and s.sent >= s.count):
                s.done, s.reason = True, "once" if s.period == 0 else "count"
                return
            s.due += s.period

    def _run(self):
        try:
            while not self._stop.is_set():
                self._wake.clear()          # before scanning, so a start() during the scan is not lost
                next_due = None
                with self.lock:
                    now = self.clock()
                    for s in self.slots.values():
                        if s.done:
                            continue
                        if self.auto_stop_s and s.period and now - s.start >= self.auto_stop_s:
                            s.done, s.reason = True, "auto"
                            continue
                        self._send_due(s, now)
                        if s.done:
                            continue
                        if s.period and now - s.due > RESYNC_S:
                            behind = int((now - s.due) / s.period)
                            s.skipped += behind
                            s.due += behind * s.period
                        next_due = s.due if next_due is None else min(next_due, s.due)
                wait = 0.2 if next_due is None else min(max(next_due - self.clock(), 0.0), 0.05)
                if wait > 0:
                    self._wake.wait(wait)
        except Exception as e:  # noqa: BLE001 - report to the UI instead of dying silently
            self.error = f"{type(e).__name__}: {e}"
            with self.lock:
                for s in self.slots.values():
                    if not s.done:
                        s.done, s.reason = True, self.error
