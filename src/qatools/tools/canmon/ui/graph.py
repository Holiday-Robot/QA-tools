"""Graph tab: signal values, state signals and receive gaps on one time axis, with two cursors."""
from __future__ import annotations

import csv

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton,
                             QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

POINT_PX = 8               # draw sample points only when they are at least this many pixels apart
HOVER_PX = 14
SIG_COLS = ["신호", "A", "B", "B − A", "최소", "평균", "최대"]


class GraphTab(QWidget):
    def __init__(self, state):
        super().__init__()
        self.state = state
        self.curves = {}                   # SigRef key -> PlotDataItem
        self._moving = False

        # ---- top bar
        self.points = QCheckBox("샘플 점 표시")
        self.points.setChecked(True)
        self.shape = QComboBox()
        self.shape.addItems(["직선", "계단"])
        self.readout = QLabel("")
        self.fit = QPushButton("전체 보기")
        self.export = QPushButton("CSV 내보내기")
        self.image = QPushButton("이미지 저장")
        bar = QHBoxLayout()
        bar.addWidget(self.points)
        bar.addWidget(QLabel("선 모양"))
        bar.addWidget(self.shape)
        bar.addSpacing(12)
        bar.addWidget(self.readout)
        bar.addStretch(1)
        for w in (self.fit, self.export, self.image):
            bar.addWidget(w)

        # ---- plots
        self.glw = pg.GraphicsLayoutWidget()
        self.p_val = pg.PlotItem()
        self.p_state = pg.PlotItem()
        self.p_gap = pg.PlotItem()
        for p, title in ((self.p_val, "신호 값"), (self.p_state, "상태"), (self.p_gap, "수신 간격 (ms)")):
            # No axis grid: pyqtgraph draws it through the axis picture, which cost about 7 ms per axis per repaint.
            p.setLabel("left", title)
            p.getAxis("left").setWidth(64)
            p.getAxis("left").enableAutoSIPrefix(False)
            p.setMouseEnabled(x=True, y=False)
        self.p_gap.setLabel("bottom", "시간 (s)")
        self.p_state.setXLink(self.p_val)
        self.p_gap.setXLink(self.p_val)
        self.cursors, self.regions, self.time_lines = [], [], []
        for p in (self.p_val, self.p_state, self.p_gap):
            region = pg.LinearRegionItem(movable=False, brush=pg.mkBrush(31, 111, 159, 22), pen=pg.mkPen(None))
            region.setZValue(-10)
            p.addItem(region)
            self.regions.append(region)
            pair = []
            for name in ("A", "B"):
                line = pg.InfiniteLine(0, angle=90, movable=True, pen=pg.mkPen("#1c2530", width=1, style=Qt.PenStyle.DashLine),
                                       label=name if p is self.p_val else None,
                                       labelOpts={"position": 0.96, "color": "#ffffff", "fill": "#1c2530"})
                line.sigPositionChanged.connect(self._cursor_dragged)
                p.addItem(line)
                pair.append(line)
            self.cursors.append(pair)
            now = pg.InfiniteLine(0, angle=90, movable=False, pen=pg.mkPen("#2b8455", width=1))
            p.addItem(now)
            self.time_lines.append(now)
        self.gap_curve = self.p_gap.plot(pen=pg.mkPen("#1f6f9f", width=1))
        self.gap_over = pg.ScatterPlotItem(size=8, brush="#bd3626", pen=None)
        self.p_gap.addItem(self.gap_over)
        self.gap_limit = pg.InfiniteLine(0, angle=0, movable=False, pen=pg.mkPen("#bd3626", style=Qt.PenStyle.DashLine))
        self.p_gap.addItem(self.gap_limit)
        self.hover_dot = pg.ScatterPlotItem(size=11, brush=None, pen=pg.mkPen("#1c2530", width=2))
        self.hover_dot.setZValue(50)
        self.p_val.addItem(self.hover_dot)
        self.hover = QLabel("그래프의 점에 마우스를 올리면 시각, 값, 직전 프레임과의 간격을 보여 줍니다.")
        self.hover.setStyleSheet("color: #5b6978;")
        self.hover.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.empty = QLabel("그릴 신호가 없습니다. 왼쪽 <b>DBC 탐색</b>에서 메시지를 펼치고 신호를 체크하세요. "
                            "수신 간격은 <b>주기 통계</b> 탭에서 메시지를 고른 뒤 \"그래프에 간격 표시\"를 누르면 나옵니다.")
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet("background:#fcf1d8; color:#6b4a0c; padding:8px 10px; border-radius:6px;")
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.empty)
        ll.addWidget(self.glw, 1)
        ll.addWidget(self.hover)

        # ---- side panel
        self.table = QTableWidget(0, len(SIG_COLS))
        self.table.setHorizontalHeaderLabels(SIG_COLS)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.stat_range = QComboBox()
        self.stat_range.addItem("화면 범위", "view")
        self.stat_range.addItem("커서 A–B 구간", "cursor")
        self.stat_range.addItem("측정 전체", "all")
        self.stat_range.setCurrentIndex(1)
        self.remove = QPushButton("선택한 신호 빼기")
        self.range_name = QLineEdit()
        self.range_name.setPlaceholderText("구간 이름 (예: 동작 구간 1)")
        self.save_range = QPushButton("커서 구간 저장")
        self.ranges = QListWidget()
        self.ranges.setMaximumHeight(110)
        self.del_range = QPushButton("구간 삭제")
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.addWidget(QLabel("<b>신호</b>  (왼쪽 DBC 탐색에서 체크해 추가)"))
        sl.addWidget(self.table, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel("통계 범위"))
        row.addWidget(self.stat_range, 1)
        row.addWidget(self.remove)
        sl.addLayout(row)
        sl.addWidget(QLabel("<b>저장한 구간</b>  (누르면 커서가 그 구간으로 이동)"))
        row = QHBoxLayout()
        row.addWidget(self.range_name, 1)
        row.addWidget(self.save_range)
        sl.addLayout(row)
        sl.addWidget(self.ranges)
        sl.addWidget(self.del_range)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(side)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setSizes([820, 560])
        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(split, 1)

        # ---- wiring
        self.points.toggled.connect(self._update_points)
        self.shape.currentIndexChanged.connect(self._restyle)
        self.fit.clicked.connect(self.view_all)
        self.export.clicked.connect(self._export)
        self.image.clicked.connect(self._save_image)
        self.remove.clicked.connect(self._remove_selected)
        self.stat_range.currentIndexChanged.connect(self._update_table)
        self.save_range.clicked.connect(self._save_range)
        self.del_range.clicked.connect(self._delete_range)
        self.ranges.itemClicked.connect(self._apply_range)
        self.p_val.sigXRangeChanged.connect(self._view_changed)
        self.proxy = pg.SignalProxy(self.glw.scene().sigMouseMoved, rateLimit=30, slot=self._hover)
        state.sessionChanged.connect(self._session_changed)
        state.signalsChanged.connect(self._signals_changed)
        state.cursorsChanged.connect(self._cursors_changed)
        state.timeChanged.connect(self._time_changed)
        state.rangesChanged.connect(self._ranges_changed)
        state.gapGroupChanged.connect(self._gap_changed)
        state.jumpRequested.connect(self.jump_to)
        self._layout()

    # ------------------------------------------------------------ layout
    def _layout(self):
        """Show only the panels that have something to draw."""
        self.glw.clear()
        self.empty.setVisible(not self.state.signals and self.state.gap_group is None)
        rows = [(self.p_val, 5)]
        if any(s.is_enum for s in self.state.signals):
            rows.append((self.p_state, 2))
        if self.state.gap_group is not None:
            rows.append((self.p_gap, 3))
        for r, (p, stretch) in enumerate(rows):
            self.glw.addItem(p, row=r, col=0)
            self.glw.ci.layout.setRowStretchFactor(r, stretch)
            p.setLabel("bottom", "시간 (s)" if r == len(rows) - 1 else "")
            p.getAxis("bottom").setStyle(showValues=(r == len(rows) - 1))

    def _session_changed(self):
        for item in self.curves.values():
            item.getViewBox().removeItem(item)
        self.curves = {}
        self._gap_changed()
        self._cursors_changed()
        self._time_changed()
        self._ranges_changed()
        self._update_table()
        self.view_all()

    def view_all(self):
        ses = self.state.session
        if ses is not None:
            self.p_val.setXRange(0, max(ses.cap.duration, 1e-3), padding=0.01)
        for p in (self.p_val, self.p_state, self.p_gap):
            p.enableAutoRange(axis="y")

    def jump_to(self, t):
        lo, hi = self.p_val.viewRange()[0]
        half = min((hi - lo) / 2, 0.05)
        self.p_val.setXRange(t - half, t + half, padding=0)

    # ------------------------------------------------------------ signals
    def _signals_changed(self):
        wanted = {s.key: s for s in self.state.signals}
        for key in list(self.curves):
            if key not in wanted:
                item = self.curves.pop(key)
                item.getViewBox().removeItem(item)
        for key, s in wanted.items():
            if key not in self.curves:
                target = self.p_state if s.is_enum else self.p_val
                self.curves[key] = target.plot(pen=pg.mkPen(s.color, width=1), symbolBrush=s.color, symbolPen=None,
                                               symbolSize=6, symbol=None)
                self.curves[key].setDownsampling(auto=True, method="peak")
                self.curves[key].setClipToView(True)
        enums = [s for s in self.state.signals if s.is_enum]
        if enums:
            ticks = sorted({(float(k), str(v)) for s in enums for k, v in s.series.sig.choices.items()})
            self.p_state.getAxis("left").setTicks([ticks])
        self._layout()
        self._restyle()
        self._update_table()

    def _restyle(self):
        step = self.shape.currentIndex() == 1
        for s in self.state.signals:
            mode = "right" if (step or s.is_enum) else None
            self.curves[s.key].setData(s.series.t, s.series.y, stepMode=mode)
        self._update_points()

    def _view_changed(self):
        self._update_points()
        if self.stat_range.currentData() == "view":
            self._update_table()

    def _update_points(self):
        lo, hi = self.p_val.viewRange()[0]
        room = max(self.p_val.vb.width(), 100) / POINT_PX
        for s in self.state.signals:
            t = s.series.t
            n = int(np.searchsorted(t, hi) - np.searchsorted(t, lo))
            show = self.points.isChecked() and n <= room
            self.curves[s.key].setSymbol("o" if show else None)

    def _remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        for s in [self.state.signals[r] for r in rows if r < len(self.state.signals)]:
            self.state.remove_signal(*s.key)

    # ------------------------------------------------------------ cursors, time, ranges
    def _cursor_dragged(self, line):
        if self._moving:
            return
        for pair in self.cursors:
            if line in pair:
                k = pair.index(line)
                a, b = self.state.cursor_a, self.state.cursor_b
                self.state.set_cursors(line.value() if k == 0 else a, line.value() if k == 1 else b)

    def _cursors_changed(self):
        a, b = self.state.cursor_a, self.state.cursor_b
        self._moving = True
        for pair, region in zip(self.cursors, self.regions):
            pair[0].setValue(a)
            pair[1].setValue(b)
            region.setRegion(sorted((a, b)))
        self._moving = False
        self.readout.setText(f"커서  A {a:.4f} s   B {b:.4f} s   Δt {abs(b - a) * 1e3:.3f} ms")
        self._update_table()

    def _time_changed(self):
        for line in self.time_lines:
            line.setValue(self.state.cur_t)

    def _ranges_changed(self):
        self.ranges.clear()
        for name, lo, hi in self.state.ranges:
            self.ranges.addItem(f"{name}   {lo:.3f}–{hi:.3f} s")

    def _save_range(self):
        name = self.range_name.text().strip() or f"구간 {len(self.state.ranges) + 1}"
        self.state.save_range(name)
        self.range_name.clear()

    def _delete_range(self):
        row = self.ranges.currentRow()
        if row >= 0:
            self.state.delete_range(row)

    def _apply_range(self, item):
        _, lo, hi = self.state.ranges[self.ranges.row(item)]
        self.state.set_cursors(lo, hi)

    # ------------------------------------------------------------ gap panel
    def _gap_changed(self):
        g = self.state.gap_group
        ses = self.state.session
        if g is None or ses is None:
            self.gap_curve.setData([], [])
            self.gap_over.setData([], [])
            self.gap_limit.setVisible(False)
        else:
            st = ses.period_stats(g)
            x, y = (st.gap_t, st.gaps_ms) if st.gaps_ms is not None else ([], [])
            self.gap_curve.setData(x, y)
            self.p_gap.setTitle(f"{g.iface} · {g.title}", size="9pt")
            self.gap_limit.setVisible(bool(g.design))
            if g.design and st.gaps_ms is not None:
                limit = 5.0 * g.design
                self.gap_limit.setValue(limit)
                over = st.gaps_ms > limit
                self.gap_over.setData(st.gap_t[over], st.gaps_ms[over])
            else:
                self.gap_over.setData([], [])
        self._layout()

    # ------------------------------------------------------------ table
    def _stat_bounds(self):
        key = self.stat_range.currentData()
        if key == "view":
            return tuple(self.p_val.viewRange()[0])
        if key == "cursor":
            return self.state.cursor_range()
        return None, None

    @staticmethod
    def _at(series, t):
        k = int(np.searchsorted(series.t, t, "right")) - 1
        return None if k < 0 else float(series.y[k])

    @staticmethod
    def _text(s, v):
        if v is None:
            return "-"
        if s.is_enum:
            return str(s.series.sig.choices.get(int(v), int(v)))
        return f"{v:.4f}"

    def _update_table(self):
        sigs = self.state.signals
        self.table.setRowCount(len(sigs))
        lo, hi = self._stat_bounds()
        for r, s in enumerate(sigs):
            a, b = self._at(s.series, self.state.cursor_a), self._at(s.series, self.state.cursor_b)
            y = s.series.y
            if lo is not None:
                y = y[np.searchsorted(s.series.t, lo, "left"):np.searchsorted(s.series.t, hi, "right")]
            stats = (float(y.min()), float(y.mean()), float(y.max())) if len(y) else (None, None, None)
            unit = f" [{s.series.sig.unit}]" if s.series.sig.unit else ""
            name = QTableWidgetItem(f"■ {s.name}{unit}")
            name.setForeground(QBrush(QColor(s.color)))
            name.setToolTip(f"{s.iface} · 0x{s.can_id:06X}")
            diff = None if (a is None or b is None or s.is_enum) else b - a
            cells = [name, self._text(s, a), self._text(s, b), "-" if diff is None else f"{diff:+.4f}"] + [
                "-" if (v is None or s.is_enum) else f"{v:.4f}" for v in stats]
            for c, cell in enumerate(cells):
                item = cell if isinstance(cell, QTableWidgetItem) else QTableWidgetItem(cell)
                if c:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()

    # ------------------------------------------------------------ hover
    def _hover(self, event):
        pos = event[0]
        ses = self.state.session
        best = None
        if ses is not None and self.p_val.sceneBoundingRect().contains(pos):
            vb = self.p_val.vb
            mt = vb.mapSceneToView(pos).x()
            for s in self.state.signals:
                if s.is_enum or not len(s.series.t):
                    continue
                t = s.series.t
                k = int(np.clip(np.searchsorted(t, mt), 1, len(t) - 1))
                k = k if abs(t[k] - mt) < abs(t[k - 1] - mt) else k - 1
                p = vb.mapViewToScene(pg.Point(float(t[k]), float(s.series.y[k])))
                dist = ((p.x() - pos.x()) ** 2 + (p.y() - pos.y()) ** 2) ** 0.5
                if dist <= HOVER_PX and (best is None or dist < best[0]):
                    best = (dist, s, k)
        if best is None:
            self.hover_dot.setData([], [])
            return
        _, s, k = best
        t, y = s.series.t, s.series.y
        self.hover_dot.setData([float(t[k])], [float(y[k])])
        gap = f"{(t[k] - t[k - 1]) * 1e3:.3f} ms" if k > 0 else "-"
        unit = f" {s.series.sig.unit}" if s.series.sig.unit else ""
        i = int(s.series.fidx[k])
        payload = " ".join(f"{b:02X}" for b in ses.cap.payload(i)[:20])
        self.hover.setText(f"<b style='color:{s.color}'>{s.name}</b>   t = {t[k]:.5f} s   값 {y[k]:.5f}{unit}   "
                           f"직전 프레임과 간격 {gap}   데이터 {payload}")

    # ------------------------------------------------------------ export
    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "신호 내보내기", "signals.csv", "CSV (*.csv)")
        if path:
            self.write_csv(path)

    def write_csv(self, path):
        lo, hi = self._stat_bounds()
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["channel", "can_id", "signal", "t_s", "value"])
            for s in self.state.signals:
                t, y = s.series.t, s.series.y
                if lo is not None:
                    sl = slice(np.searchsorted(t, lo, "left"), np.searchsorted(t, hi, "right"))
                    t, y = t[sl], y[sl]
                for a, b in zip(t, y):
                    w.writerow([s.iface, f"0x{s.can_id:06X}", s.name, f"{a:.6f}", f"{b:.6g}"])

    def _save_image(self):
        path, _ = QFileDialog.getSaveFileName(self, "그래프 이미지 저장", "graph.png", "PNG (*.png)")
        if path:
            self.glw.grab().save(path)
