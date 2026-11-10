#!/usr/bin/env python3
"""Checks for hardware configuration: interface parsing, command building, read-back check, and the dialog."""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PyQt6.QtCore import QSettings  # noqa: E402

from qatools.tools.canmon import hwconfig  # noqa: E402
from qatools.tools.canmon.hwconfig import ChannelConfig  # noqa: E402
from qatools.tools.canmon.ui.hwconfig import HardwareDialog  # noqa: E402
from qatools.tools.canmon.ui.main import make_app  # noqa: E402

shots = Path(sys.argv[1]) if len(sys.argv) > 1 else None
fails = []


def can_if(name, up=True, bitrate=1000000, sp=0.875, dbitrate=5000000, dsp=0.625, modes=("LISTEN-ONLY", "BERR-REPORTING", "FD"),
           state="ERROR-ACTIVE"):
    data = {"ctrlmode": list(modes), "state": state, "berr_counter": {"tx": 0, "rx": 3}, "restart_ms": 0, "clock": 80000000}
    if bitrate:
        data["bittiming"] = {"bitrate": bitrate, "sample_point": sp, "tq": 12}
        data["data_bittiming"] = {"bitrate": dbitrate, "sample_point": dsp, "tq": 12}
    return {"ifname": name, "flags": ["NOARP", "UP", "LOWER_UP"] if up else ["NOARP"], "link_type": "can",
            "linkinfo": {"info_kind": "can", "info_data": data}}


class FakeSystem:
    """Stands in for `ip` and the setup helper: remembers commands and changes interface state like the driver would."""

    def __init__(self, links, helper_ok=True, round_sp=None):
        self.links, self.helper_ok, self.round_sp, self.calls = links, helper_ok, round_sp, []

    def __call__(self, cmd, timeout=8):
        self.calls.append(cmd)
        if cmd[0] == "ip":
            return 0, json.dumps(self.links)
        if not self.helper_ok:
            return 1, "sudo: a password is required"
        _, _, _, action, iface, *rest = cmd
        link = next(l for l in self.links if l["ifname"] == iface)
        if action == "up":
            bitrate, sp, dbitrate, dsp, listen = rest
            link.update(can_if(iface, True, int(bitrate), self.round_sp or float(sp) or 0.8, int(dbitrate), float(dsp) or 0.75,
                               (["LISTEN-ONLY"] if listen == "on" else []) + ["FD"]))
        return 0, ""


# ---- parsing
links = [can_if("can1", up=False, bitrate=None), can_if("can0"),
         {"ifname": "vcan0", "flags": ["UP"], "link_type": "can", "linkinfo": {"info_kind": "vcan"}},
         {"ifname": "eth0", "flags": ["UP"], "link_type": "ether"}]
infos = hwconfig.parse_interfaces(json.dumps(links), sys_root="/nonexistent")
if [i.name for i in infos] != ["can0", "can1", "vcan0"]:
    fails.append(f"interfaces {[i.name for i in infos]}")
c0, c1, v0 = infos
if not (c0.up and c0.bitrate == 1000000 and c0.sample_point == 0.875 and c0.dbitrate == 5000000 and c0.listen_only and c0.fd
        and c0.rx_err == 3 and c0.clock == 80000000 and "수신 전용" in c0.summary()):
    fails.append(f"can0 parse {c0}")
if c1.up or c1.bitrate is not None or c1.summary() != "설정 전" or v0.configurable or "가상" not in v0.summary():
    fails.append(f"can1/vcan parse {c1} {v0}")

# ---- commands and presets
cfg = ChannelConfig("can0")
if hwconfig.up_command(cfg) != ["sudo", "-n", hwconfig.HELPER, "up", "can0", "1000000", "0.875", "5000000", "0.625", "on"]:
    fails.append(f"up command {hwconfig.up_command(cfg)}")
dcan = ChannelConfig("can1", **hwconfig.PRESETS["D-CAN (125 k / 1 M)"])
if hwconfig.up_command(dcan)[5:] != ["125000", "0", "1000000", "0", "on"] or dcan.preset() != "D-CAN (125 k / 1 M)":
    fails.append(f"dcan command {hwconfig.up_command(dcan)}")
if cfg.preset() != "로컬 CAN (1 M / 5 M)" or ChannelConfig("can0", bitrate=500000).preset() != hwconfig.CUSTOM:
    fails.append("preset detection")
if ChannelConfig.from_dict({**cfg.to_dict(), "unknown": 1}) != cfg:
    fails.append("config round trip")

# ---- apply and read-back check
fake = FakeSystem([can_if("can0", up=False, bitrate=None), can_if("can1", up=False, bitrate=None)])
res = hwconfig.apply([cfg, ChannelConfig("can1", enabled=False)], fake)
if len(res) != 1 or not res[0][1] or "1,000,000 bps (87.5 %)" not in res[0][2] or [c[3:5] for c in fake.calls if c[0] == "sudo"] != [["up", "can0"]]:
    fails.append(f"apply ok {res} {fake.calls}")
