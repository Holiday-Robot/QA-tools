"""Live measurement screens: Trace and period statistics refreshed from the reader thread's statistics."""
from __future__ import annotations

import csv
import math

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor, QFont
from PyQt6.QtWidgets import (QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QTreeWidget,
                             QTreeWidgetItem, QVBoxLayout, QWidget)

from ..live import HIST_EDGES
from .period import FLAG_BG, FLAG_FG, NumItem
from .trace import FIXED_COLS, WARN, hexdump

P_COLS = ["채널", "CAN ID", "메시지", "수신 수", "평균 (ms)", "최소", "최대", "p99 (근사)", "설계", "최대/설계", "한계 초과"]


def mono():
    f = QFont("monospace")
    f.setStyleHint(QFont.StyleHint.Monospace)
    return f


class LiveTrace(QWidget):
    """Latest frame of every message, refreshed while measuring."""

    def __init__(self):
        super().__init__()
        self.meta = None
        self.items = {}                    # group key -> (item, core.Group)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("채널, CAN ID, 이름으로 거르기")
        self.filter.setClearButtonEnabled(True)
        self.info = QLabel("")
        bar = QHBoxLayout()
        bar.addWidget(QLabel("ID별 고정"))
        bar.addWidget(self.filter, 1)
        bar.addWidget(self.info)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(FIXED_COLS)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setFont(mono())
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tree.header().setStretchLastSection(True)
        for c, w in enumerate((90, 60, 90, 290, 190, 50, 80)):
            self.tree.setColumnWidth(c, w)
        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(self.tree, 1)
        self.filter.textChanged.connect(self._refilter)
        self.tree.itemExpanded.connect(self._fill_children)

    def begin(self, meta):
        self.meta = meta
        self.items = {}
        self.tree.clear()

    def _refilter(self):
        text = self.filter.text().strip().lower()
        for it, g in self.items.values():
            it.setHidden(bool(text) and text not in f"{g.iface} {g.title}".lower())

    def update_groups(self, groups):
        added = False
        for lg in groups:
            entry = self.items.get(lg.key)
            if entry is None:
                g = self.meta.group(lg.iface, lg.can_id, lg.sub)
                it = QTreeWidgetItem()
                it.setText(1, lg.iface)
                it.setText(2, f"0x{lg.can_id:06X}")
                it.setText(3, g.name if g.msg is not None else f"{g.name} (DBC에 없음)")
                it.setText(4, g.part)
                for c in (0, 5, 6):
                    it.setTextAlignment(c, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if g.msg is None:
                    for c in range(len(FIXED_COLS)):
                        it.setForeground(c, QBrush(WARN))
                else:
                    it.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
                it.setData(1, Qt.ItemDataRole.UserRole, (lg.iface, lg.can_id, str(lg.sub)))
                entry = self.items[lg.key] = (it, g)
                keys = sorted(self.items, key=lambda k: (k[0], k[1], str(k[2])))
                self.tree.insertTopLevelItem(keys.index(lg.key), it)
                added = True
            it = entry[0]
            if lg.tx and not it.text(1).endswith("Tx"):
                it.setText(1, f"{lg.iface} Tx")
                it.setForeground(1, QBrush(QColor("#1f6f9f")))
                it.setToolTip(1, "이 PC가 보낸 프레임이 들어 있습니다.")
            it.setText(0, f"{lg.last_t:.4f}")
            it.setText(5, str(lg.last_len))
            it.setText(6, "-" if lg.last_gap is None else f"{lg.last_gap:.2f}")
            it.setText(7, hexdump(lg.last_data))
            it.setData(0, Qt.ItemDataRole.UserRole, (lg.iface, lg.can_id, lg.last_data))
            if it.isExpanded():
                self._fill_children(it)
        if added:
            self._refilter()
        self.info.setText(f"메시지 {len(self.items)}개")

    def _fill_children(self, it):
        last = it.data(0, Qt.ItemDataRole.UserRole)
        values = self.meta.decode(*last) if last else []
        while it.childCount() > len(values):
            it.removeChild(it.child(it.childCount() - 1))
        while it.childCount() < len(values):
            QTreeWidgetItem(it)
        for k, (name, text) in enumerate(values):
            it.child(k).setText(3, name)
            it.child(k).setText(4, text)


class LivePeriod(QWidget):
    """Mean / min / max receive interval per message, refreshed while measuring."""

    def __init__(self):
        super().__init__()
        self.meta = None
        self.session = None
        self.rows = {}                     # group key -> row items
        self.groups = {}                   # group key -> latest LiveGroup
        self.names = {}

        self.limit = QDoubleSpinBox()
        self.limit.setRange(1.1, 100)
        self.limit.setValue(5.0)
        self.limit.setSingleStep(0.5)
        self.limit.setPrefix("설계 주기 × ")
        self.limit.setToolTip("바꾸면 그 뒤에 받은 프레임부터 적용됩니다.")
        self.summary = QLabel("")
        self.since = QLabel("")
        self.b_reset = QPushButton("통계 초기화")
        self.b_export = QPushButton("CSV 내보내기")
        bar = QHBoxLayout()
        bar.addWidget(QLabel("통계 범위: 측정 시작 또는 초기화 이후"))
        bar.addWidget(self.since)
        bar.addSpacing(12)
        bar.addWidget(QLabel("한계"))
        bar.addWidget(self.limit)
        bar.addWidget(self.summary)
        bar.addStretch(1)
        bar.addWidget(self.b_reset)
        bar.addWidget(self.b_export)

        self.table = QTableWidget(0, len(P_COLS))
        self.table.setHorizontalHeaderLabels(P_COLS)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.setFont(mono())

        self.title = QLabel("메시지를 선택하세요.")
        self.title.setWordWrap(True)
        self.hist = pg.PlotWidget()
        self.hist.setMinimumHeight(180)
        self.hist.setLabel("bottom", "수신 간격 (ms, 로그 눈금)")
        self.hist.setLabel("left", "프레임 수 (로그)")
        self.hist.setMouseEnabled(x=False, y=False)
        self.v_max, self.v_tmax, self.v_over, self.v_len = (QLabel("-") for _ in range(4))
        form = QFormLayout()
        form.addRow("최대 간격", self.v_max)
        form.addRow("발생 시각", self.v_tmax)
        form.addRow("한계 초과", self.v_over)
        form.addRow("프레임 길이", self.v_len)
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.addWidget(self.title)
        sl.addWidget(self.hist, 1)
        sl.addLayout(form)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.table)
        split.addWidget(side)
        split.setSizes([940, 330])
        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(split, 1)

        self.b_reset.clicked.connect(self.reset)
        self.b_export.clicked.connect(self._export)
        self.limit.valueChanged.connect(self._limit_changed)
        self.table.itemSelectionChanged.connect(self._show_selected)

    def begin(self, session):
        self.session, self.meta = session, session.meta
        self.table.setRowCount(0)
        self.rows, self.groups, self.names = {}, {}, {}
        session.stats.set_limit(self.limit.value())

    def reset(self):
        if self.session is not None:
            self.session.stats.reset()
            self.table.setRowCount(0)
            self.rows, self.groups = {}, {}

    def _limit_changed(self, v):
        if self.session is not None:
            self.session.stats.set_limit(v)

    def update_groups(self, groups, since_reset):
        self.groups = {g.key: g for g in groups}
        flagged = 0
        for lg in sorted(groups, key=lambda g: (g.iface, g.can_id, str(g.sub))):
            cells = self.rows.get(lg.key)
            if cells is None:
                g = self.meta.group(lg.iface, lg.can_id, lg.sub)
                self.names[lg.key] = g
                r = self.table.rowCount()
                self.table.insertRow(r)
                cells = [QTableWidgetItem(lg.iface), QTableWidgetItem(f"0x{lg.can_id:06X}"),
                         QTableWidgetItem(g.short if g.msg is not None else f"{g.name} (DBC에 없음)")]
                cells += [NumItem(None, "g") for _ in range(8)]
                cells[0].setData(Qt.ItemDataRole.UserRole, lg.key)
                cells[2].setToolTip(g.title)
                for c, item in enumerate(cells):
                    self.table.setItem(r, c, item)
                self.rows[lg.key] = cells
                self.table.resizeColumnsToContents()
            ratio = lg.gmax / lg.design if (lg.design and lg.count > 1) else None
            values = [(lg.count, ",d"), (lg.mean, ".3f"), (lg.gmin if lg.count > 1 else None, ".3f"),
                      (lg.gmax if lg.count > 1 else None, ".3f"), (lg.p99(), ".3f"), (lg.design, "g"), (ratio, ".1f"),
                      (lg.over, "d")]
            for item, (v, fmt) in zip(cells[3:], values):
                item.value = v
                item.setText("-" if v is None else format(v, fmt))
            flag = lg.over > 0
            flagged += flag
            if flag != bool(cells[0].data(Qt.ItemDataRole.UserRole + 1)):
                cells[0].setData(Qt.ItemDataRole.UserRole + 1, flag)
                for c, item in enumerate(cells):
                    item.setBackground(QBrush(FLAG_BG if flag else QColor(0, 0, 0, 0)))
                    if c in (6, 9, 10):
                        item.setForeground(QBrush(FLAG_FG if flag else QColor("#1c2530")))
        self.since.setText(f"({since_reset:.1f} s)")
        self.summary.setText(f"  한계 초과 {flagged}건" if flagged else "  한계 초과 없음")
        self.summary.setStyleSheet(f"color: {'#bd3626' if flagged else '#2b8455'}; font-weight: 600;")
        self._show_selected()

    def _selected_key(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole) if rows else None

    def _show_selected(self):
        lg = self.groups.get(self._selected_key())
        self.hist.clear()
        if lg is None:
            self.title.setText("메시지를 선택하세요.")
            for v in (self.v_max, self.v_tmax, self.v_over, self.v_len):
                v.setText("-")
            return
        g = self.names[lg.key]
        self.title.setText(f"<b>{g.iface} · {g.title}</b>")
        self.v_len.setText(", ".join(f"{n} B × {c:,}" for n, c in sorted(lg.lengths.items())))
        if lg.count < 2:
            return
        used = [k for k, c in enumerate(lg.hist) if c]
        lo, hi = max(used[0] - 1, 0), min(used[-1] + 2, len(lg.hist))
        x0 = [math.log10(HIST_EDGES[k]) for k in range(lo, hi)]
        x1 = [math.log10(HIST_EDGES[k + 1]) for k in range(lo, hi)]
        self.hist.addItem(pg.BarGraphItem(x0=x0, x1=x1, height=np.log10(np.asarray(lg.hist[lo:hi]) + 1), brush="#1f6f9f", pen=None))
        ticks = [(float(d), f"{10 ** d:g}") for d in range(-2, 5) if x0[0] - 1 <= d <= x1[-1] + 1]
        self.hist.getAxis("bottom").setTicks([ticks])
        if lg.design:
            self.hist.addItem(pg.InfiniteLine(math.log10(self.limit.value() * lg.design),
                                              pen=pg.mkPen("#bd3626", style=Qt.PenStyle.DashLine)))
        self.hist.autoRange()
        self.v_max.setText(f"{lg.gmax:.3f} ms")
        self.v_tmax.setText("-" if lg.t_max is None else f"{lg.t_max:.4f} s")
        self.v_over.setText(f"{lg.over}회" if lg.design else "설계 주기 없음")

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "주기 통계 내보내기", "period_stats_live.csv", "CSV (*.csv)")
        if path:
            self.write_csv(path)

    def write_csv(self, path):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["channel", "can_id", "message", "part", "count", "mean_ms", "min_ms", "max_ms", "p99_upper_ms",
                        "design_ms", "over_limit", "t_max_s"])
            for key in sorted(self.groups, key=lambda k: (k[0], k[1], str(k[2]))):
                lg, g = self.groups[key], self.names[key]
                many = lg.count > 1
                w.writerow([lg.iface, f"0x{lg.can_id:06X}", g.name, g.part, lg.count] + [
                    "" if v is None else f"{v:.4f}" for v in (lg.mean, lg.gmin if many else None, lg.gmax if many else None,
                                                              lg.p99(), lg.design)]
                    + [lg.over, "" if lg.t_max is None else f"{lg.t_max:.6f}"])


class LivePage(QTabWidget):
    """Tabs shown while measuring. The graph is offline only, so its tab is disabled here."""

    def __init__(self, settings=None):
        super().__init__()
        from .tx import TxPanel
        self.trace = LiveTrace()
        self.period = LivePeriod()
        self.tx = TxPanel(settings)
        self.addTab(self.trace, "Trace")
        self.addTab(self.period, "주기 통계")
        self.addTab(self.tx, "메시지 송신")
        note = QLabel("그래프는 측정을 멈춘 뒤 저장한 로그로 봅니다.")
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.addTab(note, "그래프 (측정 후)")
        self.setTabEnabled(3, False)

    def begin(self, session, demo=False):
        self.trace.begin(session.meta)
        self.period.begin(session)
        self.tx.begin(session, demo)
        self.setCurrentIndex(0)

    def end(self):
        self.tx.end()

    def refresh(self, groups, since_reset):
        self.trace.update_groups(groups)
        self.period.update_groups(groups, since_reset)
        self.tx.refresh()
