"""python -m navigator.conductor serve [--socket PATH] [--root DIR]"""
from __future__ import annotations

import sys
from pathlib import Path

from navigator.conductor.server import serve

ROOT = Path("/Users/Manoj/calwbot/logs/conductor")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] != "serve":
        print(__doc__, file=sys.stderr)
        return 2
    opt = lambda name, default: Path(argv[argv.index(name) + 1]) if name in argv else default
    serve(opt("--socket", ROOT / "conductor.sock"), opt("--root", ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
