"""Application setup shared by the shell and by tools that also run on their own: font, icon, app id."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QColor, QFont, QFontDatabase, QIcon, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication

APP_ID = "qa-tools"               # matches qa-tools.desktop, so the taskbar shows the logo
LOGO = Path(__file__).resolve().parent / "logo.svg"
KOREAN_FONTS = ["/mnt/c/Windows/Fonts/malgun.ttf", "C:/Windows/Fonts/malgun.ttf",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"]


def setup_font(app):
    """Make sure Korean text renders. WSL often has no Korean font installed, so fall back to the Windows one."""
    if QFontDatabase.families(QFontDatabase.WritingSystem.Korean):
        return
    for path in KOREAN_FONTS:
        if Path(path).exists():
            fid = QFontDatabase.addApplicationFont(path)
            names = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
            if names:
                f = QFont(names[0])
                f.setPointSize(10)
                app.setFont(f)
                return


def app_icon():
    """Build the window and taskbar icon from the Holiday logo at a few sizes.

    Rendered to pixmaps here because WSLg's taskbar does not pick up SVG icons reliably on its own.
    """
    icon = QIcon()
    if not LOGO.exists():
        return icon
    from PyQt6.QtSvg import QSvgRenderer
    renderer = QSvgRenderer(str(LOGO))
    if not renderer.isValid():
        return icon
    for size in (16, 24, 32, 48, 64, 128, 256):
        img = QImage(size, size, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        painter = QPainter(img)
        renderer.render(painter)
        painter.end()
        icon.addPixmap(QPixmap.fromImage(img))
    return icon


def make_app(argv=None, name="QA Tools"):
    app = QApplication.instance() or QApplication(argv or [])
    app.setApplicationName(name)
    app.setDesktopFileName(APP_ID)
    app.setWindowIcon(app_icon())
    setup_font(app)
    return app
