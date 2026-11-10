"""Hardware access: install the passwordless CAN setup helper from the UI, and check whether it is in place."""
from __future__ import annotations

import getpass
import re
import shutil
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import QProcess
from PyQt6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QPlainTextEdit,
                             QPushButton, QVBoxLayout)

HELPER = Path("/usr/local/sbin/canmon-setup")
INSTALLER = Path(__file__).resolve().parents[1] / "hw" / "install_can_access.sh"
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def access_status():
    """Return (state, text). state is "ok", "missing", "not_allowed" or "unsupported"."""
    if not sys.platform.startswith("linux"):
        return "unsupported", "CAN 측정은 Linux에서만 됩니다. 이 PC에서는 로그 파일 보기만 쓸 수 있습니다."
    if not HELPER.exists():
        return "missing", "설치되지 않았습니다."
    try:
        ok = subprocess.run(["sudo", "-n", "-l", str(HELPER)], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if ok:
        return "ok", "설치되어 있습니다. 암호 없이 CAN 속도를 설정할 수 있습니다."
    return "not_allowed", "설치는 되어 있지만 지금 사용자에게 권한이 없습니다. 사용자를 등록하거나, 등록 직후라면 로그아웃했다가 다시 로그인하세요."


def parse_users(text):
    """Split and check user names. Raises ValueError with a message for the user."""
    users = text.replace(",", " ").split()
    if not users:
        raise ValueError("등록할 사용자 이름을 입력하세요.")
    bad = [u for u in users if not USER_RE.match(u)]
    if bad:
        raise ValueError(f"사용자 이름이 올바르지 않습니다: {', '.join(bad)}")
    return users


def install_command(users, pkexec=None):
    """Command that runs the installer with a graphical password prompt, or None when pkexec is not available."""
    pkexec = pkexec if pkexec is not None else shutil.which("pkexec")
    if not pkexec:
        return None
    return [pkexec, str(INSTALLER)] + list(users)


def manual_command(users):
    return f"sudo {INSTALLER} {' '.join(users)}"


class AccessDialog(QDialog):
    """One-time setup so that later bit-rate changes need no password."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CAN 설정 권한 설치")
        self.resize(640, 420)
        self.proc = None
        intro = QLabel("측정용 노트북에서 <b>한 번만</b> 설치하면, 이후에는 암호 없이 CAN 속도를 설정할 수 있습니다. "
                       "설치할 때만 관리자 암호를 한 번 묻습니다.<br><br>"
                       "설치하면 등록한 사용자는 CAN 장치의 속도, 샘플 포인트, 수신 전용 여부만 바꿀 수 있는 "
                       "스크립트 하나를 암호 없이 실행할 수 있게 됩니다.")
        intro.setWordWrap(True)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.users = QLineEdit(getpass.getuser())
        self.users.setPlaceholderText("여러 명이면 띄어쓰기로 구분")
        form = QFormLayout()
        form.addRow("현재 상태", self.status)
        form.addRow("등록할 사용자", self.users)
        self.b_install = QPushButton("설치")
        self.b_copy = QPushButton("터미널 명령 복사")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("설치 결과가 여기에 표시됩니다.")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.addButton(self.b_install, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.b_copy, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        lay.addLayout(form)
        lay.addWidget(self.log, 1)
        lay.addWidget(buttons)
        self.b_install.clicked.connect(self.install)
        self.b_copy.clicked.connect(self.copy_command)
        self.refresh()

    def refresh(self):
        state, text = access_status()
        color = {"ok": "#2b8455", "unsupported": "#5b6978"}.get(state, "#a56a12")
        self.status.setText(f"<span style='color:{color}; font-weight:600'>{text}</span>")
        self.b_install.setEnabled(state != "unsupported")
        self.b_copy.setEnabled(state != "unsupported")
        return state

    def _users(self):
        try:
            return parse_users(self.users.text())
        except ValueError as e:
            self.log.setPlainText(str(e))
            return None

    def copy_command(self):
        users = self._users()
        if users:
            QApplication.clipboard().setText(manual_command(users))
            self.log.setPlainText(f"클립보드에 복사했습니다. 터미널에 붙여 넣어 실행하세요.\n\n{manual_command(users)}")

    def install(self):
        users = self._users()
        if not users:
            return
        cmd = install_command(users)
        if cmd is None:
            self.log.setPlainText("이 PC에는 암호 창을 띄우는 프로그램(pkexec)이 없어 여기서 설치할 수 없습니다.\n"
                                  f"터미널에서 아래 명령을 실행하세요.\n\n{manual_command(users)}")
            return
        self.b_install.setEnabled(False)
        self.log.setPlainText("관리자 암호 창이 뜨면 입력하세요…")
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.finished.connect(self._finished)
        self.proc.errorOccurred.connect(lambda _e: self._finished(-1))
        self.proc.start(cmd[0], cmd[1:])

    def _finished(self, code, _status=None):
        if self.proc is None:
            return
        out = bytes(self.proc.readAll()).decode(errors="replace").strip()
        self.proc = None
        if code == 0:
            self.log.setPlainText(f"설치했습니다. 한 번 로그아웃했다가 다시 로그인해야 적용됩니다.\n\n{out}")
        elif code in (126, 127):
            self.log.setPlainText(f"설치를 취소했거나 암호가 맞지 않습니다.\n\n{out}")
        else:
            self.log.setPlainText(f"설치하지 못했습니다 (코드 {code}).\n\n{out}")
        self.refresh()
