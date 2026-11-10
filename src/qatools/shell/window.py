"""Shell window: a list of tools on the left, the selected tool's page on the right.

A tool page is any QWidget. A QMainWindow works too and keeps its own menu, toolbar, dock and status bar
inside the page. Pages are built the first time their tool is selected and then kept, so a measurement
keeps running while another tool is shown.

A page may offer two optional methods:
  shutdown()          called when the shell closes; stop threads, close devices and files here
  open_args(argv)     handle the arguments given after the tool name on the command line
"""
from __future__ import annotations

import traceback

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QPlainTextEdit,
                             QStackedWidget, QVBoxLayout, QWidget)

from ..registry import load
from .app import app_icon

NAME = "QA Tools"
RAIL_WIDTH = 150
RAIL_STYLE = """
#rail { background: #eef2f5; border-right: 1px solid #dbe1e7; }
#rail QLabel#brand { color: #1c2530; font-weight: 700; font-size: 14px; padding: 12px 12px 8px 12px; }
#rail QListWidget { background: transparent; border: 0; outline: 0; color: #1c2530; }
#rail QListWidget::item { padding: 9px 12px; }
#rail QListWidget::item:hover { background: #e1eef6; }
#rail QListWidget::item:selected { background: #1f6f9f; color: #ffffff; }
"""


class ErrorPage(QWidget):
    """Shown in place of a tool that could not be built, so the other tools stay usable."""

    def __init__(self, tool, detail):
        super().__init__()
        self.detail = detail
        box = QVBoxLayout(self)
        box.addWidget(QLabel(f"<b>{tool.title}</b>을(를) 열지 못했습니다. 아래 오류를 확인하세요."))
        text = QPlainTextEdit(detail)
        text.setReadOnly(True)
        box.addWidget(text, 1)


class ShellWindow(QMainWindow):
    def __init__(self, tools):
        super().__init__()
        self.tools = [t for t in tools if t.page is not None]     # the others are command line only
        self.pages = {}
        self.settings = QSettings("HolidayRobotics", "qa-tools")
        self.setWindowIcon(app_icon())
        self.setWindowTitle(NAME)
        self.resize(1760, 950)

        self.list = QListWidget()
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for tool in self.tools:
            item = QListWidgetItem(tool.title)
            item.setToolTip(tool.summary)
            item.setData(Qt.ItemDataRole.UserRole, tool.key)
            self.list.addItem(item)
        brand = QLabel(NAME)
        brand.setObjectName("brand")
        self.rail = QFrame()
        self.rail.setObjectName("rail")
        self.rail.setFixedWidth(RAIL_WIDTH)
        self.rail.setStyleSheet(RAIL_STYLE)
        rl = QVBoxLayout(self.rail)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rl.addWidget(brand)
        rl.addWidget(self.list, 1)

        self.stack = QStackedWidget()
        center = QWidget()
        row = QHBoxLayout(center)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self.rail)
        row.addWidget(self.stack, 1)
        self.setCentralWidget(center)

        self.list.currentRowChanged.connect(self._row_changed)
        geometry = self.settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

    # ------------------------------------------------------------ tools
    def tool(self, key):
        for tool in self.tools:
            if tool.key == key:
                return tool
        raise KeyError(key)

    def current_key(self):
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def select(self, key=None):
        """Show a tool and return its page. Without a key: the tool used last, or the first one."""
        keys = [t.key for t in self.tools]
        if key is None:
            key = self.settings.value("last_tool", "")
            if key not in keys:
                key = keys[0]
        self.list.setCurrentRow(keys.index(key))      # raises ValueError for an unknown key
        return self.pages[key]

    def _row_changed(self, row):
        if row < 0:
            return
        tool = self.tools[row]
        page = self.pages.get(tool.key)
        if page is None:
            page = self._build(tool)
            self.pages[tool.key] = page
            self.stack.addWidget(page)
            page.windowTitleChanged.connect(lambda _t, k=tool.key: self._update_title() if k == self.current_key() else None)
        self.stack.setCurrentWidget(page)
        self.settings.setValue("last_tool", tool.key)
        self._update_title()

    def _build(self, tool):
        try:
            page = load(tool.page)()
        except Exception:  # noqa: BLE001 - one broken tool must not take the shell down
            return ErrorPage(tool, traceback.format_exc())
        if isinstance(page, QMainWindow):
            page.setWindowFlags(Qt.WindowType.Widget)     # a main window used as a page, not as its own window
        return page

    def _update_title(self):
        key = self.current_key()
        if key is None:
            self.setWindowTitle(NAME)
            return
        self.setWindowTitle(f"{NAME} · {self.pages[key].windowTitle() or self.tool(key).title}")

    # ------------------------------------------------------------ closing
    def closeEvent(self, event):
        for page in self.pages.values():
            stop = getattr(page, "shutdown", None)
            if stop is not None:
                try:
                    stop()
                except Exception:  # noqa: BLE001 - keep closing the other tools
                    traceback.print_exc()
        self.settings.setValue("geometry", self.saveGeometry())
        super().closeEvent(event)
