#!/usr/bin/env python3
"""Print a coverage summary from an lcov.info file."""

import sys


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "coverage/lcov.info"
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return

    files = {}
    current = None
    for line in lines:
        line = line.strip()
        if line.startswith("SF:"):
            current = line[3:]
        elif line.startswith("LH:"):
            files.setdefault(current, {})["hit"] = int(line[3:])
        elif line.startswith("LF:"):
            files.setdefault(current, {})["total"] = int(line[3:])

    if not files:
        return

    total_hit = total_lines = 0
    print()
    print(f"{'File':<55} {'Lines':>6} {'Hit':>6} {'Cov':>6}")
    print("-" * 75)
    for f in sorted(files.keys()):
        d = files[f]
        hit, total = d.get("hit", 0), d.get("total", 0)
        pct = (hit / total * 100) if total else 0
        total_hit += hit
        total_lines += total
        short = f.replace("lib/", "")
        print(f"{short:<55} {total:>6} {hit:>6} {pct:>5.1f}%")
    pct = (total_hit / total_lines * 100) if total_lines else 0
    print("-" * 75)
    print(f"{'TOTAL':<55} {total_lines:>6} {total_hit:>6} {pct:>5.1f}%")


if __name__ == "__main__":
    main()
