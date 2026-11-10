"""Message transmit screen shown while measuring: a transmit list, an editor for one entry, and the arm switch.

Safety rules kept here:
- Transmission is off at the start of every measurement and must be switched on with a confirmation.
- Only channels configured as "송신 가능" in the hardware settings can send (the preview sends to no bus at all).
- "전체 정지" and Esc stop every entry; periodic entries stop by themselves after the auto-stop time.
- Every frame sent is logged with dir=tx next to the received frames.
"""
from __future__ import annotations

import itertools
import json

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QKeySequence, QShortcut
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QCompleter, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
                             QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QSplitter,
                             QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .. import hwconfig
from ..core import design_period, key_of
from ..tx import (KIND_LOAD, KIND_MSG, KIND_RAW, LOAD_ID, MAX_FPS, Transmitter, TxEntry, active_signals, added_load,
                 build, default_value, period_s)
from .trace import hexdump, mono

COLS = ["송신", "채널", "종류", "CAN ID", "메시지", "주기 / 부하", "횟수", "보낸 수", "상태", "추가 부하", "길이", "데이터"]
C = {name: i for i, name in enumerate(COLS)}
KIND_TEXT = {KIND_MSG: "DBC", KIND_RAW: "직접 입력", KIND_LOAD: "부하"}
REASON_TEXT = {"once": "1회 송신함", "count": "횟수 완료", "auto": "자동 정지", "stopped": "정지"}
RED, GREEN, GREY = QColor("#bd3626"), QColor("#2b8455"), QColor("#5b6978")
LOAD_WARN_PCT = 50.0
PILL = "font-weight:600; border-radius:9px; padding:3px 8px;"


def parse_value(text):
    text = str(text).strip()
    try:
        return int(text, 0)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