res = hwconfig.apply([dcan], fake)
if not res[0][1]:
    fails.append(f"auto sample point should be accepted {res}")
fake = FakeSystem([can_if("can0", up=False, bitrate=None)], round_sp=0.8)
res = hwconfig.apply([cfg], fake)
if res[0][1] or "샘플 포인트 80.0 % (요청 87.5 %)" not in res[0][2]:
    fails.append(f"sample point mismatch should be reported {res}")
fake = FakeSystem([can_if("can0", up=False, bitrate=None)], helper_ok=False)
res = hwconfig.apply([cfg], fake)
if res[0][1] or "권한 설치" not in res[0][2]:
    fails.append(f"missing permission should point to the installer {res}")
if hwconfig.check(cfg, None) == [] or hwconfig.read_interfaces(lambda c, timeout=8: (1, "boom"))[1] != "boom":
    fails.append("error paths")

# ---- dialog
app = make_app([])
with tempfile.TemporaryDirectory() as d:
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, d)
    settings = QSettings("HolidayRobotics", "canmon-test")
    from qatools.tools.canmon.core import DBC_DIR
    fake = FakeSystem([can_if("can0", up=False, bitrate=None), can_if("can1", up=False, bitrate=None),
                       {"ifname": "vcan0", "flags": ["UP"], "link_type": "can", "linkinfo": {"info_kind": "vcan"}}])
    dlg = HardwareDialog(None, settings, DBC_DIR, runner=fake)
    if list(dlg.rows) != ["can0", "can1", "vcan0"] or not dlg.empty.isHidden() or dlg.rows["vcan0"]["bitrate"].isEnabled():
        fails.append(f"dialog rows {list(dlg.rows)}")
    w1 = dlg.rows["can1"]
    w1["preset"].setCurrentText("D-CAN (125 k / 1 M)")
    dlg._preset_picked("can1")
    if (w1["bitrate"].value(), w1["sp"].value(), w1["dbitrate"].value(), w1["sp"].text()) != (125000, 0.0, 1000000, "자동"):
        fails.append(f"preset fill {w1['bitrate'].value()} {w1['sp'].text()}")
    dlg.rows["can0"]["bitrate"].setValue(500000)
    if dlg.rows["can0"]["preset"].currentText() != hwconfig.CUSTOM:
        fails.append("editing a value should switch the preset to custom")
    dlg.rows["can0"]["bitrate"].setValue(1000000)
    dbc0 = str(DBC_DIR / "HETC-L-TACT_ch0.dbc")
    dlg.rows["can0"]["dbc"].setCurrentIndex(dlg.rows["can0"]["dbc"].findData(dbc0))
    dlg.save_profile("L-TACT 측정")
    results = dlg.apply()
    ups = [c[3:] for c in fake.calls if c[0] == "sudo"]
    if ups != [["up", "can0", "1000000", "0.875", "5000000", "0.625", "on"], ["up", "can1", "125000", "0", "1000000", "0", "on"]]:
        fails.append(f"dialog commands {ups}")
    if not all(ok for _, ok, _ in results) or "✔ can0" not in dlg.log.toPlainText() or "켜짐" not in dlg.table.item(0, 10).text():
        fails.append(f"dialog apply {results} {dlg.table.item(0, 10).text()}")
    if settings.value("map/can0") != dbc0 or json.loads(settings.value("hw/config"))["can1"]["bitrate"] != 125000:
        fails.append("settings not stored")
    dlg.show()
    app.processEvents()
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
        dlg.grab().save(str(shots / "9_hardware.png"))
    # a new dialog restores the stored values; a profile brings back its own
    dlg2 = HardwareDialog(None, settings, DBC_DIR, runner=fake)
    if dlg2.rows["can1"]["bitrate"].value() != 125000 or dlg2.rows["can0"]["dbc"].currentData() != dbc0:
        fails.append("stored config not restored")
    dlg2.rows["can0"]["bitrate"].setValue(250000)
    dlg2.load_profile("L-TACT 측정")
    if dlg2.rows["can0"]["bitrate"].value() != 1000000 or "적용" not in dlg2.log.toPlainText():
        fails.append("profile load")
    dlg2.profile.setCurrentText("L-TACT 측정")
    dlg2.delete_profile()
    if json.loads(settings.value("hw/profiles")) != {}:
        fails.append("profile delete")
    # no devices
    dlg3 = HardwareDialog(None, settings, DBC_DIR, runner=FakeSystem([{"ifname": "eth0", "link_type": "ether"}]))
    dlg3.show()
    app.processEvents()
    if dlg3.rows or dlg3.empty.isHidden() or dlg3.b_apply.isEnabled():
        fails.append("empty state")
    if shots:
        dlg3.grab().save(str(shots / "10_hardware_empty.png"))

print("FAILED:\n" + "\n".join(fails) if fails else "ALL CHECKS PASSED")
sys.exit(1 if fails else 0)
