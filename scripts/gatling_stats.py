#!/usr/bin/env python3
"""Print per-request statistics from a Gatling HTML report folder (one line per request name).

Usage: python3 scripts/gatling_stats.py results/gatling/<run-folder>
"""
import glob
import html as html_lib
import os
import re
import sys


def cell(page, title):
    match = re.search(r'<td class="title">' + re.escape(title) + r'</td>\s*<td class="total">([^<]*)</td>', page)
    return match.group(1).strip() if match else "-"


def main(folder):
    rows = []
    for path in glob.glob(os.path.join(folder, "req_*.html")):
        page = open(path, encoding="utf-8").read()
        name = re.search(r"<title>Gatling Stats - ([^<]+)</title>", page)
        name = html_lib.unescape(name.group(1)) if name else os.path.basename(path)
        count = re.search(r'<td class="title">Total count</td>\s*<td class="total">([^<]*)</td>\s*'
                          r'<td class="ok">([^<]*)</td>\s*<td class="ko">([^<]*)</td>', page)
        rows.append((name,
                     count.group(1) if count else "-", count.group(3) if count else "-",
                     cell(page, "Mean"), cell(page, "50th percentile"),
                     cell(page, "95th percentile"), cell(page, "99th percentile"),
                     cell(page, "Max"), cell(page, "Mean count/s")))
    print(f"{'request':38s} {'count':>6s} {'ko':>4s} {'mean':>6s} {'p50':>6s} {'p95':>6s} {'p99':>6s} {'max':>6s} {'rps':>6s}")
    for row in sorted(rows):
        print(f"{row[0]:38s} {row[1]:>6s} {row[2]:>4s} {row[3]:>6s} {row[4]:>6s} {row[5]:>6s} {row[6]:>6s} {row[7]:>6s} {row[8]:>6s}")


if __name__ == "__main__":
    main(sys.argv[1])
