"""Print the saving report for a ledger file.

    python -m provider.report path/to/ledger.jsonl [--since-hours N]

With no path, the ledger named by ``AIPROVIDER_LEDGER`` (or the config's ``ledger``).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .accounting import Ledger, report
from .config import load


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m provider.report")
    parser.add_argument("ledger", nargs="?", type=Path)
    parser.add_argument("--since-hours", type=float, default=None)
    args = parser.parse_args(argv)
    path = args.ledger or load().ledger_path
    if path is None or not path.is_file():
        print("no ledger: pass a path, or set AIPROVIDER_LEDGER", file=sys.stderr)
        return 2
    ledger = Ledger(path)
    entries = ledger.entries if args.since_hours is None else ledger.since(time.time() - args.since_hours * 3600)
    print(report(entries).render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
