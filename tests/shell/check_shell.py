#!/usr/bin/env python3
"""Offscreen test of the QA Tools shell. Usage: python3 test_shell.py [screenshot_dir]"""
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

from PyQt6.QtCore import QSettings  # noqa: E402
from PyQt6.QtWidgets import QLabel, QMainWindow  # noqa: E402

from qatools.shell.app import make_app  # noqa: E402
from qatools.registry import TOOLS, Tool
from qatools.shell.window import ErrorPage, ShellWindow  # noqa: E402

shots = Path(sys.argv[1]) if len(sys.argv) > 1 else None
fails = []


def check(ok, what):
    if not ok:
        fails.append(what)


app = make_app([])
QSettings.setDefaultFormat(QSettings.Format.IniFormat)


class Probe(QLabel):
    """A minimal second tool: records that the shell told it to shut down."""
    stopped = 0

    def shutdown(self):
        Probe.stopped += 1


def broken():
    raise RuntimeError("tool cannot start")


with tempfile.TemporaryDirectory() as d:
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, d)
    QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, d)
    sample = Path(d) / "sample.csv"
    sample_tool.main([str(sample), "--seconds", "3"])

    check([t.key for t in TOOLS] == ["canmon"], "registry should list canmon")
    built = []
    tools = list(TOOLS) + [Tool("probe", "Probe", "second tool", page=lambda: built.append(1) or Probe("probe page")),
                           Tool("broken", "Broken", "fails to start", page=broken),
                           Tool("clionly", "CLI only", "no window page yet", cli=lambda argv: 0)]
    win = ShellWindow(tools)
    check(win.list.count() == 3 and win.list.item(0).text() == "CAN Monitor", "tool list should hold the three tools with a page")
    check(not win.pages, "pages must be built on first use, not at start")

    # ---- CAN monitor as a page
    page = win.select()
    win.show()
    app.processEvents()
    check(win.current_key() == "canmon" and list(win.pages) == ["canmon"], "first tool selected by default")
    check(isinstance(page, QMainWindow) and not page.isWindow() and page.isVisible(), "CAN monitor should be embedded, not its own window")
    check(win.windowTitle() == "QA Tools · FM26 CAN Monitor", f"title {win.windowTitle()!r}")
    check(page.open_args([str(sample), "--map", "can0=HETC-L-TACT_ch0", "--map", "can1=HETC-L-TACT_ch1"]), "open_args")
    app.processEvents()
    check(page.state.session is not None and page.state.session.cap.n > 0, "capture not loaded")
    check(win.windowTitle() == "QA Tools · FM26 CAN Monitor — sample.csv", f"title after open {win.windowTitle()!r}")
    check(page.menuBar().isVisible() and page.statusBar().isVisible() and page.dock.isVisible(), "page keeps its menu, status bar and dock")
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
        win.grab().save(str(shots / "shell_canmon.png"))

    # ---- switching tools keeps pages alive
    check(page.start_demo() and page.live is not None, "preview should start")
    probe = win.select("probe")
    app.processEvents()
    check(isinstance(probe, Probe) and built == [1] and win.stack.currentWidget() is probe, "second tool page")
    check(win.windowTitle() == "QA Tools · Probe", f"title on second tool {win.windowTitle()!r}")
    check(page.live is not None, "measurement must keep running while another tool is shown")
    page.setWindowTitle("changed in the background")
    check(win.windowTitle() == "QA Tools · Probe", "a hidden tool must not change the window title")
    check(win.select("canmon") is page and built == [1], "pages are built once")

    # ---- a tool that fails to start
    err = win.select("broken")
    app.processEvents()
    check(isinstance(err, ErrorPage) and "tool cannot start" in err.detail, "broken tool should show its error")
    check(win.select("probe") is probe, "other tools stay usable after a failure")
    try:
        win.select("nope")
        fails.append("unknown tool accepted")
    except ValueError:
        pass

    # ---- closing: every page is told to stop, the last tool is remembered
    quit_action = [a for m in page.menuBar().actions() for a in m.menu().actions() if a.text() == "끝내기"][0]
    quit_action.trigger()
    app.processEvents()
    check(not win.isVisible(), "끝내기 in the CAN monitor menu should close the shell")
    check(page.live is None, "closing the shell must stop the measurement")
    check(Probe.stopped == 1, "shutdown not called on the second tool")
    win2 = ShellWindow(tools)
    win2.select()
    check(win2.current_key() == "probe", f"last tool not remembered: {win2.current_key()!r}")
    win2.close()

    # ---- command line
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src")] + sys.path))
    qa = [sys.executable, "-m", "qatools"]
    out = subprocess.run(qa + ["list"], capture_output=True, text=True, cwd=HERE, env=env)
    check(out.returncode == 0 and out.stdout.startswith("canmon") and "[cli + gui]" in out.stdout, f"list: {out.stdout!r} {out.stderr[-200:]!r}")
    for bad in (["nope"], ["gui", "nope"], ["canmon", "nope"], ["canmon"]):
        out = subprocess.run(qa + bad, capture_output=True, text=True, cwd=HERE, env=env)
        check(out.returncode == 2, f"qa {bad} should be refused, got {out.returncode}")
    out = subprocess.run(qa + ["canmon", "-h"], capture_output=True, text=True, cwd=HERE, env=env)
    check(out.returncode == 0 and "qa canmon capture" in out.stdout, "qa canmon -h should list the commands")
    out = subprocess.run(qa + ["canmon", "sample", str(Path(d) / "s.csv"), "--seconds", "1"], capture_output=True, text=True, cwd=HERE, env=env)
    check(out.returncode == 0 and (Path(d) / "s.csv").stat().st_size > 1000, f"qa canmon sample: {out.stderr[-200:]!r}")
    out = subprocess.run(qa + ["canmon", "analyze", str(Path(d) / "s.csv"), "--map", "can1=HETC-L-TACT_ch1"], capture_output=True, text=True, cwd=HERE, env=env)
    check(out.returncode == 0 and "ANGLE_MULTI" in out.stdout, f"qa canmon analyze: {out.stderr[-200:]!r}")
    for args in (["gui", "canmon"], ["canmon", "view"]):
        out = subprocess.run(qa + args + ["x.csv", "--map", "can0"], capture_output=True, text=True, cwd=HERE, env=env)
        check(out.returncode == 2 and "IFACE=DBC" in out.stderr, f"bad --map via {args}: {out.returncode} {out.stderr[-200:]!r}")
    out = subprocess.run(qa + ["gui", "canmon", "x.csv", "--map", "can0"], capture_output=True, text=True, cwd=HERE, env=env)
    check(out.returncode == 2 and "IFACE=DBC" in out.stderr, f"bad --map: {out.returncode} {out.stderr[-200:]!r}")

print("FAILED:\n" + "\n".join(fails) if fails else "ALL CHECKS PASSED")
sys.exit(1 if fails else 0)
