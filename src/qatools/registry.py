"""The tools of this repository. Adding a tool is one line in TOOLS.

A tool starts as a command line (cli) and gets a window page (page) once it is worth porting.
Both are given as "module:name" and imported only when used, so the command line does not need Qt
and a tool with a missing package cannot stop the others from starting.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Callable, Optional, Union

Spec = Union[str, Callable, None]


@dataclass(frozen=True)
class Tool:
    key: str                # name on the command line: qa <key> ...
    title: str              # shown in the tool list of the window
    summary: str            # one line for `qa list` and the tooltip
    cli: Spec = None        # main(argv) -> exit code; handles `qa <key> <args>`
    page: Spec = None       # builds the page widget; a tool without one is command line only


def load(spec):
    """Turn "package.module:name" into the object it names. A callable is returned as it is."""
    if callable(spec):
        return spec
    module, name = spec.split(":")
    return getattr(import_module(module), name)


TOOLS = [
    Tool("canmon", "CAN Monitor", "CAN FD 버스 기록, 메시지 주기와 신호 분석, 시험용 메시지 송신",
         cli="qatools.tools.canmon.cli:main", page="qatools.tools.canmon.ui.main:MainWindow"),
]


def find(key, tools: Optional[list] = None):
    for tool in tools if tools is not None else TOOLS:
        if tool.key == key:
            return tool
    return None
