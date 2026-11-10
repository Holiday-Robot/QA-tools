"""CAN hardware configuration without Qt: find interfaces, build setup commands, check what the driver accepted."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

HELPER = "/usr/local/sbin/canmon-setup"
# Profiles kept in the repository, shared by every PC that uses this checkout. Profiles saved from the dialog stay per user.
PROFILES_FILE = Path(os.environ.get("CANMON_PROFILES") or Path(__file__).resolve().parents[4] / "data" / "hw_profiles.json")
PRESETS = {
    "로컬 CAN (1 M / 5 M)": dict(bitrate=1_000_000, sample_point=0.875, dbitrate=5_000_000, dsample_point=0.625),
    "D-CAN (125 k / 1 M)": dict(bitrate=125_000, sample_point=0.0, dbitrate=1_000_000, dsample_point=0.0),
}
CUSTOM = "직접 입력"
SP_TOLERANCE = 0.02


@dataclass
class ChannelConfig:
    """Requested settings of one interface. A sample point of 0 lets the driver choose."""
    iface: str
    enabled: bool = True
    bitrate: int = 1_000_000
    sample_point: float = 0.875
    dbitrate: int = 5_000_000
    dsample_point: float = 0.625
    listen_only: bool = True
    dbc: str = ""

    def preset(self):
        for name, p in PRESETS.items():
            if all(abs(getattr(self, k) - v) < 1e-9 for k, v in p.items()):
                return name
        return CUSTOM

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


@dataclass
class Interface:
    """What the system reports for one CAN interface."""
    name: str
    kind: str = "can"                 # "can" for real hardware, "vcan" for a virtual interface
    up: bool = False
    state: str = ""                   # ERROR-ACTIVE, ERROR-PASSIVE, BUS-OFF, STOPPED ...
    bitrate: int | None = None
    sample_point: float | None = None
    dbitrate: int | None = None
    dsample_point: float | None = None
    fd: bool = False
    listen_only: bool = False
    tx_err: int = 0
    rx_err: int = 0
    clock: int | None = None
    device: str = ""

    @property
    def configurable(self):
        return self.kind == "can"

    def summary(self):
        if not self.configurable:
            return "가상 장치 (속도 설정 없음)"
        if self.bitrate is None:
            return "설정 전"
        rate = fmt_rate(self.bitrate) + (f" / {fmt_rate(self.dbitrate)}" if self.dbitrate else "")
        mode = "수신 전용" if self.listen_only else "송신 가능"
        return f"{'켜짐' if self.up else '꺼짐'} · {self.state or '-'} · {rate} · {mode} · 오류 카운터 tx {self.tx_err} rx {self.rx_err}"


def fmt_rate(bps):
    return f"{bps / 1e6:g} M" if bps >= 1_000_000 else f"{bps / 1e3:g} k"


def _num(v, cast):
    try:
        return cast(v)
    except (TypeError, ValueError):
        return None


def describe_device(name, sys_root="/sys/class/net"):
    """USB product name and serial of the adapter behind an interface, when sysfs has them."""
    base = Path(sys_root) / name / "device"
    parts = []
    try:
        usb = (base / "..").resolve()
        for f in ("product", "serial"):
            p = usb / f
            if p.is_file():
                parts.append(p.read_text().strip())
        drv = base / "driver"
        if drv.exists():
            parts.append(f"드라이버 {drv.resolve().name}")
        port = Path(sys_root) / name / "dev_port"
        if port.is_file():
            parts.append(f"채널 {int(port.read_text().strip()) + 1}")
    except (OSError, ValueError):
        pass
    return " · ".join(parts)


def parse_interfaces(text, sys_root="/sys/class/net"):
    """Parse `ip -details -json link show` output into Interface objects (CAN and virtual CAN only)."""
    out = []
    for item in json.loads(text or "[]"):
        info = item.get("linkinfo") or {}
        kind = info.get("info_kind")
        if kind not in ("can", "vcan") and item.get("link_type") != "can":
            continue
        data = info.get("info_data") or {}
        bt, dbt = data.get("bittiming") or {}, data.get("data_bittiming") or {}
        modes = [str(m).upper() for m in (data.get("ctrlmode") or [])]
        berr = data.get("berr_counter") or {}
        out.append(Interface(
            name=item.get("ifname", "?"), kind="vcan" if kind == "vcan" else "can",
            up="UP" in (item.get("flags") or []), state=str(data.get("state") or ""),
            bitrate=_num(bt.get("bitrate"), int), sample_point=_num(bt.get("sample_point"), float),
            dbitrate=_num(dbt.get("bitrate"), int), dsample_point=_num(dbt.get("sample_point"), float),
            fd="FD" in modes, listen_only="LISTEN-ONLY" in modes,
            tx_err=_num(berr.get("tx"), int) or 0, rx_err=_num(berr.get("rx"), int) or 0,
            clock=_num(data.get("clock"), int), device=describe_device(item.get("ifname", ""), sys_root)))
    return sorted(out, key=lambda i: i.name)


def load_shared_profiles(path=None):
    """Profiles from the repository file: {name: {iface: ChannelConfig dict}}. A missing or broken file gives {}."""
    try:
        data = json.loads(Path(path or PROFILES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def run(cmd, timeout=8):
    """Run a command; return (exit code, combined output). Missing programs give code 127."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except FileNotFoundError as e:
        return 127, str(e)
    except subprocess.TimeoutExpired:
        return 124, "시간 초과"


