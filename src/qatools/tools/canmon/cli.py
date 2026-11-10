"""qa canmon: CAN monitor on the command line.

  qa canmon view [capture.csv] [--map can0=HETC-L-TACT_ch0 ...]    open the CAN monitor window on its own
  qa canmon setup <iface> <local|dcan>                             bring a channel up, listen-only (needs sudo)
  qa canmon capture can0 can1 -o capture.csv [--seconds 60]        record frames to CSV
  qa canmon analyze capture.csv --map can0=... [-o report.md]      period report, or --decode <id> to export signals
  qa canmon sample sample.csv [--seconds 20]                       write a synthetic capture for trying the tools

Each command explains its options with -h.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HW = Path(__file__).resolve().parent / "hw"


def view(argv):
    from .ui.main import MainWindow, make_app, parse_args
    parse_args(argv)                    # report a bad command line before any window opens
    app = make_app(sys.argv[:1])
    win = MainWindow()
    win.show()
    win.open_args(argv)
    return app.exec()


def setup(argv):
    if len(argv) != 2 or argv[0].startswith("-"):
        print("usage: sudo qa canmon setup <iface> <local|dcan>", file=sys.stderr)
        return 2
    return subprocess.run(["bash", str(HW / "can_setup.sh"), *argv]).returncode


def capture(argv):
    from . import capture as mod
    return mod.main(argv)


def analyze(argv):
    from . import analyze as mod
    return mod.main(argv)


def sample(argv):
    from . import sample as mod
    return mod.main(argv)


COMMANDS = {"view": view, "setup": setup, "capture": capture, "analyze": analyze, "sample": sample}


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    run = COMMANDS.get(argv[0])
    if run is None:
        print(f"qa canmon: unknown command {argv[0]!r}\n\n{__doc__.strip()}", file=sys.stderr)
        return 2
    return run(argv[1:]) or 0
