"""Run every simulation in order and say which held.

    python sim/run_all.py            # all of them
    python sim/run_all.py s03 s05    # the ones whose names start with these

Offline: no network, no subscription, no key. Each simulation prints what it measured;
this prints a one-line verdict per simulation and exits non-zero if any claim failed.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main(prefixes: list[str]) -> int:
    scripts = sorted(p for p in HERE.glob("s[0-9][0-9]_*.py") if not prefixes or p.name.startswith(tuple(prefixes)))
    verdicts = []
    for script in scripts:
        started = time.perf_counter()
        print(f"\n######## {script.stem}", flush=True)
        code = subprocess.call([sys.executable, str(script)], cwd=HERE)
        verdicts.append((script.stem, code, time.perf_counter() - started))
    print("\n######## summary")
    for name, code, seconds in verdicts:
        print(f"  {name:<32} {'held' if code == 0 else 'FAILED':<8} {seconds:6.1f}s")
    return 0 if all(code == 0 for _, code, _ in verdicts) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