def read_interfaces(runner=run):
    """List CAN interfaces. Returns (interfaces, error text or "")."""
    code, out = runner(["ip", "-details", "-statistics", "-json", "link", "show"])
    if code != 0:
        return [], out or "ip 명령을 실행하지 못했습니다."
    try:
        return parse_interfaces(out), ""
    except (ValueError, TypeError) as e:
        return [], f"ip 출력 해석 실패: {e}"


def up_command(cfg):
    return ["sudo", "-n", HELPER, "up", cfg.iface, str(int(cfg.bitrate)), f"{cfg.sample_point:g}", str(int(cfg.dbitrate)),
            f"{cfg.dsample_point:g}", "on" if cfg.listen_only else "off"]


def down_command(iface):
    return ["sudo", "-n", HELPER, "down", iface]


def check(cfg, info):
    """Differences between what was asked for and what the driver reports. Empty list means it matches."""
    if info is None:
        return [f"{cfg.iface}: 장치를 찾을 수 없습니다."]
    diffs = []
    if not info.up:
        diffs.append("채널이 켜지지 않았습니다.")
    if info.bitrate != cfg.bitrate:
        diffs.append(f"중재 속도 {info.bitrate} bps (요청 {cfg.bitrate})")
    if info.dbitrate != cfg.dbitrate:
        diffs.append(f"데이터 속도 {info.dbitrate} bps (요청 {cfg.dbitrate})")
    for label, want, got in (("샘플 포인트", cfg.sample_point, info.sample_point),
                             ("데이터 샘플 포인트", cfg.dsample_point, info.dsample_point)):
        if want and (got is None or abs(got - want) > SP_TOLERANCE):
            diffs.append(f"{label} {'-' if got is None else f'{got * 100:.1f} %'} (요청 {want * 100:.1f} %)")
    if info.listen_only != cfg.listen_only:
        diffs.append("수신 전용 설정이 요청과 다릅니다.")
    if not info.fd:
        diffs.append("CAN FD가 켜지지 않았습니다.")
    return diffs


def apply(configs, runner=run):
    """Configure the enabled interfaces, then read them back. Returns [(iface, ok, message)]."""
    results = []
    for cfg in configs:
        if not cfg.enabled:
            continue
        code, out = runner(up_command(cfg))
        if code != 0:
            hint = ""
            if code == 127 or "command not found" in out or "password is required" in out or "not allowed" in out:
                hint = " 메뉴 '도구 → CAN 설정 권한 설치…'를 먼저 실행하세요."
            results.append((cfg.iface, False, f"설정 실패 (코드 {code}): {out or '출력 없음'}.{hint}"))
            continue
        infos, err = read_interfaces(runner)
        info = next((i for i in infos if i.name == cfg.iface), None)
        diffs = [err] if err else check(cfg, info)
        if diffs:
            results.append((cfg.iface, False, "적용했지만 요청과 다릅니다: " + "; ".join(diffs)))
        else:
            sp = f"{info.sample_point * 100:.1f} %" if info.sample_point is not None else "-"
            dsp = f"{info.dsample_point * 100:.1f} %" if info.dsample_point is not None else "-"
            results.append((cfg.iface, True, f"적용됨: {info.bitrate:,} bps ({sp}) / {info.dbitrate:,} bps ({dsp}), "
                                             f"{'수신 전용' if info.listen_only else '송신 가능'}, 상태 {info.state or '-'}"))
    return results
