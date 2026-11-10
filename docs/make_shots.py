#!/usr/bin/env python3
"""Make the numbered screenshots of the user manual.

    PYTHONPATH=.deps python3 docs/make_shots.py [out_dir]     (default: docs/img)

Runs the app offscreen on the synthetic sample (qa canmon sample), stages each screen, and draws numbered boxes at the
widgets' real positions, so the numbers stay right when the layout changes. Run it again after UI changes.
The live screens are fed from the sample file, and the transmit screen pretends can1 is a transmit channel;
nothing is sent anywhere.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from PyQt6.QtCore import QPoint, QRect, QSettings, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QFont, QPainter, QPen  # noqa: E402
from PyQt6.QtWidgets import QMessageBox, QToolBar  # noqa: E402

from qatools.tools.canmon import sample as sample_tool  # noqa: E402
from qatools.tools.canmon.core import DBC_DIR  # noqa: E402
from qatools.tools.canmon.ui.main import MainWindow, make_app  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "img"
RED = QColor("#d6452f")
app = make_app([])
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
index = {}


# ---------------------------------------------------------------- geometry helpers
def wrect(w, top):
    return QRect(w.mapTo(top, QPoint(0, 0)), w.size())


def union(*rects):
    r = rects[0]
    for x in rects[1:]:
        r = r.united(x)
    return r


def tab_rect(tabs, i, top):
    bar = tabs.tabBar()
    return bar.tabRect(i).translated(bar.mapTo(top, QPoint(0, 0)))


def col_rect(table, cols, top):
    """Header plus visible body of one or more table columns."""
    h = table.horizontalHeader()
    x0 = min(h.sectionViewportPosition(c) for c in cols)
    x1 = max(h.sectionViewportPosition(c) + h.sectionSize(c) for c in cols)
    vp = table.viewport()
    origin = vp.mapTo(top, QPoint(0, 0))
    hy = h.mapTo(top, QPoint(0, 0)).y()
    return QRect(origin.x() + x0, hy, x1 - x0, origin.y() + vp.height() - hy)


def row_rect(table, row, top):
    vp = table.viewport()
    y = table.rowViewportPosition(row)
    return QRect(vp.mapTo(top, QPoint(0, y)), vp.mapTo(top, QPoint(vp.width() - 1, y + table.rowHeight(row) - 1)))


def tree_item_rect(tree, item, top, with_children=True):
    vp = tree.viewport()
    r = tree.visualItemRect(item)
    if with_children and item.isExpanded() and item.childCount():
        r = r.united(tree.visualItemRect(item.child(item.childCount() - 1)))
    r.setLeft(0)
    r.setWidth(vp.width())
    return r.translated(vp.mapTo(top, QPoint(0, 0)))


def plot_rect(glw, item, top):
    r = glw.mapFromScene(item.sceneBoundingRect()).boundingRect()
    return r.translated(glw.viewport().mapTo(top, QPoint(0, 0)))


# ---------------------------------------------------------------- drawing
def shot(name, top, marks, crop=None, title=""):
    """Grab a window, draw numbered boxes, save PNG. marks = [(n, rect, label, badge position)]."""
    app.processEvents()
    pix = top.grab()
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    for n, rect, label, *_ in marks:
        p.setPen(QPen(RED, 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 4, 4)
    f = QFont(app.font())
    f.setBold(True)
    f.setPixelSize(13)
    p.setFont(f)
    rad = 11
    for n, rect, label, *pos in marks:
        where = pos[0] if pos else "tl"
        x = {"tl": rect.left() - 5, "tr": rect.right() + 5, "l": rect.left() - rad - 2, "r": rect.right() + rad + 2,
             "bl": rect.left(), "c": rect.center().x()}[where]
        y = {"tl": rect.top() - 5, "tr": rect.top() - 5, "l": rect.center().y(), "r": rect.center().y(),
             "bl": rect.bottom(), "c": rect.center().y()}[where]
        x = min(max(x, rad + 1), pix.width() - rad - 2)
        y = min(max(y, rad + 1), pix.height() - rad - 2)
        p.setPen(QPen(QColor("white"), 2))
        p.setBrush(RED)
        p.drawEllipse(QPoint(x, y), rad, rad)
        p.setPen(QColor("white"))
        p.drawText(QRect(x - rad, y - rad, 2 * rad, 2 * rad), Qt.AlignmentFlag.AlignCenter, str(n))
    p.end()
    if crop is not None:
        pix = pix.copy(crop)
    OUT.mkdir(parents=True, exist_ok=True)
    pix.save(str(OUT / f"{name}.png"))
    index[name] = {"title": title, "size": [pix.width(), pix.height()], "marks": [[m[0], m[2]] for m in marks]}
    print(f"{name}: {len(marks)} marks")


def pump(seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


# ---------------------------------------------------------------- fake system for the hardware dialog
def can_if(name, up=True, bitrate=1000000, sp=0.875, dbitrate=5000000, dsp=0.625, listen=True):
    data = {"ctrlmode": (["LISTEN-ONLY"] if listen else []) + ["BERR-REPORTING", "FD"], "state": "ERROR-ACTIVE",
            "berr_counter": {"tx": 0, "rx": 0}, "clock": 80000000,
            "bittiming": {"bitrate": bitrate, "sample_point": sp}, "data_bittiming": {"bitrate": dbitrate, "sample_point": dsp}}
    return {"ifname": name, "flags": ["NOARP", "UP", "LOWER_UP"] if up else ["NOARP"], "link_type": "can",
            "linkinfo": {"info_kind": "can", "info_data": data}}


class FakeSystem:
    def __init__(self, links):
        self.links = links

    def __call__(self, cmd, timeout=8):
        if cmd[0] == "ip":
            return 0, json.dumps(self.links)
        _, _, _, action, iface, *rest = cmd
        if action == "up":
            bitrate, sp, dbitrate, dsp, listen = rest
            i = next(k for k, l in enumerate(self.links) if l["ifname"] == iface)
            self.links[i] = can_if(iface, True, int(bitrate), float(sp) or 0.8, int(dbitrate), float(dsp) or 0.75, listen == "on")
        return 0, ""


with tempfile.TemporaryDirectory() as d:
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, d)
    sample = Path(d) / "ltact_bench.csv"
    sample_tool.main([str(sample), "--seconds", "12"])
    MAP = {"can0": "HETC-L-TACT_ch0", "can1": "HETC-L-TACT_ch1"}
    win = MainWindow()
    win.resize(1600, 950)
    win.show()
    win.open(str(sample), MAP)
    st = win.state
    toolbar = win.findChild(QToolBar)
    act = toolbar.widgetForAction

    # ---- 1. main window overview (Trace, fixed view)
    st.set_time(8.205)
    pump(0.2)
    shot("01_main", win, [
        (1, QRect(wrect(win.menuBar(), win).topLeft(), QPoint(90, wrect(win.menuBar(), win).bottom())), "메뉴 (파일, 도구)", "r"),
        (2, wrect(act(win.a_open), win), "캡처 파일 열기"),
        (3, wrect(act(win.a_hw), win), "하드웨어 설정"),
        (4, wrect(act(win.a_live), win), "측정 시작 / 정지"),
        (5, union(wrect(act(win.a_play), win), wrect(win.speed, win)), "재생 / 일시정지와 배속"),
        (6, union(wrect(win.slider, win), wrect(win.clock, win)), "시각 슬라이더와 현재 시각"),
        (7, wrect(win.mode, win), "모드 표시", "tr"),
        (8, wrect(win.chan_box, win), "채널과 DBC"),
        (9, union(wrect(win.search, win), wrect(win.browser, win)), "DBC 탐색"),
        (10, union(*(tab_rect(win.tabs, i, win) for i in range(3))), "화면 탭"),
        (11, wrect(win.statusBar(), win), "상태 줄", "tl"),
    ], title="메인 창")

    # ---- 2. Trace fixed view with an expanded message
    tr = win.trace
    win.tabs.setCurrentWidget(tr)
    row = {(g.iface, g.can_id, g.part): it for g, it in tr.items}
    it51 = row[("can1", 0xF13382, "ANGLE_MULTI")]
    tr.tree.scrollToItem(it51)
    it51.setExpanded(True)
    pump(0.2)
    top_tr = wrect(tr, win)
    hdr = tr.tree.header()
    hx = lambda c: hdr.sectionViewportPosition(c)
    vp0 = tr.tree.viewport().mapTo(win, QPoint(0, 0))
    hy = hdr.mapTo(win, QPoint(0, 0)).y()
    shot("02_trace_fixed", win, [
        (1, wrect(tr.mode, win), "표시 방식"),
        (2, wrect(tr.filter, win), "거르기"),
        (3, wrect(tr.info, win), "보이는 메시지 수", "tr"),
        (4, QRect(vp0.x() + hx(0), hy, hdr.sectionSize(0), hdr.height()), "시각 (재생 시각의 최신 프레임)"),
        (5, QRect(vp0.x() + hx(6), hy, hdr.sectionSize(6), hdr.height()), "주기 (직전 프레임과의 간격)", "tr"),
        (6, tree_item_rect(tr.tree, it51, win), "펼친 메시지의 신호 값"),
    ], crop=top_tr, title="Trace · ID별 고정")

    # ---- 3. Trace chronological view
    tr.mode.setCurrentIndex(1)
    tr.filter.setText("0xf13d82")
    pump(0.2)
    tr.table.selectRow(3)
    pump(0.2)
    shot("03_trace_chrono", win, [
        (1, wrect(tr.mode, win), "시간순으로 바꾸기"),
        (2, wrect(tr.filter, win), "CAN ID로 거른 상태"),
        (3, wrect(tr.table, win), "전체 프레임 (시간순)"),
        (4, wrect(tr.detail, win), "선택한 프레임의 신호 값"),
    ], crop=top_tr, title="Trace · 시간순")
    tr.filter.setText("")
    tr.mode.setCurrentIndex(0)

    # ---- 4. Period statistics
    pt = win.period
    win.tabs.setCurrentWidget(pt)
    pt.table.sortItems(10, Qt.SortOrder.DescendingOrder)
    pt.table.selectRow(0)
    pump(0.3)
    top_pt = wrect(pt, win)
    shot("04_period", win, [
        (1, wrect(pt.range, win), "통계 범위"),
        (2, wrect(pt.limit, win), "한계 (설계 주기의 몇 배)"),
        (3, wrect(pt.summary, win), "한계 초과 건수"),
        (4, wrect(pt.export, win), "CSV 내보내기", "tr"),
        (5, row_rect(pt.table, 0, win), "한계를 넘은 메시지 (빨간 줄)"),
        (6, col_rect(pt.table, range(4, 10), win), "수신 수 · 평균 · 최소 · 최대 · p99 · 설계", "tr"),
        (7, wrect(pt.title, win).united(wrect(pt.hist, win)), "선택한 메시지의 간격 분포"),
        (8, union(wrect(pt.v_max, win), wrect(pt.v_over, win)), "최대 간격, 발생 시각, 한계 초과 횟수", "tr"),
        (9, union(wrect(pt.to_graph, win), wrect(pt.jump, win)), "그래프로 보기"),
    ], crop=top_pt, title="주기 통계")

    # ---- 5. Graph
    st.set_cursors(1.0, 4.0)
    st.save_range("정지 구간")
    pt.jump.click()
    pump(0.2)
    gt = win.graph
    for key in (("can1", 0xF13382, "Enc0_AngleRad"), ("can0", 0xF13D82, "Enc0_AngleRad"), ("can1", 0x040382, "PresentPosition"),
                ("can1", 0x040382, "PresentState")):
        win.check_signal(*key)
    pump(0.3)
    lo, hi = gt.p_val.viewRange()[0]
    st.set_cursors(lo + (hi - lo) * 0.25, lo + (hi - lo) * 0.7)
    pump(0.3)
    top_g = wrect(gt, win)
    shot("05_graph", win, [
        (1, union(wrect(gt.points, win), wrect(gt.shape, win)), "샘플 점, 선 모양"),
        (2, wrect(gt.fit, win), "전체 보기", "tl"),
        (3, union(wrect(gt.export, win), wrect(gt.image, win)), "CSV · 이미지 저장", "tr"),
        (4, plot_rect(gt.glw, gt.p_val, win), "신호 값과 커서 A · B"),
        (5, plot_rect(gt.glw, gt.p_state, win), "상태 신호"),
        (6, plot_rect(gt.glw, gt.p_gap, win), "수신 간격과 한계선"),
        (7, wrect(gt.hover, win), "점 정보 (마우스를 올린 점)"),
        (8, wrect(gt.table, win), "신호별 A · B · 차이 · 최소/평균/최대"),
        (9, union(wrect(gt.stat_range, win), wrect(gt.remove, win)), "통계 범위, 신호 빼기"),
        (10, union(wrect(gt.range_name, win), wrect(gt.ranges, win), wrect(gt.del_range, win)), "구간 저장 · 불러오기"),
        (11, wrect(gt.readout, win), "커서 A · B 시각과 간격 Δt"),
    ], crop=top_g, title="그래프")

    # ---- 6. Hardware settings
    from qatools.tools.canmon.ui.hwconfig import HardwareDialog
    fake = FakeSystem([can_if("can0"), can_if("can1", listen=True)])
    dlg = HardwareDialog(win, win.settings, DBC_DIR, runner=fake)
    dlg.resize(1400, 560)
    for iface, ch in MAP.items():
        w = dlg.rows[iface]
        w["dbc"].setCurrentIndex(max(w["dbc"].findData(str(DBC_DIR / f"{ch}.dbc")), 0))
    dlg.rows["can1"]["mode"].setCurrentIndex(1)
    dlg.show()
    pump(0.1)
    dlg.apply()
    pump(0.2)
    shot("06_hardware", dlg, [
        (1, wrect(dlg.b_refresh, dlg), "장치 다시 찾기"),
        (2, union(wrect(dlg.profile, dlg), wrect(dlg.b_save, dlg), wrect(dlg.b_delete, dlg)), "프로필"),
        (3, wrect(dlg.b_access, dlg), "권한 설치", "tr"),
        (4, col_rect(dlg.table, [0, 1, 2], dlg), "사용할 채널과 장치"),
        (5, col_rect(dlg.table, [3, 4, 5, 6, 7], dlg), "프리셋, 속도, 샘플 포인트"),
        (6, col_rect(dlg.table, [8], dlg), "모드 (수신 전용 / 송신 가능)"),
        (7, col_rect(dlg.table, [9], dlg), "DBC"),
        (8, col_rect(dlg.table, [10], dlg), "현재 상태 (장치에서 읽은 값)", "tr"),
        (9, wrect(dlg.log, dlg), "적용 결과"),
        (10, wrect(dlg.b_apply, dlg), "적용", "tr"),
    ], title="하드웨어 설정")
    dlg.close()

    # ---- 7. Access dialog
    from qatools.tools.canmon.ui.hw import AccessDialog
    acc = AccessDialog(win)
    acc.show()
    acc.refresh()
    pump(0.1)
    shot("07_access", acc, [
        (1, wrect(acc.status, acc), "현재 상태"),
        (2, wrect(acc.users, acc), "등록할 사용자"),
        (3, wrect(acc.b_install, acc), "설치 (관리자 암호 한 번)"),
        (4, wrect(acc.b_copy, acc), "터미널 명령 복사", "tr"),
        (5, wrect(acc.log, acc), "진행 기록"),
    ], title="CAN 설정 권한 설치")
    acc.close()

    # ---- 8. live measurement, fed from the sample at recorded speed
    from qatools.tools.canmon.live import ReplaySource
    live_map = {k: str(DBC_DIR / f"{v}.dbc") for k, v in MAP.items()}
    log_dir = Path(d) / "canmon_logs"
    src = ReplaySource(str(sample), speed=1)
    win.start_live(src, live_map, log_dir / "can_20261109_143012.csv")
    lp = win.live_page
    pump(4.0)
    shot("08_live_trace", win, [
        (1, wrect(act(win.a_live), win), "측정 정지"),
        (2, wrect(win.clock, win), "측정 시간과 프레임 수"),
        (3, wrect(win.mode, win), "실시간 측정 표시", "tr"),
        (4, union(*(tab_rect(lp, i, win) for i in range(4))), "측정 중 화면 (그래프는 측정 후)"),
        (5, wrect(lp.trace.tree, win), "메시지별 최신 프레임 (초당 10회 갱신)"),
        (6, wrect(win.statusBar(), win), "채널별 부하, 프레임/s, 오류, 수신 누락, 타임스탬프, 로그 위치"),
    ], title="실시간 측정 · Trace")
    lp.setCurrentIndex(1)
    lp.period.table.sortItems(3, Qt.SortOrder.DescendingOrder)
    lp.period.table.selectRow(0)
    pump(0.5)
    lv = lp.period
    shot("09_live_period", win, [
        (1, wrect(lv.since, win).united(wrect(lv.since, win).adjusted(-210, 0, 0, 0)), "통계 범위와 경과 시간"),
        (2, wrect(lv.limit, win), "한계"),
        (3, wrect(lv.summary, win), "한계 초과 건수"),
        (4, wrect(lv.b_reset, win), "통계 초기화"),
        (5, wrect(lv.b_export, win), "CSV 내보내기", "tr"),
        (6, wrect(lv.table, win), "메시지별 통계 (p99는 근삿값)"),
        (7, union(wrect(lv.title, win), wrect(lv.hist, win)), "간격 분포"),
    ], crop=wrect(lp, win), title="실시간 측정 · 주기 통계")

    # ---- 10. message transmit (can1 treated as a transmit channel; frames loop back, no bus)
    import qatools.tools.canmon.ui.tx as ui_tx
    from qatools.tools.canmon.tx import KIND_LOAD, KIND_MSG, KIND_RAW, LOAD_ID, TxEntry
    tp = lp.tx
    tp.from_json("[]")
    tp._transmit_ifaces = lambda: ({"can1"}, "")
    ui_tx.QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    tp.arm()
    tp.add(TxEntry(iface="can1", kind=KIND_MSG, message="QUIET_HETC_L_TACT_to_ALL", period_ms=0))
    tp.table.selectRow(0)
    tp.set_signal("HoldMs", 3000)
    tp.add(TxEntry(iface="can1", kind=KIND_MSG, message="EVENT_HETC_L_TACT_to_ALL"))
    tp.set_signal("EvtCode", "ESTOP")
    tp.add(TxEntry(iface="can1", kind=KIND_RAW, can_id=0x1FFFFE00, data=bytes.fromhex("0102030405060708"), period_ms=20,
                   count=500, note="벤치 확인용"))
    tp.add(TxEntry(iface="can1", kind=KIND_LOAD, can_id=LOAD_ID, length=64, load_pct=20))
    tp.add(TxEntry(iface="can1", kind=KIND_MSG, message="CTRL_HETC_L_TACT_to_ALL"))
    tp.set_signal("SubCode", 9)
    tp.table.item(2, 0).setCheckState(Qt.CheckState.Checked)
    tp.table.item(3, 0).setCheckState(Qt.CheckState.Checked)
    tp.table.selectRow(4)
    lp.setCurrentIndex(2)
    pump(1.5)
    tp.table.selectRow(4)
    pump(0.2)
    shot("10_tx", win, [
        (1, wrect(tp.b_arm, win), "송신 모드 켜기 / 끄기"),
        (2, wrect(tp.state, win), "송신 모드 상태"),
        (3, wrect(tp.auto, win), "자동 정지 시간"),
        (4, wrect(tp.b_stop, win), "전체 정지 (Esc)"),
        (5, union(wrect(tp.b_save, win), wrect(tp.b_load, win)), "목록 저장 · 불러오기", "tr"),
        (6, union(wrect(tp.b_add_msg, win), wrect(tp.b_add_load, win)), "항목 추가 (DBC, 직접 입력, 부하)"),
        (7, union(wrect(tp.b_dup, win), wrect(tp.b_del, win)), "복제 · 삭제"),
        (8, wrect(tp.b_once, win), "선택 1회 송신", "tr"),
        (9, col_rect(tp.table, [0], win), "체크 = 송신 시작 / 해제 = 정지"),
        (10, col_rect(tp.table, [ui_tx.C["보낸 수"], ui_tx.C["상태"]], win), "보낸 수와 상태"),
        (11, wrect(tp.total, win), "추가 부하 계산값"),
        (12, QRect(wrect(tp.ed_box, win).topLeft(), QPoint(wrect(tp.ed_box, win).right(), wrect(tp.sig_label, win).top() - 2)),
         "선택한 항목 편집"),
        (13, wrect(tp.sigs, win), "신호 값 (다중화에 따라 바뀜)"),
        (14, wrect(tp.preview, win), "보낼 프레임 미리 보기"),
    ], title="메시지 송신")
    # editor of a load entry, cropped to the right panel
    tp.table.selectRow(3)
    pump(0.2)
    ed = wrect(tp.ed_box, win)
    shot("11_tx_load", win, [
        (1, union(wrect(tp.e_id, win), wrect(tp.e_ext.parentWidget(), win)), "더미 CAN ID와 형식"),
        (2, wrect(tp.e_len, win), "길이 (기본 64바이트)"),
        (3, wrect(tp.e_load, win), "목표 부하"),
        (4, wrect(tp.preview, win), "간격, 프레임/s, 추가 부하 계산"),
    ], crop=QRect(ed.left(), ed.top(), ed.width(), wrect(tp.preview, win).bottom() - ed.top() + 12), title="부하 메시지 편집")
    tp.table.selectRow(2)
    pump(0.2)
    shot("12_tx_raw", win, [
        (1, wrect(tp.e_id, win), "CAN ID (16진수)"),
        (2, wrect(tp.e_ext.parentWidget(), win), "29비트 · FD · BRS"),
        (3, wrect(tp.e_data, win), "데이터 (16진수 바이트)"),
        (4, union(wrect(tp.e_period, win), wrect(tp.e_count, win)), "주기와 횟수"),
    ], crop=QRect(ed.left(), ed.top(), ed.width(), wrect(tp.preview, win).bottom() - ed.top() + 12), title="직접 입력 편집")
    tp.stop_all()
    win.stop_live(open_log=False)
    win.close()

(OUT / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"saved to {OUT}")
