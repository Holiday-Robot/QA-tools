"""Main window of the CAN monitor: toolbar, channel dock, DBC browser, tabs and status bar.

Runs on its own (qa canmon view) or as a page inside the QA Tools window (qa gui canmon).
"""
from __future__ import annotations

import argparse
import json
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import pyqtgraph as pg
from PyQt6.QtCore import QSettings, Qt, QTimer
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (QApplication, QComboBox, QDockWidget, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                             QLineEdit, QMainWindow, QMessageBox, QPushButton, QSlider, QStackedWidget, QTabWidget, QToolBar,
                             QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from qatools.shell.app import app_icon
from qatools.shell.app import make_app as _make_app

from .. import hwconfig
from ..core import DBC_DIR, Capture, Session, dbc_files
from ..live import LiveMeta, LiveSession, ReplaySource, SocketSource
from .state import AppState
from .graph import GraphTab
from .hw import AccessDialog
from .hwconfig import HardwareDialog
from .live import LivePage
from .period import PeriodTab
from .trace import TraceTab

NO_DBC = "(DBC 없음)"
BROWSE = "__browse__"
SPEEDS = [0.1, 0.5, 1.0, 2.0, 5.0]
TICK_MS = 50
LIVE_TICK_MS = 100
BIG_LOG_FRAMES = 1_500_000        # ask before loading a log larger than this into the offline viewer
PILL = "font-weight:600; border-radius:9px; padding:3px 8px;"


def setup_plots():
    # Antialiased wide pens make pyqtgraph redraw several times slower, which shows on software-rendered displays (WSLg).
    pg.setConfigOptions(antialias=False, background="w", foreground="#1c2530")


def parse_args(argv):
    """Arguments of the CAN monitor window, shared by qa canmon view and qa gui canmon."""
    ap = argparse.ArgumentParser(prog="qa canmon view", description="FM26 CAN Monitor window")
    ap.add_argument("capture", nargs="?", help="capture CSV written by qa canmon capture or a live measurement")
    ap.add_argument("--map", action="append", default=[], metavar="IFACE=DBC", help="e.g. can0=HETC-L-ARM_ch0")
    a = ap.parse_args(argv)
    bad = [m for m in a.map if "=" not in m]
    if bad:
        ap.error(f"--map needs IFACE=DBC, got {bad[0]!r}")
    return a


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        setup_plots()
        self.state = AppState()
        self.settings = QSettings("HolidayRobotics", "canmon")
        self.setWindowTitle("FM26 CAN Monitor")
        self.setWindowIcon(app_icon())
        self.resize(1600, 950)

        # ---- tabs
        self.trace = TraceTab(self.state)
        self.period = PeriodTab(self.state)
        self.graph = GraphTab(self.state)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.trace, "Trace")
        self.tabs.addTab(self.period, "주기 통계")
        self.tabs.addTab(self.graph, "그래프")
        self.live_page = LivePage(self.settings)
        self.live_page.tx.armedChanged.connect(lambda _on: self._update_mode())
        self.stack = QStackedWidget()
        self.stack.addWidget(self.tabs)
        self.stack.addWidget(self.live_page)
        self.setCentralWidget(self.stack)
        self.live = None
        self.live_demo = False
        self._rate = {}
        self.live_timer = QTimer(self)
        self.live_timer.setInterval(LIVE_TICK_MS)
        self.live_timer.timeout.connect(self._live_tick)

        # ---- menu
        m_file = self.menuBar().addMenu("파일")
        m_tool = self.menuBar().addMenu("도구")
        self.a_hw = QAction("하드웨어 설정…", self)
        self.a_hw.triggered.connect(self.show_hardware)
        m_tool.addAction(self.a_hw)
        self.a_live = QAction("● 측정 시작", self)
        self.a_live.triggered.connect(self.toggle_live)
        m_tool.addAction(self.a_live)
        self.a_demo = QAction("실시간 화면 미리 보기 (연 파일을 실시간처럼 재생)", self)
        self.a_demo.triggered.connect(self.start_demo)
        m_tool.addAction(self.a_demo)
        self.a_access = QAction("CAN 설정 권한 설치…", self)
        self.a_access.triggered.connect(self.show_access)
        m_tool.addAction(self.a_access)

        # ---- toolbar
        tb = QToolBar("도구")
        tb.setMovable(False)
        self.addToolBar(tb)
        self.a_open = QAction("캡처 파일 열기", self)
        self.a_open.triggered.connect(self.open_dialog)
        tb.addAction(self.a_open)
        m_file.addAction(self.a_open)
        a_quit = QAction("끝내기", self)
        a_quit.triggered.connect(lambda: self.window().close())   # the shell window when embedded
        m_file.addAction(a_quit)
        tb.addAction(self.a_hw)
        tb.addAction(self.a_live)
        tb.addSeparator()
        self.a_play = QAction("▶ 재생", self)
        self.a_play.triggered.connect(self.toggle_play)
        tb.addAction(self.a_play)
        self.speed = QComboBox()
        for s in SPEEDS:
            self.speed.addItem(f"{s:g}배속", s)
        self.speed.setCurrentIndex(SPEEDS.index(1.0))
        tb.addWidget(self.speed)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setMinimumWidth(260)
        self.slider.valueChanged.connect(self._slider_moved)
        tb.addWidget(self.slider)
        self.clock = QLabel("  -")
        tb.addWidget(self.clock)
        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().Policy.Expanding, spacer.sizePolicy().Policy.Preferred)
        tb.addWidget(spacer)
        self.mode = QLabel("  파일 재생 · 수신 전용  ")
        self.mode.setStyleSheet("background:#e2f3ea; color:#2b8455;" + PILL)
        tb.addWidget(self.mode)

        # ---- left dock
        self.dbc_dir = Path(self.settings.value("dbc_dir", str(DBC_DIR)))
        if not self.dbc_dir.is_dir():
            self.dbc_dir = DBC_DIR
        self.chan_box = QGroupBox("채널과 DBC")
        box = QVBoxLayout(self.chan_box)
        self.chan_form = QFormLayout()
        box.addLayout(self.chan_form)
        self.b_dir = QPushButton("DBC 폴더 변경…")
        self.b_reload = QPushButton("DBC 다시 읽기")
        self.b_reload.setToolTip("DBC 파일을 고친 뒤 누르면 다시 불러옵니다.")
        row = QHBoxLayout()
        row.addWidget(self.b_dir)
        row.addWidget(self.b_reload)
        box.addLayout(row)
        self.dir_label = QLabel("")
        self.dir_label.setStyleSheet("color:#5b6978; font-size:11px;")
        self.dir_label.setWordWrap(True)
        box.addWidget(self.dir_label)
        self.b_dir.clicked.connect(self.pick_dbc_dir)
        self.b_reload.clicked.connect(self.reload_dbc)
        self.combos = {}
        self.search = QLineEdit()
        self.search.setPlaceholderText("신호 이름 검색")
        self.search.setClearButtonEnabled(True)
        self.browser = QTreeWidget()
        self.browser.setHeaderHidden(True)
        dock_w = QWidget()
        dl = QVBoxLayout(dock_w)
        dl.addWidget(self.chan_box)
        dl.addWidget(QLabel("<b>DBC 탐색</b>  (체크하면 그래프에 추가)"))
        dl.addWidget(self.search)
        dl.addWidget(self.browser, 1)
        self.dock = QDockWidget("측정 설정", self)
        self.dock.setWidget(dock_w)
        self.dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable)
        self.dock.setMinimumWidth(320)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock)

        self.status = QLabel("캡처 파일을 여세요.")
        self.statusBar().addWidget(self.status, 1)

        self.timer = QTimer(self)
        self.timer.setInterval(TICK_MS)
        self.timer.timeout.connect(self._tick)
        self._building = False

        self.browser.itemChanged.connect(self._browser_checked)
        self.browser.itemExpanded.connect(self._fill_signals)
        self.search.textChanged.connect(self._search)
        self.state.sessionChanged.connect(self._session_changed)
        self.state.mappingChanged.connect(self._mapping_changed)
        self.state.signalsChanged.connect(self._sync_checks)
        self.state.timeChanged.connect(self._time_changed)
        self.state.jumpRequested.connect(lambda _t: self.tabs.setCurrentWidget(self.graph))
        self.state.gapGroupChanged.connect(self._gap_selected)

    def show_access(self):
        AccessDialog(self).exec()

    def show_hardware(self):
        HardwareDialog(self, self.settings, self.dbc_dir).exec()

    # ------------------------------------------------------------ live measurement
    def toggle_live(self):
        if self.live is None:
            self.start_live()
        else:
            self.stop_live()

    def _live_interfaces(self):
        """Interfaces to measure: the ones enabled in the hardware settings that are up right now."""
        try:
            stored = json.loads(self.settings.value("hw/config", "{}") or "{}")
        except ValueError:
            stored = {}
        infos, err = hwconfig.read_interfaces()
        up = [i.name for i in infos if i.up]
        wanted = [n for n, d in stored.items() if d.get("enabled", True)] or up
        return [n for n in wanted if n in up], err

    def start_live(self, source=None, mapping=None, log_path=None, demo=False):
        """Start measuring. Without arguments this uses the channels applied in the hardware settings."""
        if self.live is not None:
            return False
        if source is None:
            ifaces, err = self._live_interfaces()
            if not ifaces:
                QMessageBox.information(self, "측정할 채널이 없습니다",
                                        "켜져 있는 CAN 채널이 없습니다. 하드웨어 설정에서 채널을 고르고 \"적용\"을 누르세요."
                                        + (f"\n\n{err}" if err else ""))
                self.show_hardware()
                return False
            try:
                source = SocketSource(ifaces)
            except OSError as e:
                QMessageBox.warning(self, "CAN 채널을 열 수 없습니다", f"{', '.join(ifaces)}\n\n{e}")
                return False
            mapping = {i: self.settings.value(f"map/{i}", "") for i in ifaces}
            log_dir = Path(self.settings.value("log_dir", str(Path.home() / "canmon_logs")))
            log_path = log_dir / f"can_{datetime.now():%Y%m%d_%H%M%S}.csv"
        meta = LiveMeta(mapping or {}, self.dbc_dir)
        self.timer.stop()
        self.a_play.setText("▶ 재생")
        self.live = LiveSession(source, meta, log_path, self.live_page.period.limit.value())
        self.live_demo = demo
        self._rate = {}
        try:
            self.live.start()
        except OSError as e:
            self.live = None
            source.close()
            QMessageBox.warning(self, "로그 파일을 만들 수 없습니다", f"{log_path}\n\n{e}")
            return False
        self.live_page.begin(self.live, demo)
        self.stack.setCurrentWidget(self.live_page)
        self._set_live_ui(True)
        self.live_timer.start()
        if meta.errors:
            # No modal box here: measuring must not wait for someone to click it away.
            names = ", ".join(e.split(":")[0] for e in meta.errors)
            self.statusBar().showMessage(f"DBC를 불러오지 못해 {names}은(는) DBC 없이 측정합니다.", 15000)
        return True

    def start_demo(self):
        """Feed the open capture through the live screens at its recorded pace. Not a measurement."""
        ses = self.state.session
        if ses is None:
            QMessageBox.information(self, "미리 보기", "먼저 캡처 파일을 여세요. 그 파일을 실시간처럼 재생합니다.")
            return False
        mapping = {i: str(p) for i, p in ses.chmap.items()}
        return self.start_live(ReplaySource(ses.cap.path), mapping, None, demo=True)

    def stop_live(self, open_log=True):
        if self.live is None:
            return
        self.live_timer.stop()
        self.live_page.end()            # stop sending before the sockets close
        live, self.live = self.live, None
        live.stop()
        self._set_live_ui(False)
        self.stack.setCurrentWidget(self.tabs)
        total = live.stats.total
        if live.error:
            QMessageBox.warning(self, "측정이 중단되었습니다", live.error)
        if live.log_path is None or total == 0:
            self.statusBar().showMessage("미리 보기를 끝냈습니다." if self.live_demo else "받은 프레임이 없습니다.", 5000)
            if self.state.session is not None:
                self._update_status()
            return
        if not open_log:
            return
        if total > BIG_LOG_FRAMES:
            ask = QMessageBox.question(self, "로그 열기", f"로그가 큽니다 ({total:,} 프레임). 지금 열면 시간이 걸리고 메모리를 "
                                                         f"많이 씁니다. 열까요?\n\n{live.log_path}")
            if ask != QMessageBox.StandardButton.Yes:
                self.status.setText(f"로그를 저장했습니다: {live.log_path}")
                return
        self.open(str(live.log_path), {i: str(p) for i, p in live.meta.paths.items()})

    def _set_live_ui(self, on):
        for w in (self.a_open, self.a_play, self.a_hw, self.a_demo, self.speed, self.slider, self.dock):
            w.setEnabled(not on)
        self.a_live.setText("■ 측정 정지" if on else "● 측정 시작")
        self._update_mode()

    def _update_mode(self):
        armed = self.live is not None and self.live_page.tx.armed
        if self.live is None:
            text, style = "파일 재생 · 수신 전용", "background:#e2f3ea; color:#2b8455;"
        elif self.live_demo:
            text = "실시간 화면 미리 보기 (측정 아님)" + (" · 송신 모드 (버스에 안 보냄)" if armed else "")
            style = "background:#fcf1d8; color:#a56a12;"
        elif armed:
            text, style = "실시간 측정 · 송신 모드 켜짐", "background:#fbe3df; color:#bd3626;"
        else:
            text, style = "실시간 측정 · 수신 전용", "background:#e1eef6; color:#1f6f9f;"
        self.mode.setText(f"  {text}  ")
        self.mode.setStyleSheet(style + PILL)

    def _live_tick(self):
        live = self.live
        if live is None:
            return
        groups, ifaces, elapsed, since = live.stats.snapshot()
        self.live_page.refresh(groups, since)
        now = time.monotonic()
        dropped = live.dropped()
        parts = []
        for name in sorted(ifaces):
            ic = ifaces[name]
            hist = self._rate.setdefault(name, deque())
            hist.append((now, ic.frames, ic.busy_us))
            while len(hist) > 2 and now - hist[0][0] > 1.0:
                hist.popleft()
            t0, f0, b0 = hist[0]
            dt = now - t0
            fps = (ic.frames - f0) / dt if dt > 0 and ic.frames >= f0 else 0.0
            load = (ic.busy_us - b0) / 1e6 / dt * 100 if dt > 0 and ic.busy_us >= b0 else 0.0
            text = f"{name}  부하 {load:.1f} %  ·  {fps:,.0f} 프레임/s  ·  오류 프레임 {ic.errors}"
            if ic.tx:
                text += f"  ·  송신 {ic.tx:,}"
            if dropped.get(name):
                text += f"  ·  수신 누락 {dropped[name]:,}"
            parts.append(text)
        kind = "하드웨어" if live.stats.all_hw else "소프트웨어 (1 ms 미만 차이는 신뢰 불가)"
        parts.append(f"타임스탬프 {kind}")
        parts.append(f"로그 {live.log_path}" if live.log_path else "로그 저장 안 함 (미리 보기)")
        self.status.setText("     ".join(parts))
        self.clock.setText(f"  측정 {elapsed:8.1f} s  ·  {live.stats.total:,} 프레임")
        if live.error or (self.live_demo and getattr(live.source, "finished", False)):
            self.stop_live()

    def shutdown(self):
        """Stop measuring and sending. The shell calls this when its window closes."""
        self.stop_live(open_log=False)

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)

    def open_args(self, argv):
        """Open what the command line asks for. Returns False when the capture could not be opened."""
        a = parse_args(argv)
        if not a.capture:
            return True
        return self.open(a.capture, dict(m.split("=", 1) for m in a.map))

    # ------------------------------------------------------------ file
    def open_dialog(self):
        start = self.settings.value("last_dir", str(Path.home()))
        path, _ = QFileDialog.getOpenFileName(self, "캡처 파일 열기", start, "캡처 CSV (*.csv)")
        if path:
            self.open(path)

    def open(self, path, mapping=None):
        try:
            cap = Capture(path)
        except (OSError, ValueError, KeyError) as e:
            QMessageBox.warning(self, "파일을 열 수 없습니다", f"{path}\n\n{e}")
            return False
        self.settings.setValue("last_dir", str(Path(path).parent))
        ses = Session(cap, self.dbc_dir)
        problems = []
        for iface in cap.ifaces:
            spec = (mapping or {}).get(iface) or self.settings.value(f"map/{iface}", "")
            if not spec:
                continue
            try:
                ses.set_channel(iface, spec)
            except Exception as e:  # noqa: BLE001 - a missing or broken DBC must not block opening the capture
                problems.append(f"{iface}: {spec}\n{e}")
        self.state.set_session(ses)
        if problems:
            QMessageBox.warning(self, "DBC를 불러오지 못했습니다", "\n\n".join(problems))
        return True

    # ------------------------------------------------------------ dock
    def _session_changed(self):
        ses = self.state.session
        while self.chan_form.rowCount():
            self.chan_form.removeRow(0)
        self.combos = {}
        for iface in ses.cap.ifaces:
            combo = QComboBox()
            combo.activated.connect(lambda _i, n=iface, c=combo: self._channel_picked(n, c.currentData()))
            self.chan_form.addRow(iface, combo)
            self.combos[iface] = combo
        self._fill_combos()
        self.setWindowTitle(f"FM26 CAN Monitor — {ses.cap.path.name}")
        self._build_browser()
        self._update_status()
        self.slider.blockSignals(True)
        self.slider.setValue(1000)
        self.slider.blockSignals(False)
        self._time_changed()

    def _fill_combos(self):
        """List the DBC files of the current folder, the file in use, and an entry to browse for any file."""
        ses = self.state.session
        self.dir_label.setText(f"DBC 폴더: {self.dbc_dir}")
        if ses is None:
            return
        for iface, combo in self.combos.items():
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(NO_DBC, "")
            for path in dbc_files(self.dbc_dir):
                combo.addItem(path.stem, str(path))
            cur = ses.chmap.get(iface)
            if cur is not None and combo.findData(str(cur)) < 0:
                combo.addItem(f"{cur.stem}  (다른 폴더)", str(cur))
                combo.setItemData(combo.count() - 1, str(cur), Qt.ItemDataRole.ToolTipRole)
            combo.addItem("파일에서 선택…", BROWSE)
            combo.setCurrentIndex(max(combo.findData(str(cur) if cur else ""), 0))
            combo.setToolTip(str(cur) if cur else "")
            combo.blockSignals(False)

    def _channel_picked(self, iface, data):
        if data == BROWSE:
            start = str(self.state.session.chmap.get(iface, self.dbc_dir))
            data, _ = QFileDialog.getOpenFileName(self, f"{iface}에 쓸 DBC 파일", start, "DBC (*.dbc)")
            if not data:
                self._fill_combos()
                return
        self.apply_dbc(iface, data)

    def apply_dbc(self, iface, spec):
        """Use a DBC (path or bundled channel name) for one interface; an empty value clears it."""
        try:
            self.state.set_channel(iface, spec or None)
        except Exception as e:  # noqa: BLE001 - show the parser's message, keep the previous DBC
            QMessageBox.warning(self, "DBC를 불러오지 못했습니다", f"{spec}\n\n{e}")
            self._fill_combos()
            return False
        path = self.state.session.chmap.get(iface)
        self.settings.setValue(f"map/{iface}", str(path) if path else "")
        self._fill_combos()
        return True

    def pick_dbc_dir(self):
        path = QFileDialog.getExistingDirectory(self, "DBC 폴더 선택", str(self.dbc_dir))
        if path:
            self.set_dbc_dir(path)

    def set_dbc_dir(self, path):
        self.dbc_dir = Path(path)
        self.settings.setValue("dbc_dir", str(self.dbc_dir))
        if self.state.session is not None:
            self.state.session.dbc_dir = self.dbc_dir
        self._fill_combos()

    def reload_dbc(self):
        if self.state.session is None:
            return
        try:
            self.state.reload_dbc()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "DBC를 다시 읽지 못했습니다", str(e))
            return
        self.statusBar().showMessage("DBC를 다시 읽었습니다.", 3000)

    def _mapping_changed(self):
        self._build_browser()
        self._update_status()

    def _build_browser(self):
        self._building = True
        self.browser.clear()
        ses = self.state.session
        for iface in ses.cap.ifaces:
            top = QTreeWidgetItem(self.browser, [f"{iface} · {ses.channel_name(iface) or NO_DBC}"])
            top.setExpanded(True)
            for g in ses.cap.groups:
                if g.iface != iface or g.msg is None or not ses.signals_for(g):
                    continue
                node = QTreeWidgetItem(top, [f"{g.short}  (0x{g.can_id:06X})"])
                node.setToolTip(0, g.title)
                node.setData(0, Qt.ItemDataRole.UserRole, g)
                node.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        self._building = False
        self._search(self.search.text())

    def _fill_signals(self, node):
        g = node.data(0, Qt.ItemDataRole.UserRole)
        if g is None or node.childCount():
            return
        self._building = True
        for sig in self.state.session.signals_for(g):
            leaf = QTreeWidgetItem(node, [sig.name + (f"  [{sig.unit}]" if sig.unit else "")])
            leaf.setData(0, Qt.ItemDataRole.UserRole, (g.iface, g.can_id, sig.name))
            leaf.setFlags(leaf.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            on = self.state.has_signal(g.iface, g.can_id, sig.name)
            leaf.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self._building = False

    def _browser_checked(self, item, _col):
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if self._building or not isinstance(key, tuple):
            return
        if item.checkState(0) == Qt.CheckState.Checked:
            self.state.add_signal(*key)
            self.tabs.setCurrentWidget(self.graph)
        else:
            self.state.remove_signal(*key)

    def _sync_checks(self):
        self._building = True
        for i in range(self.browser.topLevelItemCount()):
            top = self.browser.topLevelItem(i)
            for j in range(top.childCount()):
                node = top.child(j)
                for k in range(node.childCount()):
                    leaf = node.child(k)
                    on = self.state.has_signal(*leaf.data(0, Qt.ItemDataRole.UserRole))
                    leaf.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self._building = False

    def _search(self, text):
        text = text.strip().lower()
        ses = self.state.session
        if ses is None:
            return
        for i in range(self.browser.topLevelItemCount()):
            top = self.browser.topLevelItem(i)
            for j in range(top.childCount()):
                node = top.child(j)
                g = node.data(0, Qt.ItemDataRole.UserRole)
                names = [s.name.lower() for s in ses.signals_for(g)]
                hit = not text or text in g.title.lower() or any(text in n for n in names)
                node.setHidden(not hit)
                if text and hit and any(text in n for n in names):
                    self._fill_signals(node)
                    node.setExpanded(True)
                for k in range(node.childCount()):
                    leaf = node.child(k)
                    leaf.setHidden(bool(text) and text not in leaf.text(0).lower() and text not in g.title.lower())

    def check_signal(self, iface, can_id, name):
        """Add a signal to the graph (used by the command line and tests)."""
        self.state.add_signal(iface, can_id, name)

    # ------------------------------------------------------------ status, playback
    def _update_status(self):
        ses = self.state.session
        cap = ses.cap
        parts = []
        for iface in cap.ifaces:
            load, fps, err = ses.bus_load(iface)
            parts.append(f"{iface}  부하 {load:.1f} %  ·  {fps:,.0f} 프레임/s  ·  오류 프레임 {err}")
        kind = "하드웨어" if cap.ts_kind == "hardware" else "소프트웨어 (1 ms 미만 차이는 신뢰 불가)"
        parts.append(f"타임스탬프 {kind}")
        parts.append(f"길이 {cap.duration:.2f} s  ·  {cap.n:,} 프레임")
        self.status.setText("     ".join(parts))

    def _gap_selected(self):
        if self.state.gap_group is not None:
            self.tabs.setCurrentWidget(self.graph)

    def toggle_play(self):
        if self.state.session is None:
            return
        if self.timer.isActive():
            self.timer.stop()
            self.a_play.setText("▶ 재생")
        else:
            if self.state.cur_t >= self.state.session.cap.duration:
                self.state.set_time(0.0)
            self.timer.start()
            self.a_play.setText("⏸ 일시정지")

    def _tick(self):
        t = self.state.cur_t + TICK_MS / 1000 * self.speed.currentData()
        if t >= self.state.session.cap.duration:
            self.timer.stop()
            self.a_play.setText("▶ 재생")
        self.state.set_time(t)

    def _slider_moved(self, v):
        if self.state.session is not None:
            self.state.set_time(self.state.session.cap.duration * v / 1000)

    def _time_changed(self):
        ses = self.state.session
        dur = ses.cap.duration
        self.clock.setText(f"  t = {self.state.cur_t:8.3f} s / {dur:.3f} s")
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(1000 * self.state.cur_t / dur)) if dur else 0)
        self.slider.blockSignals(False)


def make_app(argv=None):
    setup_plots()
    return _make_app(argv, "FM26 CAN Monitor")