class TxPanel(QWidget):
    armedChanged = pyqtSignal(bool)

    def __init__(self, settings=None):
        super().__init__()
        self.settings = settings
        self.session = None
        self.demo = False
        self.tx = None                    # Transmitter while armed
        self.allowed = set()              # interfaces that may send
        self.entries = []                 # [(uid, TxEntry)]
        self._uids = itertools.count(1)
        self._loading = False

        # ---- arm bar
        self.b_arm = QPushButton("송신 모드 켜기")
        self.b_arm.setCheckable(True)
        self.state = QLabel("")
        self.auto = QSpinBox()
        self.auto.setRange(0, 3600)
        self.auto.setValue(60)
        self.auto.setSuffix(" s")
        self.auto.setSpecialValueText("끔")
        self.auto.setToolTip("주기 송신을 시작하고 이 시간이 지나면 저절로 멈춥니다. 0이면 멈추지 않습니다.")
        self.b_stop = QPushButton("■ 전체 정지 (Esc)")
        self.b_stop.setStyleSheet("QPushButton { color:#bd3626; font-weight:600; }")
        self.b_save = QPushButton("목록 저장…")
        self.b_load = QPushButton("목록 불러오기…")
        bar = QHBoxLayout()
        bar.addWidget(self.b_arm)
        bar.addWidget(self.state, 1)
        bar.addWidget(QLabel("자동 정지"))
        bar.addWidget(self.auto)
        bar.addWidget(self.b_stop)
        bar.addSpacing(12)
        bar.addWidget(self.b_save)
        bar.addWidget(self.b_load)

        # ---- list
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.setFont(mono())
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        for c, w in enumerate((44, 50, 66, 90, 230, 80, 60, 70, 150, 70, 40)):
            self.table.setColumnWidth(c, w)
        self.b_add_msg = QPushButton("+ DBC 메시지")
        self.b_add_raw = QPushButton("+ 직접 입력")
        self.b_add_load = QPushButton("+ 부하 메시지")
        self.b_add_load.setToolTip("DBC에 없는 더미 프레임을 목표 부하율로 보내 버스 부하를 올립니다.")
        self.b_dup = QPushButton("복제")
        self.b_del = QPushButton("삭제")
        self.b_once = QPushButton("선택 1회 송신")
        lbar = QHBoxLayout()
        for b in (self.b_add_msg, self.b_add_raw, self.b_add_load, self.b_dup, self.b_del):
            lbar.addWidget(b)
        lbar.addStretch(1)
        lbar.addWidget(self.b_once)
        self.total = QLabel("")
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addLayout(lbar)
        ll.addWidget(self.table, 1)
        ll.addWidget(self.total)

        # ---- editor
        self.ed_box = QGroupBox("선택한 항목")
        form = QFormLayout()
        self.e_iface = QComboBox()
        self.e_msg = QComboBox()
        self.e_msg.setEditable(True)
        self.e_msg.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.e_msg.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.e_msg.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.e_id = QLineEdit()
        self.e_id.setPlaceholderText("예: 0x1FFFFF00")
        self.e_ext = QCheckBox("29비트")
        self.e_fd = QCheckBox("FD")
        self.e_brs = QCheckBox("BRS")
        flags = QHBoxLayout()
        for w in (self.e_ext, self.e_fd, self.e_brs):
            flags.addWidget(w)
        flags.addStretch(1)
        self.e_len = QSpinBox()
        self.e_len.setRange(0, 64)
        self.e_len.setSpecialValueText("데이터 길이대로")
        self.e_data = QLineEdit()
        self.e_data.setPlaceholderText("16진수, 예: 00 00 B8 0B 00 00")
        self.e_period = QDoubleSpinBox()
        self.e_period.setRange(0, 600_000)
        self.e_period.setDecimals(2)
        self.e_period.setSuffix(" ms")
        self.e_period.setSpecialValueText("1회")
        self.e_count = QSpinBox()
        self.e_count.setRange(0, 10_000_000)
        self.e_count.setSpecialValueText("제한 없음")
        self.e_load = QDoubleSpinBox()
        self.e_load.setRange(0.1, 100)
        self.e_load.setDecimals(1)
        self.e_load.setSuffix(" %")
        self.e_note = QLineEdit()
        self.e_note.setPlaceholderText("메모 (선택)")
        self.sigs = QTableWidget(0, 4)
        self.sigs.setHorizontalHeaderLabels(["신호", "값", "단위", "범위 / 선택값"])
        self.sigs.verticalHeader().setVisible(False)
        self.sigs.verticalHeader().setDefaultSectionSize(22)
        self.sigs.horizontalHeader().setStretchLastSection(True)
        self.sigs.setColumnWidth(0, 170)
        self.sigs.setColumnWidth(1, 110)
        self.sigs.setColumnWidth(2, 50)
        self.preview = QLabel("")
        self.preview.setWordWrap(True)
        self.preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.preview.setFont(mono())
        self.rows = {}
        for label, w in (("채널", self.e_iface), ("메시지", self.e_msg), ("CAN ID", self.e_id), ("형식", flags),
                         ("길이", self.e_len), ("데이터", self.e_data), ("주기", self.e_period), ("횟수", self.e_count),
                         ("목표 부하", self.e_load), ("메모", self.e_note)):
            if isinstance(w, QHBoxLayout):
                holder = QWidget()
                holder.setLayout(w)
                w.setContentsMargins(0, 0, 0, 0)
                w = holder
            form.addRow(label, w)
            self.rows[label] = w
        self.form = form
        el = QVBoxLayout(self.ed_box)
        el.addLayout(form)
        self.sig_label = QLabel("신호 값 (비워 두면 DBC 초기값 또는 0)")
        el.addWidget(self.sig_label)
        el.addWidget(self.sigs, 1)
        el.addWidget(self.preview)
        el.addStretch(0)               # takes the space when the signal table is hidden

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(self.ed_box)
        split.setSizes([900, 420])
        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(split, 1)

        self.b_arm.toggled.connect(self._arm_toggled)
        self.b_stop.clicked.connect(self.stop_all)
        self.esc = QShortcut(QKeySequence("Esc"), self)
        self.esc.setContext(Qt.ShortcutContext.WindowShortcut)
        self.esc.activated.connect(self.stop_all)
        self.auto.valueChanged.connect(self._auto_changed)
        self.b_add_msg.clicked.connect(lambda: self.add(TxEntry(kind=KIND_MSG)))
        self.b_add_raw.clicked.connect(lambda: self.add(TxEntry(kind=KIND_RAW, can_id=LOAD_ID, data=bytes(8), period_ms=100)))
        self.b_add_load.clicked.connect(lambda: self.add(TxEntry(kind=KIND_LOAD, can_id=LOAD_ID, length=64, load_pct=10)))
        self.b_dup.clicked.connect(self._duplicate)
        self.b_del.clicked.connect(self._delete)
        self.b_once.clicked.connect(self.send_selected_once)
        self.b_save.clicked.connect(self._save_file)
        self.b_load.clicked.connect(self._load_file)
        self.table.itemSelectionChanged.connect(self._show_editor)
        self.table.itemChanged.connect(self._check_changed)
        self.e_iface.activated.connect(lambda _i: self._edited("iface"))
        self.e_msg.activated.connect(lambda _i: self._edited("message"))
        self.e_msg.lineEdit().editingFinished.connect(lambda: self._edited("message"))
        self.e_id.editingFinished.connect(lambda: self._edited("id"))
        for w in (self.e_ext, self.e_fd, self.e_brs):
            w.toggled.connect(lambda _v: self._edited("flags"))
        self.e_len.valueChanged.connect(lambda _v: self._edited("len"))
        self.e_data.editingFinished.connect(lambda: self._edited("data"))
        self.e_period.valueChanged.connect(lambda _v: self._edited("period"))
        self.e_count.valueChanged.connect(lambda _v: self._edited("count"))
        self.e_load.valueChanged.connect(lambda _v: self._edited("load"))
        self.e_note.editingFinished.connect(lambda: self._edited("note"))
        self.sigs.itemChanged.connect(self._sig_item_changed)
        self._load_settings()
        self._update_arm_ui()
        self._show_editor()

    # ------------------------------------------------------------ measurement lifecycle
    def begin(self, session, demo=False):
        """A measurement started. Transmission stays off until switched on."""
        self.disarm()
        self.session, self.demo = session, demo
        self.e_iface.clear()
        self.e_iface.addItems(list(getattr(session.source, "ifaces", [])))
        self._rebuild_table()
        self._update_arm_ui()

    def end(self):
        """The measurement stops: stop sending and switch off."""
        self.disarm()
        self.session = None
        self._update_arm_ui()

    @property
    def armed(self):
        return self.tx is not None

    def _transmit_ifaces(self):
        """Interfaces of this measurement that may send, and a reason when there are none."""
        ifaces = list(getattr(self.session.source, "ifaces", []))
        if self.demo:
            return set(ifaces), ""
        infos, err = hwconfig.read_interfaces()
        ok = {i.name for i in infos if i.name in ifaces and i.up and (i.kind == "vcan" or not i.listen_only)}
        return ok, err

    def arm(self, confirm=True):
        if self.armed:
            return True
        if self.session is None or not hasattr(self.session.source, "send"):
            QMessageBox.information(self, "메시지 송신", "측정 중일 때만 보낼 수 있습니다.")
            return False
        ok, err = self._transmit_ifaces()
        if not ok:
            QMessageBox.information(
                self, "보낼 수 있는 채널이 없습니다",
                "측정 중인 채널이 모두 수신 전용입니다. 측정을 멈춘 뒤 하드웨어 설정에서 보낼 채널의 모드를 "
                "\"송신 가능\"으로 바꾸고 \"적용\"을 누르세요." + (f"\n\n{err}" if err else ""))
            return False
        if confirm:
            where = "미리 보기라 실제 버스에는 보내지 않습니다." if self.demo else "분석기가 실제 버스에 프레임을 보냅니다."
            ask = QMessageBox.question(
                self, "송신 모드 켜기",
                f"{where}\n\n보낼 수 있는 채널: {', '.join(sorted(ok))}\n\n"
                "로봇이 움직이고 있다면 먼저 멈추세요. 전체 정지는 Esc입니다. 켤까요?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if ask != QMessageBox.StandardButton.Yes:
                return False
        self.allowed = ok
        self.tx = Transmitter(self.session.source, self.auto.value())
        self._update_arm_ui()
        self.armedChanged.emit(True)
        return True

    def disarm(self):
        tx, self.tx = self.tx, None
        if tx is not None:
            tx.close()
            self.allowed = set()
            self.armedChanged.emit(False)
        self._set_checks(set())
        self._update_arm_ui()

    def _arm_toggled(self, on):
        if on and not self.armed:
            if not self.arm():
                self.b_arm.blockSignals(True)
                self.b_arm.setChecked(False)
                self.b_arm.blockSignals(False)
        elif not on and self.armed:
            self.disarm()

    def _auto_changed(self, v):
        if self.tx is not None:
            self.tx.auto_stop_s = v

    def _update_arm_ui(self):
        on = self.armed
        self.b_arm.blockSignals(True)
        self.b_arm.setChecked(on)
        self.b_arm.blockSignals(False)
        self.b_arm.setText("송신 모드 끄기" if on else "송신 모드 켜기")
        self.b_arm.setEnabled(self.session is not None)
        if on:
            target = f"채널 {', '.join(sorted(self.allowed))}" + (" · 미리 보기라 버스에 보내지 않음" if self.demo else "")
            self.state.setText(f"  송신 모드 켜짐 · {target}  ")
            self.state.setStyleSheet("background:#fbe3df; color:#bd3626;" + PILL)
        else:
            self.state.setText("  송신 모드 꺼짐 · 분석기는 아무것도 보내지 않습니다  ")
            self.state.setStyleSheet("background:#e2f3ea; color:#2b8455;" + PILL)
        self.b_once.setEnabled(on)
        self.b_stop.setEnabled(on)

    # ------------------------------------------------------------ list
    def add(self, entry, select=True):
        if not entry.iface and self.e_iface.count():
            entry.iface = self.e_iface.itemText(0)
        uid = next(self._uids)
        self.entries.append((uid, entry))
        self._rebuild_table()
        if select:
            self.table.selectRow(len(self.entries) - 1)
        self._save_settings()
        return uid

    def _selected(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return rows[0].row() if rows else None

    def _duplicate(self):
        r = self._selected()
        if r is not None:
            self.add(self.entries[r][1].copy())

    def _delete(self):
        r = self._selected()
        if r is None:
            return
        uid, _ = self.entries.pop(r)
        if self.tx is not None:
            self.tx.stop(uid)
        self._rebuild_table()
        self._save_settings()

    def _db(self, iface):
        return self.session.meta.dbs.get(iface) if self.session is not None else None

    def _rates(self, iface):
        return self.session.meta.rates(iface) if self.session is not None else (1.0, 5.0)

    def _build(self, entry):
        """(TxFrame, period seconds, None) or (None, None, error text)."""
        try:
            frame = build(entry, self._db(entry.iface))
        except ValueError as e:
            return None, None, str(e)
        return frame, period_s(entry, frame, self._rates(entry.iface)), None

    def _rebuild_table(self):
        self._loading = True
        self.table.setRowCount(len(self.entries))
        for r, (uid, e) in enumerate(self.entries):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
            chk.setCheckState(Qt.CheckState.Checked if self.tx is not None and self.tx.running(uid) else Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, uid)
            self.table.setItem(r, 0, chk)
            for c in range(1, len(COLS)):
                self.table.setItem(r, c, QTableWidgetItem(""))
            self._fill_row(r)
        self._loading = False
        self._update_total()

    def _fill_row(self, r):
        uid, e = self.entries[r]
        frame, period, err = self._build(e)
        cells = {"채널": e.iface, "종류": KIND_TEXT[e.kind]}
        if frame is None:
            cells.update({"CAN ID": f"0x{e.can_id:X}" if e.kind != KIND_MSG else "-",
                          "메시지": e.message or "(메시지를 고르세요)", "주기 / 부하": "", "횟수": "", "추가 부하": "",
                          "길이": "-", "데이터": err})
        else:
            if e.kind == KIND_LOAD:
                timing = f"{e.load_pct:g} %"
            else:
                timing = f"{e.period_ms:g} ms" if e.period_ms else "1회"
            load = added_load(e, frame, self._rates(e.iface))
            name = e.message if e.kind == KIND_MSG else (e.note or ("더미" if e.kind == KIND_LOAD else "-"))
            cells.update({"CAN ID": f"0x{frame.can_id:X}", "메시지": name, "주기 / 부하": timing,
                          "횟수": "-" if not period else (str(e.count) if e.count else "∞"),
                          "추가 부하": f"{load:.1f} %" if load else "-", "길이": str(len(frame.data)),
                          "데이터": hexdump(frame.data, 16)})
        for key, t in cells.items():
            item = self.table.item(r, C[key])
            item.setText(t)
            item.setForeground(QBrush(RED if frame is None and key in ("메시지", "데이터") else QColor("#1c2530")))

    def _update_total(self):
        if self.session is None:
            self.total.setText("측정을 시작하면 이 목록의 메시지를 보낼 수 있습니다.")
            return
        per = {}
        for uid, e in self.entries:
            if self.tx is None or not self.tx.running(uid):
                continue
            frame, period, err = self._build(e)
            if frame is not None and period:
                per[e.iface] = per.get(e.iface, 0.0) + added_load(e, frame, self._rates(e.iface))
        if not per:
            self.total.setText("보내는 중인 주기 메시지 없음")
            self.total.setStyleSheet("color:#5b6978;")
            return
        hi = max(per.values())
        self.total.setText("추가 부하 (계산값): " + ",  ".join(f"{k} {v:.1f} %" for k, v in sorted(per.items()))
                           + ("   — 버스가 포화되면 원래 메시지가 밀립니다." if hi >= LOAD_WARN_PCT else ""))
        self.total.setStyleSheet(f"color:{'#bd3626' if hi >= LOAD_WARN_PCT else '#1c2530'}; font-weight:600;")

    # ------------------------------------------------------------ sending
    def _check_changed(self, item):
        if self._loading or item.column() != 0:
            return
        r = item.row()
        uid, e = self.entries[r]
        if item.checkState() == Qt.CheckState.Checked:
            if not self.start(uid):
                self._loading = True
                item.setCheckState(Qt.CheckState.Unchecked)
                self._loading = False
        elif self.tx is not None:
            self.tx.stop(uid)
        self._update_total()

    def _status(self, r, text, color=GREY):
        item = self.table.item(r, C["상태"])
        item.setText(text)
        item.setForeground(QBrush(color))

    def start(self, uid):
        """Start sending one entry (periodic, or once when its period is 0)."""
        r = next((k for k, (u, _) in enumerate(self.entries) if u == uid), None)
        if r is None:
            return False
        e = self.entries[r][1]
        if self.tx is None:
            self._status(r, "송신 모드가 꺼져 있습니다", RED)
            return False
        if e.iface not in self.allowed:
            self._status(r, f"{e.iface}는 수신 전용 채널입니다", RED)
            return False
        frame, period, err = self._build(e)
        if frame is None:
            self._status(r, err, RED)
            return False
        self.tx.start(uid, frame, period, e.count)
        self._status(r, "보내는 중" if period else "보냄", GREEN)
        return True

    def send_selected_once(self):
        r = self._selected()
        if r is None or self.tx is None:
            return False
        uid, e = self.entries[r]
        if e.iface not in self.allowed:
            self._status(r, f"{e.iface}는 수신 전용 채널입니다", RED)
            return False
        frame, _, err = self._build(e)
        if frame is None:
            self._status(r, err, RED)
            return False
        self.tx.once(frame)
        self._status(r, "1회 보냄", GREEN)
        return True

    def stop_all(self):
        if self.tx is not None:
            self.tx.stop_all()
        self._set_checks(set())
        self._update_total()

    def _set_checks(self, running):
        self._loading = True
        for r, (uid, _) in enumerate(self.entries):
            item = self.table.item(r, 0)
            if item is not None:
                item.setCheckState(Qt.CheckState.Checked if uid in running else Qt.CheckState.Unchecked)
        self._loading = False

    def refresh(self):
        """Called from the measurement timer: counters, finished entries, scheduler errors."""
        if self.tx is None:
            return
        if self.tx.error or not self.tx.alive:
            err = self.tx.error or "송신 스레드가 멈췄습니다."
            self.disarm()
            QMessageBox.warning(self, "송신을 멈췄습니다", err)
            return
        status = self.tx.status()
        running = {k for k, s in status.items() if not s["done"]}
        self._loading = True
        for r, (uid, _) in enumerate(self.entries):
            s = status.get(uid)
            if s is None:
                continue
            extra = []
            if s["full"]:
                extra.append(f"큐 가득 {s['full']:,}")
            if s["skipped"]:
                extra.append(f"지연으로 건너뜀 {s['skipped']:,}")
            self.table.item(r, C["보낸 수"]).setText(f"{s['sent']:,}")
            if s["done"]:
                text = REASON_TEXT.get(s["reason"], s["reason"])
                self._status(r, " · ".join([text] + extra), RED if s["reason"].startswith("송신 오류") else GREY)
                self.table.item(r, 0).setCheckState(Qt.CheckState.Unchecked)
            else:
                self._status(r, " · ".join(["보내는 중"] + extra), GREEN)
        self._loading = False
        self._update_total()
        self.b_stop.setText(f"■ 전체 정지 (Esc) · {len(running)}개 송신 중" if running else "■ 전체 정지 (Esc)")

    # ------------------------------------------------------------ editor
    def _show_editor(self):
        r = self._selected()
        self.ed_box.setEnabled(r is not None)
        if r is None:
            self.preview.setText("")
            self.sigs.setRowCount(0)
            return
        e = self.entries[r][1]
        self._loading = True
        if self.e_iface.findText(e.iface) < 0 and e.iface:
            self.e_iface.addItem(e.iface)
        self.e_iface.setCurrentText(e.iface)
        is_msg, is_load = e.kind == KIND_MSG, e.kind == KIND_LOAD
        for label, show in (("메시지", is_msg), ("CAN ID", not is_msg), ("형식", not is_msg), ("길이", not is_msg),
                            ("데이터", e.kind == KIND_RAW), ("주기", not is_load), ("목표 부하", is_load)):
            self.form.setRowVisible(self.rows[label], show)
        self.sig_label.setVisible(is_msg)
        self.sigs.setVisible(is_msg)
        self.e_msg.clear()
        db = self._db(e.iface)
        if db is not None:
            self.e_msg.addItems(sorted(m.name for m in db.messages))
        self.e_msg.setCurrentText(e.message)
        self.e_id.setText(f"0x{e.can_id:X}")
        self.e_ext.setChecked(e.ext)
        self.e_fd.setChecked(e.fd)
        self.e_brs.setChecked(e.brs)
        self.e_len.setValue(e.length)
        self.e_data.setText(" ".join(f"{b:02X}" for b in e.data))
        self.e_period.setValue(e.period_ms)
        self.e_count.setValue(e.count)
        self.e_load.setValue(e.load_pct)
        self.e_note.setText(e.note)
        self._loading = False
        self._fill_signals(e)
        self._update_preview(e)

    def _fill_signals(self, e):
        self._loading = True
        self.sigs.setRowCount(0)
        db = self._db(e.iface)
        msg = None
        if e.kind == KIND_MSG and db is not None and e.message:
            try:
                msg = db.get_message_by_name(e.message)
            except KeyError:
                msg = None
        if msg is not None:
            sigs = active_signals(msg, e.values)
            self.sigs.setRowCount(len(sigs))
            for r, s in enumerate(sigs):
                name = QTableWidgetItem(s.name + (" (다중화)" if s.is_multiplexer else ""))
                name.setFlags(Qt.ItemFlag.ItemIsEnabled)
                name.setData(Qt.ItemDataRole.UserRole, s.name)
                self.sigs.setItem(r, 0, name)
                cur = e.values.get(s.name, default_value(s))
                if s.choices:
                    box = QComboBox()
                    for num, label in sorted(s.choices.items()):
                        box.addItem(f"{num} {label}", str(label))
                    idx = next((k for k, (num, label) in enumerate(sorted(s.choices.items()))
                                if str(label) == str(cur) or num == cur), -1)
                    if idx < 0:
                        box.addItem(str(cur), str(cur))
                        idx = box.count() - 1
                    box.setCurrentIndex(idx)
                    box.activated.connect(lambda _i, n=s.name, b=box: self._sig_set(n, b.currentData()))
                    self.sigs.setCellWidget(r, 1, box)
                else:
                    val = QTableWidgetItem(str(cur))
                    val.setData(Qt.ItemDataRole.UserRole, s.name)
                    self.sigs.setItem(r, 1, val)
                unit = QTableWidgetItem(s.unit or "")
                unit.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.sigs.setItem(r, 2, unit)
                lo = "" if s.minimum is None else f"{s.minimum:g}"
                hi = "" if s.maximum is None else f"{s.maximum:g}"
                rng = QTableWidgetItem(f"{lo} … {hi}" if lo or hi else "")
                rng.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.sigs.setItem(r, 3, rng)
        self._loading = False

    def _sig_item_changed(self, item):
        if self._loading or item.column() != 1:
            return
        self._sig_set(item.data(Qt.ItemDataRole.UserRole), parse_value(item.text()))

    def _sig_set(self, name, value):
        r = self._selected()
        if r is None:
            return
        e = self.entries[r][1]
        if isinstance(value, str):
            value = parse_value(value) if value.strip().lstrip("-").replace(".", "", 1).isdigit() else value
        e.values[name] = value
        db = self._db(e.iface)
        msg = db.get_message_by_name(e.message) if db is not None else None
        if msg is not None and any(s.name == name and s.is_multiplexer for s in msg.signals):
            keep = {s.name for s in active_signals(msg, e.values)}
            e.values = {k: v for k, v in e.values.items() if k in keep}
            self._fill_signals(e)
            self._default_period(e, msg)
        self._entry_changed(r)

    def set_signal(self, name, value):
        """Set one signal value of the selected entry (tests and scripting)."""
        self._sig_set(name, value)

    def _default_period(self, e, msg):
        """Fill the period from the DBC design period of the selected message part."""
        try:
            frame = build(e, self._db(e.iface))
        except ValueError:
            return
        _, sub = key_of(frame.can_id, frame.data)
        p = design_period(msg, sub)
        e.period_ms = float(p) if p else 0.0
        self._loading = True
        self.e_period.setValue(e.period_ms)
        self._loading = False

    def _edited(self, what):
        if self._loading:
            return
        r = self._selected()
        if r is None:
            return
        e = self.entries[r][1]
        if what == "iface":
            e.iface = self.e_iface.currentText()
            self._show_editor()
        elif what == "message":
            name = self.e_msg.currentText().strip()
            if name == e.message:
                return
            e.message, e.values = name, {}
            db = self._db(e.iface)
            msg = None
            if db is not None:
                try:
                    msg = db.get_message_by_name(name)
                except KeyError:
                    msg = None
            if msg is not None:
                self._default_period(e, msg)
            self._fill_signals(e)
        elif what == "id":
            try:
                e.can_id = int(self.e_id.text().strip(), 16)
            except ValueError:
                self.preview.setText("CAN ID는 16진수로 입력합니다.")
                return
        elif what == "flags":
            e.ext, e.fd, e.brs = self.e_ext.isChecked(), self.e_fd.isChecked(), self.e_brs.isChecked()
        elif what == "len":
            e.length = self.e_len.value()
        elif what == "data":
            try:
                e.data = bytes.fromhex(self.e_data.text().replace(",", " "))
            except ValueError:
                self.preview.setText("데이터는 16진수 바이트로 입력합니다 (예: 01 02 0A).")
                self.preview.setStyleSheet("color:#bd3626;")
                return
        elif what == "period":
            e.period_ms = self.e_period.value()
        elif what == "count":
            e.count = self.e_count.value()
        elif what == "load":
            e.load_pct = self.e_load.value()
        elif what == "note":
            e.note = self.e_note.text()
        self._entry_changed(r)

    def _entry_changed(self, r):
        uid, e = self.entries[r]
        self._loading = True
        self._fill_row(r)
        self._loading = False
        self._update_preview(e)
        if self.tx is not None and self.tx.running(uid):
            frame, period, err = self._build(e)
            if frame is None or e.iface not in self.allowed:
                self.tx.stop(uid, "error")
                self._status(r, err or f"{e.iface}는 수신 전용 채널입니다", RED)
            else:
                self.tx.update(uid, frame, period, e.count)
        self._update_total()
        self._save_settings()

    def _update_preview(self, e):
        frame, period, err = self._build(e)
        if frame is None:
            self.preview.setText(err)
            self.preview.setStyleSheet("color:#bd3626;")
            return
        kind = ("FD" + (" BRS" if frame.brs else "")) if frame.fd else "Classic"
        lines = [f"0x{frame.can_id:X}  [{len(frame.data)}]  {kind}", hexdump(frame.data, 64)]
        if period:
            fps = 1 / period
            lines.append(f"{period * 1e3:.3f} ms 간격 · {fps:,.0f} 프레임/s · 추가 부하 약 "
                         f"{added_load(e, frame, self._rates(e.iface)):.1f} %"
                         + ("  (최대 송신 속도로 제한됨)" if fps >= MAX_FPS * 0.999 else ""))
        else:
            lines.append("체크하면 한 번 보냅니다.")
        self.preview.setText("\n".join(lines))
        self.preview.setStyleSheet("color:#1c2530;")

    # ------------------------------------------------------------ persistence
    def to_json(self):
        return json.dumps([e.to_dict() for _, e in self.entries], ensure_ascii=False, indent=1)

    def from_json(self, text):
        items = [TxEntry.from_dict(d) for d in json.loads(text or "[]")]
        for uid, _ in self.entries:
            if self.tx is not None:
                self.tx.stop(uid)
        self.entries = [(next(self._uids), e) for e in items]
        self._rebuild_table()
        self._show_editor()

    def _save_settings(self):
        if self.settings is not None:
            self.settings.setValue("tx/list", self.to_json())

    def _load_settings(self):
        if self.settings is None:
            return
        try:
            self.from_json(self.settings.value("tx/list", "[]") or "[]")
        except (ValueError, TypeError):
            self.entries = []

    def _save_file(self):
        path, _ = QFileDialog.getSaveFileName(self, "송신 목록 저장", "tx_list.json", "송신 목록 (*.json)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.to_json())

    def _load_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "송신 목록 불러오기", "", "송신 목록 (*.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                self.from_json(f.read())
        except (OSError, ValueError, TypeError) as e:
            QMessageBox.warning(self, "목록을 불러오지 못했습니다", f"{path}\n\n{e}")
            return
        self._save_settings()
