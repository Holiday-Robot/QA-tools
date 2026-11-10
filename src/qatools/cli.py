"""qa: command line of the QA tools.

  qa                          open the window on the tool used last
  qa gui [tool] [args ...]    open the window on a tool; the arguments go to that tool's page
  qa list                     print the tools
  qa <tool> [args ...]        run a tool on the command line (qa <tool> -h lists its commands)
"""
from __future__ import annotations

import sys

from .registry import TOOLS, find, load


def usage(file=sys.stdout):
    print(__doc__.strip(), file=file)
    print("\ntools:", file=file)
    for t in TOOLS:
        print(f"  {t.key:12} {t.summary}", file=file)


def fail(text):
    print(f"qa: {text}", file=sys.stderr)
    usage(sys.stderr)
    return 2


def gui(argv):
    key, rest = (argv[0], argv[1:]) if argv else (None, [])
    pages = [t for t in TOOLS if t.page is not None]
    if key is not None and find(key, pages) is None:
        return fail(f"no tool with a window page named {key!r}")
    from .shell.app import make_app
    from .shell.window import ErrorPage, ShellWindow
    app = make_app(sys.argv[:1])
    win = ShellWindow(pages)
    page = win.select(key)
    if rest:
        if isinstance(page, ErrorPage):
            print(page.detail, file=sys.stderr)
            return 1
        if not hasattr(page, "open_args"):
            return fail(f"the {key} page takes no arguments")
        page.open_args(rest)
    win.show()
    return app.exec()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return gui([])
    head, rest = argv[0], argv[1:]
    if head in ("-h", "--help", "help"):
        usage()
        return 0
    if head == "list":
        for t in TOOLS:
            kinds = " + ".join(k for k, on in (("cli", t.cli), ("gui", t.page)) if on)
            print(f"{t.key:12} {t.title}: {t.summary}  [{kinds}]")
        return 0
    if head == "gui":
        return gui(rest)
    tool = find(head)
    if tool is None:
        return fail(f"unknown tool or command {head!r}")
    if tool.cli is None:
        return fail(f"{head} has no command line yet")
    return load(tool.cli)(rest) or 0


if __name__ == "__main__":
    sys.exit(main())
