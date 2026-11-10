#!/usr/bin/env python3
"""Run every check script under tests/. Each script prints what failed and exits non-zero.

  python3 tests/test_checks.py            run all, print a summary
  python3 tests/test_checks.py canmon     only the scripts whose path contains "canmon"
  pytest                                  the same scripts, one test each

The scripts run offscreen and need no CAN hardware.
"""
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = sorted(HERE.glob("*/check_*.py"))


def run(script):
    env = dict(os.environ, QT_QPA_PLATFORM=os.environ.get("QT_QPA_PLATFORM", "offscreen"))
    return subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env, timeout=600)


def _test(script):
    def test():
        out = run(script)
        assert out.returncode == 0, (out.stdout + out.stderr)[-3000:]
    return test


for _s in SCRIPTS:
    globals()[f"test_{_s.parent.name}_{_s.stem[6:]}"] = _test(_s)


if __name__ == "__main__":
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    failed = 0
    for s in SCRIPTS:
        name = f"{s.parent.name}/{s.name}"
        if want not in name:
            continue
        out = run(s)
        print(f"{'ok  ' if out.returncode == 0 else 'FAIL'}  {name}")
        if out.returncode:
            failed += 1
            print((out.stdout + out.stderr)[-3000:])
    sys.exit(1 if failed else 0)
