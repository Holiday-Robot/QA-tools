"""Period statistics tab: mean / min / max receive interval per message, checked against the design period."""
from __future__ import annotations

import csv

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor, QFont
from PyQt6.QtWidgets import (QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QPushButton,
                             QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

COLS = ["채널", "CAN ID", "메시지", "", "수신 수", "평균 (ms)", "최소", "최대", "p99", "설계", "최대/설계", "한계 초과"]
FLAG_BG = QColor("#fbe8e5")
FLAG_FG = QColor("#bd3626")


class NumItem(QTableWidgetItem):
    """Table cell that sorts by its number, with empty cells last."""

    def __init__(self, value, fmt):
        super().__init__("-" if value is None else format(value, fmt))
        self.value = value
        self.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def __lt__(self, other):
        a = -1e300 if self.value is None else self.value
        b = -1e300 if other.value is None else other.value
        return a < b


class PeriodTab(QWidget):
    def __init__(self, state):
        super().__init__()
        self.state = state
        self.stats = []                    # [(group, PeriodStats)] in table row order before sorting

        self.range = QComboBox()
        self.limit = QDoubleSpinBox()
        self.limit.setRange(1.1, 100)
        self.limit.setValue(5.0)
        self.limit.setSingleStep(0.5)
        self.limit.setPrefix("설계 주기 × ")
        self.summary = QLabel("")
        self.export = QPushButton("CSV 내보내기")
        bar = QHBoxLayout()
        bar.addWidget(QLabel("통계 범위"))
        bar.addWidget(self.range)
        bar.addSpacing(12)
        bar.addWidget(QLabel("한계"))
        bar.addWidget(self.limit)
        bar.addWidget(self.summary)
        bar.addStretch(1)
        bar.addWidget(self.export)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.setSortingEnabled(True)
        self.table.setColumnHidden(3, True)   # the part name is folded into the message column
        f = QFont("monospace")
        f.setStyleHint(QFont.StyleHint.Monospace)
        self.table.setFont(f)

        self.title = QLabel("메시지를 선택하세요.")
        self.title.setWordWrap(True)
        self.hist = pg.PlotWidget()
        self.hist.setMinimumHeight(180)
        self.hist.setLabel("bottom", "수신 간격 (ms)")
        self.hist.setLabel("left", "프레임 수 (로그)")
        self.hist.setMouseEnabled(x=True, y=False)
        self.v_max, self.v_tmax, self.v_over, self.v_p99, self.v_len = (QLabel("-") for _ in range(5))
        form = QFormLayout()
        form.addRow("최대 간격", self.v_max)
        form.addRow("발생 시각", self.v_tmax)
        form.addRow("한계 초과", self.v_over)
        form.addRow("p99", self.v_p99)
        form.addRow("프레임 길이", self.v_len)
        self.to_graph = QPushButton("그래프에 간격 표시")
        self.jump = QPushButton("최대 간격 위치로 이동")
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.addWidget(self.title)
        sl.addWidget(self.hist, 1)
        sl.addLayout(form)
        sl.addWidget(self.to_graph)
        sl.addWidget(self.jump)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.table)
        split.addWidget(side)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        split.setSizes([940, 330])
        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(split, 1)

        self.range.currentIndexChanged.connect(self.recompute)
        self.limit.valueChanged.connect(self.recompute)
        self.table.itemSelectionChanged.connect(self._show_selected)
        self.export.clicked.connect(self._export)
        self.to_graph.clicked.connect(self._to_graph)
        self.jump.clicked.connect(self._jump)
        state.sessionChanged.connect(self._ranges_changed)
        state.mappingChanged.connect(self.recompute)
        state.rangesChanged.connect(self._ranges_changed)
        state.cursorsChanged.connect(self._cursor_moved)
        self._ranges_changed()

    # ------------------------------------------------------------ range
    def _ranges_changed(self):
        cur = self.range.currentData()
        self.range.blockSignals(True)
        self.range.clear()
        self.range.addItem("측정 전체", "all")
        self.range.addItem("커서 A–B 구간", "cursor")
        for k, (name, lo, hi) in enumerate(self.state.ranges):
            self.range.addItem(f"{name} ({lo:.3f}–{hi:.3f} s)", f"saved:{k}")
        i = self.range.findData(cur)
        self.range.setCurrentIndex(max(i, 0))
        self.range.blockSignals(False)
        self.recompute()

    def bounds(self):
        key = self.range.currentData()
        if key == "cursor":
            return self.state.cursor_range()
        if key and key.startswith("saved:"):
            _, lo, hi = self.state.ranges[int(key.split(":")[1])]
            return lo, hi
        return None, None

    def _cursor_moved(self):
        if self.range.currentData() == "cursor":
            self.recompute()

    # ------------------------------------------------------------ table
    def recompute(self):
        ses = self.state.session
        selected = self._selected_group()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        self.stats = []
        if ses is None:
            return
        lo, hi = self.bounds()
        flagged = 0
        for g in ses.cap.groups:
            st = ses.period_stats(g, lo, hi, self.limit.value())
            if st.count == 0:
                continue
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.stats.append((g, st))
            cells = [QTableWidgetItem(g.iface), QTableWidgetItem(f"0x{g.can_id:06X}"),
                     QTableWidgetItem(g.short if g.msg is not None else f"{g.name} (DBC에 없음)"), QTableWidgetItem(""),
                     NumItem(st.count, ",d"), NumItem(st.mean, ".3f"), NumItem(st.min, ".3f"), NumItem(st.max, ".3f"),
                     NumItem(st.p99, ".3f"), NumItem(st.design, "g"), NumItem(st.ratio, ".1f"), NumItem(st.over, "d")]
            cells[0].setData(Qt.ItemDataRole.UserRole, len(self.stats) - 1)
            cells[2].setToolTip(g.title)
            for c, item in enumerate(cells):
                if st.flagged:
                    item.setBackground(QBrush(FLAG_BG))
                    if c in (7, 10, 11):
                        item.setForeground(QBrush(FLAG_FG))
                self.table.setItem(row, c, item)
            flagged += st.flagged
        self.table.setSortingEnabled(True)
        self.table.resizeColumnsToContents()
        self.summary.setText(f"  한계 초과 {flagged}건" if flagged else "  한계 초과 없음")
        self.summary.setStyleSheet(f"color: {'#bd3626' if flagged else '#2b8455'}; font-weight: 600;")
        if selected is not None:
            self.select_group(selected)
        self._show_selected()

    def _selected(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        k = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        return self.stats[k]

    def _selected_group(self):
        sel = self._selected()
        return sel[0] if sel else None

    def select_group(self, group):
        for row in range(self.table.rowCount()):
            k = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            if self.stats[k][0] is group:
                self.table.selectRow(row)
                return

    # ------------------------------------------------------------ detail
    def _show_selected(self):
        sel = self._selected()
        self.hist.clear()
        for w in (self.to_graph, self.jump):
            w.setEnabled(sel is not None and sel[1].gaps_ms is not None)
        if sel is None:
            self.title.setText("메시지를 선택하세요.")
            for v in (self.v_max, self.v_tmax, self.v_over, self.v_p99, self.v_len):
                v.setText("-")
            return
        g, st = sel
        self.title.setText(f"<b>{g.iface} · {g.title}</b>")
        self.v_len.setText(", ".join(f"{n} B × {c:,}" for n, c in st.lengths.items()))
        if st.gaps_ms is None:
            for v in (self.v_max, self.v_tmax, self.v_over, self.v_p99):
                v.setText("-")
            return
        counts, edges = np.histogram(st.gaps_ms, bins=40)
        self.hist.addItem(pg.BarGraphItem(x0=edges[:-1], x1=edges[1:], height=np.log10(counts + 1), brush="#1f6f9f", pen=None))
        if g.design:
            limit = self.limit.value() * g.design
            if limit <= edges[-1]:
                self.hist.addItem(pg.InfiniteLine(limit, pen=pg.mkPen("#bd3626", style=Qt.PenStyle.DashLine)))
        self.hist.autoRange()
        self.v_max.setText(f"{st.max:.3f} ms")
        self.v_tmax.setText(f"{st.t_max:.4f} s")
        self.v_over.setText(f"{st.over}회" if g.design else "설계 주기 없음")
        self.v_p99.setText(f"{st.p99:.3f} ms")

    def _to_graph(self):
        sel = self._selected()
        if sel:
            self.state.set_gap_group(sel[0])

    def _jump(self):
        sel = self._selected()
        if sel and sel[1].t_max is not None:
            self.state.set_gap_group(sel[0])
            self.state.jumpRequested.emit(sel[1].t_max)

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "주기 통계 내보내기", "period_stats.csv", "CSV (*.csv)")
        if path:
            self.write_csv(path)

    def write_csv(self, path):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["channel", "can_id", "message", "part", "count", "mean_ms", "min_ms", "max_ms", "p99_ms",
                        "design_ms", "max_over_design", "over_limit", "t_max_s"])
            for g, st in self.stats:
                w.writerow([g.iface, f"0x{g.can_id:06X}", g.name, g.part, st.count] + [
                    "" if v is None else f"{v:.4f}" for v in (st.mean, st.min, st.max, st.p99, st.design, st.ratio)]
                    + [st.over, "" if st.t_max is None else f"{st.t_max:.6f}"])
