"""Shared application state: the open session, playback time, cursors, saved ranges and plotted signals."""
from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import QObject, pyqtSignal

PALETTE = ["#1f6f9f", "#b85a17", "#65499c", "#2b8455", "#bd3626", "#8a6d1c", "#3d7f7a", "#9a3f7a"]


@dataclass(eq=False)
class SigRef:
    """One signal picked for the graph."""
    iface: str
    can_id: int
    name: str
    color: str
    series: object

    @property
    def key(self):
        return (self.iface, self.can_id, self.name)

    @property
    def is_enum(self):
        return bool(self.series.sig.choices)


class AppState(QObject):
    sessionChanged = pyqtSignal()     # a capture was opened
    mappingChanged = pyqtSignal()     # interface-to-DBC mapping changed
    timeChanged = pyqtSignal()        # playback time moved
    cursorsChanged = pyqtSignal()
    rangesChanged = pyqtSignal()
    signalsChanged = pyqtSignal()
    gapGroupChanged = pyqtSignal()
    jumpRequested = pyqtSignal(float)  # show this time in the graph

    def __init__(self):
        super().__init__()
        self.session = None
        self.cur_t = 0.0
        self.cursor_a = 0.0
        self.cursor_b = 0.0
        self.ranges = []              # [(name, lo, hi)]
        self.signals = []             # [SigRef]
        self.gap_group = None

    # ------------------------------------------------------------ session
    def set_session(self, session):
        self.session = session
        self.signals, self.ranges, self.gap_group = [], [], None
        dur = session.cap.duration
        self.cur_t, self.cursor_a, self.cursor_b = dur, dur * 0.25, dur * 0.75
        self.sessionChanged.emit()

    def set_channel(self, iface, spec):
        """Change the DBC of one interface. Raises when the file cannot be loaded; nothing changes then."""
        self.session.set_channel(iface, spec)
        self._after_dbc_change()

    def reload_dbc(self):
        self.session.reload()
        self._after_dbc_change()

    def _after_dbc_change(self):
        """Keep plotted signals that still exist in the new DBC, drop the rest."""
        keys = [s.key for s in self.signals]
        self.signals = []
        for key in keys:
            try:
                self.add_signal(*key)
            except KeyError:
                pass
        if self.gap_group is not None and self.gap_group.msg is None:
            self.gap_group = None
        self.mappingChanged.emit()
        self.signalsChanged.emit()
        self.gapGroupChanged.emit()

    # ------------------------------------------------------------ time, cursors, ranges
    def set_time(self, t):
        self.cur_t = min(max(t, 0.0), self.session.cap.duration)
        self.timeChanged.emit()

    def set_cursors(self, a, b):
        if (a, b) != (self.cursor_a, self.cursor_b):
            self.cursor_a, self.cursor_b = a, b
            self.cursorsChanged.emit()

    def cursor_range(self):
        return tuple(sorted((self.cursor_a, self.cursor_b)))

    def save_range(self, name):
        lo, hi = self.cursor_range()
        self.ranges.append((name, lo, hi))
        self.rangesChanged.emit()

    def delete_range(self, i):
        del self.ranges[i]
        self.rangesChanged.emit()

    # ------------------------------------------------------------ signals
    def has_signal(self, iface, can_id, name):
        return any(s.key == (iface, can_id, name) for s in self.signals)

    def add_signal(self, iface, can_id, name):
        if self.has_signal(iface, can_id, name):
            return
        used = {s.color for s in self.signals}
        color = next((c for c in PALETTE if c not in used), PALETTE[len(self.signals) % len(PALETTE)])
        self.signals.append(SigRef(iface, can_id, name, color, self.session.series(iface, can_id, name)))
        self.signalsChanged.emit()

    def remove_signal(self, iface, can_id, name):
        self.signals = [s for s in self.signals if s.key != (iface, can_id, name)]
        self.signalsChanged.emit()

    def set_gap_group(self, group):
        self.gap_group = group
        self.gapGroupChanged.emit()
