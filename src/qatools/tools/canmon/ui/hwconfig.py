"""Hardware configuration dialog: pick interfaces, bit rates, sample points and the DBC of each channel."""
from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QHBoxLayout, QHeaderView,
                             QInputDialog, QLabel, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
                             QVBoxLayout)

from .. import hwconfig
from ..core import dbc_files
from ..hwconfig import CUSTOM, PRESETS, ChannelConfig
from .hw import AccessDialog

COLS = ["사용", "채널", "장치", "프리셋", "중재 속도 (bps)", "샘플 포인트", "데이터 속도 (bps)", "데이터 샘플 포인트", "모드", "DBC",
        "현재 상태"]
NO_DBC, BROWSE = "(DBC 없음)", "__browse__"
MODE_LISTEN, MODE_TX = "수신 전용", "송신 가능"


class HardwareDialog(QDialog):
    def __init__(self, parent, settings, dbc_dir, runner=hwconfig.run, profiles_file=None):
        super().__init__(parent)
        self.shared = hwconfig.load_shared_profiles(profiles_file)    # read-only profiles from the repository
        self.setWindowTitle("하드웨어 설정")
        self.resize(1320, 520)
        self.settings, self.dbc_dir, self.runner = settings, Path(dbc_dir), runner
        self.rows = {}                     # iface -> dict of widgets
        self.infos = []

        self.b_refresh = QPushButton("장치 다시 찾기")
        self.profile = QComboBox()
        self.profile.setMinimumWidth(200)
        self.b_save = QPushButton("프로필 저장")
        self.b_delete = QPushButton("프로필 삭제")
        self.b_access = QPushButton("권한 설치…")
        bar = QHBoxLayout()
        bar.addWidget(self.b_refresh)
        bar.addSpacing(16)
        bar.addWidget(QLabel("프로필"))
        bar.addWidget(self.profile)
        bar.addWidget(self.b_save)
        bar.addWidget(self.b_delete)
        bar.addStretch(1)
        bar.addWidget(self.b_access)

        self.empty = QLabel("")
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet("background:#fcf1d8; color:#6b4a0c; padding:8px 10px; border-radius:6px;")
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        note = QLabel("기본은 <b>수신 전용</b>입니다. 분석기가 버스에 아무것도 보내지 않고 ACK도 하지 않습니다. "
                      "메시지를 보내려면 그 채널만 <b>송신 가능</b>으로 바꿉니다. 송신 가능 채널은 보내지 않을 때도 버스에 ACK를 하고, "
                      "속도가 틀리면 오류 프레임을 낼 수 있습니다. "
                      "샘플 포인트 \"자동\"은 드라이버가 정합니다. 종단저항은 이 화면에서 바꿀 수 없으니 장비 설정을 확인하세요.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#5b6978;")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(130)
        self.log.setPlaceholderText("적용 결과가 여기에 표시됩니다.")
        self.b_apply = QPushButton("적용")
        self.b_apply.setDefault(True)
        self.b_close = QPushButton("닫기")
        foot = QHBoxLayout()
        foot.addStretch(1)
        foot.addWidget(self.b_apply)
        foot.addWidget(self.b_close)

        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(self.empty)
        lay.addWidget(self.table, 1)
        lay.addWidget(note)
        lay.addWidget(self.log)
        lay.addLayout(foot)

        self.b_refresh.clicked.connect(self.refresh)
        self.b_apply.clicked.connect(self.apply)
        self.b_close.clicked.connect(self.accept)
        self.b_access.clicked.connect(lambda: AccessDialog(self).exec())
        self.b_save.clicked.connect(self._ask_save)
        self.b_delete.clicked.connect(self.delete_profile)
        self.profile.activated.connect(lambda _i: self.load_profile(self.profile.currentText()))
        self._fill_profiles()
        self.refresh()

    # ------------------------------------------------------------ stored values
    def _json(self, key):
        try:
            return json.loads(self.settings.value(key, "{}") or "{}")
        except ValueError:
            return {}

    def _stored(self, iface):
        d = self._json("hw/config").get(iface)
        cfg = ChannelConfig.from_dict(d) if d else ChannelConfig(iface)
        cfg.iface = iface
        cfg.listen_only = True          # transmit mode is chosen again each time, never restored silently
        cfg.dbc = cfg.dbc or self.settings.value(f"map/{iface}", "") or ""
        return cfg

    # ------------------------------------------------------------ table
    def refresh(self):
        self.infos, err = hwconfig.read_interfaces(self.runner)
        keep = {name: self._row_config(name) for name in self.rows}
        self.table.setRowCount(0)
        self.rows = {}
        for info in self.infos:
            self._add_row(info, keep.get(info.name) or self._stored(info.name))
        if err:
            self.empty.setText(f"CAN 장치 목록을 읽지 못했습니다: {err}")
        elif not self.infos:
            self.empty.setText("CAN 장치를 찾지 못했습니다. 분석기(PCAN-USB Pro FD)를 꽂은 뒤 \"장치 다시 찾기\"를 누르세요. "
                               "Windows나 WSL에서는 CAN 장치를 쓸 수 없고, 저장한 로그 보기만 됩니다.")
        self.empty.setVisible(bool(err) or not self.infos)
        self.b_apply.setEnabled(any(i.configurable for i in self.infos))

    def _add_row(self, info, cfg):
        r = self.table.rowCount()
        self.table.insertRow(r)
        w = {"info": info}
        w["use"] = QCheckBox()
        w["use"].setChecked(cfg.enabled)
        w["preset"] = QComboBox()
        w["preset"].addItems(list(PRESETS) + [CUSTOM])
        w["bitrate"] = QSpinBox()
        w["bitrate"].setRange(10_000, 1_000_000)
        w["bitrate"].setSingleStep(25_000)
        w["dbitrate"] = QSpinBox()
        w["dbitrate"].setRange(10_000, 12_000_000)
        w["dbitrate"].setSingleStep(500_000)
        for key in ("sp", "dsp"):
            box = QDoubleSpinBox()
            box.setRange(0.0, 0.95)
            box.setDecimals(3)
            box.setSingleStep(0.005)
            box.setSpecialValueText("자동")
            w[key] = box
        w["bitrate"].setValue(cfg.bitrate)
        w["dbitrate"].setValue(cfg.dbitrate)
        w["sp"].setValue(cfg.sample_point)
        w["dsp"].setValue(cfg.dsample_point)
        w["dbc"] = QComboBox()
        self._fill_dbc(w["dbc"], cfg.dbc)
        w["mode"] = QComboBox()
        w["mode"].addItems([MODE_LISTEN, MODE_TX])
        w["mode"].setCurrentText(MODE_LISTEN if cfg.listen_only else MODE_TX)
        w["mode"].setToolTip("송신 가능은 메시지 송신을 할 채널에만 씁니다.")
        self.table.setCellWidget(r, 0, w["use"])
        for c, text in ((1, info.name), (2, info.device or "-"), (10, info.summary())):
            item = QTableWidgetItem(text)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(r, c, item)
        for c, key in ((3, "preset"), (4, "bitrate"), (5, "sp"), (6, "dbitrate"), (7, "dsp"), (8, "mode"), (9, "dbc")):
            self.table.setCellWidget(r, c, w[key])
        self.rows[info.name] = w
        w["preset"].setCurrentText(self._row_config(info.name).preset())
        w["preset"].activated.connect(lambda _i, n=info.name: self._preset_picked(n))
        for key in ("bitrate", "sp", "dbitrate", "dsp"):
            w[key].valueChanged.connect(lambda _v, n=info.name: self._value_edited(n))
        w["dbc"].activated.connect(lambda _i, n=info.name: self._dbc_picked(n))
        w["mode"].currentTextChanged.connect(lambda _t, n=info.name: self._mode_picked(n))
        self._mode_picked(info.name)
        if not info.configurable:
            for key in ("preset", "bitrate", "sp", "dbitrate", "dsp", "mode"):
                w[key].setEnabled(False)

    def _fill_dbc(self, combo, current):
        combo.clear()
        combo.addItem(NO_DBC, "")
        for path in dbc_files(self.dbc_dir):
            combo.addItem(path.stem, str(path))
        if current and combo.findData(current) < 0:
            combo.addItem(f"{Path(current).stem}  (다른 폴더)", current)
        combo.addItem("파일에서 선택…", BROWSE)
        combo.setCurrentIndex(max(combo.findData(current), 0))

    def _dbc_picked(self, iface):
        combo = self.rows[iface]["dbc"]
        if combo.currentData() == BROWSE:
            path, _ = QFileDialog.getOpenFileName(self, f"{iface}에 쓸 DBC 파일", str(self.dbc_dir), "DBC (*.dbc)")
            self._fill_dbc(combo, path or "")

    def _preset_picked(self, iface):
        w = self.rows[iface]
        p = PRESETS.get(w["preset"].currentText())
        if p is None:
            return
        for key, field in (("bitrate", "bitrate"), ("sp", "sample_point"), ("dbitrate", "dbitrate"), ("dsp", "dsample_point")):
            w[key].blockSignals(True)
            w[key].setValue(p[field])
            w[key].blockSignals(False)

    def _mode_picked(self, iface):
        box = self.rows[iface]["mode"]
        box.setStyleSheet("" if box.currentText() == MODE_LISTEN else "color:#bd3626; font-weight:600;")

    def _value_edited(self, iface):
        self.rows[iface]["preset"].setCurrentText(self._row_config(iface).preset())

    def _row_config(self, iface):
        w = self.rows[iface]
        return ChannelConfig(iface, w["use"].isChecked(), w["bitrate"].value(), round(w["sp"].value(), 3),
                             w["dbitrate"].value(), round(w["dsp"].value(), 3), w["mode"].currentText() != MODE_TX,
                             w["dbc"].currentData() or "")

    def configs(self):
        return [self._row_config(n) for n in self.rows]

    def set_config(self, cfg):
        """Put one channel's values into its row (used by profiles and tests)."""
        w = self.rows.get(cfg.iface)
        if w is None:
            return False
        w["use"].setChecked(cfg.enabled)
        w["mode"].setCurrentText(MODE_LISTEN if cfg.listen_only else MODE_TX)
        for key, v in (("bitrate", cfg.bitrate), ("sp", cfg.sample_point), ("dbitrate", cfg.dbitrate), ("dsp", cfg.dsample_point)):
            w[key].setValue(v)
        self._fill_dbc(w["dbc"], cfg.dbc)
        return True

    # ------------------------------------------------------------ apply
    def apply(self):
        configs = [c for c in self.configs() if self.rows[c.iface]["info"].configurable]
        stored = self._json("hw/config")
        for c in self.configs():
            stored[c.iface] = c.to_dict()
            self.settings.setValue(f"map/{c.iface}", c.dbc)
        self.settings.setValue("hw/config", json.dumps(stored))
        results = hwconfig.apply(configs, self.runner)
        lines = [f"{'✔' if ok else '✖'} {iface}: {msg}" for iface, ok, msg in results]
        if not results:
            lines.append("사용으로 표시한 채널이 없습니다.")
        elif all(ok for _, ok, _ in results):
            lines.append("설정을 저장했습니다. 실시간 측정을 시작하면 이 설정을 사용합니다.")
        self.log.setPlainText("\n".join(lines))
        keep = {n: self._row_config(n) for n in self.rows}
        self.infos, _ = hwconfig.read_interfaces(self.runner)
        for info in self.infos:
            if info.name in self.rows:
                self.rows[info.name]["info"] = info
                row = list(self.rows).index(info.name)
                self.table.item(row, 10).setText(info.summary())
        return results

    # ------------------------------------------------------------ profiles
    def _profiles(self):
        """Shared profiles from the repository, overridden by the user's own profiles of the same name."""
        return {**self.shared, **self._json("hw/profiles")}

    def _fill_profiles(self, select=""):
        self.profile.clear()
        self.profile.addItem("")
        self.profile.addItems(sorted(self._profiles()))
        self.profile.setCurrentText(select)

    def _ask_save(self):
        name, ok = QInputDialog.getText(self, "프로필 저장", "프로필 이름 (예: L-TACT 측정)", text=self.profile.currentText())
        if ok and name.strip():
            self.save_profile(name.strip())

    def save_profile(self, name):
        profiles = self._json("hw/profiles")
        profiles[name] = {c.iface: c.to_dict() for c in self.configs()}
        self.settings.setValue("hw/profiles", json.dumps(profiles))
        self._fill_profiles(name)

    def load_profile(self, name):
        data = self._profiles().get(name)
        if not data:
            return
        missing = []
        for iface, d in data.items():
            cfg = ChannelConfig.from_dict({**d, "iface": iface})
            if cfg.dbc and Path(cfg.dbc).suffix.lower() != ".dbc":     # shared profiles name a DBC channel, not a path
                cfg.dbc = str(self.dbc_dir / f"{cfg.dbc}.dbc")
            if not self.set_config(cfg):
                missing.append(iface)
        text = f"프로필 \"{name}\"을 불러왔습니다. \"적용\"을 눌러야 장치에 반영됩니다."
        if missing:
            text += f"\n지금 연결되지 않은 채널은 건너뛰었습니다: {', '.join(missing)}"
        self.log.setPlainText(text)

    def delete_profile(self):
        name = self.profile.currentText()
        profiles = self._json("hw/profiles")
        if profiles.pop(name, None) is not None:
            self.settings.setValue("hw/profiles", json.dumps(profiles))
            self._fill_profiles(name if name in self.shared else "")
        elif name in self.shared:
            self.log.setPlainText(f"\"{name}\"은 저장소에 들어 있는 공용 프로필이라 여기서 지울 수 없습니다 "
                                  f"({hwconfig.PROFILES_FILE.name}).")
