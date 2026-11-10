"""Trace tab: latest frame per message (fixed view) or every frame in time order."""
from __future__ import annotations

import numpy as np
from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PyQt6.QtGui import QBrush, QColor, QFont
from PyQt6.QtWidgets import (QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QSplitter,
                             QStackedWidget, QTableView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

WARN = QColor("#a56a12")
TX = QColor("#1f6f9f")             # frames this PC sent
FIXED_COLS = ["시각 (s)", "채널", "CAN ID", "메시지", "형식", "길이", "주기 (ms)", "데이터"]
CHRONO_COLS = ["시각 (s)", "채널", "CAN ID", "메시지", "형식", "길이", "데이터"]


def hexdump(payload, limit=24):
    text = " ".join(f"{b:02X}" for b in payload[:limit])
    return text + (" …" if len(payload) > limit else "")


def mono():
    f = QFont("monospace")
    f.setStyleHint(QFont.StyleHint.Monospace)
    return f


class FrameModel(QAbstractTableModel):
    """All frames of the capture in time order, filtered by text."""

    def __init__(self, state):
        super().__init__()
        self.state = state
        self.rows = np.zeros(0, np.int64)
        self.group_of = np.zeros(0, np.int64)

    def rebuild(self, text):
        self.beginResetModel()
        ses = self.state.session
        if ses is None:
            self.rows = np.zeros(0, np.int64)
        else:
            cap = ses.cap
            self.group_of = np.full(cap.n, -1, np.int64)
            keep = []
            for k, g in enumerate(cap.groups):
                self.group_of[g.idx] = k
                if not text or text in f"{g.iface} {g.title}".lower():
                    keep.append(g.idx)
            if not text:
                self.rows = np.arange(cap.n)
            else:
                self.rows = np.sort(np.concatenate(keep)) if keep else np.zeros(0, np.int64)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(CHRONO_COLS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return CHRONO_COLS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        cap = self.state.session.cap
        i = int(self.rows[index.row()])
        g = cap.groups[self.group_of[i]] if self.group_of[i] >= 0 else None
        if role == Qt.ItemDataRole.DisplayRole:
            c = index.column()
            if c == 0:
                return f"{cap.t[i]:.4f}"
            if c == 1:
                return cap.ifaces[cap.iface[i]] + (" Tx" if cap.tx[i] else "")
            if c == 2:
                return "오류 프레임" if cap.err[i] else f"0x{cap.can_id[i]:06X}"
            if c == 3:
                return g.name if g else "-"
            if c == 4:
                return g.part if g else "-"
            if c == 5:
                return str(cap.length[i])
            return hexdump(cap.payload(i))
        if role == Qt.ItemDataRole.ForegroundRole and cap.tx[i]:
            return QBrush(TX)
        if role == Qt.ItemDataRole.ForegroundRole and (g is None or g.msg is None):
            return QBrush(WARN)
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in (0, 5):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def frame(self, row):
        return int(self.rows[row])


class TraceTab(QWidget):
    def __init__(self, state):
        super().__init__()
        self.state = state
        self.mode = QComboBox()
        self.mode.addItems(["ID별 고정", "시간순"])
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("채널, CAN ID, 이름으로 거르기")
        self.filter.setClearButtonEnabled(True)
        self.info = QLabel("")
        bar = QHBoxLayout()
        bar.addWidget(QLabel("표시"))
        bar.addWidget(self.mode)
        bar.addWidget(self.filter, 1)
        bar.addWidget(self.info)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(FIXED_COLS)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setFont(mono())
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tree.header().setStretchLastSection(True)
        self.items = []                       # [(group, top-level item)]

        self.model = FrameModel(state)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setFont(mono())
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFont(mono())
        self.detail.setPlaceholderText("프레임을 선택하면 신호 값을 보여 줍니다.")
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.detail)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.tree)
        self.stack.addWidget(split)
        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(self.stack, 1)

        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.filter.textChanged.connect(self._refilter)
        self.tree.itemExpanded.connect(self._fill_children)
        self.table.selectionModel().currentRowChanged.connect(self._show_detail)
        state.sessionChanged.connect(self.rebuild)
        state.mappingChanged.connect(self.rebuild)
        state.timeChanged.connect(self.refresh_time)

    # ------------------------------------------------------------ build
    def rebuild(self):
        self.tree.clear()
        self.items = []
        ses = self.state.session
        if ses is None:
            return
        for g in ses.cap.groups:
            it = QTreeWidgetItem(self.tree)
            it.setText(1, g.iface + (" Tx" if g.tx else ""))
            if g.tx:
                it.setToolTip(1, f"이 PC가 보낸 프레임 {g.tx:,}개 포함 (전체 {len(g.idx):,}개)")
                it.setForeground(1, QBrush(TX))
            it.setText(2, f"0x{g.can_id:06X}")
            it.setText(3, g.name if g.msg is not None else f"{g.name} (DBC에 없음)")
            it.setText(4, g.part)
            for c in (0, 5, 6):
                it.setTextAlignment(c, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if g.msg is None:
                for c in range(len(FIXED_COLS)):
                    it.setForeground(c, QBrush(WARN))
            else:
                it.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
            self.items.append((g, it))
        for c, w in enumerate((90, 60, 90, 290, 190, 50, 80)):
            self.tree.setColumnWidth(c, w)
        for c, w in enumerate((90, 60, 100, 290, 190, 50)):
            self.table.setColumnWidth(c, w)
        self._refilter()
        self.refresh_time()

    def _mode_changed(self, i):
        self.stack.setCurrentIndex(i)
        self._refilter()

    def _refilter(self):
        text = self.filter.text().strip().lower()
        shown = 0
        for g, it in self.items:
            hide = bool(text) and text not in f"{g.iface} {'tx ' if g.tx else ''}{g.title}".lower()
            it.setHidden(hide)
            shown += not hide
        if self.stack.currentIndex() == 1:
            self.model.rebuild(text)
            self.info.setText(f"프레임 {len(self.model.rows):,}개")
        else:
            self.info.setText(f"메시지 {shown}개")

    # ------------------------------------------------------------ fixed view
    def refresh_time(self):
        ses = self.state.session
        if ses is None or self.stack.currentIndex() != 0:
            return
        cap, t = ses.cap, self.state.cur_t
        for g, it in self.items:
            if it.isHidden():
                continue
            k = int(np.searchsorted(cap.t[g.idx], t, "right")) - 1
            if k < 0:
                for c in (0, 5, 6, 7):
                    it.setText(c, "")
                it.setData(0, Qt.ItemDataRole.UserRole, None)
                continue
            i = int(g.idx[k])
            it.setText(0, f"{cap.t[i]:.4f}")
            it.setText(5, str(cap.length[i]))
            it.setText(6, f"{(cap.t[i] - cap.t[g.idx[k - 1]]) * 1e3:.2f}" if k > 0 else "-")
            it.setText(7, hexdump(cap.payload(i)))
            it.setData(0, Qt.ItemDataRole.UserRole, i)
            if it.isExpanded():
                self._fill_children(it)

    def _fill_children(self, it):
        i = it.data(0, Qt.ItemDataRole.UserRole)
        values = self.state.session.decode(i) if i is not None else []
        while it.childCount() > len(values):
            it.removeChild(it.child(it.childCount() - 1))
        while it.childCount() < len(values):
            child = QTreeWidgetItem(it)
            child.setFirstColumnSpanned(False)
        for k, (name, text) in enumerate(values):
            child = it.child(k)
            child.setText(3, name)
            child.setText(4, text)

    # ------------------------------------------------------------ chronological view
    def _show_detail(self, current, _previous=None):
        if not current.isValid():
            self.detail.clear()
            return
        i = self.model.frame(current.row())
        cap = self.state.session.cap
        lines = [f"t = {cap.t[i]:.6f} s   {cap.ifaces[cap.iface[i]]}   0x{cap.can_id[i]:06X}   길이 {cap.length[i]}",
                 hexdump(cap.payload(i), 64), ""]
        values = self.state.session.decode(i)
        lines += [f"{name:<22} {text}" for name, text in values] or ["DBC에 정의가 없어 신호로 풀 수 없습니다."]
        self.detail.setPlainText("\n".join(lines))
