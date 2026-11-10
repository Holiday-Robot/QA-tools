#!/usr/bin/env python3
"""Offscreen smoke test of the viewer. Usage: python3 test_ui.py [screenshot_dir]"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
from qatools.tools.canmon import sample as sample_tool  # noqa: E402

from PyQt6.QtCore import QSettings, Qt  # noqa: E402

from qatools.tools.canmon.ui.main import MainWindow, make_app  # noqa: E402

shots = Path(sys.argv[1]) if len(sys.argv) > 1 else None
fails = []
app = make_app([])
QSettings.setDefaultFormat(QSettings.Format.IniFormat)

with tempfile.TemporaryDirectory() as d:
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, d)
    QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, d)
    sample = Path(d) / "sample.csv"
    sample_tool.main([str(sample), "--seconds", "12"])
    win = MainWindow()
    win.show()
    if not win.open(str(sample), {"can0": "HETC-L-TACT_ch0", "can1": "HETC-L-TACT_ch1"}):
        sys.exit("open failed")
    app.processEvents()
    st, ses = win.state, win.state.session

    def grab(name):
        app.processEvents()
        if shots:
            shots.mkdir(parents=True, exist_ok=True)
            win.grab().save(str(shots / f"{name}.png"))

    # ---- Trace, fixed view
    tr = win.trace
    if tr.tree.topLevelItemCount() != len(ses.cap.groups):
        fails.append("trace rows != groups")
    st.set_time(8.2050)   # inside the HTAC_61 dropout
    row = {(g.iface, g.can_id, g.part): it for g, it in tr.items}
    it61 = row[("can0", 0xF13D82, "ANGLE_MULTI")]
    it51 = row[("can1", 0xF13382, "ANGLE_MULTI")]
    if not (8.19 < float(it61.text(0)) < 8.2 and 8.2030 < float(it51.text(0)) <= 8.2050 and it51.text(5) == "16"):
        fails.append(f"trace time rows {it61.text(0)} {it51.text(0)} {it51.text(5)}")
    it51.setExpanded(True)
    app.processEvents()
    kids = {it51.child(k).text(3): it51.child(k).text(4) for k in range(it51.childCount())}
    if kids.get("Enc0_Id") != "52" or "rad" not in kids.get("Enc0_AngleRad", ""):
        fails.append(f"trace children {list(kids.items())[:5]}")
    grab("1_trace")
    # ---- Trace, chronological view with a filter
    tr.mode.setCurrentIndex(1)
    tr.filter.setText("0xf13d82")
    app.processEvents()
    want = sum(len(g.idx) for g in ses.cap.groups if g.can_id == 0xF13D82)
    if tr.model.rowCount() != want:
        fails.append(f"chrono filter {tr.model.rowCount()} != {want}")
    tr.table.selectRow(3)
    app.processEvents()
    if "SubCode" not in tr.detail.toPlainText():
        fails.append("chrono detail has no decoded signals")
    grab("2_trace_chrono")
    tr.filter.setText("")
    tr.mode.setCurrentIndex(0)

    # ---- Period statistics
    pt = win.period
    win.tabs.setCurrentWidget(pt)
    flagged = [(g, s) for g, s in pt.stats if s.flagged]
    if [(g.iface, g.can_id, g.part) for g, _ in flagged] != [("can0", 0xF13D82, "ANGLE_MULTI")]:
        fails.append(f"flagged {[(g.iface, hex(g.can_id), g.part) for g, _ in flagged]}")
    pt.table.sortItems(10, Qt.SortOrder.DescendingOrder)
    pt.table.selectRow(0)
    app.processEvents()
    if pt.table.item(0, 1).text() != "0xF13D82" or not (11.5 < float(pt.v_max.text().split()[0]) < 12.5) or pt.v_over.text() != "1회":
        fails.append(f"period detail {pt.table.item(0, 1).text()} {pt.v_max.text()} {pt.v_over.text()}")
    grab("3_period")
    st.set_cursors(1.0, 4.0)
    st.save_range("정지 구간")
    pt.range.setCurrentIndex(pt.range.findData("saved:0"))
    app.processEvents()
    if any(s.flagged for _, s in pt.stats) or "없음" not in pt.summary.text():
        fails.append("saved range 1-4 s should have no flagged message")
    out = Path(d) / "p.csv"
    pt.write_csv(out)
    if len(out.read_text(encoding="utf-8-sig").splitlines()) != len(pt.stats) + 1:
        fails.append("period csv rows")
    pt.range.setCurrentIndex(0)
    pt.table.sortItems(10, Qt.SortOrder.DescendingOrder)
    pt.table.selectRow(0)
    pt.jump.click()
    app.processEvents()

    # ---- Graph
    gt = win.graph
    if win.tabs.currentWidget() is not gt or st.gap_group is None:
        fails.append("jump did not open the graph with the gap panel")
    lo, hi = gt.p_val.viewRange()[0]
    if not (lo < 8.2103 < hi and hi - lo < 0.2):
        fails.append(f"jump view {lo} {hi}")
    for key in (("can1", 0xF13382, "Enc0_AngleRad"), ("can0", 0xF13D82, "Enc0_AngleRad"), ("can1", 0x040382, "PresentPosition"),
                ("can1", 0x040382, "PresentState")):
        win.check_signal(*key)
    app.processEvents()
    if len(gt.curves) != 4 or gt.table.rowCount() != 4:
        fails.append("graph curves/table")
    if gt.curves[("can0", 0xF13D82, "Enc0_AngleRad")].opts["symbol"] != "o":
        fails.append("sample points should show when zoomed in")
    grab("4_graph_zoom")
    gt.view_all()
    st.set_cursors(6.0, 7.25)
    app.processEvents()
    if gt.curves[("can0", 0xF13D82, "Enc0_AngleRad")].opts["symbol"] is not None:
        fails.append("sample points should hide when zoomed out")
    s = st.signals[0].series
    a_txt, b_txt, min_txt, max_txt = (gt.table.item(0, c).text() for c in (1, 2, 4, 6))
    import numpy as np
    ka, kb = np.searchsorted(s.t, 6.0, "right") - 1, np.searchsorted(s.t, 7.25, "right") - 1
    seg = s.y[np.searchsorted(s.t, 6.0):np.searchsorted(s.t, 7.25, "right")]
    if (abs(float(a_txt) - s.y[ka]) > 1e-3 or abs(float(b_txt) - s.y[kb]) > 1e-3 or abs(float(min_txt) - seg.min()) > 1e-3
            or abs(float(max_txt) - seg.max()) > 1e-3):
        fails.append(f"graph table values {a_txt} {b_txt} {min_txt} {max_txt}")
    if gt.table.item(3, 1).text() != "RUN":
        fails.append(f"enum value text {gt.table.item(3, 1).text()}")
    gt.shape.setCurrentIndex(1)
    grab("5_graph_all")
    sig_out = Path(d) / "s.csv"
    gt.write_csv(sig_out)
    if len(sig_out.read_text(encoding="utf-8-sig").splitlines()) < 1000:
        fails.append("signal csv too short")
    st.remove_signal("can1", 0x040382, "PresentState")
    app.processEvents()
    if len(gt.curves) != 3:
        fails.append("remove signal")

    # ---- playback and remapping
    st.set_time(0.0)
    win.toggle_play()
    for _ in range(3):
        win._tick()
    win.toggle_play()
    if not (0.14 < st.cur_t < 0.16):
        fails.append(f"playback time {st.cur_t}")
    # ---- DBC selection: clear, pick any file, reload after an edit, change the folder
    win.apply_dbc("can0", "")
    app.processEvents()
    if any(g.msg is not None for g in ses.cap.groups if g.iface == "can0"):
        fails.append("clearing the DBC should drop the can0 messages")
    if sorted(x.key for x in st.signals) != [("can1", 0x040382, "PresentPosition"), ("can1", 0xF13382, "Enc0_AngleRad")]:
        fails.append(f"signals of the other channel should stay: {[x.key for x in st.signals]}")
    grab("6_no_dbc")
    from qatools.tools.canmon.core import DBC_DIR
    other = Path(d) / "elsewhere"
    other.mkdir()
    custom = other / "my_ltact_ch1.dbc"
    text = (DBC_DIR / "HETC-L-TACT_ch1.dbc").read_text()
    custom.write_text(text.replace("SG_ PresentPosition ", "SG_ MotorPosition "))
    if not win.apply_dbc("can1", str(custom)):
        fails.append("custom DBC not applied")
    app.processEvents()
    combo = win.combos["can1"]
    if "다른 폴더" not in combo.currentText() or combo.itemData(combo.count() - 1) != "__browse__":
        fails.append(f"combo after custom file: {combo.currentText()}")
    if [x.key for x in st.signals] != [("can1", 0xF13382, "Enc0_AngleRad")]:
        fails.append(f"renamed signal should be dropped, others kept: {[x.key for x in st.signals]}")
    tpdo = next(g for g in ses.cap.groups if g.iface == "can1" and g.can_id == 0x040382)
    if "MotorPosition" not in [x.name for x in ses.signals_for(tpdo)] or tpdo.design != 10:
        fails.append("custom DBC content not used")
    custom.write_text(custom.read_text().replace(f'"GenMsgCycleTime" BO_ {0x040382 | 0x80000000} 10;',
                                                 f'"GenMsgCycleTime" BO_ {0x040382 | 0x80000000} 20;'))
    win.reload_dbc()
    app.processEvents()
    if tpdo.design != 20:
        fails.append(f"reload should pick up the edited cycle time, got {tpdo.design}")
    (other / "broken.dbc").write_text("this is not a dbc file BO_ x")
    import PyQt6.QtWidgets as QtW
    shown = []
    real = QtW.QMessageBox.warning
    QtW.QMessageBox.warning = staticmethod(lambda *a, **k: shown.append(a[1]))
    import qatools.tools.canmon.ui.main as um
    um.QMessageBox.warning = QtW.QMessageBox.warning
    ok = win.apply_dbc("can1", str(other / "broken.dbc"))
    um.QMessageBox.warning = real
    if ok or not shown or ses.chmap["can1"] != custom:
        fails.append(f"broken DBC must be refused and the previous one kept: ok={ok} shown={shown}")
    win.set_dbc_dir(str(other))
    names = [combo.itemText(i) for i in range(win.combos["can1"].count())]
    if names != ["(DBC 없음)", "broken", "my_ltact_ch1", "파일에서 선택…"]:
        fails.append(f"folder listing {names}")
    if win.settings.value("map/can1") != str(custom) or win.settings.value("dbc_dir") != str(other):
        fails.append("selection not saved in settings")
    grab("7_custom_dbc")
    # ---- live measurement, fed by the sample played 20 times faster than real time
    import time
    from qatools.tools.canmon.live import ReplaySource
    live_map = {"can0": str(DBC_DIR / "HETC-L-TACT_ch0.dbc"), "can1": str(DBC_DIR / "HETC-L-TACT_ch1.dbc")}
    log = Path(d) / "logs" / "live.csv"
    src = ReplaySource(str(sample), speed=20)
    if not win.start_live(src, live_map, log):
        fails.append("live did not start")
    if win.live.meta.errors:
        fails.append(f"live DBC errors {win.live.meta.errors}")
    lp = win.live_page
    if win.stack.currentWidget() is not lp or lp.isTabEnabled(3) or win.a_open.isEnabled() or win.dock.isEnabled():
        fails.append("live UI state: graph tab must be disabled, file controls locked")
    if "실시간 측정" not in win.mode.text() or win.a_live.text() != "■ 측정 정지":
        fails.append(f"live labels {win.mode.text()} {win.a_live.text()}")
    end = time.time() + 20
    reset_done = False
    while not src.finished and time.time() < end:
        app.processEvents()
        time.sleep(0.02)
        if not reset_done and win.live.stats.t_last > 3:
            n_before = lp.period.table.rowCount()
            if n_before < 30 or lp.trace.tree.topLevelItemCount() != n_before:
                fails.append(f"live rows trace {lp.trace.tree.topLevelItemCount()} period {n_before}")
            item = lp.trace.items[("can1", 0xF13382, (0x1B,))][0]
            item.setExpanded(True)
            app.processEvents()
            names = [item.child(k).text(3) for k in range(item.childCount())]
            if "Enc0_AngleRad" not in names or not item.text(7):
                fails.append(f"live trace decode {names[:4]}")
            if "프레임/s" not in win.status.text() or "하드웨어" not in win.status.text() or str(log) not in win.status.text():
                fails.append(f"live status {win.status.text()}")
            grab("11_live_trace")
            lp.setCurrentIndex(1)
            lp.period.table.selectRow(0)
            grab("12_live_period")
            lp.period.reset()
            reset_done = True
    for _ in range(5):
        app.processEvents()
        time.sleep(0.05)
    lg = lp.period.groups.get(("can0", 0xF13D82, (0x1B,)))
    if lg is None or lg.over != 1 or not (11.5 < lg.gmax < 12.5):
        fails.append(f"live period should flag the HTAC_61 dropout after the reset: {lg}")
    if "한계 초과 1건" not in lp.period.summary.text():
        fails.append(f"live summary {lp.period.summary.text()}")
    out = Path(d) / "live_stats.csv"
    lp.period.write_csv(out)
    if len(out.read_text(encoding="utf-8-sig").splitlines()) != len(lp.period.groups) + 1:
        fails.append("live period csv")
    win.stop_live()
    app.processEvents()
    if win.live is not None or win.stack.currentWidget() is not win.tabs or not win.dock.isEnabled():
        fails.append("UI not restored after stop")
    ses = win.state.session
    st = win.state
    if ses.cap.path != log or ses.cap.n != len(src.frames) or ses.channel_name("can1") != "HETC-L-TACT_ch1":
        fails.append(f"saved log should open after stop: {ses.cap.path} {ses.cap.n}")
    win.check_signal("can1", 0xF13382, "Enc0_AngleRad")
    if len(win.graph.curves) != 1 or "파일 재생" not in win.mode.text():
        fails.append("graph should work on the saved log")
    # preview from the open file: no log, yellow label, stops by itself at the end
    if not win.start_demo() or win.live.log_path is not None or "미리 보기" not in win.mode.text():
        fails.append("demo start")
    win.live.source.speed = 200
    end = time.time() + 15
    while win.live is not None and time.time() < end:
        app.processEvents()
        time.sleep(0.02)
    if win.live is not None or win.state.session.cap.path != log:
        fails.append("demo should stop at the end and leave the open file alone")

    # ---- message transmit, through the preview source (loops frames back, reaches no bus)
    import qatools.tools.canmon.ui.tx as ui_tx
    from PyQt6.QtWidgets import QMessageBox
    from qatools.tools.canmon.tx import KIND_LOAD, KIND_MSG, LOAD_ID, TxEntry
    log2 = Path(d) / "logs" / "tx.csv"
    src2 = ReplaySource(str(sample), speed=1)
    win.start_live(src2, live_map, log2, demo=True)
    tp = lp.tx
    tp.from_json("[]")
    if tp.armed or "꺼짐" not in tp.state.text():
        fails.append("transmit must start switched off")
    uid_q = tp.add(TxEntry(iface="can1", kind=KIND_MSG, message="QUIET_HETC_L_TACT_to_ALL"))
    if tp.start(uid_q):
        fails.append("sending must be refused while transmit mode is off")
    ui_tx.QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    if not tp.arm() or "송신 모드" not in win.mode.text() or "can1" not in tp.state.text():
        fails.append(f"arm: {tp.armed} {win.mode.text()}")
    tp.table.selectRow(0)
    tp.set_signal("HoldMs", 3000)
    tp.e_period.setValue(10)
    if tp.entries[0][1].values.get("HoldMs") != 3000 or "00 00 B8 0B 00 00" not in tp.preview.text():
        fails.append(f"QUIET editor {tp.entries[0][1].values} {tp.preview.text()}")
    tp.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    uid_l = tp.add(TxEntry(iface="can1", kind=KIND_LOAD, can_id=LOAD_ID, length=64, load_pct=20))
    tp.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
    uid_c = tp.add(TxEntry(iface="can1", kind=KIND_MSG, message="CTRL_HETC_L_TACT_to_ALL"))
    tp.set_signal("SubCode", 9)
    sig_names = [tp.sigs.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in range(tp.sigs.rowCount())]
    if "Rtc_Year2000" not in sig_names:
        fails.append(f"multiplexed signal list {sig_names}")
    lp.setCurrentIndex(2)
    end = time.time() + 1.2
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)
    stq = tp.tx.status().get(uid_q, {})
    stl = tp.tx.status().get(uid_l, {})
    if not (90 <= stq.get("sent", 0) <= 130) or not stl.get("sent", 0) > 500 or "보내는 중" not in tp.table.item(0, ui_tx.C["상태"]).text():
        fails.append(f"transmit counts quiet {stq} load {stl.get('sent')}")
    if "can1" not in tp.total.text() or "can1 20.6 %" not in tp.total.text():
        fails.append(f"added load label {tp.total.text()}")
    txrow = lp.trace.items.get(("can1", 0xF282FF, None))
    if txrow is None or txrow[0].text(1) != "can1 Tx":
        fails.append("live trace should show the sent QUIET frame as Tx")
    if "송신" not in win.status.text():
        fails.append(f"status bar Tx count {win.status.text()}")
    grab("13_live_tx")
    tp.esc.activated.emit()
    app.processEvents()
    if tp.tx.any_running() or tp.table.item(0, 0).checkState() != Qt.CheckState.Unchecked:
        fails.append("Esc should stop every entry")
    tp.table.selectRow(2)
    if not tp.send_selected_once():
        fails.append("send once")
    for _ in range(5):
        app.processEvents()
        time.sleep(0.03)
    win.stop_live()
    app.processEvents()
    if tp.armed or "파일 재생" not in win.mode.text():
        fails.append("stopping the measurement must switch transmit off")
    cap2 = win.state.session.cap
    tx_groups = {g.can_id: g.tx for g in cap2.groups if g.tx}
    if set(tx_groups) != {0xF282FF, LOAD_ID, 0xF082FF} or tx_groups[0xF082FF] != 1:
        fails.append(f"Tx frames in the saved log {tx_groups}")
    if win.trace.items and not any(it.text(1) == "can1 Tx" for _g, it in win.trace.items):
        fails.append("offline trace should mark Tx")
    if len(json.loads(win.settings.value("tx/list"))) != 3:
        fails.append("transmit list should be saved in the settings")

    # ---- access dialog: status, user check, command building
    import qatools.tools.canmon.ui.hw as hw
    dlg = hw.AccessDialog(win)
    state = dlg.refresh()
    if state not in ("missing", "not_allowed", "ok") or not dlg.status.text():
        fails.append(f"access status {state}")
    cmd = hw.install_command(["kim", "lee"], pkexec="/usr/bin/pkexec")
    if cmd != ["/usr/bin/pkexec", str(hw.INSTALLER), "kim", "lee"] or not hw.INSTALLER.exists():
        fails.append(f"install command {cmd}")
    if hw.install_command(["kim"], pkexec="") is not None:
        fails.append("no pkexec should give no command")
    for bad in ("", "kim; rm -rf /", "Kim", "a b$"):
        try:
            hw.parse_users(bad)
            fails.append(f"user check accepted {bad!r}")
        except ValueError:
            pass
    if hw.parse_users("kim, lee  park_1") != ["kim", "lee", "park_1"]:
        fails.append("user parsing")
    dlg.users.setText("bad name!")
    dlg.install()
    if "올바르지" not in dlg.log.toPlainText() or dlg.proc is not None:
        fails.append("dialog should refuse a bad user name without starting anything")
    if [a.text() for a in win.menuBar().actions()] != ["파일", "도구"]:
        fails.append("menu bar")
    dlg.show()
    app.processEvents()
    if shots:
        dlg.grab().save(str(shots / "8_access.png"))
    dlg.close()
    win.close()

print("FAILED:\n" + "\n".join(fails) if fails else "ALL CHECKS PASSED")
sys.exit(1 if fails else 0)
